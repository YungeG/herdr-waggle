"""Read-only, snapshotted business history. Pi remains the original process log."""
import asyncio
from collections.abc import Callable
from copy import deepcopy

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Static, Tree
from textual.widgets.tree import TreeNode

from journal import EVENT_LABELS, REPORT_KINDS, SOURCE_LABELS, Event, local_time, readable_event
from model import SUPPLEMENT_STATES, Board, Run, Task
from runtime import locate_session

STATES = {"queued": "等待确认", "working": "执行中", "waiting": "等待你", "finished": "执行已结束",
          "failed": "失败", "stopped": "已停止", "interrupted": "中断", "abandoned": "人工确认已停止"}


def event_body(event: Event) -> str:
    parts = [f"{EVENT_LABELS.get(event.kind, '关键变化')} · {SOURCE_LABELS[event.source]}", local_time(event.at)]
    if report := event.report:
        parts.append("当时保存的报告快照 · 不代表当前可以验收")
        if report.brief:
            parts.extend(["当时的动作\n" + "\n".join(f"• {action}" for action in report.brief.actions),
                          f"当时的交付概述\n{report.brief.outcome}", f"当时的验证概述 · 自报\n{report.brief.verification}"])
        parts.extend(["当时的完整正文\n" + report.summary,
                      "当时的依据 · 自报，非独立核实\n" + ("\n".join(f"• {check}" for check in report.checks) or "未提供依据。"),
                      "当时的剩余与限制\n" + (report.remaining or ("当时自报无剩余项。" if event.kind == "full" else "当时未列明；不代表无剩余项。"))])
    elif event.kind in REPORT_KINDS:
        parts.extend(["旧版报告仅保留以下摘录；当时的完整正文、依据与剩余项未保存为快照。", event.text])
    else:
        parts.append(event.text)
    return "\n\n".join(parts)


def round_body(run: Run) -> str:
    return (f"该轮最后保存的结果 · 自报，非选中事件的快照\n执行状态：{STATES[run.state]}\n"
            f"报告类型：{EVENT_LABELS.get(run.delivery, '未提供交付')}\n\n"
            f"{run.summary or '未提供交付正文。'}\n\n依据 · 自报\n" + ("\n".join(run.checks) or "未提供依据。") +
            "\n\n剩余与限制\n" + (run.remaining or ("执行者自报无剩余项；是否验收以任务当前状态为准。" if run.delivery == "full" else "未列明；不代表无剩余项。")) +
            f"\n\n该轮任务说明 · 派发时固定\n{run.description}\n\n该轮补充要求\n{run.requirements or '无补充要求。'}")


