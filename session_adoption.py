"""Explicit native-session attachment values; no execution or database migration."""
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from journal import _unique_object, readable_event
from task_context import canonical

if TYPE_CHECKING:
    from model import Board, Run, Task

REQUEST_KIND = "session_adoption_requested"
ATTACHED_KIND = "session_adopted"
REJECTED_KIND = "session_adoption_rejected"
MAX_WAIT_MS = 600_000


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def task_digest(task: "Task") -> str:
    return digest([task.id, task.title, task.project, task.description, task.execution])


def run_digest(run: "Run") -> str:
    return digest(asdict(run))


def _object(value: object, keys: set[str]) -> dict:
    if type(value) is not dict or set(value) != keys:
        raise ValueError("纳管记录字段不兼容。")
    return value


def _text(value: object, limit: int, *, required: bool = False) -> str:
    if type(value) is not str or len(value) > limit or (required and not value.strip()):
        raise ValueError("纳管文字为空、超限或类型错误。")
    value.encode("utf-8")
    return value


def _uuid(value: object) -> str:
    text = _text(value, 36)
    if str(UUID(text)) != text:
        raise ValueError("纳管身份无效。")
    return text


def _integer(value: object, *, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= 2**53 - 1:
        raise ValueError("纳管整数或时间无效。")
    return value


def _digest(value: object) -> str:
    text = _text(value, 64)
    if not re.fullmatch(r"[0-9a-f]{64}", text):
        raise ValueError("纳管来源摘要无效。")
    return text


@dataclass(frozen=True)
class AdoptionSource:
    run_id: str
    agent_id: str
    session_id: str
    cwd: str
    seq: int
    digest: str
    pid: int

    @classmethod
    def from_record(cls, value: object) -> "AdoptionSource":
        row = _object(value, set(cls.__dataclass_fields__))
        source = cls(_uuid(row["run_id"]), _uuid(row["agent_id"]), _uuid(row["session_id"]),
                     _text(row["cwd"], 4096, required=True), _integer(row["seq"]),
                     _digest(row["digest"]), _integer(row["pid"], minimum=1))
        if not source.cwd.startswith("/"):
            raise ValueError("纳管来源必须是精确绝对目录。")
        return source

    def matches(self, run: "Run") -> bool:
        return (run.terminal and (run.id, run.agent_id, run.session_id, run.cwd, run.seq, run_digest(run))
                == (self.run_id, self.agent_id, self.session_id, self.cwd, self.seq, self.digest))


@dataclass(frozen=True)
class AdoptionIntent:
    id: str
    task_id: int
    agent_id: str
    session_id: str
    cwd: str
    pid: int
    task_digest: str
    checkpoint: str
    source: AdoptionSource | None
    previous_checked: bool
    requested_at: int
    expires: int
    request_hash: str

    @classmethod
    def from_record(cls, value: object) -> "AdoptionIntent":
        row = _object(value, {"version", *cls.__dataclass_fields__})
        if type(row["version"]) is not int or row["version"] != 1 or type(row["previous_checked"]) is not bool:
            raise ValueError("纳管版本或人工核对字段错误。")
        source = AdoptionSource.from_record(row["source"]) if row["source"] is not None else None
        intent = cls(_uuid(row["id"]), _integer(row["task_id"], minimum=1), _uuid(row["agent_id"]),
                     _uuid(row["session_id"]), _text(row["cwd"], 4096, required=True),
                     _integer(row["pid"], minimum=1), _digest(row["task_digest"]),
                     _text(row["checkpoint"], 1800, required=True), source, row["previous_checked"],
                     _integer(row["requested_at"], minimum=1), _integer(row["expires"], minimum=1),
                     _digest(row["request_hash"]))
        if (not intent.cwd.startswith("/") or not 0 < intent.expires - intent.requested_at <= MAX_WAIT_MS
                or (source is not None and ((source.session_id, source.cwd) != (intent.session_id, intent.cwd)
                                           or not intent.previous_checked))
                or (source is None and intent.previous_checked)):
            raise ValueError("只允许明确核对后的同一会话接续；不能跨会话接管或继承预算。")
        return intent

    def record(self) -> dict:
        return {"version": 1, **asdict(self)}

    def validate_task(self, board: "Board", task: "Task") -> None:
        if (board.tasks.get(task.id) is not task or task.id != self.task_id or task_digest(task) != self.task_digest
                or task.execution != "pi" or task.retention != "active" or task.status == "done"
                or board.projects.get(task.project) != self.cwd):
            raise ValueError("纳管任务、目标或目录已变化；不能按旧确认关联。")
        if self.source:
            run = board.runs.get(self.source.run_id)
            if (not run or run.task_id != task.id or not self.source.matches(run)
                    or (task.title, task.description) != (run.title, run.description)):
                raise ValueError("纳管原轮次或已结束来源已变化。")
        elif task.run_id:
            run = board.runs.get(task.run_id)
            if not run or run.adoption_id != self.id:
                raise ValueError("没有原轮次的纳管确认不能认领其他执行。")


@dataclass(frozen=True)
class AdoptionRequest:
    intent: AdoptionIntent
    title: str
    project: str
    description: str
    requirements: str

    def record(self) -> dict:
        intent = self.intent
        return {"version": 1, "id": intent.id, "agent_id": intent.agent_id, "session_id": intent.session_id,
                "cwd": intent.cwd, "pid": intent.pid, "task_id": intent.task_id, "title": self.title,
                "project": self.project, "description": self.description, "requirements": self.requirements,
                "checkpoint": intent.checkpoint, "source": asdict(intent.source) if intent.source else None,
                "previous_checked": intent.previous_checked, "requested_at": intent.requested_at,
                "expires": intent.expires}

    @classmethod
    def from_record(cls, value: object) -> "AdoptionRequest":
        keys = {"version", "id", "agent_id", "session_id", "cwd", "pid", "task_id", "title", "project",
                "description", "requirements", "checkpoint", "source", "previous_checked", "requested_at", "expires"}
        row = _object(value, keys)
        title, project = _text(row["title"], 300, required=True), _text(row["project"], 300, required=True)
        description, requirements = _text(row["description"], 20000), _text(row["requirements"], 6000)
        intent = AdoptionIntent.from_record({key: item for key, item in row.items()
                                            if key not in {"title", "project", "description", "requirements"}}
                                           | {"task_digest": digest([row["task_id"], title, project, description, "pi"]),
                                              "request_hash": digest(row)})
        return cls(intent, title, project, description, requirements)

    def validate_run(self, run: "Run") -> None:
        intent = self.intent
        if ((run.task_id, run.agent_id, run.session_id, run.cwd, run.title, run.description, run.requirements)
                != (intent.task_id, intent.agent_id, intent.session_id, intent.cwd, self.title,
                    self.description, self.requirements)
                or run.adoption_id != intent.id
                or run.adoption_parent != (intent.source.run_id if intent.source else "")
                or run.adoption_source_digest != (intent.source.digest if intent.source else "")):
            raise ValueError("纳管后的原生输入与已确认身份、检查点或来源不符。")


@dataclass(frozen=True)
class AdoptionReceipt:
    id: str
    request_hash: str
    agent_id: str
    session_id: str
    cwd: str
    task_id: int
    seq: int
    state: str
    at: int
    accepted_at: int
    branch_anchor: str
    run_id: str
    detail: str

    @classmethod
    def from_record(cls, value: object, intent: AdoptionIntent) -> "AdoptionReceipt":
        row = _object(value, {"version", *cls.__dataclass_fields__})
        if type(row["version"]) is not int or row["version"] != 1:
            raise ValueError("纳管回执版本错误。")
        receipt = cls(_uuid(row["id"]), _digest(row["request_hash"]), _uuid(row["agent_id"]),
                      _uuid(row["session_id"]), _text(row["cwd"], 4096, required=True),
                      _integer(row["task_id"], minimum=1), _integer(row["seq"], minimum=1),
                      _text(row["state"], 16), _integer(row["at"], minimum=1),
                      _integer(row["accepted_at"]), _text(row["branch_anchor"], 128),
                      _uuid(row["run_id"]) if row["run_id"] else "", _text(row["detail"], 600))
        if ((receipt.id, receipt.request_hash, receipt.agent_id, receipt.session_id, receipt.cwd, receipt.task_id)
                != (intent.id, intent.request_hash, intent.agent_id, intent.session_id, intent.cwd, intent.task_id)
                or receipt.state not in {"waiting", "attached", "rejected", "consumed", "detached"}
                or (bool(receipt.run_id) != (receipt.state == "consumed"))
                or receipt.at < intent.requested_at
                or (receipt.state in {"attached", "consumed", "detached"}
                    and not intent.requested_at <= receipt.accepted_at <= min(intent.expires, receipt.at))
                or (receipt.state in {"waiting", "rejected"} and receipt.accepted_at)):
            raise ValueError("纳管回执身份、状态或原始确认时间不符。")
        return receipt

    def check_after(self, previous: "AdoptionReceipt | None") -> None:
        if previous and (self.seq < previous.seq or (self.seq == previous.seq and self != previous)
                         or (previous.state in {"rejected", "consumed", "detached"} and self != previous)
                         or (previous.accepted_at and self.accepted_at != previous.accepted_at)):
            raise ValueError("纳管回执倒退、改写终态或刷新原确认时间。")


def latest_adoption(task: "Task") -> AdoptionIntent | None:
    for raw in reversed(task.events):
        event = readable_event(raw)
        if event and event.kind == REQUEST_KIND:
            if event.source != "human":
                raise ValueError("纳管必须有明确人工确认，不能从普通聊天猜测。")
            row = json.loads(event.text, object_pairs_hook=_unique_object)
            intent = AdoptionIntent.from_record(row)
            if event.text != canonical(intent.record()) or intent.task_id != task.id:
                raise ValueError("纳管意图不是本任务的规范确认记录。")
            return intent
    return None


def adoption_confirmed(task: "Task", intent: AdoptionIntent) -> bool:
    if task.agent != intent.agent_id or latest_adoption(task) != intent:
        return False
    for raw in reversed(task.events):
        event = readable_event(raw)
        if event and event.source == "system" and event.text == intent.id and event.kind in {ATTACHED_KIND, REJECTED_KIND}:
            return event.kind == ATTACHED_KIND
    return False


def validate_adoptions(board: "Board") -> None:
    for task in board.tasks.values():
        for raw in task.events:
            event = readable_event(raw)
            if event and event.kind == REQUEST_KIND:
                intent = AdoptionIntent.from_record(json.loads(event.text, object_pairs_hook=_unique_object))
                if intent.task_id != task.id or event.source != "human" or event.text != canonical(intent.record()):
                    raise ValueError("纳管历史身份或来源不符。")
                if intent.source:
                    source = board.runs.get(intent.source.run_id)
                    if not source or source.task_id != task.id or not intent.source.matches(source):
                        raise ValueError("纳管原轮次必须保留精确终态身份与原始内容。")
