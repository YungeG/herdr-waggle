"""Pure current-round views; compact briefs never replace full human acceptance."""
import re
from collections.abc import Collection

from rich.text import Text

from failure_view import project_failure
from journal import (
    EVENT_LABELS,
    Event,
    current_report,
    last_progress_report,
    local_time,
    parse_event,
    readable_event,
)
from model import Board, Run, Task
from task_actions import (
    DecisionItem,
    needs_next_round,
    needs_supplement,
    project_handoff,
)


def submission_status(board: Board, task: Task, *, published_runs: Collection[tuple] = (),
                      published_supplements: Collection[tuple] = ()) -> str:
    """Pure validated-state projection; write-ahead and self-report never prove publication or acceptance."""
    if task.execution != "pi":
        return "模拟状态 · 不代表真实发送、执行或验收。"
    if task.run == "none":
        return ("本次要求草稿已保存 · 未发送、未启动；请重新核对后明确提交。" if task.remaining.strip() else
                "尚未派发 · 创建或分配不代表发送与执行。")
    run = board.runs.get(task.run_id)
    if (not run or run.task_id != task.id or task.agent != run.agent_id
            or (task.title, task.description) != (run.title, run.description)
            or board.projects.get(task.project) != run.cwd):
        return "提交来源待核对；不能推定发布、接收或完成。"
    events = [event for raw in [*run.events, *task.events] if (event := parse_event(raw)) is not None
              and event.run_id == run.id]
    report = current_report(task, run)
    if report and (report.run_id != run.id or report.source != "executor" or not 0 < report.seq <= run.seq):
        report = None
    if task.status == "done":
        accepted = bool(run.terminal and run.delivery == "full" and report and report.kind == "full" and any(
            event.kind == "accepted" and event.source == "human" and event.seq == run.seq
            and event.at >= report.at for event in events))
        return ("本任务已人工验收 · 新工作须另行安排。" if accepted else
                "任务已标记验收 · 人工验收原记录待核对；不推定新的授权。")
    latest = next((record for record in reversed(list(board.decisions.values()))
                   if report and record.task_id == task.id and record.preview.source.run_id == run.id
                   and (record.preview.source.event_id, record.preview.source.seq) == (report.id, report.seq)
                   and (record.preview.context.title, record.preview.context.description,
                        record.preview.context.project, record.preview.context.cwd,
                        record.preview.context.requirements, record.preview.context.remaining) == (
                            task.title, task.description, task.project, run.cwd, run.requirements, task.remaining)
                   and record.intent.kind in {"draft", "saved"}), None)
    if latest:
        if latest.intent.kind == "draft":
            return "答复草稿已保存 · 尚未确认、未发送，不构成批准。"
        answers = latest.preview.answers
        if any(answer.kind in {"choice", "approve", "budget"} for answer in answers):
            return "正式决定已确认保存 · 未发送、未启动；不自动沿用旧批准。"
        if any(answer.business is not None for answer in answers):
            return "业务答案已确认保存 · 未发送，不批准技术方案或预算。"
        return "反馈已确认保存 · 未发送，不构成方案批准。"
    received = any(event.kind == "acknowledged" and event.source == "system"
                   and 0 < event.seq <= run.seq for event in events)
    if run.terminal:
        base = ("执行者自报报告已到达 · 尚未人工验收；空闲或完整自报不等于整任务通过。"
                if report and run.delivery in {"partial", "full"} else
                "执行已结束 · 报告原记录待核对，尚未人工验收；不自动重试。")
    elif received:
        base = "Pi 已接收本轮任务 · 尚未确认完成或人工验收。"
    elif run.identity in published_runs:
        base = "请求已发布 · 等待接收确认；未确认开始或完成，不会自动重试。"
    elif run.state == "queued":
        base = "派发意图已保存 · 发布未确认，接收未知；请核实原 Pi，不要重复派发。"
    else:
        base = "执行者状态已上报 · 接收消息原记录待核实，尚未确认完成或人工验收。"
    notes = board.run_supplements(run.id)
    if not notes:
        return base
    note = notes[-1]
    if (note.status in {"queued", "received", "covered"} and not (note.seq > 0 and 0 < note.run_seq <= run.seq)
            or note.status == "rejected" and not (note.seq > 0 and 0 <= note.run_seq <= run.seq)):
        extra = "补充回执修订待核对；不推定接收或覆盖。"
    elif note.status == "covered":
        extra = "补充已自报覆盖 · 尚未人工验收；请核对报告与原件。"
    elif note.status == "received":
        extra = "Pi 已接收补充 · 等待报告覆盖，不等于完成或验收。"
    elif note.status == "queued":
        extra = "补充已排入 Pi 队列 · 尚未确认接收或覆盖。"
    elif note.status == "rejected":
        extra = "补充未被接收 · 请核对原 Pi 与回执，不自动重发。"
    elif (run.identity, note.identity) in published_supplements:
        extra = "补充请求已发布 · 等待接收确认，不等于执行或覆盖。"
    else:
        extra = "补充意图已保存 · 投递未确认；请核实原 Pi，不要重发。"
    return base + "\n最近补充：" + extra


