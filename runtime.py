"""Private filesystem mailbox for explicitly joined Pi sessions; no terminal injection."""
import argparse
import json
import os
import stat
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from uuid import uuid4

from activity import MAX_ACTIVITY_BYTES, Activity
from journal import check_history, validate_events
from model import Board, Run, Supplement, Task, valid_uuid
from session_adoption import (
    AdoptionIntent,
    AdoptionReceipt,
    AdoptionRequest,
    adoption_confirmed,
    latest_adoption,
    run_digest,
)
from task_context import has_valid_contract, source_base

MAX_BYTES = 1024 * 1024
HEARTBEAT_SECONDS = 10
SUPPLEMENT_BYTES = 256 * 1024
RUN_IDENTITY_FIELDS = ("id", "task_id", "agent_id", "session_id", "cwd", "title", "description", "requirements")


def supplement_request(run: Run, note: Supplement) -> dict:
    Supplement.from_record(asdict(note))
    if note.run_id != run.id:
        raise BridgeError("补充说明不属于本轮。")
    return {"version": 1, "id": note.id, "expires": note.expires, "text": note.text,
            "run": {key: getattr(run, key) for key in RUN_IDENTITY_FIELDS}}


class BridgeError(ValueError):
    """No execution may be assumed from a failed or ambiguous mailbox operation."""


def bridge_root() -> Path:
    explicit = os.environ.get("WAGGLE_BRIDGE_DIR")
    if explicit:
        if not Path(explicit).is_absolute():
            raise BridgeError("WAGGLE_BRIDGE_DIR 必须是绝对目录。")
        return Path(explicit)
    state = os.environ.get("XDG_STATE_HOME", "")
    root = Path(state) if state and Path(state).is_absolute() else Path.home() / ".local/state"
    return root / "waggle/bridges"


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


def read_json(path: Path, *, limit: int = MAX_BYTES) -> dict:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        _private(info, directory=False)
        if info.st_size > limit:
            raise BridgeError(f"桥接记录超过 {limit} 字节上限。")
        text = stream.read(limit + 1)
    if len(text.encode("utf-8")) > limit:
        raise BridgeError("桥接记录超过上限。")
    try:
        result = json.loads(text)
        if not isinstance(result, dict):
            raise BridgeError("桥接记录必须是 JSON 对象。")
        return result
    except (ValueError, UnicodeError, RecursionError) as error:
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


@dataclass(frozen=True)
class SessionObservation:
    agent_id: str
    session_id: str
    cwd: str
    seq: int
    observed_at: int
    state: str
    run_id: str
    linked_run_id: str
    following: bool
    input_pending: bool

    @classmethod
    def from_record(cls, row: dict, run: Run) -> "SessionObservation":
        return cls._from_identity(row, (run.agent_id, run.session_id, run.cwd))

    @classmethod
    def for_agent(cls, row: dict, agent: Agent) -> "SessionObservation":
        return cls._from_identity(row, (agent.id, agent.session_id, agent.cwd))

    @classmethod
    def _from_identity(cls, row: dict, expected_identity: tuple[str, str, str]) -> "SessionObservation":
        names = {"agent_id", "session_id", "cwd", "seq", "observed_at", "state", "run_id", "linked_run_id", "following", "input_pending"}
        if set(row) != names | {"version"} or type(row["version"]) is not int or row["version"] != 1:
            raise BridgeError("Pi 会话观测协议不兼容。")
        for key in names:
            field_type = bool if key in {"following", "input_pending"} else int if key in {"seq", "observed_at"} else str
            if type(row[key]) is not field_type:
                raise BridgeError("Pi 会话观测字段类型错误。")
        snapshot = cls(**{key: row[key] for key in names})
        if ((snapshot.agent_id, snapshot.session_id, snapshot.cwd) != expected_identity
                or not 0 < snapshot.seq <= 2**53 - 1 or not 0 < snapshot.observed_at <= 2**53 - 1
                or snapshot.state not in {"idle", "working", "waiting", "offline"}
                or any(value and not valid_uuid(value) for value in (snapshot.run_id, snapshot.linked_run_id))
                or (snapshot.following and not snapshot.linked_run_id)):
            raise BridgeError("Pi 会话观测身份或状态错误。")
        return snapshot

    def check_after(self, previous: "SessionObservation | None") -> None:
        if previous and (self.agent_id, self.session_id, self.cwd) != (previous.agent_id, previous.session_id, previous.cwd):
            raise BridgeError("不同连接的观测修订不可比较；须先核对明确纳管回执。")
        if previous and (self.seq < previous.seq or (self.seq == previous.seq and self != previous)):
            raise BridgeError("Pi 会话观测修订倒退或同修订改写。")


