"""Human decision sheet: drafts, confirmed local records and explicit delivery."""
from collections.abc import Awaitable, Callable
from contextlib import nullcontext

from rich import box
from rich.console import Group
from rich.padding import Padding
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Collapsible,
    Input,
    OptionList,
    Select,
    Static,
    TextArea,
)
from textual.widgets.option_list import Option, OptionDoesNotExist

from decisions import (
    Answer,
    BusinessAnswer,
    DecisionOption,
    DecisionPreview,
    DecisionSpec,
    Target,
    answer_text,
    business_input,
    decision_source,
    historical_business_fact,
    preview_decisions,
    prior_answers,
    question_question,
    request_only_decisions,
    unchanged_evidence_keys,
)
from model import Board
from store import StorageError


class _ReviewText(Group):
    """Keep plain-text inspection while wrapped fields retain their group indent."""

    def __init__(self, plain: str) -> None:
        self.plain = plain
        renderables = []
        for line in plain.splitlines():
            indent = min(len(line) - len(line.lstrip(" ")), 8)
            text = Text(line[indent:], style="bold" if line.rstrip().endswith("：") else "", overflow="fold")
            renderables.append(Padding(text, (0, 0, 0, indent)))
        super().__init__(*renderables)

    def __str__(self) -> str:
        return self.plain


def _comparison_table(options: tuple[DecisionOption, ...], first: int = 1) -> Table:
    table = Table(box=box.SQUARE, show_lines=True, expand=True, padding=(0, 1),
                  border_style="#6c9db7", header_style="bold #f3f9ff on #31536c",
                  row_styles=["on #192e43", "on #263f54"])
    table.add_column("对照", width=4, no_wrap=True, style="bold #d2ecfa")
    for number, option in enumerate(options, first):
        table.add_column(Text(f"{number}. {option.label}\n[{option.key}]", style="bold #d2ecfa", overflow="fold"), ratio=1, overflow="fold")
    for heading, field, color in (("收益", "benefit", "#90d4ad"), ("代价", "cost", "#f1b778"),
                                  ("风险", "risk", "#f1b778"), ("范围", "scope", "#c3d6e9")):
        table.add_row(Text(heading, style=f"bold {color}"),
                      *(Text(getattr(option, field), style="#e6edf7") for option in options))
    return table


# Read-only interpretation of one immutable D002 report. Never an answer or authorization.
_D002_REPORT_ID = "7db8453b-95ae-401d-ba8d-8cb8c6088b8a"
_D002_PLAIN = {
    "koru_r1f_lifecycle": (
        "旧订单还没结束，新信号要反向怎么办？",
        "你只需说：是否必须在旧订单未成交前反向；若必须，最多能等多久、什么失败不能接受。不确定就先不选方案。",
        "负责人补撤单确认、费用与失败处理规则，以及能核查的旧失败记录；不是要你写程序。",
        "等你回答",
    ),
    "koru_native_identity": (
        "新功能要怎样与旧功能区分？",
        "你只需说明业务上是否必须把新旧功能分开；技术编号和数据格式由负责人审，不用你猜。",
        "负责人核对编号不冲突、新旧结果能否对照，并签收具体技术方案。",
        "等负责人补证",
    ),
    "koru_candidate_scope": (
        "将来如需只读其他文件，你允许看哪里？",
        "只有你能指定将来允许只读的确切目录或文件；不确定就维持当前范围，不要猜路径。",
        "负责人列出候选文件、来源和改动清单；只读不等于允许修改。",
        "等你回答",
    ),
    "koru_r1f_total_budget": (
        "旧账未核清，要不要以后增加额度？",
        "你现在不用填新次数、时间或空间；先看核对后的旧批准与已用量，再决定是否增加额度。",
        "实验负责人提交旧批准、已用量和停止条件；本轮不运行新试验。",
        "等负责人补证",
    ),
}


def _d002_plain_help(task_id: int, source_event_id: str, key: str) -> tuple[str, str, str, str] | None:
    return _D002_PLAIN.get(key) if task_id == 2 and source_event_id == _D002_REPORT_ID else None


class DiscardChanges(ModalScreen[bool]):
    """Safe editor exit; neither choice persists, confirms or publishes anything."""
    BINDINGS = [Binding("escape", "keep", "继续编辑"), Binding("ctrl+enter", "keep", "继续编辑", priority=True)]

    def compose(self) -> ComposeResult:
        with Vertical(id="discard-changes", classes="decision-dialog"):
            yield Static("还有未保存的修改", classes="dialog-title", markup=False)
            yield Static("继续编辑会保留本窗口的文字和选择。放弃修改才关闭；不会保存、确认或发送。",
                         id="discard-message", markup=False)
            with Grid(classes="decision-controls"):
                yield Button("继续编辑", id="keep-editing", variant="primary")
                yield Button("放弃修改", id="discard-edit")

    def on_mount(self) -> None:
        self.query_one("#keep-editing", Button).focus()

    def action_keep(self) -> None:
        if self.is_running and self.app.screen is self:
            self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if not self.is_running or self.app.screen is not self:
            return
        if event.button.id == "keep-editing":
            self.action_keep()
        elif event.button.id == "discard-edit":
            self.dismiss(True)