class TaskRecords(ModalScreen[None]):
    BINDINGS = [Binding("escape", "close", "返回"), Binding("ctrl+enter", "close", "返回", show=False, priority=True)]

    def __init__(self, board: Board, task: Task, *, show_location: Callable[[str], object]) -> None:
        super().__init__()
        self.task_snapshot = deepcopy(task)
        self.runs = {run.id: deepcopy(run) for run in board.runs.values() if run.task_id == task.id}
        self.supplements = {run_id: deepcopy(board.run_supplements(run_id)) for run_id in self.runs}
        self.show_location = show_location
        self.selected_run_id = ""

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="records-dialog"):
            yield Static(f"任务记录 · D{self.task_snapshot.id:03} · {self.task_snapshot.title}", classes="dialog-title", markup=False)
            yield Static("关键变化 · 只读，不验收 · ↑↓ 选择，Enter/空格展开轮次，Tab 切正文", id="records-notice", classes="muted")
            with Vertical(id="records-layout"):
                yield Tree("轮次与记录", id="records-tree")
                yield VerticalScroll(Static("", id="record-body", markup=False), id="record-body-scroll", can_focus=True)
            with Horizontal(classes="dialog-buttons"):
                yield Button("原 Pi 会话", id="locate-session", disabled=True)
                yield Button("返回  Esc", id="records-close")

    def on_mount(self) -> None:
        tree = self.query_one("#records-tree", Tree)
        tree.show_root = False
        tree.root.expand()
        notes = [event for raw in self.task_snapshot.events if (event := readable_event(raw))]
        has_gap = any(event.kind == "history_gap" for event in notes)
        first = None
        for index, run in reversed(list(enumerate(self.runs.values(), 1))):
            role = "本轮" if run.id == self.task_snapshot.run_id and self.task_snapshot.run != "none" else "历史"
            group = tree.root.add(Text(f"第 {index} 轮 · {role} · {STATES[run.state]}"), (run.id, None), expand=first is None)
            saved = [event for raw in run.events if (event := readable_event(raw))]
            has_gap |= any(event.kind == "history_gap" for event in saved)
            bound = [event for event in notes if event.run_id == run.id]
            # Bridge revision then human observation anchor, with stable append order on ties.
            typed = sorted([event for event in saved if event.id] + bound, key=lambda event: event.seq)
            for event in reversed(typed):
                node = group.add_leaf(self.label(event), (run.id, event))
                if first is None:
                    first = node
            legacy = [event for event in saved if not event.id]
            if legacy:
                old = group.add(Text("旧版记录 · 时间/修订未知"), (run.id, None), expand=not typed)
                for event in reversed(legacy):
                    node = old.add_leaf(self.label(event), (run.id, event))
                    if first is None:
                        first = node
            if first is None:
                first = group
        unbound = [event for event in notes if not event.run_id]
        if unbound:
            group = tree.root.add(Text("任务级记录 · 未关联具体轮次"), ("", None), expand=not self.runs)
            for event in reversed(unbound):
                node = group.add_leaf(self.label(event), ("", event))
                if first is None:
                    first = node
        if has_gap:
            self.query_one("#records-notice", Static).update("部分记录未保留 · 以下仅为保留的关键变化；关闭不会验收。")
        if first:
            tree.select_node(first)
        else:
            self.query_one("#record-body", Static).update("暂无已保存的关键记录；不从工具流水推断进度。")
        self.set_layout()
        tree.focus()

    @staticmethod
    def label(event: Event) -> Text:
        # The body keeps the exact text; rows contain only type/time, never clipped conclusions.
        return Text(f"{EVENT_LABELS.get(event.kind, '关键变化')} · {local_time(event.at)}")

    def on_resize(self, event: events.Resize) -> None:
        if self.is_mounted:
            self.set_layout()

    def set_layout(self) -> None:
        self.query_one("#records-layout").set_class(self.size.width >= 112, "wide")

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        self.show_node(event.node)

    def show_node(self, node: TreeNode) -> None:
        data = node.data
        if not data:
            return
        run_id, record = data
        self.selected_run_id = run_id
        body = event_body(record) if record else round_body(self.runs[run_id]) if run_id else "任务级记录未关联具体轮次；旧记录不补造时间或轮次。"
        if run_id and record is None and self.supplements[run_id]:
            body += "\n\n本轮补充说明 · 打开记录时的保存状态，不是当时报告的别名\n"
            for note in self.supplements[run_id]:
                body += f"\n{SUPPLEMENT_STATES[note.status]} · {note.id}\n{note.text}\n"
        self.query_one("#record-body", Static).update(body)
        self.query_one("#record-body-scroll", VerticalScroll).scroll_home(animate=False)
        self.query_one("#locate-session", Button).disabled = not bool(run_id)

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        self.show_node(event.node)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "records-close":
            self.action_close()
        elif event.button.id == "locate-session" and self.selected_run_id:
            run_id = self.selected_run_id
            event.button.disabled = True
            body = await asyncio.to_thread(locate_session, self.runs[run_id])
            if self.is_mounted and self.app.screen is self:
                event.button.disabled = not bool(self.selected_run_id)
                if self.selected_run_id == run_id:
                    self.show_location(body)

    def action_close(self) -> None:
        self.dismiss(None)
