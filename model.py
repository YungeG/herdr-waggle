"""Pure tasks, execution state and immutable human decision records. No I/O."""
from copy import copy
from dataclasses import asdict, dataclass, field, fields, replace
from uuid import UUID, uuid4

from budget_ledger import (
    BudgetGrant,
    BudgetRecordT,
    BudgetScope,
    LedgerVerification,
    UsageObservation,
    append_record,
)
from decisions import (
    EVIDENCE_ONLY_PREFIX,
    DecisionPreview,
    DecisionRecord,
)
from dependencies import (
    DependencyCheck,
    OwnershipReceipt,
    OwnershipRelease,
    TaskDependency,
    check_dependencies,
    check_dependencies_structure,
    dependency_export,
    operation,
    request_for,
    validate_check,
    validate_receipt,
    validate_release,
)
from dependencies import (
    _fresh as validate_dependency,
)
from dependencies import (
    historical_quote_valid as dependency_history_valid,
)
from evidence import (
    EvidenceItem,
    EvidenceVerification,
    evidence_export,
    historical_quote_valid,
    validate_item,
    validate_verification,
)
from journal import (
    append_event,
    check_history,
    encoded_event,
    readable_event,
    validate_events,
)
from session_adoption import (
    ATTACHED_KIND,
    REJECTED_KIND,
    REQUEST_KIND,
    AdoptionIntent,
    AdoptionReceipt,
    AdoptionRequest,
    adoption_confirmed,
    latest_adoption,
    run_digest,
)
from steps import MAX_STEPS, Step, step_time, validate_plan
from task_context import (
    DEPENDENCY_MARKER,
    TaskContext,
    WorkContract,
    canonical,
    check_followup_input,
    context_export,
    has_valid_contract,
    source_base,
    validate_source,
)

PROJECTS = {
    "收集箱": "未选择目录 · 启动前请安排项目",
    "数据实验室": "/demo/data-lab/research/中文样本/feature-pipeline",
    "终端工作台": "/demo/terminal-workbench/packages/task-ui",
    "个人事务": "/demo/personal/notes",
}
AGENTS = ("Pi · 数据助手（虚构）", "Pi · 界面助手（虚构）", "Pi · 通用助手（虚构）")
STATUSES = {
    "draft": "待安排", "ready": "待执行", "running": "执行中",
    "blocked": "阻塞", "review": "待验收", "done": "已验收",
}
RUNS = {"none": "未启动", "queued": "已排队，未确认", "working": "已确认，运行中", "blocked": "等待补充", "finished": "本轮结束，验收独立确认"}

RUN_STATES = {"queued", "working", "waiting", "finished", "failed", "stopped", "interrupted", "abandoned"}
DELIVERIES = {"", "progress", "blocked", "partial", "full"}
RETENTIONS = {"active": "当前任务", "archived": "已归档", "trashed": "回收站"}