class DecisionReview(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "back", "返回修改")]

    def __init__(self, preview: DecisionPreview, target_label: str,
                 commit: Callable[[DecisionPreview], Awaitable[None]]) -> None:
        super().__init__()
        self.preview, self.target_label, self.commit = preview, target_label, commit
        self.busy = False

    def set_processing_stage(self, message: str) -> None:
        if not self.is_mounted or not self.is_running or self.app.screen is not self:
            return
        stage = self.query_one("#decision-stage", Static)
        stage.update(message)
        stage.display = bool(message)
        if message:
            stage.scroll_visible(top=True, animate=False)

    def show_error(self, message: str) -> None:
        if not self.is_mounted or not self.is_running or self.app.screen is not self:
            return
        error = self.query_one("#decision-error", Static)
        error.update(message)
        error.display = True
        error.scroll_visible(top=True, animate=False)
        error.focus()

    def summary(self) -> str:
        target = self.preview.target.kind
        request_only = target in {"new_run", "live_supplement"} and request_only_decisions(self.preview.specs, self.preview.answers)
        heading = ("本次：只保存草稿，不发送、不执行" if target == "draft" else
                   "本次：确认本次答复，仅本地保存；有确认记录，但不发送、不执行" if target == "saved" else
                   "本次：发送反馈／请求建议，仅核对已有材料；不批准方案" if request_only and self.target_label.startswith("反馈／") else
                   "本次：仅发送补证请求，不实施方案" if request_only else
                   "本次：提交已选方案，可能执行真实任务；仍需 Pi 权限确认")
        lines = [f"所属任务：D{self.preview.task_id:03} · {self.preview.context.title}", heading]
        for index, (spec, answer, prior) in enumerate(zip(self.preview.specs, self.preview.answers, self.preview.prior, strict=True), 1):
            plain = _d002_plain_help(self.preview.task_id, self.preview.source.event_id, spec.key)
            question = plain[0] if plain else spec.question
            if answer.business is not None:
                source = answer.business.question or business_input(spec, self.preview.task_id, self.preview.source.event_id, spec.key)
                if source is not None:
                    question = source.question
                label = next((o.label for o in source.options if o.key == answer.business.value), answer.business.value) if source else answer.business.value
                if answer.business.value == "unknown":
                    label += "（尚无业务结论）"
                business = label + ("；条件：" + answer.business.detail if answer.business.detail else "")
            else:
                question_input = business_input(spec, self.preview.task_id, self.preview.source.event_id, spec.key)
                business = ("未回答" if question_input else
                            "等负责人补证，无需你填写" if plain and plain[3] == "等负责人补证" else
                            "本项无独立业务问题")
            scope = ""
            if prior:
                solution = "沿用此前确认的决定（展开原文核对）"
            elif answer.kind == "choice":
                option = next(o for o in spec.options if o.key == answer.choice)
                solution = "正式选择：" + option.label
                scope = option.scope
            elif answer.kind == "approve":
                solution = "批准"
                scope = spec.scope
            elif answer.kind == "preference":
                option = next(o for o in spec.options if o.key == answer.choice)
                solution = "仅表达倾向：" + option.label + "（不授权）"
            else:
                solution = {"undecided": "未选择", "request_info": "请解释、补材料", "defer": "暂缓", "reject": "拒绝"}.get(answer.kind, "待核对")
            intent = ("正式决定" if answer.kind in {"choice", "approve", "budget"} or prior else
                      "补充业务事实（非授权）" if answer.business else
                      "等待/拒绝（非授权）" if answer.kind in {"defer", "reject"} else
                      "反馈/追问（非授权）" if answer.kind != "undecided" or answer.comment.strip() else "未选 · 本次不发送")
            lines.append(f"{index}. 针对的问题：{question}\n"
                         f"   问题来源：D{self.preview.task_id:03} · 当前报告第{index}项 · 版本{self.preview.source.seq}\n"
                         f"   本次答复：\n     业务：{business}\n     方案：{solution}")
            if scope:
                lines.append("     范围：" + scope)
            lines.append("   意图：" + intent)
            if answer.comment:
                lines.append("   备注（不扩权）：" + answer.comment)
            if answer.kind in {"choice", "approve"} or prior:
                lines.append("   风险：" + spec.risk + "；代价：" + spec.cost)
                if answer.kind == "choice":
                    chosen = next(o for o in spec.options if o.key == answer.choice)
                    if (chosen.risk, chosen.cost) != (spec.risk, spec.cost):
                        lines.append("   所选方案风险/代价：" + chosen.risk + " / " + chosen.cost)
                lines.append("   排除：" + ("；".join(spec.exclusions) or "仍受原任务范围限制"))
        return "\n".join(lines)

    def confirm_label(self) -> str:
        target = self.preview.target.kind
        if target in {"new_run", "live_supplement"} and request_only_decisions(self.preview.specs, self.preview.answers):
            if self.target_label.startswith("反馈／"):
                return "确认并发送反馈·仅补证"
            return "确认并发送补证请求"
        if target == "saved" and all(a.kind == "undecided" for a in self.preview.answers) and any(
                a.comment.strip() for a in self.preview.answers):
            return "确认仅保存反馈"
        if target == "saved" and any(a.business is not None for a in self.preview.answers) and not any(
                a.kind in {"choice", "approve", "budget"} for a in self.preview.answers):
            return "确认仅保存业务答案"
        return {"draft": "保存草稿", "saved": "确认决定，仅保存", "new_run": "确认并开始下一轮", "live_supplement": "确认并发送本轮"}[target]

    def compose(self) -> ComposeResult:
        with Vertical(id="decision-review", classes="decision-dialog"):
            yield Static("汇总核对 · 先核对本次答案和后果", classes="dialog-title")
            if len(self.preview.specs) > 1:
                yield Static(f"共 {len(self.preview.specs)} 项 · 向下滚动核对其余答案；默认返回修改",
                             id="decision-review-progress", classes="decision-meta", markup=False)
            with VerticalScroll(classes="decision-body"):
                stage = Static("", id="decision-stage", classes="decision-meta", markup=False)
                stage.display = False
                yield stage
                error = Static("", id="decision-error", classes="error", markup=False)
                error.can_focus = True
                yield error
                yield Static(_ReviewText(self.summary()), id="decision-review-summary", markup=False)
                if self.preview.target.kind in {"new_run", "live_supplement"}:
                    lines = self.target_label.splitlines()
                    receiver = next((line for line in lines if line.startswith("接收方：")), "接收方身份见完整来源详情")
                    target = (lines[0] + "\n提交目标：\n  " + receiver + "\n  目录：" + self.preview.target.cwd
                              + "\n确认后果：\n  " + lines[-1])
                    yield Static(_ReviewText(target), id="decision-target", markup=False)
                with Collapsible(title="完整原文、连接和报告来源 · 按需展开", collapsed=True, id="decision-review-source"):
                    yield Static(self.target_label, markup=False)
                    yield Static("原任务范围：\n" + self.preview.context.description, markup=False)
                    yield Static(f"报告来源：{self.preview.source.run_id}\n报告修订：{self.preview.source.seq} · {self.preview.source.event_id}", markup=False, classes="muted")
                    yield TextArea(self.preview.compiled_text, read_only=True, id="decision-preview-text")
            with Grid(classes="decision-controls"):
                yield Button(self.confirm_label(), id="decision-confirm", variant="primary")
                yield Button("返回修改", id="decision-back")

    def on_mount(self) -> None:
        self.query_one("#decision-back", Button).focus()

    def on_resize(self) -> None:
        narrow = self.size.width < 64
        self.set_class(narrow, "decision-review-narrow")
        if self.is_mounted:
            self.query_one("#decision-confirm", Button).label = self.confirm_label()
            if len(self.preview.specs) > 1:
                self.query_one("#decision-review-progress", Static).update(
                    f"共 {len(self.preview.specs)} 项 · ↓看其余；默认返回" if narrow else
                    f"共 {len(self.preview.specs)} 项 · 向下滚动核对其余答案；默认返回修改")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "decision-back":
            self.action_back()
        elif event.button.id == "decision-confirm" and not self.busy:
            self.busy = True
            for button in self.query("Button"):
                button.disabled = True
            self.run_worker(self.confirm())

    async def confirm(self) -> None:
        try:
            if not self.is_running or self.app.screen is not self:
                return
            await self.commit(self.preview)
        except (ValueError, OSError, StorageError) as error:
            if self.is_running:
                self.show_error(f"未提交：{error}")
        else:
            if self.is_running:
                self.dismiss(True)
        finally:
            self.busy = False
            if self.is_running:
                self.set_processing_stage("")
                for button in self.query("Button"):
                    button.disabled = False

    def action_back(self) -> None:
        if not self.busy:
            self.dismiss(False)


