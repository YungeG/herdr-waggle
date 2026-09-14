"""Explicit fresh Pi creation. Herdr owns the PTY; ambiguous calls are never replayed."""
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import time
from uuid import uuid4

from model import valid_uuid
from runtime import Agent, BridgeError, Runtime, bridge_entry, canonical_directory, private_dir, publish, read_json


@dataclass
class Launch:
    id: str
    session_id: str
    cwd: str
    name: str
    model: str
    state: str = "prepared"
    tab_id: str = ""
    pane_id: str = ""
    message: str = "创建意图已保存；尚未确认启动，不会自动重试。"

    def directory(self, runtime: Runtime) -> Path:
        if not valid_uuid(self.id):
            raise BridgeError("新建 Pi 身份无效。")
        return runtime.root / "launches" / self.id


def save_launch(runtime: Runtime, launch: Launch) -> None:
    directory = launch.directory(runtime)
    private_dir(directory)
    target = directory / "launch.json"
    if target.exists():
        read_json(target)  # Reject unsafe existing files, including symlinks.
    temporary = directory / f".{uuid4()}.json"
    try:
        publish(temporary, asdict(launch))
        os.replace(temporary, target)
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def prepare_launch(runtime: Runtime, directory: str, name: str, model: str, launch_id: str) -> Launch:
    """Persist one user's fresh-session connection consent; never grants task execution."""
    exact = canonical_directory(directory)
    name, model = name.strip(), model.strip()
    if not valid_uuid(launch_id) or not name or len(name) > 80 or len(model) > 200:
        raise BridgeError("请填写名称（最多 80 字符）；模型最多 200 字符。")
    if any(ord(char) < 32 or ord(char) == 127 for text in (exact, name, model) for char in text):
        raise BridgeError("目录、名称和模型不能包含控制字符。")
    bridge_entry(exact)  # Conflicting project loaders fail before any side effect.
    private_dir(runtime.root, create=True)
    private_dir(runtime.root / "launches", create=True)
    launch = Launch(launch_id, str(uuid4()), exact, name, model)
    target = launch.directory(runtime)
    target.mkdir(mode=0o700)  # Exclusive: a previous attempt is not a new consent.
    private_dir(target)
    private_dir(target / "sessions", create=True)
    save_launch(runtime, launch)
    publish(target / "request.json", {"version": 1, "id": launch.id, "session_id": launch.session_id,
            "cwd": exact, "expires": int(time.time() * 1000) + 600_000})
    return launch


def pi_command(runtime: Runtime, launch: Launch) -> list[str]:
    directory = launch.directory(runtime)
    command = ["pi", "-e", str(bridge_entry(launch.cwd)), "--session-dir", str(directory / "sessions"),
               "--session-id", launch.session_id, "--name", launch.name,
               "--taskroom-launch", str(directory / "request.json")]
    if launch.model:
        command.extend(["--model", launch.model])
    return command


def _herdr(*arguments: str) -> dict:
    result = subprocess.run(["herdr", *arguments], stdin=subprocess.DEVNULL, capture_output=True,
                            text=True, timeout=15, check=False)
    if result.returncode:
        raise BridgeError(f"Herdr 未确认操作：{(result.stderr or result.stdout)[-1500:]}")
    if arguments[:2] == ("pane", "run") and not result.stdout.strip():
        return {"type": "pane_run"}  # Installed Herdr acknowledges this command via exit status only.
    try:
        response = json.loads(result.stdout)
        value = response["result"]
        if not isinstance(value, dict):
            raise ValueError("expected result object")
        return value
    except (KeyError, ValueError, TypeError) as error:
        raise BridgeError("Herdr 返回格式不兼容；操作是否生效需人工核实。") from error


def _handle(record: object, key: str) -> str:
    if not isinstance(record, dict):
        raise BridgeError(f"Herdr 缺少 {key} 的对象；不会猜测目标。")
    value = record.get(key)
    if not isinstance(value, str) or not value or any(char.isspace() for char in value):
        raise BridgeError(f"Herdr 缺少有效 {key}；不会猜测目标。")
    return value


def create_pi(runtime: Runtime, directory: str, name: str, model: str, launch_id: str) -> Launch:
    """Called once per submitted form; validates the caller before creating a NEW tab."""
    if os.environ.get("HERDR_ENV") != "1" or not os.environ.get("HERDR_PANE_ID"):
        raise BridgeError("新建 Pi 需要在 Herdr 中运行工作台；外部终端仍可使用 just pi。")
    if not shutil.which("herdr") or not shutil.which("pi"):
        raise BridgeError("需要已安装的 herdr 和 pi。")
    # Query-only discovery has no creation effects. Do not use whichever pane a client focused.
    caller = _herdr("pane", "current", "--current").get("pane")
    workspace = _handle(caller, "workspace_id")
    launch = prepare_launch(runtime, directory, name, model, launch_id)
    try:
        environment = {"TASKROOM_BRIDGE_DIR": str(runtime.root)}
        # Only configuration paths/non-secret startup policy; never copy API keys into argv.
        for key in ("PI_CODING_AGENT_DIR", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "PI_OFFLINE", "PI_TELEMETRY", "PI_SKIP_VERSION_CHECK"):
            if key in os.environ:
                environment[key] = os.environ[key]
        env_args = [part for key, value in environment.items() for part in ("--env", f"{key}={value}")]
        created = _herdr("tab", "create", "--workspace", workspace, "--cwd", launch.cwd,
                         "--label", f"Pi · {launch.name} · {launch.id[:8]}", "--no-focus", *env_args)
        launch.tab_id = _handle(created["tab"], "tab_id")
        launch.pane_id = _handle(created["root_pane"], "pane_id")
        if created["tab"].get("workspace_id") != workspace or created["root_pane"].get("workspace_id") != workspace:
            raise BridgeError("新标签页归属不匹配；未发送启动命令。")
        launch.state = "tab_created"
        launch.message = "标签页已创建；尚未确认启动命令。"
        save_launch(runtime, launch)  # Preserve exact new IDs before sending any terminal input.
        _herdr("pane", "run", launch.pane_id, shlex.join(pi_command(runtime, launch)))
        launch.state = "starting"
        launch.message = "启动命令已提交；等待新 Pi 连接。项目/工具权限请在新标签页处理，尚未派发任务。"
        save_launch(runtime, launch)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        launch.state = "unknown"
        launch.message = f"新建未确认：{error}。请核实标签页；不会重试启动，也未派发任务。"
        try:
            save_launch(runtime, launch)
        except (OSError, ValueError) as storage_error:
            launch.message += f" 状态也未写入：{storage_error}；原创建意图仍保留。"
    return launch


def connected_agent(runtime: Runtime, launch: Launch) -> Agent | None:
    try:
        agent = runtime.agent(launch.id)
    except FileNotFoundError:
        return None
    if agent.session_id != launch.session_id or agent.cwd != launch.cwd:
        raise BridgeError("新 Pi 的会话/目录与创建意图不符，不会分配。")
    return agent if agent.fresh else None
