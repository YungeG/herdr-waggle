"""Local step-plan UI: selecting or saving a step never dispatches execution."""
from collections.abc import Callable
from copy import deepcopy

from rich.style import Style
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Select, SelectionList, Static, TextArea

from journal import local_time
from model import Run, Task
from steps import STEP_LIMITS, STEP_STATES, Step
from store import StorageError

MARKERS = {"pending": "○", "doing": "●", "blocked": "!", "done": "✓"}
STEP_COLORS = {"pending": "#a6b3c6", "doing": "#79ccee", "blocked": "#f1b778", "done": "#90d4ad"}


class StepFlow(Static, can_focus=True):
    """Stable node IDs, literal Rich text, no clocks or model mutations on render."""
    BINDINGS = [Binding("up", "move(-1)", "上一步", show=False),
                Binding("down", "move(1)", "下一步", show=False),
                Binding("enter", "select", "查看步骤", show=False)]
    DEFAULT_CSS = """
    StepFlow { height: auto; margin-bottom: 1; padding: 0 1; background: #142a2d; }
    StepFlow:focus { border-left: solid #72bbd4; }
    """

    class Selected(Message):
        def __init__(self, task_id: int, step_id: str):
            super().__init__()
            self.task_id, self.step_id = task_id, step_id

    def __init__(self, **kwargs):
        super().__init__("", markup=False, **kwargs)
        self.task_id: int | None = None
        self.steps: list[Step] = []
        self.cursor = 0

    def show_plan(self, task: Task | None) -> None:
        task_id, steps = (task.id, task.steps) if task else (None, [])
        self.display = bool(steps)
        if (self.task_id, self.steps) == (task_id, steps):
            return
        self.task_id, self.steps = task_id, deepcopy(steps)
        self.cursor = next((i for i, step in enumerate(steps) if step.status in {"doing", "blocked"}), 0)
        self.refresh_flow()

    def on_resize(self, event: events.Resize) -> None:
        self.refresh_flow()

    def refresh_flow(self) -> None:
        labels = [f"{MARKERS[step.status]} {i + 1} {step.title}" for i, step in enumerate(self.steps)]
        horizontal = self.app.size.width >= 100 and Text(" → ".join(labels)).cell_len <= self.content_size.width
        text = Text("步骤计划 · 人工记录，不代表任务验收\n", style="bold #a7e0f2")
        for i, (step, label) in enumerate(zip(self.steps, labels)):
            if i:
                text.append(" → " if horizontal else "\n  ↓\n", style="#a6b3c6")
            style = Style(color=STEP_COLORS[step.status], underline=i == self.cursor,
                          meta={"step_id": step.id})
            text.append(label if horizontal else f"{label} · {STEP_STATES[step.status]}", style=style)
        current = next((step for step in self.steps if step.status in {"doing", "blocked"}), None)
        text.append("\n")
        if current:
            text.append(f"当前位置 · {current.title}（{STEP_STATES[current.status]}）\n", style="bold #79ccee")
            if current.status == "blocked":
                text.append("阻塞 · " + current.blocker + "\n", style="#f1b778")
            if current.next_action:
                text.append("下一步 · " + current.next_action + "\n", style="#c5d1e2")
        else:
            text.append("全部步骤已记录完成；仍须独立核对任务交付。\n" if self.steps and all(step.status == "done" for step in self.steps)
                        else "当前位置未指定；待处理不等于正在执行。\n", style="#c5d1e2")
        text.append("点击节点查看/记录 · Tab 聚焦后 ↑↓ / Enter", style="#a6b3c6")
        if self.content != text:
            self.update(text)

    def on_click(self, event: events.Click) -> None:
        step_id = event.style.meta.get("step_id")
        if event.button == 1 and self.task_id is not None and step_id in {step.id for step in self.steps}:
            event.stop()
            self.cursor = next(i for i, step in enumerate(self.steps) if step.id == step_id)
            self.refresh_flow()
            self.post_message(self.Selected(self.task_id, step_id))

    def action_move(self, direction: int) -> None:
        if self.steps:
            self.cursor = max(0, min(len(self.steps) - 1, self.cursor + direction))
            self.refresh_flow()

    def action_select(self) -> None:
        if self.steps and self.task_id is not None:
            self.post_message(self.Selected(self.task_id, self.steps[self.cursor].id))


