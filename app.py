# /// script
# requires-python = ">=3.11"
# dependencies = ["textual==8.2.8"]
# ///
"""TASKROOM: durable tasks and explicitly joined Pi; acceptance stays human."""
import argparse
import asyncio
from collections.abc import Awaitable, Callable
from copy import deepcopy
from pathlib import Path
import sys
import time
from typing import TypeVar
from uuid import uuid4

from rich.text import Text
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Input, OptionList, Select, Static, TextArea
from textual.widgets.option_list import Option

from delivery import activity_text, delivery_text
from launch import Launch, connected_agent, create_pi
from model import AGENTS, PROJECTS, RUNS, STATUSES, Board, Run, Task, sample_board
from paths import DirectorySuggestions, directory_suggestions
from runtime import Agent, BridgeError, Runtime, canonical_directory
from store import StorageError, Store, default_path

T = TypeVar("T")

COLORS = {"draft": "#8794aa", "ready": "#aebcff", "running": "#79ccee", "blocked": "#f1b778", "review": "#d0b0f5", "done": "#90d4ad"}
LANES = [("draft", "待安排"), ("ready", "待执行"), ("running", "执行 / 阻塞"), ("review", "待验收"), ("done", "已验收")]
FILTERS = {"all": "全部状态", **STATUSES}


class Dialog(ModalScreen):
    BINDINGS = [Binding("escape", "cancel", "返回"), Binding("ctrl+enter", "submit", "确认", priority=True)]

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_submit(self) -> None:
        self.dismiss(True)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.action_cancel()
        elif event.button.id == "submit":
            self.action_submit()


class Choice(Dialog):
    def __init__(self, title: str, options: dict[str, str], caption: str = "输入搜索 · ↓↑ 选择 · Enter 确认 · Esc 返回") -> None:
        super().__init__()
        self.title_text, self.options, self.caption = title, options, caption

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="choice-dialog"):
            yield Static(self.title_text, classes="dialog-title", markup=False)
            yield Static(self.caption, classes="muted", markup=False)
            yield Input(placeholder="搜索动作 / 名称…", id="choice-query")
            yield OptionList(id="choices", markup=False)
            yield Button("返回  Esc", id="cancel")

    def on_mount(self) -> None:
        self.fill("")
        self.query_one(Input).focus()

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
    def __init__(self, directory: str, runtime: Runtime, create: Callable[[str, str, str], Awaitable[Launch]], ready: Callable[[Agent], None]) -> None:
        super().__init__()
        self.directory, self.runtime, self.create, self.ready = directory, runtime, create, ready
        self.launch_id = str(uuid4())
        self.launch: Launch | None = None
        self.creating = False
        self.reading = False
        self.connected = False

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static("新建 Pi · 独立会话", classes="dialog-title")
            with VerticalScroll(classes="form-body"):
                yield Static(f"精确目录：{self.directory}\n\n确认后在新的 Herdr 标签页启动并连接 Pi，不接管已有会话。\n允许工作台随后向它派发/请求停止；本次不派发任务。\n项目与工具权限仍在 Pi 窗口处理，不自动批准。", markup=False)
                yield Static("名称", classes="field-label")
                yield Input(f"Pi · {Path(self.directory).name}", id="agent-name", max_length=80)
                yield Static("模型 · 可选", classes="field-label")
                yield Input(placeholder="留空使用 Pi 当前配置，或 provider/model", id="agent-model", max_length=200)
                yield Static("连接后返回任务，按 s 核对并派发。", id="launch-result", markup=False, classes="muted")
                yield Static("", id="form-error", classes="error", markup=False)
            with Horizontal(classes="dialog-buttons"):
                yield Button("返回  Esc", id="cancel")
                yield Button("确认新建并连接", id="submit", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#agent-name", Input).focus()
        self.set_interval(0.5, self.poll_connection)

    def action_submit(self) -> None:
        if self.connected:
            self.dismiss(self.launch)
        elif self.launch:
            self.poll_connection()
        elif not self.creating:
            self.creating = True
            self.query_one("#submit", Button).disabled = True
            self.run_worker(self.submit_creation())

    async def submit_creation(self) -> None:
        try:
            self.launch = await self.create(self.query_one("#agent-name", Input).value,
                                            self.query_one("#agent-model", Input).value, self.launch_id)
            for field in self.query(Input):
                field.disabled = True
            self.query_one("#submit", Button).label = "刷新连接"
            result = self.query_one("#launch-result", Static)
            result.update(f"{self.launch.message}\n标签页：{self.launch.tab_id or '尚未确认'}\n标识：{self.launch.id}\n若等待授权，请到新 Pi 标签页处理。返回不退出 Pi，也不会重新创建。")
            result.scroll_visible(animate=False)
        except (OSError, ValueError) as error:
            self.query_one("#form-error", Static).update(f"未创建：{error}\n创建标识：{self.launch_id}")
        finally:
            self.creating = False
            self.query_one("#submit", Button).disabled = False
        self.poll_connection()

    def poll_connection(self) -> None:
        if self.launch and not self.connected and not self.reading:
            self.reading = True
            self.run_worker(self.read_connection())

    async def read_connection(self) -> None:
        try:
            assert self.launch is not None
            agent = await asyncio.to_thread(connected_agent, self.runtime, self.launch)
            if agent and self.is_mounted:
                self.connected = True
                self.query_one("#submit", Button).label = "返回任务"
                self.query_one("#launch-result", Static).update(f"Pi 已连接：{agent.label}\n目录：{agent.cwd}\n尚未派发任务；返回后按 s 明确派发。")
                try:
                    self.ready(agent)
                except (ValueError, StorageError) as error:
                    self.query_one("#form-error", Static).update(f"Pi 已连接，但分配未保存：{error}。返回后按 a 重新选择。")
        except (ValueError, OSError) as error:
            if self.is_mounted:
                self.query_one("#form-error", Static).update(f"连接未确认：{error}；只读取结果，不重新启动。")
        finally:
            self.reading = False

    def action_cancel(self) -> None:
        if not self.creating:
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


class Note(Dialog):
    def __init__(self, title: str, placeholder: str, *, save: Callable[[str], None], live: bool = False) -> None:
        super().__init__()
        self.title_text, self.placeholder, self.save = title, placeholder, save
        self.live = live

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="note-dialog"):
            yield Static(self.title_text, classes="dialog-title", markup=False)
            yield Static(("只保存说明，不自动执行" if self.live else "执行仍为模拟") + " · Ctrl+Enter 提交 · Esc 取消", classes="muted")
            yield TextArea(placeholder=self.placeholder, id="note")
            yield Static("", id="form-error", classes="error", markup=False)
            with Horizontal(classes="dialog-buttons"):
                yield Button("返回  Esc", id="cancel")
                yield Button("提交说明", id="submit", variant="primary")

    def on_mount(self) -> None:
        self.query_one(TextArea).focus()

    def action_submit(self) -> None:
        text = self.query_one(TextArea).text.strip()
        if not text:
            self.query_one("#form-error", Static).update("请填写原因，不能静默跳过。")
            return
        try:
            self.save(text)
        except (ValueError, StorageError) as error:
            self.query_one("#form-error", Static).update(f"未保存：{error}")
            return
        self.dismiss(text)