def valid_uuid(value: str) -> bool:
    try:
        return str(UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def carried_requirements(*values: str) -> str:
    return "\n\n".join(dict.fromkeys(value.strip() for value in values if value.strip()))


@dataclass
class Run:
    id: str
    task_id: int
    agent_id: str
    session_id: str
    cwd: str
    title: str
    description: str
    requirements: str
    state: str = "queued"
    seq: int = 0
    delivery: str = ""
    summary: str = ""
    checks: list[str] = field(default_factory=list)
    remaining: str = ""
    events: list[str] = field(default_factory=list)

    @property
    def terminal(self) -> bool:
        return self.state in {"finished", "failed", "stopped", "interrupted", "abandoned"}

    @property
    def identity(self) -> tuple:
        return (self.id, self.task_id, self.agent_id, self.session_id, self.cwd,
                self.title, self.description, self.requirements)

    @property
    def continuation_parent(self) -> str:
        return next((event.text for event in validate_events(self.events, run_id=self.id, seq=self.seq)
                     if event.kind == "continued"), "")

    @property
    def adoption_origin(self) -> dict[str, str]:
        import json
        event = next((item for item in validate_events(self.events, run_id=self.id, seq=self.seq)
                      if item.kind == "adopted"), None)
        return json.loads(event.text) if event else {}

    @property
    def adoption_id(self) -> str:
        return self.adoption_origin.get("adoption_id", "")

    @property
    def adoption_parent(self) -> str:
        return self.adoption_origin.get("parent_run_id", "")

    @property
    def adoption_source_digest(self) -> str:
        return self.adoption_origin.get("source_digest", "")

    def check_adoption_parent(self, parent: "Run") -> None:
        if (not parent.terminal or self.adoption_parent != parent.id or self.id == parent.id
                or (self.task_id, self.session_id, self.cwd, self.title, self.description)
                != (parent.task_id, parent.session_id, parent.cwd, parent.title, parent.description)
                or self.adoption_source_digest != run_digest(parent)):
            raise ValueError("纳管只能接续同一会话和目标，原终态轮次不可改写。")

    def check_continuation(self, parent: "Run") -> None:
        base = carried_requirements(parent.requirements, parent.remaining)
        if (not parent.terminal or parent.state == "abandoned" or self.continuation_parent != parent.id
                or self.identity[1:-1] != parent.identity[1:-1] or self.id == parent.id or self.seq < 1
                or len(self.requirements) > 6000
                or (base and self.requirements != base and not self.requirements.startswith(base + "\n\n"))):
            raise ValueError("续接身份、父轮次终态或继承要求不一致。")

    @classmethod
    def from_record(cls, row: object) -> "Run":
        if not isinstance(row, dict) or set(row) != {item.name for item in fields(cls)}:
            raise ValueError("执行记录字段不兼容。")
        for name, value in row.items():
            expected = int if name in ("task_id", "seq") else list if name in ("checks", "events") else str
            if type(value) is not expected or (isinstance(value, list) and any(type(item) is not str for item in value)):
                raise ValueError(f"执行字段 {name} 类型错误。")
        run = cls(**row)
        if not all(valid_uuid(value) for value in (run.id, run.agent_id, run.session_id)):
            raise ValueError("执行/连接/会话身份无效。")
        if run.task_id <= 0 or run.seq < 0 or not run.cwd.startswith("/") or not run.title.strip():
            raise ValueError("执行任务、目录或版本无效。")
        if run.state not in RUN_STATES or run.delivery not in DELIVERIES:
            raise ValueError("执行状态不兼容。")
        if run.delivery == "full" and run.requirements.startswith(EVIDENCE_ONLY_PREFIX):
            raise ValueError("仅补证轮不能冒充整个任务的完整交付。")
        if run.delivery == "full" and (not run.summary.strip() or not run.checks or run.remaining.strip()):
            raise ValueError("完整交付必须有摘要、证据且无剩余工作。")
        validate_events(run.events, run_id=run.id, seq=run.seq)
        return run


SUPPLEMENT_STATES = {"pending": "等待接收确认", "queued": "已排入 Pi 消息队列", "received": "Pi 已接收，待报告覆盖", "rejected": "未接收，需核实", "covered": "执行者报告已覆盖"}


@dataclass(frozen=True)
class Supplement:
    id: str
    run_id: str
    text: str
    expires: int
    status: str = "pending"
    seq: int = 0
    run_seq: int = 0
    detail: str = ""

    @property
    def identity(self) -> tuple:
        return self.id, self.run_id, self.text, self.expires

    @classmethod
    def from_record(cls, row: object) -> "Supplement":
        if not isinstance(row, dict) or set(row) != {item.name for item in fields(cls)}:
            raise ValueError("补充说明字段不兼容。")
        for key, value in row.items():
            if type(value) is not (int if key in {"expires", "seq", "run_seq"} else str):
                raise ValueError("补充说明字段类型错误。")
        note = cls(**row)
        if not valid_uuid(note.id) or not valid_uuid(note.run_id) or note.status not in SUPPLEMENT_STATES:
            raise ValueError("补充说明身份或状态错误。")
        if not note.text.strip() or len(note.text) > 4000 or len(note.detail) > 600:
            raise ValueError("补充说明需要 1–4000 字符，回执说明最多 600 字符。")
        note.text.encode("utf-8")
        note.detail.encode("utf-8")
        if not 0 < note.expires <= 2**53 - 1 or not 0 <= note.run_seq <= 2**53 - 1 or not 0 <= note.seq <= 2**53 - 1:
            raise ValueError("补充说明修订或有效期错误。")
        if (note.status == "pending" and (note.seq or note.run_seq or note.detail)) or (note.status != "pending" and note.seq == 0) or (note.status in {"queued", "received", "covered"} and note.run_seq == 0):
            raise ValueError("补充说明回执修订错误。")
        return note

    def check_after(self, previous: "Supplement") -> None:
        if self.identity != previous.identity or self.seq < previous.seq or (self.seq == previous.seq and self != previous):
            raise ValueError("补充身份不匹配、回执倒退或同修订改写。")
        allowed = {"pending": {"queued", "received", "rejected", "covered"}, "queued": {"queued", "received", "rejected", "covered"}, "received": {"received", "covered"}}
        if self != previous and (self.status not in allowed.get(previous.status, set()) or self.run_seq < previous.run_seq):
            raise ValueError("补充说明回执状态不能回退或改写终态。")


@dataclass
class Task:
    id: int
    title: str
    project: str
    description: str = ""
    status: str = "draft"
    agent: str = ""
    run: str = "none"
    blocker: str = ""
    summary: str = ""
    checks: list[str] = field(default_factory=list)
    remaining: str = ""
    full_delivery: bool = False
    events: list[str] = field(default_factory=lambda: ["任务创建 · 未发起执行"])
    execution: str = "simulation"
    run_id: str = ""
    retention: str = "active"
    steps: list[Step] = field(default_factory=list)

    @property
    def can_accept(self) -> bool:
        return self.status == "review" and self.full_delivery and not self.remaining


class Board:
    def __init__(self, live: bool = False) -> None:
        self.live = live
        self.projects: dict[str, str] = {"收集箱": ""} if live else dict(PROJECTS)
        self.tasks: dict[int, Task] = {}
        self.runs: dict[str, Run] = {}
        self.supplements: dict[str, Supplement] = {}
        self.decisions: dict[str, DecisionRecord] = {}
        self.task_contexts: dict[str, TaskContext] = {}
        self.evidence_items: dict[str, EvidenceItem] = {}
        self.evidence_verifications: dict[str, EvidenceVerification] = {}
        self.task_dependencies: dict[str, TaskDependency] = {}
        self.dependency_checks: dict[str, DependencyCheck] = {}
        self.ownership_releases: dict[str, OwnershipRelease] = {}
        self.ownership_receipts: dict[str, OwnershipReceipt] = {}
        self.budget_scopes: dict[str, BudgetScope] = {}
        self.usage_observations: dict[str, UsageObservation] = {}
        self.ledger_verifications: dict[str, LedgerVerification] = {}
        self.budget_grants: dict[str, BudgetGrant] = {}
        self.next_task_id = 1

    def _require_active(self, task: Task) -> None:
        if self.tasks.get(task.id) is not task or task.retention != "active":
            raise ValueError("任务已变化或不在当前任务中；请先恢复，再执行操作。")

    def _require_step_edit(self, task: Task) -> None:
        self._require_active(task)
        if task.status == "done":
            raise ValueError("已验收任务的步骤只读；请另建后续任务。")

    def check_steps(self, task: Task, steps: list[Step]) -> None:
        validate_plan(steps)
        for step in steps:
            for run_id in step.run_ids:
                run = self.runs.get(run_id)
                if not run or run.task_id != task.id:
                    raise ValueError("步骤只能关联本任务已保存的轮次。")

    def add_steps(self, task: Task, titles: list[str]) -> None:
        self._require_step_edit(task)
        if type(titles) is not list or not titles or any(type(title) is not str for title in titles):
            raise ValueError("请逐行填写步骤标题，不从报告自动生成计划。")
        if len(task.steps) + len(titles) > MAX_STEPS:
            raise ValueError("每个任务最多 20 个步骤。")
        added = [Step.from_record({item.name: getattr(step, item.name) for item in fields(Step)})
                 for step in (Step(str(uuid4()), title.strip(), step_time()) for title in titles)]
        candidate = [*task.steps, *added]
        self.check_steps(task, candidate)
        self.note(task, "step_added" if task.steps else "plan_defined",
                  "人工确认步骤计划 · 仅本地跟踪，不派发\n" + "\n".join(f"{step.id} · {step.title}" for step in added))
        task.steps = candidate

    def update_step(self, task: Task, step_id: str, *, title: str, goal: str, status: str,
                    output: str, evidence: str, blocker: str, next_action: str, run_ids: list[str]) -> None:
        self._require_step_edit(task)
        old = next((step for step in task.steps if step.id == step_id), None)
        if old is None:
            raise ValueError("步骤已变化或不存在，请重新打开。")
        values = {"title": title, "goal": goal, "status": status, "output": output,
                  "evidence": evidence, "blocker": blocker, "next_action": next_action}
        if any(type(value) is not str for value in values.values()):
            raise ValueError("步骤字段必须是文字。")
        step = Step.from_record({"id": old.id, "updated_at": step_time(), "source": "human",
                                **{key: value.strip() for key, value in values.items()}, "run_ids": run_ids})
        candidate = [step if item.id == step.id else item for item in task.steps]
        self.check_steps(task, candidate)
        self.note(task, "step_updated", f"人工更新步骤 · {step.id} · {step.title} · {step.status}\n仅本地跟踪，不派发、不验收。")
        task.steps = candidate

    def run_supplements(self, run_id: str) -> list[Supplement]:
        return [note for note in self.supplements.values() if note.run_id == run_id]

    def lineage(self, run: Run) -> list[Run]:
        result, seen = [run], {run.id}
        while parent_id := result[-1].continuation_parent or result[-1].adoption_parent:
            parent = self.runs.get(parent_id)
            if parent is None or parent.id in seen:
                raise ValueError("续接历史缺少父轮次或包含循环。")
            if result[-1].adoption_parent:
                result[-1].check_adoption_parent(parent)
            else:
                result[-1].check_continuation(parent)
            result.append(parent)
            seen.add(parent.id)
        return result

    def supplements_satisfied(self, run: Run) -> bool:
        if not all(note.status == "covered" and note.run_seq <= run.seq for note in self.run_supplements(run.id)):
            return False
        return not self.uninherited_supplements(run)

    def uninherited_supplements(self, run: Run) -> list[Supplement]:
        return [note for parent in self.lineage(run)[1:] for note in self.run_supplements(parent.id)
                if note.status != "covered" and note.text not in run.requirements]

    def supplement_mode(self, task: Task) -> str:
        self._require_active(task)
        if task.status == "done":
            raise ValueError("任务已验收；请另建后续任务，不修改已验收交付。")
        run = self.runs.get(task.run_id)
        if task.execution == "pi":
            if (task.run_id and (not run or run.task_id != task.id)) or (task.run != "none" and not run):
                raise ValueError("原轮次信息缺失或不匹配，请到原 Pi 核实。")
            if any(not item.terminal and item.task_id == task.id and (task.run == "none" or item.id != task.run_id) for item in self.runs.values()):
                raise ValueError("仍有未确认结束的执行，不能保存下一轮要求。")
        if task.run == "none" and task.status in {"draft", "ready"}:
            return "pending"
        if task.execution == "simulation":
            if task.run == "working":
                return "simulation"
            if task.status in {"review", "blocked"}:
                return "next"
        elif run:
            if run.terminal and task.status in {"review", "blocked"}:
                return "next"
            if run.state == "working":
                notes = self.run_supplements(run.id)
                if len(notes) >= 16 or any(note.status in {"pending", "queued"} for note in notes):
                    raise ValueError("已有补充尚未确认接收或本轮已达上限，请核实，不重复发送。")
                updates = [event for raw in run.events if (event := readable_event(raw)) is not None]
                updates.extend(event for raw in task.events if (event := readable_event(raw)) is not None and event.run_id == run.id)
                if not any(event.kind == "stop_requested" or (event.kind == "invalidated" and event.text.startswith("Pi 收到")) for event in updates):
                    return "live"
        raise ValueError("本轮排队、等待确认或停止/归属未确认；请先到原 Pi 核实，不会转为下一轮补充。")

    def supplement_prefill(self, task: Task, mode: str) -> str:
        if mode == "live" or mode == "simulation":
            return ""
        run = self.runs.get(task.run_id)
        notes = [note for attempt in self.lineage(run) for note in self.run_supplements(attempt.id)] if mode == "next" and run else []
        unresolved = [note.text for note in notes if note.status != "covered"]
        return "\n\n".join(([task.remaining] if task.remaining and (mode == "pending" or unresolved) else []) + (["本轮尚未确认覆盖的补充，请核对后编辑：\n" + "\n\n".join(unresolved)] if unresolved else []))

    def save_supplement(self, task: Task, text: str, expected_mode: str) -> None:
        mode = self.supplement_mode(task)
        if mode != expected_mode or mode == "live" or not text.strip() or len(text) > 6000:
            raise ValueError("任务状态已变化或补充内容无效，请重新核对。")
        if mode == "next":
            self.return_work(task, text)
        else:
            task.remaining = text.strip()
            if mode == "simulation":
                task.full_delivery = False
            self.note(task, "supplement_saved", ("模拟本轮补充 · " if mode == "simulation" else "待派发补充要求 · ") + text.strip())

    def prepare_supplement(self, task: Task, note_id: str, text: str, expires: int, *, decision: DecisionPreview | None = None) -> Supplement:
        from decisions import request_only_decisions, validate_preview
        if decision is not None:
            validate_preview(self, decision)
            if decision.task_id != task.id or decision.target.kind != 'live_supplement' or decision.compiled_text != text:
                raise ValueError('本轮补充决定与精确文本/目标不符。')
        purpose = 'evidence' if decision is not None and request_only_decisions(decision.specs, decision.answers) else operation(text)
        self.check_dependencies(task, purpose, agent_id=task.agent, requirements=text)
        if self.supplement_mode(task) != "live":
            raise ValueError("只能向仍明确执行的本轮发送补充。")
        notes = self.run_supplements(task.run_id)
        if note_id in self.supplements or len(notes) >= 16 or any(note.status in {"pending", "queued"} for note in notes):
            raise ValueError("本轮已有未确认补充或达到 16 条上限；请核实，不重复投递。")
        note = Supplement.from_record({"id": note_id, "run_id": task.run_id, "text": text.strip(), "expires": expires,
                                       "status": "pending", "seq": 0, "run_seq": 0, "detail": ""})
        self.supplements[note.id] = note
        task.full_delivery = False
        self.note(task, "supplement_requested", f"本轮补充 {note.id} · 等待 Pi 接收\n{note.text}", run_id=task.run_id)
        return note

    def apply_supplement(self, incoming: Supplement) -> bool:
        incoming = Supplement.from_record({item.name: getattr(incoming, item.name) for item in fields(Supplement)})
        previous = self.supplements.get(incoming.id)
        if previous is None:
            raise ValueError("未保存本轮补充意图，不能接管未知回执。")
        incoming.check_after(previous)
        run = self.runs[incoming.run_id]
        if incoming == previous or incoming.run_seq > run.seq:
            return False  # Re-read after the corresponding Run revision; never infer report coverage early.
        self.supplements[incoming.id] = incoming
        task = self.tasks[run.task_id]
        if incoming.status != previous.status:
            self.note(task, "supplement_" + incoming.status, f"补充 {incoming.id} · {SUPPLEMENT_STATES[incoming.status]}\n{incoming.detail}", run_id=run.id, source="system")
        if task.run_id == run.id and task.run == "finished" and task.status != "done":
            task.full_delivery = run.state == "finished" and run.delivery == "full" and self.supplements_satisfied(run)
        return True

    def retirement_target(self, task: Task) -> str:
        """Reject uncertain execution and recommend a retention view for ended tasks."""
        if self.tasks.get(task.id) is not task:
            raise ValueError("任务已变化，请重新选择。")
        runs = [run for run in self.runs.values() if run.task_id == task.id]
        current = self.runs.get(task.run_id)
        if task.run_id and (not current or current.task_id != task.id):
            raise ValueError("原执行身份缺失或不匹配；先核实，不能删除或归档。")
        if task.execution == "pi" and task.run != "none" and not current:
            raise ValueError("原执行记录缺失；不能把未知执行当成未派发。")
        if task.run in {"queued", "working", "blocked"} or task.status == "running" or any(not run.terminal for run in runs):
            raise ValueError("执行或停止尚未确认结束；失联不等于停止，不能删除或归档。")
        execution_kinds = {"dispatched", "acknowledged", "progress", "blocked", "partial", "full", "waiting", "stop_requested",
                           "stopped", "settled", "failed", "interrupted", "invalidated", "returned", "accepted", "abandoned", "result_received"}
        recorded_execution = any(event.run_id or event.kind in execution_kinds
                                 for raw in task.events if (event := readable_event(raw)) is not None)
        if task.execution == "pi" and recorded_execution and not runs:
            raise ValueError("存在执行记录但原轮次缺失；先核实，不能删除或归档。")
        history = bool(runs or recorded_execution or task.run_id or task.run != "none" or task.summary or task.checks or task.full_delivery
                       or task.status in {"blocked", "review", "done"})
        return "archived" if history else "trashed"

    def retire(self, task: Task, destination: str) -> None:
        target = self.retirement_target(task)
        if task.retention not in {"active", "archived"} or destination not in {"archived", "trashed"}:
            raise ValueError("任务不在可删除或归档的状态，请重新确认。")
        if destination == "archived" and (task.retention != "active" or target != "archived"):
            raise ValueError("任务的执行历史已变化，请重新确认。")
        self.note(task, destination, "人工归档任务 · 保留成果与执行记录" if destination == "archived" else "人工移入回收站 · 保留任务 ID、成果与历史，可恢复")
        task.retention = destination

    def restore(self, task: Task) -> None:
        target = self.retirement_target(task)
        if task.retention not in {"archived", "trashed"} or (task.retention == "archived" and target != "archived"):
            raise ValueError("任务不在可恢复的归档或回收站中。")
        self.note(task, "restored", "人工恢复任务 · 仅恢复可见，不启动执行或改变验收")
        task.retention = "active"

    def purge(self, task: Task) -> None:
        self.retirement_target(task)
        if task.retention != "trashed":
            raise ValueError("只能永久删除回收站中的任务；请先移入回收站。")
        # Check if this task is referenced as a parent by any TaskContext
        for ctx in self.task_contexts.values():
            if ctx.parent_task_id == task.id:
                raise ValueError("该任务被后续任务引用为来源，不能永久删除；请先删除后续任务。")
        if any(item.source_task_id == task.id and item.owner_task_id != task.id for item in self.evidence_items.values()):
            raise ValueError('该任务的报告或人工事实被其他任务库存引用，不能永久删除。')
        if any(dep.source_task_id == task.id and dep.owner_task_id != task.id for dep in self.task_dependencies.values()):
            raise ValueError('该任务被明确局部依赖引用，不能永久删除来源或写者事实。')
        removed = {run.id for run in self.runs.values() if run.task_id == task.id}
        self.supplements = {key: note for key, note in self.supplements.items() if note.run_id not in removed}
        self.runs = {key: run for key, run in self.runs.items() if run.task_id != task.id}
        self.decisions = {key: record for key, record in self.decisions.items() if record.task_id != task.id}
        # Remove TaskContexts where this task is the child
        self.task_contexts = {key: ctx for key, ctx in self.task_contexts.items() if ctx.child_task_id != task.id}
        self.evidence_items = {key: item for key, item in self.evidence_items.items() if item.owner_task_id != task.id}
        self.evidence_verifications = {key: record for key, record in self.evidence_verifications.items()
                                       if record.item_id in self.evidence_items}
        self.task_dependencies = {key: dep for key, dep in self.task_dependencies.items() if dep.owner_task_id != task.id}
        self.dependency_checks = {key: record for key, record in self.dependency_checks.items() if record.dependency_id in self.task_dependencies}
        self.ownership_releases = {key: record for key, record in self.ownership_releases.items() if record.dependency_id in self.task_dependencies}
        self.ownership_receipts = {key: record for key, record in self.ownership_receipts.items() if record.release_id in self.ownership_releases}
        del self.tasks[task.id]

    def project_tasks_for_deletion(self, name: str) -> list[Task]:
        if name == "收集箱" or name not in self.projects:
            raise ValueError("收集箱不能删除；请选择仍已登记的具体项目。")
        tasks = [task for task in self.tasks.values() if task.project == name]
        for task in tasks:
            try:
                self.retirement_target(task)
            except ValueError as error:
                raise ValueError(f"D{task.id:03} 阻止删除项目：{error}") from error
        return tasks

    def delete_project(self, name: str) -> None:
        if any(self.tasks[dep.source_task_id].project == name or self.tasks[dep.owner_task_id].project == name
               for dep in self.task_dependencies.values()):
            raise ValueError('项目包含来源绑定的局部依赖；先处理引用，不能使原写者/接续者目录失效。')
        if any(self.tasks[ctx.parent_task_id].project == name or self.tasks[ctx.child_task_id].project == name
               for ctx in self.task_contexts.values()):
            raise ValueError("项目含明确前序引用；先处理关联任务，不能使来源目录失效。")
        if any(self.tasks[item.source_task_id].project == name or self.tasks[item.owner_task_id].project == name
               for item in self.evidence_items.values()):
            raise ValueError('项目包含来源绑定的成果库存；先处理引用，不能使精确目录失效。')
        tasks = self.project_tasks_for_deletion(name)  # Check every member before changing any.
        directory = self.projects[name]
        for task in tasks:
            if task.retention != "trashed":
                self.retire(task, "trashed")
            self.note(task, "project_deleted", f"人工删除项目登记 · {name} · {directory}\n任务及历史移入回收站；原目录和文件未改动。")
            task.project = "收集箱"
        del self.projects[name]

    def add_project(self, name: str, canonical_path: str) -> None:
        name = name.strip()
        if not name or name in self.projects or name in {"all", "__add_project__"}:
            raise ValueError("项目名称不能为空、重复或使用保留名称。")
        if not canonical_path.startswith("/") or canonical_path in self.projects.values():
            raise ValueError("需要唯一的绝对目录；不会回退到 Git 根目录。")
        self.projects[name] = canonical_path

    def create(self, title: str, project: str, description: str = "") -> Task:
        if not title.strip():
            raise ValueError("请先写一个任务标题。")
        if project not in self.projects:
            raise ValueError("请选择已登记的项目。")
        task = Task(self.next_task_id, title.strip(), project, description.strip(),
                    events=[encoded_event("created", "任务创建 · 未发起执行", source="human") if self.live else "任务创建 · 未发起执行"],
                    execution="pi" if self.live else "simulation")
        self.next_task_id += 1
        self.tasks[task.id] = task
        return task

    def note(self, task: Task, kind: str, text: str, *, run_id: str = "", source: str = "human") -> None:
        """Human/client records stay task-owned; bind only an explicitly chosen round."""
        if task.execution == "simulation":
            task.events.append(text)  # Fictional demo history remains timeless and deterministic.
            return
        run = self.runs.get(run_id)
        if run_id and (not run or run.task_id != task.id):
            raise ValueError("关键记录不能关联其他任务或不存在的执行。")
        if len(text) > 6000:
            text = text[:5960] + "…（记录文字超出上限）"
        value = encoded_event(kind, text, source=source, run_id=run_id, seq=run.seq if run else 0)
        task.events = append_event(task.events, value)

    def edit(self, task: Task, title: str, project: str, description: str) -> None:
        self._require_active(task)
        if task.execution == "pi" and task.run != "none":
            raise ValueError("本轮输入已固定；请先结束执行并通过补充/退回安排新一轮。")
        if not title.strip() or project not in self.projects:
            raise ValueError("标题不能为空，项目必须已登记。")
        if task.run in ("queued", "working", "blocked") and project != task.project:
            raise ValueError("执行尚未结束，不能更换目录。")
        if project != task.project and any(ctx.child_task_id == task.id for ctx in self.task_contexts.values()):
            raise ValueError("后续任务已绑定精确前序目录；不能悄悄更换，请另建草稿重新核对来源。")
        if project != task.project and task.execution == "pi":
            task.agent, task.status = "", "draft"
        task.title, task.project, task.description = title.strip(), project, description.strip()
        self.note(task, "edited", "人工编辑 · 未自动启动")

    def assign(self, task: Task, agent: str) -> None:
        self._require_active(task)
        valid_agent = agent in AGENTS if task.execution == "simulation" else valid_uuid(agent)
        if not valid_agent or task.status not in ("draft", "ready") or task.run != "none":
            raise ValueError("仅未启动的待安排/待执行任务可分配。")
        if task.execution == "pi":
            self.check_dependencies(task, operation(request_for(self, task)), agent_id=agent)
        task.agent, task.status = agent, "ready"
        self.note(task, "assigned", f"人工分配给 {agent} · 尚未启动")

    def queue(self, task: Task) -> None:
        self._require_active(task)
        if task.execution != "simulation":
            raise ValueError("真实任务必须通过已连接 Pi 派发。")
        if not task.agent or task.project == "收集箱":
            raise ValueError("先选择具体项目并分配执行者，再模拟启动。")
        if task.status != "ready" or task.run != "none":
            raise ValueError("任务已启动或尚未准备好；不会重复启动。")
        task.run = "queued"
        task.events.append("模拟排队 · 尚未获得执行确认")

    def simulate(self, task: Task, action: str, reason: str = "") -> None:
        self._require_active(task)
        if task.execution != "simulation":
            raise ValueError("真实任务不能模拟进展。")
        if action == "ack" and task.run == "queued":
            task.run, task.status = "working", "running"
            task.events.append("模拟执行者确认 · 开始处理")
        elif action == "progress" and task.run == "working":
            task.events.append("模拟进展 · 已梳理样例，正在补充边界检查")
        elif action == "block" and task.run == "working":
            if not reason.strip():
                raise ValueError("请填写阻塞原因。")
            task.run, task.status, task.blocker = "blocked", "blocked", reason.strip()
            task.events.append(f"模拟阻塞 · {task.blocker}")
        elif action in ("full", "partial") and task.run == "working":
            task.run, task.status = "finished", "review"
            task.full_delivery = action == "full"
            task.summary = "样例方案和回归说明已整理。" if task.full_delivery else "仅完成第一阶段：样例与方案整理。"
            task.checks = ["模拟通过 · 基础流程（没有运行真实测试）", "模拟通过 · 中文及空输入边界（非真实证据）"]
            task.remaining = "" if task.full_delivery else "第二阶段实现与验证尚未完成，不能验收整个任务。"
            task.events.append("模拟整任务交付 · 等待人工验收" if task.full_delivery else "模拟阶段交付 · 整任务仍未完成")
        else:
            raise ValueError("当前状态不支持这个模拟动作。")

    def accept(self, task: Task) -> None:
        self._require_active(task)
        run = self.runs.get(task.run_id)
        if task.execution == "pi" and (not run or run.state != "finished" or run.delivery != "full" or not self.supplements_satisfied(run)):
            raise ValueError("Pi 尚未完整交付且完全空闲，不能验收。")
        if not task.can_accept:
            raise ValueError("仅完整交付、无剩余工作的任务可人工验收。")
        task.status = "done"
        self.note(task, "accepted", "人工验收 · 已确认整任务成果" if task.execution == "pi" else "人工验收样例 · 这是本地交互演示，不代表真实成果通过", run_id=task.run_id)

    def return_work(self, task: Task, reason: str) -> None:
        self._require_active(task)
        if task.execution == "pi" and task.run_id and not self.runs[task.run_id].terminal:
            raise ValueError("执行尚未确认结束；请先停止，不能并发重派。")
        if task.status not in ("review", "blocked") or not reason.strip():
            raise ValueError("请给待验收或阻塞任务填写补充/退回原因。")
        self.note(task, "returned", f"人工补充/退回 · {reason.strip()}", run_id=task.run_id)
        task.status, task.run = "ready", "none"
        task.blocker, task.remaining = "", reason.strip()
        task.full_delivery = False
        task.summary = "上轮交付已退回；补充要求见剩余工作和时间线。"
        task.checks = []

    def queue_real(self, task: Task, run_id: str, session_id: str, cwd: str, *, decision: DecisionPreview | None = None) -> Run:
        self._require_active(task)
        previous = self.runs.get(task.run_id) if task.run_id else None
        if task.run_id and (previous is None or previous.task_id != task.id):
            raise ValueError('上轮身份缺失或不匹配；不会重派。')
        agent_id = task.agent
        if decision is not None:
            # Validate on the ORIGINAL canonical source, before changing remaining/status.
            from decisions import validate_preview
            try:
                validate_preview(self, decision)
            except ValueError as error:
                raise ValueError(f"正式决定预览与来源或请求不匹配：{error}") from error
            if (not previous or not previous.terminal or decision.task_id != task.id
                    or decision.source.run_id != previous.id or decision.target.kind != 'new_run'
                    or (decision.target.session_id, decision.target.cwd) != (session_id, cwd)):
                raise ValueError('正式决定预览与本次派发身份不匹配。')
            agent_id = decision.target.agent_id
            requirements = self.compiled_requirements(task, decision.compiled_text)
            if requirements != decision.compiled_text:
                raise ValueError('正式决定预览与完整请求不匹配。')
        else:
            if task.status != 'ready' or task.run != 'none':
                raise ValueError('请先明确本轮合同并分配 Pi；不会重复派发。')
            requirements = self.compiled_requirements(task)
            # Source data is appended separately and cannot make an incomplete contract valid.
            base = source_base(requirements)
            if not has_valid_contract(base):
                raise ValueError('真实派发必须明确交付物、允许范围、验收依据与排除项；不能只写继续或留空。')
        if task.execution != 'pi' or not valid_uuid(agent_id):
            raise ValueError('请先分配已连接的 Pi。')
        if len(task.title) > 300 or len(task.description) > 20000:
            raise ValueError('派发上限：标题 300、说明 20000 字。')
        if not cwd or cwd != self.projects[task.project]:
            raise ValueError('Pi 必须在所选项目的精确目录中。')
        if not valid_uuid(run_id) or not valid_uuid(session_id):
            raise ValueError('执行身份无效。')
        if run_id in self.runs or any(not run.terminal and (run.agent_id == agent_id or run.task_id == task.id
                                                           or (run.session_id, run.cwd) == (session_id, cwd))
                                      for run in self.runs.values()):
            raise ValueError('任务或 Pi 仍有未确认结束的执行，不能重派。')
        from decisions import request_only_decisions
        purpose = 'evidence' if decision is not None and request_only_decisions(decision.specs, decision.answers) else operation(requirements)
        self.check_dependencies(task, purpose, agent_id=agent_id, requirements=requirements)
        run = Run(run_id, task.id, agent_id, session_id, cwd, task.title, task.description, requirements)
        if decision is not None:
            self.save_supplement(task, requirements, 'next')
            # The validated preview and dependency gate above already prove this exact purpose.
            # Do not reclassify its genuine structured evidence packet as arbitrary free text.
            task.agent, task.status = agent_id, 'ready'
            self.note(task, 'assigned', f'人工分配给 {agent_id} · 尚未启动')
        self.runs[run.id] = run
        task.run_id, task.run = run.id, 'queued'
        task.full_delivery, task.summary, task.checks = False, '', []
        self.note(task, 'dispatched', '派发意图已保存 · 等待 Pi 确认（未自动重试）', run_id=run.id)
        return run

    def create_followup(self, parent_task_id: int, context: TaskContext, new_title: str, new_goal: str) -> Task:
        """Only save an independently named draft; do not copy prior execution grants."""
        parent = self.tasks.get(parent_task_id)
        if (not parent or context.parent_task_id != parent_task_id or context.child_task_id != 0
                or context.id in self.task_contexts):
            raise ValueError('前序身份或上下文已变化；不会重复创建。')
        check_followup_input(new_title, new_goal)
        validate_source(self, context)
        task = self.create(new_title, parent.project, new_goal)
        self.task_contexts[context.id] = replace(context, child_task_id=task.id)
        return task

    def prepare_recovery(self, task: Task, source: Run, checkpoint: str, remaining: str, contract: WorkContract) -> None:
        """Save a new explicit request only; keep the exact terminal source unchanged."""
        from failure_view import recovery_request
        self._require_active(task)
        current = self.runs.get(task.run_id)
        if (task.status == 'done' or task.execution != 'pi' or current is None or current != source
                or current.task_id != task.id or not current.terminal
                or task.status not in {'blocked', 'review', 'ready'}
                or (task.title, task.description) != (current.title, current.description)
                or self.projects.get(task.project) != current.cwd
                or any(not run.terminal and run.task_id == task.id for run in self.runs.values())):
            raise ValueError('恢复来源或任务状态已变化；旧执行必须已确认结束，已验收任务须另建后续。')
        text = recovery_request(task, current, contract, checkpoint, remaining)
        self.compiled_requirements(task, text)  # Source/size checks precede every mutation.
        self.note(task, 'returned', '人工准备恢复草稿 · 不派发、不重放；来源 Run ' + current.id, run_id=current.id)
        task.remaining, task.run, task.status = text, 'none', 'ready' if task.agent else 'draft'
        task.full_delivery, task.blocker = False, ''
        task.summary, task.checks = '恢复请求仅本地保存；旧轮错误和进度仍在历史记录。', []

    def request_adoption(self, task: Task, intent: AdoptionIntent) -> None:
        """Persist explicit attachment consent before publication; never create a Run."""
        self._require_active(task)
        intent = AdoptionIntent.from_record(intent.record())
        intent.validate_task(self, task)
        source_id = intent.source.run_id if intent.source else ""
        if task.run_id != source_id or any(not run.terminal and (run.task_id == task.id
                or (run.session_id, run.cwd) == (intent.session_id, intent.cwd)) for run in self.runs.values()):
            raise ValueError("原轮次或同一会话仍有未确认结束的执行；不能重复纳管。")
        previous = latest_adoption(task)
        if previous == intent:
            return
        if previous and not adoption_confirmed(task, previous) and not any(
                event and event.kind == REJECTED_KIND and event.text == previous.id
                for raw in task.events if (event := readable_event(raw)) is not None):
            raise ValueError("已有纳管接收尚未确认；只核对原意图，不生成新 ID 重试。")
        self.note(task, REQUEST_KIND, canonical(intent.record()), run_id=source_id)

    def apply_adoption(self, intent: AdoptionIntent, receipt: AdoptionReceipt) -> bool:
        receipt = AdoptionReceipt.from_record({"version": 1, **asdict(receipt)}, intent)
        task = self.tasks[intent.task_id]
        intent.validate_task(self, task)
        if latest_adoption(task) != intent:
            raise ValueError("纳管意图已变化；不能接管迟到回执。")
        if receipt.state == "waiting":
            return False
        kind = ATTACHED_KIND if receipt.state in {"attached", "consumed"} else REJECTED_KIND
        if any(event and event.kind == kind and event.text == intent.id
               for raw in task.events if (event := readable_event(raw)) is not None):
            return False
        source_id = intent.source.run_id if intent.source else ""
        if task.run_id != source_id:
            raise ValueError("任务已开始另一轮；旧纳管回执不改变当前任务。")
        self.note(task, kind, intent.id, run_id=source_id, source="system")
        if kind == ATTACHED_KIND:
            task.agent = intent.agent_id
            if task.status == "draft":
                task.status = "ready"
        return True

    def ingest_adopted(self, request: AdoptionRequest, incoming: Run) -> bool:
        """Receive only the first explicitly attached native input, not past conversation."""
        incoming = Run.from_record(asdict(incoming))
        request.validate_run(incoming)
        intent = request.intent
        task = self.tasks[intent.task_id]
        intent.validate_task(self, task)
        if latest_adoption(task) != intent or not adoption_confirmed(task, intent):
            raise ValueError("没有本任务已确认的会话关联；不接管原生输入。")
        if incoming.id in self.runs:
            return self.ingest(incoming)
        if (task.run_id != (intent.source.run_id if intent.source else "")
                or any(run.adoption_id == intent.id or (not run.terminal and
                       (run.task_id == task.id or (run.session_id, run.cwd) == (intent.session_id, intent.cwd)))
                       for run in self.runs.values())):
            raise ValueError("纳管已有消费或其他执行；不接管分叉。")
        self.runs[incoming.id] = replace(incoming, state="queued", seq=0, delivery="", summary="", checks=[], remaining="", events=[])
        task.run_id, task.run, task.full_delivery = incoming.id, "queued", False
        return self.ingest(incoming)

    def set_work_contract(self, task: Task, contract: WorkContract) -> None:
        from failure_view import RECOVERY_END, RECOVERY_MARKER, split_recovery
        self._require_active(task)
        if task.status not in ('draft', 'ready') or task.run != 'none':
            raise ValueError('只有未启动的草稿或待执行任务可以设置本轮合同。')
        compiled = contract.compile()
        recovery = split_recovery(source_base(task.remaining))
        if recovery:
            compiled += RECOVERY_MARKER + recovery[1] + RECOVERY_END
        self.compiled_requirements(task, compiled)  # Validate source/length before changing the draft.
        self.note(task, 'contract_set', f'人工设置工作合同 · 目的：{contract.purpose}')
        task.remaining = compiled

    def save_budget_record(self, record: BudgetRecordT) -> BudgetRecordT:
        """Append local accounting only; no assignment, Run, mailbox or decision."""
        return append_record(self, record)

    def _source_exports(self, task: Task, *, extra_item: EvidenceItem | None = None,
                        extra_verification: EvidenceVerification | None = None) -> str:
        contexts = [ctx for ctx in self.task_contexts.values() if ctx.child_task_id == task.id]
        if len(contexts) > 1:
            raise ValueError('后续任务来源冲突；不会选择任意一个来源。')
        export = ''
        if contexts:
            context = contexts[0]
            if self.projects.get(task.project) != context.cwd:
                raise ValueError('后续任务目录与前序选定目录不符；请重新核对。')
            export = context_export(self, context)
        return (export + evidence_export(self, task, extra_item=extra_item, extra_verification=extra_verification)
                + dependency_export(self, task))

    def inherited_requirements(self, task: Task, run: Run) -> str:
        """Revalidate retained old quotes before regenerating current audit exactly once."""
        if self.runs.get(run.id) is not run or run.task_id != task.id or task.run_id != run.id:
            raise ValueError('原轮次身份已变化；不能继承任意请求。')
        base = source_base(run.requirements)
        quote = run.requirements[len(base):]
        if quote:
            contexts = [ctx for ctx in self.task_contexts.values() if ctx.child_task_id == task.id]
            if contexts:
                if len(contexts) != 1:
                    raise ValueError('历史前序来源冲突。')
                context = context_export(self, contexts[0])
                if not quote.startswith(context):
                    raise ValueError('历史前序引用尾部不符；不能隐藏伪造来源。')
                quote = quote[len(context):]
            inventory_quote, marker, dependency_quote = quote.partition(DEPENDENCY_MARKER)
            if (not historical_quote_valid(self, task, inventory_quote)
                    or not dependency_history_valid(self, task, marker + dependency_quote)):
                raise ValueError('历史库存/依赖引用尾部不符；不能隐藏伪造来源或核对。')
        return source_base(self.compiled_requirements(task, base))

    def compiled_requirements(self, task: Task, requirements: str | None = None) -> str:
        """One source-aware compiler for preview, preflight and committed Run bytes."""
        if self.tasks.get(task.id) is not task:
            raise ValueError('任务已变化；请重新核对。')
        text = task.remaining if requirements is None else requirements
        base = source_base(text)
        from failure_view import validate_recovery
        validate_recovery(self, task, base)
        export = self._source_exports(task)
        if base != text and base + export != text:
            raise ValueError('引用数据与已保存来源不符；不能改写、复制旧核验或注入来源。')
        compiled = base + export
        if len(compiled) > 6000:
            raise ValueError('合同与选定来源合计超过 6000 字；请缩短内容，不截断边界。')
        return compiled

    def add_evidence(self, task: Task, item: EvidenceItem) -> None:
        """Explicit local selection only; preserve source task/run and all old approvals."""
        self._require_active(task)
        validate_item(self, item)
        if item.owner_task_id != task.id or item.id in self.evidence_items:
            raise ValueError('库存选择身份不符或已经保存。')
        current = self.compiled_requirements(task)
        proposed = source_base(current) + self._source_exports(task, extra_item=item)
        if len(proposed) > 6000:
            raise ValueError('合同与选定库存合计超过 6000 字；未保存，不截断。')
        self.evidence_items[item.id] = item
        if source_base(task.remaining) != task.remaining:
            task.remaining = proposed

    def verify_evidence(self, task: Task, record: EvidenceVerification) -> None:
        """Append an explicit human attestation, not a permission or execution intent."""
        self._require_active(task)
        validate_verification(self, record)
        item = self.evidence_items[record.item_id]
        if item.owner_task_id != task.id or record.id in self.evidence_verifications:
            raise ValueError('人工核验只属于所选库存项；不能移用或重复保存。')
        current = self.compiled_requirements(task)
        proposed = source_base(current) + self._source_exports(task, extra_verification=record)
        if len(proposed) > 6000:
            raise ValueError('完整核验审计与合同超过 6000 字；未保存，不截断。')
        self.evidence_verifications[record.id] = record
        if source_base(task.remaining) != task.remaining:
            task.remaining = proposed

    def check_dependencies(self, task: Task, purpose: str, *, agent_id: str | None = None,
                           requirements: str | None = None) -> None:
        check_dependencies(self, task, purpose, agent_id=agent_id, requirements=requirements)

    def _append_dependency(self, task: Task, registry: str,
                           record: TaskDependency | DependencyCheck | OwnershipRelease | OwnershipReceipt) -> None:
        self._require_active(task)
        if isinstance(record, TaskDependency):
            owner_id = record.owner_task_id
        else:
            dep_id = self.ownership_releases[record.release_id].dependency_id if isinstance(record, OwnershipReceipt) else record.dependency_id
            owner_id = self.task_dependencies[dep_id].owner_task_id
        current_registry = getattr(self, registry)
        if owner_id != task.id or record.id in current_registry:
            raise ValueError('依赖核对只属于明确任务；不可重复保存或移用。')
        current = self.compiled_requirements(task)
        prospective = copy(self)
        setattr(prospective, registry, {**current_registry, record.id: record})
        check_dependencies_structure(prospective)  # DAG and audit checks precede mutation.
        proposed = source_base(current) + prospective._source_exports(task)
        if len(proposed) > 6000:
            raise ValueError('完整依赖审计与请求超过 6000 字；未保存，不截断。')
        current_registry[record.id] = record
        if source_base(task.remaining) != task.remaining:
            task.remaining = proposed

    def add_dependency(self, task: Task, record: TaskDependency) -> None:
        validate_dependency(self, record)
        self._append_dependency(task, 'task_dependencies', record)

    def check_dependency(self, task: Task, record: DependencyCheck) -> None:
        validate_check(self, record)
        self._append_dependency(task, 'dependency_checks', record)

    def release_ownership(self, task: Task, record: OwnershipRelease) -> None:
        validate_release(self, record)
        self._append_dependency(task, 'ownership_releases', record)

    def receive_ownership(self, task: Task, record: OwnershipReceipt) -> None:
        validate_receipt(self, record)
        self._append_dependency(task, 'ownership_receipts', record)

    def continue_from(self, parent_id: str, incoming: Run) -> bool:
        """Adopt a proven Pi-side input, never execute it or revive its parent."""
        incoming = Run.from_record({item.name: getattr(incoming, item.name) for item in fields(Run)})
        parent = self.runs.get(parent_id)
        if parent is None:
            raise ValueError("不能接管未知父轮次的执行。")
        incoming.check_continuation(parent)
        if incoming.id in self.runs:
            return self.ingest(incoming)
        task = self.tasks[parent.task_id]
        self._require_active(task)
        if (task.status == "done" or task.run == "none" or task.run_id != parent.id or task.agent != parent.agent_id
                or task.title != parent.title or task.description != parent.description
                or self.projects.get(task.project) != parent.cwd):
            raise ValueError("任务已验收、退回或重新安排；不接管后续 Pi 活动。")
        if any(run.continuation_parent == parent.id or (not run.terminal and
                   (run.agent_id == parent.agent_id or run.task_id == parent.task_id)) for run in self.runs.values()):
            raise ValueError("父轮次已有续接或仍有未结束的执行；不接管分叉。")
        self.runs[incoming.id] = replace(incoming, state="queued", seq=0, delivery="", summary="", checks=[], remaining="", events=[])
        task.run_id, task.run, task.full_delivery = incoming.id, "queued", False
        return self.ingest(incoming)

    def ingest(self, incoming: Run) -> bool:
        previous = self.runs.get(incoming.id)
        if previous is None or previous.identity != incoming.identity:
            raise ValueError("执行结果身份与派发快照不匹配。")
        if incoming.delivery == "full" and incoming.requirements.startswith(EVIDENCE_ONLY_PREFIX):
            raise ValueError("仅补证轮不能冒充整个任务的完整交付。")
        if previous.terminal or incoming.seq <= previous.seq:
            return False
        events = validate_events(incoming.events, run_id=incoming.id, seq=incoming.seq)
        check_history(previous.events, incoming.events)
        self.runs[incoming.id] = incoming
        task = self.tasks[incoming.task_id]
        if task.run_id != incoming.id or task.status == "done":
            return True  # A historical attempt cannot overwrite the current task.
        task.summary, task.checks = incoming.summary, list(incoming.checks)
        task.remaining = incoming.remaining or incoming.requirements
        task.full_delivery = False
        if not incoming.terminal:
            task.run = "queued" if incoming.state == "queued" else "working"
            task.status = "blocked" if incoming.state == "waiting" else "ready" if incoming.state == "queued" else "running"
            task.blocker = "等待 Pi 窗口中的人工确认；工作台不会自动回答。" if incoming.state == "waiting" else ""
        elif incoming.state == "finished" and incoming.delivery in ("full", "partial"):
            task.run, task.status = "finished", "review"
            task.full_delivery = incoming.delivery == "full" and self.supplements_satisfied(incoming)
            task.remaining, task.blocker = incoming.remaining, ""
            if not any(event.kind == "settled" for event in events):
                self.note(task, "result_received", f"工作台收到本轮交付且空闲的结果 · {incoming.delivery} · 等待人工验收", run_id=incoming.id, source="system")
        else:
            task.run, task.status = "finished", "blocked"
            task.blocker = incoming.summary or f"执行 {incoming.state}；没有可验收的完整交付。"
            if not any(event.kind in {"settled", "failed", "stopped", "interrupted"} for event in events):
                self.note(task, "result_received", f"工作台收到本轮结束结果 · {incoming.state} · {task.blocker}", run_id=incoming.id, source="system")
        return True

    def abandon(self, task: Task, reason: str) -> None:
        self._require_active(task)
        run = self.runs.get(task.run_id)
        if not run or run.terminal or not reason.strip():
            raise ValueError("仅未确认结束的执行可人工解除，且必须填写已核实情况。")
        self.note(task, "abandoned", f"人工确认旧 Pi 已停止 · {reason.strip()}", run_id=run.id)
        run.state = "abandoned"
        task.run, task.status, task.full_delivery = "finished", "blocked", False
        task.blocker = "人工解除失联执行；可补充要求后另起一轮。"


def sample_board() -> Board:
    board = Board()
    samples = [
        ("把任务详情改成可读、可滚动的交付视图", "终端工作台", "保留摘要、证据、剩余工作和时间线。\n长内容不能挤掉关键操作。"),
        ("核对中文样本的时间边界与缺失字段处理，不读取任何真实数据", "数据实验室", "这是虚构任务，不是数据采集指令。\n检查跨日样例、空值和重复记录；结果仅作交互演示。"),
        ("等待确认：样例字段应该采用哪个口径？", "数据实验室", "执行者缺少定义，需要人类补充后才能继续。"),
        ("让窄终端也能舒适地查看任务", "终端工作台", "80×24 使用列表/详情切换，不压缩成九条窄泳道。"),
        ("整理一份周末阅读清单", "个人事务", "先记下来，不需要立刻交给 Agent。"),
        ("写下下一轮想试的交互", "收集箱", "只写标题也能创建草稿。"),
        ("补齐路径显示与长中文标题的回归说明", "终端工作台", "确认目录可完整读取，不能静默回退到其他路径。"),
        ("为样例统计补充异常输入说明", "数据实验室", "没有真实文件，也不会运行命令。"),
        ("清理演示任务命名", "个人事务", "人类验收与执行者回合结束是两件事。"),
        ("设计收藏和稍后处理的键盘手感", "收集箱", "讨论项，尚未安排。"),
        ("给排队反馈增加明确的确认状态", "终端工作台", "提交不等于开始，必须等待模拟执行确认。"),
        ("完成任务工作台原型的第一阶段", "终端工作台", "此卡故意只交付阶段成果，用于演示不能直接整任务验收。"),
    ]
    for title, project, description in samples:
        board.create(title, project, description)
    for task_id in (1, 2, 3, 4, 7, 8, 9, 11, 12):
        task = board.tasks[task_id]
        board.assign(task, AGENTS[0 if task.project == "数据实验室" else 1])
        if task_id == 7:
            continue
        board.queue(task)
        if task_id == 11:
            continue
        board.simulate(task, "ack")
        if task_id == 3:
            board.simulate(task, "block", "需要你确认样例口径：按事件时间还是按入库时间？")
        elif task_id in (1, 9, 12):
            board.simulate(task, "partial" if task_id == 12 else "full")
            if task_id == 9:
                board.accept(task)
    return board
