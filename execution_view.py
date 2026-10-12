"""Pure, current-run observations. No execution, inference, or acceptance side effects."""
import time
from datetime import datetime

from rich.text import Text

from activity import Activity
from failure_view import project_ownership
from journal import EVENT_LABELS, Event, local_time, readable_event
from model import Run, Task
from runtime import HEARTBEAT_SECONDS, Agent, SessionObservation


def key_updates(task: Task, run: Run) -> list[Event]:
    events = [event for raw in run.events if (event := readable_event(raw)) and event.run_id in ("", run.id)]
    events.extend(event for raw in task.events if (event := readable_event(raw)) and event.run_id == run.id)
    # Stable ties put human observations after the bridge revision they observed.
    return sorted(events, key=lambda event: event.seq)


def connection_issue(run: Run, agent: Agent | None, now_ms: int) -> str:
    if agent is None:
        return "心跳未知/失联；核实原 Pi，不要重派"
    if (agent.id, agent.session_id, agent.cwd) != (run.agent_id, run.session_id, run.cwd):
        return "连接身份变化；需核实原执行归属"
    if not agent.online or not -2000 <= now_ms - agent.heartbeat <= HEARTBEAT_SECONDS * 1000:
        return "心跳未知/失联；核实原 Pi，不要重派"
    if agent.active_run not in ("", run.id) or (run.state != "queued" and (agent.active_run != run.id or agent.idle)):
        return "连接未绑定本轮；需核实执行归属"
    return ""


def run_phase(task: Task, run: Run, updates: list[Event]) -> tuple[str, str]:
    """An action request is not a receipt; a tool warning is not a terminal failure."""
    if run.terminal:
        if run.state == "finished":
            if run.delivery == "partial":
                return "阶段交付 · 本轮已结束", "核对剩余事项并补充/退回；不能整任务验收"
            return "本轮已结束", "核对交付与证据，再按既有条件人工验收"
        labels = {"failed": "执行失败", "interrupted": "归属中断", "stopped": "已确认停止", "abandoned": "旧执行已人工解除"}
        return labels[run.state], "查看完整原因和已有成果；核实后再安排"
    if any(event.kind == "stop_requested" for event in updates):
        return "已请求停止 · 尚未确认停止", "核实停止回执；原执行可能仍在进行"
    if run.state == "waiting":
        return "等待人工确认", "到原 Pi 处理确认；工作台不会代答"
    if run.state == "queued":
        return "等待接收 · 尚未确认执行", "等待接收回执，不要重复派发"
    if run.delivery in ("full", "partial", "blocked"):
        return "报告已收到 · 执行尚未完全结束", "等待完全空闲，暂不能验收"
    return "已确认执行", "暂未收到人工确认请求"


