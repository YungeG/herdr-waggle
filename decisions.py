"""Bounded proposals and immutable human decisions. No model-facing approval API."""
import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass, fields, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

if TYPE_CHECKING:
    from journal import Event
    from model import Board, Run, Supplement

MAX_RECORDS = 128  # Per task; authorization evidence is never silently pruned.
EVIDENCE_ONLY_PREFIX = "仅补证请求："  # Scope marker for a run that cannot finish the whole task.
REVIEW_SCOPE_PREFIX = "本轮核查合同："
_PROPOSAL_LABEL = "交接目标：交付可审提案，不批准或执行新工作。\n"
_QUESTION_LABEL = "追问目标：只解释已有材料与来源，不批准或执行新工作。\n"
_TASK_QUESTION_LABEL = "\n用户本次问题（非授权）："


def obj(value: object, cls) -> dict:
    if type(value) is not dict or set(value) != {f.name for f in fields(cls)}:
        raise ValueError("决策字段缺失或不兼容。")
    return value


def text(value: object, limit: int, required: bool = True) -> str:
    if type(value) is not str or len(value) > limit or (required and not value.strip()):
        raise ValueError("决策文字类型、长度或内容无效。")
    value.encode("utf-8")
    return value


def texts(value: object, count: int, limit: int) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or len(value) > count:
        raise ValueError("决策列表超过上限或类型错误。")
    return tuple(text(item, limit) for item in value)


def identity(value: object) -> str:
    value = text(value, 36)
    if str(UUID(value)) != value:
        raise ValueError("决策身份无效。")
    return value


def key(value: object) -> str:
    value = text(value, 48)
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,47}", value):
        raise ValueError("决策/选项 key 必须是稳定的短标识。")
    return value


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Budget:
    attempts: int
    minutes: int
    storage_mib: int
    stop_conditions: tuple[str, ...]

    @classmethod
    def from_record(cls, value: object) -> "Budget":
        row = obj(value, cls)
        if any(type(row[k]) is not int or not 0 <= row[k] <= 2**31 - 1 for k in ("attempts", "minutes", "storage_mib")):
            raise ValueError("预算必须是有单位的非负整数，不接受布尔值、小数或无界数值。")
        stops = texts(row["stop_conditions"], 6, 200)
        if not stops:
            raise ValueError("预算必须填写停止条件。")
        return cls(row["attempts"], row["minutes"], row["storage_mib"], stops)


@dataclass(frozen=True)
class DecisionOption:
    key: str
    label: str
    benefit: str
    risk: str
    cost: str
    scope: str

    @classmethod
    def from_record(cls, value: object) -> "DecisionOption":
        row = obj(value, cls)
        return cls(key(row["key"]), *(text(row[k], 300) for k in ("label", "benefit", "risk", "cost", "scope")))


@dataclass(frozen=True)
class BusinessChoice:
    key: str
    label: str

    @classmethod
    def from_record(cls, value: object) -> "BusinessChoice":
        row = obj(value, cls)
        return cls(key(row["key"]), text(row["label"], 120))


@dataclass(frozen=True)
class BusinessInput:
    kind: str  # choice or text; a user question, never a technical solution or grant.
    question: str
    options: tuple[BusinessChoice, ...]

    @classmethod
    def from_record(cls, value: object) -> "BusinessInput":
        row = obj(value, cls)
        if row["kind"] not in {"choice", "text"}:
            raise ValueError("业务输入类型无效。")
        options = row["options"]
        if not isinstance(options, (list, tuple)) or len(options) > 6:
            raise ValueError("业务答案选项无效。")
        choices = tuple(BusinessChoice.from_record(option) for option in options)
        if len({option.key for option in choices}) != len(choices) or (row["kind"] == "choice" and len(choices) < 2) or (row["kind"] == "text" and choices):
            raise ValueError("业务答案选项缺失或重复。")
        return cls(row["kind"], text(row["question"], 200), choices)


@dataclass(frozen=True)
class BusinessAnswer:
    value: str
    detail: str = ""
    question: BusinessInput | None = None

    @classmethod
    def from_record(cls, value: object) -> "BusinessAnswer":
        row = obj(value, cls)
        return cls(text(row["value"], 500), text(row["detail"], 500, False).strip(),
                   BusinessInput.from_record(row["question"]) if row["question"] is not None else None)

    def check(self, question: BusinessInput) -> None:
        BusinessAnswer.from_record(asdict(self))
        if question.kind == "choice" and self.value not in {option.key for option in question.options}:
            raise ValueError("业务答案与当前问题不匹配。")
        if self.value == "conditional" and not self.detail:
            raise ValueError("选择有条件时，请说明条件；不会推断业务要求。")


_D002_REPORT_ID = "7db8453b-95ae-401d-ba8d-8cb8c6088b8a"
_D002_INPUTS = {
    "koru_r1f_lifecycle": BusinessInput("choice", "旧订单未成交时出现反向信号，是否必须提前反转？", (
        BusinessChoice("yes", "需要"), BusinessChoice("no", "不需要"),
        BusinessChoice("conditional", "视条件而定"), BusinessChoice("unknown", "暂不确定"))),
    "koru_candidate_scope": BusinessInput("text", "将来允许只读哪些确切目录或文件？不知道可留空；这不是访问许可。", ()),
}


def business_input(spec: "DecisionSpec | None", task_id: int, event_id: str, spec_key: str) -> BusinessInput | None:
    if spec is not None and spec.human_input is not None:
        return spec.human_input
    if task_id == 2 and event_id == _D002_REPORT_ID and (spec is None or spec.key == spec_key):
        return _D002_INPUTS.get(spec_key)
    return None


@dataclass(frozen=True)
class EvidenceNeed:
    gap: str
    owner: str
    source: str

    @classmethod
    def from_record(cls, value: object) -> "EvidenceNeed":
        row = obj(value, cls)
        return cls(text(row["gap"], 200), text(row["owner"], 120), text(row["source"], 300))