def delivery_text(task: Task, run: Run | None = None) -> Text:
    """Keep the complete acceptance body and its ordering unchanged."""
    text = Text()
    text.append(task.title + "\n\n", style="bold #e6edf7")

    def section(title: str, content: str, color: str = "#79ccee") -> None:
        text.append(title + "\n", style=f"bold {color}")
        text.append(content + "\n\n", style="#c5d1e2")

    if task.blocker:
        section("本轮阻碍（自报）" if task.execution == "pi" else "需要你补充", task.blocker, "#f1b778")
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
        return "等待后续进展；需要时到 Pi 核实。" if current.summary else "等待执行者报告；需要时到 Pi 核实进度。"
    if task.run == "none":
        return "你：核对要求、选择项目和执行者，再明确派发。"
    if task.status == "done":
        return "本任务已由你验收；新的工作请另行安排。"
    if task.status == "review":
        return "你：核对本轮交付与证据，再人工验收。" if task.can_accept else "你：核对剩余工作，补充/退回；不能验收整任务。"
    if task.blocker:
        return "你：核实阻塞原因和执行状态，再补充/退回。"
    return "用 g 推进模拟，不会执行真实任务。" if task.execution == "simulation" else "你：核实本轮结果，再明确安排后续工作。"


ROUND_STATES = {"queued": "等待确认", "working": "执行中", "waiting": "等待你", "finished": "执行已结束",
                "failed": "失败", "stopped": "已停止", "interrupted": "中断", "abandoned": "人工确认已停止"}


def decision_text(task: Task) -> Text:
    """State the human decision without promoting executor claims to proof."""
    if task.status not in {"review", "done"}:
        return Text()
    text = Text()
    if task.status == "done":
        text.append("人工决定 · 本轮已人工验收；新的工作需要另行安排。\n\n", style="#c5d1e2")
        return text
    if task.can_accept:
        text.append("人工决定 · 可进入人工核对，尚未验收。\n", style="bold #d0b0f5")
    else:
        text.append("人工决定 · 当前不能验收。\n", style="bold #f1b778")
    evidence = f"依据 · 执行者自报 {len(task.checks)} 条；" if task.checks else "依据 · 未上报核验依据；"
    if task.execution == "pi":
        text.append(evidence + "工作台未独立验证。\n\n", style="#a6b3c6")
    else:
        text.append(evidence + "此为本地模拟。\n\n", style="#a6b3c6")
    return text


def _round_events(task: Task, run: Run) -> list[Event]:
    saved = [event for raw in run.events if (event := readable_event(raw)) and event.id and event.at]
    bound = [event for raw in task.events if (event := readable_event(raw)) and event.id and event.at and event.run_id == run.id]
    return sorted(saved + bound, key=lambda event: event.seq)


def round_timeline_text(task: Task, runs: list[Run], *, include_heading: bool = True) -> Text:
    """Concise per-round history; detailed records remain in TaskRecords."""
    if not runs:
        return Text()
    text = Text()
    if include_heading:
        text.append("轮次与关键时间\n", style="bold #79ccee")
    text.append("按保存的轮次与修订顺序；时间仅标记记录，不推断执行状态。完整逐项记录见“记录”。\n\n", style="#a6b3c6")
    for number, run in reversed(list(enumerate(runs, 1))):
        role = "本轮" if task.run != "none" and run.id == task.run_id else "历史"
        color = "#d0b0f5" if role == "本轮" else "#a6b3c6"
        text.append(f"第 {number} 轮 · {role} · {ROUND_STATES.get(run.state, run.state)}\n", style=f"bold {color}")
        events = _round_events(task, run)
        if not events:
            text.append("关键时间 · 时间未知（旧版或未带时间的保留记录）\n\n", style="#a6b3c6")
            continue
        if len(events) == 1:
            event = events[0]
            text.append(f"关键记录 · {EVENT_LABELS.get(event.kind, '关键变化')} · {local_time(event.at)}\n\n", style="#c5d1e2")
            continue
        first = events[0]
        text.append(f"首条记录 · {EVENT_LABELS.get(first.kind, '关键变化')} · {local_time(first.at)}\n", style="#a6b3c6")
        for event in events[-2:]:
            text.append(f"最近操作 · {EVENT_LABELS.get(event.kind, '关键变化')} · {local_time(event.at)}\n", style="#c5d1e2")
        text.append("\n")
    return text