class StepForm(ModalScreen[bool | None]):
    BINDINGS = [Binding("escape", "cancel", "返回"), Binding("ctrl+enter", "submit", "保存记录", priority=True)]
    DEFAULT_CSS = """
    StepForm { align: center middle; background: #090e17 80%; }
    StepForm TextArea { height: 5; min-height: 3; margin-bottom: 1; }
    StepForm SelectionList { height: 6; min-height: 3; }
    #step-error { height: auto; max-height: 4; color: #f5b28e; }
    """

    def __init__(self, task: Task, step: Step, runs: list[Run], *, save: Callable[..., None] | None):
        super().__init__()
        self.task_snapshot, self.step = deepcopy(task), deepcopy(step)
        self.runs, self.save = deepcopy(runs), save

    def compose(self) -> ComposeResult:
        step = self.step
        with Vertical(id="dialog"):
            yield Static(f"D{self.task_snapshot.id:03} · 步骤 {self.task_snapshot.steps.index(step) + 1} · {'只读' if self.save is None else '人工记录'}",
                         classes="dialog-title", markup=False)
            yield Static("只保存本地跟踪，不发送给 Pi；执行要求仍用“补充说明”。步骤完成 ≠ 任务验收。", classes="muted")
            with VerticalScroll(classes="form-body"):
                yield Static("标题", classes="field-label")
                yield Input(step.title, id="step-title", max_length=STEP_LIMITS["title"], disabled=self.save is None)
                yield Static("人工记录的状态", classes="field-label")
                yield Select(((label, key) for key, label in STEP_STATES.items()), value=step.status,
                             allow_blank=False, id="step-status", disabled=self.save is None)
                yield Static("最近更新 · " + local_time(step.updated_at) + " · 人工保存", classes="muted", markup=False)
                for name, label in [("goal", "本步目标"), ("output", "已交付 / 已完成内容"), ("evidence", "依据 / 核对记录（并非工作台验证）"),
                                    ("blocker", "阻塞原因"), ("next_action", "下一步 / 需要谁处理")]:
                    yield Static(f"{label} · 最多 {STEP_LIMITS[name]} 字符", classes="field-label")
                    yield TextArea(getattr(step, name), id="step-" + name, read_only=self.save is None)
                yield Static("关联本任务轮次 · 可多选，空格切换；不自动认领或续接", classes="field-label")
                yield SelectionList[str](*[(f"第 {i} 轮 · {run.id}", run.id, run.id in step.run_ids)
                                           for i, run in enumerate(self.runs, 1)], id="step-runs", disabled=self.save is None)
                if not self.runs:
                    yield Static("本任务尚无已保存的执行轮次。", classes="muted")
            yield Static("", id="step-error", markup=False)
            with Horizontal(classes="dialog-buttons"):
                yield Button("返回  Esc", id="cancel")
                if self.save is not None:
                    yield Button("保存步骤记录", id="submit", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#cancel", Button).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_submit(self) -> None:
        if self.save is None:
            return
        values = {name: self.query_one("#step-" + name, TextArea).text
                  for name in ("goal", "output", "evidence", "blocker", "next_action")}
        try:
            self.save(title=self.query_one("#step-title", Input).value,
                      status=str(self.query_one("#step-status", Select).value),
                      run_ids=list(self.query_one("#step-runs", SelectionList).selected), **values)
        except (ValueError, StorageError) as error:
            self.query_one("#step-error", Static).update("未保存：" + str(error))
            return
        self.dismiss(True)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "cancel":
            self.action_cancel()
        elif event.button.id == "submit":
            self.action_submit()
