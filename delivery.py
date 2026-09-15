"""Pure current-round views; compact briefs never replace full human acceptance."""
from rich.text import Text

from journal import Event, local_time, readable_event
from model import Run, Task


def delivery_text(task: Task, run: Run | None = None) -> Text:
    """Keep the complete acceptance body and its ordering unchanged."""
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
    current = run if task.run != "none" else None
    scope = current.description if current else task.description
    if current and current.requirements:
        scope += f"\n\n本轮补充要求：\n{current.requirements}"
    section("本轮要求 / 验收范围", scope or "未提供补充说明，请按任务标题核对范围。")
    evidence = "\n".join(f"• {check}" for check in task.checks)
    section("产物与核实依据 · 自报，不是独立验证" if task.execution == "pi" else "样例证据 · 全部虚构",
            evidence or "未提供产物位置或核实依据；请要求执行者补充，不从工具名称推断成果。")
    return text


def _next_step(task: Task, current: Run | None) -> str:
    if current and not current.terminal:
        if current.state == "waiting":
            return "你：到 Pi 处理权限或交互确认；工作台不会自动回答。"
        if current.state == "queued":
            return "等待 Pi 确认接收；尚未确定开始，不要重复派发。"
        if current.delivery in ("full", "partial", "blocked"):
            return "等待 Pi 完全空闲；报告已提交不等于执行结束或可验收。"
        return "等待执行者报告；需要时到 Pi 核实进度。"
    if task.run == "none":
        return "你：核对要求、选择项目和执行者，再明确派发。"
    if task.status == "done":
        return "本任务已由你验收；新的工作请另行安排。"
    if task.status == "review":
        return "你：核对本轮交付与证据，再人工验收。" if task.can_accept else "你：核对剩余工作，补充/退回；不能验收整任务。"
    if task.blocker:
        return "你：核实阻塞原因和执行状态，再补充/退回。"
    return "用 g 推进模拟，不会执行真实任务。" if task.execution == "simulation" else "你：核实本轮结果，再明确安排后续工作。"


def current_report(task: Task, run: Run | None) -> Event | None:
    """A historical/full-but-invalidated brief must not masquerade as today's result."""
    if not run or task.run == "none":
        return None
    latest = next((event for value in reversed(run.events)
                   if (event := readable_event(value)) and event.kind in {"progress", "blocked", "partial", "full"}), None)
    if latest and (report := latest.report) and latest.kind == run.delivery:
        projected_remaining = run.remaining if run.terminal else run.remaining or run.requirements
        if ((report.summary, report.checks, report.remaining) == (run.summary, run.checks, run.remaining)
                and (task.summary, task.checks, task.remaining) == (run.summary, run.checks, projected_remaining)):
            return latest
    return None


def overview_text(task: Task, run: Run | None = None) -> Text:
    current = run if task.run != "none" else None
    remaining = current.remaining if current else task.remaining
    event = current_report(task, current)
    brief = event.report.brief if event and event.report else None
    text = Text(task.title + "\n", style="bold #e6edf7")
    text.append("下一步 · " + _next_step(task, current) + "\n", style="#c5d1e2")
    if task.blocker:
        text.append("需要你补充 / 核实：" + task.blocker + "\n", style="#f1b778")
    if task.run == "none":
        text.append("尚未派发本轮执行，没有本轮交付。\n", style="#c5d1e2")
        if task.remaining:
            text.append("待派发的补充要求：" + task.remaining + "\n", style="#f1b778")
        if run:
            text.append("上轮仅作历史，请看「记录」。\n", style="#a6b3c6")
        return text
    if task.status == "review" and not task.can_accept:
        text.append("阶段交付 · 不能整任务验收\n", style="bold #f1b778")
    if remaining and remaining != task.blocker:
        text.append("剩余工作与限制：" + remaining + "\n", style="#f1b778")
    elif not remaining and not (task.can_accept or task.status == "done"):
        text.append("剩余工作尚未确认；不能视为完成。\n", style="#f1b778")
    caption = "执行者自报 · 须人工核实" if task.execution == "pi" else "模拟 · 全部虚构"
    text.append(caption + " · " + (local_time(event.at) if event else "报告时间未知或无匹配快照") + "\n", style="#a6b3c6")
    if brief:
        text.append("已做\n" + "\n".join(f"• {action}" for action in brief.actions) + "\n", style="#c5d1e2")
        text.append("交付：" + brief.outcome + "\n", style="#c5d1e2")
        text.append("验证（自报）：" + brief.verification + "\n", style="#c5d1e2")
    else:
        text.append("已有报告，展开查看。未提供简要动作/交付/验证，不自动总结或推断通过。\n" if task.summary else "尚无执行者报告；验证情况未知，不从工具数量推断进度。\n", style="#c5d1e2")
    return text


def detail_text(task: Task, run: Run | None = None) -> Text:
    text = Text(task.title + "\n", style="bold #e6edf7")

    def section(title: str, content: str, color: str = "#79ccee") -> None:
        text.append(title + "\n", style=f"bold {color}")
        text.append(content + "\n\n", style="#c5d1e2")

    current = run if task.run != "none" else None
    remaining = current.remaining if current else task.remaining
    event = current_report(task, current)
    brief = event.report.brief if event and event.report else None
    text.append("下一步 · " + _next_step(task, current) + "\n\n", style="#c5d1e2")
    if task.blocker:
        section("需要你补充 / 核实", task.blocker, "#f1b778")
    if task.run != "none":
        if task.status == "review" and not task.can_accept:
            text.append("阶段交付 · 不能整任务验收\n", style="bold #f1b778")
        if remaining and remaining != task.blocker:
            section("剩余工作与限制", remaining, "#f1b778")
        elif not remaining:
            text.append(("本轮已人工验收。" if task.status == "done" else "执行者自报无剩余项，仍需核实。" if task.can_accept else "剩余工作尚未确认；不能视为完成。") + "\n\n", style="#a6b3c6")
    if task.run == "none":
        section("本轮交付", "尚未派发本轮执行，没有本轮交付。")
        if task.remaining:
            section("待派发的补充要求", task.remaining, "#f1b778")
        if run:
            text.append("上轮仅作历史，请看「记录」。\n\n", style="#a6b3c6")
    else:
        text.append("执行者自报 · 须人工核实\n" if task.execution == "pi" else "模拟 · 全部虚构，没有真实执行\n", style="#a6b3c6")
        text.append("报告时间：" + (local_time(event.at) if event else "未知或无匹配的本轮快照") + "\n\n", style="#a6b3c6")
        if brief:
            section("本轮做了什么", "\n".join(f"• {action}" for action in brief.actions))
        section("本轮交付正文", task.summary or "尚未提供交付正文，工具调用不代表完成。")
        section("产物与核实依据 · 自报，不是独立验证", "\n".join(f"• {check}" for check in task.checks) or "未提供核实依据；不能推断验证通过。")
    section("任务说明 / 验收范围" + (" · 本轮派发时固定" if current else " · 当前保存的说明"),
            (current.description if current else task.description) or "未提供说明，请按任务标题核对范围。")
    if current and current.requirements:
        section("本轮补充要求 · 派发时固定", current.requirements)
    text.append("要求与补充按原文展示，不从进展推断新的验收标准。\n", style="#a6b3c6")
    return text
