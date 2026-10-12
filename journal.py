"""Source-stamped key records in the existing events:list[str] contract.

Decode only the reserved envelope. Legacy text is left untouched, not migrated or
assigned a time on read. Timestamps never decide run state or acceptance.
"""
import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from decisions import DecisionSpec, parse_specs
from handoff import Handoff

if TYPE_CHECKING:
    from model import Run, Task

PREFIX = "waggle:event:1 "
RESERVED = "waggle:event:"
REPORT_KINDS = {"progress", "blocked", "partial", "full"}
MAX_EVENTS = 120
MAX_EVENT_BYTES = 384 * 1024


@dataclass(frozen=True)
class Brief:
    actions: list[str]
    outcome: str
    verification: str


@dataclass(frozen=True)
class Report:
    summary: str
    checks: list[str]
    remaining: str
    brief: Brief | None = None
    decisions: tuple[DecisionSpec, ...] | None = None
    handoff: Handoff | None = None


@dataclass(frozen=True)
class Event:
    id: str
    at: str
    kind: str
    source: str
    run_id: str
    seq: int
    text: str
    report: Report | None


def _object(value: object, names: set[str]) -> dict:
    if not isinstance(value, dict) or set(value) != names:
        raise ValueError("关键记录字段缺失或不兼容。")
    return value