@dataclass(frozen=True)
class RecommendationResult:
    status: str  # supported or insufficient; a suggestion never grants execution authority.
    value: str
    unit: str
    conditions: str
    evidence: tuple[str, ...]
    uncertainty: str
    owner_signed: bool

    @classmethod
    def from_record(cls, value: object) -> "RecommendationResult":
        row = obj(value, cls)
        if row["status"] not in {"supported", "insufficient"} or type(row["owner_signed"]) is not bool:
            raise ValueError("建议状态或负责人签收无效。")
        advice = cls(row["status"], text(row["value"], 200, False), text(row["unit"], 40, False),
                     text(row["conditions"], 300, False), texts(row["evidence"], 6, 300),
                     text(row["uncertainty"], 300), row["owner_signed"])
        if advice.status == "supported" and (not advice.value.strip() or not advice.unit.strip() or
                                              not advice.conditions.strip() or not advice.evidence):
            raise ValueError("建议必须有数值、单位、适用条件与依据。")
        if advice.status == "insufficient" and (advice.value or advice.unit or advice.conditions or advice.owner_signed):
            raise ValueError("证据不足时不能猜设置值、单位、条件或声称签收。")
        return advice


@dataclass(frozen=True)
class DecisionRouting:
    audience: str  # user_fact, user_decision, owner_evidence
    evidence_needs: tuple[EvidenceNeed, ...]
    advice: RecommendationResult

    @classmethod
    def from_record(cls, value: object) -> "DecisionRouting":
        row = obj(value, cls)
        if row["audience"] not in {"user_fact", "user_decision", "owner_evidence"}:
            raise ValueError("决策受众无效。")
        needs = row["evidence_needs"]
        if not isinstance(needs, (list, tuple)) or len(needs) > 6:
            raise ValueError("证据缺口及负责人超过上限。")
        return cls(row["audience"], tuple(EvidenceNeed.from_record(v) for v in needs),
                   RecommendationResult.from_record(row["advice"]))


@dataclass(frozen=True)
class DecisionSpec:
    key: str
    kind: str
    question: str
    why: str
    owner: str
    evidence: tuple[str, ...]
    missing: tuple[str, ...]
    scope: str
    risk: str
    cost: str
    options: tuple[DecisionOption, ...]
    recommendation: str
    reason: str
    exclusions: tuple[str, ...]
    budget: Budget | None
    human_input: BusinessInput | None = None
    routing: DecisionRouting | None = None

    @classmethod
    def from_record(cls, value: object) -> "DecisionSpec":
        names = {field.name for field in fields(cls)}
        if type(value) is not dict or not names - {"human_input", "routing"} <= set(value) <= names:
            raise ValueError("决策字段缺失或不兼容。")
        row = value
        if row["kind"] not in ("choice", "approval", "budget", "preference"):
            raise ValueError("未知决策输入类型。")
        options = row["options"]
        if type(options) not in (list, tuple) or len(options) > 6:
            raise ValueError("最多提供六个可比较选项。")
        options = tuple(DecisionOption.from_record(o) for o in options)
        if len({o.key for o in options}) != len(options) or (len(options) < 2 if row["kind"] in {"choice", "preference"} else bool(options)):
            raise ValueError("选项身份重复、缺失或与类型不兼容。")
        budget = Budget.from_record(row["budget"]) if row["budget"] is not None else None
        if budget is not None and row["kind"] != "budget":
            raise ValueError("只有预算项可以带预算提案。")
        spec = cls(key(row["key"]), row["kind"], text(row["question"], 200), text(row["why"], 400), text(row["owner"], 120),
                   texts(row["evidence"], 6, 300), texts(row["missing"], 6, 200), text(row["scope"], 500),
                   text(row["risk"], 300), text(row["cost"], 200), options, text(row["recommendation"], 200),
                   text(row["reason"], 400), texts(row["exclusions"], 6, 200), budget,
                   BusinessInput.from_record(row["human_input"]) if "human_input" in row else None,
                   DecisionRouting.from_record(row["routing"]) if "routing" in row else None)
        route = spec.routing
        if route:
            if tuple(need.gap for need in route.evidence_needs) != spec.missing:
                raise ValueError("证据缺口必须逐项标明负责人、来源且与报告缺口一致。")
            if route.audience == "user_fact" and spec.human_input is None:
                raise ValueError("询问用户业务事实必须给出完整可答 human_input。")
            if route.audience == "owner_evidence" and spec.human_input is not None:
                raise ValueError("仅负责人补证的事项不能伪装成用户问题。")
            if route.audience == "user_decision" and (not spec.complete or route.advice.status != "supported"):
                raise ValueError("证据不足或暂无建议时不能催用户批准技术方案。")
            if route.advice.status == "supported" and (not spec.complete or
                    any(evidence not in spec.evidence for evidence in route.advice.evidence)):
                raise ValueError("有依据的建议必须引用已核依据，缺证时不能推荐设置值。")
            if route.advice.status == "insufficient" and not spec.missing:
                raise ValueError("暂无建议必须写明缺口与证据提供者。")
        return spec

    @property
    def complete(self) -> bool:
        return bool(self.evidence) and not self.missing and (self.kind != "budget" or self.budget is not None)


def parse_specs(value: object) -> tuple[DecisionSpec, ...]:
    if not isinstance(value, (list, tuple)) or not 1 <= len(value) <= 6:
        raise ValueError("决策单必须包含 1–6 项。无待决策项时请省略 decisions。")
    specs = tuple(DecisionSpec.from_record(v) for v in value)
    if len({s.key for s in specs}) != len(specs):
        raise ValueError("同一决策单的 key 不能重复。")
    if any(s.routing is not None for s in specs) and not all(s.routing is not None for s in specs):
        raise ValueError("新版报告每项都必须写明受众、证据提供者与建议结果。")
    return specs


