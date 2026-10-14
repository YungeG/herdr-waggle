"""Pure display values, never an answer, authorization or execution request."""
from dataclasses import dataclass
from typing import Literal

from decisions import BusinessInput, DecisionSpec, Source
from journal import Event, parse_event

Category = Literal["owner_evidence", "user_fact", "user_decision", "legacy"]


@dataclass(frozen=True)
class PresentedNeed:
    index: int
    gap: str
    owner: str
    source: str | None


@dataclass(frozen=True)
class ProviderGroup:
    owner: str
    needs: tuple[PresentedNeed, ...]


@dataclass(frozen=True)
class DecisionPresentation:
    category: Category
    question: str
    why: str
    guidance: str
    provider_groups: tuple[ProviderGroup, ...]


_ROUTED_CATEGORIES: dict[str, Category] = {
    "owner_evidence": "owner_evidence",
    "user_fact": "user_fact",
    "user_decision": "user_decision",
}
_GUIDANCE: dict[Category, str] = {
    "owner_evidence": "材料由报告列出的负责人补齐；可反馈或追问，不批准技术方案。",
    "user_fact": "仅回答明确的业务问题；回答不批准技术方案或访问范围。",
    "user_decision": "核对方案、范围、风险和成本；能否正式提交仍由原校验决定。",
    "legacy": "旧报告未明确受众；按原有问题和校验回应，不推断新授权。",
}


def present_decision(spec: DecisionSpec, *, human_input: BusinessInput | None) -> DecisionPresentation:
    """Organize an already parsed item without interpreting its execution authority."""
    category: Category = "legacy"
    grouped: dict[str, list[PresentedNeed]] = {}
    if spec.routing is not None:
        try:
            category = _ROUTED_CATEGORIES[spec.routing.audience]
        except KeyError as error:
            raise ValueError("展示受众无效；不从文本推断新受众。") from error
        for index, need in enumerate(spec.routing.evidence_needs, 1):
            grouped.setdefault(need.owner, []).append(PresentedNeed(index, need.gap, need.owner, need.source))
    elif spec.missing:
        grouped[spec.owner] = [PresentedNeed(index, gap, spec.owner, None)
                               for index, gap in enumerate(spec.missing, 1)]
    question = spec.question
    if category == "user_fact":
        if human_input is None or human_input != spec.human_input:
            raise ValueError("业务问题缺失或与本项来源不符；不替换问题。")
        question = human_input.question
    groups = tuple(ProviderGroup(owner, tuple(needs)) for owner, needs in grouped.items())
    return DecisionPresentation(category, question, spec.why, _GUIDANCE[category], groups)


@dataclass(frozen=True)
class ReportViewSnapshot:
    source: Source
    source_bytes: str


def capture_report_view(source: Source, source_bytes: str) -> ReportViewSnapshot:
    """Bind supplied metadata; callers must still compare these bytes with their current source."""
    snapshot = ReportViewSnapshot(source, source_bytes)
    report_view_event(snapshot)
    return snapshot


def report_view_event(snapshot: ReportViewSnapshot) -> Event:
    """Parse afresh so mutable report lists cannot be shared across display consumers."""
    if (not isinstance(snapshot, ReportViewSnapshot) or not isinstance(snapshot.source, Source)
            or type(snapshot.source_bytes) is not str or type(snapshot.source.seq) is not int):
        raise ValueError("原报告展示快照字段类型无效。")
    event = parse_event(snapshot.source_bytes)
    if (event is None or event.source != "executor" or not event.run_id or event.seq < 1
            or event.report is None or not event.report.decisions):
        raise ValueError("原报告展示快照缺失有效轮次、执行者报告或决策项。")
    if (event.run_id, event.id, event.seq) != (snapshot.source.run_id, snapshot.source.event_id, snapshot.source.seq):
        raise ValueError("原报告展示快照与轮次、事件或修订不符。")
    return event