@dataclass(frozen=True)
class ReportCapabilityDiagnostic:
    code: str
    message: str
    evidence: str
    uncertainty: str
    next_step: str

    @property
    def ready(self) -> bool:
        return self.code == 'ready'

    def describe(self) -> str:
        return self.message + '\n依据：' + self.evidence + '\n限制：' + self.uncertainty + '\n下一步：' + self.next_step


@dataclass
class RuntimeUpdates:
    runs: list[Run] = field(default_factory=list)
    continuations: list[tuple[Run, Run]] = field(default_factory=list)
    agents: dict[str, Agent] = field(default_factory=dict)
    sessions: dict[str, SessionObservation] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    session_errors: dict[str, str] = field(default_factory=dict)
    activity: Activity | None = None
    activity_issue: str = ""
    supplements: list[Supplement] = field(default_factory=list)
    supplement_errors: dict[str, str] = field(default_factory=dict)


@dataclass
class AdoptionBatch:
    receipts: dict[int, AdoptionReceipt] = field(default_factory=dict)
    requests: dict[int, AdoptionRequest] = field(default_factory=dict)
    runs: list[tuple[AdoptionRequest, Run]] = field(default_factory=list)
    errors: dict[int, str] = field(default_factory=dict)


class Runtime:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root if root is not None else bridge_root()
        self._observations: dict[str, tuple[tuple, Path]] = {}
        self._task_observations: set[str] = set()
        self._adoption_bindings: dict[str, AdoptionIntent] = {}
        self._adoption_receipts: dict[str, AdoptionReceipt] = {}

    def observe(self, run: Run, root: Path, *, follow: bool = False) -> None:
        """Pin one existing run to an explicit read-only source, for this process only."""
        if not root.is_absolute():
            raise BridgeError("只读观测目录必须是绝对路径。")
        identity = Run.from_record(asdict(run)).identity
        if run.id in self._observations:
            raise BridgeError("本轮已经指定只读观测来源，不能重复或替换绑定。")
        self._observations[run.id] = (identity, root)
        if follow:
            self._task_observations.add(run.id)

    def follows_task(self, run: Run) -> bool:
        return not self.observation_only(run) or run.id in self._task_observations

    def inherit_observation(self, parent: Run, child: Run) -> None:
        """Call only after the child's local transaction commits; no command grant."""
        child.check_continuation(parent)
        if not self.observation_only(parent):
            return
        if not self.follows_task(parent):
            raise BridgeError("原授权仅观测单轮，不能扩展到后续执行。")
        identity, root = self._observations[parent.id]
        if parent.identity != identity:
            raise BridgeError("只读观测身份已改变。")
        if child.id in self._observations:
            if self._observations[child.id] != (child.identity, root) or child.id not in self._task_observations:
                raise BridgeError("续接观测来源或权限冲突。")
        else:
            self.observe(child, root, follow=True)

    def observation_only(self, run: Run) -> bool:
        return run.id in self._observations

    def require_control(self, run: Run) -> None:
        if self.observation_only(run):
            raise BridgeError("本轮仅只读观测，不能派发、停止或发送补充；请到原 Pi 处理。")

    def _read_endpoint(self, run: Run) -> Path:
        observation = self._observations.get(run.id)
        if observation is None:
            return self.endpoint(run.agent_id)
        identity, root = observation
        if run.identity != identity:
            raise BridgeError("只读观测的原轮次身份已改变，拒绝读取。")
        return Runtime(root).endpoint(run.agent_id)

    def observation_identity(self, run: Run) -> tuple[str, str, str]:
        attachment = self._adoption_bindings.get(run.id)
        if attachment and run.terminal and attachment.source and attachment.source.matches(run):
            return attachment.agent_id, attachment.session_id, attachment.cwd
        return run.agent_id, run.session_id, run.cwd

    def read_agent(self, run: Run) -> Agent:
        identity = self.observation_identity(run)
        if not self.observation_only(run) and identity[0] != run.agent_id:
            return self.agent(identity[0])
        if not self.observation_only(run):
            return self.agent(run.agent_id)
        agent = Agent.from_record(read_json(self._read_endpoint(run) / "agent.json"))
        if (agent.id, agent.session_id, agent.cwd) != (run.agent_id, run.session_id, run.cwd):
            raise BridgeError("只读观测的 Pi 连接与原轮次身份不一致。")
        return agent

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

    def adoption_capabilities(self, agent: Agent) -> dict:
        row = read_json(self.endpoint(agent.id) / "adoption-capabilities.json", limit=4096)
        if (set(row) != {"version", "agent_id", "session_id", "cwd", "pid", "observed_at", "association_id"}
                or type(row["version"]) is not int or row["version"] != 1
                or (row["agent_id"], row["session_id"], row["cwd"], row["pid"])
                != (agent.id, agent.session_id, agent.cwd, agent.pid)
                or type(row["pid"]) is not int or type(row["observed_at"]) is not int
                or not -2000 <= int(time.time() * 1000) - row["observed_at"] <= HEARTBEAT_SECONDS * 1000
                or type(row["association_id"]) is not str
                or (row["association_id"] and not valid_uuid(row["association_id"]))):
            raise BridgeError("纳管能力、精确会话或观测新鲜度不符；不创建关联。")
        return row

    def prepare_adoption(self, board: Board, task: Task, expected: Agent, checkpoint: str,
                         *, previous_checked: bool = False) -> AdoptionRequest:
        """Prepare one explicit association; busy native turns may finish without interruption."""
        if (board.tasks.get(task.id) is not task or task.execution != "pi" or task.retention != "active"
                or task.status == "done" or not task.description.strip()):
            raise BridgeError("请选择未验收的真实任务，并明确其独立目标；已验收任务请另建后续。")
        agent = self.agent(expected.id)
        if (not agent.fresh or (agent.id, agent.session_id, agent.cwd, agent.pid)
                != (expected.id, expected.session_id, expected.cwd, expected.pid)
                or canonical_directory(agent.cwd) != agent.cwd or board.projects.get(task.project) != agent.cwd):
            raise BridgeError("会话、进程或精确目录已变化；不能按旧选择纳管。")
        capability = self.adoption_capabilities(agent)
        if capability["association_id"]:
            raise BridgeError("此会话已有任务关联；换任务前请在原 Pi 明确解除关联，不接管其他任务。")
        snapshot = SessionObservation.for_agent(read_json(self.endpoint(agent.id) / "session.json", limit=16384), agent)
        if (not -2000 <= int(time.time() * 1000) - snapshot.observed_at <= HEARTBEAT_SECONDS * 1000
                or snapshot.state == "offline" or snapshot.run_id or snapshot.input_pending or agent.active_run):
            raise BridgeError("会话有已关联执行、待接收输入或观测未知；先核实，不重复纳管。")
        if any(not run.terminal and (run.task_id == task.id or
               (run.session_id, run.cwd) == (agent.session_id, agent.cwd)) for run in board.runs.values()):
            raise BridgeError("任务或同一会话仍有未确认结束的 Run；先核对原执行。")
        source = board.runs.get(task.run_id) if task.run_id else None
        if task.run_id and source is None:
            raise BridgeError("原轮次缺失，不能从会话名称猜测归属。")
        source_record = None
        if source is not None:
            self.require_control(source)
            if (not source.terminal or (source.task_id, source.session_id, source.cwd, source.title, source.description)
                    != (task.id, agent.session_id, agent.cwd, task.title, task.description) or not previous_checked):
                raise BridgeError("只允许明确核对后的同一原会话接续；跨会话仍需独立写者交接。")
            self.check_current(source, known_session=True)
            original = self.agent(source.agent_id)
            stored = Run.from_record(read_json(self._read_endpoint(source) / "runs" / f"{source.id}.json"))
            if stored != source or original.active_run:
                raise BridgeError("原轮次或原连接仍有额外执行；先核实，不能用离线代替停止。")
            source_record = {"run_id": source.id, "agent_id": source.agent_id, "session_id": source.session_id,
                             "cwd": source.cwd, "seq": source.seq, "digest": run_digest(source), "pid": original.pid}
            if snapshot.linked_run_id not in {"", source.id}:
                raise BridgeError("当前会话仍关联其他轮次，不能接管。")
        elif snapshot.linked_run_id or snapshot.following or previous_checked:
            raise BridgeError("新任务只能纳管尚未关联的会话，不认领旧任务或旧授权。")
        requirements = board.compiled_requirements(task)
        if not has_valid_contract(source_base(requirements)):
            requirements = "纳管只建立归属；旧授权、预算和已结束轮次不是新增许可。后续操作须遵守当前明确输入及 Pi 权限；不能重放旧任务。"
        self.check_report_capabilities(agent, requirements)
        now = int(time.time() * 1000)
        return AdoptionRequest.from_record({
            "version": 1, "id": str(uuid4()), "agent_id": agent.id, "session_id": agent.session_id,
            "cwd": agent.cwd, "pid": agent.pid, "task_id": task.id, "title": task.title,
            "project": task.project, "description": task.description, "requirements": requirements,
            "checkpoint": checkpoint.strip(), "source": source_record, "previous_checked": previous_checked,
            "requested_at": now, "expires": now + 600_000,
        })

    def publish_adoption(self, request: AdoptionRequest) -> None:
        intent = request.intent
        agent = self.agent(intent.agent_id)
        capability = self.adoption_capabilities(agent)
        if (not agent.fresh or (agent.session_id, agent.cwd, agent.pid) != (intent.session_id, intent.cwd, intent.pid)
                or capability["association_id"] or int(time.time() * 1000) > intent.expires):
            raise BridgeError("纳管目标或确认窗口已变化；不发布，不生成新意图重试。")
        publish(self.endpoint(intent.agent_id) / "adoptions" / "requests" / f"{intent.id}.json", request.record())

    def _adoption_request(self, intent: AdoptionIntent) -> AdoptionRequest:
        root = self.endpoint(intent.agent_id) / "adoptions"
        try:
            row = read_json(root / "requests" / f"{intent.id}.json")
        except FileNotFoundError:
            row = read_json(root / "history" / f"{intent.id}.json")
        request = AdoptionRequest.from_record(row)
        if request.intent != intent:
            raise BridgeError("纳管请求与人工确认摘要不符；不能替换身份或检查点。")
        return request

    def _adoption_receipt(self, intent: AdoptionIntent) -> AdoptionReceipt:
        row = read_json(self.endpoint(intent.agent_id) / "adoptions" / "receipts" / f"{intent.id}.json", limit=16384)
        receipt = AdoptionReceipt.from_record(row, intent)
        receipt.check_after(self._adoption_receipts.get(intent.id))
        if receipt.at > int(time.time() * 1000) + 2000:
            raise BridgeError("纳管回执来自未来时间；不推断成功。")
        self._adoption_receipts[intent.id] = receipt
        return receipt

    def read_adoptions(self, board: Board) -> AdoptionBatch:
        batch = AdoptionBatch()
        self._adoption_bindings = {}
        for task in board.tasks.values():
            if task.retention != "active" or task.status == "done":
                continue
            try:
                intent = latest_adoption(task)
                if intent is None:
                    continue
                intent.validate_task(board, task)
                if intent.source:
                    self.require_control(board.runs[intent.source.run_id])
                request = self._adoption_request(intent)
                receipt = self._adoption_receipt(intent)
                batch.requests[task.id], batch.receipts[task.id] = request, receipt
                if (receipt.state == "attached" and intent.source and task.run_id == intent.source.run_id
                        and adoption_confirmed(task, intent)):
                    agent = self.agent(intent.agent_id)
                    capability = self.adoption_capabilities(agent)
                    if agent.fresh and capability["association_id"] == intent.id:
                        self._adoption_bindings[intent.source.run_id] = intent
                if receipt.state == "consumed":
                    incoming = self.read_run(Run(receipt.run_id, task.id, intent.agent_id, intent.session_id,
                                                  intent.cwd, request.title, request.description, request.requirements))
                    if incoming and incoming.adoption_id:
                        request.validate_run(incoming)
                        batch.runs.append((request, incoming))
            except FileNotFoundError:
                batch.errors[task.id] = "纳管接收未确认；只观察原意图，不生成新 ID 重发。"
            except (ValueError, OSError) as error:
                batch.errors[task.id] = f"纳管状态待核实：{error}；不会自动重试或派发。"
        return batch

    def report_capability_diagnostic(self, agent: Agent, requirements: str = "", *, structured: bool = False,
                                     endpoint: Path | None = None) -> ReportCapabilityDiagnostic:
        """Bounded read of existing v2 proof; no probe, model turn or wire changes."""
        def reason(code: str, message: str, evidence: str, uncertainty: str = '未执行新探测；不读取凭据，不推断内存中的 SDK 版本。') -> ReportCapabilityDiagnostic:
            return ReportCapabilityDiagnostic(code, message, evidence, uncertainty,
                '此 Pi 使用者先核对当前活动、精确连接及实际加载路径；未知原因先查已有诊断，不重派。源码更新不等于进程更新。')
        try:
            path = endpoint or self.endpoint(agent.id)
            row = read_json(path / 'report-capabilities.json', limit=16 * 1024)
        except FileNotFoundError:
            return reason('missing', '这个 Pi 尚未证明能接收本次要求；旧记录仍可查看，不要重复发送。', '当前连接没有可读取的 v2 能力证明；在线不等于可报告。')
        except (ValueError, OSError) as error:
            return reason('unreadable', '这个 Pi 的能力证明不可读；不会发送或重试。', str(error)[:500])
        names = {"version", "agent_id", "session_id", "cwd", "observed_at", "tool", "active", "schema_sha256", "features",
                 "callable", "transport", "ref", "verified_at", "boundary"}
        if (set(row) != names or type(row["version"]) is not int or row["version"] != 2
                or (row["agent_id"], row["session_id"], row["cwd"]) != (agent.id, agent.session_id, agent.cwd)
                or row["tool"] != "waggle_report" or type(row["active"]) is not bool
                or type(row["observed_at"]) is not int or not 0 < row["observed_at"] <= 2**53 - 1
                or type(row["schema_sha256"]) is not str
                or (row["schema_sha256"] != "" and (len(row["schema_sha256"]) != 64
                    or any(char not in "0123456789abcdef" for char in row["schema_sha256"])))
                or type(row["features"]) is not list or len(row["features"]) > 16
                or any(type(item) is not str or len(item) > 48 for item in row["features"])
                or len(set(row["features"])) != len(row["features"])
                or type(row["callable"]) is not bool or type(row["transport"]) is not str
                or row["transport"] not in {"native", "fabric", "unknown"} or type(row["ref"]) is not str
                or type(row["verified_at"]) is not int or not 0 <= row["verified_at"] <= row["observed_at"]
                or type(row["boundary"]) is not str
                or (row["callable"] and (not row["verified_at"] or (row["transport"], row["ref"], row["boundary"]) not in {
                    ("native", "waggle_report", "native-report-guard"),
                    ("fabric", "extensions.waggle_report", "fabric-report-guard"),
                    ("fabric", "extensions.waggle_report", "fabric-host-guard")}))
                or (not row["callable"] and (row["transport"], row["ref"], row["verified_at"], row["boundary"]) != ("unknown", "", 0, ""))):
            mismatch = any(row.get(key) != value for key, value in
                           (('agent_id', agent.id), ('session_id', agent.session_id), ('cwd', agent.cwd)))
            return reason('identity_mismatch' if mismatch else 'invalid_proof',
                          '这个 Pi 的能力证明与当前连接或 schema 不匹配；不会发送或重试。',
                          '精确连接/会话/目录不符。' if mismatch else 'v2 字段、schema 摘要或实际调用边界不兼容；未扩大 wire schema。')
        if not agent.fresh:
            return reason('connection_stale', '这个 Pi 已离线或心跳已过期；先核实连接，不要重复发送。', 'Agent v1 心跳不在有效窗口；不能据此推断后台已结束。')
        if not -2 <= time.time() - row['observed_at'] / 1000 <= HEARTBEAT_SECONDS:
            return reason('stale_proof', '这个 Pi 的能力证明已过期；先核实连接，不要重复发送。', 'v2 observed_at 超出有效窗口；在线也不能用旧证明。')
        required = {"report-v1", "handoff-v1"}
        if structured or "交接目标：交付可审提案，不批准或执行新工作。" in requirements or "结构化人工决定 · 草稿不是批准" in requirements:
            required.add("decisions-v3")
        prefix = '这个 Pi 当前不能提交本次所需的报告或提案；'
        if not row['active']:
            return reason('inactive', prefix + '报告入口未激活，不要重复发送。', 'v2 active=false；不能区分隐藏、禁用或加载问题。')
        if not row['schema_sha256']:
            return reason('schema_unconfirmed', prefix + 'schema 尚未核验，不要重复发送。', 'v2 schema_sha256 为空；不把注册当兼容。')
        if not required <= set(row['features']):
            return reason('features_missing', prefix + '所需功能未被证明，不要重复发送。', '缺少：' + ', '.join(sorted(required - set(row['features']))))
        if not row['callable']:
            return reason('callability_unconfirmed', prefix + '实际调用入口未确认，不要重复发送。',
                          'v2 callable=false，transport=unknown。',
                          '现有 v2 不携带细分原因；Fabric ref 缺失、探测拒绝/超时或原生 hook 未确认均未被独立证明，不能武断诊断。')
        if not -2 <= time.time() - row['verified_at'] / 1000 <= HEARTBEAT_SECONDS:
            return reason('stale_probe', prefix + '实际调用证明已过期，不要重复发送。', 'verified_at 超出窗口；observed_at 新鲜不等于探测新鲜。')
        return reason('ready', '当前精确连接可提交本次报告；这不是任务或预算授权。',
                      f"v2 transport={row['transport']} ref={row['ref']} boundary={row['boundary']}")

    def check_report_capabilities(self, agent: Agent, requirements: str = '', *, structured: bool = False,
                                  endpoint: Path | None = None) -> None:
        diagnostic = self.report_capability_diagnostic(agent, requirements, structured=structured, endpoint=endpoint)
        if not diagnostic.ready:
            raise BridgeError(diagnostic.describe())

    def report_capability_issue(self, agent: Agent, requirements: str = "", *, endpoint: Path | None = None) -> str:
        try:
            self.check_report_capabilities(agent, requirements, endpoint=endpoint)
        except (ValueError, OSError) as error:
            return str(error) if isinstance(error, BridgeError) else "这个 Pi 的能力证明不可读；先核对桥接连接，不要重复发送。"
        return ""

    def check_dispatch(self, expected: Agent, requirements: str, *, structured: bool = False) -> Agent:
        agent = self.agent(expected.id)
        if (agent.id, agent.session_id, agent.cwd) != (expected.id, expected.session_id, expected.cwd) or canonical_directory(agent.cwd) != agent.cwd:
            raise BridgeError("Pi 会话或精确项目目录已改变；不会派发。")
        if not agent.available:
            raise BridgeError("Pi 不再空闲或心跳已过期；不会派发或自动重试。")
        self.check_report_capabilities(agent, requirements, structured=structured)
        return agent

    def dispatch(self, run: Run) -> None:
        self.require_control(run)
        agent = self.agent(run.agent_id)
        if agent.session_id != run.session_id or agent.cwd != run.cwd or canonical_directory(run.cwd) != run.cwd:
            raise BridgeError("Pi 会话或精确项目目录已改变；不会派发。")
        if not agent.available:
            raise BridgeError("Pi 不再空闲或心跳已过期；本次未确认派发，不会自动重试。")
        if run.state != "queued" or run.seq != 0:
            raise BridgeError("只能发布全新的派发意图。")
        self.check_report_capabilities(agent, run.requirements)
        self._command(run, "dispatch")

    def stop(self, run: Run) -> None:
        self.require_control(run)
        agent = self.agent(run.agent_id)
        if run.terminal or not agent.fresh or agent.session_id != run.session_id or agent.cwd != run.cwd:
            raise BridgeError("旧执行已结束或 Pi 身份/心跳失效；不能确认停止。")
        self._command(run, "stop")

    def _command(self, run: Run, kind: str) -> None:
        self.require_control(run)
        command_id = str(uuid4())
        envelope = {"version": 1, "id": command_id, "kind": kind,
                    "agent_id": run.agent_id, "session_id": run.session_id,
                    "expires": int(time.time() * 1000) + 30_000, "run": asdict(run)}
        publish(self.endpoint(run.agent_id) / "commands" / f"{command_id}.json", envelope)

    def check_supplement(self, run: Run) -> None:
        self.require_control(run)
        agent = self.agent(run.agent_id)
        if run.state != "working" or not agent.fresh or agent.idle or agent.active_run != run.id or (agent.session_id, agent.cwd) != (run.session_id, run.cwd):
            raise BridgeError("本轮不在可确认的执行状态或连接归属/心跳失效；不能发送补充。")
        try:
            capability = read_json(self.endpoint(run.agent_id) / "capabilities.json", limit=4096)
        except FileNotFoundError as error:
            raise BridgeError("原 Pi 桥接未声明运行中补充能力；请在原 Pi 处理，不会向旧协议发送请求。") from error
        expected = {"version": 1, "agent_id": run.agent_id, "session_id": run.session_id, "cwd": run.cwd, "supplement": 1}
        if capability != expected or type(capability.get("version")) is not int or type(capability.get("supplement")) is not int:
            raise BridgeError("运行中补充能力或会话绑定不兼容。")
        private_dir(self.endpoint(run.agent_id) / "supplements")
        private_dir(self.endpoint(run.agent_id) / "supplements/requests")

    def send_supplement(self, run: Run, note: Supplement) -> None:
        self.check_supplement(run)
        if "结构化人工决定 · 草稿不是批准" in note.text:
            self.check_report_capabilities(self.agent(run.agent_id), note.text, structured=True)
        now = int(time.time() * 1000)
        if note.status != "pending" or not now <= note.expires <= now + 60000:
            raise BridgeError("补充意图已过期或已有回执；不重放发送。")
        request = supplement_request(run, note)
        if len(json.dumps(request, ensure_ascii=False).encode()) > SUPPLEMENT_BYTES:
            raise BridgeError("补充请求超出大小上限。")
        publish(self.endpoint(run.agent_id) / "supplements/requests" / f"{note.id}.json", request)

    def read_supplement(self, run: Run, note: Supplement) -> Supplement | None:
        endpoint = self._read_endpoint(run)
        try:
            private_dir(endpoint / "supplements")
            private_dir(endpoint / "supplements/receipts")
            row = read_json(endpoint / "supplements/receipts" / f"{note.id}.json", limit=SUPPLEMENT_BYTES)
        except FileNotFoundError:
            return None
        if set(row) != {"version", "request", "status", "seq", "run_seq", "detail"} or type(row["version"]) is not int or row["version"] != 1 or json.dumps(row["request"], sort_keys=True) != json.dumps(supplement_request(run, note), sort_keys=True) or row["status"] == "pending":
            raise BridgeError("补充回执版本或完整请求身份不匹配。")
        incoming = Supplement.from_record({**asdict(note), **{key: row[key] for key in ("status", "seq", "run_seq", "detail")}})
        incoming.check_after(note)
        return incoming

    def check_new_round(self, run: Run, *, adoption: AdoptionIntent | None = None) -> None:
        """New work still requires fresh idle; an explicit same-session receipt may change transport."""
        self.require_control(run)
        if not run.terminal:
            raise BridgeError('旧轮尚未确认结束；不能创建下一轮意图。')
        expected = (run.agent_id, run.session_id, run.cwd)
        if adoption is not None:
            if not adoption.source or not adoption.source.matches(run):
                raise BridgeError("纳管回执不属于这个原轮次。")
            self._adoption_request(adoption)
            receipt = self._adoption_receipt(adoption)
            agent = self.agent(adoption.agent_id)
            capability = self.adoption_capabilities(agent)
            if receipt.state != "attached" or capability["association_id"] != adoption.id:
                raise BridgeError("同一会话纳管未确认或已解除；不能派发。")
            expected = (adoption.agent_id, adoption.session_id, adoption.cwd)
            self._adoption_bindings[run.id] = adoption
        else:
            agent = self.agent(run.agent_id)
        if (agent.id, agent.session_id, agent.cwd) != expected:
            raise BridgeError('原 Pi 身份不匹配；核实原 Pi 当前活动，不会创建新意图。')
        self.check_current(run, known_session=True)
        snapshot = self.read_session(run) if adoption else self._original_session(run)
        if (not agent.available or snapshot is None or snapshot.state != 'idle' or snapshot.input_pending
                or not -2000 <= int(time.time() * 1000) - snapshot.observed_at <= HEARTBEAT_SECONDS * 1000
                or snapshot.run_id not in ('', run.id) or snapshot.linked_run_id not in ('', run.id)
                or run.id not in (snapshot.run_id, snapshot.linked_run_id)):
            raise BridgeError('核实原 Pi 当前活动；非空闲、离线或未归属本轮/未知时只能保存，不创建新意图。')

    def check_current(self, run: Run, *, known_session: bool = False) -> None:
        """Recheck before a human changes a terminal task; never grant control."""
        snapshot = self.read_session(run)
        try:
            # Exact-run observers cannot inspect successors, but their explicit source must exist.
            self._read_endpoint(run)
            successors = self.read_continuations(run, limit=1)
        except FileNotFoundError as error:
            if snapshot or known_session or self.observation_only(run) or run.continuation_parent:
                raise BridgeError("原观测来源不可用；请核对 Pi 后重新确认。") from error
            successors = []  # Legacy imported terminal records have no live protocol to recheck.
        if successors:
            raise BridgeError("Pi 已续接新一轮；请先刷新并核对，不能操作旧交付。")
        if snapshot is None:
            if known_session or run.continuation_parent:
                raise BridgeError("Pi 会话观测暂不可用；不能按旧快照确认。")
            return
        age = int(time.time() * 1000) - snapshot.observed_at
        if snapshot.state != "offline" and not -2000 <= age <= HEARTBEAT_SECONDS * 1000:
            raise BridgeError("Pi 会话观测过期；请核对当前执行后重试。")
        if (snapshot.input_pending or snapshot.run_id not in ("", run.id)
                or (snapshot.following and snapshot.linked_run_id != run.id)
                or (snapshot.linked_run_id == run.id and snapshot.state not in {"idle", "offline"})):
            raise BridgeError("Pi 有新输入、执行或未纳入的续接；请先核对当前任务。")

    def read_updates(self, runs: list[Run], notes: list[tuple[Run, Supplement]], target: Run | None) -> RuntimeUpdates:
        """Read-only poll: executions, live session facts and proven successor edges."""
        batch = RuntimeUpdates()
        revisions = {run.id: run.seq for run in runs}
        for run in runs:
            latest = run
            try:
                incoming = self.read_run(run) if not run.terminal else None
                if incoming and incoming.seq > run.seq:
                    batch.runs.append(incoming)
                    latest = incoming
                    revisions[run.id] = incoming.seq
                for child in self.read_continuations(latest):
                    batch.continuations.append((latest, child))
                    latest = child
            except (ValueError, OSError) as error:
                batch.errors[run.id] = f"进展读取失败：{error}；保留最后记录，不重派。"
            try:
                agent = self.read_agent(run)
                batch.agents[run.agent_id] = agent
                if not agent.fresh:
                    batch.errors.setdefault(run.id, "Pi 离线/心跳过期；不推断执行结束，不会自动重派。")
            except (ValueError, OSError):
                batch.errors.setdefault(run.id, "连接身份/心跳不可读；不推断执行结束。")
            try:
                snapshot = self.read_session(run)
                if snapshot:
                    batch.sessions[run.id] = snapshot
            except (ValueError, OSError):
                batch.session_errors[run.id] = "Pi 会话快照不可读或身份不匹配"
        if target is not None:
            try:
                batch.activity = self.read_activity(target)
            except (ValueError, OSError):
                batch.activity_issue = "活动快照不可读或不匹配"
        for run, note in notes:
            try:
                receipt = self.read_supplement(run, note)
                if receipt and receipt.seq > note.seq and receipt.run_seq <= revisions.get(run.id, run.seq):
                    batch.supplements.append(receipt)
            except (ValueError, OSError) as error:
                batch.supplement_errors[note.id] = f"补充回执不可读：{error}"
        return batch

    def _original_session(self, run: Run) -> SessionObservation | None:
        try:
            row = read_json(self._read_endpoint(run) / "session.json", limit=16 * 1024)
        except FileNotFoundError:
            return None  # Legacy bridges do not attest native session activity.
        return SessionObservation.from_record(row, run)

    def read_session(self, run: Run) -> SessionObservation | None:
        identity = self.observation_identity(run)
        if identity[0] != run.agent_id and not self.observation_only(run):
            agent = self.agent(identity[0])
            return SessionObservation.for_agent(read_json(self.endpoint(agent.id) / "session.json", limit=16384), agent)
        return self._original_session(run)

    def read_continuations(self, parent: Run, *, limit: int = 8) -> list[Run]:
        if not self.follows_task(parent) or not parent.terminal or parent.state == "abandoned":
            return []  # Exact-run grants do not even inspect another run's files.
        if not 1 <= limit <= 8:
            raise BridgeError("单次最多读取八个续接轮次。")
        endpoint = self._read_endpoint(parent)
        private_dir(endpoint / "runs")
        result, seen = [], {parent.id}
        for _ in range(limit):
            if not parent.terminal:
                break
            try:
                row = read_json(endpoint / "runs" / f"{parent.id}.next.json")
            except FileNotFoundError:
                break
            expected = {key: getattr(parent, key) for key in RUN_IDENTITY_FIELDS}
            if (set(row) != {"version", "parent", "parent_seq", "child"} or type(row["version"]) is not int
                    or row["version"] != 1 or type(row["parent_seq"]) is not int or row["parent_seq"] != parent.seq
                    or json.dumps(row["parent"], sort_keys=True) != json.dumps(expected, sort_keys=True)):
                raise BridgeError("Pi 续接来源、父轮次或终态修订不匹配。")
            initial = Run.from_record(row["child"])
            initial.check_continuation(parent)
            if initial.id in seen or initial.state != "queued" or initial.delivery:
                raise BridgeError("Pi 续接预约重复或不是初始排队状态。")
            incoming = Run.from_record(read_json(endpoint / "runs" / f"{initial.id}.json"))
            if incoming.identity != initial.identity or incoming.seq < initial.seq or (incoming.seq == initial.seq and incoming != initial):
                raise BridgeError("Pi 续接记录身份或修订不匹配。")
            check_history(initial.events, incoming.events)
            incoming.check_continuation(parent)
            result.append(incoming)
            seen.add(incoming.id)
            parent = incoming
        return result

    def read_activity(self, run: Run) -> Activity | None:
        try:
            row = read_json(self._read_endpoint(run) / "activity.json", limit=MAX_ACTIVITY_BYTES)
        except FileNotFoundError:
            return None  # Old bridges have no optional activity file. Never infer zero work.
        return Activity.from_record(row, run)

    def read_run(self, run: Run) -> Run | None:
        endpoint = self._read_endpoint(run)
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
    return f"// Waggle: opt-in only; run /waggle connect after /reload.\nexport {{ default }} from {json.dumps(str(source))};\n"