@dataclass(frozen=True)
class Answer:
    kind: str = "undecided"
    choice: str = ""
    budget: Budget | None = None
    comment: str = ""
    business: BusinessAnswer | None = None

    @classmethod
    def from_record(cls, value: object) -> "Answer":
        names = {field.name for field in fields(cls)}
        if type(value) is not dict or set(value) not in (names, names - {"business"}):
            raise ValueError("答复字段缺失或不兼容。")
        row = value
        if row["kind"] not in {"undecided", "request_info", "defer", "reject", "approve", "choice", "budget", "preference"}:
            raise ValueError("未知人工答复。")
        choice = text(row["choice"], 48, False)
        budget = Budget.from_record(row["budget"]) if row["budget"] is not None else None
        if (bool(choice) != (row["kind"] in {"choice", "preference"})) or ((budget is not None) != (row["kind"] == "budget")):
            raise ValueError("答复类型与值不兼容。")
        return cls(row["kind"], choice, budget, text(row["comment"], 500, False).strip(),
                   BusinessAnswer.from_record(row["business"]) if row.get("business") is not None else None)

    def check(self, spec: DecisionSpec, human_input: BusinessInput | None = None) -> None:
        Answer.from_record(asdict(self))
        if self.business is not None:
            question = human_input or spec.human_input or self.business.question
            if self.business.question is not None and human_input is not None and self.business.question != human_input:
                raise ValueError("业务问题已变化；不能沿用旧答案。")
            if question is None:
                raise ValueError("本项没有业务问题；不能附加业务答案。")
            self.business.check(question)
        if self.kind in {"undecided", "request_info", "defer", "reject"}:
            return
        if self.kind == "preference":
            if spec.kind not in {"choice", "preference"} or self.choice not in {o.key for o in spec.options}:
                raise ValueError("倾向所指方案已不存在；不能形成授权。")
            return  # An incomplete proposal may accept a non-authorizing inclination.
        if spec.routing and spec.routing.audience != "user_decision":
            raise ValueError("本项由负责人补证或只问业务事实，不能批准技术方案。")
        if not spec.complete:
            raise ValueError("材料不足：只能要求补齐、暂缓或拒绝，不能批准。")
        if self.kind != ("approve" if spec.kind == "approval" else spec.kind):
            raise ValueError("答复与决策类型不匹配。")
        if self.kind in {"choice", "preference"} and self.choice not in {o.key for o in spec.options}:
            raise ValueError("所选选项已不存在。")


@dataclass(frozen=True)
class Source:
    run_id: str
    event_id: str
    seq: int


@dataclass(frozen=True)
class Context:
    task_id: int
    title: str
    description: str
    project: str
    cwd: str
    execution: str
    run_id: str
    agent_id: str
    requirements: str
    remaining: str

    @property
    def scope(self) -> tuple:
        # Identical proposal wording is not enough when the work it would authorize changed.
        return (self.task_id, self.title, self.description, self.project, self.cwd,
                self.execution, self.requirements, self.remaining)


@dataclass(frozen=True)
class Target:
    kind: str
    agent_id: str = ""
    session_id: str = ""
    cwd: str = ""


@dataclass(frozen=True)
class Intent:
    kind: str
    run_id: str = ""
    supplement_id: str = ""


@dataclass(frozen=True)
class DecisionPreview:
    task_id: int
    source: Source
    context: Context
    specs: tuple[DecisionSpec, ...]
    answers: tuple[Answer, ...]
    prior: tuple[str, ...]
    fingerprint: str
    compiled_text: str
    target: Target


@dataclass(frozen=True)
class DecisionRecord:
    version: int
    id: str
    confirmed_at: str
    preview: DecisionPreview
    intent: Intent

    @property
    def task_id(self) -> int:
        return self.preview.task_id

    @property
    def specs(self) -> tuple[DecisionSpec, ...]:
        return self.preview.specs

    @classmethod
    def from_record(cls, value: object) -> "DecisionRecord":
        row = obj(value, cls)
        if type(row["version"]) is not int or row["version"] not in (1, 2, 3):
            raise ValueError("决策记录版本不兼容。")
        p = obj(row["preview"], DecisionPreview)
        source = Source(**obj(p["source"], Source))
        identity(source.run_id)
        identity(source.event_id)
        if type(source.seq) is not int or not 1 <= source.seq <= 2**53 - 1:
            raise ValueError("决策报告修订无效。")
        c = obj(p["context"], Context)
        if type(c["task_id"]) is not int or c["task_id"] <= 0 or c["execution"] != "pi":
            raise ValueError("决策任务身份无效。")
        for k, limit in {"title": 300, "description": 20000, "project": 300, "cwd": 4096, "requirements": 6000, "remaining": 6000}.items():
            text(c[k], limit, k not in {"description", "requirements", "remaining"})
        identity(c["run_id"])
        identity(c["agent_id"])
        context = Context(**c)
        if not context.cwd.startswith("/") or context.run_id != source.run_id or type(p["task_id"]) is not int or p["task_id"] != context.task_id:
            raise ValueError("决策范围与来源不匹配。")
        if type(p["specs"]) not in (list, tuple) or type(p["answers"]) not in (list, tuple) or any(
                type(s) is not dict for s in p["specs"]) or any(type(a) is not dict for a in p["answers"]):
            raise ValueError("决策规格或答复格式无效。")
        if row["version"] == 1 and (any("human_input" in s or "routing" in s for s in p["specs"]) or
                                   any("business" in a for a in p["answers"])):
            raise ValueError("旧版记录不可夹带新业务答案。")
        if row["version"] == 2 and any("business" not in a for a in p["answers"]):
            raise ValueError("新版记录业务答案字段缺失。")
        if row["version"] == 2 and any("routing" in s for s in p["specs"]):
            raise ValueError("旧版决定不能夹带新版角色合同。")
        if row["version"] == 3 and (any("routing" not in s for s in p["specs"]) or
                                   any("business" not in a for a in p["answers"])):
            raise ValueError("新版角色合同或答复字段缺失。")
        specs = parse_specs(p["specs"])
        if type(p["answers"]) not in (list, tuple) or len(p["answers"]) != len(specs):
            raise ValueError("每个显示项必须有且只有一个答复。")
        answers = tuple(Answer.from_record(a) for a in p["answers"])
        for spec, answer in zip(specs, answers, strict=True):
            if row["version"] in (2, 3) and answer.business is not None and answer.business.question is None:
                raise ValueError("业务答案缺少原问题；拒绝无来源的旧记录。")
            answer.check(spec, business_input(spec, p["task_id"], source.event_id, spec.key))
        if type(p["prior"]) not in (list, tuple) or len(p["prior"]) > 6:
            raise ValueError("此前确认引用必须是有界列表。")
        prior = tuple(text(v, 36, False) for v in p["prior"])
        if len(prior) != len(specs):
            raise ValueError("此前确认的引用不匹配。")
        for reference in prior:
            if reference:
                identity(reference)
        target = Target(**obj(p["target"], Target))
        check_target(target)
        fingerprint = text(p["fingerprint"], 64)
        if not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
            raise ValueError("决策指纹无效。")
        preview = DecisionPreview(p["task_id"], source, context, specs, answers, prior, fingerprint,
                                  text(p["compiled_text"], 4000 if target.kind == "live_supplement" else 6000), target)
        intent = Intent(**obj(row["intent"], Intent))
        text(intent.run_id, 36, False)
        text(intent.supplement_id, 36, False)
        if intent.kind != target.kind:
            raise ValueError("决策动作与持久化意图不一致。")
        if intent.kind in {"draft", "saved"}:
            if intent.run_id or intent.supplement_id:
                raise ValueError("仅保存意图不可带执行目标。")
        else:
            identity(intent.run_id)
            if intent.kind == "live_supplement":
                identity(intent.supplement_id)
            elif intent.supplement_id:
                raise ValueError("新派发不能冒充本轮补充。")
        at = text(row["confirmed_at"], 24, False)
        if intent.kind == "draft":
            if at:
                raise ValueError("草稿不能带人工确认时间。")
        elif not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", at) or datetime.fromisoformat(at).isoformat(timespec="milliseconds").replace("+00:00", "Z") != at:
            raise ValueError("人工确认时间无效。")
        return cls(row["version"], identity(row["id"]), at, preview, intent)