class DecisionSection(Vertical, can_focus=True):
    """Reading focus target; the overview retains arrow-key question navigation."""


class DecisionSheet(ModalScreen):
    BINDINGS = [Binding("escape", "back", "返回"), Binding("ctrl+enter", "saved", "核对决定", priority=True)]

    def __init__(self, board: Board, task_id: int,
                 prepare: Callable[["DecisionSheet", str, tuple[Answer, ...]], Awaitable[None]], *, focus_key: str = "") -> None:
        super().__init__()
        self.task_id, self.prepare = task_id, prepare
        self.focus_key = focus_key
        event_task = board.tasks[task_id]
        self.title_text = f"D{task_id:03} · {event_task.title} · 决策单"
        source = decision_source(board, task_id)
        assert source.report and source.report.decisions
        self.specs = source.report.decisions
        self.source_event_id = source.id
        source_run = board.runs.get(event_task.run_id)
        self.question_only = bool(source_run and question_question(source_run.requirements) is not None)
        self.business_inputs = tuple(business_input(spec, task_id, source.id, spec.key) for spec in self.specs)
        self.selected_index = next((i for i, spec in enumerate(self.specs) if spec.key == focus_key), 0)
        self.needs_evidence = any(not spec.complete or spec.kind == "budget" for spec in self.specs)
        self.readonly = event_task.status == "done" or event_task.retention != "active"
        self.basis = None if self.readonly else preview_decisions(board, task_id, tuple(Answer() for _ in self.specs), Target("draft"))
        self.previous = prior_answers(board, task_id)
        self.business_history = tuple(historical_business_fact(board, task_id, spec.key) if question else None
                                      for spec, question in zip(self.specs, self.business_inputs, strict=True))
        self.waiting_keys = set(unchanged_evidence_keys(board, task_id))
        self.initial = tuple(Answer() for _ in self.specs)
        latest = next((r for r in reversed(list(board.decisions.values())) if r.task_id == task_id), None)
        same_report = bool(latest and self.basis and latest.preview.source == self.basis.source
                           and latest.preview.context == self.basis.context and latest.specs == self.specs)
        self.has_draft = bool(same_report and latest and latest.intent.kind == "draft")
        self.has_saved_feedback = bool(same_report and latest and latest.intent.kind == "saved"
                                       and all(a.kind == "undecided" for a in latest.preview.answers)
                                       and any(a.comment.strip() for a in latest.preview.answers))
        self.owner_requests: set[int] = set()
        if (self.has_draft or self.has_saved_feedback) and latest:
            self.initial = latest.preview.answers
        # Old confirmed facts/choices remain read-only background, never input defaults.
        self.intents = [self.answer_intent(answer) for answer in self.initial]
        self.technical_values: dict[tuple[int, str], str] = {}
        for index, answer in enumerate(self.initial):
            if answer.kind != "budget":
                self.technical_values[index, self.intents[index]] = answer.kind + (":" + answer.choice if answer.choice else "")
        self.busy = False
        self._discard_prompt_open = False
        self._initial_edit_values: tuple[tuple[str | None, str], ...] | None = None

    @staticmethod
    def answer_intent(answer: Answer) -> str:
        if answer.kind in {"choice", "approve", "budget"}:
            return "formal"
        if answer.business is not None:
            return "fact"
        if answer.kind == "defer" and not answer.comment:
            return "wait"
        return "feedback" if answer.kind != "undecided" or answer.comment.strip() else ""

    def set_processing_stage(self, message: str) -> None:
        if not self.is_mounted or not self.is_running or self.app.screen is not self:
            return
        stage = self.query_one("#decision-stage", Static)
        stage.update(message)
        stage.display = bool(message)
        if message:
            stage.scroll_visible(top=True, animate=False)

    def show_error(self, message: str) -> None:
        if not self.is_mounted or not self.is_running or self.app.screen is not self:
            return
        error = self.query_one("#decision-error", Static)
        error.update(message)
        error.display = True
        error.scroll_visible(top=True, animate=False)
        error.focus()

    def formal_available(self, spec: DecisionSpec) -> bool:
        return (not self.question_only and spec.complete and not self.needs_evidence and spec.kind in {"choice", "approval"}
                and (not spec.routing or spec.routing.audience == "user_decision"))

    def item_status(self, index: int) -> str:
        if self.specs[index].key in self.waiting_keys:
            return "等待负责人材料 · 无新证据"
        if self.has_saved_feedback and self.initial[index].comment.strip():
            return "反馈仅保存 · 未发送"
        if self.business_history[index]:
            return "历史业务事实 · 待核对"
        spec = self.specs[index]
        previous = self.previous.get(spec.key)
        if previous:
            if previous[0].intent.kind == "saved":
                return "业务答案已存未发" if previous[1].business else "已存未发"
            if previous[1].business and previous[1].kind == "undecided":
                return "业务答案已提交 · 待核实"
            if spec.kind == "budget":
                return "旧预算待核账本"
            return {"reject": "已拒绝", "defer": "已暂缓"}.get(
                previous[1].kind, "已提交补证，待核实" if not spec.complete else "已有正式决定")
        plain = _d002_plain_help(self.task_id, self.source_event_id, spec.key)
        return plain[3] if plain else "待补证" if not spec.complete or spec.kind == "budget" else "待决定"

    def item_prompt(self, index: int, status: str) -> str:
        spec = self.specs[index]
        plain = _d002_plain_help(self.task_id, self.source_event_id, spec.key)
        if plain:
            return f"{index + 1}. {status} · {plain[0]}"
        question = spec.question[:14] + ("…" if len(spec.question) > 14 else "")
        owner = spec.owner[:6] + ("…" if len(spec.owner) > 6 else "")
        return f"{index + 1}. {status} · {question} · 责任：{owner}"

    def intent_options(self, index: int) -> list[tuple[str, str]]:
        choices = [("暂未选择", "")]
        if self.business_inputs[index]:
            choices.append(("补充业务事实（不批准方案）", "fact"))
        choices.append(("反馈／追问／表达倾向（非授权）", "feedback"))
        if self.formal_available(self.specs[index]):
            choices.append(("正式决定（需完整核对后确认）", "formal"))
        choices.append(("等待材料（不重复派发）", "wait"))
        return choices

    def answer_options(self, index: int, intent: str) -> list[tuple[str, str]]:
        spec = self.specs[index]
        choices = [("尚未正式选择" if intent == "formal" else "仅写反馈，不选技术方案", "undecided")]
        if intent == "formal":
            if self.formal_available(spec):
                if spec.kind == "choice":
                    choices.extend(("正式选择 · " + o.label, "choice:" + o.key) for o in spec.options)
                else:
                    choices.append(("批准所述范围", "approve"))
        else:
            choices.extend([("请解释并补齐材料", "request_info"), ("暂缓（非授权）", "defer"), ("拒绝", "reject")])
            if spec.kind in {"choice", "preference"}:
                choices.extend(("表达倾向（非授权）· " + o.label, "preference:" + o.key) for o in spec.options)
        return choices

    def reply_controls(self, index: int, spec: DecisionSpec, initial: Answer) -> ComposeResult:
        intent = self.intents[index]
        yield Static("本次答复类型", classes="decision-section-heading", markup=False)
        yield Static("未选择的条目不会发送；选择类型不等于保存或发送。", classes="decision-meta", markup=False)
        yield Static("旧确认仅作只读背景，不默认沿用。", classes="decision-meta", markup=False)
        yield Select(self.intent_options(index), value=intent, allow_blank=False, id=f"intent-{index}", disabled=self.readonly)
        question = self.business_inputs[index]
        if question:
            with Vertical(id=f"decision-fact-{index}", classes="decision-intent-fields"):
                yield Static("业务事实 · " + question.question + "（不批准方案）", classes="decision-meta", markup=False)
                if question.kind == "choice":
                    value = initial.business.value if initial.business else ""
                    yield Select([("尚未回答（默认）", "")] + [(o.label, o.key) for o in question.options], value=value,
                                 allow_blank=False, id=f"business-choice-{index}", disabled=self.readonly)
                    yield Input(initial.business.detail if initial.business else "", placeholder="有条件时必填：条件/等待/失败边界",
                                id=f"business-detail-{index}", classes="decision-business-detail", max_length=500, disabled=self.readonly)
                    yield Static("", id=f"business-error-{index}", classes="decision-inline-error", markup=False)
                else:
                    yield Static("目录/文件只作定位引用；未核实，不授予访问许可。", classes="decision-meta", markup=False)
                    yield Input(initial.business.value if initial.business else "", placeholder="具体目录或文件（不授予访问许可）",
                                id=f"business-text-{index}", max_length=500, disabled=self.readonly)
            history = self.business_history[index]
            if history:
                record, fact = history
                label = next((option.label for option in question.options if option.key == fact.value), fact.value)
                yield Static(f"历史已确认业务事实 · {label}" + (f"；条件：{fact.detail}" if fact.detail else "") +
                             f"；{record.confirmed_at}；只读背景，仅供核对，不自动沿用或授权，未填入本次答案。",
                             id=f"decision-business-history-{index}", classes="decision-meta", markup=False)
        with Vertical(id=f"decision-technical-{index}", classes="decision-intent-fields"):
            yield Static("方案选择与业务事实分开确认；倾向不是批准。", classes="decision-meta", markup=False)
            choices = self.answer_options(index, intent)
            value = self.technical_values.get((index, intent), "undecided")
            if value not in {key for _, key in choices}:
                value = "undecided"
            yield Select(choices, value=value, allow_blank=False, id=f"answer-{index}", disabled=self.readonly)
        yield Static("等待材料：不会再次要求执行。需要追问时明确切换反馈，或写本次问题。",
                     id=f"decision-wait-guide-{index}", classes="decision-meta", markup=False)
        with Vertical(id=f"decision-comment-{index}", classes="decision-intent-fields"):
            yield Static("本次反馈／备注 · 最多 500 字，可多行；不扩展授权。",
                         id=f"comment-length-{index}", classes="decision-meta", markup=False)
            yield TextArea(initial.comment, id=f"comment-{index}", classes="decision-comment-input", disabled=self.readonly)
            yield Button("请给有依据的建议", id=f"decision-recommend-{index}", classes="decision-reply-jump", disabled=self.readonly)
            yield Button(f"核对本单反馈 · 共{len(self.specs)}项", id=f"decision-feedback-{index}", classes="decision-reply-jump", disabled=self.readonly)

    def compose(self) -> ComposeResult:
        with Vertical(id="decision-sheet", classes="decision-dialog"):
            yield Static(self.title_text, classes="dialog-title", markup=False)
            yield Static(f"第 {self.selected_index + 1}/{len(self.specs)} 项 · ↑↓切项" +
                         (" · 草稿已恢复" if self.has_draft else " · 反馈已本地保存" if self.has_saved_feedback else " · Tab读缺口"),
                         id="decision-navigation", classes="muted", markup=False)
            overview = [Option(self.item_prompt(index, self.item_status(index)), id=str(index))
                        for index in range(len(self.specs))]
            yield OptionList(*overview, id="decision-overview", markup=False)
            with VerticalScroll(classes="decision-body", id="decision-body"):
                stage = Static("", id="decision-stage", classes="decision-meta", markup=False)
                stage.display = False
                yield stage
                error = Static("已验收/归档任务只读。" if self.readonly else "", id="decision-error", classes="error", markup=False)
                error.can_focus = True
                yield error
                for index, (spec, initial) in enumerate(zip(self.specs, self.initial, strict=True)):
                    classes = "decision-item" + (" decision-inactive" if index != self.selected_index else "")
                    with DecisionSection(classes=classes, id=f"decision-item-{index}"):
                        plain = _d002_plain_help(self.task_id, self.source_event_id, spec.key)
                        yield Static(f"要决定 · {index + 1}/{len(self.specs)}", classes="decision-section-heading decision-first", markup=False)
                        yield Static(plain[0] if plain else spec.question, classes="decision-question", markup=False)
                        unresolved = not spec.complete or spec.kind == "budget"
                        if unresolved and self.business_inputs[index]:
                            yield Button("回答业务问题 ↓", id=f"decision-reply-{index}",
                                         classes="decision-reply-jump decision-business-jump", disabled=self.readonly)
                            yield from self.reply_controls(index, spec, initial)
                        yield Static("你先做什么", id=f"decision-action-{index}", classes="decision-section-heading", markup=False)
                        if unresolved and not self.business_inputs[index]:
                            yield Button("请负责人补证本项", id=f"decision-owner-request-{index}",
                                         classes="decision-reply-jump", disabled=self.readonly)
                            yield Button("其他意见 ↓",
                                         id=f"decision-reply-{index}", classes="decision-reply-jump", disabled=self.readonly)
                        if spec.routing and spec.routing.audience == "owner_evidence":
                            yield Static("等待负责人 · " + ("；".join(f"{need.owner}提供{need.source}" for need in spec.routing.evidence_needs) or spec.owner)
                                         + "；可反馈，但不要选技术方案。", classes="decision-meta", markup=False)
                        yield Static("你来回答 · " + plain[1] if plain else
                                     "不懂也可以处理：不必填写；只写你清楚的业务目标，技术证据请负责人提供。" if unresolved else
                                     "核对方案范围和风险；不确定就先请解释，不要猜。正式选择后仍需确认。", id=f"decision-action-guide-{index}", classes="decision-meta", markup=False)
                        if spec.key in self.waiting_keys:
                            yield Static("补证后没有新证据；请等待负责人材料。仍可明确再次提交反馈，不会自动派发。",
                                         id=f"decision-waiting-{index}", classes="decision-meta", markup=False)
                        if unresolved and not self.business_inputs[index]:
                            yield from self.reply_controls(index, spec, initial)
                        if unresolved:
                            yield Button("看不懂？请解释本项", id=f"decision-explain-{index}", classes="decision-explain", disabled=self.readonly)
                        if plain:
                            yield Static("不是你补 · " + plain[2], id=f"decision-other-{index}", classes="decision-meta", markup=False)
                        yield Static("本项 · " + self.item_status(index) + "；这里只是填写，不会发送。", id=f"decision-live-{index}", classes="decision-live", markup=False)
                        with (Collapsible(title="查看缺口、方案与原报告（Agent 自报）", collapsed=True, id=f"decision-details-{index}", classes="decision-details") if unresolved else nullcontext()):
                            yield Static("具体缺什么 · 尚不可批准" if unresolved else "具体缺什么 · 未列缺口，仍需核对",
                                         classes="decision-section-heading decision-warning" if unresolved else "decision-section-heading", markup=False)
                            gap = "缺少可核对依据。" if not spec.evidence else "原上限及累计已用的结构化账本尚未提供。" if spec.kind == "budget" else "无已列缺口；仍须核对依据。"
                            yield Static("\n".join(f"{number}. {missing}" for number, missing in enumerate(spec.missing, 1)) if spec.missing else gap,
                                         id=f"decision-gaps-{index}", classes="decision-missing", markup=False)
                            yield Static("谁负责（Agent 自报；缺口未逐项分配）· " + spec.owner, id=f"decision-owner-{index}", classes="decision-meta", markup=False)
                            yield Static(("原报告问题（专业表述）· " + spec.question + "\n" if plain else "") +
                                         "为什么要决定（Agent 自报）· " + spec.why, id=f"decision-context-{index}", classes="decision-meta", markup=False)
                            if spec.options:
                                yield Static("方案对照 · 收益 / 代价 / 风险 / 范围（Agent 自报）", classes="decision-section-heading", markup=False)
                                if len(spec.options) == 2:
                                    yield Static(_comparison_table(spec.options), id=f"comparison-wide-{index}", classes="decision-comparison decision-compare-wide")
                                for number, option in enumerate(spec.options):
                                    yield Static(_comparison_table((option,), number + 1), id=f"comparison-compact-{index}-{number}",
                                                 classes="decision-comparison" + (" decision-compare-compact" if len(spec.options) == 2 else ""))
                            if spec.kind == "budget":
                                yield Static("预算核对 · 建议不是批准", classes="decision-section-heading decision-warning", markup=False)
                                yield Static("累计账本请在更多工具 / Ctrl+K 单独核对。正式预算批准入口未启用；业务答案、反馈与本地账本不替代派发许可。", classes="decision-meta", markup=False)
                                b = spec.budget
                                previous_budget = self.previous.get(spec.key)
                                old = previous_budget[1].budget if previous_budget and previous_budget[1].kind == "budget" else None
                                baseline = (f"{old.attempts} 次 / {old.minutes} 分钟 / {old.storage_mib} MiB（本地此前确认的总帽，非剩余额度）" if old else "未知（无可核对的结构化旧账本）")
                                proposal = (f"{b.attempts} 次 / {b.minutes} 分钟 / {b.storage_mib} MiB" if b else "未提供")
                                yield Static(f"原上限：{baseline}\n累计已用：未知（未提供结构化用量）\n建议新上限（未采纳）：{proposal}\n建议停止条件：" + ("；".join(b.stop_conditions) if b else "未提供"), id=f"budget-facts-{index}", classes="decision-budget", markup=False)
                            yield Static("推荐与边界 · Agent 自报，非默认选择", classes="decision-section-heading", markup=False)
                            if spec.routing:
                                route = spec.routing
                                advice = route.advice
                                note = (f"有据候选 · {advice.value} {advice.unit}；条件：{advice.conditions}\n"
                                        + "依据：" + "；".join(advice.evidence)
                                        + f"\n不确定性：{advice.uncertainty}；负责人签收：{'是' if advice.owner_signed else '否'}；非批准。"
                                        if advice.status == "supported" else
                                        "证据不足 · 暂不能推荐具体设置值；等 " + "；".join(
                                            f"{need.owner}提供{need.source}（{need.gap}）" for need in route.evidence_needs))
                            else:
                                note = f"旧报告建议（未结构化核验）· {spec.recommendation}\n理由 · {spec.reason}；不作默认选择。"
                            yield Static(note, id=f"decision-recommendation-{index}", classes="decision-recommendation", markup=False)
                            yield Static("范围 · " + spec.scope + "\n不包括 · " + ("；".join(spec.exclusions) or "无额外排除，仍受原任务范围限制"), classes="decision-boundary", markup=False)
                            with Collapsible(title=f"完整依据与背景 · {len(spec.evidence)} 条（Agent 自报）", collapsed=True, classes="decision-evidence"):
                                yield Static(f"完整决策责任：{spec.owner}\n为什么：{spec.why}\n整体风险：{spec.risk}\n整体成本：{spec.cost}\n依据：\n" + ("\n".join(f"{number}. {evidence}" for number, evidence in enumerate(spec.evidence, 1)) if spec.evidence else "未提供，不可批准"), markup=False)
                            previous = self.previous.get(spec.key)
                            if previous:
                                record, answer = previous
                                is_formal = answer.kind in {"choice", "approve", "budget"}
                                title = ("此前预算记录 · 旧用量未知不能沿用" if spec.kind == "budget" else
                                         "此前正式决定" if is_formal else
                                         "此前业务事实（非授权）" if answer.business else "此前用户反馈（非授权）")
                                note = ("旧预算用量未知，不能继续沿用；请核对账本。" if spec.kind == "budget" else
                                        "业务答案不会自动沿用或扩大此前的技术授权；实施方案须另行核对。" if answer.business else
                                        "材料仍缺，倾向不构成授权。" if not spec.complete else
                                        "方案未变，无需重选；不新增授权或重置预算。")
                                delivery = (("仅本地保存，未发送；如需补证，请在本项选择“看不懂：请解释”并确认提交。" if unresolved else
                                             "仅本地保存，未发送；如需发送本次答复，请重新核对目标与后果。") if record.intent.kind == "saved" else
                                            "已提交意图；是否收到或完成仍需核实。")
                                yield Static(f"{title} · {record.confirmed_at}\n{answer_text(spec, answer, self.business_inputs[index])}\n{note}\n{delivery}", id=f"decision-previous-{index}", classes="decision-previous", markup=False)
                        if not unresolved:
                            yield from self.reply_controls(index, spec, initial)
                            yield Button("看不懂？请解释本项", id=f"decision-explain-{index}", classes="decision-explain", disabled=self.readonly)
            yield Static("保存≠发送；补证需再确认；材料齐后再决定，不自动执行。" if self.needs_evidence else
                         "保存≠发送；正式选择仍需确认，不自动执行。", id="decision-flow", classes="decision-meta", markup=False)
            with Grid(classes="decision-controls"):
                yield Button("核对并发送", id="decision-continue", variant="primary", disabled=self.readonly)
                yield Button("确认但不发送", id="decision-saved", disabled=self.readonly)
                yield Button("保存草稿", id="decision-draft", disabled=self.readonly)
                yield Button("返回", id="decision-close")

    def on_mount(self) -> None:
        overview = self.query_one("#decision-overview", OptionList)
        overview.highlighted = self.selected_index
        overview.focus()  # Keep arrows on the question list, including home-row entry.
        for index in range(len(self.specs)):
            self.update_live_status(index)
        self.sync_continue()
        self._initial_edit_values = self.edit_values()

    def edit_values(self) -> tuple[tuple[str | None, str], ...]:
        values: list[tuple[str | None, str]] = []
        for control in self.query("Input, Select, TextArea"):
            if isinstance(control, TextArea):
                values.append((control.id, control.text))
            elif isinstance(control, (Input, Select)):
                values.append((control.id, str(control.value)))
        return tuple(values)

    def select_item(self, index: int) -> None:
        if not 0 <= index < len(self.specs):
            return
        self.query_one("#decision-navigation", Static).update(
            f"第 {index + 1}/{len(self.specs)} 项 · ↑↓切项" + (" · 草稿已恢复" if self.has_draft else " · 反馈已本地保存" if self.has_saved_feedback else " · Tab读缺口"))
        if index != self.selected_index:
            self.selected_index = index
            for position, section in enumerate(self.query(".decision-item")):
                section.display = position == index
            self.query_one("#decision-body", VerticalScroll).scroll_home(animate=False)

    def update_live_status(self, index: int) -> None:
        if not self.is_mounted or not 0 <= index < len(self.specs):
            return
        try:
            intent = self.query_one(f"#intent-{index}", Select).value
            self.query_one(f"#decision-technical-{index}").display = intent in {"feedback", "formal"}
            self.query_one(f"#decision-comment-{index}").display = bool(intent)
            self.query_one(f"#decision-wait-guide-{index}").display = intent == "wait"
            for name in ("recommend", "feedback"):
                self.query_one(f"#decision-{name}-{index}").display = intent == "feedback"
            question = self.business_inputs[index]
            if question:
                self.query_one(f"#decision-fact-{index}").display = intent == "fact"
                if question.kind == "choice":
                    selected = self.query_one(f"#business-choice-{index}", Select).value
                    widget = self.query_one(f"#business-detail-{index}", Input)
                    conditional = intent == "fact" and selected == "conditional"
                    widget.display = conditional
                    error = self.query_one(f"#business-error-{index}", Static)
                    error.display = conditional and not bool(widget.value.strip())
                    error.update("请写明条件；不填不能提交。" if error.display else "")
                    error.can_focus = bool(error.display)
            value = self.query_one(f"#answer-{index}", Select).value
            if intent != "feedback" or value != "request_info":
                self.owner_requests.discard(index)
            comment = self.query_one(f"#comment-{index}", TextArea).text
            length = self.query_one(f"#comment-length-{index}", Static)
            length.update(f"反馈／备注 {len(comment)}/500 字符 · " +
                          ("超出上限；完整原文保留，请精简后再核对。" if len(comment) > 500 else "可多行，不扩展授权。"))
            length.can_focus = len(comment) > 500
            length.set_class(len(comment) > 500, "error")
            status = {"": self.item_status(index) + " · 未选，本次不发送", "fact": "未确认 · 已答业务问题（非授权）",
                      "feedback": "未确认 · 反馈（非授权）", "formal": "未确认 · 方案待确认",
                      "wait": "等待材料 · 不重复发送"}.get(str(intent), "待核对")
            if intent == "feedback" and isinstance(value, str):
                status = "未确认 · " + {"request_info": "待负责人补证" if index in self.owner_requests else "待解释",
                                        "preference": "倾向（非授权）", "defer": "暂缓", "reject": "拒绝",
                                        "undecided": "已写反馈（非授权）" if comment.strip() else "反馈（非授权）"}.get(value.partition(":")[0], "待核对")
            if self.has_saved_feedback and intent == "feedback" and value == "undecided" and comment == self.initial[index].comment:
                status = "反馈仅保存 · 未发送"
            if intent == "fact" and question and question.kind == "choice":
                if selected == "unknown":
                    status = "未确认 · 暂不确定（无业务结论）"
                elif selected == "conditional" and not widget.value.strip():
                    status = "未确认 · 有条件（请写明条件）"
                elif not selected:
                    status = "未确认 · 业务事实尚未填写"
            if self.has_draft and intent == self.answer_intent(self.initial[index]):
                status = "草稿 · 未确认；" + status
            self.query_one("#decision-overview", OptionList).replace_option_prompt_at_index(index, self.item_prompt(index, status))
            self.query_one(f"#decision-live-{index}", Static).update("本项 · " + status + "；未发送。")
            self.sync_continue()
        except (NoMatches, OptionDoesNotExist):
            return  # Queued input after unmount has no decision side effects.

    def sync_continue(self) -> None:
        if not self.is_mounted:
            return
        ready = False
        answers: tuple[Answer, ...] = ()
        error = ""
        if not self.busy and not self.readonly:
            try:
                answers = self.answers()
                ready = any(a.business is not None or a.kind in {"request_info", "preference", "choice", "approve"}
                            or a.comment.strip() for a in answers)
            except (NoMatches, ValueError) as invalid:
                error = str(invalid)
        formal = any(a.kind in {"choice", "approve", "budget"} for a in answers)
        mixed = formal and any(a.business is not None or a.kind in {"request_info", "preference", "defer", "reject"}
                              or a.kind == "undecided" and a.comment.strip() for a in answers)
        button = self.query_one("#decision-continue", Button)
        active = sum(answer != Answer() for answer in answers)
        button.label = f"核对本单{active}项"
        button.disabled = not ready or mixed
        saved = self.query_one("#decision-saved", Button)
        saved.label = "确认不发送"
        saved.disabled = self.busy or self.readonly or not any(a != Answer() for a in answers)
        reason = ("正在核对，请稍候；不会重复提交。" if self.busy else "任务只读，不能提交。" if self.readonly else
                  "正式决定与事实/反馈须拆分，不能混合发送。" if mixed else error if error else
                  "请先选择本次答复类型并填写答案；未选项不会发送。")
        button.tooltip = reason if button.disabled else f"核对本单{active}项；最后确认前不会发送。"
        saved.tooltip = reason if saved.disabled else "确认本单答案，仅保存记录，不发送或启动。"
        self.query_one("#decision-flow", Static).update(
            "正式决定与事实/反馈请拆分，分别核对；旧批准不会自动复制。" if mixed else
            "填写未完成：" + error if error else
            "草稿不确认；确认不发有记录；发送须最后核对，仍保留 Pi 权限确认。" if ready else
            "未选问题不发送；旧确认不自动沿用。等待无新材料，不会重复派发。")

    def change_intent(self, index: int) -> None:
        widget = self.query_one(f"#answer-{index}", Select)
        old = self.intents[index]
        if isinstance(widget.value, str):
            self.technical_values[index, old] = widget.value
        intent = self.query_one(f"#intent-{index}", Select).value
        if not isinstance(intent, str):
            return
        self.intents[index] = intent
        choices = self.answer_options(index, intent)
        value = self.technical_values.get((index, intent), "undecided")
        widget.set_options(choices)
        widget.value = value if value in {key for _, key in choices} else "undecided"
        self.update_live_status(index)

    def on_select_changed(self, event: Select.Changed) -> None:
        for index in range(len(self.specs)):
            if event.select.id == f"intent-{index}":
                if self.is_mounted:
                    self.change_intent(index)
                break
            if event.select.id in {f"answer-{index}", f"business-choice-{index}"}:
                self.update_live_status(index)
                break

    def on_input_changed(self, event: Input.Changed) -> None:
        for index in range(len(self.specs)):
            if event.input.id in {f"business-detail-{index}", f"business-text-{index}"}:
                self.update_live_status(index)
                break

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        for index in range(len(self.specs)):
            if event.text_area.id == f"comment-{index}":
                self.update_live_status(index)
                break

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option_list.id == "decision-overview":
            self.select_item(event.option_index)
            event.stop()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id == "decision-overview":
            self.select_item(event.option_index)
            event.option_list.focus()
            event.stop()

    def on_resize(self) -> None:
        self.set_class(self.size.width < 64, "decision-narrow")
        self.set_class(self.size.height >= 32, "decision-spaced")

    def answers(self) -> tuple[Answer, ...]:
        rows = []
        for index, spec in enumerate(self.specs):
            intent = self.query_one(f"#intent-{index}", Select).value
            if intent not in {key for _, key in self.intent_options(index)}:
                raise ValueError("请逐项选择本次答复类型；正式决定须全单材料齐备。")
            if not intent:
                rows.append(Answer())
                continue  # Hidden field values are editable memory, never a new grant.
            value = self.query_one(f"#answer-{index}", Select).value
            if intent in {"feedback", "formal"} and value not in {key for _, key in self.answer_options(index, str(intent))}:
                raise ValueError("答复与本次答复类型不兼容，请分别核对。")
            kind, _, choice = str(value).partition(":") if intent in {"feedback", "formal"} else ("undecided", "", "")
            question = self.business_inputs[index]
            business = None
            if intent == "fact" and question:
                if question.kind == "choice":
                    selected = self.query_one(f"#business-choice-{index}", Select).value
                    detail = self.query_one(f"#business-detail-{index}", Input).value
                    if isinstance(selected, str) and selected:
                        business = BusinessAnswer(selected, detail.strip() if selected == "conditional" else "")
                    elif detail.strip():
                        raise ValueError("先回答业务问题，再补充条件；只写备注不会变成业务事实。")
                else:
                    value_text = self.query_one(f"#business-text-{index}", Input).value.strip()
                    if value_text:
                        business = BusinessAnswer(value_text)
            comment = self.query_one(f"#comment-{index}", TextArea).text
            if len(comment) > 500:
                raise ValueError("反馈超过 500 字，请精简后再核对；已有文字保留。")
            answer = Answer(kind, choice, None, comment, business)
            answer.check(spec, question)
            rows.append(answer)
        return tuple(rows)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "decision-close":
            self.action_back()
        elif (event.button.id or "").startswith("decision-owner-request-") and not self.busy and not self.readonly:
            for index in range(len(self.specs)):
                if event.button.id == f"decision-owner-request-{index}":
                    self.query_one(f"#intent-{index}", Select).value = "feedback"
                    self.change_intent(index)
                    self.query_one(f"#answer-{index}", Select).value = "request_info"
                    self.owner_requests.add(index)
                    self.update_live_status(index)
                    break
        elif (event.button.id or "").startswith("decision-reply-") and not self.busy and not self.readonly:
            for index in range(len(self.specs)):
                if event.button.id == f"decision-reply-{index}":
                    question = self.business_inputs[index]
                    self.query_one(f"#intent-{index}", Select).value = "fact" if question else "feedback"
                    self.change_intent(index)
                    reply = (self.query_one(f"#business-choice-{index}", Select) if question and question.kind == "choice" else
                             self.query_one(f"#business-text-{index}", Input) if question else
                             self.query_one(f"#answer-{index}", Select))
                    def focus_reply(reply=reply) -> None:
                        reply.scroll_visible(top=True, animate=False)
                        reply.focus()
                    self.call_after_refresh(focus_reply)
                    break
        elif (event.button.id or "").startswith("decision-explain-") and not self.busy and not self.readonly:
            for index in range(len(self.specs)):
                if event.button.id == f"decision-explain-{index}":
                    self.owner_requests.discard(index)
                    self.query_one(f"#intent-{index}", Select).value = "feedback"
                    self.change_intent(index)
                    self.query_one(f"#answer-{index}", Select).value = "request_info"
                    self.update_live_status(index)
                    break
        elif (event.button.id or "").startswith("decision-recommend-") and not self.busy and not self.readonly:
            for index in range(len(self.specs)):
                if event.button.id == f"decision-recommend-{index}":
                    self.query_one(f"#intent-{index}", Select).value = "feedback"
                    self.change_intent(index)
                    widget = self.query_one(f"#comment-{index}", TextArea)
                    prompt = ("请根据已核对资料给出有来源的建议（设置值或区间、单位、适用条件、风险差异、未知项及负责人是否签收）；"
                              "若依据不足，请说明缺少什么原始回执、由谁提供。不猜数值、不构成批准。")
                    value = widget.text.rstrip() + ("；" if widget.text.strip() else "") + prompt if prompt not in widget.text else widget.text
                    if len(value) > 500:
                        self.query_one("#decision-error", Static).update("反馈超过 500 字，请先精简，已有文字未改动。")
                    else:
                        widget.text = value
                        widget.focus()
                    break
        elif (event.button.id or "").startswith("decision-feedback-") and not self.busy and not self.readonly:
            for index in range(len(self.specs)):
                if event.button.id == f"decision-feedback-{index}":
                    if self.query_one(f"#comment-{index}", TextArea).text.strip():
                        self.request("feedback")
                    else:
                        self.query_one("#decision-error", Static).update("请先写本项反馈，或点击‘请给有依据的建议’。")
                    break
        elif event.button.id in {"decision-draft", "decision-saved", "decision-continue"}:
            self.request((event.button.id or "").removeprefix("decision-"))

    def action_saved(self) -> None:
        self.request("saved")

    def request(self, kind: str) -> None:
        if self.busy or self.readonly or not self.is_running or self.app.screen is not self:
            return
        try:
            answers = self.answers()
        except ValueError as error:
            self.query_one("#decision-error", Static).update(str(error))
            return
        if kind == "continue" and all(a.kind == "undecided" for a in answers) and any(a.comment.strip() for a in answers):
            kind = "feedback"  # Non-authorizing free text still needs the exact final source/target review.
        self.busy = True
        for control in self.query("Button, Input, Select, TextArea, OptionList"):
            control.disabled = True
        self.run_worker(self.request_preview(kind, answers))

    async def request_preview(self, kind: str, answers: tuple[Answer, ...]) -> None:
        try:
            await self.prepare(self, kind, answers)
        except (ValueError, OSError, StorageError) as error:
            if self.is_running:
                self.show_error(f"未提交：{error}")
        finally:
            self.busy = False
            if self.is_running:
                for control in self.query("Button, Input, Select, TextArea, OptionList"):
                    control.disabled = False
                self.sync_continue()
                self.set_processing_stage("")

    def action_back(self) -> None:
        if self.busy or self._discard_prompt_open or not self.is_running or self.app.screen is not self:
            return
        if self.readonly or self._initial_edit_values is None or self.edit_values() == self._initial_edit_values:
            self.dismiss(None)
            return
        self._discard_prompt_open = True
        def finished(discard: bool | None) -> None:
            self._discard_prompt_open = False
            if not self.is_running or self.app.screen is not self or self.busy:
                return
            if discard:
                self.dismiss(None)
            else:
                self.query_one("#decision-overview", OptionList).focus()
        self.app.push_screen(DiscardChanges(), finished)
