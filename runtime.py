"""Private filesystem mailbox for explicitly joined Pi sessions; no terminal injection."""
from dataclasses import asdict, dataclass
import argparse
import json
import os
from pathlib import Path
import stat
import subprocess
import time
from uuid import uuid4

from journal import check_history, validate_events
from model import Run, valid_uuid

MAX_BYTES = 1024 * 1024
HEARTBEAT_SECONDS = 10


class BridgeError(ValueError):
    """No execution may be assumed from a failed or ambiguous mailbox operation."""


def bridge_root() -> Path:
    explicit = os.environ.get("TASKROOM_BRIDGE_DIR")
    if explicit:
        if not Path(explicit).is_absolute():
            raise BridgeError("TASKROOM_BRIDGE_DIR 必须是绝对目录。")
        return Path(explicit)
    state = os.environ.get("XDG_STATE_HOME", "")
    root = Path(state) if state and Path(state).is_absolute() else Path.home() / ".local/state"
    return root / "taskroom/bridges"


def canonical_directory(value: str) -> str:
    if not value.strip():
        raise BridgeError("请填写真实目录；不会使用 HOME 或 Git 根目录兜底。")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise BridgeError("请填写绝对目录。")
    try:
        canonical = path.resolve(strict=True)
        if not canonical.is_dir():
            raise BridgeError("所选路径不是目录。")
        return str(canonical)
    except OSError as error:
        raise BridgeError(f"目录不可用：{error}") from error


def locate_session(run: Run) -> str:
    """Verify only the captured original file's bounded header; never resume Pi."""
    identity = f"原项目目录：{run.cwd}\n原 Session：{run.session_id}\nRUN_ID={run.id}"
    path_text = ""
    try:
        events = validate_events(run.events, run_id=run.id, seq=run.seq)
        origin = next((event for event in events if event.kind == "origin"), None)
        path_text = origin.text if origin else ""
        if not path_text:
            raise BridgeError("未保存原会话路径（旧记录或临时会话）；不会寻找执行者现在的会话。")
        path = Path(path_text)
        if not path.is_absolute():
            raise BridgeError("原会话路径不是绝对路径。")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise BridgeError("原会话必须是当前用户拥有的普通文件，不接受链接或管道。")
            line = stream.readline(16 * 1024 + 1)
        if len(line) > 16 * 1024:
            raise BridgeError("原会话头超过 16 KiB，不读取正文。")
        header = json.loads(line)
        if not isinstance(header, dict) or header.get("type") != "session" or header.get("id") != run.session_id:
            raise BridgeError("文件头与原 Session 不匹配。")
        if not isinstance(header.get("cwd"), str) or canonical_directory(header["cwd"]) != run.cwd:
            raise BridgeError("文件头与原项目目录不匹配。")
        status = "已核对原会话的文件头；未读取或恢复对话。"
    except (OSError, ValueError) as error:
        status = f"无法核对原会话：{error}"
    return (f"{status}\n\n{identity}\n原会话文件：{path_text or '未知'}\n\n"
            "同一 Session 可执行多个任务；原任务消息以以上 RUN_ID 标识。\n"
            "请在原 Pi 中自行核对 /session，再搜索对应 RUN_ID。/tree 中选择条目会改变对话位置；仅查看时用 Esc 返回。\n"
            "这里只提供定位信息，不会切换、恢复或新建会话，不会发送任务。")


def _private(info: os.stat_result, *, directory: bool) -> None:
    correct_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not correct_type or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise BridgeError("桥接文件必须由当前用户拥有、无符号链接且仅本人可读写。")


def private_dir(path: Path, *, create: bool = False) -> None:
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    _private(path.lstat(), directory=True)
    if str(path.resolve()) != str(path.absolute()):
        raise BridgeError("桥接目录不接受符号链接或非规范路径。")


def read_json(path: Path) -> dict:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        _private(info, directory=False)
        if info.st_size > MAX_BYTES:
            raise BridgeError("桥接记录超过 1 MiB 上限。")
        text = stream.read(MAX_BYTES + 1)
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise BridgeError("桥接记录超过上限。")
    try:
        result = json.loads(text)
        if not isinstance(result, dict):
            raise BridgeError("桥接记录必须是 JSON 对象。")
        return result
    except (ValueError, UnicodeError) as error:
        raise BridgeError(f"桥接记录损坏：{path.name}") from error