def spec_wire(spec: DecisionSpec) -> dict:
    row = asdict(spec)
    if row["human_input"] is None:
        del row["human_input"]
    if row["routing"] is None:
        del row["routing"]
    return row


def decision_record_wire(record: DecisionRecord) -> dict:
    row = asdict(record)
    for spec in row["preview"]["specs"]:
        if spec["human_input"] is None:
            del spec["human_input"]
        if spec["routing"] is None:
            del spec["routing"]
    if record.version == 1:
        for answer in row["preview"]["answers"]:
            del answer["business"]
    return row


@dataclass(frozen=True)
class PreparedDecision:
    record: DecisionRecord
    run: "Run | None" = None
    note: "Supplement | None" = None


def check_target(target: Target) -> None:
    text(target.agent_id, 36, False)
    text(target.session_id, 36, False)
    text(target.cwd, 4096, False)
    if target.kind not in {"draft", "saved", "new_run", "live_supplement"}:
        raise ValueError("未知决策提交动作。")
    if target.kind in {"draft", "saved"}:
        if target.agent_id or target.session_id or target.cwd:
            raise ValueError("仅保存不能隐含派发目标。")
    else:
        identity(target.agent_id)
        identity(target.session_id)
        if not text(target.cwd, 4096).startswith("/"):
            raise ValueError("执行目录必须明确。")


def decision_source(board: "Board", task_id: int) -> "Event":
    from journal import current_report, validate_events
    task = board.tasks.get(task_id)
    if not task:
        raise ValueError("任务不存在。")
    run = board.runs.get(task.run_id)
    if run:
        validate_events(run.events, run_id=run.id, seq=run.seq)
    event = current_report(task, run)
    if not event or not event.report or not event.report.decisions or event.source != "executor":
        raise ValueError("当前报告没有有效结构化决策材料；请要求执行者补齐，不从旧文本推断批准。")
    if run and (proposal_question(run.requirements) is not None or question_question(run.requirements) is not None):
        specs = event.report.decisions
        replied = {spec.key for spec in specs if spec.routing is not None}
        full_proposal = proposal_question(run.requirements) is not None
        if (full_proposal and len(replied) != len(specs) or
                any(key not in replied for key in proposal_selected_keys(run.requirements))):
            purpose = "提案" if full_proposal else "追问"
            raise ValueError(f"{purpose}尚未逐条回应已勾选旧问题并标明责任与来源；不能进入正式决定。")
    return event


def context_for(board: "Board", task_id: int) -> Context:
    task = board.tasks[task_id]
    run = board.runs[task.run_id]
    return Context(task.id, task.title, task.description, task.project, board.projects[task.project], task.execution,
                   task.run_id, task.agent, run.requirements, task.remaining)


def unchanged_evidence_keys(board: "Board", task_id: int) -> tuple[str, ...]:
    """Only a NEW report after a sent request can establish no-new-evidence waiting."""
    event = decision_source(board, task_id)
    assert event.report and event.report.decisions
    task = board.tasks[task_id]
    project_cwd = board.projects[task.project]
    rows = {spec.key: spec for spec in event.report.decisions if spec.missing}
    waiting: set[str] = set()
    for record in reversed(list(board.decisions.values())):
        if (record.task_id != task_id or not record.confirmed_at or
                record.intent.kind not in {"new_run", "live_supplement"} or
                not record.preview.compiled_text.startswith(EVIDENCE_ONLY_PREFIX) or
                record.preview.source.event_id == event.id):
            continue
        old_scope = record.preview.context
        if (old_scope.title, old_scope.description, old_scope.project, old_scope.cwd) != (
                task.title, task.description, task.project, project_cwd):
            continue
        for old, answer in zip(record.specs, record.preview.answers, strict=True):
            current = rows.get(old.key)
            if (current and answer.kind == "request_info" and old.key not in waiting and
                    (old.kind, old.scope, old.owner, old.missing, old.evidence,
                     old.routing.evidence_needs if old.routing else None) ==
                    (current.kind, current.scope, current.owner, current.missing, current.evidence,
                     current.routing.evidence_needs if current.routing else None)):
                waiting.add(old.key)
    return tuple(spec.key for spec in event.report.decisions if spec.key in waiting)