def report_text(task: Task, run: Run | None, runs: list[Run], *, addendum: str = "") -> str:
    """Read the report and contextual notes first; multi-round history is an appendix."""
    current = detail_text(task, run).plain + addendum
    if len(runs) < 2:
        return current
    timeline = round_timeline_text(task, runs, include_heading=False).plain
    return "## 当前轮报告\n\n" + current + "\n\n---\n\n## 轮次与关键时间\n\n" + timeline


def _overview_list(value: str, *, tight: bool = False) -> str:
    """Use compact lists in overviews; source records and reports stay unchanged."""
    if re.search(r"(?m)^\s*(?:[-*+•]|\d+[.)、])\s", value):
        return "\n".join(line for line in value.splitlines() if line.strip())
    items = (part.strip() for part in re.split(r"(?<=[。；])|\n+", value))
    return "\n".join(f"{'•' if tight else '• '}{item}" for item in items if item)


def prior_feedback_candidates(board: Board, task: Task) -> tuple[tuple[str, int, str], ...]:
    """Latest same-scope confirmed non-authorizing comments, never default grants."""
    scope = (task.title, task.description, task.project, board.projects[task.project])
    allowed = {"undecided", "request_info", "preference"}
    previous = next((record for record in reversed(list(board.decisions.values()))
                     if record.task_id == task.id and record.confirmed_at
                     and (record.preview.context.title, record.preview.context.description,
                          record.preview.context.project, record.preview.context.cwd) == scope
                     and any(answer.comment.strip() and answer.kind in allowed for answer in record.preview.answers)), None)
    if previous is None:
        return ()
    return tuple((f"{previous.id}:{index}", index, answer.comment) for index, answer in enumerate(previous.preview.answers, 1)
                 if answer.kind in allowed and answer.comment.strip())


def next_round_reference(board: Board, task: Task, run: Run | None) -> tuple[str, str]:
    """Read-only references for a NEW round; never put them into the editable request."""
    event = current_report(task, run)
    if not event or not event.report:
        return "", ""
    comments = [(index, comment) for _, index, comment in prior_feedback_candidates(board, task)]
    lines: list[str] = []
    hint = ""
    if comments:
        lines.append("你先前输入的反馈（旧报告；仅供参考，不是本轮批准）：")
        lines.extend(f"旧项 {index}. {comment}" for index, comment in comments)
        sample = "；".join(f"{index}.{comment[:22]}{'…' if len(comment) > 22 else ''}"
                          for index, comment in comments[:2])
        hint = f"旧反馈{len(comments)}项（不授权）：{sample}" + (f"；另{len(comments)-2}项↑" if len(comments) > 2 else "")
    if event.report.brief:
        lines.append("本轮结果（执行者自报，未独立验证）：" + event.report.brief.outcome)
    if event.report.remaining:
        lines.append("本轮待处理（执行者自报，不自动成为新要求）：\n" + event.report.remaining)
    if lines and not hint:
        hint = "本轮自报结果与待办见上方参考区；这些参考不构成本轮新要求。"
    return hint, "\n".join(lines)


