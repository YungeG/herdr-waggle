# /// script
# requires-python = ">=3.11"
# dependencies = ["textual==8.2.8"]
# ///
"""WAGGLE: durable tasks and explicitly joined Pi; acceptance stays human."""

import argparse
import asyncio
import json
import os
import sys
import time
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TypeVar
from uuid import uuid4

# Herdr may forward cell coordinates after Textual negotiates pixel mouse mode.
# Set the default before importing Textual; keep explicit user settings intact.
if os.environ.get("HERDR_ENV") == "1":
    os.environ.setdefault("TEXTUAL_SMOOTH_SCROLL", "0")

from rich.text import Text
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.errors import NoWidget
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    Collapsible,
    DataTable,
    Footer,
    Input,
    Markdown,
    OptionList,
    Select,
    SelectionList,
    Static,
    TextArea,
)
from textual.widgets.option_list import Option

from activity import Activity
from budget_ledger import (
    BudgetRecord,
    ledger_text,
    ledger_verification_record,
    observation_record,
    revoke_record,
    scope_record,
)
from budget_view import BudgetForm, BudgetPanel
from decision_presentation import present_decision, report_view_event
from decision_view import DecisionReview, DecisionSheet, DiscardChanges
from decisions import (
    EVIDENCE_ONLY_PREFIX,
    Answer,
    DecisionPreview,
    Target,
    business_input,
    decision_source,
    feedback_answers,
    feedback_question_key,
    prepare_decision_intent,
    preview_decisions,
    prior_answers,
    proposal_question,
    proposal_requirements,
    question_question,
    question_requirements,
    recover_feedback_draft,
    request_only_decisions,
    review_fields,
    review_requirements,
    task_evidence_question,
    task_evidence_request,
    validate_preview,
)
from delivery import (
    decision_text,
    delivery_text,
    detail_parts,
    next_round_reference,
    overview_text,
    prior_feedback_candidates,
    report_text,
    round_timeline_text,
    submission_status,
)
from dependencies import (
    REGISTRIES,
    DependencyCheck,
    OwnershipRelease,
    TaskDependency,
    check_record,
    dependency_record,
    dependency_text,
    latest_dependencies,
    operation,
    receipt_record,
    release_record,
    request_for,
)
from dependency_view import DependencyForm, DependencyPanel, DependencyRecord
from evidence import (
    EvidenceItem,
    EvidenceVerification,
    inventory_candidates,
    inventory_text,
    reference_from_candidate,
    verification_record,
)
from evidence_view import EvidenceForm, EvidenceInventory, EvidenceVerificationForm
from execution_view import connection_issue, key_updates, status_text
from failure_view import (
    RECOVERY_MARKER,
    project_failure,
    project_ownership,
    split_recovery,
)
from journal import current_report, last_progress_report, readable_event
from launch import Launch, connected_agent, create_pi
from model import (
    AGENTS,
    PROJECTS,
    RETENTIONS,
    RUNS,
    STATUSES,
    SUPPLEMENT_STATES,
    Board,
    Run,
    Task,
    sample_board,
)
from paths import DirectorySuggestions, directory_suggestions
from records import TaskRecords
from report_reader import ReportMarkdown, ReportReader, report_markdown
from runtime import Agent, BridgeError, Runtime, SessionObservation, canonical_directory
from session_adoption import adoption_confirmed, latest_adoption
from step_view import StepFlow, StepForm
from steps import STEP_STATES
from store import StorageError, Store, default_path, encode
from task_actions import (
    DecisionItem,
    PrimaryAction,
    decision_items,
    needs_next_round,
    needs_supplement,
    project_primary_action,
)
from task_context import (
    WorkContract,
    canonical,
    create_followup_context,
    has_valid_contract,
    parse_compiled_contract,
    source_base,
)

T = TypeVar("T")

COLORS = {"draft": "#8794aa", "ready": "#aebcff", "running": "#79ccee", "blocked": "#f1b778", "review": "#d0b0f5", "done": "#90d4ad"}
LANES = [("draft", "待安排"), ("ready", "待执行"), ("running", "执行 / 待处理"), ("review", "待验收"), ("done", "已验收")]
FILTERS = {"all": "全部状态", **STATUSES, "blocked": "需要处理（安排 / 阻塞 / 补充 / 核实）", "archived": "已归档", "trashed": "回收站"}


class TaskList(OptionList):
    """Narrow screens open on one tap; keyboard arrows only highlight."""
    BINDINGS = [Binding("right", "select", "查看详情", show=False)]

    def on_click(self, event: events.Click) -> None:
        event.prevent_default()  # OptionList normally activates on every click.
        event.stop()
        index = event.style.meta.get("option")
        if event.button != 1 or index is None or not 0 <= index < self.option_count:
            return
        if self.get_option_at_index(index).disabled:
            return
        self.highlighted = index
        if event.chain == 2 or (self.id == "task-list" and self.app.size.width < 100):
            self.action_select()


class TaskActions(Grid):
    """Wrap the single toolbar by its actual pane width, not the terminal width."""

    def on_resize(self, event: events.Resize) -> None:
        self.set_class(event.size.width < 88, "wrapped")
        self.set_class(event.size.width < 54, "narrow")


class TaskDetail(Vertical):
    # Override the scroll area's Left binding only inside the detail panel.
    BINDINGS = [Binding("left", "app.close_detail", "返回任务", show=False, priority=True)]


class Dialog(ModalScreen):
    BINDINGS = [Binding("escape", "cancel", "返回"), Binding("ctrl+enter", "submit", "确认", priority=True)]

    def on_resize(self, event: events.Resize) -> None:
        self.set_class(event.size.width < 60 or event.size.height < 23, "compact-dialog")

    def action_cancel(self) -> None:
        if self.is_running and self.app.screen is self:
            self.dismiss(None)

    def action_submit(self) -> None:
        if self.is_running and self.app.screen is self:
            self.dismiss(True)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id not in {"cancel", "submit"}:
            return
        # Textual also walks base-class handlers; an explicit super() must not
        # cause a second cancel/submit from the same physical button event.
        event.prevent_default()
        event.stop()
        if not self.is_running or self.app.screen is not self:
            return
        if event.button.id == "cancel":
            self.action_cancel()
        elif event.button.id == "submit":
            self.action_submit()