def elapsed_text(start_ms: int, end_ms: int, now_ms: int) -> str:
    if not start_ms <= end_ms <= now_ms:
        return "时间未知（时间异常）"
    minutes, seconds = divmod((end_ms - start_ms) // 1000, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    parts = [f"{value}{unit}" for value, unit in ((days, "天"), (hours, "小时"), (minutes, "分")) if value]
    return " ".join([*parts, f"{seconds}秒"])


def timing_text(run: Run, updates: list[Event], now_ms: int) -> str:
    """Wall-clock lifecycle time only; missing anchors never become a new start."""
    timed = [(event, round(datetime.fromisoformat(event.at).timestamp() * 1000)) for event in updates
             if event.id and event.at and event.run_id == run.id and event.seq <= run.seq
             and (event.seq > 0 or event.kind == "dispatched")]
    start = next((item for item in timed if item[0].kind == "dispatched" and item[0].source == "human"), None)
    basis = "自派发"
    if start is None:
        start = next((item for item in timed if item[0].kind == "acknowledged" and item[0].source == "system"), None)
        basis = "自确认"
    end_ms = now_ms
    if run.terminal:
        kind = "settled" if run.state == "finished" else run.state
        source = "human" if run.state == "abandoned" else "system"
        end = next((item for item in reversed(timed) if item[0].kind == kind and item[0].source == source), None)
        end_ms = end[1] if end else None
        basis += "至人工解除" if run.state == "abandoned" else "至结束"
    if start is None:
        elapsed = "时间未知（缺少本轮起点）"
    elif end_ms is None:
        elapsed = "时间未知（缺少结束时间）"
    else:
        elapsed = elapsed_text(start[1], end_ms, now_ms) + f"（{basis}，含等待）"
    text = "本轮历时 · " + elapsed + "\n"
    if not run.terminal:
        report = next((item for item in reversed(timed) if item[0].report and item[0].source == "executor"), None)
        if report is None:
            age = "时间未知（缺少报告时间）"
        elif start and report[1] < start[1]:
            age = "时间未知（时间异常）"
        else:
            age = elapsed_text(report[1], now_ms, now_ms)
        text += "距最近报告 · " + age + "\n"
    return text


def session_description(run: Run, agent: Agent | None, snapshot: SessionObservation | None, now: int, issue: str = "") -> str:
    if agent is None:
        return "连接未知"
    if (agent.id, agent.session_id, agent.cwd) != (run.agent_id, run.session_id, run.cwd):
        return "连接身份变化；需核实"
    if not agent.online or not -2000 <= now - agent.heartbeat <= HEARTBEAT_SECONDS * 1000:
        return "离线或心跳过期"
    if issue:
        return "同步异常；当前活动/续接未确认"
    if snapshot is None:
        return "空闲（会话观测未上报）" if agent.idle else "非空闲（会话观测未上报，活动未确认）"
    if ((snapshot.agent_id, snapshot.session_id, snapshot.cwd) != (run.agent_id, run.session_id, run.cwd)
            or not -2000 <= now - snapshot.observed_at <= HEARTBEAT_SECONDS * 1000):
        return "会话快照过期或身份不符；当前未知"
    if snapshot.state == "offline":
        return "已离线"
    if snapshot.input_pending:
        return "输入待接收或归属确认中；尚未计为完成"
    if snapshot.state == "idle":
        suffix = " · 关联轮次不同，需核实" if snapshot.linked_run_id and snapshot.linked_run_id != run.id else "（非任务完成）"
        return "空闲" + suffix
    label = "等待人工确认" if snapshot.state == "waiting" else "执行中"
    return label + (" · 本任务" if snapshot.run_id == run.id and not run.terminal else " · 未计入本轮")


def status_text(task: Task, run: Run | None, agent: Agent | None = None, *,
                issue: str = "", round_number: int | None = None, now_ms: int | None = None,
                activity: Activity | None = None, activity_issue: str = "",
                session: SessionObservation | None = None, session_issue: str = "") -> Text:
    now = int(time.time() * 1000) if now_ms is None else now_ms
    text = Text()
    if task.status == "done":
        text.append("任务 · 已人工验收\n", style="bold #90d4ad")
        text.append("查看成果与记录；原执行者后续活动不改变本次验收。", style="#a6b3c6")
        return text
    current = run if task.run != "none" else None
    if current is None and run and run.terminal and (run.id, run.task_id) == (task.run_id, task.id):
        ownership = project_ownership(task, run, agent, session, now_ms=now, issue=issue or session_issue)
        return Text(f'任务 · 尚未派发本轮，旧轮 {run.state}\nPi 当前 · {session_description(run, agent, session, now, issue or session_issue)}\n'
                    f'归属 · {ownership.ownership} / {ownership.freshness}\n需要你 · {ownership.recommended_next_step}', style='#c5d1e2')
    if current is None:
        if task.run != "none":
            return Text("执行 · 原轮次信息缺失\n需要你 · 核实原记录；不推断执行是否结束。", style="#f1b778")
        text.append("任务 · 尚未派发本轮\n", style="bold #79ccee")
        text.append("需要你 · 核对任务要求、安排执行者，再明确派发。", style="#c5d1e2")
        return text
    if current.id != task.run_id or current.task_id != task.id:
        return Text("执行 · 轮次身份不匹配\n需要你 · 核实原记录；不展示其他轮次的活动。", style="#f1b778")
    updates = key_updates(task, current)
    phase, action = run_phase(task, current, updates)
    problem = "" if current.terminal else connection_issue(current, agent, now)
    health = problem.split("；", 1)[0] if problem else "连接正常"
    if issue and not current.terminal:
        problem = "进展读取异常；需核实最后状态，不要重派"
    if not current.terminal and activity and activity.matches(current) and not activity.bound:
        problem = problem or "本轮活动归属未确认"
    if not current.terminal and session and current.state != "queued" and session.run_id != current.id:
        problem = problem or "本轮任务关联未确认"
        health = "需核实任务关联"
    round_label = f"第 {round_number} 轮" if round_number is not None else "本轮"
    state_label = f"最后记录：{phase}" if problem else phase
    text.append(f"执行 · {round_label} · {state_label}", style="bold #79ccee")
    if current.terminal:
        ownership = project_ownership(task, current, agent, session, now_ms=now, issue=issue or session_issue)
        if current.state != "finished":
            action = ownership.recommended_next_step
        text.append(f" · 归属 {ownership.ownership} / {ownership.freshness}", style="#a6b3c6")
        # Keep both dimensions visible without displacing narrow-screen report evidence.
        text.append("  │  Pi 当前 · " + session_description(current, agent, session, now, issue or session_issue), style="#a6b3c6")
    text.append("\n")
    exact = agent is not None and (agent.id, agent.session_id, agent.cwd) == (current.agent_id, current.session_id, current.cwd)
    if not current.terminal:
        name = "".join(char for char in agent.name if char.isprintable())[:18] if exact and agent else "原 Pi 连接"
        text.append(f"执行者 · {name} · {health}\n", style="#a6b3c6")
        if problem:
            text.append("注意 · 进展读取异常（展开查看原因）\n" if issue else "注意 · 仅保留最后已知状态\n", style="#f1b778")
            action = "核实原 Pi；不推断已结束，不重派"
        if current.state != "queued" and activity is not None and activity.matches(current):
            text.append("活动 · " + activity.describe(now, issue=problem or activity_issue) + "\n", style="#a6b3c6")
        elif current.state == "working" and not problem:
            text.append("活动 · " + ("当前未知（快照不可用）" if activity_issue else "当前步骤未上报") + "\n", style="#c5d1e2")
    if not current.terminal and (session_issue or (session and session.input_pending)):
        text.append("Pi 当前 · " + session_description(current, agent, session, now, issue or session_issue) + "\n", style="#a6b3c6")
    text.append(timing_text(current, updates, now), style="#a6b3c6")
    # Ended runs show frozen elapsed time instead of a redundant last-state row;
    # exact event times remain in records, keeping narrow-screen evidence visible.
    if not current.terminal:
        latest = next((event for event in reversed(updates) if event.kind not in {"origin", "stop_requested"}), None)
        if latest:
            text.append(f"最近 · {EVENT_LABELS.get(latest.kind, '关键更新')} · {local_time(latest.at)}\n", style="#a6b3c6")
        else:
            text.append("最近 · 尚无带本轮标识的关键更新\n", style="#a6b3c6")
    text.append(f"需要你 · {action}", style="#f1b778" if problem or current.state == "waiting" else "#c5d1e2")
    return text