def overview_text(task: Task, run: Run | None = None, *, decisions: tuple[DecisionItem, ...] | None = None,
                  capability_issue: str = "") -> Text:
    current = run if task.run != "none" else None
    remaining = current.remaining if current else task.remaining
    if task.execution == "pi" and current and (current.id != task.run_id or current.task_id != task.id):
        return Text("轮次身份不匹配；请核实原记录，不展示其他轮次的进展。", style="#f1b778")
    event = current_report(task, current)
    brief = event.report.brief if event and event.report else None
    text = Text("" if task.execution == "pi" else task.title + "\n", style="bold #e6edf7")

    def section(title: str, content: str, *, emphasis: bool = False, compact: bool = False) -> None:
        color = "#f1b778" if title in {"需要你", "卡在这里"} or title.startswith(("待处理事项", "上轮待确认事项")) else "#79ccee"
        text.append(" " + title + " ", style=f"bold #101620 on {color}")
        if compact:
            text.append("\n" + _overview_list(content, tight=True) + "\n",
                        style="bold #e6edf7" if emphasis else "not bold #c5d1e2")
            return
        text.append("\n" + _overview_list(content) + "\n\n", style="bold #e6edf7" if emphasis else "not bold #c5d1e2")

    if task.execution == 'pi' and current and current.terminal and current.state != 'finished' and not capability_issue:
        failure = project_failure(current)
        progress = last_progress_report(current)
        section('故障与限制', failure.category + ' · ' + failure.uncertainty, compact=True)
        section('失败前已有进度（自报）', progress.report.summary if progress and progress.report else '未提供真实进度报告；不推断已完成。', compact=True)
        section('下一步', failure.recommended_next_step + ' 可在“更多”准备恢复草稿；保存不发送。', compact=True)
        text.append('原始错误、报告身份和完整依据见详情；进度不能验收或作为正式授权来源。\n', style='#a6b3c6')
        return text

    handoff = project_handoff(task, run, decisions=decisions, capability_issue=capability_issue)
    if handoff is not None:
        if current and current.terminal and current.delivery == "partial" and not task.can_accept:
            text.append("阶段交付 · 不能整任务验收\n", style="bold #f1b778")
        section("本轮结果（自报）", handoff.result, emphasis=True, compact=True)
        if handoff.blocker:
            section("卡在这里", handoff.blocker, compact=True)
        actor = {"executor": "执行者", "user": "你", "none": "暂无操作"}[handoff.next_actor]
        section("下一步", actor + "：" + handoff.next_action, compact=True)
        user = ("现在需要你处理连接，不是批准任务方案。" if capability_issue else
                "现在不需要你批准；执行者先完成交接。" if handoff.user_need == "none" and handoff.next_actor == "executor" else
                "现在不需要你操作。" if handoff.user_need == "none" else
                "现在需要你核对投递；不是批准任务方案。" if task.run == "none" else
                "现在需要你核对并决定；推荐不等于批准。" if handoff.user_need == "decision" else
                "现在需要你补充信息或核对确认；不等于批准执行。")
        text.append(user + "\n", style="bold #f1b778" if handoff.user_need != "none" else "#c5d1e2")
        text.append("依据与技术原文见「展开详情 / 全屏阅读」；自报不等于验证或验收。\n", style="#a6b3c6")
        return text

    if current and needs_supplement(task, current):
        result = "\n".join(f"• {action}" for action in brief.actions) if brief else current.summary
        verification = brief.verification if brief else (
            f"已上报 {len(current.checks)} 条依据，工作台未独立验证；请查看完整报告。" if current.checks
            else "未提供核验说明；不能推断测试已运行或验证通过。")
        for label, body in (("需要你", remaining or "请核实阻塞原因，补充下一轮要求。"),
                            ("本轮结果（自报）", result or "未提供已完成工作的说明，请查看完整报告。"),
                            ("验证情况（自报）", verification)):
            section(label, body)
        return text
    if needs_next_round(task, current):
        text.append("阶段交付 · 不能整任务验收\n", style="bold #f1b778")
        if brief:
            section("本轮结果（自报）", brief.outcome, emphasis=True, compact=True)
        else:
            text.append("本轮报告已保存；实际结果请到全屏阅读核对。\n", style="#c5d1e2")
        section("待处理事项（自报）", remaining if remaining.strip() else
                "尚未提供明确剩余事项；先核对完整报告，再明确下一轮范围。", compact=True)
        if brief:
            section("已做工作（自报）", "\n".join(f"• {action}" for action in brief.actions), compact=True)
            section("核验与限制（自报）", brief.verification, compact=True)
        return text
    if task.execution != "pi" and (not current or current.state != "working" or current.delivery in ("full", "partial", "blocked")):
        text.append("下一步 · " + _next_step(task, current) + "\n", style="#c5d1e2")
    if task.execution == "simulation":
        text.append("模拟 · 全部虚构\n", style="#a6b3c6")
    if task.execution == "pi" and task.status != "done" and current and not current.terminal:
        if brief:
            section("最近进展（执行者自报）", "\n".join(f"• {action}" for action in brief.actions))
            section("阶段结果（自报）", brief.outcome)
        elif current.summary.strip():
            section("最近进展（执行者自报）", current.summary)
        else:
            section("最近进展", "尚未收到本轮进展报告；不代表执行未开始。")
        if task.blocker:
            section("需处理的问题", task.blocker)
        if remaining and remaining != task.blocker:
            section("剩余工作 / 下一步（自报）", remaining)
        if brief:
            section("核验说明（执行者自报）", brief.verification)
        elif current.checks:
            section("核验依据（执行者自报）", f"已上报 {len(current.checks)} 条依据，工作台未独立验证；展开详情查看。")
        elif current.summary.strip():
            section("核验说明", "未提供核实依据；不能推断验证通过。")
        return text
    if task.blocker:
        text.append("注意：有阻塞，需你核实（展开详情）。\n", style="#f1b778")
    if task.run == "none":
        # A saved next-round request hides the old report as a *current* result,
        # but must not hide its outstanding questions. This is historical text only.
        historical_pending = bool(task.execution == "pi" and task.retention == "active" and task.status in {"draft", "ready"}
                                  and run and (run.id, run.task_id) == (task.run_id, task.id)
                                  and run.terminal and run.delivery in {"partial", "blocked"} and run.remaining.strip())
        if historical_pending and run:
            section("上轮待确认事项（历史自报，不构成批准）", run.remaining)
        text.append("尚未派发本轮执行，没有本轮交付。\n", style="#c5d1e2")
        if task.remaining:
            if historical_pending:
                section("已保存待派发请求（未发送）", task.remaining)
            else:
                text.append("待派发的补充要求：有，展开查看。\n", style="#f1b778")
        if run:
            text.append("上轮仅作历史；完整报告与建议请看「记录」。\n", style="#a6b3c6")
        return text
    if task.status == "done":
        if brief:
            section("交付结果（自报）", brief.outcome)
            section("已完成", "\n".join(f"• {action}" for action in brief.actions))
            section("验证依据", brief.verification)
        else:
            completed = "\n".join(
                line if re.match(r"\s*(?:[-*+•]|\d+[.)、])\s", line) else f"• {line}"
                for line in task.summary.splitlines() if line.strip()
            )
            section("已完成（自报）", completed or "未保存最终交付正文。")
            evidence = "\n".join(f"• {check}" for check in task.checks)
            section("验证依据", evidence or "未保存验证依据；不能推断验证通过。")
    elif not brief:
        if current and current.terminal and current.state != "finished":
            reason = task.blocker or current.summary
            text.append("原因 · " + reason + "\n" if reason and len(reason) <= 160 else "完整原因与已有成果见详情。\n", style="#f1b778")
        elif task.summary:
            text.append("本轮报告已保存，完整内容见详情。\n", style="#c5d1e2")
        elif task.execution != "pi":
            text.append("进展：尚无执行者报告。\n", style="#c5d1e2")
    if task.status == "review" and not task.can_accept:
        text.append("完整自报尚不具备验收条件\n" if current and current.delivery == "full" else "阶段交付 · 不能整任务验收\n", style="bold #f1b778")
    if remaining and remaining != task.blocker and not needs_next_round(task, current):
        text.append("注意：有剩余工作或限制（展开详情）。\n", style="#f1b778")
    elif not remaining and not (task.can_accept or task.status == "done") and (task.execution != "pi" or (current and current.terminal)):
        text.append("剩余工作尚未确认；不能视为完成。\n", style="#f1b778")
    if brief and task.status != "done":
        section("本轮结果（自报）", brief.outcome, emphasis=True)
        section("已做工作（自报）", "\n".join(f"• {action}" for action in brief.actions))
        section("核验与限制（自报）", brief.verification)
    return text