def publish(path: Path, data: dict) -> None:
    """Publish an immutable command once. Existing UUIDs are never overwritten."""
    private_dir(path.parent)
    payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
    if len(payload) > MAX_BYTES:
        raise BridgeError("派发内容超过 1 MiB；请缩短任务说明。")
    temporary = path.parent / f".{uuid4()}.tmp"
    try:
        with temporary.open("xb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path, follow_symlinks=False)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class Agent:
    id: str
    session_id: str
    cwd: str
    name: str
    model: str
    pid: int
    heartbeat: int
    online: bool
    idle: bool
    active_run: str

    @property
    def fresh(self) -> bool:
        age = time.time() - self.heartbeat / 1000
        return self.online and -2 <= age <= HEARTBEAT_SECONDS

    @property
    def available(self) -> bool:
        return self.fresh and self.idle and not self.active_run

    @property
    def label(self) -> str:
        return f"{self.name} · {self.model} · {self.id[:8]} · {'空闲' if self.available else '忙碌/失联'}"

    @classmethod
    def from_record(cls, row: dict) -> "Agent":
        if set(row) != {"version", "id", "session_id", "cwd", "name", "model", "pid", "heartbeat", "online", "idle", "active_run"} or type(row["version"]) is not int or row["version"] != 1:
            raise BridgeError("Pi 连接协议版本不兼容。")
        for key, value in row.items():
            expected = int if key in ("version", "pid", "heartbeat") else bool if key in ("online", "idle") else str
            if type(value) is not expected:
                raise BridgeError(f"连接字段 {key} 类型错误。")
        data = dict(row)
        del data["version"]
        agent = cls(**data)
        if not valid_uuid(agent.id) or not valid_uuid(agent.session_id) or (agent.active_run and not valid_uuid(agent.active_run)) or not agent.cwd.startswith("/") or agent.pid <= 0:
            raise BridgeError("Pi 连接身份无效。")
        return agent


class Runtime:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root if root is not None else bridge_root()

    def endpoint(self, agent_id: str) -> Path:
        if not valid_uuid(agent_id):
            raise BridgeError("Pi 连接 ID 无效。")
        private_dir(self.root)
        endpoint = self.root / agent_id
        private_dir(endpoint)
        return endpoint

    def agent(self, agent_id: str) -> Agent:
        agent = Agent.from_record(read_json(self.endpoint(agent_id) / "agent.json"))
        if agent.id != agent_id:
            raise BridgeError("Pi 连接身份与邮箱目录不一致。")
        return agent

    def discover(self) -> list[Agent]:
        if not self.root.exists():
            return []
        private_dir(self.root)
        result = []
        for path in self.root.iterdir():
            if valid_uuid(path.name):
                try:
                    agent = self.agent(path.name)
                except FileNotFoundError:
                    continue  # An incompletely initialized endpoint is not yet a connection.
                if agent.fresh:
                    result.append(agent)
        return sorted(result, key=lambda agent: (agent.cwd, agent.name, agent.id))

    def dispatch(self, run: Run) -> None:
        agent = self.agent(run.agent_id)
        if agent.session_id != run.session_id or agent.cwd != run.cwd or canonical_directory(run.cwd) != run.cwd:
            raise BridgeError("Pi 会话或精确项目目录已改变；不会派发。")
        if not agent.available:
            raise BridgeError("Pi 不再空闲或心跳已过期；本次未确认派发，不会自动重试。")
        if run.state != "queued" or run.seq != 0:
            raise BridgeError("只能发布全新的派发意图。")
        self._command(run, "dispatch")

    def stop(self, run: Run) -> None:
        agent = self.agent(run.agent_id)
        if run.terminal or not agent.fresh or agent.session_id != run.session_id or agent.cwd != run.cwd:
            raise BridgeError("旧执行已结束或 Pi 身份/心跳失效；不能确认停止。")
        self._command(run, "stop")

    def _command(self, run: Run, kind: str) -> None:
        command_id = str(uuid4())
        envelope = {"version": 1, "id": command_id, "kind": kind,
                    "agent_id": run.agent_id, "session_id": run.session_id,
                    "expires": int(time.time() * 1000) + 30_000, "run": asdict(run)}
        publish(self.endpoint(run.agent_id) / "commands" / f"{command_id}.json", envelope)

    def read_run(self, run: Run) -> Run | None:
        endpoint = self.endpoint(run.agent_id)
        private_dir(endpoint / "runs")
        try:
            incoming = Run.from_record(read_json(endpoint / "runs" / f"{run.id}.json"))
        except FileNotFoundError:
            return None  # Publication is not an acknowledgement; never infer failure/finish.
        if incoming.identity != run.identity:
            raise BridgeError("Pi 返回的执行身份不匹配，拒绝应用。")
        check_history(run.events, incoming.events)
        return incoming


def bridge_loader_content() -> str:
    source = Path(__file__).with_name("pi-bridge.ts").resolve()
    return f"// Taskroom: opt-in only; run /taskroom connect after /reload.\nexport {{ default }} from {json.dumps(str(source))};\n"


def bridge_entry(directory: str) -> Path:
    """Reuse our installed entry so explicit loading and discovery cannot double-register."""
    project = Path(directory)
    target = project / ".pi/extensions/taskroom.ts"
    for path in (project / ".pi", target.parent, target):
        if path.is_symlink():
            raise BridgeError("项目桥接入口含符号链接，未启动 Pi。")
    if target.exists():
        if not target.is_file() or target.read_text() != bridge_loader_content():
            raise BridgeError("已有不同的 taskroom.ts，拒绝覆盖或重复加载。")
        return target
    return Path(__file__).with_name("pi-bridge.ts").resolve()


def install_bridge(directory: str) -> Path:
    project = Path(canonical_directory(directory))
    target_dir = project / ".pi/extensions"
    for path in (project / ".pi", target_dir):
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise BridgeError("现有 .pi/extensions 路径不是普通目录，未安装。")
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "taskroom.ts"
    content = bridge_loader_content()
    if target.is_symlink():
        raise BridgeError("已有同名链接，拒绝覆盖。")
    if target.exists():
        if target.read_text() != content:
            raise BridgeError("已有同名扩展，拒绝覆盖。")
    else:
        with target.open("x") as stream:
            stream.write(content)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description="Explicit local Pi bridge setup; never changes global Pi settings.")
    parser.add_argument("action", choices=("pi", "install"))
    parser.add_argument("directory")
    parser.add_argument("args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        directory = canonical_directory(args.directory)
        if args.action == "install":
            print(f"已准备 {install_bridge(directory)}\n在该 Pi 中 /reload，然后 /taskroom connect；不会自动接管。")
            return 0
        return subprocess.call(["pi", "-e", str(bridge_entry(directory)), *args.args], cwd=directory)
    except (OSError, ValueError) as error:
        print(f"Taskroom: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