def historical_business_fact(board: "Board", task_id: int, spec_key: str) -> tuple[DecisionRecord, BusinessAnswer] | None:
    """Confirmed facts may be shown as history, never projected as a fresh answer."""
    event = decision_source(board, task_id)
    assert event.report and event.report.decisions
    current = next((s for s in event.report.decisions if s.key == spec_key), None)
    question = business_input(current, task_id, event.id, spec_key) if current else None
    if question is None:
        return None
    scope = context_for(board, task_id).scope
    for record in reversed(list(board.decisions.values())):
        if (record.task_id != task_id or not record.confirmed_at or record.preview.source.event_id == event.id or
                record.preview.context.scope != scope):
            continue
        for old, answer in zip(record.specs, record.preview.answers, strict=True):
            if old.key == spec_key and answer.business and answer.business.question == question:
                return record, answer.business
    return None


def prior_answers(board: "Board", task_id: int) -> dict[str, tuple[DecisionRecord, Answer]]:
    event = decision_source(board, task_id)
    assert event.report and event.report.decisions
    specs = {s.key: s for s in event.report.decisions}
    scope = context_for(board, task_id).scope
    result: dict[str, tuple[DecisionRecord, Answer]] = {}
    seen: set[str] = set()
    for record in reversed(list(board.decisions.values())):
        if record.task_id != task_id or not record.confirmed_at:
            continue
        for spec, answer in zip(record.specs, record.preview.answers, strict=True):
            if spec.key in seen or (answer.kind == "undecided" and answer.business is None):
                continue
            seen.add(spec.key)  # A later changed/rejected decision must not resurrect an older grant.
            if record.preview.context.scope == scope and spec == specs.get(spec.key):
                if answer.business and business_input(spec, task_id, event.id, spec.key) != business_input(
                        spec, task_id, record.preview.source.event_id, spec.key):
                    continue  # A new report cannot reuse an answer to a retired source-bound question.
                result[spec.key] = (record, answer)
    return result


def answer_text(spec: DecisionSpec, answer: Answer, human_input: BusinessInput | None = None) -> str:
    labels = {"undecided": "未决定，不授权", "request_info": "请补齐材料", "defer": "暂缓，不授权", "reject": "拒绝", "approve": "批准所述范围"}
    if answer.kind in {"choice", "preference"}:
        option = next(o for o in spec.options if o.key == answer.choice)
        result = ("偏好，不是授权" if answer.kind == "preference" else "选择") + f"：{option.key} · {option.label}\n选项范围：{option.scope}"
    elif answer.budget:
        b = answer.budget
        result = f"预算上限：{b.attempts} 次 / {b.minutes} 分钟 / {b.storage_mib} MiB\n停止条件：" + "；".join(b.stop_conditions)
    else:
        result = labels[answer.kind]
    if answer.kind == "request_info":
        fallback = "请核对旧总帽及累计已用量与原始账本" if spec.kind == "budget" else "请补齐可比较方案、证据、风险与范围"
        result += ("：" + "；".join(spec.missing or (fallback,)) + "\n"
                   "请用不带术语的话说明：用户需要回答什么（给出日常例子、可选回答和不确定时的做法）；"
                   "执行者需要补什么依据、由谁提供。不要替用户决定、批准或编造业务要求。")
    if answer.kind == "preference" and not spec.complete:
        result += "\n尚缺材料（倾向不替代证据）：" + "；".join(spec.missing or ("请补齐可比较的依据",))
    result += ("\n备注（不扩展授权）：" + answer.comment if answer.comment else "")
    if answer.business is not None:
        question = human_input or spec.human_input or answer.business.question
        if question is None:
            raise ValueError("业务问题来源无效；不能升级为决定。")
        label = next((o.label for o in question.options if o.key == answer.business.value), answer.business.value)
        result += "\n业务答案（仅描述需求，不构成技术方案或执行授权）：" + question.question + " → " + label
        if answer.business.detail:
            result += "；条件或限制：" + answer.business.detail
        if question.kind == "text":
            result += "\n所填路径仅是未来核对候选，不授权本轮读取所填路径或扩大当前访问范围。"
        if spec.missing:
            result += "\n仍需负责人核对：" + "；".join(spec.missing)
    return result


def review_requirements(goal: str, scope: str, verification: str) -> str:
    """One new real round has a deliverable, bounded scope and checkable result."""
    for name, value in (("交付物", goal), ("允许的范围", scope), ("验收依据", verification)):
        if type(value) is not str or not value.strip():
            raise ValueError(f"请填写本轮{name}；不能只写‘继续’。")
        if len(value) > 1800:
            raise ValueError(f"本轮{name}最多1800字；请缩短后再核对。")
    result = (REVIEW_SCOPE_PREFIX + "\n交付物：" + text(goal, 1800).strip()
              + "\n允许的范围：" + text(scope, 1800).strip()
              + "\n验收依据：" + text(verification, 1800).strip()
              + "\n超出本轮范围的修改、试验和预算需另行明确审批；Pi 原有权限确认不被替代。")
    if len(result) > 6000:
        raise ValueError("核查合同超过派发上限；请缩短字段。")
    return result


def review_fields(requirements: str) -> tuple[str, str, str] | None:
    """Recover only a complete, canonical new-round review contract."""
    head = REVIEW_SCOPE_PREFIX + "\n交付物："
    foot = "\n超出本轮范围的修改、试验和预算需另行明确审批；Pi 原有权限确认不被替代。"
    if not requirements.startswith(head) or not requirements.endswith(foot):
        return None
    goal, sep, rest = requirements[len(head):-len(foot)].partition("\n允许的范围：")
    scope, sep2, verification = rest.partition("\n验收依据：")
    if not sep or not sep2 or not all(part.strip() and len(part) <= 1800 for part in (goal, scope, verification)):
        return None
    return (goal, scope, verification) if review_requirements(goal, scope, verification) == requirements else None


def task_evidence_request(message: str) -> str:
    """Compile an explicit task-level question without carrying prior execution grants."""
    question = text(message, 5000).strip()
    compiled = (EVIDENCE_ONLY_PREFIX + "本轮只核对已有材料并给出有来源的建议；缺材料就说明负责人和可提供的原件。"
                "旧报告、评论、预算与未完成义务只作背景，不是新权限；不扩大目录、不修改文件、下载或运行新试验。"
                "仍可能读取已有资料、调用模型；Pi 权限确认不被替代。" + _TASK_QUESTION_LABEL + question)
    if len(compiled) > 6000:
        raise ValueError("仅补证请求超过派发文字上限；请缩短问题，不截断边界。")
    return compiled