class TaskRoom(App):
    TITLE = "TASKROOM · 假数据交互原型"
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
        Binding("r", "review", "人工验收", show=False), Binding("b", "return_work", "补充/退回", show=False),
        Binding("question_mark", "help", "帮助", show=False),
    ]

    def __init__(self, board: Board | None = None, *, store: Store | None = None, runtime: Runtime | None = None) -> None:
        super().__init__()
        self.store = store
        self.board = store.load() if store is not None else board if board is not None else sample_board()
        self.runtime = (runtime or Runtime()) if self.board.live or any(task.execution == "pi" for task in self.board.tasks.values()) else None
        self.agents: dict[str, Agent] = {}
        self.runtime_errors: dict[str, str] = {}
        self._polling = False
        self._sending = False
        self._visual_dirty = False
        self._poll_save_error = ""
        if store is not None:
            self.title = "TASKROOM · 本地任务 · Pi 显式接入" if self.runtime else "TASKROOM · 本地任务 · 执行仍模拟"
        self.selected_id = next(iter(self.board.tasks), None)
        self.project_name = "all"
        self.status_filter = "all"
        self.view_mode = "list"
        self.detail_open = False
        self.viewport_width = 80
        self.theme = "textual-dark"

    def compose(self) -> ComposeResult:
        with Horizontal(id="masthead"):
            yield Static("◈  TASKROOM", id="brand")
            yield Static(("LOCAL · 本地保存 · Pi 显式接入" if self.runtime else "LOCAL · 本地保存 · 执行模拟") if self.store else "DEMO · 仅内存 · 退出即丢弃", id="demo-badge")
            yield Button("看板  v", id="view-button")
            yield Button("动作  Ctrl+K", id="palette-button")
        with Horizontal(id="workspace"):
            with Vertical(id="sidebar"):
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
                        yield OptionList(id="task-list", markup=False)
                        yield Static("没有匹配的任务\n试试清空搜索 / 修改项目或状态筛选，或按 n 新建。", id="empty", markup=False)
                    with Horizontal(id="board"):
                        for key, label in LANES:
                            with Vertical(classes="lane", id=f"column-{key}"):
                                yield Static(label, id=f"heading-{key}", classes="lane-heading")
                                yield OptionList(id=f"lane-{key}", markup=False)
                    with Vertical(id="inspector"):
                        yield Static("", id="detail-heading", markup=False)
                        yield VerticalScroll(Static("", id="detail-text", markup=False), id="detail-scroll", can_focus=True)
                        with Horizontal(id="detail-actions"):
                            yield Button("验收 r", id="accept-button", variant="success")
                            yield Button("补充/退回 b", id="return-button")
                            yield Button("记录", id="activity-button")
                            yield Button("动作", id="detail-menu")
        yield Static("", id="context-hint", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_tasks()
        self.apply_layout()
        self.focus_tasks()
        if self.runtime:
            self.set_interval(1, self.schedule_poll)
            self.schedule_poll()

    @property
    def selected_task(self) -> Task | None:
        return self.board.tasks.get(self.selected_id) if self.selected_id is not None else None

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "quit" or action not in {binding.action for binding in self.BINDINGS if isinstance(binding, Binding)}:
            return True  # Keep Textual's Tab/focus/accessibility actions in modals.
        if len(self.screen_stack) > 1:
            return False
        if isinstance(self.focused, (Input, TextArea, Select)):
            return action == "back"
        return True

    def visible_tasks(self) -> list[Task]:
        query = self.query_one("#search", Input).value.casefold().strip()
        return [task for task in self.board.tasks.values()
                if (self.project_name == "all" or task.project == self.project_name)
                and (self.status_filter == "all" or task.status == self.status_filter)
                and query in f"D{task.id:03} {task.title} {task.project} {task.description} {self.agent_label(task.agent)} {task.blocker} {self.board.projects[task.project]}".casefold()]

    def card(self, task: Task, compact: bool = False) -> Text:
        text = Text()
        text.append(f"{'!' if task.status == 'blocked' else '●'} {STATUSES[task.status]}", style=COLORS[task.status])
        text.append(f"  D{task.id:03}\n", style="#78879d")
        text.append(task.title, style="bold #dde6f3")
        text.append(f"\n{task.project}", style="#99a8bd")
        if not compact:
            text.append(f" · {self.agent_label(task.agent)}", style="#99a8bd")
        runtime = RUNS[task.run]
        if task.status == "done":
            runtime = "人工已验收"
        elif task.status == "review" and not task.full_delivery:
            runtime = "阶段交付，仍有剩余工作"
        prefix = "Pi · " if task.execution == "pi" else "模拟 · "
        text.append(f"\n{prefix + runtime if task.run != 'none' else '未启动'}\n", style=COLORS[task.status])
        return text

    def fill_tasks(self, widget: OptionList, tasks: list[Task], compact: bool = False) -> None:
        ids = [f"task-{task.id}" for task in tasks]
        current = [widget.get_option_at_index(i).id for i in range(widget.option_count)]
        if ids == current:
            for task, option_id in zip(tasks, ids):
                widget.replace_option_prompt(option_id, self.card(task, compact))
        else:
            widget.clear_options().add_options(Option(self.card(task, compact), id=option_id) for task, option_id in zip(tasks, ids))
        selected = f"task-{self.selected_id}"
        widget.highlighted = ids.index(selected) if selected in ids else (0 if ids else None)

    def refresh_tasks(self) -> None:
        if self.runtime and len(self.screen_stack) > 1:
            self._visual_dirty = True
            return  # Never rebuild the underlying selection while a form is being edited.
        self._visual_dirty = False
        focused = self.focused
        follow_lane = isinstance(focused, OptionList) and bool(focused.id and focused.id.startswith("lane-"))
        tasks = self.visible_tasks()
        if not any(task.id == self.selected_id for task in tasks):
            self.selected_id = tasks[0].id if tasks else None
        narrow_board = self.view_mode == "board" and self.viewport_width < 112
        flat = sorted(tasks, key=lambda t: list(STATUSES).index(t.status)) if narrow_board else tasks
        self.fill_tasks(self.query_one("#task-list", OptionList), flat)
        for key, label in LANES:
            lane = [task for task in tasks if task.status == key or (key == "running" and task.status == "blocked")]
            lane_widget = self.query_one(f"#lane-{key}", OptionList)
            lane_widget.can_focus = bool(lane)
            self.fill_tasks(lane_widget, lane, compact=True)
            self.query_one(f"#heading-{key}", Static).update(f"{label}  {len(lane):02}")
        nav = self.query_one("#nav-status", OptionList)
        labels = [("all", "全部任务"), ("review", "待我验收"), ("blocked", "需要补充"), ("running", "正在执行")]
        nav.clear_options().add_options(Option(f"{label}  {sum(key == 'all' or t.status == key for t in self.board.tasks.values())}", id=key) for key, label in labels)
        nav.highlighted = next((i for i, (key, _) in enumerate(labels) if key == self.status_filter), None)
        context = self.project_name if self.project_name != "all" else "全部项目"
        self.query_one("#context-line", Static).update(f"{context}  /  {FILTERS[self.status_filter]}  /  {len(tasks)} 个任务")
        self.query_one("#list-heading", Static).update("看板 · 窄屏按状态分组（Tab 切区）" if narrow_board else "任务队列                         Enter 查看详情")
        self.query_one("#empty").display = not tasks
        self.query_one("#task-list").display = bool(tasks)
        self.show_detail()
        if follow_lane:
            self.focus_tasks()

    def show_detail(self) -> None:
        task = self.selected_task
        detail = Text()
        if task:
            heading = Text(f"D{task.id:03}  ·  {STATUSES[task.status]}", style=f"bold {COLORS[task.status]}")
            self.query_one("#detail-heading", Static).update(heading)
            detail = delivery_text(task, self.board.runs.get(task.run_id))
            detail.append("安排\n", style="bold #79ccee")
            directory_label = "精确项目目录" if task.execution == "pi" else "样例目录（不会访问）"
            detail.append(f"项目    {task.project}\n执行者  {self.agent_label(task.agent)}\n{directory_label}\n{self.board.projects[task.project] or '未选择'}\n\n")
            detail.append("Pi 执行\n" if task.execution == "pi" else "模拟执行\n", style="bold #79ccee")
            detail.append(RUNS[task.run] + "\n创建 ≠ 分配 ≠ 启动 ≠ 执行确认 ≠ 人工验收\n\n")
            if task.execution == "pi":
                current = self.board.runs.get(task.run_id)
                if current:
                    detail.append(f"Run {current.id}\nSession {current.session_id}\n状态 {current.state} · 修订 {current.seq}\n")
                    if not current.terminal:
                        agent = self.agents.get(current.agent_id)
                        if not agent or not agent.fresh:
                            detail.append("心跳未知/失联 ≠ 已停止；不会自动解除或重派。\n", style="#f1b778")
                    if self.runtime_errors.get(current.id):
                        detail.append(self.runtime_errors[current.id] + "\n", style="#f1b778")
            detail.append("\n工具日志与历史轮次请点“记录”，或 Ctrl+K → 执行记录。\n", style="#a6b3c6")
        else:
            self.query_one("#detail-heading", Static).update("没有选中的任务")
            detail.append("换个筛选条件，或按 n 记下一个新任务。")
        self.query_one("#detail-text", Static).update(detail)
        self.query_one("#accept-button", Button).disabled = not bool(task and task.can_accept)
        self.query_one("#activity-button", Button).disabled = task is None
        self.query_one("#return-button", Button).disabled = not bool(task and task.status in ("review", "blocked") and not self.active_run(task))
        hints = "Enter 详情 · p 项目 · f 状态 · Ctrl+K 全部动作"
        if task:
            if task.status == "review":
                hints = "r 人工验收 · b 退回补充" if task.can_accept else "阶段交付不能整任务验收 · b 退回继续"
            elif self.active_run(task):
                hints = "g 刷新进展 · x 请求停止 · Pi 中确认权限；失联处理见 Ctrl+K"
            elif task.status == "blocked":
                hints = "需要你补充信息 · Enter 看原因 · b 补充后重新安排"
            elif task.run in ("queued", "working"):
                hints = "g 模拟确认 / 进展 / 阻塞 / 交付 · 不会真实执行"
            elif task.status in ("draft", "ready"):
                hints = "e 编辑 · a 分配 · s 显式启动（分配不会启动）"
        self.query_one("#context-hint", Static).update(hints + "  │  Esc 返回")

    def on_resize(self, event: events.Resize) -> None:
        self.viewport_width = event.size.width
        if self.is_mounted:
            focused = self.focused
            was_tasks = isinstance(focused, OptionList) and bool(focused.id and (focused.id == "task-list" or focused.id.startswith("lane-")))
            self.apply_layout()
            self.refresh_tasks()
            if was_tasks:
                self.call_after_refresh(self.focus_tasks)

    def apply_layout(self) -> None:
        width = self.viewport_width
        self.query_one("#sidebar").display = width >= 140
        board = self.view_mode == "board" and width >= 112
        self.query_one("#board").display = board and not self.detail_open
        self.query_one("#task-region").display = not board and not (self.detail_open and width < 100)
        self.query_one("#inspector").display = self.detail_open or (not board and width >= 100)
        self.query_one("#inspector").set_class(width < 100 or board, "full-detail")
        self.query_one("#view-button", Button).label = "列表  v" if self.view_mode == "board" else "看板  v"

    @on(Input.Changed, "#search")
    def search_changed(self) -> None:
        self.refresh_tasks()

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        widget = event.widget
        if isinstance(widget, OptionList) and widget.has_focus and widget.highlighted is not None and widget.id and widget.id.startswith("lane-"):
            task_id = widget.get_option_at_index(widget.highlighted).id
            if task_id:
                self.selected_id = int(task_id.removeprefix("task-"))
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
        task_id = int(event.option.id.removeprefix("task-"))
        if task_id != self.selected_id:
            self.selected_id = task_id
            self.query_one("#detail-scroll", VerticalScroll).scroll_home(animate=False)
            self.show_detail()

    @on(OptionList.OptionSelected)
    def select_option(self, event: OptionList.OptionSelected) -> None:
        source, key = event.option_list.id, event.option.id
        if source == "nav-project" and key:
            self.set_project(key)
        elif source == "nav-status" and key:
            self.set_filter(key)
        elif key and (source == "task-list" or (source and source.startswith("lane-"))):
            self.selected_id = int(key.removeprefix("task-"))
            self.detail_open = True
            self.show_detail()
            self.apply_layout()
            scroll = self.query_one("#detail-scroll", VerticalScroll)
            # Explicit opening returns to outcomes; ordinary progress refresh never jumps.
            scroll.scroll_home(animate=False)
            scroll.focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        actions = {"view-button": self.action_view, "palette-button": self.action_palette,
                   "detail-menu": self.action_palette, "new-button": self.action_new_task,
                   "filter-button": self.action_filter, "accept-button": self.action_review,
                   "return-button": self.action_return_work, "activity-button": self.action_activity}
        action = actions.get(event.button.id or "")
        if action:
            action()

    def focus_tasks(self) -> None:
        if self.selected_id is None:
            self.query_one("#new-button", Button).focus()
        elif self.detail_open:
            self.query_one("#detail-scroll", VerticalScroll).focus()
        elif self.view_mode == "board" and self.viewport_width >= 112:
            status = self.selected_task.status if self.selected_task else "draft"
            key = "running" if status == "blocked" else status
            self.query_one(f"#lane-{key}", OptionList).focus()
        else:
            self.query_one("#task-list", OptionList).focus()

    def action_back(self) -> None:
        self.detail_open = False
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

    def action_edit_task(self) -> None:
        task = self.selected_task
        if task:
            if task.execution == "pi" and task.run != "none":
                self.notify("本轮输入已固定；结束后用 b 补充/退回，再编辑。", severity="warning")
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

    async def choose_pi(self, task_id: int) -> None:
        if not self.runtime:
            return
        task = self.board.tasks[task_id]
        if task.status not in ("draft", "ready") or task.run != "none" or not self.board.projects[task.project]:
            self.notify("先选真实项目；只能分配尚未启动的任务。", severity="warning")
            return
        try:
            discovered = await asyncio.to_thread(self.runtime.discover)
        except (OSError, ValueError) as error:
            self.notify(f"读取 Pi 连接失败：{error}", severity="error")
            return
        self.agents.update((agent.id, agent) for agent in discovered)
        options = {agent.id: agent.label for agent in discovered if agent.cwd == self.board.projects[task.project]}
        if len(self.screen_stack) > 1:
            return
        options["__new_pi__"] = "＋ 新建 Pi · 独立标签页并连接（不派发）"
        options["__pi_help__"] = "接入已有 Pi · 查看帮助"
        def chosen(agent) -> None:
            if agent == "__new_pi__":
                self.new_agent_for(task_id)
            elif agent == "__pi_help__":
                self.action_pi_help()
            elif agent:
                self.mutate(lambda board: board.assign(board.tasks[task_id], agent))
        self.push_screen(Choice("选择 / 新建 Pi · 分配不会启动任务", options,
                                f"只列精确目录匹配的连接：{self.board.projects[task.project]}"), chosen)

    def action_new_agent(self) -> None:
        self.new_agent_for(self.selected_id)

    def new_agent_for(self, task_id: int | None) -> None:
        runtime = self.runtime
        if not runtime or not self.store:
            self.notify("新建 Pi 需要真实项目和本地任务库。", severity="warning")
            return
        task = deepcopy(self.board.tasks.get(task_id)) if task_id is not None else None
        project = task.project if task else self.project_name
        directory = self.board.projects.get(project, "")
        if not directory:
            self.notify("先按 p 添加或选择一个真实项目目录。", severity="warning")
            return
        expected = task if task and task.execution == "pi" and task.run == "none" and task.status in ("draft", "ready") else None
        async def create(name: str, model: str, launch_id: str) -> Launch:
            return await asyncio.to_thread(create_pi, runtime, directory, name, model, launch_id)
        def ready(agent: Agent) -> None:
            self.agents[agent.id] = agent
            if expected:
                if self.board.tasks.get(expected.id) != expected:
                    raise BridgeError("原任务在新建期间已改变，不会自动更换执行者")
                assigned_task_id = expected.id
                self.apply_change(lambda board: board.assign(board.tasks[assigned_task_id], agent.id))
        self.push_screen(NewAgentForm(directory, runtime, create, ready), lambda _: self.refresh_tasks())

    async def confirm_dispatch(self, task_id: int) -> None:
        if not self.runtime or self._sending:
            return
        task = deepcopy(self.board.tasks[task_id])
        try:
            if not self.store:
                raise BridgeError("真实派发必须先启用本地数据库。")
            if task.status != "ready" or task.run != "none":
                raise BridgeError("请先分配 Pi；本轮未结束时不会重复派发。")
            agent = await asyncio.to_thread(self.runtime.agent, task.agent)
            if not agent.available or agent.cwd != self.board.projects[task.project]:
                raise BridgeError("Pi 必须空闲、心跳有效，并处于项目的精确目录。")
        except (ValueError, OSError) as error:
            self.notify(str(error), severity="warning", timeout=8)
            return
        if len(self.screen_stack) > 1:
            return
        self.agents[agent.id] = agent
        body = f"D{task.id:03} · {task.title}\n\n精确目录：{agent.cwd}\nPi：{agent.label}\n连接：{agent.id}\n会话：{agent.session_id}\n\n{task.description}\n\n补充要求：{task.remaining or '无'}\n\n这会向该 Pi 发起真实任务，可按其现有工具权限读写文件和执行命令。\n保持 Pi 原有权限确认；请在 Pi 窗口处理提示。不会绕过确认、切换目录或自动验收。"
        def confirmed(yes) -> None:
            if yes:
                self.run_worker(self.dispatch_task(task, agent))
        self.push_screen(Confirm("真实派发 · 请核对任务与权限", body, "保存意图并派发"), confirmed)

    async def dispatch_task(self, expected: Task, agent: Agent) -> None:
        if self._sending or not self.runtime:
            return
        self._sending = True
        run = None
        try:
            if self.board.tasks[expected.id] != expected:
                raise BridgeError("任务在确认期间发生变化；请重新核对后派发。")
            run = self.apply_change(lambda board: board.queue_real(board.tasks[expected.id], str(uuid4()), agent.session_id, agent.cwd))
            # Write-ahead intent above must commit before anything can reach Pi.
            await asyncio.to_thread(self.runtime.dispatch, run)
            self.notify("派发请求已发布 · 等待 Pi 对应消息确认，不会自动重试")
        except (ValueError, OSError, StorageError) as error:
            if run is not None:
                self.record_runtime_warning(run, f"派发未确认：{error}。可能已发布；保留 Run 身份，仅观察，不会自动重试。")
            else:
                self.notify(f"未派发：{error}", severity="error", timeout=10)
        finally:
            self._sending = False
            self.refresh_tasks()
            self.schedule_poll()

    def record_runtime_warning(self, run: Run, message: str) -> None:
        self.runtime_errors[run.id] = message
        try:
            self.apply_change(lambda board: board.tasks[run.task_id].events.append(message))
        except (ValueError, StorageError) as error:
            message += f"\n警告也未能保存：{error}；原意图仍保留，请勿重派。"
        self.notify(message, severity="error", timeout=12)

    def schedule_poll(self) -> None:
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
        active = [deepcopy(run) for run in self.board.runs.values() if not run.terminal]
        if not active:
            self._polling = False
            return
        def read_updates() -> tuple[list[Run], dict[str, Agent], dict[str, str]]:
            updates, agents, errors = [], {}, {}
            for run in active:
                try:
                    incoming = runtime.read_run(run)
                    if incoming and incoming.seq > run.seq:
                        updates.append(incoming)
                except (ValueError, OSError) as error:
                    errors[run.id] = f"进展读取失败：{error}；无法确认结束，不会重派。"
                try:
                    agents[run.agent_id] = runtime.agent(run.agent_id)
                    if not agents[run.agent_id].fresh:
                        errors.setdefault(run.id, "Pi 离线/心跳过期；无法确认旧执行已停止，不会自动重派。")
                except (ValueError, OSError):
                    errors.setdefault(run.id, "连接身份/心跳不可读；仍会读取已有结果，不推断已停止。")
            return updates, agents, errors
        try:
            updates, agents, errors = await asyncio.to_thread(read_updates)
            changed = False
            for run in active:
                before = self.agents.get(run.agent_id)
                after = agents.get(run.agent_id)
                changed |= (before is None) != (after is None) or bool(before and after and (before.available != after.available or before.online != after.online))
                if after:
                    self.agents[run.agent_id] = after
                else:
                    self.agents.pop(run.agent_id, None)
                if errors.get(run.id) != self.runtime_errors.get(run.id):
                    changed = True
                if run.id in errors:
                    self.runtime_errors[run.id] = errors[run.id]
                else:
                    self.runtime_errors.pop(run.id, None)
            if updates:
                self.apply_change(lambda board: [board.ingest(incoming) for incoming in updates])
                self._poll_save_error = ""
                changed = True
            if changed:
                self.refresh_tasks()
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
        def confirmed(yes) -> None:
            if yes:
                self.run_worker(self.stop_run(run))
        self.push_screen(Confirm("请求停止本轮 Pi 执行", f"Run {run.id}\n会话 {run.session_id}\n目录 {run.cwd}\n\n仅停止工作台派发且仍能确认归属的这一轮；不退出 Pi，不中断其他工作。\n停止请求 ≠ 已停止，仍需等待 Pi 确认完全空闲。", "请求停止"), confirmed)

    async def stop_run(self, expected: Run) -> None:
        if self._sending or not self.runtime:
            return
        self._sending = True
        try:
            run = self.board.runs[expected.id]
            if run.terminal or self.board.tasks[run.task_id].run_id != run.id:
                raise BridgeError("这轮执行已结束或不再是当前轮次。")
            self.apply_change(lambda board: board.tasks[run.task_id].events.append(f"人工请求停止 · {run.id} · 等待 Pi 确认"))
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
        body = f"只会发现主动 /taskroom connect 的 Pi，不扫描或接管现有会话。\n\n在此工作台目录：\n{workbench}\n\n已有 Pi：\njust install-bridge '/你的精确项目目录'\n然后在该 Pi 中 /reload，再 /taskroom connect 并确认。\n只新增项目 .pi/extensions/taskroom.ts；已有不同内容则拒绝覆盖。\nPi 必须信任该项目才能加载；若没有 /taskroom，用下面显式启动方式。\n工作台不替你更改信任配置。\n\n新开 Pi：\njust pi '/你的精确项目目录'\n然后 /taskroom connect 并确认。\n\n当前项目目录：{directory or '先按 p 添加项目'}\n在工作台按 a 选择连接，s 核对任务并真实派发。\n每次重启、换会话、/reload 都要重新连接，旧任务不会自动重派。\n权限提示请在 Pi 中处理；工作台不会自动回答。\n\n断开：Pi 完全结束本轮后 /taskroom disconnect。\n不修改全局配置、不影响原 Code Board。"
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

    def action_activity(self) -> None:
        task = self.selected_task
        if task:
            self.push_screen(Confirm("执行记录 · 不代表交付成果", activity_text(self.board, task), "返回交付内容"))

    def action_review(self) -> None:
        task = self.selected_task
        if not task:
            return
        if not task.can_accept:
            self.notify("当前不能验收：需要完整交付，且没有剩余工作。", severity="warning")
            return
        report = delivery_text(task, self.board.runs.get(task.run_id)).plain
        evidence = "Pi 本轮已结束。请核实以上实际成果与范围；只有你明确确认，才标记本任务已验收。" if task.execution == "pi" else "证据全部为虚构，不代表真实测试通过；确认仅验收本地样例。"
        self.push_screen(Confirm("人工验收" if task.execution == "pi" else "人工验收样例", f"D{task.id:03}\n{report}\n{evidence}", "确认验收"), lambda yes: self.mutate(lambda board: board.accept(board.tasks[task.id])) if yes else None)

    def action_return_work(self) -> None:
        task = self.selected_task
        if not task or task.status not in ("review", "blocked"):
            self.notify("只有待验收或阻塞任务需要补充/退回。", severity="warning")
            return
        if self.active_run(task):
            self.notify("Pi 本轮尚未确认结束；请在 Pi 中处理权限提示或按 x 请求停止。", severity="warning")
            return
        self.push_screen(Note("补充信息 / 退回继续", "写清补充信息或未达到的验收要求…", save=lambda reason: self.apply_change(lambda board: board.return_work(board.tasks[task.id], reason)), live=task.execution == "pi"), lambda result: self.refresh_tasks() if result else None)

    def action_palette(self) -> None:
        actions = {"new_task": "新建草稿  n", "search": "搜索任务  /", "project": "切换项目  p", "filter": "筛选状态  f", "view": "切换列表 / 看板  v", "help": "使用帮助  ?"}
        if self.board.live:
            actions["add_project"] = "添加真实项目目录"
        if self.runtime:
            actions["new_agent"] = "新建 Pi · 独立标签页并连接"
            actions["pi_help"] = "接入已有 / 新开 Pi · 连接帮助"
        task = self.selected_task
        if task:
            actions["activity"] = "执行记录 · 工具日志与历史轮次（只读）"
            if task.execution == "simulation" or task.run == "none":
                actions["edit_task"] = "编辑标题 / 说明 / 项目  e"
            if task.status in ("draft", "ready") and task.run == "none":
                actions["assign"] = "选择已连接的 Pi（不启动） a" if task.execution == "pi" else "分配虚构执行者（不启动） a"
                actions["start"] = "核对任务并真实派发  s" if task.execution == "pi" else "显式模拟启动 · 仅排队  s"
            if self.simulation_options():
                actions["simulate"] = "模拟执行确认 / 进度 / 阻塞 / 交付  g"
            if self.active_run(task):
                actions["simulate"] = "刷新 Pi 进展（不重新派发） g"
                actions["stop"] = "请求停止当前 Pi 执行  x"
                actions["abandon"] = "失联恢复 · 人工核实旧 Pi 已停止后解除"
            if task.can_accept:
                actions["review"] = "人工验收完整交付  r"
            if task.status in ("review", "blocked") and not self.active_run(task):
                actions["return_work"] = "补充信息 / 退回继续  b"
        self.push_screen(Choice("任务动作", actions), lambda action: getattr(self, f"action_{action}")() if action else None)

    def action_help(self) -> None:
        storage_note = f"本地库：{self.store.path}\n提交成功的记录重启后保留；未提交表单不保存。" if self.store else "演示模式：只在内存里变化，退出即丢弃；重开恢复 12 个样例。"
        runtime_note = "s 核对并真实派发；g 刷新进展；x 请求停止。\nCtrl+K → 新建 Pi，或 a 选择 / 新建；创建与分配都不派发任务。\n退出工作台不会停止 Pi；重开只观察，不重派。\nPi 工具权限请在 Pi 窗口确认；交付证据自报，须人工核实。" if self.runtime else "s 模拟启动；g 模拟确认、进展、阻塞或交付。\n模拟任务没有真实执行，所有证据均为虚构。"
        self.push_screen(Confirm("先记下，再安排；交付后，由你验收。", "n 新建草稿（继承当前项目；全部项目下放入收集箱）\n/ 搜索  ·  p 项目/添加目录  ·  f 状态\n↑↓ 选任务  ·  Enter 详情  ·  Esc 返回\nTab / Shift+Tab 切区；详情用 ↑↓ / PgUp / PgDn 滚动\nv 列表 / 看板；窄屏看板按状态分组\nCtrl+K 搜索当前可用动作\n\ne 编辑  ·  a 分配（不会启动）\nr 人工验收  ·  b 补充/退回（不自动重启）\n详情先展示实际交付；“记录”查看工具日志（不验收）。\n重新 Enter 打开详情回到正文顶部，普通更新保留滚动。\n\n输入框内字母只输入，不触发任务快捷键。\n表单标题 Enter 保存；多行编辑 Ctrl+Enter 提交。\n\n" + storage_note + "\n\n" + runtime_note + "\n\n不读写原 Code Board。", "知道了"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TASKROOM 本地任务工作台 · 显式接入 Pi，交付由人验收")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--demo", action="store_true", help="只使用内存样例，不创建数据库")
    modes.add_argument("--db", type=Path, help="独立 SQLite 路径（默认 XDG_STATE_HOME/taskroom/tasks.sqlite3）")
    args = parser.parse_args(argv)
    if args.demo:
        TaskRoom().run()
        return 0
    try:
        with Store(args.db if args.db is not None else default_path()) as store:
            TaskRoom(store=store).run()
    except (StorageError, BridgeError) as error:
        print(f"TASKROOM 启动失败：{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