def detail_parts(task: Task, run: Run | None = None, *, include_title: bool = True) -> tuple[Text, str, Text]:
    """Separate native facts from the current report body for inline Markdown."""
    before, after = Text(), Text()

    def section(target: Text, title: str, content: str, color: str = "#79ccee") -> None:
        target.append(title + "\n", style=f"bold {color}")
        target.append(content + "\n\n", style="#c5d1e2")

    if include_title:
        before.append(task.title + "\n", style="bold #e6edf7")
    current = run if task.run != "none" else None
    remaining = current.remaining if current else task.remaining
    event = current_report(task, current)
    brief = event.report.brief if event and event.report else None
    if task.execution != "pi":
        before.append("下一步 · " + _next_step(task, current) + "\n\n", style="#c5d1e2")
    if task.blocker:
        section(before, "本轮阻碍（自报） / 需核实" if task.execution == "pi" else "需要你补充 / 核实", task.blocker, "#f1b778")
    if task.run != "none":
        if task.status == "review" and not task.can_accept:
            before.append("完整自报尚不具备验收条件\n" if current and current.delivery == "full" else "阶段交付 · 不能整任务验收\n", style="bold #f1b778")
        if remaining and remaining != task.blocker:
            section(before, "剩余工作与限制", remaining, "#f1b778")
        elif not remaining and (task.execution != "pi" or (current and current.terminal)):
            before.append(("本轮已人工验收。" if task.status == "done" else "执行者自报无剩余项，仍需核实。" if task.can_accept else "剩余工作尚未确认；不能视为完成。") + "\n\n", style="#a6b3c6")
    body = ""
    if task.execution == 'pi' and run and run.terminal and run.state != 'finished' and (run.id, run.task_id) == (task.run_id, task.id):
        failure = project_failure(run)
        progress = last_progress_report(run)
        section(before, '故障分类与来源', f'{failure.category}\nRun {failure.run_id}\n事件 {failure.source_event_id or "未知"}\n{failure.uncertainty}', '#f1b778')
        section(before, '原始错误 · 未覆盖或改写', failure.evidence or '原始错误来源未知；请先核对。', '#f1b778')
        section(before, '恢复责任 / 下一步', failure.recommended_next_step, '#f1b778')
        if progress and progress.report:
            section(before, '失败前最近进度 · 执行者自报，不能验收或正式授权',
                    f'事件 {progress.id} · 修订 {progress.seq} · {local_time(progress.at)}\n{progress.report.summary}')
            section(before, '失败前核验依据 · 自报，不是独立验证', '\n'.join(progress.report.checks) or '未提供')
            section(before, '失败前未完成工作 · 自报', progress.report.remaining or '未提供，不能推断无剩余工作。')
            if progress.report.brief:
                section(before, '失败前结论 · 自报，不能验收或正式授权', progress.report.brief.outcome)
                section(before, '失败前做了什么 · 执行者自报', '\n'.join(f'• {action}' for action in progress.report.brief.actions))
                section(before, '失败前摘要核验与限制 · 自报，不是独立验证', progress.report.brief.verification)
    if task.run == "none":
        section(before, "本轮交付", "尚未派发本轮执行，没有本轮交付。")
        if task.remaining:
            section(before, "待派发的补充要求", task.remaining, "#f1b778")
        if run:
            before.append("上轮仅作历史，请看「记录」。\n\n", style="#a6b3c6")
    elif task.summary or task.checks:
        before.append("执行者自报 · 须人工核实\n" if task.execution == "pi" else "模拟 · 全部虚构，没有真实执行\n", style="#a6b3c6")
        before.append("报告时间：" + (local_time(event.at) if event else "未知或无匹配的本轮快照") + "\n\n", style="#a6b3c6")
        if brief:
            section(before, "本轮结论（自报）", brief.outcome)
            section(before, "本轮做了什么", "\n".join(f"• {action}" for action in brief.actions))
            section(before, "核验与限制（自报）", brief.verification)
        body = task.summary or "尚未提供交付正文，工具调用不代表完成。"
        if task.checks or (current and current.terminal):
            section(after, "产物与核实依据 · 自报，不是独立验证", "\n".join(f"• {check}" for check in task.checks) or "未提供核实依据；不能推断验证通过。")
    section(after, "任务说明 / 验收范围" + (" · 本轮派发时固定" if current else " · 当前保存的说明"),
            (current.description if current else task.description) or "未提供说明，请按任务标题核对范围。")
    if current and current.requirements:
        section(after, "本轮补充要求 · 派发时固定", current.requirements)
    after.append("要求与补充按原文展示，不从进展推断新的验收标准。\n", style="#a6b3c6")
    return before, body, after


def detail_text(task: Task, run: Run | None = None, *, include_title: bool = True) -> Text:
    before, body, after = detail_parts(task, run, include_title=include_title)
    if body:
        before.append("本轮交付正文\n", style="bold #79ccee")
        before.append(body + "\n\n", style="#c5d1e2")
    before.append_text(after)
    return before