def _text(value: object, limit: int, *, nonempty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > limit or (nonempty and not value.strip()):
        raise ValueError("关键记录文字类型、长度或内容无效。")
    value.encode("utf-8")  # Reject unpaired surrogates, not silently replace persisted content.
    return value


def _strings(value: object, count: int, limit: int, *, nonempty: bool = False) -> list[str]:
    if not isinstance(value, list) or len(value) > count or (nonempty and not value):
        raise ValueError("关键记录列表类型或长度无效。")
    return [_text(item, limit, nonempty=nonempty) for item in value]


def _uuid(value: object, *, optional: bool = False) -> str:
    text = _text(value, 36)
    if optional and not text:
        return text
    if str(UUID(text)) != text:
        raise ValueError("关键记录身份无效。")
    return text


def _brief(value: object) -> Brief | None:
    if value is None:
        return None
    row = _object(value, {"actions", "outcome", "verification"})
    return Brief(_strings(row["actions"], 3, 100, nonempty=True),
                 _text(row["outcome"], 160, nonempty=True), _text(row["verification"], 160, nonempty=True))


def _report(value: object) -> Report | None:
    if value is None:
        return None
    names = {"summary", "checks", "remaining", "brief"}
    optional = {key for key in ("decisions", "handoff") if isinstance(value, dict) and key in value}
    row = _object(value, names | optional)
    checks = _strings(row["checks"], 32, 1000)
    if any(not check.strip() for check in checks):
        raise ValueError("报告依据不能是空白文字。")
    return Report(_text(row["summary"], 6000, nonempty=True), checks,
                  _text(row["remaining"], 6000), _brief(row["brief"]),
                  parse_specs(row["decisions"]) if "decisions" in row else None,
                  Handoff.from_record(row["handoff"]) if "handoff" in row else None)


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("关键记录存在重复 JSON 字段。")
        result[key] = value
    return result


def parse_event(value: str) -> Event | None:
    """None means legacy text; an invalid reserved envelope always raises."""
    if not value.startswith(RESERVED):
        return None
    if not value.startswith(PREFIX) or len(value.encode("utf-8")) > MAX_EVENT_BYTES:
        raise ValueError("关键记录版本未知或内容过大。")
    # Strict codec failures deliberately propagate to Store/runtime boundaries; never admit malformed data.
    # pi-lens-ignore: ast-grep:unchecked-throwing-call-python
    row = _object(json.loads(value[len(PREFIX):], object_pairs_hook=_unique_object),
                  {"id", "at", "kind", "source", "run_id", "seq", "text", "report"})
    at = _text(row["at"], 24)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", at):
        raise ValueError("关键记录时间必须是带毫秒的 UTC 时间。")
    if datetime.fromisoformat(at).isoformat(timespec="milliseconds").replace("+00:00", "Z") != at:
        raise ValueError("关键记录时间无效。")
    source = _text(row["source"], 16)
    if source not in {"human", "system", "executor", "simulation"}:
        raise ValueError("关键记录来源无效。")
    seq = row["seq"]
    if type(seq) is not int or not 0 <= seq <= 2**53 - 1:
        raise ValueError("关键记录修订锚点无效。")
    kind = _text(row["kind"], 48, nonempty=True)
    report = _report(row["report"])
    if source == "executor" and kind not in REPORT_KINDS:
        raise ValueError("执行者来源只能对应明确的自报快照。")
    if kind in {"created", "edited", "assigned", "dispatched", "returned", "accepted", "abandoned", "archived", "trashed", "restored", "supplement_requested", "supplement_saved", "plan_defined", "step_added", "step_updated"} and source not in {"human", "simulation"}:
        raise ValueError("人工操作记录的来源不正确。")
    if (kind in REPORT_KINDS) != (report is not None) or (report and source not in {"executor", "simulation"}):
        raise ValueError("报告快照与记录类型/来源不一致。")
    if kind == "full" and report and report.decisions:
        raise ValueError("待决策事项不能同时标记为完整交付。")
    if kind == "full" and report and (not report.checks or report.remaining.strip()):
        raise ValueError("完整交付快照必须有依据且无剩余项。")
    if kind in {"blocked", "partial"} and report and not report.remaining.strip():
        raise ValueError("阻塞/阶段交付快照必须说明剩余工作。")
    return Event(_uuid(row["id"]), at, kind, source, _uuid(row["run_id"], optional=True), seq,
                 _text(row["text"], 6000), report)


def validate_events(values: list[str], *, run_id: str | None = None, seq: int | None = None) -> list[Event]:
    events = [event for value in values if (event := parse_event(value)) is not None]
    if events and (len(values) > MAX_EVENTS or len(json.dumps(values, ensure_ascii=False).encode("utf-8")) > MAX_EVENT_BYTES):
        raise ValueError("关键记录超过保存上限。")
    if len({event.id for event in events}) != len(events):
        raise ValueError("关键记录 ID 重复。")
    for event in events:
        if run_id is not None and (event.run_id != run_id or event.source not in {"system", "executor"}):
            raise ValueError("关键记录不属于原执行或来源无效。")
        if seq is not None and (event.seq > seq or event.seq == 0):
            raise ValueError("关键记录来自尚未发生的修订或缺少修订。")
    origins = [event for event in events if event.kind == "origin"]
    if len(origins) > 1 or any(event.source != "system" or not event.run_id for event in origins):
        raise ValueError("原 Pi 会话定位记录冲突。")
    parents = [event for event in events if event.kind == "continued"]
    if len(parents) > 1 or any(event.source != "system" or not event.run_id or
                               _uuid(event.text) == event.run_id for event in parents):
        raise ValueError("执行续接的父轮次标识无效或重复。")
    return events


def check_history(previous: list[str], incoming: list[str]) -> None:
    """Retained event identities cannot be edited; the origin must never disappear."""
    old = {event.id: event for value in previous if (event := parse_event(value))}
    new = {event.id: event for value in incoming if (event := parse_event(value))}
    for key, event in old.items():
        if (key in new and new[key] != event) or (event.kind in {"origin", "continued"} and key not in new):
            raise ValueError("原执行的关键记录或 Pi 会话定位被更改，拒绝应用。")


def append_event(values: list[str], value: str) -> list[str]:
    """Bound changed journals only; protect origin/latest report and signal gaps."""
    added = parse_event(value)
    if added is None:
        raise ValueError("新增关键记录必须包含来源时间。")
    items = [(raw, parse_event(raw)) for raw in [*values, value]]
    gap = next((raw for raw, event in items if event and event.kind == "history_gap"), None)
    kept = [(raw, event) for raw, event in items if not event or event.kind != "history_gap"]
    latest_report = next((raw for raw, event in reversed(kept) if event and event.report), None)
    protected = {value, latest_report, *(raw for raw, event in kept if event and event.kind in {"origin", "continued"})}
    while True:
        result = ([gap] if gap else []) + [raw for raw, _ in kept]
        if len(result) <= MAX_EVENTS and len(json.dumps(result, ensure_ascii=False).encode("utf-8")) <= MAX_EVENT_BYTES:
            return result
        if gap is None:
            gap = encoded_event("history_gap", "部分较早记录已超出保存上限；以下仅为保留记录。", source="system",
                                run_id=added.run_id, seq=added.seq)
        index = next((index for index, (raw, _) in enumerate(kept) if raw not in protected), None)
        if index is None:
            raise ValueError("新记录与受保护的报告/会话位置超过上限；不会截断报告或覆盖旧数据。")
        kept.pop(index)


def local_time(at: str) -> str:
    return datetime.fromisoformat(at).astimezone().strftime("%Y-%m-%d %H:%M:%S %z") if at else "时间未知"


EVENT_LABELS = {
    "created": "创建任务", "edited": "修改要求", "assigned": "分配执行者", "dispatched": "保存派发意图",
    "acknowledged": "确认开始", "progress": "进展自报", "blocked": "阻塞自报", "partial": "阶段交付自报",
    "full": "完整交付自报", "waiting": "等待人工确认", "resumed": "人工确认返回", "stop_requested": "请求停止（未确认）",
    "stopped": "确认停止", "settled": "执行空闲（非验收）", "failed": "执行失败", "interrupted": "执行中断",
    "invalidated": "此前交付失效", "warning": "需要核实", "returned": "补充 / 退回", "accepted": "人工验收",
    "abandoned": "人工确认旧执行停止", "result_received": "工作台收到结果", "history_gap": "部分记录未保留",
    "archived": "归档任务", "trashed": "移入回收站", "restored": "恢复任务",
    "supplement_saved": "保存补充要求", "supplement_requested": "补充发送意图",
    "supplement_queued": "补充等待接收", "supplement_received": "Pi 接收补充",
    "supplement_rejected": "补充未被接收", "supplement_covered": "补充已自报覆盖",
    "continued": "Pi 直接续接新一轮", "direct_input": "Pi 本任务输入", "input_received": "Pi 接收直接输入",
    "plan_defined": "人工定义步骤计划", "step_added": "人工追加步骤", "step_updated": "人工更新步骤进度",
}
SOURCE_LABELS = {"human": "人工操作", "system": "系统记录", "executor": "执行者自报", "simulation": "模拟 · 虚构"}


def readable_event(value: str) -> Event | None:
    """Project known legacy business events only; empty id/time means NOT persisted metadata."""
    if event := parse_event(value):
        return event if event.kind != "origin" else None
    parts = value.split(" · ", 2)
    if len(parts) == 3 and parts[0] == "执行者报告" and parts[1] in REPORT_KINDS:
        return Event("", "", parts[1], "executor", "", 0, parts[2], None)
    prefixes = {
        "任务创建 ·": "created", "人工编辑 ·": "edited", "人工分配给 ": "assigned",
        "人工归档任务 ·": "archived", "人工移入回收站 ·": "trashed", "人工恢复任务 ·": "restored",
        "派发意图已保存 ·": "dispatched", "人工补充/退回 ·": "returned", "人工验收": "accepted",
        "人工确认旧 Pi 已停止 ·": "abandoned", "人工请求停止 ·": "stop_requested",
        "Pi 已确认对应任务消息 ·": "acknowledged", "Pi agent_settled": "settled",
        "等待 Pi 窗口中的人工确认": "waiting", "Pi 人工确认已返回": "resumed",
        "收到人工停止请求 ·": "stop_requested", "停止尚未执行：": "warning", "停止请求已过期": "warning",
        "Pi 收到额外输入": "invalidated", "Pi 响应错误 ·": "warning", "工具失败 ·": "warning",
        "派发未确认：": "warning", "停止未确认：": "warning", "较早进展已截断": "history_gap",
        "模拟排队 ·": "dispatched", "模拟执行者确认 ·": "acknowledged", "模拟进展 ·": "progress",
        "模拟阻塞 ·": "blocked", "模拟阶段交付 ·": "partial", "模拟整任务交付 ·": "full",
    }
    kind = next((kind for prefix, kind in prefixes.items() if value.startswith(prefix)), None)
    if kind is None:
        return None
    source = "simulation" if value.startswith(("模拟", "人工验收样例")) else "human" if value.startswith(("人工", "任务创建", "派发意图")) else "system"
    return Event("", "", kind, source, "", 0, value, None)


def last_progress_report(run: "Run") -> Event | None:
    """Last real self-report, even after a fault; never an acceptance/decision source."""
    return next((event for event in reversed(validate_events(run.events, run_id=run.id, seq=run.seq))
                 if event.source == "executor" and event.id and event.report and event.kind in REPORT_KINDS), None)


def current_report(task: "Task", run: "Run | None") -> Event | None:
    """Canonical current report for rendering AND human decision preparation."""
    if (not run or task.run == "none" or task.run_id != run.id or run.task_id != task.id
            or run.state in {"failed", "stopped", "interrupted", "abandoned"}):
        return None
    latest = next((event for value in reversed(run.events)
                   if (event := readable_event(value)) and event.kind in REPORT_KINDS), None)
    if latest and (report := latest.report) and latest.kind == run.delivery:
        projected_remaining = run.remaining if run.terminal else run.remaining or run.requirements
        if ((report.summary, report.checks, report.remaining) == (run.summary, run.checks, run.remaining)
                and (task.summary, task.checks, task.remaining) == (run.summary, run.checks, projected_remaining)):
            return latest
    return None


def encoded_event(kind: str, text: str, *, source: str, run_id: str = "", seq: int = 0,
                  report: Report | None = None) -> str:
    """Assign time/identity once at the producing action, never on display/load."""
    event = Event(str(uuid4()), datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                  kind, source, run_id, seq, text, report)
    row = asdict(event)
    if report is not None:
        if report.handoff is None:
            del row["report"]["handoff"]
        if report.decisions is None:
            del row["report"]["decisions"]
        else:
            for spec in row["report"]["decisions"]:
                if spec["human_input"] is None:
                    del spec["human_input"]
                if spec["routing"] is None:
                    del spec["routing"]
    value = PREFIX + json.dumps(row, ensure_ascii=False, separators=(",", ":"))
    parse_event(value)
    return value