class Choice(Dialog):
    def __init__(self, title: str, options: dict[str, str], caption: str = "输入搜索 · ↓↑ 选择 · Enter 确认 · Esc 返回", *, simple: bool = False) -> None:
        super().__init__()
        self.title_text, self.options, self.caption, self.simple = title, options, caption, simple

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="choice-dialog intent-choice" if self.simple else "choice-dialog"):
            yield Static(self.title_text, classes="dialog-title", markup=False)
            yield Static(self.caption, classes="muted", markup=False)
            if not self.simple:
                yield Input(placeholder="搜索动作 / 名称…", id="choice-query")
            yield OptionList(id="choices", markup=False)
            yield Button("返回  Esc", id="cancel")

    def on_mount(self) -> None:
        self.fill("")
        self.query_one(OptionList if self.simple else Input).focus()

    def fill(self, query: str) -> None:
        choices = self.query_one("#choices", OptionList)
        choices.clear_options().add_options(Option(label, id=key) for key, label in self.options.items() if query.casefold() in label.casefold())
        choices.highlighted = 0 if choices.option_count else None

    @on(Input.Changed, "#choice-query")
    def search(self, event: Input.Changed) -> None:
        self.fill(event.value)

    def on_key(self, event) -> None:
        if event.key in ("down", "up") and isinstance(self.focused, Input):
            self.query_one("#choices", OptionList).focus()
            event.stop()
            event.prevent_default()

    @on(Input.Submitted, "#choice-query")
    def choose_first(self) -> None:
        self.action_submit()

    @on(OptionList.OptionSelected, "#choices")
    def choose(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def action_submit(self) -> None:
        choices = self.query_one("#choices", OptionList)
        if choices.highlighted is not None:
            self.dismiss(choices.get_option_at_index(choices.highlighted).id)


class TaskForm(Dialog):
    def __init__(self, project: str, task: Task | None = None, *, save: Callable[[str, str, str], Task], durable: bool = False, projects: dict[str, str] | None = None) -> None:
        super().__init__()
        self.project, self.selected_task = project, task
        self.save, self.durable = save, durable
        self.projects = projects if projects is not None else PROJECTS

    def compose(self) -> ComposeResult:
        task = self.selected_task
        with Vertical(id="dialog"):
            yield Static("编辑任务" if task else "捕捉一个任务", classes="dialog-title")
            yield Static("提交后保存在本地 · 未提交不保存 · 不启动 Agent" if self.durable else "只写标题即可存草稿 · 不启动 Agent · 退出不保存", classes="muted")
            with VerticalScroll(classes="form-body"):
                yield Static("标题", classes="field-label")
                yield Input(task.title if task else "", placeholder="你想完成什么？", id="task-title", max_length=300)
                yield Static("归属项目 · 创建/编辑不会启动执行", classes="field-label")
                yield Select(((name, name) for name in self.projects), value=self.project, allow_blank=False, id="task-project", disabled=bool(task and task.run in ("queued", "working", "blocked")))
                yield Static(self.projects[self.project] or "尚未选择目录", id="form-path", markup=False, classes="muted")
                yield Static("补充说明 · 可选 · Tab 离开编辑区", classes="field-label")
                yield TextArea(task.description if task else "", id="task-description", placeholder="背景、验收要求，或者先留空…")
                yield Static("", id="form-error", classes="error", markup=False)
            with Horizontal(classes="dialog-buttons"):
                yield Button("返回  Esc", id="cancel")
                yield Button("保存修改" if task else "保存草稿", id="submit", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#task-title", Input).focus()

    @on(Select.Changed, "#task-project")
    def project_changed(self, event: Select.Changed) -> None:
        self.query_one("#form-path", Static).update(self.projects.get(str(event.value), "") or "尚未选择目录")

    @on(Input.Submitted, "#task-title")
    def save_title(self) -> None:
        self.action_submit()

    def action_submit(self) -> None:
        title = self.query_one("#task-title", Input).value.strip()
        if not title:
            self.query_one("#form-error", Static).update("先写一个标题，其他内容可以稍后补。")
            return
        try:
            task = self.save(title, str(self.query_one(Select).value), self.query_one(TextArea).text)
        except (ValueError, StorageError) as error:
            message = self.query_one("#form-error", Static)
            message.update(f"未保存：{error}")
            message.scroll_visible(animate=False)
            return
        self.dismiss(task)


class FollowupForm(Dialog):
    def __init__(self, source: str, *, save: Callable[[str, str, list[str]], Task]) -> None:
        super().__init__()
        self.source, self.save = source, save

    def compose(self) -> ComposeResult:
        with Vertical(id='dialog', classes='contract-dialog'):
            yield Static('基于已验收成果创建后续任务', classes='dialog-title')
            with VerticalScroll(classes='form-body'):
                yield Static(self.source, markup=False, classes='muted')
                yield Static('新任务标题 · 不改动原任务', classes='field-label')
                yield Input(id='followup-title', max_length=300)
                yield Static('独立目标 · 旧验收不是新许可', classes='field-label')
                yield TextArea(id='followup-goal', classes='contract-input')
                yield Static('明确选择引用数据 · 不含聊天或外部目录', classes='field-label')
                yield SelectionList(('成果摘要（执行者自报）', 'summary', False),
                                    ('核验依据（自报，非独立核验）', 'checks', False),
                                    ('原始说明（历史范围，非新许可）', 'description', False), id='followup-refs')
                yield Static('', id='form-error', classes='error', markup=False)
            with Horizontal(classes='dialog-buttons'):
                yield Button('返回', id='cancel')
                yield Button('仅创建草稿', id='submit', variant='primary')

    def on_mount(self) -> None:
        self.query_one('#followup-title', Input).focus()

    def action_submit(self) -> None:
        try:
            task = self.save(self.query_one('#followup-title', Input).value,
                             self.query_one('#followup-goal', TextArea).text,
                             list(self.query_one('#followup-refs', SelectionList).selected))
        except (ValueError, StorageError) as error:
            self.query_one('#form-error', Static).update(f'未创建：{error}')
            self.query_one('#form-error', Static).scroll_visible(animate=False)
            return
        self.dismiss(task)


class WorkContractForm(Dialog):
    def __init__(self, task: Task, *, save: Callable[[WorkContract], None]) -> None:
        super().__init__()
        self.selected_task, self.save = task, save
        self.initial = parse_compiled_contract(source_base(task.remaining).partition(RECOVERY_MARKER)[0])

    def compose(self) -> ComposeResult:
        with Vertical(id='dialog', classes='contract-dialog'):
            yield Static(f'D{self.selected_task.id:03} · 明确本轮工作合同', classes='dialog-title', markup=False)
            with VerticalScroll(classes='form-body'):
                yield Static('保存不派发。核对后仍须最后确认；不会继承旧实施、目录或预算授权。', classes='muted')
                yield Static('本轮目的 · 请明确选择', classes='field-label')
                yield Select((('独立任务：成果盘点', 'inventory'), ('明确核查 / 实施', 'implementation'),
                              ('原任务：仅补证（不能报告整任务 full）', 'evidence')),
                             value=self.initial.purpose if self.initial else Select.NULL, id='contract-purpose')
                for key, label in (('deliverable', '交付物'), ('allowed_scope', '允许的范围'),
                                   ('verification', '验收依据'), ('exclusions', '排除项 / 停止边界')):
                    yield Static(label + ' · 必填，最多 1800 字', classes='field-label')
                    yield TextArea(getattr(self.initial, key) if self.initial else '', id='contract-' + key,
                                   classes='contract-input')
                yield Static('仅补证是请求边界，不是工具硬只读沙箱。未知预算不产生资源批准。', classes='muted')
                yield Static('', id='form-error', classes='error', markup=False)
            with Horizontal(classes='dialog-buttons'):
                yield Button('返回', id='cancel')
                yield Button('仅保存', id='contract-save')
                yield Button('保存并核对', id='submit', variant='primary')

    def on_mount(self) -> None:
        self.query_one('#contract-purpose', Select).focus()

    @on(Button.Pressed, '#contract-save')
    def save_only(self, event: Button.Pressed) -> None:
        event.stop()
        self._save(False)

    def action_submit(self) -> None:
        self._save(True)

    def _save(self, preview: bool) -> None:
        try:
            contract = WorkContract(str(self.query_one('#contract-purpose', Select).value),
                                    *(self.query_one('#contract-' + key, TextArea).text for key in
                                      ('deliverable', 'allowed_scope', 'verification', 'exclusions')))
            self.save(contract)
        except (ValueError, StorageError) as error:
            self.query_one('#form-error', Static).update(f'未保存：{error}')
            self.query_one('#form-error', Static).scroll_visible(animate=False)
            return
        self.dismiss('preview' if preview else 'saved')


class RecoveryForm(Dialog):
    def __init__(self, task: Task, run: Run, status: str, *, save: Callable[[WorkContract, str, str], None]) -> None:
        super().__init__()
        self.selected_task, self.source_run, self.ownership_status, self.save = task, run, status, save

    def compose(self) -> ComposeResult:
        failure = project_failure(self.source_run)
        progress = last_progress_report(self.source_run)
        with Vertical(id='dialog', classes='contract-dialog'):
            yield Static(f'D{self.selected_task.id:03} · 准备恢复 · 仅保存', classes='dialog-title', markup=False)
            with VerticalScroll(classes='form-body'):
                yield Static(f'来源 Run：{self.source_run.id} · 修订 {self.source_run.seq}\n精确目录：{self.source_run.cwd}\n'
                             + self.ownership_status + '\n旧轮原封保留；服务恢复不是重放许可。', markup=False)
                yield Static('原始错误：\n' + failure.evidence + '\n' + failure.uncertainty, markup=False)
                yield Static('失败前最近自报（不能验收或作为正式授权来源）：\n' +
                             (progress.report.summary if progress and progress.report else '未提供真实进度报告。'), markup=False)
                for key, label in (('checkpoint', '明确选择的继续检查点'), ('remaining', '未完成工作')):
                    yield Static(label + ' · 必填，最多 1800 字；不自动复制旧要求', classes='field-label')
                    yield TextArea('', id='recovery-' + key, classes='contract-input')
                yield Static('新轮目的 · 必须重新选择', classes='field-label')
                yield Select((('明确核查 / 实施', 'implementation'), ('原任务仅补证', 'evidence')),
                             value=Select.NULL, id='contract-purpose')
                yield Static('独立成果盘点须新建任务；不能借盘点完成原实施目标。', classes='muted')
                for key, label in (('deliverable', '新交付物'), ('allowed_scope', '允许的范围'),
                                   ('verification', '验收依据'), ('exclusions', '排除项 / 停止边界')):
                    yield Static(label + ' · 必填，最多 1800 字', classes='field-label')
                    yield TextArea('', id='contract-' + key, classes='contract-input')
                yield Static('本页只生成草稿：不创建 Run、不发送、不认领聊天、不解除观测权限；工具层不承诺硬只读。', classes='muted')
                yield Static('', id='form-error', classes='error', markup=False)
            with Horizontal(classes='dialog-buttons'):
                yield Button('返回', id='cancel')
                yield Button('仅保存恢复草稿', id='submit', variant='primary')

    def on_mount(self) -> None:
        self.query_one('#recovery-checkpoint', TextArea).focus()

    def action_submit(self) -> None:
        try:
            contract = WorkContract(str(self.query_one('#contract-purpose', Select).value),
                                    *(self.query_one('#contract-' + key, TextArea).text for key in
                                      ('deliverable', 'allowed_scope', 'verification', 'exclusions')))
            self.save(contract, self.query_one('#recovery-checkpoint', TextArea).text,
                      self.query_one('#recovery-remaining', TextArea).text)
        except (ValueError, StorageError) as error:
            self.query_one('#form-error', Static).update(f'未保存：{error}')
            self.query_one('#form-error', Static).scroll_visible(animate=False)
            return
        self.dismiss(True)


class AdoptionForm(Dialog):
    """Choose a task/checkpoint in one form; consent never submits work to Pi."""
    def __init__(self, agent: Agent, tasks: dict[str, Task], default: str,
                 submit: Callable[[str, str, str, str, bool], Awaitable[bool]]) -> None:
        super().__init__()
        self.agent, self.targets, self.default, self.submit = agent, tasks, default, submit
        self.processing = False
        self.new_title, self.new_goal = agent.name[:80], ""

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="contract-dialog"):
            yield Static("纳管已有会话 · 不启动工作", classes="dialog-title")
            with VerticalScroll(classes="form-body"):
                yield Static(f"Pi：{self.agent.name}\n会话：{self.agent.session_id}\n精确目录：{self.agent.cwd}\n"
                             "同一会话重连可接续；忙碌时只等待原回合结束，不插话、不停止。", markup=False)
                yield Static("关联到哪项任务", classes="field-label")
                yield Select([(f"D{task.id:03} · {task.title}", key) for key, task in self.targets.items()]
                             + [("＋ 创建独立任务", "new")], value=self.default, allow_blank=False, id="adopt-task")
                yield Static("任务标题", classes="field-label")
                yield Input(id="adopt-title", max_length=300)
                yield Static("独立目标 · 已有任务只读，不导入整段聊天", classes="field-label")
                yield TextArea("", id="adopt-goal", classes="contract-input")
                yield Static("从哪个检查点开始 · 必填，最多1800字", classes="field-label")
                yield TextArea("", id="adopt-checkpoint", classes="contract-input")
                yield Checkbox("已核对原执行及相关子任务结束；仅接续同一会话", id="adopt-checked")
                yield Static("确认只建立关联，不创建Run、不派发、不恢复旧预算；后续明确输入/派发才开始新轮。"
                             "任务事实与历史交付不会从聊天中猜测。", classes="muted")
                yield Static("", id="form-error", classes="error", markup=False)
            with Horizontal(classes="dialog-buttons"):
                yield Button("返回", id="cancel")
                yield Button("核对并纳管（不启动）", id="submit", variant="primary")

    def on_mount(self) -> None:
        self.set_target(self.default)
        self.query_one("#adopt-checkpoint", TextArea).focus()

    @on(Select.Changed, "#adopt-task")
    def target_changed(self, event: Select.Changed) -> None:
        if self.is_mounted:
            self.set_target(str(event.value))

    def set_target(self, key: str) -> None:
        task = self.targets.get(key)
        title = self.query_one("#adopt-title", Input)
        goal = self.query_one("#adopt-goal", TextArea)
        if not title.disabled:
            self.new_title, self.new_goal = title.value or self.new_title, goal.text
        title.value, title.disabled = (task.title, True) if task else (self.new_title, False)
        goal.text, goal.read_only = (task.description, True) if task else (self.new_goal, False)
        checkbox = self.query_one("#adopt-checked", Checkbox)
        checkbox.display, checkbox.value = bool(task and task.run_id), False
        checkpoint = ""
        if task:
            recovered = split_recovery(source_base(task.remaining))
            if recovered:
                checkpoint = json.loads(recovered[1])["checkpoint"]
        self.query_one("#adopt-checkpoint", TextArea).text = checkpoint

    def action_cancel(self) -> None:
        if not self.processing:
            super().action_cancel()

    def action_submit(self) -> None:
        if not self.processing:
            self.run_worker(self._submit())

    async def _submit(self) -> None:
        self.processing = True
        try:
            result = await self.submit(str(self.query_one("#adopt-task", Select).value),
                                       self.query_one("#adopt-title", Input).value,
                                       self.query_one("#adopt-goal", TextArea).text,
                                       self.query_one("#adopt-checkpoint", TextArea).text,
                                       self.query_one("#adopt-checked", Checkbox).value)
            if result:
                self.dismiss(True)
        except (ValueError, OSError) as error:
            self.query_one("#form-error", Static).update(f"未纳管：{error}；输入保留，不派发。")
            self.query_one("#form-error", Static).scroll_visible(animate=False)
        finally:
            self.processing = False


class ProjectForm(Dialog):
    def __init__(self, save: Callable[[str, str], Awaitable[None]]) -> None:
        super().__init__()
        self.save = save
        self.saving = False
        self._path_version = 0
        self._path_changed_at = 0.0
        self._path_pending = False
        self._path_reading = False
        self._suggestions_query = ""
        self._path_matches: tuple[str, ...] = ()

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static("添加真实项目", classes="dialog-title")
            yield Static("保留精确子目录 · 只提示当前一层目录 · 不回退到 Git 根目录", classes="muted")
            with VerticalScroll(classes="form-body"):
                yield Static("项目名称", classes="field-label")
                yield Input(placeholder="例如：中文样本实验", id="project-name", max_length=80)
                yield Static("现有绝对目录（可用 ~）", classes="field-label")
                yield Input(placeholder="/home/you/project/subdirectory", id="project-path", max_length=4096)
                yield OptionList(id="path-options", markup=False, disabled=True)
                yield Static(DirectorySuggestions().message, id="path-hint", classes="muted", markup=False)
                yield Static("", id="form-error", classes="error", markup=False)
            with Horizontal(classes="dialog-buttons"):
                yield Button("返回  Esc", id="cancel")
                yield Button("验证目录并保存", id="submit", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#project-name", Input).focus()
        self.query_one("#path-options", OptionList).display = False
        self.set_interval(0.1, self.schedule_paths)

    @on(Input.Changed, "#project-path")
    def path_changed(self, event: Input.Changed) -> None:
        if not self.is_mounted:
            return
        self._path_version += 1
        self._path_changed_at = time.monotonic()
        self._path_pending = True
        self._path_matches = ()
        choices = self.query_one("#path-options", OptionList)
        if self.focused is choices:
            self.query_one("#project-path", Input).focus()
        choices.clear_options()
        choices.disabled, choices.display = True, False
        self.query_one("#path-hint", Static).update("正在匹配目录…" if event.value else DirectorySuggestions().message)

    def schedule_paths(self) -> None:
        if self._path_pending and not self._path_reading and not self.saving and time.monotonic() - self._path_changed_at >= 0.2:
            self._path_pending = False
            self._path_reading = True
            self.run_worker(self.read_paths(self.query_one("#project-path", Input).value, self._path_version))

    async def read_paths(self, value: str, version: int) -> None:
        try:
            try:
                result = await asyncio.to_thread(directory_suggestions, value)
            except (OSError, ValueError) as error:
                result = DirectorySuggestions(message=f"路径提示不可用：{error}；仍可手动输入。")
            if not self.is_mounted or version != self._path_version:
                return
            self._path_matches, self._suggestions_query = result.paths, value
            choices = self.query_one("#path-options", OptionList)
            choices.clear_options().add_options(Option(candidate, id=candidate) for candidate in result.paths)
            choices.disabled, choices.display = not bool(result.paths), bool(result.paths)
            choices.highlighted = 0 if result.paths else None
            self.query_one("#path-hint", Static).update(result.message)
            self.call_after_refresh(self.reveal_paths)
        finally:
            self._path_reading = False

    def reveal_paths(self) -> None:
        if not self.is_mounted:
            return
        field = self.query_one("#project-path", Input)
        if self.focused is field and self._path_matches and self._suggestions_query == field.value:
            self.query_one("#path-options", OptionList).scroll_visible(animate=False, immediate=True)

    def on_key(self, event: events.Key) -> None:
        choices = self.query_one("#path-options", OptionList)
        if event.key == "down" and not self.saving and self.focused is self.query_one("#project-path", Input) and choices.option_count:
            choices.focus()
            event.stop()
            event.prevent_default()

    @on(OptionList.OptionSelected, "#path-options")
    def choose_path(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        field = self.query_one("#project-path", Input)
        candidate = event.option.id
        if self.saving or self._suggestions_query != field.value or candidate not in self._path_matches:
            return
        assert candidate is not None
        field.value = candidate
        field.cursor_position = len(candidate)
        field.focus()

    def action_submit(self) -> None:
        if not self.saving:
            self.saving = True
            self.query_one("#submit", Button).disabled = True
            self.run_worker(self.submit_project())

    async def submit_project(self) -> None:
        saved = False
        try:
            await self.save(self.query_one("#project-name", Input).value, self.query_one("#project-path", Input).value)
            saved = True
        except (ValueError, StorageError, OSError) as error:
            message = self.query_one("#form-error", Static)
            message.update(f"未保存：{error}")
            message.scroll_visible(animate=False)
        finally:
            self.saving = False
            self.query_one("#submit", Button).disabled = False
        if saved:
            self.dismiss(True)

    def action_cancel(self) -> None:
        if not self.saving:
            super().action_cancel()


class NewAgentForm(Dialog):
    def __init__(self, directory: str, runtime: Runtime, create: Callable[[str, str, str], Awaitable[Launch]], ready: Callable[[Agent], None], *, task_title: str = "",
                 start_task: Callable[[Agent], Awaitable[bool]] | None = None, task_context: str = "") -> None:
        super().__init__()
        self.directory, self.runtime, self.create, self.ready = directory, runtime, create, ready
        self.default_name = (task_title or f"Pi · {Path(directory).name}")[:80]
        self.launch_id = str(uuid4())
        self.launch: Launch | None = None
        self.creating = False
        self.reading = False
        self.connected = False
        self._start_task, self.task_context = start_task, task_context
        self._agent: Agent | None = None
        self.start_requested = self.start_attempted = self.starting = self.confirming = False
        self._creation_closed = False

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static("新建 Pi · 独立会话", classes="dialog-title")
            with VerticalScroll(classes="form-body"):
                yield Static(f"精确目录：{self.directory}\n\n新建独立 Herdr 标签页和 Pi 会话，不接管已有会话。\n仅新建/分配不派发任务；如选择新建并启动，需先确认任务范围。\n项目与工具权限仍在 Pi 窗口处理，不自动批准。", markup=False)
                yield Static("名称", classes="field-label")
                yield Input(self.default_name, id="agent-name", max_length=80)
                yield Static("模型 · 可选", classes="field-label")
                yield Input(placeholder="留空使用 Pi 当前配置，或 provider/model", id="agent-model", max_length=200)
                hint = "可在此新建并启动本任务；Ctrl+Enter 仅新建并连接。" if self._start_task else "仅新建连接，不派发任务。"
                yield Static(hint, id="launch-result", markup=False, classes="muted")
                yield Static("", id="form-error", classes="error", markup=False)
            with Horizontal(classes="dialog-buttons"):
                if self._start_task:
                    yield Button("新建并启动", id="create-start", variant="primary")
                yield Button("仅新建并连接", id="submit", variant="default" if self._start_task else "primary")
                yield Button("返回  Esc", id="cancel")

    @property
    def _can_update(self) -> bool:
        return self.is_running and not self._creation_closed

    def on_unmount(self) -> None:
        self._creation_closed = True
        self.start_requested = False

    def on_mount(self) -> None:
        self.query_one("#agent-name", Input).focus()
        self.set_interval(0.5, self.poll_connection)

    def update_start_button(self) -> None:
        if self._start_task and self._can_update:
            button = self.query_one("#create-start", Button)
            button.label = "启动本任务" if self.connected else "新建并启动"
            button.disabled = bool(self.creating or self.starting or self.start_requested or self.start_attempted
                                   or (self.launch and self._agent is None))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "create-start":
            event.stop()
            self.confirm_start()
        else:
            super().on_button_pressed(event)

    def confirm_start(self) -> None:
        if (not self._can_update or not self._start_task or self.creating or self.starting or self.confirming or self.start_attempted
                or self.start_requested or (self.launch and self._agent is None)):
            return
        name = self.query_one("#agent-name", Input).value
        model = self.query_one("#agent-model", Input).value
        connection = f"\n连接：{self._agent.id}\n会话：{self._agent.session_id}" if self._agent else ""
        body = (f"{self.task_context}\n\n精确目录：{self.directory}\n名称：{name}\n模型：{model or 'Pi 默认配置'}{connection}\n\n"
                "确认后，连接和分配成功即保存意图并派发本任务，无需返回再按 s。\n"
                "这会执行真实工作，可能读写文件、运行命令和使用模型额度；Pi 的权限确认不变。\n"
                "等待连接时关闭此窗口会取消尚未发生的派发，不退出已创建的 Pi。")
        self.confirming = True
        def confirmed(yes) -> None:
            self.confirming = False
            if not yes or not self._can_update:
                return
            if (name, model) != (self.query_one("#agent-name", Input).value, self.query_one("#agent-model", Input).value):
                self.query_one("#form-error", Static).update("名称或模型已变化，请重新确认。")
                return
            self.start_requested = True
            self.update_start_button()
            if self.connected:
                self.run_worker(self.start_connected())
            else:
                self.action_submit()
        self.app.push_screen(Confirm("启动本任务" if self.connected else "新建 Pi 并启动任务", body,
                                     "确认启动" if self.connected else "新建并启动"), confirmed)

    def action_submit(self) -> None:
        if self.starting or self.confirming or self._creation_closed or (self.start_requested and self.connected):
            return
        if self.connected:
            self.start_requested = False
            self._creation_closed = True
            self.dismiss(self.launch)
        elif self.launch:
            self.poll_connection()
        elif not self.creating:
            self.creating = True
            self.query_one("#submit", Button).disabled = True
            self.update_start_button()
            self.run_worker(self.submit_creation())

    async def submit_creation(self) -> None:
        if not self._can_update:
            self.creating = self.start_requested = False
            return
        try:
            self.launch = await self.create(self.query_one("#agent-name", Input).value,
                                            self.query_one("#agent-model", Input).value, self.launch_id)
            if not self._can_update:
                return
            self.query_one("#agent-name", Input).disabled = True
            self.query_one("#agent-model", Input).disabled = True
            self.query_one("#submit", Button).label = "刷新连接"
            result = self.query_one("#launch-result", Static)
            pending = "\n已确认连接后启动本任务；关闭此窗口可取消待派发。" if self.start_requested else ""
            result.update(f"{self.launch.message}\n标签页：{self.launch.tab_id or '尚未确认'}\n标识：{self.launch.id}\n若等待授权，请到新 Pi 标签页处理。返回不退出 Pi，也不会重新创建。{pending}")
            result.scroll_visible(animate=False)
        except (OSError, ValueError) as error:
            self.start_requested = False
            if self._can_update:
                self.query_one("#form-error", Static).update(f"未创建：{error}\n创建标识：{self.launch_id}")
        finally:
            self.creating = False
            if self._can_update:
                self.query_one("#submit", Button).disabled = False
                self.update_start_button()
        self.poll_connection()

    def poll_connection(self) -> None:
        if self.launch and not self.connected and not self.reading and self._can_update:
            self.reading = True
            self.run_worker(self.read_connection())

    async def read_connection(self) -> None:
        try:
            assert self.launch is not None
            agent = await asyncio.to_thread(connected_agent, self.runtime, self.launch)
            if agent and self._can_update:
                self.connected = True
                self.query_one("#submit", Button).label = "返回任务"
                self.query_one("#launch-result", Static).update(f"Pi 已连接：{agent.label}\n目录：{agent.cwd}\n尚未派发任务；可在此启动本任务，或返回后按 s 明确派发。" if self._start_task else f"Pi 已连接：{agent.label}\n目录：{agent.cwd}\n仅新建连接，未派发任务。")
                try:
                    self.ready(agent)
                    self._agent = agent
                except (ValueError, StorageError) as error:
                    self.start_requested = False
                    self.query_one("#form-error", Static).update(f"Pi 已连接，但分配未保存：{error}。返回后按 a 重新选择。")
                self.update_start_button()
                if self.start_requested:
                    await self.start_connected()
        except (ValueError, OSError) as error:
            if self._can_update:
                self.query_one("#form-error", Static).update(f"连接未确认：{error}；只读取结果，不重新启动。")
        finally:
            self.reading = False

    async def start_connected(self) -> None:
        if (not self.start_requested or self.start_attempted or not self._start_task
                or self._agent is None or not self._can_update):
            return
        self.start_requested = False
        self.start_attempted = self.starting = True
        self.query_one("#submit", Button).disabled = True
        self.query_one("#cancel", Button).disabled = True
        self.update_start_button()
        self.query_one("#launch-result", Static).update("分配已保存，正在核对连接并派发本任务…")
        try:
            dispatched = await self._start_task(self._agent)
            if self._can_update:
                if dispatched:
                    self._creation_closed = True
                    self.dismiss(self.launch)
                else:
                    self.query_one("#form-error", Static).update("派发未确认；请返回查看当前轮次及记录，不会自动重试或重新创建 Pi。")
        except (ValueError, OSError, StorageError) as error:
            if self._can_update:
                self.query_one("#form-error", Static).update(f"未启动任务：{error}；请返回核对，不会自动重试。")
        finally:
            self.starting = False
            if self._can_update:
                self.query_one("#submit", Button).disabled = False
                self.query_one("#cancel", Button).disabled = False
                self.update_start_button()

    def action_cancel(self) -> None:
        if not self._creation_closed and not self.creating and not self.starting and not self.confirming:
            self.start_requested = False
            self._creation_closed = True
            self.dismiss(self.launch)


class Confirm(Dialog):
    def __init__(self, title: str, body: str, label: str = "确认") -> None:
        super().__init__()
        self.title_text, self.body, self.label = title, body, label

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(self.title_text, classes="dialog-title", markup=False)
            with VerticalScroll(classes="form-body"):
                yield Static(self.body, markup=False)
            with Horizontal(classes="dialog-buttons"):
                yield Button("返回  Esc", id="cancel")
                yield Button(self.label, id="submit", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#cancel", Button).focus()


def feedback_source_label(board: Board, task_id: int, key: str, index: int, comment: str) -> str:
    """Legacy wire bytes for one immutable same-scope human-feedback source."""
    record = board.decisions[key.split(":")[0]]
    spec = record.specs[index - 1]
    return (f"问题：{spec.question}\n旧答复：{comment}\n来源：D{task_id:03} · "
            f"报告 {record.preview.source.event_id} / {record.preview.source.seq} · {record.confirmed_at}\n"
            f"状态：{'已确认仅保存，未发送' if record.intent.kind == 'saved' else '已提交意图，结果待核实'}；默认不选，不是许可")


def feedback_review_cards(board: Board, task_id: int, rows: tuple[tuple[str, int, str], ...]) -> tuple[tuple[str, str, str], ...]:
    """Display exact selected questions and prior HUMAN feedback; never guess reply targets."""
    cards = []
    for key, index, comment in rows:
        record_id, _, _ = key.partition(":")
        record = board.decisions.get(record_id)
        if (record is None or record.task_id != task_id or type(index) is not int
                or not 1 <= index <= len(record.specs) or key != f"{record.id}:{index}"
                or not record.confirmed_at or comment != record.preview.answers[index - 1].comment):
            raise ValueError("旧问题的任务、项序或反馈来源无法核对；不会猜测回复对象。")
        status = ("此前确认仅保存，未发送" if record.intent.kind == "saved" else
                  "此前提交意图，接收结果待核实")
        source = (f"D{task_id:03} · {record.preview.context.title} · 报告修订{record.preview.source.seq} / 第{index}项"
                  f"\n记录时间：{record.confirmed_at}；{status}")
        cards.append((record.specs[index - 1].question, comment, source))
    return tuple(cards)

class RequestReview(Confirm):
    """Human sections and exact outgoing text from one immutable request snapshot."""

    BINDINGS = [Binding("escape", "cancel", "返回修改"),
                Binding("ctrl+enter", "final_shortcut_hint", "请明确点击发送", priority=True)]

    def __init__(self, title: str, body: str, label: str, *, purpose: str, user_text: str,
                 request: str, target_label: str, source_details: str, consequences: str, task_label: str,
                 selected_feedback: tuple[tuple[str, str, str], ...] = (), unselected_feedback: int = 0) -> None:
        super().__init__(title, body, label)
        self.purpose, self.user_text, self.request = purpose, user_text, request
        self.target_label, self.source_details, self.consequences = target_label, source_details, consequences
        if type(task_label) is not str or not task_label.strip():
            raise ValueError("核对页缺少明确的所属任务；不能猜测问题来源。")
        self.task_label = task_label
        self.selected_feedback = tuple(selected_feedback)
        if type(unselected_feedback) is not int or unselected_feedback < 0:
            raise ValueError("未选历史问题计数无效。")
        self.unselected_feedback = unselected_feedback

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="request-review"):
            yield Static(self.title_text, classes="dialog-title", markup=False)
            yield Static("本次操作：" + self.purpose, id="request-purpose", markup=False)
            with VerticalScroll(id="request-target-scroll", can_focus=True):
                yield Static(self.target_label, id="request-target", markup=False)
            with VerticalScroll(classes="form-body", id="request-review-body"):
                with Vertical(id="request-human-summary"):
                    yield Static("所属任务：" + self.task_label, id="request-task", classes="muted", markup=False)
                    count = len(self.selected_feedback)
                    yield Static(f"明确关联 {count} 项已有问题；下面逐项核对" if count else
                                 "独立新追问（未关联旧问题）" if self.purpose == "追问已有材料" else
                                 "独立新要求（未关联旧问题）", id="request-question-context", classes="request-heading", markup=False)
                    yield Static(f"本次明确选中 {count} 项旧问题；其余 {self.unselected_feedback} 项不发送",
                                 id="request-selection-status", classes="request-heading", markup=False)
                    if count:
                        for index, (question, comment, source) in enumerate(self.selected_feedback, 1):
                            with Vertical(id=f"request-card-{index}", classes="request-reply-card"):
                                yield Static(f"针对的问题 {index}/{count}：{question}\n问题来源：{source}",
                                             id=f"request-history-{index}", classes="request-history", markup=False)
                                label = ("本次共同追问（同一段文字，非逐项独立答复）" if count > 1 else
                                         "本次针对上述问题的追问") if self.purpose == "追问已有材料" else (
                                             "本次共同补充要求（同一段文字，非逐项独立答复）" if count > 1 else
                                             "本次针对上述问题的补充要求")
                                yield Static(label, id=f"request-reply-label-{index}", classes="request-heading", markup=False)
                                yield Static(self.user_text, id="request-user-text" if index == 1 else f"request-reply-text-{index}", markup=False)
                                with Collapsible(title="此前保存的用户反馈 · 只读参考", collapsed=True, id=f"request-prior-details-{index}"):
                                    yield Static("此前保存的用户反馈：" + comment,
                                                 id=f"request-prior-feedback-{index}", markup=False)
                    else:
                        yield Static("本次新问题／要求", classes="request-heading", markup=False)
                        yield Static(self.user_text, id="request-user-text", markup=False)
                        yield Static("未选择旧问题；不会把旧问题自动加入本次请求。", markup=False)
                    yield Static("确认后的后果", classes="request-heading", markup=False)
                    yield Static(self.consequences, id="request-consequences", markup=False)
                with Collapsible(title="完整发送文本 · 技术详情，可复制", collapsed=True, id="request-raw-details"):
                    yield TextArea(self.request, read_only=True, id="request-raw")
                with Collapsible(title="完整连接、会话与来源 · 按需核对", collapsed=True, id="request-source-details"):
                    yield Static(self.source_details, markup=False)
            yield Static("费用未知，可能消耗模型额度；Pi 权限仍须核对。\n发布不等于收到或完成；工具层不承诺硬只读。",
                         id="request-risk", classes="muted", markup=False)
            yield Static("默认返回修改；最后发送请明确选择按钮。", id="request-shortcut-hint", classes="muted", markup=False)
            with Horizontal(classes="dialog-buttons"):
                yield Button("返回修改", id="cancel")
                yield Button(self.label, id="submit", variant="primary")

    def action_final_shortcut_hint(self) -> None:
        if self.is_running and self.app.screen is self:
            self.query_one("#request-shortcut-hint", Static).update("尚未发送；请明确点击发送按钮，或将焦点移到按钮后按 Enter。")


class Note(Dialog):
    def __init__(self, title: str, placeholder: str, *, save: Callable[[str], None] | None = None, live: bool = False,
                 initial: str = "", caption: str | None = None, submit_label: str = "提交说明",
                 async_save: Callable[[str], Awaitable[None]] | None = None, confirmation: str = "") -> None:
        super().__init__()
        self.title_text, self.placeholder, self.save = title, placeholder, save
        self.live, self.initial, self.caption, self.submit_label = live, initial, caption, submit_label
        self.async_save, self.confirmation = async_save, confirmation
        self._saving = self._confirming = False

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="note-dialog"):
            yield Static(self.title_text, classes="dialog-title", markup=False)
            with VerticalScroll(id="note-scope", can_focus=True):
                yield Static((self.caption or ("只保存说明，不自动执行" if self.live else "执行仍为模拟")) + " · Ctrl+Enter 提交 · Esc 取消", classes="muted", markup=False)
                stage = Static("", id="note-stage", classes="muted", markup=False)
                stage.display = False
                yield stage
                error = Static("", id="form-error", classes="error", markup=False)
                error.can_focus = True
                error.display = False
                yield error
            yield TextArea(self.initial, placeholder=self.placeholder, id="note")
            with Horizontal(classes="dialog-buttons"):
                yield Button(self.submit_label, id="submit", variant="primary")
                yield Button("返回  Esc", id="cancel")

    def on_mount(self) -> None:
        self.query_one(TextArea).focus()

    def set_processing_stage(self, message: str) -> None:
        if not self.is_mounted or not self.is_running or self.app.screen is not self:
            return
        stage = self.query_one("#note-stage", Static)
        stage.update(message)
        stage.display = bool(message)
        if message:
            self.query_one("#form-error", Static).update("")
            self.query_one("#form-error", Static).display = False
            stage.scroll_visible(top=True, animate=False)

    def show_form_error(self, message: str) -> None:
        if not self.is_mounted or not self.is_running or self.app.screen is not self:
            return
        error = self.query_one("#form-error", Static)
        error.display = bool(message)
        error.update(message)
        if message:
            error.scroll_visible(top=True, animate=False)
            error.focus()

    def action_submit(self) -> None:
        if self._saving or self._confirming:
            return
        text = self.query_one(TextArea).text.strip()
        if not text:
            self.show_form_error("请填写原因，不能静默跳过。")
            self.query_one(TextArea).focus()
            return
        if self.confirmation:
            self._confirming = True
            def confirmed(yes) -> None:
                self._confirming = False
                if yes:
                    self._save(text)
                else:
                    self.query_one(TextArea).focus()
            self.app.push_screen(Confirm("发送到当前执行 · 请核对", self.confirmation + "\n\n补充内容：\n" + text, "发送到当前执行"), confirmed)
        else:
            self._save(text)

    def _save(self, text: str) -> None:
        if self.async_save:
            self._saving = True
            self.query_one("#submit", Button).disabled = True
            self.query_one("#cancel", Button).disabled = True
            self.set_processing_stage("核对本次提交（尚未确认结果）")
            self.run_worker(self._save_async(text))
            return
        try:
            if self.save:
                self.save(text)
        except (ValueError, StorageError) as error:
            self.show_form_error(f"未保存：{error}")
            return
        self.dismiss(text)

    async def _save_async(self, text: str) -> None:
        try:
            if self.async_save:
                await self.async_save(text)
        except (ValueError, OSError, StorageError) as error:
            if self.is_running:
                self.show_form_error(f"未保存：{error}")
        else:
            if self.is_running:
                self.dismiss(text)
        finally:
            self._saving = False
            if self.is_running:
                self.query_one("#submit", Button).disabled = False
                self.query_one("#cancel", Button).disabled = False
                self.set_processing_stage("")

    def action_cancel(self) -> None:
        if not self._saving:
            super().action_cancel()


@dataclass(frozen=True)
class InputCapacity:
    """Display-only measurement from the same source-aware request compiler."""
    input_chars: int
    input_limit: int
    history_chars: int = 0
    request_chars: int | None = None
    remaining_chars: int | None = None
    error: str = ""


class DispatchNote(Note):
    """A visible execution scope makes the primary submission explicit consent."""

    def __init__(self, title: str, placeholder: str, *, scope: str, initial: str,
                 save: Callable[[str], None], submit: Callable[[str], Awaitable[None]],
                 reference_hint: str = "", review_inputs: tuple[str, str] | None = None,
                 feedback_options: tuple[tuple[str, str], ...] = (), initial_feedback: tuple[str, ...] = (),
                 feedback_titles: tuple[tuple[str, str], ...] = (), feedback_details: str = "",
                 input_capacity: Callable[[str], InputCapacity] | None = None, target_hint: str = "") -> None:
        super().__init__(title, placeholder, save=save, live=True, initial=initial,
                         caption="主提交先核对后派发；仅保存不会执行",
                         submit_label="提交并核对", async_save=submit)
        self.scope = scope
        self.reference_hint = reference_hint
        self.review_inputs, self.feedback_options = review_inputs, feedback_options
        available_keys = {key for key, _ in feedback_options}
        if len(set(initial_feedback)) != len(initial_feedback) or not set(initial_feedback).issubset(available_keys):
            raise ValueError("已保存旧问题的来源无法核对；不能恢复选择。")
        self.initial_feedback = initial_feedback
        self.feedback_titles = dict(feedback_titles)
        if len(self.feedback_titles) != len(feedback_titles) or not set(self.feedback_titles).issubset(available_keys):
            raise ValueError("历史问题显示来源不匹配；不能猜测问题。")
        self.feedback_details = feedback_details
        self.target_hint = target_hint
        self.input_capacity = input_capacity
        self._input_error = ""
        self._form_error = ""
        self.start_requested = False

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="note-dialog dispatch-note" + (" review-note" if self.review_inputs is not None else "") + (" proposal-note" if self.feedback_options else "")):
            yield Static(self.title_text, classes="dialog-title", markup=False)
            if self.target_hint:
                yield Static(self.target_hint, id="note-target", markup=False)
            with VerticalScroll(id="note-scope", can_focus=True):
                stage = Static("", id="note-stage", classes="muted", markup=False)
                stage.display = False
                yield stage
                validation = Static("", id="note-validation", classes="error", markup=False)
                validation.can_focus = True
                yield validation
                yield Static(self.scope, id="note-scope-text", markup=False)
                if self.reference_hint:
                    yield Static(self.reference_hint, id="note-reference", markup=False)
                yield Static("主按钮先核对再派发；仅保存不会执行。Ctrl+Enter 核对 · Esc 取消",
                             id="note-submit-hint", classes="muted")
                if self.feedback_details:
                    with Collapsible(title="旧问题全文、此前用户反馈与来源", collapsed=True):
                        yield Static(self.feedback_details, id="feedback-source-text", markup=False)
            if self.feedback_options:
                yield Static("旧问题 · 默认不选；空格勾选", classes="feedback-list-hint", markup=False)
                yield SelectionList[str](*[(self.feedback_titles.get(key, label), key, key in self.initial_feedback)
                                          for key, label in self.feedback_options], id="prior-feedback")
                yield Static(f"已选 {len(self.initial_feedback)}/{len(self.feedback_options)} 项；其余不发送",
                             id="selected-feedback-summary", markup=False)
            yield TextArea(self.initial, placeholder=self.placeholder, id="note")
            yield Static("", id="note-length", classes="muted", markup=False)
            if self.review_inputs is not None:
                yield Static("本轮允许的范围 · 必填", id="review-scope-length", classes="field-label review-field", markup=False)
                yield Input(self.review_inputs[0], placeholder="精确目录 / 文件与允许的操作…", id="review-scope", classes="review-input")
                yield Static("验收依据 · 必填", id="review-verification-length", classes="field-label review-field", markup=False)
                yield Input(self.review_inputs[1], placeholder="需要核对的原件或测试结果…", id="review-verification", classes="review-input")
            error = Static("", id="form-error", classes="error", markup=False)
            error.can_focus = True
            error.display = False  # Full reasons live in note-validation; retain compatibility content only.
            yield error
            with Horizontal(classes="dialog-buttons"):
                submit = Button(self.submit_label, id="submit", variant="primary")
                submit.active_effect_duration = 0  # A corrected form must accept the next click immediately.
                yield submit
                save_only = Button("仅保存", id="save-only")
                save_only.active_effect_duration = 0
                yield save_only
                yield Button("返回  Esc", id="cancel")

    def on_mount(self) -> None:
        super().on_mount()
        self.update_selected_summary()
        self.update_input_capacity()

    def update_input_capacity(self) -> None:
        if not self.is_mounted or not self.is_running or self.app.screen is not self or self._saving:
            return
        text = self.query_one("#note", TextArea).text
        state = self.input_capacity(text) if self.input_capacity else InputCapacity(len(text.strip()), 6000)
        compact = self.has_class("note-compact")
        request = (("请求" if compact else "完整请求 ") + f"{state.request_chars}/6000"
                   if state.request_chars is not None else "请求待核对" if compact else "完整请求暂不可核对")
        remaining = ((" · 余" if compact else " · 可再写 ") + str(state.remaining_chars)
                     if state.remaining_chars is not None else "")
        input_line = (f"输入{state.input_chars}/{state.input_limit} · 旧引用{state.history_chars}" if compact else
                      f"输入 {state.input_chars}/{state.input_limit} 字符 · 旧问题占用 {state.history_chars}")
        self.query_one("#note-length", Static).update(input_line + "\n" + request + remaining)
        self._input_error = state.error
        validation = self.query_one("#note-validation", Static)
        reason = state.error or self._form_error
        validation.update(reason)
        validation.display = bool(reason and (text.strip() or self._form_error))
        self.query_one("#submit", Button).tooltip = "查看不可核对原因：" + state.error if state.error else "先核对本次内容与后果"
        if self.review_inputs is not None:
            for field_id, name, short in (("review-scope", "允许的范围", "范围"),
                                          ("review-verification", "验收依据", "验收")):
                widget = self.query_one("#" + field_id, Input)
                value = widget.value
                reason = (f"请填写本轮{name}；不能只写‘继续’。" if not value.strip() else
                          f"本轮{name}最多1800字；请缩短后再核对。" if len(value) > 1800 else "")
                title = short if compact else "本轮" + name
                label = self.query_one("#" + field_id + "-length", Static)
                label.update(f"{title} · 必填 {len(value)}/1800" + (" · 超出" if len(value) > 1800 else ""))
                label.set_class(bool(reason), "error")
                widget.tooltip = reason or f"本轮{name}，最多1800字；不截断范围或验收依据。"

    def set_processing_stage(self, message: str) -> None:
        if not self.is_mounted or not self.is_running or self.app.screen is not self:
            return
        stage = self.query_one("#note-stage", Static)
        stage.update(message)
        stage.display = bool(message)
        if message:
            self._form_error = ""
            validation = self.query_one("#note-validation", Static)
            validation.update("")
            validation.display = False
            stage.scroll_visible(top=True, animate=False)

    def show_form_error(self, message: str) -> None:
        if not self.is_mounted or not self.is_running or self.app.screen is not self:
            return
        error = self.query_one("#form-error", Static)
        self._form_error = message
        error.update(message)
        error.display = False
        reason = self.query_one("#note-validation", Static)
        reason.update(message)
        reason.display = bool(message)
        if message:
            reason.scroll_visible(top=True, animate=False)
            reason.focus()

    def show_input_error(self) -> None:
        self.show_form_error("未保存：" + self._input_error)

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area.id == "note":
            self._form_error = ""
            self.update_input_capacity()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id in {"review-scope", "review-verification"}:
            self._form_error = ""
            self.update_input_capacity()

    def update_selected_summary(self) -> None:
        if not self.is_mounted or not self.feedback_options:
            return
        selected = set(self.query_one("#prior-feedback", SelectionList).selected)
        names = [self.feedback_titles.get(key, "历史问题") for key, _ in self.feedback_options if key in selected]
        text = f"已选 {len(selected)}/{len(self.feedback_options)} 项；其余不发送" + (" · " + "；".join(names) if names else "")
        self.query_one("#selected-feedback-summary", Static).update(text)

    def on_selection_list_selected_changed(self, event: SelectionList.SelectedChanged) -> None:
        if event.selection_list.id == "prior-feedback":
            self._form_error = ""
            self.update_selected_summary()
            self.update_input_capacity()

    def on_resize(self) -> None:
        compact = self.size.width < 64 or self.size.height <= 20
        self.set_class(compact, "note-compact")
        if self.is_mounted:
            self.query_one("#note-submit-hint", Static).update(
                "先核对后派发；仅保存不执行。" if compact else
                "主按钮先核对再派发；仅保存不会执行。Ctrl+Enter 核对 · Esc 取消")
            self.query_one("#submit", Button).label = "提交并核对" if compact else self.submit_label
            self.query_one("#cancel", Button).label = "关闭" if compact else "关闭 · 未保存"
            self.update_input_capacity()

    def action_submit(self) -> None:
        if not self.is_running or self.app.screen is not self or self._saving or self._confirming:
            return
        self.update_input_capacity()
        if self.input_capacity and self._input_error:
            self.show_input_error()
            return
        super().action_submit()
        if self._saving:
            self.query_one("#save-only", Button).disabled = True
            self.query_one(TextArea).read_only = True

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save-only":
            event.stop()
            if self._saving or not self.is_running or self.app.screen is not self:
                return
            self.update_input_capacity()
            if self.input_capacity and self._input_error:
                self.show_input_error()
                return
            text = self.query_one(TextArea).text.strip()
            if not text:
                self.show_form_error("请填写补充说明。")
                return
            try:
                self.set_processing_stage("保存本地草稿（不发送）")
                if self.save:
                    self.save(text)
            except (ValueError, OSError, StorageError) as error:
                self.show_form_error(f"未保存：{error}")
            else:
                self.dismiss(text)
            finally:
                self.set_processing_stage("")
        else:
            super().on_button_pressed(event)

    def action_cancel(self) -> None:
        if self._saving or self._confirming or not self.is_running or self.app.screen is not self:
            return
        selected = set(self.query_one("#prior-feedback", SelectionList).selected) if self.feedback_options else set()
        review = ((self.query_one("#review-scope", Input).value, self.query_one("#review-verification", Input).value)
                  if self.review_inputs is not None else None)
        dirty = (self.query_one("#note", TextArea).text != self.initial or selected != set(self.initial_feedback)
                 or review != self.review_inputs)
        if not dirty:
            super().action_cancel()
            return
        self._confirming = True
        def finished(discard: bool | None) -> None:
            self._confirming = False
            if not self.is_running or self.app.screen is not self or self._saving:
                return
            if discard:
                super(DispatchNote, self).action_cancel()
            else:
                self.query_one("#note", TextArea).focus()
        self.app.push_screen(DiscardChanges(), finished)

    async def _save_async(self, text: str) -> None:
        self.query_one("#save-only", Button).disabled = True
        self.query_one("#cancel", Button).disabled = True
        self.query_one(TextArea).read_only = True
        self.set_processing_stage("核对来源与输入（尚未保存、未发送）")
        try:
            await super()._save_async(text)
        finally:
            if self.is_running and self.app.screen is self:
                self.query_one("#save-only", Button).disabled = False
                self.query_one("#cancel", Button).disabled = False
                self.query_one(TextArea).read_only = False
                self.set_processing_stage("")


class Waggle(App):
    TITLE = "WAGGLE · 假数据交互原型"
    CSS_PATH = "style.tcss"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("n", "new_task", "新建"), Binding("slash", "search", "搜索"),
        Binding("v", "view", "列表/看板"), Binding("ctrl+k", "palette", "动作", priority=True),
        Binding("escape", "back", "返回"), Binding("ctrl+q", "quit", "退出", priority=True),
        Binding("p", "project", "项目", show=False), Binding("f", "filter", "状态", show=False),
        Binding("e", "edit_task", "编辑", show=False), Binding("a", "assign", "分配", show=False),
        Binding("s", "start", "显式启动", show=False), Binding("g", "simulate", "进展", show=False),
        Binding("x", "stop", "停止", show=False),
        Binding("r", "review", "人工验收", show=False), Binding("b", "return_work", "补充说明", show=False),
        Binding("d", "decisions", "逐项决定", show=False),
        Binding("question_mark", "help", "帮助", show=False),
    ]

    def __init__(self, board: Board | None = None, *, store: Store | None = None, runtime: Runtime | None = None,
                 observations: dict[str, Path] | None = None, task_observations: dict[str, Path] | None = None) -> None:
        super().__init__()
        self.store = store
        self.board = store.load() if store is not None else board if board is not None else sample_board()
        self.runtime = (runtime or Runtime()) if self.board.live or any(task.execution == "pi" for task in self.board.tasks.values()) else None
        for run_id, directory in (observations or {}).items():
            run = self.board.runs.get(run_id)
            task = self.board.tasks.get(run.task_id) if run else None
            if not self.runtime or not run or not task or task.run == "none" or task.run_id != run_id:
                raise BridgeError("只读观测必须指定本库任务的当前 Run ID；不会接管未知或历史轮次。")
            self.runtime.observe(run, directory)
        for anchor_id, directory in (task_observations or {}).items():
            anchor = self.board.runs.get(anchor_id)
            task = self.board.tasks.get(anchor.task_id) if anchor else None
            current = self.board.runs.get(task.run_id) if task else None
            if not self.runtime or not anchor or not task or not current or task.run == "none" or task.status == "done" or task.retention != "active":
                raise BridgeError("任务观测须锚定本库未验收、未退回的当前任务。")
            lineage = self.board.lineage(current)
            if anchor not in lineage:
                raise BridgeError("任务观测锚点不是当前轮次的已验证祖先。")
            self.runtime.observe(anchor, directory, follow=True)
            parent = anchor
            for child in reversed(lineage[:lineage.index(anchor)]):
                self.runtime.inherit_observation(parent, child)
                parent = child
        self.agents: dict[str, Agent] = {}
        self.sessions: dict[str, SessionObservation] = {}
        self.session_errors: dict[str, str] = {}
        self.runtime_errors: dict[str, str] = {}
        self._report_capability_issues: dict[str, str] = {}
        self.supplement_errors: dict[str, str] = {}
        self.activity: Activity | None = None
        self.activity_run_id = ""
        self.activity_issue = ""
        self._polling = False
        self._sending = False
        self._published_runs: set[tuple] = set()
        self._published_supplements: set[tuple] = set()
        self._submission_stages: dict[int, str] = {}
        self._visual_dirty = False
        self._poll_save_error = ""
        self._adoption_states: dict[int, str] = {}
        self._adoption_errors: dict[int, str] = {}
        self._primary_action: PrimaryAction | None = None
        self._primary_snapshot: tuple[object, ...] | None = None
        self._primary_task_id: int | None = None
        if store is not None:
            self.title = "WAGGLE · 本地任务 · Pi 显式接入" if self.runtime else "WAGGLE · 本地任务 · 执行仍模拟"
        self.selected_id = next(iter(self.board.tasks), None)
        self.project_name = "all"
        self.status_filter = "all"
        self.view_mode = "list"
        self.detail_open = False
        self.detail_expanded = False
        self._detail_task_id: int | None = None
        self._detail_markdown_key: tuple[int, str, str] | None = None
        self._detail_markdown_generation = 0
        self.sidebar_open = False
        self._decision_rows: tuple[DecisionItem, ...] = ()
        self.viewport_width = 80
        self.theme = "textual-dark"

    def compose(self) -> ComposeResult:
        with Horizontal(id="masthead"):
            yield Button("展开导航", id="sidebar-button")
            yield Static("◈  WAGGLE", id="brand")
            yield Static(("LOCAL · 本地保存 · Pi 显式接入" if self.runtime else "LOCAL · 本地保存 · 执行模拟") if self.store else "DEMO · 仅内存 · 退出即丢弃", id="demo-badge")
            yield Button("看板  v", id="view-button")
            yield Button("动作  Ctrl+K", id="palette-button")
        with Horizontal(id="workspace"):
            with VerticalScroll(id="sidebar", can_focus=False):
                yield Static("工作队列", classes="nav-heading")
                yield OptionList(id="nav-status", markup=False)
                yield Static("项目 · 精确目录" if self.runtime else "项目 · 全部虚构", classes="nav-heading")
                yield OptionList(Option("全部项目", id="all"), *(Option(name, id=name) for name in self.board.projects), id="nav-project", markup=False)
                yield Static("本地 SQLite 保存\n可新建 / 接入 Pi\n交付由你验收" if self.runtime else "执行始终为模拟\n不接真实 Agent", id="sandbox-note")
            with Vertical(id="main"):
                yield Static("", id="context-line", markup=False)
                with Horizontal(id="toolbar"):
                    yield Input(placeholder="搜索任务、项目、执行者…  /", id="search")
                    yield Button("状态  f", id="filter-button")
                    yield Button("＋ 新建", id="new-button", variant="primary")
                with Horizontal(id="content"):
                    with Vertical(id="task-region"):
                        yield Static("", id="list-heading", markup=False)
                        yield TaskList(id="task-list", markup=False)
                        yield Static("没有匹配的任务\n试试清空搜索 / 修改项目或状态筛选，或按 n 新建。", id="empty", markup=False)
                    with Horizontal(id="board"):
                        for key, label in LANES:
                            with Vertical(classes="lane", id=f"column-{key}"):
                                yield Static(label, id=f"heading-{key}", classes="lane-heading")
                                yield TaskList(id=f"lane-{key}", markup=False)
                    with TaskDetail(id="inspector"):
                        with Vertical(id="detail-header"):
                            with Horizontal(id="detail-title-row"):
                                yield Button("← 返回", id="detail-back")
                                yield Static("", id="detail-heading", markup=False)
                                yield Button("动作", id="detail-palette")
                            yield Static("", id="detail-state", markup=False)
                        with TaskActions(id="detail-actions"):
                            with Grid(id="task-operations"):
                                yield Button("开始任务 s", id="accept-button", variant="primary")
                                yield Button("补充说明 b", id="return-button")
                            with Grid(id="task-tools"):
                                yield Button("全屏阅读", id="report-button")
                                yield Button("更多", id="more-button")
                        yield Static("", id="next-action", markup=False)
                        with VerticalScroll(id="detail-scroll", can_focus=True):
                            yield Static("", id="submission-status", markup=False)
                            yield Static("", id="decision-status", markup=False)
                            yield OptionList(id="decision-items", markup=False)
                            yield StepFlow(id="step-flow")
                            with Collapsible(title="执行与连接", collapsed=False, id="runtime-details"):
                                yield Static("", id="execution-status", markup=False)
                            yield Static("", id="detail-text", markup=False)
                            yield Static("", id="detail-markdown-heading", markup=False)
                            yield report_markdown("detail-markdown")
                            yield Static("", id="detail-tail", markup=False)
        yield Static("", id="context-hint", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self.apply_layout()
        self.refresh_tasks()
        self.focus_tasks()
        if self.runtime:
            self.set_interval(1, self.schedule_poll)
            self.schedule_poll()

    @property
    def selected_task(self) -> Task | None:
        return self.board.tasks.get(self.selected_id) if self.selected_id is not None else None

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        task = self.selected_task
        if task and task.retention != "active" and action in {"edit_task", "assign", "start", "simulate", "stop", "review", "return_work", "new_agent", "abandon"}:
            return False
        if action == "quit" or action not in {binding.action for binding in self.BINDINGS if isinstance(binding, Binding)}:
            return True  # Keep Textual's Tab/focus/accessibility actions in modals.
        if len(self.screen_stack) > 1:
            return False
        if isinstance(self.focused, (Input, TextArea, Select)):
            return action == "back"
        return True

    def task_display(self, task: Task) -> tuple[str, str, str]:
        """Project connection uncertainty into the UI, never into saved run history."""
        run = self.board.runs.get(task.run_id)
        if (task.retention == "active" and task.execution == "pi" and task.run != "none"
                and task.status != "done" and run and not run.terminal
                and (self.runtime_errors.get(run.id)
                     # pi-lens-ignore: ast-grep:unchecked-throwing-call-python
                     or connection_issue(run, self.agents.get(run.agent_id), int(time.time() * 1000)))):
            return "blocked", "执行待核实", "连接/观测异常 · 核实原 Pi"
        action = self.primary_action(task)
        if action.state:
            return "blocked", action.state, RUNS[task.run]
        if needs_supplement(task, run):
            return "blocked", "待补充需求", "本轮已结束 · 等待补充"
        if needs_next_round(task, run):
            return "blocked", "待安排下一轮", "阶段交付 · 本轮已结束"
        runtime = RUNS[task.run]
        if task.status == "done":
            runtime = "人工已验收"
        elif task.status == "review" and not task.can_accept:
            full = bool(run and run.delivery == "full")
            return "blocked", "交付条件待核对" if full else "阶段交付待处理", (
                "完整自报，补充/验收条件待核实" if full else "阶段交付，仍有剩余工作")
        return task.status, STATUSES[task.status], runtime

    def visible_tasks(self) -> list[Task]:
        query = self.query_one("#search", Input).value.casefold().strip()
        return [task for task in self.board.tasks.values()
                if (self.project_name == "all" or task.project == self.project_name)
                and (task.retention == self.status_filter if self.status_filter in {"archived", "trashed"}
                     else task.retention == "active" and (self.status_filter == "all" or self.task_display(task)[0] == self.status_filter))
                and query in f"D{task.id:03} {task.title} {task.project} {task.description} {self.agent_label(task.agent)} {task.blocker} {self.board.projects[task.project]}".casefold()]

    def card(self, task: Task, compact: bool = False, *, selected: bool = False) -> Text:
        text = Text()
        if task.retention != "active":
            text.append(RETENTIONS[task.retention] + " · ", style="#a6b3c6")
        status, label, _ = self.task_display(task)
        marker = "▶" if selected else "!" if status == "blocked" else "●"
        text.append(f"{marker} {label}", style=COLORS[status])
        text.append(f"  D{task.id:03}\n", style="#78879d")
        text.append(task.title, style="bold #dde6f3")
        text.append(f"\n{task.project}" + (" · 模拟" if task.execution == "simulation" else ""), style="#99a8bd")
        action = self.primary_action(task)
        next_step = {"核实执行": "到原 Pi 核实", "查看确认信息": "到原 Pi 处理确认",
                     "查看停止状态": "到原 Pi 核实停止", "查看派发状态": "等待接收回执"}.get(action.label, action.label) if task.execution == "pi" else action.label
        text.append(f"\n下一步 · {next_step}\n", style=COLORS[status])
        return text

    def fill_tasks(self, widget: OptionList, tasks: list[Task], compact: bool = False) -> None:
        ids = [f"task-{task.id}" for task in tasks]
        current = [widget.get_option_at_index(i).id for i in range(widget.option_count)]
        if ids == current:
            for task, option_id in zip(tasks, ids, strict=True):
                widget.replace_option_prompt(option_id, self.card(task, compact, selected=task.id == self.selected_id))
        else:
            widget.clear_options().add_options(Option(self.card(task, compact, selected=task.id == self.selected_id), id=option_id) for task, option_id in zip(tasks, ids, strict=True))
        selected = f"task-{self.selected_id}"
        widget.highlighted = ids.index(selected) if selected in ids else (0 if ids else None)

    def refresh_selection_markers(self) -> None:
        for widget in (self.query_one("#task-list", OptionList), *(self.query_one(f"#lane-{key}", OptionList) for key, _ in LANES)):
            compact = bool(widget.id and widget.id.startswith("lane-"))
            for index in range(widget.option_count):
                option = widget.get_option_at_index(index)
                # pi-lens-ignore: ast-grep:unchecked-throwing-call-python
                if option.id and (task := self.board.tasks.get(int(option.id.removeprefix("task-")))):
                    widget.replace_option_prompt(option.id, self.card(task, compact, selected=task.id == self.selected_id))

    def refresh_tasks(self) -> None:
        if self.runtime and len(self.screen_stack) > 1:
            self._visual_dirty = True
            return  # Never rebuild the underlying selection while a form is being edited.
        self._visual_dirty = False
        focused = self.focused
        follow_lane = isinstance(focused, OptionList) and bool(focused.id and focused.id.startswith("lane-"))
        tasks = self.visible_tasks()
        statuses = {task.id: self.task_display(task)[0] for task in self.board.tasks.values()}
        if not any(task.id == self.selected_id for task in tasks):
            self.selected_id = tasks[0].id if tasks else None
        narrow_board = self.view_mode == "board" and self.viewport_width < 112
        flat = sorted(tasks, key=lambda t: list(STATUSES).index(statuses[t.id])) if narrow_board else tasks
        self.fill_tasks(self.query_one("#task-list", OptionList), flat)
        for key, label in LANES:
            lane = [task for task in tasks if statuses[task.id] == key or (key == "running" and statuses[task.id] == "blocked")]
            lane_widget = self.query_one(f"#lane-{key}", OptionList)
            lane_widget.can_focus = bool(lane)
            self.fill_tasks(lane_widget, lane, compact=True)
            self.query_one(f"#heading-{key}", Static).update(f"{label}  {len(lane):02}")
        nav = self.query_one("#nav-status", OptionList)
        labels = [("all", "全部任务"), ("review", "待我验收"), ("blocked", "需要处理"), ("running", "正在执行")]
        nav.clear_options().add_options(Option(f"{label}  {sum(t.retention == 'active' and (key == 'all' or statuses[t.id] == key) for t in self.board.tasks.values())}", id=key) for key, label in labels)
        nav.highlighted = next((i for i, (key, _) in enumerate(labels) if key == self.status_filter), None)
        context = self.project_name if self.project_name != "all" else "全部项目"
        self.query_one("#context-line", Static).update(f"{context}  /  {FILTERS[self.status_filter]}  /  {len(tasks)} 个任务")
        self.query_one("#list-heading", Static).update("看板 · 窄屏按状态分组（Tab 切区） · → 详情" if narrow_board else "任务队列 · ↑↓ 选中 / → 打开")
        self.query_one("#empty").display = not tasks
        self.query_one("#task-list").display = bool(tasks)
        self.show_detail()
        if follow_lane:
            self.focus_tasks()

    def refresh_status(self) -> None:
        """Refresh observations without rebuilding the report, task list, or buttons."""
        self.refresh_primary_action()
        self.refresh_secondary_action()
        self.refresh_hint()
        widget = self.query_one("#execution-status", Static)
        task = self.selected_task
        widget.display = bool(task and task.execution == "pi")
        panel = self.query_one("#runtime-details", Collapsible)
        panel.display = widget.display
        run = self.board.runs.get(task.run_id) if task else None
        current = run if task and task.run != "none" else None
        key = (task.id if task else None, current.id if current else "", current.terminal if current else True, self.detail_expanded)
        if key != getattr(self, "_runtime_panel_key", None):
            self._runtime_panel_key = key
            panel.collapsed = not self.detail_expanded and (current is None or current.terminal)
        panel.title = "执行与连接 · " + ("本轮已结束" if current and current.terminal else "当前状态" if current else "尚未派发")
        if not task or task.execution != "pi":
            return
        if task.retention != "active":
            content = Text(f"{RETENTIONS[task.retention]} · 仅查看保留的任务与记录\n需要你 · Ctrl+K → 恢复任务后再操作", style="#a6b3c6")
            if widget.content != content:
                widget.update(content)
            return
        run = self.board.runs.get(task.run_id)
        rounds = [item.id for item in self.board.runs.values() if item.task_id == task.id]
        number = rounds.index(run.id) + 1 if run and run.id in rounds else None
        content = status_text(task, run, self.agents.get(run.agent_id) if run else None,
                              issue=self.runtime_errors.get(run.id, "") if run else "",
                              round_number=number,
                              activity=self.activity if run and self.activity_run_id == run.id else None,
                              activity_issue=self.activity_issue if run and self.activity_run_id == run.id else "",
                              session=self.sessions.get(run.id) if run else None,
                              session_issue=self.session_errors.get(run.id, "") if run else "")
        if run and task.run != "none" and task.status != "done" and self.runtime and self.runtime.observation_only(run):
            scope = "本任务只读跟随" if self.runtime.follows_task(run) else "本轮只读接入（不跟随后续轮次）"
            content.append(f"\n观测 · {scope}；派发、补充与停止不可用", style="#f1b778")
        if run and task.run != "none":
            missing = self.board.uninherited_supplements(run)
            if missing:
                content.append(f"\n补充 · 此前 {len(missing)} 条要求未纳入本轮，不能验收；请补充/退回", style="#f1b778")
            notes = self.board.run_supplements(run.id)
            if notes:
                unresolved = [note for note in notes if note.status != "covered"]
                latest = (unresolved or notes)[-1]
                label = "回执不可读，接收/覆盖未确认" if latest.id in self.supplement_errors else SUPPLEMENT_STATES[latest.status]
                content.append(f"\n补充 · {label}" + (f" · {len(unresolved)} 条未确认覆盖，不能验收" if unresolved else " · 自报覆盖，仍需人工核实"), style="#f1b778")
        if widget.content != content:
            widget.update(content)

    def update_detail_markdown(self, task: Task | None, run: Run | None, body: str) -> None:
        heading = self.query_one("#detail-markdown-heading", Static)
        markdown = self.query_one("#detail-markdown", ReportMarkdown)
        if not task or not self.detail_expanded or not body:
            self._detail_markdown_generation += 1
            self._detail_markdown_key = None
            self.workers.cancel_group(self, "detail-markdown")
            heading.display = markdown.display = False
            return
        key = (task.id, run.id if run else "", body)
        heading.update("本轮交付正文 · Markdown 渲染 · 执行者自报")
        heading.display = True
        if key == self._detail_markdown_key:
            return
        self._detail_markdown_key = key
        self._detail_markdown_generation += 1
        generation = self._detail_markdown_generation
        markdown.display = False
        self.run_worker(self.render_detail_markdown(key, generation, body), name="detail-markdown",
                        group="detail-markdown", exclusive=True, exit_on_error=False)

    async def render_detail_markdown(self, key: tuple[int, str, str], generation: int, body: str) -> None:
        task = self.selected_task
        if (not self.is_running or not self.detail_expanded or self._detail_markdown_generation != generation
                or self._detail_markdown_key != key or not task or task.id != key[0]):
            return
        markdown = self.query_one("#detail-markdown", ReportMarkdown)
        await markdown.update(body)
        task = self.selected_task
        if (self.is_running and self.detail_expanded and self._detail_markdown_generation == generation
                and self._detail_markdown_key == key and task and task.id == key[0]):
            markdown.display = True

    @on(Markdown.LinkClicked, "#detail-markdown")
    def detail_markdown_link(self, event: Markdown.LinkClicked) -> None:
        event.stop()

    def refresh_submission_status(self) -> None:
        if not self.is_mounted or not self.is_running or len(self.screen_stack) != 1:
            return
        task = self.selected_task
        result = self.query_one("#submission-status", Static)
        result.display = bool(task and task.execution == "pi")
        result.can_focus = result.display
        if task:
            text = self._submission_stages.get(task.id) or submission_status(
                self.board, task, published_runs=self._published_runs,
                published_supplements=self._published_supplements)
            if self.store is None:
                text = "仅本会话，尚未持久化 · " + text
            result.update(text)
        else:
            result.update("")

    def set_submission_stage(self, task_id: int, message: str) -> None:
        if message:
            self._submission_stages[task_id] = message
        else:
            self._submission_stages.pop(task_id, None)
        if self.selected_id == task_id:
            self.refresh_submission_status()

    def show_detail(self) -> None:
        task = self.selected_task
        detail, tail = Text(), Text()
        markdown_body = ""
        if task:
            if task.id != self._detail_task_id:
                self.detail_expanded = False
                self._detail_task_id = task.id
            retention = f" · {RETENTIONS[task.retention]}" if task.retention != "active" else ""
            title_widget = self.query_one("#detail-heading", Static)
            status, label, runtime_label = self.task_display(task)
            title_widget.update(Text(f"D{task.id:03} · {task.title}", style=f"bold {COLORS[status]}"))
            plan_label = " · 未定义步骤" if not task.steps else ""
            self.query_one("#detail-state", Static).update(Text(f"任务 · {label}{retention}{plan_label}",
                                                                  style=f"bold {COLORS[status]}"))
            current = self.board.runs.get(task.run_id)
            for context in self.board.task_contexts.values():
                if context.child_task_id == task.id:
                    detail.append(f"前序 D{context.parent_task_id:03} · 指定报告已人工验收\n", style="bold #79ccee")
                    detail.append("选定来源仅作引用数据；不是新许可。\n", style="#a6b3c6")
                    if self.detail_expanded:
                        detail.append(f"报告 {context.source_report_event_id} / 修订 {context.source_report_seq}\n验收 {context.acceptance_event_id}\n{context.source_snapshot}\n\n")
            inventory = [item for item in self.board.evidence_items.values() if item.owner_task_id == task.id]
            if inventory:
                detail.append(f'成果库存 · {len(inventory)} 项显式历史引用；不是新许可。\n', style='bold #79ccee')
                if self.detail_expanded:
                    detail.append(inventory_text(self.board, task) + '\n\n')
            if latest_dependencies(self.board, task.id):
                detail.append("局部依赖 · 有明确操作范围；未知释放不能接管。\n", style="bold #79ccee")
                if self.detail_expanded:
                    detail.append(dependency_text(self.board, task, durable=self.store is not None) + "\n\n")
            if current and task.run != "none" and self.detail_expanded and self.runtime_errors.get(current.id):
                detail.append(self.runtime_errors[current.id] + "\n", style="#f1b778")
            if self.detail_expanded:
                detail.append_text(decision_text(task))
                before, markdown_body, after = detail_parts(task, current, include_title=False)
                detail.append_text(before)
                if current and task.run != "none":
                    detail.append_text(self.supplement_details(current.id))
                tail.append_text(after)
                tail.append("\n安排\n", style="bold #79ccee")
                directory = current.cwd if current and task.run != "none" else self.board.projects[task.project]
                agent = self.agents.get(task.agent)
                exact = bool(agent and current and (agent.id, agent.session_id, agent.cwd) == (current.agent_id, current.session_id, current.cwd))
                executor = f"{agent.name} · {agent.model}" if agent and exact and current and not current.terminal else "原 Pi 连接（身份见记录定位）" if task.execution == "pi" and task.agent else task.agent or "未分配"
                tail.append(f"项目    {task.project}\n执行者  {executor}\n{'精确项目目录' if task.execution == 'pi' else '样例目录（不会访问）'}\n{directory or '未选择'}\n\n")
                if task.execution == "pi" and task.agent:
                    tail.append(f"连接 ID  {task.agent}\n", style="#a6b3c6")
                tail.append(runtime_label + "\n创建 ≠ 分配 ≠ 启动 ≠ 执行确认 ≠ 人工验收\n\n", style="#a6b3c6")
                runs = [run for run in self.board.runs.values() if run.task_id == task.id]
                tail.append_text(round_timeline_text(task, runs))
            else:
                detail.append_text(overview_text(task, current, decisions=decision_items(self.board, task),
                                                 capability_issue=self.report_capability_issue(task)))
        else:
            self.query_one("#detail-heading", Static).update("没有选中的任务")
            self.query_one("#detail-state", Static).update("")
            detail.append("换个筛选条件，或按 n 记下一个新任务。")
        self.refresh_submission_status()
        self.query_one("#step-flow", StepFlow).show_plan(task)
        self.query_one("#detail-text", Static).update(detail)
        self.query_one("#detail-tail", Static).update(tail)
        self.update_detail_markdown(task, current if task else None, markdown_body)
        self.query_one("#report-button", Button).disabled = task is None
        self.query_one("#more-button", Button).disabled = task is None
        has_decisions = self.decision_entry(task)
        decision_status = self.query_one("#decision-status", Static)
        decision_status.display = has_decisions
        if task and has_decisions:
            try:
                event = decision_source(self.board, task.id)
                assert event.report and event.report.decisions
                confirmed = prior_answers(self.board, task.id)
                decision_status.update(f"决策单 · {len(event.report.decisions)} 项 / 已保存决定或意见 {len(confirmed)} 项 · d 逐项核对；保存不等于执行")
            # The exception type is a plain ValueError; boolean conditions belong to recovery.
            # pi-lens-ignore: ast-grep:no-boolean-in-except
            except ValueError:
                decision_status.display = False  # No current question; historical decisions remain under Ctrl+K.
        self.refresh_decision_items(task)
        self.refresh_status()

    def refresh_decision_items(self, task: Task | None) -> None:
        rows = decision_items(self.board, task)
        widget = self.query_one("#decision-items", OptionList)
        widget.display = bool(rows)
        if rows:
            pending = sum(row.pending for row in rows)
            title = "决定事项 · 只读" if task and (task.status == "done" or task.retention != "active") else "待处理事项"
            self.query_one("#decision-status", Static).update(f"{title} · {pending} 项待处理 / {len(rows) - pending} 项已记录 · 点击或 Enter 核对")
        if rows == self._decision_rows:
            return
        selected = widget.get_option_at_index(widget.highlighted).id if widget.highlighted is not None else None
        selected_key = selected.rsplit(":", 1)[-1] if selected else None
        options = []
        readonly = bool(task and (task.status == "done" or task.retention != "active"))
        for index, row in enumerate(rows, 1):
            spec = row.spec
            presentation = present_decision(spec, human_input=business_input(spec, row.task_id, row.report_id, spec.key))
            category = {"owner_evidence": "待补材料", "user_fact": "待答问题",
                        "user_decision": "待核对方案", "legacy": "待处理事项（旧报告）"}[presentation.category]
            label = "只读查看" if readonly else "核对记录" if not row.pending else "进入本项"
            text = Text()
            text.append(f"{index}. {category} · {presentation.question}", style="bold")
            text.append(f"\n   [{row.state}] · 缺口 {len(spec.missing)} 项 · {label}", style="#f1b778" if row.pending else "#90d4ad")
            options.append(Option(text, id=row.id))
        widget.clear_options()
        widget.add_options(options)
        if rows:
            widget.highlighted = next((i for i, row in enumerate(rows) if row.spec.key == selected_key), 0)
        self._decision_rows = rows

    def refresh_hint(self) -> None:
        """Urgent action stays visible even while the full report is scrolled away."""
        task = self.selected_task
        hints = "→/Enter 详情 · p 项目 · f 状态 · Ctrl+K 全部动作"
        if task:
            if task.retention != "active":
                hints = f"{RETENTIONS[task.retention]} · Ctrl+K → 恢复任务" + (" / 永久删除" if task.retention == "trashed" else " / 移入回收站")
            elif task.status == "review":
                hints = "r 人工验收 · b 补充说明" if task.can_accept else "尚不具备验收条件 · b 补充说明，安排下一轮"
            elif (run := self.active_run(task)) is not None:
                if run.state == "waiting":
                    hints = "Pi 等待人工确认 · 到原 Pi 核实并处理"
                elif any(event.kind == "stop_requested" for event in key_updates(task, run)):
                    hints = "停止请求尚未确认 · 到原 Pi 核实回执"
                # pi-lens-ignore: ast-grep:unchecked-throwing-call-python
                elif self.runtime_errors.get(run.id) or connection_issue(run, self.agents.get(run.agent_id), int(time.time() * 1000)):
                    hints = "观测异常 · 不推断已停止，不自动重派"
                elif self.activity_run_id == run.id and self.activity and not self.activity.bound:
                    hints = "执行归属需核实 · 不接管原 Pi 的其他工作"
                else:
                    hints = "b 补充说明 · g 刷新进展 · x 请求停止 · 权限仍在 Pi 确认"
            elif task.status == "blocked":
                hints = "需要你补充信息 · →/Enter 看原因 · b 补充后重新安排"
            elif task.run in ("queued", "working"):
                hints = "g 模拟确认 / 进展 / 阻塞 / 交付 · 不会真实执行"
            elif task.status in ("draft", "ready"):
                hints = "e 编辑 · a 分配 · s 显式启动（分配不会启动）"
        primary = self.primary_action(task)
        urgent = bool(task and (task.retention != "active" or primary.label in {"核实执行", "查看停止状态", "查看确认信息"}))
        widget = self.query_one("#context-hint", Static)
        widget.display = urgent
        if primary.label == "核实执行":
            hints = "观测异常 · 请到原 Pi 核实，不推断结束、不自动重派"
        content = hints + "  │  ←/Esc 返回"
        if widget.content != content:
            widget.update(content)

    def on_resize(self, event: events.Resize) -> None:
        self.viewport_width = event.size.width
        if self.is_mounted:
            focused = self.focused
            was_tasks = isinstance(focused, OptionList) and bool(focused.id and (focused.id == "task-list" or focused.id.startswith("lane-")))
            self.apply_layout()
            self.refresh_tasks()
            inspector = self.query_one("#inspector")
            if (self.detail_open and self.viewport_width < 100 and len(self.screen_stack) == 1
                    and (focused is None or (focused is not inspector and inspector not in focused.ancestors))):
                self.call_after_refresh(self.query_one("#detail-scroll", VerticalScroll).focus)
            elif was_tasks:
                self.call_after_refresh(self.focus_tasks)
            elif focused is not None and self.query_one("#sidebar") in focused.ancestors and not self.query_one("#sidebar").display:
                self.call_after_refresh(self.query_one("#sidebar-button", Button).focus)

    def apply_layout(self) -> None:
        width = self.viewport_width
        mobile_detail = width < 100 and self.detail_open
        self.query_one("#workspace").set_class(width < 60, "phone")
        self.query_one("#masthead").display = not mobile_detail
        self.query_one("#context-line").display = not mobile_detail
        self.query_one("#toolbar").display = not mobile_detail
        self.query_one("#brand").display = width >= 60
        self.query_one("#demo-badge").display = width >= 60
        self.query_one("#detail-back", Button).display = mobile_detail
        self.query_one("#detail-palette", Button).display = mobile_detail
        self.query_one("#main").display = not (width < 60 and self.sidebar_open and not mobile_detail)
        self.query_one("#sidebar").display = self.sidebar_open and not mobile_detail
        toggle = self.query_one("#sidebar-button", Button)
        toggle.display = True
        toggle.label = "收起导航" if self.sidebar_open else "展开导航"
        board = self.view_mode == "board" and width >= 112
        self.query_one("#board").display = board and not self.detail_open
        self.query_one("#task-region").display = not board and not (self.detail_open and width < 100)
        self.query_one("#inspector").display = self.detail_open or (not board and width >= 100)
        self.query_one("#inspector").set_class(width < 100 or board, "full-detail")
        self.query_one("#content").set_class(width >= 100 and not board, "split-view")
        self.query_one("#view-button", Button).label = "列表  v" if self.view_mode == "board" else "看板  v"
        self.query_one("#view-button", Button).display = width >= 60

    @on(Input.Changed, "#search")
    def search_changed(self) -> None:
        self.refresh_tasks()

    def on_mouse_up(self, event: events.MouseUp) -> None:
        """Copy a completed read-only selection, not intermediate drags or form edits."""
        if event.button != 1:
            return
        widget = event.widget
        if widget is None:  # Real terminal events lack Pilot's widget hint.
            try:
                widget, _ = self.screen.get_widget_at(event.screen_x, event.screen_y)
            except NoWidget:
                return
        if not widget.is_attached or widget.screen is not self.screen:
            return
        if isinstance(widget, TextArea):
            text = widget.selected_text if widget.read_only else ""
        elif isinstance(widget, Input) or not widget.allow_select:
            return  # Buttons, lists and scrollbars must not re-copy an old selection.
        else:
            text = self.screen.get_selected_text()
        if text:
            self.copy_to_clipboard(text)

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        widget = event.widget
        if isinstance(widget, DataTable):
            widget.scroll_visible(top=True, animate=False)
        if isinstance(widget, TaskList) and widget.has_focus and self.detail_open:
            self.detail_open = False
            self.apply_layout()
        if isinstance(widget, OptionList) and widget.has_focus and widget.highlighted is not None and widget.id and widget.id.startswith("lane-"):
            task_id = widget.get_option_at_index(widget.highlighted).id
            if task_id:
                # pi-lens-ignore: ast-grep:unchecked-throwing-call-python
                self.selected_id = int(task_id.removeprefix("task-"))
                self.refresh_selection_markers()
                self.show_detail()

    @on(OptionList.OptionHighlighted)
    def highlight_task(self, event: OptionList.OptionHighlighted) -> None:
        widget = event.option_list
        if not widget.id or not (widget.id == "task-list" or widget.id.startswith("lane-")):
            return
        # Ignore stale queued highlight messages after filtering/reordering.
        if not widget.has_focus or widget.highlighted != event.option_index or not event.option.id:
            return
        if widget.get_option_at_index(event.option_index).id != event.option.id:
            return
        # pi-lens-ignore: ast-grep:unchecked-throwing-call-python
        task_id = int(event.option.id.removeprefix("task-"))
        if task_id != self.selected_id:
            self.selected_id = task_id
            self.refresh_selection_markers()
            self.query_one("#detail-scroll", VerticalScroll).scroll_home(animate=False)
            self.show_detail()

    @on(OptionList.OptionSelected)
    def select_option(self, event: OptionList.OptionSelected) -> None:
        source, key = event.option_list.id, event.option.id
        if source == "decision-items" and key:
            row = next((item for item in decision_items(self.board, self.selected_task) if item.id == key), None)
            if row is None:
                self.notify("事项或任务已变化，请核对最新清单。", severity="warning")
                return
            self.action_decisions(row.spec.key)
        elif source == "nav-project" and key:
            self.set_project(key)
        elif source == "nav-status" and key:
            self.set_filter(key)
        elif key and (source == "task-list" or (source and source.startswith("lane-"))):
            # pi-lens-ignore: ast-grep:unchecked-throwing-call-python
            self.selected_id = int(key.removeprefix("task-"))
            self.refresh_selection_markers()
            self.detail_open = True
            self.detail_expanded = False
            self.apply_layout()
            self.show_detail()
            scroll = self.query_one("#detail-scroll", VerticalScroll)
            # Explicit opening returns to outcomes; ordinary progress refresh never jumps.
            scroll.scroll_home(animate=False)
            scroll.focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "return-button":
            task = self.selected_task
            label, target = self.secondary_action(task, self.primary_action(task))
            if not target or (self._primary_task_id != self.selected_id
                              or self._primary_snapshot != self.primary_snapshot(task)
                              or str(event.button.label) != label):
                if target:
                    self.notify("任务或下一步操作已变化，请重新核对。", severity="warning")
                return
            {"assign": self.action_assign, "read_report": self.action_read_report,
             "return_work": self.action_return_work}[target]()
            return
        actions = {"view-button": self.action_view, "palette-button": self.action_palette,
                   "more-button": self.action_more_tools, "new-button": self.action_new_task,
                   "filter-button": self.action_filter, "sidebar-button": self.action_sidebar,
                   "detail-back": self.action_close_detail, "detail-palette": self.action_palette,
                   "accept-button": self.action_primary, "report-button": self.action_read_report}
        action = actions.get(event.button.id or "")
        if action:
            action()

    def action_primary(self) -> None:
        expected = self._primary_action
        if expected is None or self._primary_task_id != self.selected_id:
            self.notify("任务选择已变化，请重新核对主要操作。", severity="warning")
            return
        if (expected.action in {"assign", "edit_task", "review", "start", "return_work", "decisions"}
                and (self._primary_snapshot != self.primary_snapshot(self.selected_task) or self.primary_action() != expected)):
            self.notify("任务或安排已更新，请重新核对主要操作。", severity="warning")
            return
        actions = {"new_task": self.action_new_task, "assign": self.action_assign,
                   "edit_task": self.action_edit_task, "start": self.action_start,
                   "review": self.action_review, "return_work": self.action_return_work, "activity": self.action_activity,
                   "read_report": self.action_read_report, "decisions": self.action_decisions,
                   "capability_help": self.action_capability_help, "prepare_recovery": self.action_prepare_recovery}
        actions[expected.action]()

    def focus_tasks(self) -> None:
        if self.selected_id is None:
            self.query_one("#new-button", Button).focus()
        elif self.detail_open:
            self.query_one("#detail-scroll", VerticalScroll).focus()
        elif self.view_mode == "board" and self.viewport_width >= 112:
            status = self.task_display(self.selected_task)[0] if self.selected_task else "draft"
            key = "running" if status == "blocked" else status
            self.query_one(f"#lane-{key}", OptionList).focus()
        else:
            self.query_one("#task-list", OptionList).focus()

    def action_sidebar(self) -> None:
        self.sidebar_open = not self.sidebar_open
        self.apply_layout()
        self.query_one("#nav-status" if self.sidebar_open else "#sidebar-button").focus()

    def action_back(self) -> None:
        if self.detail_open and self.viewport_width < 100:
            self.action_close_detail()
            return
        if self.sidebar_open:
            self.action_sidebar()
            return
        self.action_close_detail()

    def action_close_detail(self) -> None:
        self.detail_open = False
        if self.viewport_width < 60:
            self.sidebar_open = False
        self.apply_layout()
        self.focus_tasks()

    def action_search(self) -> None:
        self.query_one("#search", Input).focus()

    def action_view(self) -> None:
        self.view_mode = "board" if self.view_mode == "list" else "list"
        self.detail_open = False
        self.refresh_tasks()
        self.apply_layout()
        self.focus_tasks()

    def set_project(self, key: str | None) -> None:
        if key == "__add_project__":
            self.action_add_project()
            return
        if key:
            self.project_name = key
            self.refresh_tasks()
            self.focus_tasks()

    def set_filter(self, key: str | None) -> None:
        if key:
            self.status_filter = key
            self.refresh_tasks()
            self.focus_tasks()

    def action_project(self) -> None:
        options = {"all": "全部项目", **{key: f"{key} · {path or '未选择目录'}" for key, path in self.board.projects.items()}}
        if self.board.live:
            options["__add_project__"] = "＋ 添加真实项目目录"
        self.push_screen(Choice("切换项目", options), self.set_project)

    def action_filter(self) -> None:
        self.push_screen(Choice("筛选任务状态", FILTERS), self.set_filter)

    def action_new_task(self) -> None:
        project = "收集箱" if self.project_name == "all" else self.project_name
        def saved(task: Task | None) -> None:
            if task:
                self.project_name, self.status_filter = task.project, "all"
                self.query_one("#search", Input).value = ""
                self.selected_id = task.id
                self.refresh_tasks()
                self.focus_tasks()
                self.notify("草稿已创建并保存到本地 · 没有启动 Agent" if self.store else "草稿已创建 · 没有启动 Agent · 退出即丢弃")
        self.push_screen(TaskForm(project, save=lambda title, project, description: self.apply_change(lambda board: board.create(title, project, description)), durable=self.store is not None, projects=self.board.projects), saved)

    def action_budget(self) -> None:
        if len(self.screen_stack) > 1:
            return
        def chosen(action: str | None) -> None:
            if action == 'record':
                def mode_chosen(mode: str | None) -> None:
                    if mode:
                        self.open_budget_form(mode)
                self.push_screen(Choice('独立累计账本 · 只保存审计',
                                        {'scope': '声明稳定累计计量范围（不清零旧用量）',
                                         'observe': '记录来源回执（自报不等于已核验）',
                                         'verify': '完整人工核验（不授权、不派发）',
                                         'revoke': '明确撤销已有累计授权（不发送停止命令）'},
                                        '正式批准入口关闭；可信实际证据与启用门须独立通过。'), mode_chosen)
        self.push_screen(BudgetPanel(ledger_text(self.board, durable=self.store is not None)), chosen)

    def open_budget_form(self, mode: str) -> None:
        if mode not in {'scope', 'observe', 'verify', 'revoke'} or len(self.screen_stack) > 1:
            return
        expected = encode(self.board)
        scopes = {scope.id: scope.resource_key + ' · ' + scope.id for scope in self.board.budget_scopes.values()}
        observations = {row.id: (row.scope_id, row.receipt_id + f' · 修订 {row.revision} · {row.used} · {row.source_kind}')
                        for row in self.board.usage_observations.values()}
        items = {item.id: item.name + ' · ' + item.id for item in self.board.evidence_items.values()}
        def unchanged(board: Board) -> None:
            if encode(board) != expected:
                raise ValueError('来源、任务或累计审计在最后确认前变化；未保存，输入保留，请重新核对。')
        def integer(values: dict[str, str], key: str) -> int:
            value = values.get(key, '')
            if not value or not value.isascii() or not value.isdecimal() or len(value) > 16:
                raise ValueError('资源数字与来源修订必须明确填写非负十进制整数，不能猜测或自动填零。')
            return int(value)
        def prepare(selected: str, values: dict[str, str]) -> BudgetRecord:
            unchanged(self.board)
            if mode == 'scope':
                return scope_record(self.board, **{key: values.get(key, '') for key in
                                                  ('resource_key', 'meter_id', 'definition', 'author', 'started_at')})
            if mode == 'observe':
                return observation_record(self.board, selected, receipt_id=values.get('receipt_id', ''),
                                          meter_id=values.get('meter_id', ''), revision=integer(values, 'revision'),
                                          used=(integer(values, 'attempts'), integer(values, 'minutes'), integer(values, 'storage_mib')),
                                          observed_at=values.get('observed_at', ''), source_kind=values.get('source_kind', ''),
                                          evidence_item_id=values.get('evidence_item', ''))
            if mode == 'verify':
                if values.get('complete') not in {'yes', 'no'}:
                    raise ValueError('必须明确选择完整覆盖或未完整覆盖，不默认核验全部历史用量。')
                return ledger_verification_record(self.board, selected, observation_id=values.get('observation', ''),
                                                  complete_coverage=values['complete'] == 'yes',
                                                  **{key: values.get(key, '') for key in
                                                     ('result', 'verifier', 'method', 'limits', 'gaps', 'next_owner', 'checked_at')})
            return revoke_record(self.board, selected, author=values.get('author', ''), reason=values.get('reason', ''))
        def save(record: BudgetRecord) -> None:
            def change(board: Board) -> None:
                unchanged(board)
                board.save_budget_record(record)
            self.apply_change(change)
        self.push_screen(BudgetForm(mode, scopes=scopes, observations=observations, items=items,
                                    prepare=prepare, save=save, durable=self.store is not None),
                         lambda saved: self.refresh_tasks() if saved else None)

    def action_dependencies(self) -> None:
        task = self.selected_task
        if not task or task.execution != 'pi' or len(self.screen_stack) > 1:
            return
        task_id = task.id
        def chosen(action: str | None) -> None:
            if action == 'add':
                self.run_worker(self.open_dependency_form(task_id, 'add'))
            elif action == 'audit':
                def audit(mode: str | None) -> None:
                    if mode:
                        self.run_worker(self.open_dependency_form(task_id, mode))
                self.push_screen(Choice('独立核对 · 不指派、不派发', {'check': '契约 / 证据条件核对',
                                        'release': '原写者明确释放（仍未接收）', 'receipt': '接续者独立接收已保存释放'},
                                        '释放与接收分开持久化；空闲、离线和验收不等于释放。'), audit)
        self.push_screen(DependencyPanel(f'D{task.id:03} · 局部依赖', dependency_text(self.board, task, durable=self.store is not None),
                                         writable=task.retention == 'active'), chosen)

    async def open_dependency_form(self, task_id: int, mode: str) -> None:
        task = self.board.tasks.get(task_id)
        if not task or task.execution != 'pi' or task.retention != 'active' or len(self.screen_stack) > 1:
            return
        if mode == 'receipt' and not self.store:
            self.notify('释放尚未持久化，尚未交接；不能记录为已接收。', severity='warning')
            return
        expected = deepcopy(task)
        if mode in {'release', 'receipt'} and self.runtime:
            try:
                discovered = await asyncio.to_thread(self.runtime.discover)
            except (ValueError, OSError) as error:
                self.notify(f'接续者身份仍未知：{error}；不猜测接收人。', severity='warning')
                return
            self.agents.update((agent.id, agent) for agent in discovered)
        if not self.is_running or len(self.screen_stack) > 1 or self.board.tasks.get(task_id) != expected:
            return
        snapshot = deepcopy(self.board)
        audit_snapshot = {name: getattr(snapshot, name) for name in REGISTRIES}
        sources = {str(source.id): source for source in snapshot.tasks.values()
                   if source.id != task_id and source.execution == 'pi' and snapshot.projects.get(source.project, '').startswith('/')}
        deps = {dep.id: dep for dep in latest_dependencies(snapshot, task_id) if (dep.kind != 'write-conflict') == (mode == 'check')}
        releases = {record.id: record for record in snapshot.ownership_releases.values() if record.dependency_id in deps}
        if mode == 'add':
            choices = {key: f'D{source.id:03} · {source.title}' for key, source in sources.items()}
            hints = {key: f'D{source.id:03} · {source.title}\n目标：{source.description}\n精确目录：{snapshot.projects[source.project]}\n当前执行：{source.run_id or "无，释放事实未知"}'
                     for key, source in sources.items()}
        else:
            records = releases if mode == 'receipt' else deps
            choices = {key: key + ' · ' + (record.condition if isinstance(record, TaskDependency) else '已保存原写者释放') for key, record in records.items()}
            hints = {key: canonical(asdict(record)) + '\n上述是已保存人工记录，字段不自动填入新确认。' for key, record in records.items()}
        receivers = {agent.id: agent.label + ' · ' + agent.id for agent in self.agents.values()
                     if agent.cwd == snapshot.projects[task.project]}
        items = {item.id: item.name + ' · ' + item.id for item in snapshot.evidence_items.values() if item.owner_task_id == task_id}
        def unchanged(board: Board) -> Task:
            current = board.tasks.get(task_id)
            if current != expected or any(getattr(board, name) != audit_snapshot[name] for name in REGISTRIES):
                raise ValueError('任务或依赖审计在最后确认前变化；输入保留，请重新核对。')
            assert current is not None
            return current
        def prepare(selected: str, values: dict[str, str], operations: list[str]) -> DependencyRecord:
            current = unchanged(self.board)
            files = values.get('files', '').splitlines()
            if mode == 'add':
                source = sources.get(selected)
                actual = self.board.tasks.get(source.id) if source else None
                if (source is None or actual != source or self.board.runs.get(source.run_id) != snapshot.runs.get(source.run_id)
                        or not values.get('proof')):
                    raise ValueError('来源已变化或证明等级尚未明确选择；不默认满足依赖。')
                assert actual is not None
                return dependency_record(self.board, current, actual, kind=values.get('kind', ''), operations=operations,
                                         files=files, condition=values.get('condition', ''), proof=values['proof'])
            notes = {key: values.get(key, '') for key in ('verifier', 'method', 'checked_at')}
            if mode != 'receipt':
                notes.update({key: values.get(key, '') for key in ('limits', 'gaps', 'next_owner')})
            if mode == 'check':
                return check_record(self.board, selected, result=values.get('result', ''), evidence_item_id=values.get('evidence_item', ''), **notes)
            if mode == 'release':
                return release_record(self.board, selected, values.get('receiver', ''), files, expires_at=values.get('expires_at', ''), **notes)
            return receipt_record(self.board, selected, values.get('receiver', ''), files, **notes)
        def save(record: DependencyRecord) -> None:
            def change(board: Board) -> None:
                current = unchanged(board)
                if isinstance(record, TaskDependency):
                    source = sources.get(str(record.source_task_id))
                    if source is None or board.tasks.get(source.id) != source or board.runs.get(source.run_id) != snapshot.runs.get(source.run_id):
                        raise ValueError('明确来源在最后确认前变化；未保存，输入保留。')
                    board.add_dependency(current, record)
                elif isinstance(record, DependencyCheck):
                    board.check_dependency(current, record)
                elif isinstance(record, OwnershipRelease):
                    board.release_ownership(current, record)
                else:
                    board.receive_ownership(current, record)
            self.apply_change(change)
        def saved(result: str | None) -> None:
            if result:
                self.refresh_tasks()
                self.notify('明确记录已本地保存 · 未分配、未派发；释放仍须独立接收与原 Pi 活动核对。' if self.store else
                            '仅本会话，尚未持久化、尚未交接；未分配、未派发。')
        self.push_screen(DependencyForm(f'D{task_id:03}', mode, choices=choices, hints=hints, receivers=receivers,
                                        items=items, prepare=prepare, save=save, durable=self.store is not None), saved)

    def action_evidence(self) -> None:
        task = self.selected_task
        if not task or task.execution != 'pi' or len(self.screen_stack) > 1:
            return
        task_id = task.id
        def chosen(action: str | None) -> None:
            if action == 'add':
                self.open_evidence_reference(task_id)
            elif action == 'verify':
                self.open_evidence_verification(task_id)
        self.push_screen(EvidenceInventory(f'D{task.id:03} · 成果库存', inventory_text(self.board, task),
                                           writable=task.retention == 'active', durable=self.store is not None), chosen)

    def open_evidence_reference(self, task_id: int) -> None:
        task = self.board.tasks.get(task_id)
        if not task or task.retention != 'active' or task.execution != 'pi':
            self.notify('只有当前真实任务可以选择库存引用。', severity='warning')
            return
        expected = deepcopy(task)
        candidates = inventory_candidates(self.board)
        def prepare(key: str, refs: list[str], values: dict[str, str]) -> EvidenceItem:
            current = self.board.tasks.get(task_id)
            if current != expected or key not in candidates:
                raise ValueError('任务或所选来源已变化；输入保留，请重新核对。')
            assert current is not None
            return reference_from_candidate(self.board, current, candidates[key], refs, **values)
        def save(item: EvidenceItem) -> None:
            def change(board: Board) -> None:
                current = board.tasks.get(task_id)
                if current != expected:
                    raise ValueError('任务在最后确认前已变化；未保存、未派发，输入保留。')
                assert current is not None
                board.add_evidence(current, item)
            self.apply_change(change)
        def saved(result: str | None) -> None:
            if result:
                self.refresh_tasks()
                self.notify('引用仅保存 · 来源未改动 · 原件仍未核实 · 没有发送或新许可')
        self.push_screen(EvidenceForm(f'D{task.id:03}', candidates, prepare=prepare, save=save), saved)

    def open_evidence_verification(self, task_id: int) -> None:
        task = self.board.tasks.get(task_id)
        if not task or task.retention != 'active' or task.execution != 'pi':
            self.notify('只有当前真实任务可以追加独立人工核验记录。', severity='warning')
            return
        expected = deepcopy(task)
        items = {key: item for key, item in self.board.evidence_items.items() if item.owner_task_id == task_id}
        def prepare(item_id: str, values: dict[str, str]) -> EvidenceVerification:
            if self.board.tasks.get(task_id) != expected or self.board.evidence_items.get(item_id) != items.get(item_id):
                raise ValueError('任务或库存项已变化；输入保留，请重新核对。')
            return verification_record(self.board, item_id, **values)
        def save(record: EvidenceVerification) -> None:
            def change(board: Board) -> None:
                current = board.tasks.get(task_id)
                if current != expected or board.evidence_items.get(record.item_id) != items.get(record.item_id):
                    raise ValueError('任务或核验来源在最后确认前已变化；输入保留，未保存。')
                assert current is not None
                board.verify_evidence(current, record)
            self.apply_change(change)
        def saved(result: str | None) -> None:
            if result:
                self.refresh_tasks()
                self.notify('人工核验审计仅保存 · 不是平台探测、任务验收或新许可 · 没有派发')
        self.push_screen(EvidenceVerificationForm(f'D{task.id:03}', items, prepare=prepare, save=save), saved)

    def action_followup(self) -> None:
        task = self.selected_task
        if not task or len(self.screen_stack) > 1:
            return
        run = self.board.runs.get(task.run_id)
        report = current_report(task, run)
        acceptance = next((event for raw in reversed(task.events) if (event := readable_event(raw))
                           and event.kind == 'accepted' and event.source == 'human' and event.run_id == task.run_id), None)
        if task.status != 'done' or not report or report.kind != 'full' or not acceptance or not run:
            self.notify('需要规范完整报告和同轮人工验收；不能从标题或聊天猜测前序关系。', severity='warning')
            return
        expected, source_run = deepcopy(task), deepcopy(run)
        directory = self.board.projects.get(task.project)
        source = (f'前序 D{task.id:03} · {task.title}\nRun {run.id}\n报告 {report.id} / 修订 {report.seq}'
                  f'\n人工验收 {acceptance.id}\n时间 {acceptance.at}\n精确目录 {directory}'
                  '\n\n只引用选定数据；验收不是新实施、访问路径或预算许可。')
        def save(title: str, goal: str, refs: list[str]) -> Task:
            def create(board: Board) -> Task:
                parent = board.tasks.get(expected.id)
                if parent != expected or board.runs.get(source_run.id) != source_run or board.projects.get(expected.project) != directory:
                    raise ValueError('前序来源在填写期间已变化；输入保留，请重新核对。')
                assert parent is not None
                context = create_followup_context(board, parent, report.id, acceptance.id, refs, title, goal)
                return board.create_followup(parent.id, context, title, goal)
            return self.apply_change(create)
        def saved(child: Task | None) -> None:
            if child:
                self.project_name, self.status_filter, self.selected_id = child.project, 'all', child.id
                self.refresh_tasks()
                self.notify('后续草稿已保存 · 原验收不变 · 没有派发或新授权')
        self.push_screen(FollowupForm(source, save=save), saved)

    def action_work_contract(self) -> None:
        task = self.selected_task
        if not task or len(self.screen_stack) > 1 or task.status not in ('draft', 'ready') or task.run != 'none':
            self.notify('先准备未启动的本轮草稿；合同修改不改变正在执行的输入。', severity='warning')
            return
        expected = deepcopy(task)
        def save(contract: WorkContract) -> None:
            def change(board: Board) -> None:
                current = board.tasks.get(expected.id)
                if current != expected:
                    raise ValueError('任务在填写期间已变化；文字保留，请重新核对。')
                assert current is not None
                board.set_work_contract(current, contract)
            self.apply_change(change)
        def saved(result: str | None) -> None:
            if not result:
                return
            self.refresh_tasks()
            current = self.board.tasks[expected.id]
            if result == 'preview' and current.status == 'ready' and self.runtime:
                self.run_worker(self.confirm_dispatch(expected.id))
            else:
                self.notify('合同仅保存 · 未派发；分配 Pi 后再核对启动')
        self.push_screen(WorkContractForm(expected, save=save), saved)

    def action_prepare_recovery(self) -> None:
        task = self.selected_task
        run = self.board.runs.get(task.run_id) if task else None
        if (not task or not run or task.execution != 'pi' or not run.terminal
                or task.status == 'done' or task.retention != 'active' or len(self.screen_stack) > 1):
            self.notify('只有已确认结束且未验收的任务可准备恢复；已验收成果请新建后续。', severity='warning')
            return
        expected, source = deepcopy(task), deepcopy(run)
        view = self.ownership_for(task, run)
        status = f'任务 {view.task_state} / Pi {view.pi_state} / 归属 {view.ownership} / 新鲜度 {view.freshness}\n{view.recommended_next_step}'
        def save(contract: WorkContract, checkpoint: str, remaining: str) -> None:
            def prepare(board: Board) -> None:
                current = board.tasks.get(expected.id)
                if current is None or current != expected or board.runs.get(source.id) != source:
                    raise ValueError('来源或任务在填写期间变化；保留输入，请重新核对。')
                board.prepare_recovery(current, source, checkpoint, remaining, contract)
            self.apply_change(prepare)
        def saved(result) -> None:
            if result:
                self.refresh_tasks()
                self.notify('恢复草稿仅保存 · 旧 Run 未改动；新轮需核实原 Pi 活动并另行确认。', timeout=8)
        self.push_screen(RecoveryForm(expected, source, status, save=save), saved)

    def action_edit_task(self) -> None:
        task = self.selected_task
        if task:
            if task.execution == "pi" and task.run != "none":
                self.notify("本轮原始输入已固定；b 可追加说明，修改原要求须结束后安排新轮。", severity="warning")
                return
            def save(title: str, project: str, description: str) -> Task:
                self.apply_change(lambda board: board.edit(board.tasks[task.id], title, project, description))
                return self.board.tasks[task.id]
            self.push_screen(TaskForm(task.project, task, save=save, durable=self.store is not None, projects=self.board.projects), lambda result: self.refresh_tasks() if result else None)

    def apply_change(self, operation: Callable[[Board], T]) -> T:
        # Publish only after SQLite commits, leaving current widgets/state intact on failure.
        candidate = deepcopy(self.board) if self.store is not None else self.board
        result = operation(candidate)
        if self.store is not None:
            self.store.save(candidate)
        self.board = candidate
        return result

    def check_terminal_task(self, board: Board, task: Task) -> None:
        run = board.runs.get(task.run_id)
        if self.runtime and task.execution == "pi" and task.status != "done" and task.run != "none" and run and run.terminal:
            # Only a matching, task-owned human decision releases the old observation gate.
            if run.state == "abandoned" and any(
                event.kind == "abandoned" and event.source == "human"
                and event.run_id == run.id and event.seq == run.seq
                for raw in task.events if (event := readable_event(raw))
            ):
                return
            try:
                self.runtime.check_current(run, known_session=run.id in self.sessions)
            except OSError as error:
                raise BridgeError("原 Pi 观测来源不可读；请核实当前活动后重新确认。") from error

    def check_new_round_task(self, board: Board, task: Task) -> None:
        """Save-only does not erase the prior live ownership gate."""
        if not self.runtime or task.execution != 'pi' or not task.run_id:
            return
        source = board.runs.get(task.run_id)
        if (source is None or source.task_id != task.id
                or (task.title, task.description, board.projects.get(task.project)) != (source.title, source.description, source.cwd)):
            raise BridgeError('原轮次身份或精确范围不符；不会创建新意图。')
        intent = latest_adoption(task)
        # A transport override belongs only to its original Run; successors prove their own identity.
        adoption = intent if (intent and intent.source and intent.source.run_id == source.id
                              and adoption_confirmed(task, intent)) else None
        if adoption:
            adoption.validate_task(board, task)
        self.runtime.check_new_round(source, adoption=adoption)

    def check_dependency_execution(self, board: Board, task: Task, *, agent_id: str | None = None,
                                   requirements: str | None = None, purpose: str | None = None) -> None:
        purpose = purpose or operation(request_for(board, task) if requirements is None else requirements)
        board.check_dependencies(task, purpose, agent_id=agent_id, requirements=requirements)
        writers = [dep for dep in latest_dependencies(board, task.id) if dep.kind == 'write-conflict' and purpose in dep.operations]
        if writers and (self.store is None or self.runtime is None):
            raise BridgeError('写入权记录尚未持久化或原 Pi 归属不可观测；不能指定接续写者。')
        if writers and agent_id is not None and self.runtime:
            receiver = self.runtime.agent(agent_id)
            if not receiver.available or receiver.cwd != board.projects[task.project]:
                raise BridgeError("接续 Pi 必须新鲜空闲且处于精确目录；接收陈述不代表当前可用。")
        for dep in writers:
            source = board.tasks[dep.source_task_id]
            run = board.runs.get(source.run_id)
            if not run or not self.runtime:
                raise BridgeError('原写者执行来源未知；不能接管。')
            self.runtime.check_new_round(run)  # Fresh idle + exact old session/ownership, never offline => release.

    def check_decision_observation(self, task: Task, run: Run) -> None:
        """Only a proved terminal offline session may outlive its old Pi heartbeat."""
        current = self.board.tasks.get(task.id)
        if current is None or current != task or self.board.runs.get(current.run_id) != run:
            raise BridgeError("任务或轮次在确认期间变化；请重新核对。")
        runtime = self.runtime
        if runtime is None or runtime.observation_only(run):
            raise BridgeError("本轮为只读观测或状态不明；可以仅保存，不能提交执行。")
        issue = self.runtime_errors.get(run.id)
        if not issue:
            return
        # Match ONLY the read_updates stale-agent warning. Any changed wording fails closed.
        if (not run.terminal or self.board.supplement_mode(current) != "next" or
                issue != "Pi 离线/心跳过期；不推断执行结束，不会自动重派。"):
            raise BridgeError("本轮为只读观测或状态不明；可以仅保存，不能提交执行。")
        session = runtime.read_session(run)
        if (not session or session.state != "offline" or session.input_pending or session.following or
                session.run_id not in ("", run.id) or session.linked_run_id not in ("", run.id)):
            raise BridgeError("原 Pi 离线状态未能核实，或有新输入/任务归属；不能按旧报告提交。")
        self.check_terminal_task(self.board, current)  # Recheck successors and source before selecting a new Pi.

    def mutate(self, operation: Callable[[Board], object]) -> None:
        try:
            self.apply_change(operation)
        except (ValueError, StorageError) as error:
            self.notify(f"未保存：{error}", severity="error", timeout=8)
            return
        self.refresh_tasks()

    def agent_label(self, agent_id: str) -> str:
        agent = self.agents.get(agent_id)
        return agent.label if agent else agent_id or "未分配"

    def active_run(self, task: Task) -> Run | None:
        run = self.board.runs.get(task.run_id)
        return run if run and not run.terminal else None

    def ownership_for(self, task: Task, run: Run):
        return project_ownership(task, run, self.agents.get(run.agent_id), self.sessions.get(run.id),
                                 issue=self.session_errors.get(run.id, ''),
                                 readonly=bool(self.runtime and self.runtime.observation_only(run)),
                                 current_identity=self.runtime.observation_identity(run) if self.runtime else None)

    def report_capability_issue(self, task: Task | None) -> str:
        if not task or not self.runtime or task.execution != "pi" or not task.agent or task.status == "done":
            return ""
        previous = self.board.runs.get(task.run_id)
        if previous and previous.terminal:
            ownership = self.ownership_for(task, previous)
            if 'send_new_round' not in ownership.allowed_actions:
                return ''  # Current activity/ownership has higher priority than report capability.
        run = previous if task.run != "none" else None
        if run and (not run.terminal or self.runtime.observation_only(run)):
            return ""  # Existing execution and explicit read-only observations are not new dispatch targets.
        try:
            agent = self.runtime.read_agent(run) if run else self.runtime.agent(task.agent)
            if not agent.fresh or agent.cwd != self.board.projects.get(task.project):
                return ""  # Availability/identity guards retain their own failure precedence.
            return self.runtime.report_capability_issue(agent, run.requirements if run else task.remaining)
        except (ValueError, OSError):
            return ""

    def action_capability_help(self) -> None:
        task = self.selected_task
        if not task or not self.runtime:
            return
        issue = self.report_capability_issue(task)
        if not issue:
            self.refresh_tasks()
            return
        directory = self.board.projects.get(task.project, "")
        source = Path(__file__).resolve().with_name("pi-bridge.ts")
        body = (issue + "\n\n谁处理：使用这条 Pi 连接的人。不是业务方案或预算审批。\n"
                "1. 先保存未提交输入，核实原 Pi 与相关子任务的当前活动；离线不是已结束。本页不会停止它们。\n"
                f"2. 核对精确连接/会话及实际加载的桥接路径：\n{source}\n"
                "   源码更新不等于已加载更新；加载路径未知时先核对启动入口。不能由进程启动时间断言内存 SDK 版本。\n"
                "3. 区分刷新层级：重开工作台只刷新展示；/reload 只更新扩展，不保证刷新 Pi 核心。\n"
                "   只有核实是扩展加载问题后，才由你另行决定手动 reload/重新连接。疑似旧核心但无公共版本证据时保持未知，\n"
                "   若需完整 Pi 进程刷新，应先保存工作、核实执行停止并另行明确授权；本页不执行、不反复催 reload。\n"
                f"   精确项目目录须保持：{directory}\n"
                "4. 回到工作台人工选择已核验连接；同目录合格连接不会自动换入。任何新请求须另行核对与确认，不重发原任务。\n\n"
                "本页只读已有 v2 证明：不探测、不创建模型回合或报告，不索取凭据、不安装或修改 trust。")
        self.push_screen(Confirm("检查连接 · 仅说明，不执行", body, "知道了"))

    def primary_action(self, task: Task | None = None) -> PrimaryAction:
        task = self.selected_task if task is None else task
        run = self.board.runs.get(task.run_id) if task else None
        if (task and task.execution == "pi" and task.status != "done" and task.retention == "active"
                and (run is None or run.terminal)):
            adoption_state = self._adoption_states.get(task.id)
            if adoption_state == "waiting":
                return PrimaryAction("read_report", "等待会话空闲 · 不启动", "当前原生回合继续，不插话、不停止；接收后只建立关联。")
            if task.id in self._adoption_errors:
                return PrimaryAction("adopt_session", "核对纳管状态", self._adoption_errors[task.id])
            if adoption_state == "attached" and not run:
                return PrimaryAction("start", "会话已纳管 · 核对新要求 s", "关联不等于执行；可在原会话明确输入，或先填写本轮合同再派发。")
        needs_verification = False
        stop_requested = False
        if task and run and task.execution == "pi" and not run.terminal:
            agent = self.agents.get(run.agent_id)
            session = self.sessions.get(run.id)
            needs_verification = bool(
                self.runtime_errors.get(run.id)
                # pi-lens-ignore: ast-grep:unchecked-throwing-call-python
                or connection_issue(run, agent, int(time.time() * 1000))
                or (self.activity_run_id == run.id and self.activity and not self.activity.bound)
                or (session and run.state != "queued" and session.run_id != run.id)
                or (self.runtime and self.runtime.observation_only(run))
            )
            stop_requested = any(event.kind == "stop_requested" for event in key_updates(task, run))
        dependency_issue = ''
        if task and task.execution == 'pi' and task.run == 'none' and latest_dependencies(self.board, task.id):
            try:
                self.board.check_dependencies(task, operation(task.remaining), agent_id=task.agent or None)
                if self.store is None:
                    dependency_issue = '记录尚未持久化，尚未交接'
            except ValueError as error:
                dependency_issue = str(error)
        has_directory = bool(task and task.project != "收集箱" and self.board.projects.get(task.project))
        return project_primary_action(task, run, has_directory=has_directory,
                                      needs_verification=needs_verification, stop_requested=stop_requested,
                                      decisions=decision_items(self.board, task), capability_issue=self.report_capability_issue(task),
                                      dependency_issue=dependency_issue,
                                      ownership=self.ownership_for(task, run) if task and run and run.terminal else None)

    @staticmethod
    def primary_button_label(action: PrimaryAction) -> str:
        if action.action == "read_report" and action.label in {"核实执行", "查看停止状态", "查看确认信息", "查看派发状态"}:
            return "查看状态"
        return action.label

    def primary_snapshot(self, task: Task | None) -> tuple[object, ...] | None:
        if task is None:
            return None
        run = self.board.runs.get(task.run_id) if task.run != "none" else None
        run_snapshot = (run.identity, run.state, run.seq, run.delivery, run.summary,
                        tuple(run.checks), run.remaining, tuple(run.events)) if run else None
        return (task.id, task.title, task.project, task.description, task.status, task.agent,
                task.run, task.run_id, task.remaining, task.full_delivery, task.execution,
                task.retention, run_snapshot, tuple(r.id for r in self.board.decisions.values() if r.task_id == task.id))

    def secondary_action(self, task: Task | None, primary: PrimaryAction) -> tuple[str, str | None]:
        """Show the adjacent operation only when this task can safely reach it."""
        if not task or task.retention != "active" or task.status == "done":
            return "", None
        if primary.action == "return_work":
            if task.execution == "pi" and task.status == "ready" and task.run == "none":
                return ("更换执行者 a" if task.agent else "选择执行者 a"), "assign"
            if task.status == "review" and not task.can_accept:
                return "", None  # A disabled agent selector is not a next action; the editor chooses Pi later.
            return "选择/新建 Pi a" if task.execution == "pi" else "安排执行 a", None
        if primary.action == "decisions":
            return "决定后继续", None  # Continue is confirmed inside the decision sheet.
        if primary.action in {"assign", "edit_task"}:
            return "开始任务 s" if primary.action == "assign" else "选择执行者 a", None
        if primary.action == "start" and task.execution == "pi":
            return "更换执行者 a", "assign"
        if primary.action == "review":
            return "核对报告", "read_report"
        if primary.action == "read_report" and primary.label == "查看进展" and self.available_supplement_mode(task) == "live":
            return "补充说明 b", "return_work"
        return "", None

    def refresh_secondary_action(self) -> None:
        label, target = self.secondary_action(self.selected_task, self._primary_action or self.primary_action())
        button = self.query_one("#return-button", Button)
        button.display = bool(label)
        button.label = label or "无紧邻操作"
        button.disabled = target is None
        self.query_one("#task-operations", Grid).set_class(not label, "single")
        self.query_one("#detail-actions", TaskActions).set_class(not label, "single")

    def refresh_primary_action(self) -> None:
        action = self.primary_action()
        button = self.query_one("#accept-button", Button)
        button.label = self.primary_button_label(action)
        button.variant = "primary" if action.action in {"new_task", "edit_task", "assign", "start", "review", "return_work", "decisions"} else "default"
        button.disabled = False
        guidance = action.reason or {
            "review": "先核对成果与证据；打开报告不等于验收。",
            "start": "核对任务范围与执行者，明确确认后才派发。",
            "assign": "选择或新建执行者；分配不会启动。",
            "activity": "查看历史与原会话定位，不改变任务状态。",
            "new_task": "先记录目标与范围，之后再安排执行。",
            "read_report": "查看本轮报告与执行状态；阅读不会启动或验收。",
        }.get(action.action, "核对当前要求后再操作。")
        current = self.active_run(self.selected_task) if self.selected_task else None
        if current and action.action == "read_report":
            if current.state == "waiting" and action.label != "核实执行":
                guidance = "到原 Pi 处理确认；工作台不会代答。"
            elif current.state == "queued" and not action.reason:
                guidance = "等待接收回执；不要重复派发。"
            elif not action.reason:
                guidance = "等待停止回执；未确认前不要重派。" if action.label == "查看停止状态" else "暂未收到人工确认请求；等待进展，可按需补充。"
        heading = "需核实执行" if action.label == "核实执行" else "等待原 Pi 确认" if current and current.state == "waiting" else "等待停止回执" if action.label == "查看停止状态" else "下一步"
        content = Text(heading + "\n", style="bold #f1b778" if action.action == "return_work" or action.reason else "bold #79ccee")
        content.append(guidance, style="not bold #c5d1e2")
        if current and current.state == "working" and action.label == "查看进展" and self.selected_task and self.selected_task.execution == "pi":
            content = Text("正在执行 · 暂不需要你操作", style="bold #79ccee")
        next_action = self.query_one("#next-action", Static)
        if next_action.content != content:
            next_action.update(content)
        next_action.set_class(action.action in {"return_work", "decisions"} or
                              action.label in {"核实执行", "查看停止状态", "查看确认信息"}, "needs-attention")
        self._primary_action = action
        self._primary_task_id = self.selected_id
        self._primary_snapshot = self.primary_snapshot(self.selected_task)

    def action_add_project(self) -> None:
        if not self.board.live:
            return
        async def save(name: str, directory: str) -> None:
            exact = await asyncio.to_thread(canonical_directory, directory)
            self.apply_change(lambda board: board.add_project(name, exact))
        def saved(result) -> None:
            if result:
                nav = self.query_one("#nav-project", OptionList)
                nav.clear_options().add_options([Option("全部项目", id="all"), *(Option(name, id=name) for name in self.board.projects)])
                self.set_project(next(reversed(self.board.projects)))
                self.notify("已登记精确目录 · 尚未分配或启动 Agent")
        self.push_screen(ProjectForm(save), saved)

    def action_delete_project(self) -> None:
        options = {name: f"{name} · {path}" for name, path in self.board.projects.items() if name != "收集箱"}
        if not options:
            self.notify("没有可删除的项目；收集箱不能删除。", severity="warning")
        elif self.project_name in options:
            self.confirm_delete_project(self.project_name)
        else:
            self.push_screen(Choice("删除哪个项目的登记？", options), self.confirm_delete_project)

    def confirm_delete_project(self, name: str | None) -> None:
        if name is None:
            return
        try:
            self.board.project_tasks_for_deletion(name)
        except ValueError as error:
            self.notify(str(error), severity="warning", timeout=10)
            return
        def snapshot(board: Board) -> tuple:
            tasks = {key: task for key, task in board.tasks.items() if task.project == name}
            return board.projects.get(name), tasks, {key: run for key, run in board.runs.items() if run.task_id in tasks}
        expected = deepcopy(snapshot(self.board))
        body = (f"项目：{name}\n目录：{expected[0]}\n关联任务：{len(expected[1])} 条（含归档/回收站）\n\n"
                "只移除工作台中的项目登记，不删除磁盘目录或文件，不停止 Pi。\n"
                "全部关联任务移入回收站，报告、历史轮次和验收状态保留。恢复后归属收集箱，不恢复项目登记或自动启动。\n"
                "只有在回收站再次确认永久删除，才清除工作台中的任务与历史。\n\n请先核实相关后台工作已结束。")
        def confirmed(yes) -> None:
            if not yes:
                return
            def change(board: Board) -> None:
                if snapshot(board) != expected:
                    raise ValueError("项目、关联任务或执行记录在确认期间发生变化，请重新核对。")
                for task in board.project_tasks_for_deletion(name):
                    self.check_terminal_task(board, task)
                board.delete_project(name)
            try:
                self.apply_change(change)
            except (ValueError, StorageError) as error:
                self.notify(f"未保存：{error}", severity="error", timeout=10)
                return
            nav = self.query_one("#nav-project", OptionList)
            nav.clear_options().add_options([Option("全部项目", id="all"), *(Option(key, id=key) for key in self.board.projects)])
            self.project_name = "收集箱" if expected[1] else "all"
            self.status_filter = "trashed" if expected[1] else "all"
            self.selected_id = next(iter(expected[1]), None)
            self.detail_open = False
            self.query_one("#search", Input).value = ""
            self.refresh_tasks()
            self.apply_layout()
            self.focus_tasks()
            self.notify("项目登记已删除 · 任务及历史在回收站 · 磁盘文件未改动")
        self.push_screen(Confirm("删除项目 · 仅移除登记", body, "删除项目"), confirmed)

    def action_adopt_session(self) -> None:
        self.run_worker(self.open_adoption())

    async def open_adoption(self) -> None:
        runtime = self.runtime
        if not runtime or not self.store:
            self.notify("纳管需要真实任务库和明确连接；演示与只读观测不升级为控制。", severity="warning")
            return
        selected = self.selected_task
        project = selected.project if selected else self.project_name
        directory = self.board.projects.get(project, "")
        if not directory:
            self.notify("先选择已登记的真实项目目录，再纳管会话。", severity="warning")
            return
        agents = await asyncio.to_thread(runtime.discover)
        if not self.is_running or self.runtime is not runtime or len(self.screen_stack) > 1:
            return
        options = {agent.id: agent.label for agent in agents if agent.cwd == directory and not agent.active_run}
        if not options:
            self.notify("这个精确目录没有可纳管连接；先连接已有 Pi，不新建第二执行者。", severity="warning", timeout=10)
            return
        captured = {agent.id: agent for agent in agents}
        def chosen(key: str | None) -> None:
            if key:
                self.run_worker(self.adoption_form(captured[key], project, directory))
        self.push_screen(Choice("第一步 · 选择已有会话（不启动）", options,
                                "只列同目录、无已关联执行的连接；忙碌会话只等待当前回合结束。"), chosen)

    async def adoption_form(self, agent: Agent, project: str, directory: str) -> None:
        runtime = self.runtime
        if not runtime or not self.store or self.board.projects.get(project) != directory:
            return
        targets = {}
        for task in self.board.tasks.values():
            source = self.board.runs.get(task.run_id)
            if (task.project == project and task.execution == "pi" and task.retention == "active"
                    and task.status != "done" and task.description.strip()
                    and (not task.run_id or source and source.terminal
                         and (source.session_id, source.cwd) == (agent.session_id, directory))):
                targets[str(task.id)] = deepcopy(task)
        default = str(self.selected_id) if str(self.selected_id) in targets else "new"
        async def submit(key: str, title: str, goal: str, checkpoint: str, checked: bool) -> bool:
            if not self.store or self.runtime is not runtime or self.board.projects.get(project) != directory:
                raise BridgeError("任务库或目录已变化；请重新核对。")
            if not title.strip() or not goal.strip() or len(title) > 300 or len(goal) > 20000:
                raise BridgeError("请明确独立标题与目标，不从会话聊天猜测。")
            proposed = deepcopy(self.board)
            if key == "new":
                task = proposed.create(title, project, goal)
            else:
                expected = targets.get(key)
                if not expected or self.board.tasks.get(expected.id) != expected:
                    raise BridgeError("原任务已变化；不按旧选择关联。")
                task = proposed.tasks[expected.id]
            request = await asyncio.to_thread(runtime.prepare_adoption, proposed, task, agent, checkpoint,
                                              previous_checked=checked)
            body = (f"D{task.id:03} · {task.title}\n目标：{task.description}\n目录：{directory}\n"
                    f"Pi：{agent.name}\n会话：{agent.session_id}\n连接：{agent.id}\n"
                    f"检查点：{request.intent.checkpoint}\n\n"
                    "只建立任务关联：不创建Run、不注入模型消息、不重放旧工作、不继承旧预算。"
                    "忙碌会话只等待原回合完全结束；期间有新输入或身份变化则拒绝。"
                    "后续明确输入/派发才开始新轮；工具权限与人工验收仍独立。")
            if not await self.push_screen_wait(Confirm("第二步 · 确认纳管，不启动", body, "确认纳管（不启动）")):
                return False
            self._adoption_errors.pop(task.id, None)
            def persist(board: Board) -> None:
                if board.projects.get(project) != directory:
                    raise BridgeError("目录已变化；未发布。")
                if key == "new":
                    if board.next_task_id != task.id:
                        raise BridgeError("新任务身份已变化；保留输入重新核对。")
                    actual = board.create(title, project, goal)
                else:
                    actual = board.tasks[task.id]
                    if actual != targets[key]:
                        raise BridgeError("确认期间任务已变化；未发布。")
                board.request_adoption(actual, request.intent)
            self.apply_change(persist)
            self.selected_id = task.id
            try:
                await asyncio.to_thread(runtime.publish_adoption, request)
                self.notify("纳管请求已发布 · 等待会话回执；未启动、未派发工作。", timeout=10)
            except (ValueError, OSError) as error:
                self._adoption_errors[task.id] = f"发布未确认：{error}；保留原意图，不生成新ID重发。"
                self.notify(self._adoption_errors[task.id], severity="warning", timeout=12)
            self.refresh_tasks()
            self.schedule_poll()
            return True
        self.push_screen(AdoptionForm(agent, targets, default, submit), lambda _: self.refresh_tasks())

    async def choose_pi(self, task_id: int, *, start: bool = False, expected: Task | None = None) -> None:
        if not self.runtime:
            return
        task = self.board.tasks.get(task_id)
        if task is None or (expected is not None and task != expected):
            self.notify("任务已变化，请重新核对后选择执行者。", severity="warning")
            return
        directory = self.board.projects.get(task.project, "")
        if task.retention != "active" or task.status not in ("draft", "ready") or task.run != "none" or not directory:
            self.notify("先选真实项目；只能分配尚未启动的任务。", severity="warning")
            return
        if expected is None:
            expected = deepcopy(task)
        try:
            discovered = await asyncio.to_thread(self.runtime.discover)
        except (OSError, ValueError) as error:
            self.notify(f"读取 Pi 连接失败：{error}", severity="error")
            return
        if not self.is_running or len(self.screen_stack) > 1:
            return
        if (self.selected_id != task_id or self.board.tasks.get(task_id) != expected
                or self.board.projects.get(task.project, "") != directory):
            self.notify("任务或目录已变化；不会继续选择或派发。", severity="warning")
            return
        self.agents.update((agent.id, agent) for agent in discovered)
        options = {agent.id: agent.label for agent in discovered
                   if agent.cwd == directory and (not start or agent.available)}
        options["__new_pi__"] = "＋ 新建 Pi · 可选择直接启动本任务"
        options["__pi_help__"] = "接入已有 Pi · 查看帮助"
        def chosen(agent) -> None:
            if agent is None:
                return
            if (self.selected_id != task_id or self.board.tasks.get(task_id) != expected
                    or self.board.projects.get(task.project, "") != directory):
                self.notify("任务或目录已变化；不会自动新建、分配或派发。", severity="warning")
                return
            if agent == "__new_pi__":
                self.new_agent_for(task_id)
            elif agent == "__pi_help__":
                self.action_pi_help()
            elif agent:
                if start:
                    def assign(board: Board) -> None:
                        current = board.tasks.get(task_id)
                        if current is None or (expected is not None and current != expected):
                            raise BridgeError("任务已有变化，不会自动分配或启动。")
                        self.check_dependency_execution(board, current, agent_id=agent)
                        board.assign(current, agent)
                    try:
                        self.apply_change(assign)
                    except (ValueError, StorageError) as error:
                        self.notify(f"未分配：{error}；补充要求已保留。", severity="error", timeout=10)
                        return
                    self.run_worker(self.confirm_dispatch(task_id))
                else:
                    def assign_only(board: Board) -> None:
                        if (self.selected_id != task_id or board.tasks.get(task_id) != expected
                                or board.projects.get(task.project, "") != directory):
                            raise BridgeError("任务或目录已变化；不会更换执行者。")
                        self.check_dependency_execution(board, board.tasks[task_id], agent_id=agent)
                        board.assign(board.tasks[task_id], agent)
                    self.mutate(assign_only)
        self.push_screen(Choice("补充已保存 · 选择执行者后核对并开始" if start else "选择 / 新建 Pi · 分配不会启动任务", options,
                                f"只列精确目录匹配的连接：{directory}"), chosen)

    def action_new_agent(self) -> None:
        self.new_agent_for(self.selected_id)

    def new_agent_for(self, task_id: int | None) -> None:
        runtime = self.runtime
        if not runtime or not self.store:
            self.notify("新建 Pi 需要真实项目和本地任务库。", severity="warning")
            return
        task = deepcopy(self.board.tasks.get(task_id)) if task_id is not None else None
        if task and task.retention != "active":
            self.notify("请先恢复任务，再新建或分配 Pi。", severity="warning")
            return
        project = task.project if task else self.project_name
        directory = self.board.projects.get(project, "")
        if not directory:
            self.notify("先按 p 添加或选择一个真实项目目录。", severity="warning")
            return
        expected = task if task and task.execution == "pi" and task.run == "none" and task.status in ("draft", "ready") else None
        assigned: Task | None = None
        async def create(name: str, model: str, launch_id: str) -> Launch:
            if self.board.projects.get(project) != directory:
                raise BridgeError("项目目录已改变；请重新核对后创建。")
            if task and self.board.tasks.get(task.id) != task:
                raise BridgeError("任务在确认期间发生变化；不会创建 Pi，请重新核对。")
            return await asyncio.to_thread(create_pi, runtime, directory, name, model, launch_id)
        def ready(agent: Agent) -> None:
            nonlocal assigned
            self.agents[agent.id] = agent
            if expected:
                if self.board.tasks.get(expected.id) != expected:
                    raise BridgeError("原任务在新建期间已改变，不会自动更换执行者")
                assigned_task_id = expected.id
                def assign_created(board: Board) -> None:
                    self.check_dependency_execution(board, board.tasks[assigned_task_id], agent_id=agent.id)
                    board.assign(board.tasks[assigned_task_id], agent.id)
                self.apply_change(assign_created)
                assigned = deepcopy(self.board.tasks[assigned_task_id])
        async def start(agent: Agent) -> bool:
            if assigned is None or not self.is_running or self.runtime is not runtime:
                raise BridgeError("没有已保存的本任务分配，不能启动。")
            if self._sending or self.board.tasks.get(assigned.id) != assigned or self.board.projects.get(project) != directory:
                raise BridgeError("任务、目录或派发状态已改变；请重新确认。")
            fresh = await asyncio.to_thread(runtime.agent, agent.id)
            if (not fresh.available or (fresh.id, fresh.session_id, fresh.cwd) != (agent.id, agent.session_id, directory)
                    or not self.is_running or not form._can_update or self.runtime is not runtime
                    or self.board.projects.get(project) != directory):
                raise BridgeError("新 Pi 的空闲状态、心跳或会话身份已变化；不会派发。")
            return await self.dispatch_task(assigned, fresh, expected_requirements=start_requirements)
        start_requirements = ''
        if expected:
            try:
                start_requirements = self.board.compiled_requirements(self.board.tasks[expected.id])
            except ValueError as error:
                self.notify(f'只创建和分配 Pi；暂不可启动：{error}', severity='warning')
        can_start = bool(expected and has_valid_contract(source_base(start_requirements)))
        context = (f'D{expected.id:03} · {expected.title}\n\n独立目标：\n{expected.description}\n\n完整请求：\n{start_requirements}'
                   if expected else '')
        if expected and not can_start:
            context += '\n尚缺有效工作合同；此次仅创建/分配 Pi。返回后填写合同再启动。'
        form = NewAgentForm(directory, runtime, create, ready, task_title=task.title if task else '',
                            start_task=start if can_start else None, task_context=context)
        self.push_screen(form, lambda _: self.refresh_tasks())

    async def confirm_dispatch(self, task_id: int) -> None:
        if not self.runtime or self._sending or len(self.screen_stack) > 1:
            return
        current = self.board.tasks.get(task_id)
        if not current:
            return
        task = deepcopy(current)
        try:
            if not self.store:
                raise BridgeError('真实派发必须先启用本地数据库。')
            if task.retention != 'active' or task.status != 'ready' or task.run != 'none':
                raise BridgeError('请先分配 Pi；本轮未结束时不会重复派发。')
            self.check_new_round_task(self.board, current)
            self.check_dependency_execution(self.board, current, agent_id=task.agent)
            requirements = self.board.compiled_requirements(current)
            if not has_valid_contract(source_base(requirements)):
                if self.selected_id == task_id:
                    self.action_work_contract()
                return
            runtime = self.runtime
            agent = await asyncio.to_thread(runtime.agent, task.agent)
            if not agent.available or agent.cwd != self.board.projects[task.project]:
                raise BridgeError('Pi 必须空闲、心跳有效，并处于项目的精确目录。')
            agent = await asyncio.to_thread(runtime.check_dispatch, agent, requirements)
            self.check_dependency_execution(self.board, self.board.tasks[task_id], agent_id=agent.id, requirements=requirements)
            current = self.board.tasks.get(task_id)
            if (self.runtime is not runtime or current != task or current is None
                    or self.board.compiled_requirements(current) != requirements):
                raise BridgeError('任务或选定来源在核对期间变化；未保存、未派发。')
        except (ValueError, OSError) as error:
            self.notify(str(error), severity='warning', timeout=8)
            return
        if len(self.screen_stack) > 1:
            return
        self.agents[agent.id] = agent
        boundary = ('本轮只核对已有材料，不批准改写或试验。工具层没有强制只读；超范围权限请拒绝。'
                    if requirements.startswith(EVIDENCE_ONLY_PREFIX) else
                    '本轮可能按 Pi 现有工具权限读写文件和执行命令；旧验收与引用不是新许可。')
        body = (f'D{task.id:03} · {task.title}\n\n精确目录：{agent.cwd}\nPi：{agent.label}'
                f'\n连接：{agent.id}\n会话：{agent.session_id}\n\n独立目标：\n{task.description}'
                f'\n\n本次完整新请求（与提交一致）：\n{requirements}\n\n{boundary}'
                '\n模型次数/费用未知，可能消耗额度；Pi 权限仍须在原窗口确认。不会自动验收或重试。')
        def confirmed(yes) -> None:
            if yes:
                self.run_worker(self.dispatch_task(task, agent, expected_requirements=requirements))
        if question_question(task.remaining) is not None or proposal_question(task.remaining) is not None:
            feedback = prior_feedback_candidates(self.board, task)
            options = tuple((key, feedback_source_label(self.board, task_id, key, index, comment))
                            for key, index, comment in feedback)
            recovered = recover_feedback_draft(task.remaining, options)
            if recovered is None:
                self.notify('已存请求的旧问题来源或原文无法完整恢复；请按 b 查看只读参考并重新核对，未派发。', severity='warning', timeout=10)
                return
            purpose, user_text, selected_keys = recovered
            chosen = tuple(row for row in feedback if row[0] in selected_keys)
            cards = feedback_review_cards(self.board, task_id, chosen)
            source_details = (f'D{task_id:03} · {task.title}\n原任务范围：{task.description}\n'
                              f'连接：{agent.id}\n会话：{agent.session_id}\n精确目录：{agent.cwd}\n\n' +
                              '\n\n'.join(feedback_source_label(self.board, task_id, key, index, comment)
                                          for key, index, comment in chosen))
            if requirements != task.remaining:
                source_details += '\n完整发送文本另含已绑定来源/上下文；不是从旧批准推定新许可。'
            review = RequestReview('已存请求 · 最终核对，尚未发送', body,
                                   '发送本次追问' if purpose == 'question' else '发送提案请求',
                                   purpose='追问已有材料' if purpose == 'question' else '索取可审提案 · 仅补证',
                                   user_text=user_text, request=requirements,
                                   target_label=f'待派发新一轮 · {agent.name} · {agent.model}\n目录：{agent.cwd}',
                                   source_details=source_details, consequences=boundary,
                                   task_label=f"D{task.id:03} · {task.title}", selected_feedback=cards, unselected_feedback=len(feedback) - len(chosen))
            self.push_screen(review, confirmed)
        else:
            self.push_screen(Confirm('真实派发 · 请核对任务与权限', body, '保存意图并派发'), confirmed)

    async def dispatch_task(self, expected: Task, agent: Agent, *, supplement: tuple[str, str] | None = None,
                            decision: DecisionPreview | None = None, expected_requirements: str | None = None,
                            progress: Callable[[str], None] | None = None) -> bool:
        """Return confirmed request publication, never execution/acceptance completion."""
        if self._sending or not self.runtime:
            return False
        self._sending = True
        runtime = self.runtime
        run = None
        def stage(message: str) -> None:
            self.set_submission_stage(expected.id, message)
            if progress:
                progress(message)
        try:
            stage("核对来源与本次请求（尚未写入派发意图）")
            if self.board.tasks.get(expected.id) != expected:
                raise BridgeError("任务在确认期间发生变化；请重新核对后派发。")
            if decision is not None:
                validate_preview(self.board, decision)
            dependency_purpose = "evidence" if decision is not None and request_only_decisions(decision.specs, decision.answers) else None
            base = decision.compiled_text if decision else supplement[1] if supplement else expected.remaining
            current = self.board.tasks[expected.id]
            self.check_new_round_task(self.board, current)
            requirements = self.board.compiled_requirements(current, base)
            self.check_dependency_execution(self.board, current, agent_id=agent.id, requirements=requirements, purpose=dependency_purpose)
            if expected_requirements is not None and requirements != expected_requirements:
                raise BridgeError('来源或完整请求在最后确认后变化；未保存、未派发。')
            if decision is None and not has_valid_contract(source_base(requirements)):
                raise BridgeError('缺完整工作合同；仅保存草稿，不能真实派发。')
            stage("核对目标与能力（尚未写入派发意图）")
            agent = await asyncio.to_thread(runtime.check_dispatch, agent, requirements, structured=decision is not None)
            current = self.board.tasks.get(expected.id)
            if (self.runtime is not runtime or current != expected or current is None
                    or self.board.compiled_requirements(current, base) != requirements):

                raise BridgeError("能力核对期间任务或运行时发生变化；未保存、未派发。")
            def prepare(board: Board) -> Run:
                task = board.tasks[expected.id]
                self.check_new_round_task(board, task)
                self.check_dependency_execution(board, task, agent_id=agent.id, requirements=requirements, purpose=dependency_purpose)
                if decision is not None:
                    if supplement is not None or decision.target.kind != "new_run":
                        raise BridgeError("决策派发目标无效。")
                    self.check_terminal_task(board, task)
                    prepared = prepare_decision_intent(board, decision)
                    assert prepared.run is not None
                    return prepared.run
                if supplement is not None:
                    mode, text = supplement
                    if mode == "next":
                        self.check_terminal_task(board, task)
                    board.save_supplement(task, text, mode)
                    board.assign(task, agent.id)
                return board.queue_real(task, str(uuid4()), agent.session_id, agent.cwd)
            def checked_prepare(board: Board) -> Run:
                candidate = prepare(board)
                if candidate.requirements != requirements:
                    raise BridgeError("提交要求与最后核对文本不一致；未保存、未派发。")
                return candidate
            stage("保存派发意图（尚未确认发布）")
            run = self.apply_change(checked_prepare)
            # Write-ahead intent above must commit before anything can reach Pi.
            stage("核对发布目标并发布请求（意图已保存；尚未确认接收）")
            await asyncio.to_thread(self.runtime.dispatch, run)
            self._published_runs.add(run.identity)
            stage("请求已发布；等待接收确认，不代表开始或完成")
            self.notify("派发请求已发布 · 等待 Pi 对应消息确认，不会自动重试")
            return True
        # The exception type is a tuple; only the recovery body has boolean conditions.
        # pi-lens-ignore: ast-grep:no-boolean-in-except
        except (ValueError, OSError, StorageError) as error:
            if run is not None:
                stage("发布未确认；可能已经发布，请勿重试")
                self.record_runtime_warning(run, f"派发未确认：{error}。可能已发布；保留 Run 身份，仅观察，不会自动重试。")
            elif supplement is not None or decision is not None:
                raise  # Nothing was committed: keep the editor and its input available.
            else:
                self.notify(f"未派发：{error}", severity="error", timeout=10)
            return False
        finally:
            self._sending = False
            self.set_submission_stage(expected.id, "")
            self.refresh_tasks()
            self.schedule_poll()

    def record_runtime_warning(self, run: Run, message: str) -> None:
        self.runtime_errors[run.id] = message
        try:
            self.apply_change(lambda board: board.note(board.tasks[run.task_id], "warning", message, run_id=run.id, source="system"))
        except (ValueError, StorageError) as error:
            message += f"\n警告也未能保存：{error}；原意图仍保留，请勿重派。"
        self.notify(message, severity="error", timeout=12)

    def schedule_poll(self) -> None:
        if not self.is_running:
            return
        if self._visual_dirty and len(self.screen_stack) == 1:
            self.refresh_tasks()
        if self.runtime and not self._polling:
            self._polling = True
            self.run_worker(self.poll_runtime())

    async def poll_runtime(self) -> None:
        runtime = self.runtime
        if runtime is None:
            self._polling = False
            return
        try:
            adoption_batch = await asyncio.to_thread(runtime.read_adoptions, deepcopy(self.board))
            if not self.is_running or self.runtime is not runtime:
                self._polling = False
                return
            old_states, old_errors = self._adoption_states, self._adoption_errors
            self._adoption_states = {key: value.state for key, value in adoption_batch.receipts.items()}
            self._adoption_errors = adoption_batch.errors
            needs_save = any(not adoption_confirmed(self.board.tasks[key], adoption_batch.requests[key].intent)
                             and value.state in {"attached", "consumed"}
                             for key, value in adoption_batch.receipts.items())
            needs_save |= any(incoming.id not in self.board.runs or incoming.seq > self.board.runs[incoming.id].seq
                              for _, incoming in adoption_batch.runs)
            needs_save |= any(value.state in {"rejected", "detached"} and not any(
                event and event.kind == "session_adoption_rejected" and event.text == value.id
                for raw in self.board.tasks[key].events if (event := readable_event(raw)) is not None)
                for key, value in adoption_batch.receipts.items())
            if needs_save:
                def ingest_adoptions(board: Board) -> None:
                    for key, receipt in adoption_batch.receipts.items():
                        request = adoption_batch.requests[key]
                        board.apply_adoption(request.intent, receipt)
                    for request, incoming in adoption_batch.runs:
                        board.ingest_adopted(request, incoming)
                self.apply_change(ingest_adoptions)
                await asyncio.to_thread(runtime.read_adoptions, deepcopy(self.board))  # Bind telemetry only after consent commits.
            if needs_save or old_states != self._adoption_states or old_errors != self._adoption_errors:
                self.refresh_tasks()
        except (ValueError, OSError, StorageError) as error:
            self.notify(f"纳管回执未保存：{error}；仅重读，不重新发布。", severity="warning", timeout=10)
        tracked = [deepcopy(self.board.runs[task.run_id]) for task in self.board.tasks.values()
                   if task.execution == "pi" and task.run_id in self.board.runs
                   and (task.run != "none" or (intent := latest_adoption(task)) is not None
                        and adoption_confirmed(task, intent))
                   and task.status != "done" and task.retention == "active"]
        ancestors = {old.id: old for run in tracked for old in self.board.lineage(run)
                     if not runtime.observation_only(run) or runtime.observation_only(old)}
        notes = [(deepcopy(ancestors[note.run_id]), note) for note in self.board.supplements.values()
                 if note.run_id in ancestors and note.status in {"pending", "queued", "received"}]
        if not tracked:
            self._polling = False
            return
        selected = self.selected_task
        target = next((run for run in tracked if not run.terminal and selected and run.id == selected.run_id), None)
        try:
            batch = await asyncio.to_thread(runtime.read_updates, tracked, notes, target)
            updates, agents, errors = batch.runs, batch.agents, batch.errors
            observation, activity_issue = batch.activity, batch.activity_issue
            receipts, note_errors = batch.supplements, batch.supplement_errors
            if not self.is_running:
                return  # A completed read must not update removed widgets or a closing store.
            selected = self.selected_task
            if target and selected and selected.run != "none" and selected.run_id == target.id:
                previous = self.activity if self.activity_run_id == target.id else None
                self.activity = previous
                self.activity_run_id = target.id
                if observation is not None:
                    try:
                        observation.check_after(previous)
                        self.activity = observation
                    except ValueError:
                        activity_issue = "活动修订倒退或改写"
                elif previous is not None:
                    activity_issue = activity_issue or "活动快照暂不可用"
                self.activity_issue = activity_issue
            changed = False
            self.supplement_errors = note_errors
            self.session_errors = batch.session_errors
            for run in tracked:
                snapshot = batch.sessions.get(run.id)
                if snapshot:
                    try:
                        prior = self.sessions.get(run.id)
                        if (prior and prior.agent_id != snapshot.agent_id
                                and runtime.observation_identity(run) == (snapshot.agent_id, snapshot.session_id, snapshot.cwd)):
                            prior = None  # A proved new transport has its own revision counter.
                        snapshot.check_after(prior)
                        self.sessions[run.id] = snapshot
                    except ValueError:
                        self.session_errors[run.id] = "Pi 会话观测修订倒退或改写"
                elif run.id in self.sessions:
                    self.session_errors[run.id] = "Pi 会话快照暂不可用"
                capability_issue = self.report_capability_issue(self.board.tasks[run.task_id])
                if self._report_capability_issues.get(run.id) != capability_issue:
                    changed = True
                self._report_capability_issues[run.id] = capability_issue
                before = self.agents.get(run.agent_id)
                after = agents.get(run.agent_id)
                changed |= (before is None) != (after is None) or bool(before and after and (
                    before.available != after.available
                    or (before.online, before.session_id, before.cwd, before.idle, before.active_run, before.name, before.model)
                    != (after.online, after.session_id, after.cwd, after.idle, after.active_run, after.name, after.model)))
                if after:
                    self.agents[run.agent_id] = after
                    self.agents[after.id] = after
                else:
                    self.agents.pop(run.agent_id, None)
                if errors.get(run.id) != self.runtime_errors.get(run.id):
                    changed = True
                if run.id in errors:
                    self.runtime_errors[run.id] = errors[run.id]
                else:
                    self.runtime_errors.pop(run.id, None)
            if updates or receipts or batch.continuations:
                def ingest(board: Board) -> list[tuple[Run, Run]]:
                    for incoming in updates:
                        board.ingest(incoming)
                    for receipt in receipts:
                        board.apply_supplement(receipt)
                    adopted = []
                    for parent, child in batch.continuations:
                        task = board.tasks.get(parent.task_id)
                        if not task or task.run_id != parent.id or task.run == "none" or task.status == "done" or task.retention != "active":
                            continue  # A concurrent human decision is not undone by a late read.
                        board.continue_from(parent.id, child)
                        adopted.append((parent, child))
                    return adopted
                adopted = self.apply_change(ingest)
                for parent, child in adopted:
                    runtime.inherit_observation(parent, child)
                    if parent.id in self.sessions:
                        self.sessions[child.id] = self.sessions[parent.id]
                self._poll_save_error = ""
                changed = True
            if changed:
                self.refresh_tasks()
            else:
                self.refresh_status()
        except (ValueError, StorageError) as error:
            message = f"进展未保存：{error}；保留旧修订，下次仅重读结果，不会重新执行。"
            if message != self._poll_save_error:
                self.notify(message, severity="error", timeout=12)
                self._poll_save_error = message
        finally:
            self._polling = False

    def action_stop(self) -> None:
        task = self.selected_task
        run = self.active_run(task) if task else None
        if not run or not self.runtime:
            self.notify("没有可请求停止的真实执行。", severity="warning")
            return
        if self.runtime.observation_only(run):
            self.notify("本轮是只读观测；请到原 Pi 处理停止。", severity="warning")
            return
        def confirmed(yes) -> None:
            if yes:
                self.run_worker(self.stop_run(run))
        self.push_screen(Confirm("请求停止本轮 Pi 执行", f"Run {run.id}\n会话 {run.session_id}\n目录 {run.cwd}\n\n仅停止工作台派发且仍能确认归属的这一轮；不退出 Pi，不中断其他工作。\n停止请求 ≠ 已停止，仍需等待 Pi 确认完全空闲。", "请求停止"), confirmed)

    async def stop_run(self, expected: Run) -> None:
        if self._sending or not self.runtime:
            return
        if self.runtime.observation_only(expected):
            self.notify("本轮是只读观测；请到原 Pi 处理停止。", severity="warning")
            return
        self._sending = True
        try:
            run = self.board.runs[expected.id]
            if run.terminal or self.board.tasks[run.task_id].run_id != run.id:
                raise BridgeError("这轮执行已结束或不再是当前轮次。")
            self.apply_change(lambda board: board.note(board.tasks[run.task_id], "stop_requested", "人工请求停止 · 等待 Pi 确认", run_id=run.id))
            await asyncio.to_thread(self.runtime.stop, run)
            self.notify("停止请求已发布 · 尚未确认结束")
        except (ValueError, OSError, StorageError) as error:
            self.record_runtime_warning(expected, f"停止未确认：{error}；请去 Pi 核实，不会当作已停止。")
        finally:
            self._sending = False
            self.refresh_tasks()
            self.schedule_poll()

    def action_abandon(self) -> None:
        task = self.selected_task
        run = self.active_run(task) if task else None
        if not task or not run:
            return
        def confirmed(yes) -> None:
            if yes:
                self.push_screen(Note("我已核实旧 Pi 停止 · 记录核实情况", "请写明如何确认旧 Pi 和可能的子进程已停止…",
                                      save=lambda reason: self.apply_change(lambda board: board.abandon(board.tasks[run.task_id], reason)), live=True),
                                 lambda result: self.refresh_tasks() if result else None)
        self.push_screen(Confirm("人工解除未知执行 · 不会停止任何进程", f"Run {run.id}\n旧会话 {run.session_id}\n目录 {run.cwd}\n\n仅在你已到旧 Pi 核实该轮及其子进程确实停止后使用。\n失联不代表停止；若还在运行，解除后重派会重复执行。\n尚未消费的派发请求最多 30 秒过期：先断开旧 Pi，再等待 30 秒。\n\n确认后还需填写核实情况；不会自动重派。", "我已核实旧 Pi 停止"), confirmed)

    def action_pi_help(self) -> None:
        project = self.selected_task.project if self.selected_task else self.project_name
        directory = self.board.projects.get(project, "")
        workbench = Path(__file__).resolve().parent
        body = f"只会发现主动 /waggle connect 的 Pi，不扫描或接管现有会话。\n\n在此工作台目录：\n{workbench}\n\n已有 Pi：\njust install-bridge '/你的精确项目目录'\n然后在该 Pi 中 /reload，再 /waggle connect 并确认。\n只新增项目 .pi/extensions/waggle.ts；已有不同内容则拒绝覆盖。\nPi 必须信任该项目才能加载；若没有 /waggle，用下面显式启动方式。\n工作台不替你更改信任配置。\n\n新开 Pi：\njust pi '/你的精确项目目录'\n然后 /waggle connect 并确认。\n\n当前项目目录：{directory or '先按 p 添加项目'}\n在工作台按 a 选择连接，s 核对任务并真实派发。\n每次重启、换会话、/reload 都要重新连接，旧任务不会自动重派。\n权限提示请在 Pi 中处理；工作台不会自动回答。\n\n断开：Pi 完全结束本轮后 /waggle disconnect。\n不修改全局配置、不影响原 Code Board。"
        self.push_screen(Confirm("明确接入 Pi", body, "知道了"))

    def action_assign(self) -> None:
        task = self.selected_task
        if task and task.execution == "pi":
            self.run_worker(self.choose_pi(task.id))
        elif task:
            self.push_screen(Choice("分配执行者 · 不会自动启动", {agent: agent for agent in AGENTS}), lambda agent: self.mutate(lambda board: board.assign(board.tasks[task.id], agent)) if agent else None)

    def action_start(self) -> None:
        task = self.selected_task
        if task and task.execution == "pi":
            self.run_worker(self.confirm_dispatch(task.id))
            return
        if task:
            body = f"任务  D{task.id:03} · {task.title}\n\n项目  {task.project}\n执行者  {task.agent or '未分配'}\n精确样例目录\n{self.board.projects[task.project]}\n\n此操作只模拟排队，不启动进程、不读取目录。\n排队后按 g 模拟执行者确认；不会自动进入运行中。"
            self.push_screen(Confirm("模拟启动 · 请核对安排", body, "模拟排队"), lambda yes: self.mutate(lambda board: board.queue(board.tasks[task.id])) if yes else None)

    def simulation_options(self) -> dict[str, str]:
        task = self.selected_task
        if task and task.execution == "pi":
            return {}
        if task and task.run == "queued":
            return {"ack": "模拟执行者确认 → 运行中"}
        if task and task.run == "working":
            return {"progress": "模拟进度更新（保持选择 / 输入）", "block": "模拟阻塞 · 填写原因", "partial": "模拟阶段交付 · 不可整任务验收", "full": "模拟完整交付 → 等待人工验收"}
        return {}

    def action_simulate(self) -> None:
        task = self.selected_task
        if task and task.execution == "pi":
            self.schedule_poll()
            self.notify("正在读取已保存的 Pi 进展；不会重新派发。")
            return
        options = self.simulation_options()
        if not task or not options:
            self.notify("当前没有可推进的模拟执行；先安排并显式模拟启动。", severity="warning")
            return
        def chosen(action) -> None:
            if action == "block":
                self.push_screen(Note("模拟阻塞", "缺少什么信息？需要谁做什么？", save=lambda reason: self.apply_change(lambda board: board.simulate(board.tasks[task.id], "block", reason))), lambda result: self.refresh_tasks() if result else None)
            elif action:
                self.mutate(lambda board: board.simulate(board.tasks[task.id], action))
        self.push_screen(Choice("推进模拟 · 不会执行任何真实工作", options), chosen)

    def action_more_tools(self) -> None:
        task = self.selected_task
        if task is None or len(self.screen_stack) > 1:
            return
        selected_id = task.id
        options = {"activity": "记录 · 历史与原 Pi 定位（只读）",
                   "toggle_detail": "收起详情" if self.detail_expanded else "展开详情 · 完整要求与报告",
                   "steps": "任务步骤 · 人工记录（不派发）"}
        if task.execution == "pi":
            options["evidence"] = "成果库存 · 选定引用 / 独立人工核验（不派发）"
            options["dependencies"] = "局部依赖 · 条件核对 / 原写者释放 / 独立接收（不派发）"
            options["budget"] = "累计预算账本 · 独立核验（正式批准未启用）"
            options["adopt_session"] = "纳管已有会话 · 接续 / 独立任务（不启动）"
        if task.status == "done" and task.execution == "pi":
            options["followup"] = "基于已验收成果创建后续草稿（不派发）"
        if task.status in ("draft", "ready") and task.run == "none":
            options["work_contract"] = "本轮工作合同 · 仅保存 / 核对派发"
        source_run = self.board.runs.get(task.run_id)
        if source_run and source_run.terminal and task.status != 'done' and task.retention == 'active':
            options['prepare_recovery'] = '准备恢复草稿（仅保存，不重试）'
        def chosen(key: str | None) -> None:
            if not key:
                return
            if self.selected_id != selected_id or selected_id not in self.board.tasks:
                self.notify("任务已变化；请重新打开更多操作。", severity="warning")
                return
            action = {"activity": self.action_activity, "toggle_detail": self.action_toggle_detail,
                      "steps": self.action_steps, "followup": self.action_followup, "evidence": self.action_evidence,
                      "dependencies": self.action_dependencies, "budget": self.action_budget,
                      "work_contract": self.action_work_contract, "prepare_recovery": self.action_prepare_recovery,
                      "adopt_session": self.action_adopt_session}.get(key)
            if action:
                action()
        self.push_screen(Choice(f"D{selected_id:03} · 更多任务工具", options,
                                "记录/阅读不改变任务；步骤只在本地跟踪，不发送给 Pi。"), chosen)

    def action_toggle_detail(self) -> None:
        if not self.selected_task or len(self.screen_stack) > 1:
            return
        self.detail_expanded = not self.detail_expanded
        self.detail_open = True
        self.show_detail()
        self.apply_layout()
        scroll = self.query_one("#detail-scroll", VerticalScroll)
        scroll.scroll_home(animate=False)
        scroll.focus()

    def action_read_report(self) -> None:
        task = self.selected_task
        if task is None or len(self.screen_stack) > 1:
            return
        current = self.board.runs.get(task.run_id)
        runs = [run for run in self.board.runs.values() if run.task_id == task.id]
        addendum = inventory_text(self.board, task) if any(i.owner_task_id == task.id for i in self.board.evidence_items.values()) else ""
        if latest_dependencies(self.board, task.id):
            addendum += "\n" + dependency_text(self.board, task, durable=self.store is not None)
        if current and task.run != "none":
            addendum += self.supplement_details(current.id).plain
            # A live observer failure does not alter an ended attempt's immutable report.
            if not current.terminal and (warning := self.runtime_errors.get(current.id)):
                addendum += f"\n连接提示 · 打开时状态\n{warning}\n"
        body = report_text(task, current, runs, addendum=addendum)
        self.push_screen(ReportReader(f"D{task.id:03} · {task.title}", body))

    def action_activity(self) -> None:
        task = self.selected_task
        if task:
            self.push_screen(TaskRecords(self.board, task, show_location=lambda body: self.push_screen(Confirm("原 Pi 会话 · 只读定位", body, "返回记录"))))

    def action_review(self) -> None:
        task = self.selected_task
        if not task:
            return
        if not task.can_accept:
            self.notify("当前不能验收：需要完整交付，且没有剩余工作。", severity="warning")
            return
        report = delivery_text(task, self.board.runs.get(task.run_id)).plain + self.supplement_details(task.run_id).plain
        evidence = "Pi 本轮已结束。请核实以上实际成果与范围；只有你明确确认，才标记本任务已验收。" if task.execution == "pi" else "证据全部为虚构，不代表真实测试通过；确认仅验收本地样例。"
        expected = deepcopy(task)
        expected_run = deepcopy(self.board.runs.get(task.run_id))
        def accept(board: Board) -> None:
            current = board.tasks.get(expected.id)
            if current != expected or current is None or board.runs.get(current.run_id) != expected_run:
                raise ValueError("报告或轮次在确认期间发生变化；请重新阅读后验收。")
            self.check_terminal_task(board, current)
            board.accept(current)
        self.push_screen(Confirm("人工验收" if task.execution == "pi" else "人工验收样例", f"D{task.id:03}\n{report}\n{evidence}", "确认验收"), lambda yes: self.mutate(accept) if yes else None)

    def supplement_details(self, run_id: str) -> Text:
        text = Text()
        notes = self.board.run_supplements(run_id)
        if notes:
            text.append("\n本轮补充说明 · 完整保存内容\n", style="bold #79ccee")
            for number, note in enumerate(notes, 1):
                text.append(f"{number}. {SUPPLEMENT_STATES[note.status]} · {note.id}\n{note.text}\n\n", style="#c5d1e2")
        run = self.board.runs.get(run_id)
        missing = self.board.uninherited_supplements(run) if run else []
        if missing:
            text.append("\n此前未纳入本轮的补充 · 不能验收\n", style="bold #f1b778")
            for note in missing:
                text.append(f"原轮次 {note.run_id} · {SUPPLEMENT_STATES[note.status]}\n{note.text}\n\n", style="#c5d1e2")
        return text

    def decision_entry(self, task: Task | None) -> bool:
        if not task or task.execution != "pi":
            return False
        run = self.board.runs.get(task.run_id)
        if task.run != "none" and run and run.delivery == "progress":
            try:
                decision_source(self.board, task.id)
            except ValueError:
                pass  # An ordinary progress report does not imply missing human decisions.
            else:
                return True
        return bool((task.run != "none" and run and run.delivery in {"blocked", "partial"})
                    or any(r.task_id == task.id for r in self.board.decisions.values()))

    def action_decision_history(self) -> None:
        task = self.selected_task
        if not task:
            return
        records = {r.id: r for r in self.board.decisions.values() if r.task_id == task.id}
        labels = {"draft": "草稿，未确认", "saved": "正式决定，仅保存", "new_run": "已保存新轮意图", "live_supplement": "已保存本轮补充意图"}
        if not records:
            self.push_screen(Confirm("人工决定记录", "没有结构化人工确认记录；旧说明和 Agent 对同意的解释不算正式批准。", "返回"))
            return
        def selected(record_id) -> None:
            if record_id in records:
                record = records[record_id]
                body = f"{labels[record.intent.kind]} · {record.confirmed_at or '尚未确认'}\n记录 {record.id}\n来源 {record.preview.source.run_id}\n关联执行 {record.intent.run_id or '无'}\n关联补充 {record.intent.supplement_id or '无'}\n历史只读；保存/发布不等于收到、执行或完成。\n\n{record.preview.compiled_text}"
                self.push_screen(Confirm("历史人工决定 · 只读", body, "返回"))
        self.push_screen(Choice("人工决定 / 草稿记录 · 不以时间推断执行", {r.id: f"{index}. {labels[r.intent.kind]} · {r.confirmed_at or '草稿'}" for index, r in reversed(list(enumerate(records.values(), 1))) }), selected)

    def action_decisions(self, focus_key: str = "") -> None:
        task = self.selected_task
        if not task or len(self.screen_stack) > 1:
            return
        try:
            form = DecisionSheet(self.board, task.id, self.prepare_decision_sheet, focus_key=focus_key)
        # The exception type is ValueError, not the boolean condition in its body.
        # pi-lens-ignore: ast-grep:no-boolean-in-except
        except ValueError:
            history_only = any(r.task_id == task.id for r in self.board.decisions.values())
            if task.status == "done" or task.retention != "active" or history_only:
                self.action_decision_history()
                return
            expected = deepcopy(task)
            body = ("当前报告没有有效结构化决策材料。不会把旧报告的推荐、‘我接受’或 Agent 的解释转为批准。\n\n"
                    "可要求执行者只整理材料：问题/原因/责任人、依据与缺口、可比较选项、风险/成本/范围、推荐理由和排除项。\n"
                    "下一屏可编辑并核对请求，再明确提交；这不批准研究、修改或预算。历史决定可从 Ctrl+K → 决策记录查看。")
            def request(yes) -> None:
                if yes and self.board.tasks.get(expected.id) == expected:
                    self.run_worker(self.request_decision_materials(expected.id))
            self.push_screen(Confirm("决策单 · 先补齐材料", body, "整理请求并核对"), request)
        else:
            self.push_screen(form, lambda _: self.refresh_tasks())

    async def request_decision_materials(self, task_id: int) -> None:
        task = self.board.tasks.get(task_id)
        if not task or self.selected_id != task_id:
            return
        request = ("请仅基于已有材料整理待用户决定的事项，通过 waggle_report 的 decisions 字段报告。"
                   "每项包含稳定 key、类型、问题/原因/责任人、依据与缺材料、可比较选项及收益/风险/成本/范围、推荐及理由、排除项。"
                   "预算写明次数、分钟、MiB 上限和停止条件；偏好不是授权。材料不足须明确列出，不替用户批准。"
                   "本次只整理决策材料，不授权新的研究试验、文件修改、下载或预算消耗；旧普通文字不得升级为批准。")
        try:
            mode = self.board.supplement_mode(task)
            if mode == "live":
                await self.open_live_supplement(task_id, initial=request)
            elif task.execution == "pi":
                await self.open_dispatch_supplement(task_id, initial=request, intent="proposal")
        except (ValueError, OSError) as error:
            self.notify(str(error), severity="warning")

    async def prepare_decision_sheet(self, form: DecisionSheet, kind: str, answers: tuple[Answer, ...]) -> None:
        runtime = self.runtime
        if not self.store or form.basis is None:
            raise BridgeError("正式决定和草稿需要可保存的本地任务库。")
        def check() -> Task:
            if not self.is_running or not form.is_running or self.runtime is not runtime:
                raise BridgeError("窗口或运行时已变化；不会提交。")
            assert form.basis is not None
            validate_preview(self.board, form.basis)
            event = report_view_event(form.report_view)
            assert event.report is not None
            if (form.report_view.source != form.basis.source or event.report.decisions != form.specs
                    or event != decision_source(self.board, form.task_id)
                    or self.board.runs[form.basis.source.run_id].events.count(form.report_view.source_bytes) != 1):
                raise BridgeError("原报告展示与当前来源不一致；保留输入，请重新核对。")
            return self.board.tasks[form.task_id]
        task = check()
        form.set_processing_stage("核对来源与本单答案（尚未提交）")
        if kind == "feedback":
            answers = feedback_answers(form.specs, answers)
        target = Target("continue" if kind == "feedback" else kind)
        label = ("保存草稿 · 不批准、不发送、不启动" if kind == "draft" else
                 "仅保存反馈 · 不批准、不发送、不启动" if kind == "saved" and all(a.kind == "undecided" for a in answers)
                 and any(a.comment.strip() for a in answers) else
                 "确认业务答案，仅本地保存 · 不批准、不发送、不启动" if any(a.business for a in answers)
                 and not any(a.kind in {"choice", "approve", "budget"} for a in answers) else
                 "确认补证意见/倾向，仅本地保存 · 不批准、不发送、不启动" if any(not s.complete or s.kind == "budget" for s in form.specs)
                 else "确认决定，仅本地保存 · 不发送、不启动")
        if kind in {"continue", "feedback"}:
            request_only = request_only_decisions(form.specs, answers)
            if kind == "feedback" and not request_only:
                raise BridgeError("反馈不能成为方案批准或执行授权。")
            if not runtime or self._sending:
                raise BridgeError("运行时不可用或已有提交；可以保存草稿/仅保存决定。")
            form.set_processing_stage("核对当前轮与目标（尚未提交）")
            mode = self.board.supplement_mode(task)
            original = self.board.runs[task.run_id]
            self.check_decision_observation(task, original)
            if mode == "live":
                await asyncio.to_thread(runtime.check_supplement, deepcopy(original))
                check()
                target = Target("live_supplement", original.agent_id, original.session_id, original.cwd)
            elif mode == "next":
                self.check_new_round_task(self.board, task)
                self.check_dependency_execution(self.board, task, purpose="evidence" if request_only else "implementation")
                directory = self.board.projects[task.project]
                agent = None
                try:
                    candidate = await asyncio.to_thread(runtime.agent, task.agent)
                    if candidate.available and candidate.cwd == directory:
                        agent = candidate
                except (ValueError, OSError):
                    pass  # An unavailable old agent is not a guessed dispatch target.
                check()
                if agent is None:
                    discovered = await asyncio.to_thread(runtime.discover)
                    check()
                    candidates = {a.id: a for a in discovered if a.available and a.cwd == directory}
                    selected = await self.push_screen_wait(Choice("决定尚未提交 · 选择执行者", {**{a.id: a.label for a in candidates.values()}, "__new__": "＋ 新建 Pi · 仅连接后回到决定核对"}, f"精确目录：{directory}"))
                    check()
                    if selected == "__new__":
                        created: list[Agent] = []
                        async def create(name: str, model: str, launch_id: str) -> Launch:
                            check()
                            return await asyncio.to_thread(create_pi, runtime, directory, name, model, launch_id)
                        def ready(value: Agent) -> None:
                            check()
                            created.append(value)
                            self.agents[value.id] = value
                        await self.push_screen_wait(NewAgentForm(directory, runtime, create, ready, task_title=task.title))
                        check()
                        agent = created[-1] if created else None
                    else:
                        agent = candidates.get(selected)
                    if agent is None:
                        return
                target = Target("new_run", agent.id, agent.session_id, agent.cwd)
                self.agents[agent.id] = agent
            else:
                raise BridgeError("本轮不能继续；可仅保存决定，核实执行状态后再提交。")
            heading = ("反馈／请求建议 · 仅补证 · 发送到当前执行" if target.kind == "live_supplement" else "反馈／请求建议 · 仅补证 · 开始新一轮") if kind == "feedback" else ("仅补证 · 发送到当前执行" if target.kind == "live_supplement" else "仅补证 · 开始新一轮") if request_only else ("继续当前执行" if target.kind == "live_supplement" else "开始新一轮")
            warning = ("只核实已有材料；旧要求仅供核对，不授权新增研究、试验、改文件或下载。仍可能读取文件、运行模型并消耗额度；Pi 权限确认保留。" if request_only
                       else "此操作明确提交给该 Pi，可能读写文件、运行命令和使用模型额度；仅限已批准范围，Pi 权限确认保留。")
            label = f"{heading}\n连接：{target.agent_id}\n会话：{target.session_id}\n目录：{target.cwd}\n{warning}"
        check()
        preview = preview_decisions(self.board, task.id, answers, target)
        if runtime and target.kind in {"new_run", "live_supplement"}:
            self.check_dependency_execution(self.board, task, agent_id=target.agent_id, requirements=preview.compiled_text,
                                            purpose="evidence" if request_only_decisions(preview.specs, preview.answers) else None)
            form.set_processing_stage("核对结构化报告能力（尚未提交）")
            candidate = await asyncio.to_thread(runtime.agent, target.agent_id)
            await asyncio.to_thread(runtime.check_report_capabilities, candidate, preview.compiled_text, structured=True)
            check()
            heading, rest = label.split("\n", 1)
            label = f"{heading}\n接收方：{candidate.name} · {candidate.model}\n{rest}"
        review = DecisionReview(preview, label, self.submit_decision_preview, report_view=form.report_view)
        form.set_processing_stage("等待你最后核对（尚未提交）")
        confirmed = await self.push_screen_wait(review)
        if confirmed and form.is_running:
            form.dismiss(True)

    async def submit_decision_preview(self, preview: DecisionPreview) -> None:
        runtime = self.runtime
        if (not self.store or not isinstance(self.screen, DecisionReview) or self.screen.preview is not preview
                or not self.screen.busy or self._sending):
            raise BridgeError("需要在人工汇总核对窗口明确确认；不会从报告或普通文本建立批准。")
        review = self.screen
        def check_display() -> None:
            validate_preview(self.board, preview)
            event = report_view_event(review.report_view)
            assert event.report is not None
            if (review.report_view.source != preview.source or event.report.decisions != preview.specs
                    or event != decision_source(self.board, preview.task_id)
                    or self.board.runs[preview.source.run_id].events.count(review.report_view.source_bytes) != 1):
                raise BridgeError("原报告展示与本次确认来源不一致；保留输入，不保存或发送。")
        review.set_processing_stage("重新核对来源与本次答案")
        check_display()
        if preview.target.kind in {"draft", "saved"}:
            review.set_processing_stage("保存本地记录（不发送）")
            self.apply_change(lambda board: prepare_decision_intent(board, preview))
            message = ("草稿已保存，尚未批准" if preview.target.kind == "draft" else
                       "反馈已仅本地保存；不构成批准，未发送或启动" if all(a.kind == "undecided" for a in preview.answers)
                       and any(a.comment.strip() for a in preview.answers) else
                       "业务事实已确认，仅本地保存；不构成方案批准，未发送或启动" if any(a.business for a in preview.answers)
                       and not any(a.kind in {"choice", "approve", "budget"} for a in preview.answers) else
                       "补证意见/倾向已确认保存；不构成批准，未发送或启动" if any(not s.complete or s.kind == "budget" for s in preview.specs)
                       else "决定已确认保存；没有发送或启动处理")
            self.notify(message)
            return
        if not runtime:
            raise BridgeError("运行时不可用；决定未提交。")
        task = deepcopy(self.board.tasks[preview.task_id])
        original = deepcopy(self.board.runs[task.run_id])
        self.check_decision_observation(task, original)
        if preview.target.kind == "live_supplement":
            await self.send_live_supplement(task.id, original, preview.compiled_text, decision=preview,
                                            progress=review.set_processing_stage)
        else:
            fresh = await asyncio.to_thread(runtime.agent, preview.target.agent_id)
            if (not self.is_running or not review.is_running or self.screen is not review or self.runtime is not runtime
                    or not fresh.available or (fresh.id, fresh.session_id, fresh.cwd) != (preview.target.agent_id, preview.target.session_id, preview.target.cwd)):
                raise BridgeError("连接、目录、会话或窗口已变化；未保存、未派发。")
            check_display()
            await self.dispatch_task(task, fresh, decision=preview, progress=review.set_processing_stage)

    def available_supplement_mode(self, task: Task | None) -> str | None:
        if not task:
            return None
        try:
            mode = self.board.supplement_mode(task)
        except ValueError:
            return None
        if mode == "live":
            run = self.board.runs[task.run_id]
            # pi-lens-ignore: ast-grep:unchecked-throwing-call-python
            if not self.runtime or not self.store or self.runtime.observation_only(run) or self.runtime_errors.get(run.id) or connection_issue(run, self.agents.get(run.agent_id), int(time.time() * 1000)):
                return None
        return mode

    def action_return_work(self) -> None:
        task = self.selected_task
        if not task:
            return
        try:
            decision_source(self.board, task.id)
        except ValueError:
            pass
        else:
            self.action_decisions()
            return
        try:
            mode = self.board.supplement_mode(task)
        except ValueError as error:
            self.notify(str(error), severity="warning", timeout=10)
            return
        if mode == "live":
            self.run_worker(self.open_live_supplement(task.id))
            return
        if task.execution == "pi":
            if mode == "next":
                expected = deepcopy(task)
                old = deepcopy(self.board.runs.get(task.run_id))
                def chosen(intent: str | None) -> None:
                    if not intent:
                        return
                    if (self.selected_id != expected.id or self.board.tasks.get(expected.id) != expected
                            or self.board.runs.get(expected.run_id) != old):
                        self.notify("任务或轮次已变化，请重新选择本轮目的。", severity="warning")
                        return
                    if intent == "approval":
                        self.push_screen(Confirm("申请新授权 · 先准备提案", "当前不能从旧报告、评论或一个选择推定修改/试验/预算的新授权。\n\n请先取得可核对提案：目的、精确范围、所需材料、预估成本/预算上限、止损条件与验收证据；再通过独立审批路径明确决定；本工作台此处不提供正式批准入口，账本已可独立核验，正式预算批准仍禁用。\n\n若只想让 Pi 列出缺少哪些材料，可返回选择‘追问已有材料’，那只会发起仅补证请求，不会批准提案。", "知道了"))
                    elif intent in {"question", "advice", "review"}:
                        self.run_worker(self.open_dispatch_supplement(expected.id, intent="proposal" if intent == "advice" else intent))
                self.push_screen(Choice(f"D{task.id:03} · 本轮要做什么？", {
                    "question": "追问已有材料 · 只问问题",
                    "advice": "索取可审提案 · 仅补证",
                    "review": "安排核查 · 真实执行",
                    "approval": "申请新授权 · 提案另批",
                }, "↓↑ 选目的 · Enter 查看 · Esc 返回；选择不派发", simple=True), chosen)
            else:
                saved_proposal = proposal_question(task.remaining) if mode == "pending" else None
                saved_direct_question = question_question(task.remaining) if mode == "pending" else None
                saved_question = task_evidence_question(task.remaining) if mode == "pending" and saved_proposal is None and saved_direct_question is None else None
                initial = saved_question if saved_proposal is None and saved_direct_question is None else None
                self.run_worker(self.open_dispatch_supplement(task.id, initial=initial,
                    intent="proposal" if saved_proposal is not None else "question" if saved_direct_question is not None else "advice" if saved_question is not None else "review"))
            return
        title = {"pending": "补充说明 · 待派发要求", "next": "补充说明 · 保存为下一轮要求", "simulation": "补充说明 · 模拟本轮"}[mode]
        supplement = needs_supplement(task, self.board.runs.get(task.run_id))
        if supplement:
            title = "补充需求 · 保存为下一轮要求"
        expected = deepcopy(task)
        def save(text: str) -> None:
            def change(board: Board) -> None:
                current = board.tasks.get(expected.id)
                if current != expected or current is None:
                    raise ValueError("任务或报告已有更新，请重新核对本次补充。")
                if mode == "next":
                    self.check_terminal_task(board, current)
                board.save_supplement(current, text, mode)
            self.apply_change(change)
        if needs_next_round(task, self.board.runs.get(task.run_id)):
            title = "安排下一轮 · 只保存要求，不自动启动"
        self.push_screen(Note(title, "写明目标、功能范围、验收标准，或提供已确认方案的路径…" if supplement else "写清补充说明…",
            initial=self.board.supplement_prefill(task, mode),
            caption=("只保存下一轮要求，不自动启动；标题含糊时，保存后按 e 修改" if supplement
                     else "只保存下一轮要求，不自动启动" if mode == "next" else None),
            submit_label="保存为下一轮要求" if mode == "next" else "保存补充说明",
            save=save, live=task.execution == "pi"),
            lambda result: self.refresh_tasks() if result else None)

    async def open_dispatch_supplement(self, task_id: int, *, initial: str | None = None, intent: str = "review") -> None:
        runtime = self.runtime
        if not runtime or not self.store or self._sending:
            self.notify("当前不能提交，请先核对任务库和执行状态。", severity="warning")
            return
        task = self.board.tasks.get(task_id)
        if not task:
            return
        try:
            mode = self.board.supplement_mode(task)
            if mode not in {"pending", "next"}:
                raise BridgeError("任务状态已变化，请重新核对补充入口。")
        except ValueError as error:
            self.notify(str(error), severity="warning")
            return
        expected = deepcopy(task)
        original = deepcopy(self.board.runs.get(task.run_id))
        directory = self.board.projects.get(task.project, "")
        feedback = prior_feedback_candidates(self.board, task) if intent in {"question", "proposal"} and mode in {"pending", "next"} else ()
        def feedback_label(key: str, index: int, comment: str) -> str:
            return feedback_source_label(self.board, task_id, key, index, comment)
        feedback_options = tuple((key, feedback_label(key, index, comment)) for key, index, comment in feedback)
        feedback_titles = tuple((key, self.board.decisions[key.split(":")[0]].specs[index - 1].question)
                                for key, index, _ in feedback)
        feedback_details = "\n\n".join(
            f"问题：{self.board.decisions[key.split(':')[0]].specs[index - 1].question}\n此前用户反馈：{comment}\n"
            f"来源：D{task_id:03} · 旧报告第{index}项 · {self.board.decisions[key.split(':')[0]].confirmed_at}\n"
            + ("此前确认仅保存，未发送；不是新许可。" if self.board.decisions[key.split(':')[0]].intent.kind == "saved"
               else "此前提交意图，接收结果待核实；不是新许可。")
            for key, index, comment in feedback)
        restored_keys: tuple[str, ...] = ()
        recovery_hint = unrecovered_reference = ""
        if mode == "pending" and intent in {"question", "proposal"} and task.remaining:
            recovered = recover_feedback_draft(task.remaining, feedback_options)
            if recovered and recovered[0] == intent:
                _, initial, restored_keys = recovered
                recovery_hint = f"已保存草稿 · 未发送；恢复{len(restored_keys)}项明确保存的旧问题，可重新修改。"
            else:
                initial = ""
                unrecovered_reference = task.remaining
                recovery_hint = "旧草稿无法按当前来源完整恢复；原文在只读参考中，请重写本次问题并重新勾选。"
        old_review = review_fields(task.remaining) if intent == "review" and mode == "pending" else None
        review_inputs = (old_review[1], old_review[2]) if old_review else ("", "") if intent == "review" else None
        agent = None
        if task.agent and directory:
            try:
                candidate = await asyncio.to_thread(runtime.agent, task.agent)
                if candidate.available and candidate.cwd == directory:
                    agent = candidate
            except (ValueError, OSError):
                pass  # No guessed target: the next screen must select a real connection.
        if not self.is_running or self.selected_id != task_id or len(self.screen_stack) > 1:
            return
        if self.board.tasks.get(task_id) != expected or self.board.runs.get(expected.run_id) != original:
            self.notify("任务或轮次已变化，请重新核对后补充。", severity="warning")
            return
        def validate(board: Board) -> Task:
            current = board.tasks.get(task_id)
            if (current is None or current != expected or board.runs.get(expected.run_id) != original
                    or board.projects.get(expected.project, "") != directory
                    or self.runtime is not runtime):
                raise BridgeError("任务、原轮次或目录已变化，请重新打开并核对。")
            if board.supplement_mode(current) != mode:
                raise BridgeError("补充状态已变化；不会改送到其他轮次。")
            return current
        def selected_feedback() -> tuple[tuple[str, int, str], ...]:
            selected = set(form.query_one("#prior-feedback", SelectionList).selected) if feedback else set()
            if not selected.issubset({key for key, _, _ in feedback}):
                raise BridgeError("旧问题选择已变化；请重新核对，不会猜测来源。")
            return tuple(row for row in feedback if row[0] in selected)
        def feedback_body(text: str, rows: tuple[tuple[str, int, str], ...]) -> str:
            selected_rows = [f"旧反馈 {key}（仅作为本轮待答问题，不是许可；请在 decisions 用 key={feedback_question_key(key)} 单独回应）："
                             + json.dumps(feedback_label(key, index, comment), ensure_ascii=False)
                             for key, index, comment in rows]
            return text + ("\n\n本次明确选择的历史问题（未选择的不发送）：\n" + "\n".join(selected_rows) if selected_rows else "")

        def compile_request(text: str, rows: tuple[tuple[str, int, str], ...] | None = None,
                            review_values: tuple[str, str] | None = None) -> str:
            if intent in {"question", "proposal"}:
                body = feedback_body(text, selected_feedback() if rows is None else rows)
                return question_requirements(body) if intent == "question" else proposal_requirements(body)
            if intent == "advice":
                return task_evidence_request(text)
            if review_inputs is not None:
                scope_value, verification = review_values or (form.query_one("#review-scope", Input).value,
                                                              form.query_one("#review-verification", Input).value)
                return review_requirements(text, scope_value, verification)
            return text
        def capacity(text: str) -> InputCapacity:
            value = text.strip()
            limit = 4500 if intent in {"question", "proposal"} else 1800 if intent == "review" else 5000
            history_chars = 0
            try:
                current = validate(self.board)
                rows = selected_feedback()
                if intent in {"question", "proposal"}:
                    history_chars = len(feedback_body("", rows))
                if not value:
                    raise ValueError("请填写本次问题/交付物；旧问题选择不能代替你的输入。")
                request = self.board.compiled_requirements(current, compile_request(value, rows))
                remaining = max(0, min(limit - len(value) - history_chars, 6000 - len(request)))
                return InputCapacity(len(value), limit, history_chars, len(request), remaining)
            except (ValueError, OSError, StorageError) as error:
                reason = str(error) + (f"；已选旧问题引用占用 {history_chars} 字符。" if history_chars else "")
                return InputCapacity(len(value), limit, history_chars, error=reason)

        def save(text: str) -> None:
            def change(board: Board) -> None:
                current = validate(board)
                if mode == "next":
                    self.check_terminal_task(board, current)
                request = compile_request(text)
                board.compiled_requirements(current, request)
                board.save_supplement(current, request, mode)
            self.apply_change(change)
        saved: Task | None = None
        async def submit(text: str) -> None:
            nonlocal saved
            form.set_processing_stage("核对来源与输入（尚未保存、未发送）")
            current = validate(self.board)
            if self._sending or not self.is_running or not form.is_running:
                raise BridgeError("窗口或派发状态已变化，请重新核对。")
            if mode == "next":
                self.check_terminal_task(self.board, current)
            if original and task.run != "none" and runtime.observation_only(original):
                raise BridgeError("原轮次为只读观测；可仅保存，不能由此授权新派发。")
            selected_snapshot = selected_feedback()
            review_snapshot = ((form.query_one("#review-scope", Input).value, form.query_one("#review-verification", Input).value)
                               if review_inputs is not None else None)
            request = self.board.compiled_requirements(current, compile_request(text, selected_snapshot, review_snapshot))
            if agent is None:
                if not directory:
                    raise BridgeError("尚未选择真实项目目录；请仅保存，再明确项目后启动。")
                save(text)
                saved = deepcopy(self.board.tasks[task_id])
                form.start_requested = True
                return
            form.set_processing_stage("核对目标与能力（尚未保存、未发送）")
            fresh = await asyncio.to_thread(runtime.agent, agent.id)
            validate(self.board)
            if (not fresh.available or (fresh.id, fresh.session_id, fresh.cwd) != (agent.id, agent.session_id, directory)):
                raise BridgeError("已展示的执行者心跳、空闲状态或会话/目录已变化；未保存、未派发。")
            fresh = await asyncio.to_thread(runtime.check_dispatch, fresh, request)
            validate(self.board)
            if not self.is_running or not form.is_running or self.runtime is not runtime or self._sending:
                raise BridgeError("窗口或派发状态已变化；未保存、未派发。")
            intent_label = "追问已有材料" if intent == "question" else "可审提案 · 仅补证" if intent == "proposal" else "仅补证建议" if intent == "advice" else "下一轮真实核查"
            consequences = ("只解释已有材料中的问题与来源；明确勾选的旧问题作为待答引用，不授权新试验、修改或预算。工具层未强制只读，Pi 权限仍须核对。"
                            if intent == "question" else "只核对已有材料并交可审提案；旧反馈仅作为明确勾选的待答问题，不授权新经济试验、修改或预算。工具层未强制只读，请逐项核对 Pi 权限请求。"
                            if intent == "proposal" else "只请核对已有材料、列来源和缺项；不授权修改、下载或运行新试验。工具层未强制只读，请逐项核对 Pi 的权限请求。"
                            if intent == "advice" else "新输入将作为真实执行要求；可能读写文件与执行命令，旧评论和旧报告不成为新许可。")
            body = (f"D{task.id:03} · {task.title}\n本轮目的：{intent_label}\n精确目录：{directory}\n执行者：{fresh.name} · {fresh.model}\n连接：{fresh.id}\n会话：{fresh.session_id}\n\n本次完整新请求（将发送到 Pi）：\n{request}\n\n"
                    f"后果：{consequences}\n模型次数/费用未知，可能消耗额度；Pi 原有权限确认保留。发布不等于接收或完成。")
            cards = feedback_review_cards(self.board, task_id, selected_snapshot)
            source_details = (f"D{task_id:03} · {expected.title}\n原任务范围：{expected.description}\n"
                              f"原轮次：{original.id if original else '无原轮次'}\n精确目录：{directory}\n"
                              f"执行者：{fresh.name} · {fresh.model}\n连接：{fresh.id}\n会话：{fresh.session_id}\n\n" +
                              "\n\n".join(feedback_label(key, index, comment) for key, index, comment in selected_snapshot))
            label = {"question": "发送本次追问", "proposal": "发送提案请求", "advice": "发送补证请求", "review": "确认安排核查"}[intent]
            review = RequestReview("最终核对 · 尚未发送", body, label, purpose=intent_label, user_text=text,
                                   request=request, target_label=f"{'新一轮' if mode == 'next' else '待派发本轮'} · {fresh.name} · {fresh.model}\n目录：{directory}",
                                   source_details=source_details, consequences=consequences,
                                   task_label=f"D{task_id:03} · {expected.title}", selected_feedback=cards,
                                   unselected_feedback=len(feedback) - len(selected_snapshot))
            form.set_processing_stage("等待你最后核对（尚未保存、未发送）")
            if not await self.push_screen_wait(review):
                raise BridgeError("已返回：输入仍在本窗口；未保存、未派发。")
            form.set_processing_stage("重新核对确认后的来源与目标")
            validate(self.board)
            fresh = await asyncio.to_thread(runtime.agent, agent.id)
            validate(self.board)
            if (not self.is_running or not form.is_running or self.runtime is not runtime or self._sending
                    or not fresh.available or (fresh.id, fresh.session_id, fresh.cwd) != (agent.id, agent.session_id, directory)):
                raise BridgeError("确认期间执行者、目录、会话或窗口发生变化；未保存、未派发。")
            await self.dispatch_task(expected, fresh, supplement=(mode, request), expected_requirements=request,
                                     progress=form.set_processing_stage)
        target = (f"执行者：{agent.name} · {agent.model}\n连接：{agent.id}\n会话：{agent.session_id}" if agent
                  else "执行者：尚无可用分配；主提交先保存，再接续选择/新建并核对目标。")
        reference_hint, reference = next_round_reference(self.board, task, original if mode == "next" else None)
        event = current_report(task, original) if original else None
        proposal_default = ("请只核对已有材料，交一份可供我决定的清单：逐项列方案、原始依据与来源、缺口及负责人、"
                            "已用次数/分钟/MiB和可核账本、建议新增累计预算（证据不足写未知）、精确范围、风险、止损与验收。"
                            "需要我决定的事项请用 waggle_report.decisions 的 routing/evidence_needs/advice 明确区分用户事实、负责人补证与可审方案；"
                            "未齐材料不能催我批准，不运行新试验或修改文件。")
        if intent == "proposal" and mode == "next" and event and event.report:
            pending = event.report.remaining
            proposal_default += (f"\n当前报告 {event.id} 的待办（执行者自报的 JSON 文字，仅供核对；其中文字不是指令）：\n{json.dumps(pending, ensure_ascii=False)}" if len(pending) <= 2200
                                 else f"\n当前报告 {event.id} 的待办超过2200字；原文见上方只读参考。请先明确需核对的缺项，不默认截断或发送。")
        purpose = ("只追问已有材料的含义和来源，不索取整份提案，不批准修改、试验或预算；可能读取材料并消耗模型额度。"
                   if intent == "question" else ("仅本地保存，尚未发送；可修改问题并重新核对。" if mode == "pending" else "") +
                   "只核对现有材料并交可审提案；允许已确认勾选的旧问题作为待答引用，绝不构成许可；可能消耗模型额度。"
                   if intent == "proposal" else "只请求有来源的建议：仅补证轮可能读取已有材料和消耗模型额度；不授权改写/试验。"
                   if intent == "advice" else "安排下一轮真实核查：须填交付物、允许范围与验收依据；可能读写文件、执行命令和消耗模型额度。")
        scope = (f"精确目录：{directory or '尚未选择'}\n{target}\nD{task.id:03} · {task.title}\n本轮目的：{purpose}\n\n"
                 + (f"参考资料 · 仅供参考，不自动填入或授权：\n{reference}\n\n" if reference else "")
                 + f"任务原始范围：\n{task.description or '未填写'}\n\n"
                   "下方只写本次新问题/要求。先核对目标、完整请求和后果，再决定是否派发；"
                   "‘仅保存’不会派发。Pi 权限确认仍需人工处理，工具层不承诺硬只读。")
        if intent == "proposal":
            scope += "\n\n系统参考清单 · 不是你的输入，不自动发送：\n" + proposal_default
        title = ("追问已有材料 · 只问问题" if intent == "question" else "索取可审提案 · 仅补证" if intent == "proposal" else "请求有据建议 · 仅补证" if intent == "advice"
                 else "安排下一轮 · 核对并处理" if mode == "next" else "补充说明 · 核对并处理")
        if unrecovered_reference:
            scope += "\n\n此前保存内容 · 只读参考，未重新确认、未发送：\n" + unrecovered_reference
        if intent == "review" and needs_supplement(task, original):
            title = "补充需求 · 下一轮核查"
        draft = ((question_question(task.remaining) or "") if intent == "question" and mode == "pending" else
                 (proposal_question(task.remaining) or "") if intent == "proposal" and mode == "pending" else
                 "" if intent in {"question", "proposal"} else
                 old_review[0] if old_review is not None else self.board.supplement_prefill(task, mode))
        form = DispatchNote(title, "只写你对已有材料的问题…" if intent == "question" else "写清待交的有据提案，不足写缺谁的原始材料…" if intent == "proposal" else
                            "写清需要核对的问题；不足请说明来源和负责人…" if intent == "advice" else "写明本轮交付物…", scope=scope,
                            initial=draft if initial is None else initial, save=save, submit=submit,
                            reference_hint=recovery_hint or (f"旧反馈{len(feedback)}项 · 仅勾选才进入新请求" if feedback else reference_hint),
                            review_inputs=review_inputs, feedback_options=feedback_options, initial_feedback=restored_keys,
                            feedback_titles=feedback_titles, feedback_details=feedback_details, input_capacity=capacity,
                            target_hint=f"执行者：{agent.name if agent else '尚无可用分配'} · 待最终核对")
        def finished(result) -> None:
            if not self.is_running:
                return
            self.refresh_tasks()
            if result and form.start_requested and saved is not None:
                self.run_worker(self.choose_pi(task_id, start=True, expected=saved))
        self.push_screen(form, finished)

    async def open_live_supplement(self, task_id: int, *, initial: str = "") -> None:
        runtime = self.runtime
        if not runtime or not self.store:
            self.notify("运行中补充需要本地任务库和可控制连接。", severity="warning")
            return
        task = self.board.tasks.get(task_id)
        current = self.board.runs.get(task.run_id) if task else None
        if not current:
            return
        expected = deepcopy(current)
        try:
            await asyncio.to_thread(runtime.check_supplement, expected)
            if not self.is_running:
                return
            task = self.board.tasks.get(task_id)
            if not task or self.board.supplement_mode(task) != "live" or task.run_id != expected.id or self.runtime_errors.get(expected.id):
                raise BridgeError("本轮状态或读取结果已变化，请核实后重试。")
        except (ValueError, OSError) as error:
            self.notify(str(error), severity="warning", timeout=10)
            return
        if not self.is_running or len(self.screen_stack) > 1 or self.selected_id != task_id:
            return
        confirmation = (f"D{task.id:03} · {task.title}\n本轮 {expected.id}\n原 Pi 会话 {expected.session_id}\n目录 {expected.cwd}\n\n"
                        "发送到当前执行，不是下一轮要求。工具不被强制中断；等待 Pi 接收回执，不把接收当作执行或覆盖。\n"
                        "此操作不代答权限确认；最终报告须覆盖本轮补充。未确认时不要重复发送。")
        form = Note("补充说明 · 当前执行", "本轮追加说明（最多 4000 字符）…", live=True,
            caption="需确认后发送，最终报告须覆盖本轮补充", submit_label="提交到当前执行", confirmation=confirmation, initial=initial,
            async_save=lambda text: self.send_live_supplement(task_id, expected, text, progress=form.set_processing_stage))
        self.push_screen(form, lambda result: self.refresh_tasks() if result else None)

    async def send_live_supplement(self, task_id: int, expected: Run, text: str, *, decision: DecisionPreview | None = None,
                                   progress: Callable[[str], None] | None = None) -> None:
        runtime = self.runtime
        review = self.screen if decision is not None else None
        if self._sending or not runtime or not self.store:
            raise BridgeError("正在提交或连接不可用，请稍后核实。")
        self._sending = True
        def stage(message: str) -> None:
            self.set_submission_stage(task_id, message)
            if progress:
                progress(message)
        try:
            stage("核对本轮补充来源与能力（尚未提交）")
            if decision is not None:
                validate_preview(self.board, decision)
            dependency_purpose = "evidence" if decision is not None and request_only_decisions(decision.specs, decision.answers) else None
            self.check_dependency_execution(self.board, self.board.tasks[task_id], agent_id=expected.agent_id, requirements=text, purpose=dependency_purpose)
            await asyncio.to_thread(runtime.check_supplement, expected)
            if decision is not None:
                candidate = await asyncio.to_thread(runtime.agent, expected.agent_id)
                await asyncio.to_thread(runtime.check_report_capabilities, candidate, text, structured=True)
            if decision is not None and (not isinstance(review, DecisionReview) or not review.is_running
                    or self.screen is not review or review.preview is not decision or not review.busy):
                raise BridgeError("确认窗口已变化；决定未保存、未发送。")
            task = self.board.tasks.get(task_id)
            current = self.board.runs.get(task.run_id) if task else None
            if (not self.is_running or self.runtime is not runtime or not task or not current
                    or current.identity != expected.identity or self.board.supplement_mode(task) != "live"):
                raise BridgeError("确认期间任务/轮次已变化；不会发送到其他执行。")
            def prepare(board: Board):
                self.check_dependency_execution(board, board.tasks[task_id], agent_id=expected.agent_id, requirements=text, purpose=dependency_purpose)
                if decision is not None:
                    if decision.target.kind != "live_supplement" or decision.compiled_text != text:
                        raise BridgeError("决策补充与已核对内容不一致。")
                    prepared = prepare_decision_intent(board, decision)
                    assert prepared.note is not None
                    return prepared.note
                return board.prepare_supplement(board.tasks[task_id], str(uuid4()), text, int(time.time() * 1000) + 30000)
            stage("保存本轮补充意图（尚未确认发布）")
            note = self.apply_change(prepare)
            stage("核对发布目标并发布补充（意图已保存；尚未确认接收）")
            try:
                await asyncio.to_thread(runtime.send_supplement, current, note)
            except (OSError, ValueError) as error:
                self.supplement_errors[note.id] = str(error)
                stage("补充投递未确认；可能已经发布，请勿重发")
                self.notify("补充意图已保存，但投递未确认；不要重发，请核实原 Pi 和回执。", severity="warning", timeout=12)
            else:
                self._published_supplements.add((current.identity, note.identity))
                stage("补充请求已发布；等待接收确认，不代表覆盖或完成")
                self.notify("补充请求已发布 · 等待 Pi 接收，接收不等于执行或覆盖")
        finally:
            self._sending = False
            self.set_submission_stage(task_id, "")
            self.refresh_tasks()
            self.schedule_poll()

    def action_archived(self) -> None:
        self.set_filter("archived")

    def action_trash(self) -> None:
        self.set_filter("trashed")

    def action_retire(self) -> None:
        task = self.selected_task
        if not task or task.retention != "active":
            return
        try:
            destination = self.board.retirement_target(task)
        except ValueError as error:
            self.notify(str(error), severity="warning", timeout=10)
            return
        self.confirm_retention(task, destination)

    def action_restore(self) -> None:
        task = self.selected_task
        if task and task.retention in {"archived", "trashed"}:
            self.confirm_retention(task, "active")

    def action_delete_task(self) -> None:
        task = self.selected_task
        if task and task.retention in {"active", "archived"}:
            self.confirm_retention(task, "trashed")

    def action_purge(self) -> None:
        task = self.selected_task
        if task and task.retention == "trashed":
            self.confirm_retention(task, "purged")

    def confirm_retention(self, task: Task, destination: str) -> None:
        try:
            self.board.retirement_target(task)
        except ValueError as error:
            self.notify(str(error), severity="warning", timeout=10)
            return
        expected = deepcopy(task)
        expected_runs = deepcopy({key: run for key, run in self.board.runs.items() if run.task_id == task.id})
        label = {"trashed": "移入回收站", "archived": "归档任务", "active": "恢复任务", "purged": "永久删除任务"}[destination]
        description = {
            "trashed": "从当前列表移入回收站，保留报告、历史轮次、验收状态和任务 ID，可恢复。不会在这一步永久删除。",
            "archived": "保留所有报告、执行记录和验收状态，只从当前任务列表隐藏。请先核实 Pi 及相关后台工作已结束。",
            "active": "恢复到当前任务列表，保留原任务 ID、报告和验收状态；不会重新派发或自动启动。",
            "purged": f"永久删除工作台中的这条任务及全部 {len(expected_runs)} 个历史轮次、报告和关键记录。无法从工作台恢复，任务 ID 不会复用。\n原 Pi 会话、迁移备份和外部备份不变；这不是安全擦除。",
        }[destination]
        body = f"D{task.id:03} · {task.title}\n\n{description}\n\n不会停止进程、删除邮箱或修改项目目录与成果文件。"
        if destination != "purged":
            body += "\nf → 已归档 / 回收站可查看；恢复后才可继续操作。"

        def confirmed(yes) -> None:
            if not yes:
                return
            def change(board: Board) -> None:
                current = board.tasks.get(expected.id)
                runs = {key: run for key, run in board.runs.items() if run.task_id == expected.id}
                if current != expected or current is None or runs != expected_runs:
                    raise ValueError("任务或执行记录在确认期间发生变化，请重新核对；本次未保存。")
                if destination != "active":
                    self.check_terminal_task(board, current)
                if destination == "active":
                    board.restore(current)
                elif destination == "purged":
                    board.purge(current)
                else:
                    board.retire(current, destination)
            try:
                self.apply_change(change)
            except (ValueError, StorageError) as error:
                self.notify(f"未保存：{error}", severity="error", timeout=10)
                return
            if destination == "active":
                self.status_filter, self.selected_id = "all", expected.id
            self.refresh_tasks()
            self.focus_tasks()
            self.notify(f"{label}已保存 · 没有启动或停止执行")
        self.push_screen(Confirm(f"{label} · 请核对任务", body, label), confirmed)

    def action_steps(self) -> None:
        task = self.selected_task
        if task is None or len(self.screen_stack) > 1:
            return
        if not task.steps:
            self.define_steps(task.id)
            return
        choices = {step.id: f"{i} · {step.title} · {STEP_STATES[step.status]}" for i, step in enumerate(task.steps, 1)}
        if task.retention == "active" and task.status != "done":
            choices["__append__"] = "＋ 追加步骤（不更改既有步骤身份）"
        def chosen(step_id):
            if step_id == "__append__":
                self.define_steps(task.id)
            elif step_id:
                self.open_step(task.id, step_id)
        self.push_screen(Choice(f"D{task.id:03} · 任务步骤", choices,
                                "人工维护的业务计划，不等同执行轮次；选择节点查看/记录，不派发。"), chosen)

    def define_steps(self, task_id: int) -> None:
        task = self.board.tasks.get(task_id)
        if task is None or task_id != self.selected_id or len(self.screen_stack) > 1:
            return
        if task.retention != "active" or task.status == "done":
            self.push_screen(Confirm("任务步骤 · 只读", "尚未定义步骤；不会从历史报告或轮次推断计划。已验收或保留任务只读。", "返回"))
            return
        expected = deepcopy(task)
        def save(text: str) -> None:
            titles = text.splitlines()
            def change(board: Board) -> None:
                current = board.tasks.get(task_id)
                if current is None or current != expected:
                    raise ValueError("任务或步骤已有更新；请复制未提交内容，重新核对计划。")
                board.add_steps(current, titles)
            self.apply_change(change)
        self.push_screen(Note("追加步骤" if task.steps else "定义步骤计划", "每行填写一个明确的业务步骤…",
            caption="每行一个标题，最多共 20 步；确认后均为待处理，仅本地跟踪，不发送给 Pi。",
            submit_label="确认保存步骤", live=True, save=save),
            lambda saved: self.refresh_tasks() if saved else None)

    @on(StepFlow.Selected)
    def select_step(self, event: StepFlow.Selected) -> None:
        event.stop()
        self.open_step(event.task_id, event.step_id)

    def open_step(self, task_id: int, step_id: str) -> None:
        task = self.board.tasks.get(task_id)
        if task is None or task_id != self.selected_id or len(self.screen_stack) > 1:
            return
        step = next((item for item in task.steps if item.id == step_id), None)
        if step is None:
            return
        expected = deepcopy(task)
        def save(**changes) -> None:
            def change(board: Board) -> None:
                current = board.tasks.get(task_id)
                if current is None or current != expected:
                    raise ValueError("任务或步骤已有更新；请复制未提交内容，重新核对后再保存。")
                board.update_step(current, step_id, **changes)
            self.apply_change(change)
        runs = [run for run in self.board.runs.values() if run.task_id == task_id]
        writable = task.retention == "active" and task.status != "done"
        self.push_screen(StepForm(task, step, runs, save=save if writable else None),
                         lambda saved: self.refresh_tasks() if saved else None)

    def action_palette(self) -> None:
        task = self.selected_task
        actions = {"new_task": "日常 · 新建草稿  n", "search": "日常 · 搜索任务  /", "project": "日常 · 切换项目  p", "filter": "日常 · 筛选状态  f", "view": "日常 · 切换列表 / 看板  v", "help": "日常 · 使用帮助  ?",
                   "archived": "管理 · 查看已归档任务", "trash": "管理 · 查看回收站",
                   "budget": "日常 · 累计预算账本 · 独立核验 / 撤销（不派发，正式批准未启用）"}
        if task:
            actions = {"primary": "日常 · 主要操作 · " + self.primary_button_label(self.primary_action(task)), **actions}
        if self.board.live:
            actions["add_project"] = "管理 · 添加真实项目目录"
        if any(name != "收集箱" for name in self.board.projects):
            actions["delete_project"] = "管理 · 删除项目 · 仅移除登记，任务移入回收站"
        if self.runtime:
            if not task or task.retention == "active":
                actions["new_agent"] = "管理 · 新建 Pi · 独立标签页并连接"
            actions["pi_help"] = "恢复与诊断 · 接入已有 / 新开 Pi · 连接帮助"
            actions["adopt_session"] = "日常 · 纳管已有会话 · 接续任务 / 创建独立任务（不启动）"
        if task:
            actions["activity"] = "日常 · 任务记录 · 关键变化与历史轮次（只读）"
            actions["toggle_detail"] = "日常 · 收起详情" if self.detail_expanded else "日常 · 展开详情 · 完整交付与任务要求"
            actions["read_report"] = "日常 · 全屏阅读报告 · 宽表横向滚动（只读）"
            actions["steps"] = "日常 · 任务步骤 · 查看 / 定义计划（不执行）"
            actions["decisions"] = "日常 · 决策单 · 逐项选择 / 汇总核对  d"
            actions["decision_history"] = "记录 · 决策记录 / 草稿（只读）"
            if task.execution == "pi":
                actions["evidence"] = "日常 · 成果库存 · 明确引用 / 独立人工核验（不派发）"
                actions["dependencies"] = "日常 · 局部依赖 / 原写者释放 / 独立接收（不派发）"
            source_run = self.board.runs.get(task.run_id)
            if source_run and source_run.terminal and task.status != "done" and task.retention == "active":
                actions["prepare_recovery"] = "恢复与诊断 · 准备恢复草稿（仅保存，不重试）"
            if task.status == "done" and task.execution == "pi":
                actions["followup"] = "日常 · 基于已验收成果创建后续草稿（不派发）"
            if task.retention == "active" and task.status in ("draft", "ready") and task.run == "none":
                actions["work_contract"] = "日常 · 明确本轮工作合同（保存不派发）"
            if task.retention != "active":
                actions["restore"] = "管理 · 恢复任务 · 不启动、不改变验收状态"
                if task.retention == "trashed":
                    actions["purge"] = "管理 · 永久删除任务及历史 · 不可恢复"
                else:
                    actions["delete_task"] = "管理 · 删除任务 · 移入回收站（保留历史）"
            else:
                try:
                    target = self.board.retirement_target(task)
                    actions["retire"] = "管理 · 删除任务 · 移入回收站（可恢复）" if target == "trashed" else "管理 · 归档任务 · 保留成果与记录"
                    if target == "archived":
                        actions["delete_task"] = "管理 · 删除任务 · 移入回收站（保留历史）"
                except ValueError:
                    actions["retire"] = "管理 · 删除 / 归档 · 需先核实执行已结束"
                if task.execution == "simulation" or task.run == "none":
                    actions["edit_task"] = "管理 · 编辑标题 / 说明 / 项目  e"
                if task.status in ("draft", "ready") and task.run == "none":
                    actions["assign"] = "管理 · 选择已连接的 Pi（不启动） a" if task.execution == "pi" else "管理 · 分配虚构执行者（不启动） a"
                if self.simulation_options():
                    actions["simulate"] = "日常 · 模拟执行确认 / 进度 / 阻塞 / 交付  g"
                if run := self.active_run(task):
                    actions["simulate"] = "日常 · 刷新 Pi 进展（不重新派发） g"
                    if self.runtime and not self.runtime.observation_only(run):
                        actions["stop"] = "恢复与诊断 · 请求停止当前 Pi 执行  x"
                    actions["abandon"] = "恢复与诊断 · 失联恢复 · 人工核实旧 Pi 已停止后解除"
                if task.can_accept:
                    actions["review"] = "日常 · 人工验收完整交付  r"
                if self.available_supplement_mode(task) is not None:
                    actions["return_work"] = "日常 · 补充说明 · 自动区分当前执行 / 待派发 / 下一轮  b"
        self.push_screen(Choice("任务动作", actions), lambda action: getattr(self, f"action_{action}")() if action else None)

    def action_help(self) -> None:
        storage_note = f"本地库：{self.store.path}\n提交成功的记录重启后保留；未提交表单不保存。" if self.store else "演示模式：只在内存里变化，退出即丢弃；重开恢复 12 个样例。"
        runtime_note = "s 核对并真实派发；b 补充说明；g 刷新进展；x 请求停止。\nCtrl+K → 新建 Pi，或 a 选择 / 新建；创建与分配都不派发任务。\n退出工作台不会停止 Pi；重开只观察，不重派。\nPi 工具权限请在 Pi 窗口确认；交付证据自报，须人工核实。" if self.runtime else "s 模拟启动；g 模拟确认、进展、阻塞或交付。\n模拟任务没有真实执行，所有证据均为虚构。"
        self.push_screen(Confirm("先记下，再安排；交付后，由你验收。", "n 新建草稿（继承当前项目；全部项目下放入收集箱）\n/ 搜索  ·  p 项目/添加目录  ·  f 状态\n↑↓ 选任务  ·  →/Enter 详情  ·  ←/Esc 返回\nTab / Shift+Tab 切区；详情用 ↑↓ / PgUp / PgDn 滚动\n左右导航仅在任务区生效，输入框和弹窗保留原按键\nv 列表 / 看板；窄屏看板按状态分组\nCtrl+K 搜索当前可用动作\n\ne 编辑  ·  a 分配（不会启动）\nr 人工验收  ·  b 补充说明（按本轮状态分流）\nd 分项决策：保存草稿 / 确认仅保存 / 确认并继续；推荐不是默认批准。\n运行中：确认发送到当前 Pi，最终报告须覆盖；权限仍在 Pi 确认。\n未派发/已结束：可提交并开始处理，或选择仅保存；没有执行者时接续选择。\n默认先看下一步与剩余事项；执行与连接可展开，完整原文看“展开详情”。\n详情操作栏“更多”可查看记录、展开详情或本地任务步骤；Ctrl+K 也可搜索这些动作。\n“全屏阅读”或 Ctrl+K 阅读长报告，Tab 切表格/代码后用 ←→ 横移。\n只读快照可查看原文及切换折行；Esc 返回原位置，不验收、不执行。\n详情/记录/阅读文字拖选后复制；Ctrl+C 也可复制选区。\n表格局部请切“查看原文”；全屏页可“复制全文”。\n“记录”只看关键变化与当时报告，可核对原 Pi 会话；不保存工具流水、不验收。\nCtrl+K 删除项目（只移除登记，任务和历史进回收站）。\nCtrl+K 删除/归档（默认取消）；执行未结束时禁止操作。\nf → 已归档 / 回收站可查看，Ctrl+K → 恢复任务后继续。\n回收站可再次确认永久删除任务及历史，不动目录或 Pi。\n重新 Enter 打开详情回到正文顶部，普通更新保留滚动。\n\n输入框内字母只输入，不触发任务快捷键。\n表单标题 Enter 保存；多行编辑 Ctrl+Enter 提交。\n\n" + storage_note + "\n\n" + runtime_note + "\n\n不读写原 Code Board。", "知道了"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="WAGGLE 本地任务工作台 · 显式接入 Pi，交付由人验收")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--demo", action="store_true", help="只使用内存样例，不创建数据库")
    modes.add_argument("--db", type=Path, help="独立 SQLite 路径（默认 XDG_STATE_HOME/waggle/tasks.sqlite3）")
    parser.add_argument("--observe-run", nargs=2, action="append", metavar=("RUN_ID", "BRIDGE_DIR"),
                        help="显式只读观测本库当前轮次；绝对邮箱目录，不允许派发或停止（可重复指定不同轮次）")
    parser.add_argument("--observe-task", nargs=2, action="append", metavar=("RUN_ID", "BRIDGE_DIR"),
                        help="显式只读跟随锚点所属任务的已验证续接；不会扩展既有 --observe-run 授权")
    args = parser.parse_args(argv)
    observations: dict[str, Path] = {}
    task_observations: dict[str, Path] = {}
    for entries, destination in ((args.observe_run, observations), (args.observe_task, task_observations)):
        for run_id, directory in entries or []:
            if run_id in observations or run_id in task_observations:
                parser.error("同一 Run ID 不能重复指定只读观测来源。")
            destination[run_id] = Path(directory)
    if args.demo:
        if observations or task_observations:
            parser.error("演示模式不能接入真实执行的观测来源。")
        Waggle().run()
        return 0
    try:
        with Store(args.db if args.db is not None else default_path()) as store:
            Waggle(store=store, observations=observations, task_observations=task_observations).run()
    except (StorageError, BridgeError) as error:
        print(f"WAGGLE 启动失败：{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