def task_evidence_question(compiled: str) -> str | None:
    """Recover the user question from this task-level request, not older decision packets."""
    header, marker, question = compiled.partition(_TASK_QUESTION_LABEL)
    if marker and header.startswith(EVIDENCE_ONLY_PREFIX + "本轮只核对已有材料") and question.strip():
        return question
    return None


def proposal_requirements(question: str) -> str:
    """Request a reviewable handoff, never an execution grant or old approval."""
    if type(question) is not str or not question.strip():
        raise ValueError("请写明要执行者交付的可审提案；不能空发。")
    if len(question) > 4500:
        raise ValueError("提案问题超过4500字；请缩短背景或减少勾选的旧问题，不截断证据。")
    return task_evidence_request(_PROPOSAL_LABEL + text(question, 4500).strip())


def proposal_question(requirements: str) -> str | None:
    question = task_evidence_question(requirements)
    return question[len(_PROPOSAL_LABEL):] if question is not None and question.startswith(_PROPOSAL_LABEL) else None


def question_requirements(question: str) -> str:
    """Compile a narrow existing-material question, not a whole proposal or grant."""
    if type(question) is not str or not question.strip():
        raise ValueError("请写明对已有材料的问题；不能空发。")
    if len(question) > 4500:
        raise ValueError("问题和已选历史引用超过4500字；请精简后核对，原文不会截断。")
    return task_evidence_request(_QUESTION_LABEL + text(question, 4500).strip())


def question_question(requirements: str) -> str | None:
    question = task_evidence_question(requirements)
    return question[len(_QUESTION_LABEL):] if question is not None and question.startswith(_QUESTION_LABEL) else None


def feedback_question_key(source: str) -> str:
    """Stable decision key for one explicitly selected historical question."""
    record_id, separator, index = source.partition(":")
    if not separator or index not in {"1", "2", "3", "4", "5", "6"}:
        raise ValueError("旧反馈来源或项序无效。")
    return "q_" + identity(record_id).replace("-", "") + "_" + index


def recover_feedback_draft(requirements: str, candidates: tuple[tuple[str, str], ...]) -> tuple[str, str, tuple[str, ...]] | None:
    """Recover only canonical saved user text and exact current-source references."""
    if type(requirements) is not str or len(requirements) > 6000:
        return None
    payload = question_question(requirements)
    purpose, compiler = "question", question_requirements
    if payload is None:
        payload = proposal_question(requirements)
        purpose, compiler = "proposal", proposal_requirements
    if payload is None:
        return None
    separator = "\n\n本次明确选择的历史问题（未选择的不发送）：\n"
    parts = payload.split(separator)
    if len(parts) > 2:
        return None
    try:
        known: dict[str, str] = {}
        for key, label in candidates:
            if type(key) is not str or type(label) is not str or key in known:
                return None
            known[key] = (f"旧反馈 {key}（仅作为本轮待答问题，不是许可；请在 decisions 用 key={feedback_question_key(key)} 单独回应）："
                          + json.dumps(label, ensure_ascii=False))
        user_text = parts[0]
        if not user_text.strip() or user_text != user_text.strip():
            return None
        if len(parts) == 2:
            lookup = {row: key for key, row in known.items()}
            rows = parts[1].split("\n")
            if not rows or any(row not in lookup for row in rows):
                return None
            selected = tuple(lookup[row] for row in rows)
            if len(set(selected)) != len(selected) or selected != tuple(key for key in known if key in selected):
                return None
        else:
            if any(line.startswith("旧反馈 ") for line in user_text.splitlines()):
                return None  # Ambiguous framing stays read-only; never mine prose as selections.
            selected = ()
        rebuilt = user_text + (separator + "\n".join(known[key] for key in selected) if selected else "")
        if compiler(rebuilt) != requirements:
            return None
    except (TypeError, ValueError):
        return None
    return purpose, user_text, selected


def proposal_selected_keys(requirements: str) -> tuple[str, ...]:
    """Only a selected, source-identified question creates a response obligation."""
    question = proposal_question(requirements)
    if question is None:
        question = question_question(requirements)
    if question is None:
        return ()
    sources = re.findall(r"^旧反馈 ([a-f0-9-]{36}:[1-6])（仅作为本轮待答问题，不是许可", question, re.MULTILINE)
    keys: list[str] = []
    for source in sources:
        try:
            keys.append(feedback_question_key(source))
        except ValueError:
            return ("__invalid_selected_feedback__",)  # Cannot be a valid DecisionSpec.key; fail closed.
    return tuple(dict.fromkeys(keys))


def feedback_answers(specs: tuple[DecisionSpec, ...], answers: tuple[Answer, ...]) -> tuple[Answer, ...]:
    """Explicit feedback is an evidence request, never a choice or execution grant."""
    if len(specs) != len(answers):
        raise ValueError("反馈与当前决策单不匹配。")
    if any(answer.kind != "undecided" for answer in answers):
        raise ValueError("发送反馈不能夹带技术选择、倾向或其他决定；请先恢复为未决定，或分别核对提交。")
    if not any(answer.comment.strip() for answer in answers):
        raise ValueError("请先写反馈，或明确选择‘请给有依据的建议’；不能空白提交。")
    for spec, answer in zip(specs, answers, strict=True):
        answer.check(spec)
    return tuple(replace(answer, kind="request_info", comment=answer.comment.strip()) if answer.comment.strip() else answer
                 for answer in answers)


