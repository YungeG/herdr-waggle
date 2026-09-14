"""Pure delivery and diagnostic views; tool calls never stand in for outcomes."""
from rich.text import Text

from model import Board, Run, Task


def delivery_text(task: Task, run: Run | None = None) -> Text:
    """The same scope and evidence are shown in details and human acceptance."""
    text = Text()
    text.append(task.title + "\n\n", style="bold #e6edf7")

    def section(title: str, content: str, color: str = "#79ccee") -> None:
        text.append(title + "\n", style=f"bold {color}")
        text.append(content + "\n\n", style="#c5d1e2")

    if task.blocker:
        section("需要你补充", task.blocker, "#f1b778")
    caption = "Agent 自报，须人工核实" if task.execution == "pi" else "全部虚构，没有真实执行"
    heading = "完成了什么" if task.status in ("review", "done") else "当前进展"
    section(f"{heading} · {caption}", task.summary or "尚未提供交付正文。调用工具或回合结束不等于完成任务。")
    remaining = task.remaining
    if not remaining:
        if task.status == "done":
            remaining = "本轮已由人工验收，无剩余项；不代表整个项目完成。"
        elif task.can_accept:
            remaining = "执行者自报本轮无剩余项；仍需你验收，不代表整个项目完成。"
        else:
            remaining = "尚未确认无剩余项；不能把未提供的结果或未运行的检查当作完成。"
    section("剩余工作与限制", remaining, "#f1b778" if task.remaining else "#79ccee")
    # After rework, the old run remains historical; show the next task's input instead.
    current = run if task.run != "none" else None
    scope = current.description if current else task.description
    if current and current.requirements:
        scope += f"\n\n本轮补充要求：\n{current.requirements}"
    section("本轮要求 / 验收范围", scope or "未提供补充说明，请按任务标题核对范围。")
    evidence = "\n".join(f"• {check}" for check in task.checks)
    section("产物与核实依据 · 自报，不是独立验证" if task.execution == "pi" else "样例证据 · 全部虚构",
            evidence or "未提供产物位置或核实依据；请要求执行者补充，不从工具名称推断成果。")
    return text


def activity_text(board: Board, task: Task) -> str:
    """Read-only diagnostics, deliberately separate from delivery and acceptance."""
    parts = [f"D{task.id:03} · {task.title}", "工具返回成功不等于成果或测试已验证。以下仅为执行记录，关闭不会验收。"]
    current = board.runs.get(task.run_id)
    if current:
        parts.extend([f"Run {current.id}\nSession {current.session_id}\n状态 {current.state} · 修订 {current.seq}",
                      "本轮事件 · 最新在前", "\n".join(reversed(current.events)) or "尚无事件。"])
    previous = [run for run in board.runs.values() if run.task_id == task.id and run.id != task.run_id]
    for run in previous[-10:]:
        parts.append(f"历史轮次 {run.id} · {run.state}/{run.delivery or '无交付'}\n{run.summary}\n剩余：{run.remaining or '见该轮要求'}")
    parts.extend(["任务时间线 · 最新在前", "\n".join(f"{index:02}  {event}" for index, event in reversed(list(enumerate(task.events, 1))))])
    return "\n\n".join(parts)