def bridge_entry(directory: str) -> Path:
    """Reuse our installed entry so explicit loading and discovery cannot double-register."""
    project = Path(directory)
    target = project / ".pi/extensions/waggle.ts"
    for path in (project / ".pi", target.parent, target):
        if path.is_symlink():
            raise BridgeError("项目桥接入口含符号链接，未启动 Pi。")
    legacy = target.with_name("taskroom.ts")
    if legacy.exists() or legacy.is_symlink():
        raise BridgeError(f"发现旧项目入口 {legacy}，可能与 Waggle 桥接重复加载。请先核实旧执行已结束，备份并移出 .pi/extensions 后重试；不会自动迁移、删除入口或禁用其他插件。")
    if target.exists():
        if not target.is_file() or target.read_text() != bridge_loader_content():
            raise BridgeError("已有不同的 waggle.ts，拒绝覆盖或重复加载。")
        return target
    return Path(__file__).with_name("pi-bridge.ts").resolve()


def install_bridge(directory: str) -> Path:
    project = Path(canonical_directory(directory))
    bridge_entry(str(project))  # Same preflight as explicit loading/new Pi; never add a second loader.
    target_dir = project / ".pi/extensions"
    for path in (project / ".pi", target_dir):
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise BridgeError("现有 .pi/extensions 路径不是普通目录，未安装。")
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "waggle.ts"
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
            print(f"已准备 {install_bridge(directory)}\n在该 Pi 中 /reload，然后 /waggle connect；不会自动接管。")
            return 0
        return subprocess.call(["pi", "-e", str(bridge_entry(directory)), *args.args], cwd=directory)
    except (OSError, ValueError) as error:
        print(f"Waggle: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