def request_only_decisions(specs: tuple[DecisionSpec, ...], answers: tuple[Answer, ...]) -> bool:
    """Business answers and information requests are non-authorizing even when evidence is complete."""
    incomplete = any(not spec.complete or spec.kind == "budget" for spec in specs)
    formal = any(a.kind in {"choice", "approve", "budget"} for a in answers)
    if not incomplete and formal:
        return False
    if not incomplete and not any(a.business or a.kind in {"request_info", "preference"} for a in answers):
        return False
    if formal:
        raise ValueError("补证请求不能夹带正式批准；材料齐备后分别核对。")
    if not any(answer.kind in {"request_info", "preference"} or answer.business is not None for answer in answers):
        raise ValueError("请先答一项业务问题、要求补证或表达非授权倾向，不能空白提交补证请求。")
    return True


def preview_decisions(board: "Board", task_id: int, answers: tuple[Answer, ...], target: Target) -> DecisionPreview:
    event = decision_source(board, task_id)
    task = board.tasks[task_id]
    board._require_active(task)
    if task.status == "done":
        raise ValueError("已验收任务的决定只读。")
    assert event.report and event.report.decisions
    specs = event.report.decisions
    if len(answers) != len(specs):
        raise ValueError("答复与当前决策单不匹配。")
    answers = tuple(Answer.from_record(asdict(a)) for a in answers)
    normalized = []
    for spec, answer in zip(specs, answers, strict=True):
        if answer.business is not None:
            question = business_input(spec, task_id, event.id, spec.key)
            if question is None or (answer.business.question is not None and answer.business.question != question):
                raise ValueError("业务问题已变化；不能沿用旧答案。")
            answer = replace(answer, business=replace(answer.business, question=question))
        normalized.append(answer)
    answers = tuple(normalized)
    if (question_question(board.runs[task.run_id].requirements) is not None and
            any(answer.kind in {"choice", "approve", "budget"} for answer in answers)):
        raise ValueError("追问已有材料不能形成正式决定；技术方案或预算须通过独立材料与审批路径核对。")
    for spec, answer in zip(specs, answers, strict=True):
        answer.check(spec, business_input(spec, task_id, event.id, spec.key))
        if spec.kind == "budget" and answer.kind == "budget":
            raise ValueError("旧总帽及累计已用缺少结构化核实，不能正式批准预算；请先要求补证。")
    needs_evidence = any(not s.complete or s.kind == "budget" for s in specs)
    if target.kind == "saved" and needs_evidence and any(a.kind in {"choice", "approve", "budget"} for a in answers):
        raise ValueError("部分材料仍缺，不能在补证意见里形成正式批准；可保存草稿。")
    formal = any(a.kind in {"choice", "approve", "budget"} for a in answers)
    if target.kind != "draft" and formal and any(
            a.business is not None or a.kind in {"request_info", "preference", "defer", "reject"} or
            (a.kind == "undecided" and a.comment.strip()) for a in answers):
        raise ValueError("正式决定与事实/反馈/补证/暂缓意见请拆分，分别核对确认；不会自动复制旧批准。")
    request_only = request_only_decisions(specs, answers) if target.kind in {"new_run", "live_supplement"} else False
    feedback_saved = (target.kind == "saved" and all(a.kind == "undecided" for a in answers)
                      and any(a.comment.strip() for a in answers))
    check_target(target)
    context = context_for(board, task_id)
    run = board.runs[task.run_id]
    if target.kind in {"new_run", "live_supplement"}:
        mode = board.supplement_mode(task)
        if mode != ("next" if target.kind == "new_run" else "live") or target.cwd != context.cwd:
            raise ValueError("执行轮次、补充方式或目录已变化。")
        if target.kind == "live_supplement" and (target.agent_id, target.session_id, target.cwd) != (run.agent_id, run.session_id, run.cwd):
            raise ValueError("不能把当前执行的决定发送到其他会话。")
    source = Source(run.id, event.id, event.seq)
    records = [r for r in board.decisions.values() if r.task_id == task_id]
    if len(records) >= MAX_RECORDS:
        raise ValueError("本任务决策记录已达 128 条；不会清除既有授权证据。")
    previous = prior_answers(board, task_id)
    # Current blank answers never adopt old authorizations. Historical records stay readable.
    prior = tuple("" for _ in specs)
    if target.kind != "draft" and formal and any(a.kind not in {"choice", "approve", "budget"} for a in answers):
        raise ValueError("整单正式确认需要逐项明确本次决定；未选问题不会沿用旧批准，请先补齐或拆分提案。")
    if target.kind in {"new_run", "live_supplement"} and not request_only and not (
            any(a.kind in {"choice", "approve"} for a in answers) or
            any(ref and previous[spec.key][1].kind in {"choice", "approve"} for spec, ref in zip(specs, prior, strict=True))):
        raise ValueError("没有正式方案决定或补证请求，不能空白继续执行。")
    from journal import parse_event
    source_bytes = next(raw for raw in run.events if parse_event(raw) == event)
    fingerprint = digest({"context": asdict(context), "source": asdict(source), "source_bytes": source_bytes,
                          "specs": [spec_wire(s) for s in specs],
                          "run_state": run.state, "run_seq": run.seq, "head": records[-1].id if records else ""})
    lines = ["结构化人工决定 · 草稿不是批准；未决定/偏好不是授权。保留 Pi 权限确认。",
             f"任务 D{task_id:03} · {context.title}\n目录：{context.cwd}"]
    for spec, answer, reference in zip(specs, answers, prior, strict=True):
        if request_only and answer.kind == "undecided" and not answer.comment.strip() and answer.business is None:
            continue  # Keep source/specs for whole-sheet checks, but send only explicitly selected feedback.
        lines.extend([f"\nD{task_id:03}:{spec.key} · {spec.question}", "范围：" + spec.scope])
        if spec.routing:
            route = spec.routing
            lines.append("本项下一步：" + {"owner_evidence": "等负责人补证", "user_fact": "用户仅回答业务事实",
                                     "user_decision": "用户审阅有据技术提案"}[route.audience])
            lines.extend(f"缺口：{need.gap}；负责人：{need.owner}；来源：{need.source}" for need in route.evidence_needs)
            advice = route.advice
            if advice.status == "supported":
                lines.append(f"仅供核对的建议：{advice.value} {advice.unit}；适用：{advice.conditions}；依据："
                             + "；".join(advice.evidence)
                             + f"；不确定性：{advice.uncertainty}；负责人签收：{'是' if advice.owner_signed else '否'}。非授权。")
            else:
                lines.append("证据不足，暂不能推荐具体设置值；请等待上述负责人材料。")
        if reference:
            lines.append(f"此前已确认（{reference}）；本次不新增授权、不重置预算，消耗不明须先核实。")
            lines.append(answer_text(spec, previous[spec.key][1], business_input(spec, task_id, event.id, spec.key)))
        else:
            lines.append(answer_text(spec, answer, business_input(spec, task_id, event.id, spec.key)))
        lines.append("排除：" + ("；".join(spec.exclusions) or "无额外排除，仍受原任务范围限制"))
    if target.kind == "draft":
        lines.insert(0, "仅保存草稿：以下所有选择尚未确认，不可执行。")
    if target.kind == "saved":
        caption = ("已确认反馈/补证意见，仅本地保存；不批准、不发送、不启动。" if
                   feedback_saved or (not any(a.kind in {"choice", "approve", "budget"} for a in answers) and
                   (needs_evidence or any(a.kind in {"request_info", "preference"} or a.business for a in answers)))
                   else "已确认决定，仅本地保存；没有发送给执行者或启动处理。")
        lines.insert(0, caption)
    compiled = "\n".join(lines)
    if request_only and target.kind in {"new_run", "live_supplement"}:
        compiled = (EVIDENCE_ONLY_PREFIX + "以下倾向/缺口不构成授权；只核实已有材料，不执行新研究、经济试验、文件修改或下载。\n"
                    "原任务义务仅供核对，不是本轮新增执行许可。\n" + compiled)
    if target.kind == "new_run":
        if request_only:
            # Historical obligations remain in the old run/decision context, not the Pi dispatch.
            # Never re-send free-text old approvals, budgets or unresolved instructions as executable input.
            compiled += "\n原轮次的执行要求/剩余事项只保留在历史记录；补证轮不继承其执行许可。"
        else:
            from model import carried_requirements
            unresolved = board.supplement_prefill(task, "next")
            # Old quote snapshots are nonpermission data, but must still be a proven
            # historical prefix before regenerating the latest inventory/audit once.
            inherited = board.inherited_requirements(task, run)
            base = carried_requirements(inherited, run.remaining, unresolved)
            compiled = carried_requirements(base, compiled)
    if target.kind == "new_run":
        compiled = board.compiled_requirements(task, compiled)
    if target.kind in {"new_run", "live_supplement"}:
        from dependencies import operation
        board.check_dependencies(task, "evidence" if request_only else operation(compiled), agent_id=target.agent_id, requirements=compiled)
    limit = 4000 if target.kind == "live_supplement" else 6000
    if len(compiled) > limit:
        raise ValueError(f"完整预览超过 {limit} 字符；请精简备注或让执行者拆分决策单，不能截断授权。")
    return DecisionPreview(task_id, source, context, specs, answers, prior, fingerprint, compiled, target)


def validate_preview(board: "Board", preview: DecisionPreview) -> None:
    if preview_decisions(board, preview.task_id, preview.answers, preview.target) != preview:
        raise ValueError("任务、决策材料或确认记录已变化；请重新打开决策单核对。")


def prepare_decision_intent(board: "Board", preview: DecisionPreview) -> PreparedDecision:
    """Revalidate and create ledger + intent in ONE application snapshot transaction."""
    validate_preview(board, preview)
    task = board.tasks[preview.task_id]
    target = preview.target
    run = note = None
    if target.kind == "new_run":
        run = board.queue_real(task, str(uuid4()), target.session_id, target.cwd, decision=preview)
    elif target.kind == "live_supplement":
        # The atomic application caller catches validation failures before publishing.
        # pi-lens-ignore: ast-grep:unchecked-throwing-call-python
        note = board.prepare_supplement(task, str(uuid4()), preview.compiled_text, int(time.time() * 1000) + 30000, decision=preview)
    intent = Intent(target.kind, run.id if run else note.run_id if note else "", note.id if note else "")
    at = "" if target.kind == "draft" else datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    version = (3 if any(s.routing is not None for s in preview.specs) else
               2 if any(s.human_input is not None for s in preview.specs) or any(a.business is not None for a in preview.answers) else 1)
    record = DecisionRecord(version, str(uuid4()), at, preview, intent)
    DecisionRecord.from_record(decision_record_wire(record))
    board.decisions[record.id] = record
    return PreparedDecision(record, run, note)


def check_records(board: "Board") -> None:
    counts: dict[int, int] = {}
    seen: dict[str, DecisionRecord] = {}
    for record_id, record in board.decisions.items():
        if record_id != record.id or record.task_id not in board.tasks:
            raise ValueError("决策记录与任务身份不一致。")
        p = record.preview
        source = board.runs.get(p.source.run_id)
        if not source or source.task_id != record.task_id or p.source.seq > source.seq:
            raise ValueError("决策源轮次缺失、跨任务或修订超前。")
        for spec, reference in zip(record.specs, p.prior, strict=True):
            if not reference:
                continue
            prior = seen.get(reference)
            if not prior or not prior.confirmed_at or prior.preview.context.scope != p.context.scope or not any(s == spec and a.kind != "undecided" for s, a in zip(prior.specs, prior.preview.answers, strict=True)):
                raise ValueError("此前人工决定引用无效，不可用模型文字代替。")
        intent = record.intent
        if intent.kind == "new_run":
            run = board.runs.get(intent.run_id)
            if not run or run.task_id != record.task_id or run.requirements != p.compiled_text or (run.agent_id, run.session_id, run.cwd) != (p.target.agent_id, p.target.session_id, p.target.cwd):
                raise ValueError("决策记录必须绑定准确的新执行意图。")
        elif intent.kind == "live_supplement":
            note = board.supplements.get(intent.supplement_id)
            if not note or note.run_id != source.id or intent.run_id != source.id or note.text != p.compiled_text:
                raise ValueError("决策记录必须绑定准确的本轮补充意图。")
        counts[record.task_id] = counts.get(record.task_id, 0) + 1
        if counts[record.task_id] > MAX_RECORDS:
            raise ValueError("决策记录超过容量。")
        seen[record.id] = record
