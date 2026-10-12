"""Pure primary-detail action projection; execution stays in app.py."""
from dataclasses import dataclass

from decisions import (
    DecisionSpec,
    context_for,
    decision_source,
    prior_answers,
    proposal_question,
    proposal_selected_keys,
    question_question,
    task_evidence_question,
    unchanged_evidence_keys,
)
from failure_view import OwnershipView, project_failure
from handoff import Handoff
from journal import current_report
from model import Board, Run, Task


def needs_supplement(task: Task | None, run: Run | None) -> bool:
    """Only an ended, current blocked report can open next-round planning."""
    return bool(task and run and task.execution == "pi" and task.retention == "active"
                and task.status == "blocked" and task.run != "none"
                and (run.id, run.task_id) == (task.run_id, task.id)
                and run.state in {"failed", "finished"} and run.delivery == "blocked")


def needs_next_round(task: Task | None, run: Run | None) -> bool:
    """Only a matching, ended partial delivery can offer next-round planning."""
    return bool(task and run and task.execution == "pi" and task.retention == "active"
                and task.status == "review" and task.run != "none" and not task.can_accept
                and (run.id, run.task_id) == (task.run_id, task.id)
                and run.state == "finished" and run.delivery == "partial")


@dataclass(frozen=True)
class PrimaryAction:
    action: str
    label: str
    reason: str = ""
    state: str = ""


@dataclass(frozen=True)
class DecisionItem:
    """Read-only main-page item, bound to the report that supplied its question."""
    task_id: int
    report_id: str
    spec: DecisionSpec
    state: str
    pending: bool

    @property
    def id(self) -> str:
        return f"decision:{self.task_id}:{self.report_id}:{self.spec.key}"


def decision_items(board: Board, task: Task | None) -> tuple[DecisionItem, ...]:
    if not task or task.execution != "pi":
        return ()
    try:
        event = decision_source(board, task.id)
        previous = prior_answers(board, task.id)
        waiting = set(unchanged_evidence_keys(board, task.id))
    except ValueError:
        return ()
    assert event.report and event.report.decisions
    latest = next((record for record in reversed(list(board.decisions.values())) if record.task_id == task.id), None)
    saved_notes: set[str] = set()
    if (latest and latest.intent.kind == "saved" and latest.preview.source.event_id == event.id
            and latest.specs == event.report.decisions and latest.preview.context == context_for(board, task.id)
            and all(answer.kind == "undecided" for answer in latest.preview.answers)):
        saved_notes = {spec.key for spec, answer in zip(latest.specs, latest.preview.answers, strict=True)
                       if answer.comment.strip()}
    rows = []
    for spec in event.report.decisions:
        old = previous.get(spec.key)
        if spec.key in saved_notes:
            state = "反馈仅保存 · 未发送" + ("；仍待负责人材料" if spec.key in waiting else "")
        elif spec.key in waiting:
            state = "等待负责人材料 · 无新证据"
        elif old:
            record, answer = old
            state = ("历史业务事实 · 待核对" if answer.business and record.preview.source.event_id != event.id else
                     "业务答案已记录" if answer.business and answer.kind == "undecided" else
                     "旧预算待核账本" if spec.kind == "budget" else {"request_info": "已要求补齐", "preference": "已记录意见", "defer": "已暂缓", "reject": "已拒绝"}.get(answer.kind, "已记录决定"))
            if record.intent.kind == "saved":
                state += " · 仅本地保存（未发送）"
            elif answer.kind in {"request_info", "preference"} and (not spec.complete or spec.kind == "budget"):
                state += " · 已提交补证（接收待核实）"
        else:
            state = "缺预算账本" if spec.kind == "budget" else "缺材料" if not spec.complete else "待决定"
        pending = spec.key in saved_notes or spec.key in waiting or old is None or spec.kind == "budget" or (not spec.complete and old[1].kind in {"preference", "request_info"}) or (old is not None and old[1].business is not None and old[1].kind == "undecided")
        rows.append(DecisionItem(task.id, event.id, spec, state, pending))
    return tuple(rows)


def _decision_action(items: tuple[DecisionItem, ...]) -> PrimaryAction:
    pending = [row for row in items if row.pending]
    if not pending:
        return PrimaryAction("decisions", "核对已存决定 d", "决定已记录；仅保存不等于已发送或已执行。", "决定待核对")
    missing = sum(not row.spec.complete or row.spec.kind == "budget" for row in pending)
    saved_count = sum(row.state.startswith("反馈仅保存 · 未发送") for row in pending)
    if saved_count:
        return PrimaryAction("decisions", f"核对已存反馈（{saved_count}）d",
                             f"{saved_count} 项反馈仅本地保存、尚未发送；进入决策单核对后才可明确提交。"
                             "原 Pi 若失联需选择可用 Pi；负责人原始材料仍需提供，不自动执行。", "反馈未发送")
    if pending and all(row.state.startswith("等待负责人材料") for row in pending):
        return PrimaryAction("decisions", f"等待负责人材料（{len(pending)}）d",
                             "补证轮没有新依据；不默认重启。d 可查看缺口并明确再次发送反馈。", "等待负责人材料")
    if pending and all(row.spec.routing and row.spec.routing.audience == "owner_evidence" and not row.spec.complete for row in pending):
        return PrimaryAction("decisions", f"负责人待补材料（{len(pending)}）d",
                             "当前缺口须由报告指定负责人提供原件；不需你批准，工作台未自动联系负责人。按 d 核对来源并明确请求补齐。", "等待负责人材料")
    if missing:
        return PrimaryAction("decisions", f"补齐决策材料（{len(pending)}）d", f"{len(pending)} 项待处理，其中 {missing} 项缺材料；本轮只能提交补证意见，不默认批准。", "待补齐决策材料")
    return PrimaryAction("decisions", f"逐项决定（{len(pending)}）d", f"{len(pending)} 项待处理；点击事项核对方案，推荐不是默认选择。", "待你决定")


def proposal_pending(run: Run, specs: tuple[DecisionSpec, ...]) -> bool:
    full_proposal = proposal_question(run.requirements) is not None
    if not full_proposal and question_question(run.requirements) is None:
        return False
    routed = {spec.key for spec in specs if spec.routing is not None}
    return ((full_proposal and (not specs or len(routed) != len(specs))) or
            any(key not in routed for key in proposal_selected_keys(run.requirements)))


def project_handoff(task: Task, run: Run | None, *, decisions: tuple[DecisionItem, ...] | None = None,
                    capability_issue: str = "") -> Handoff | None:
    """Action-first projection of current structured facts; never mine technical text for approval."""
    if task.execution != "pi" or task.status == "done" or task.retention != "active":
        return None
    current = run if task.run != "none" else None
    if current and (current.id, current.task_id) != (task.run_id, task.id):
        return None
    event = current_report(task, current) if current else None
    report = event.report if event else None
    short = report.handoff if report else None
    result = short.result if short else report.brief.outcome if report and report.brief else "本轮报告已保存，实际结果尚需核对。" if current and current.summary else "尚未收到本轮结果。"
    if capability_issue and (not current or current.terminal) and not task.can_accept:
        # A legacy brief is NOT the plain handoff contract: it may contain schema names or domain codes.
        # State only platform-known facts here; its complete self-report stays in details/history.
        connection_result = ("本轮报告已保存；交接尚未完成。" if report else "本轮记录已保留；交接结果待核对。") if current else "请求未发送；旧记录保留。"
        return Handoff(connection_result, "这条连接尚未证明能提交本次所需的交接。", "user",
                       "先核对并更新 Pi 桥接连接；不要重复发送同一请求。", "input")
    if current is None:
        if task.remaining.strip():
            previous = run if run and (run.id, run.task_id) == (task.run_id, task.id) and run.terminal and run.delivery in {"partial", "blocked"} else None
            return Handoff("请求仅本地保存，尚未发送；没有本轮交付。", "上轮交接缺口仍须单独核对。" if previous else "",
                           "user", "核对已存请求、目录、Pi 能力与可能费用后，再确认是否发送；也可继续仅保存。", "input")
        return None
    if not current.terminal:
        if short is None:
            return None  # Legacy in-flight progress retains its readable presentation; no new dispatch gate is inferred.
        if current.state == "waiting":
            return Handoff(result, "Pi 正在等待权限或交互确认。", "user", "到原 Pi 窗口核对确认；工作台不会替你回答。", "input")
        return Handoff(result, "报告已收到，但执行尚未结束。" if current.delivery in {"full", "partial", "blocked"} else "",
                       "executor", "继续本轮工作并提交核对结果；你无需重派。", "none")
    if task.can_accept:
        if short is None:
            return None  # Legacy complete reports retain the existing human-review presentation.
        return Handoff(result, "", "user", "核对完整交付与依据，再决定是否验收本轮。", "decision")
    specs = tuple(row.spec for row in decisions) if decisions is not None else (report.decisions or () if report else ())
    if proposal_pending(current, specs):
        if question_question(current.requirements) is not None:
            return Handoff(result, "已选旧问题的解释与来源尚未齐备。", "executor",
                           "逐项回答明确勾选的问题并给出已有材料来源；不要求提交整份方案或申请批准。", "none")
        return Handoff(result, "执行者尚未交齐可审提案或所选问题的答复。", "executor",
                       "补齐方案、已有依据、缺口归属及所选问题的逐项答复，再交回可审材料。", "none")
    if question_question(current.requirements) is not None:
        return Handoff(result, "本轮只解释已有材料，不是方案审批。", "user",
                       "核对答复与来源；可继续追问或回答明确业务问题，不形成技术方案批准。", "input")
    if specs:
        facts = [spec for spec in specs if spec.routing and spec.routing.audience == "user_fact" and spec.human_input]
        if facts:
            return Handoff(result, "有明确的业务问题待回答；技术材料仍由执行者补齐。", "user",
                           "打开逐项问题，回答所列业务事实；这不批准技术方案或预算。", "input")
        if any(not spec.complete or spec.kind == "budget" or (spec.routing and spec.routing.audience == "owner_evidence") for spec in specs):
            return Handoff(result, "方案依据或预算核对材料尚未齐备。", "executor",
                           "按报告所列缺口补材料；责任人或原件若未确定，先明确它们是否存在及由谁提供。", "none")
        return Handoff(result, "", "user", "打开逐项提案，核对方案、依据、风险和范围后再决定。", "decision")
    if short and short.user_need != "decision" and short.next_actor != "none":
        return short
    if current.delivery in {"partial", "blocked"} or short:
        blocker = "尚缺可执行的下一步交接；不是待你批准。" if current.remaining.strip() else "尚未提供明确剩余事项及下一步交接。"
        return Handoff(result, blocker, "executor",
                       "说明具体缺什么、谁能提供、下一步如何处理；材料未齐不要请求批准。", "none")
    return Handoff(result, "本轮没有可核验的完整交付。", "user", "先到原 Pi 核实执行情况，不要重复派发。", "input")


def project_primary_action(task: Task | None, run: Run | None, *, has_directory: bool,
                           needs_verification: bool = False, stop_requested: bool = False,
                           decisions: tuple[DecisionItem, ...] = (), capability_issue: str = "",
                           ownership: OwnershipView | None = None, dependency_issue: str = "") -> PrimaryAction:
    if task is None:
        return PrimaryAction("new_task", "新建任务 n")
    if task.retention != "active":
        return PrimaryAction("activity", "查看记录", "保留任务只读")
    if task.status == "done":
        return PrimaryAction("read_report", "查看成果")
    if task.can_accept:
        return PrimaryAction("review", "核对交付 r")
    if task.execution == 'pi' and run and (run.id, run.task_id) != (task.run_id, task.id):
        return PrimaryAction('read_report', '核实执行', '原轮次身份不符；不会安排恢复。')
    if task.execution == 'pi' and run and run.terminal and ownership and 'send_new_round' not in ownership.allowed_actions:
        return PrimaryAction('prepare_recovery', '准备恢复 · 仅保存', ownership.recommended_next_step, '活动/恢复待核对')
    if capability_issue and (task.run == "none" or run and run.terminal):
        return PrimaryAction("capability_help", "检查连接",
                             "你先核对 Pi 桥接连接；不是批准方案。不要重复发送。", "连接待核对")
    if task.execution == 'pi' and run and run.terminal and task.run != 'none' and run.state in {'failed', 'stopped', 'interrupted', 'abandoned'}:
        hint = ownership.recommended_next_step if ownership else project_failure(run).recommended_next_step
        return PrimaryAction('prepare_recovery', '准备恢复 · 仅保存', hint, '恢复待核对')
    if task.run != "none":
        if task.execution == "simulation":
            if task.run == "queued":
                return PrimaryAction("read_report", "查看派发状态")
            if task.status == "review":
                return PrimaryAction("read_report", "查看报告")
            if task.status == "blocked":
                return PrimaryAction("read_report", "查看结果")
            return PrimaryAction("read_report", "查看进展")
        if run is None or (run.id, run.task_id) != (task.run_id, task.id):
            return PrimaryAction("read_report", "核实执行", "当前轮次身份不可核验")
        if not run.terminal:
            if needs_verification:
                return PrimaryAction("read_report", "核实执行", "核实原 Pi；只保留最后可信状态，不要重派")
            if stop_requested:
                return PrimaryAction("read_report", "查看停止状态")
            if run.state == "queued":
                return PrimaryAction("read_report", "查看派发状态")
            if run.state == "waiting":
                return PrimaryAction("read_report", "查看确认信息")
            if decisions:
                return _decision_action(decisions)
            return PrimaryAction("read_report", "查看进展", "正在执行，暂不需要你操作；可按需查看进展。")
        if proposal_pending(run, tuple(row.spec for row in decisions)):
            if question_question(run.requirements) is not None:
                return PrimaryAction("read_report", "答复未齐 · 查看报告",
                                     "所选旧问题的解释与来源尚未交齐；先补齐答复，不需要你批准，也不自动重派。", "答复未齐")
            return PrimaryAction("read_report", "提案未交 · 查看报告",
                                 "执行者尚未交齐可审提案或所选旧问题的答复。现在不需要你批准；先补齐交接，不自动重派。", "提案未交")
        if decisions and run.state in {"finished", "failed"} and run.delivery in {"partial", "blocked"}:
            if question_question(run.requirements) is not None:
                return PrimaryAction("decisions", "核对答复／追问 d", "只核对已有材料的解释或业务事实，不批准技术方案。", "已有答复")
            return _decision_action(decisions)
        if needs_supplement(task, run):
            return PrimaryAction("return_work", "选择本轮目的 b",
                                 "先核对阻塞点及所需原方案/范围由谁提供；可仅保存，不自动派发。若需执行者列方案，可选仅补证提案。", "补充需求")
        if needs_next_round(task, run):
            return PrimaryAction("return_work", "索取可审提案 b",
                                 "执行者先交提案：方案、依据/负责人、已用预算。你不用批准；可仅保存，确认后仅补证（耗模型额度）。", "待执行者交提案")
        return PrimaryAction("read_report", "查看报告" if task.status == "review" else "查看结果")
    if not has_directory:
        return PrimaryAction("edit_task", "补充项目目录 e", "不能猜测 HOME 或仓库根目录")
    if dependency_issue:
        return PrimaryAction("dependencies", "核对局部依赖", dependency_issue + "；可先保存输入，不自动指派/派发。", "依赖待核对")
    previous = run if run and task.run_id == run.id else None
    from task_context import has_valid_contract, source_base
    if task.status == "ready" and previous and previous.terminal and not has_valid_contract(source_base(task.remaining)):
        return PrimaryAction("return_work", "补齐核查范围 b", "旧输入还缺本轮交付物、允许范围和验收依据；仅写‘继续’不能派发。")
    saved_proposal = proposal_question(task.remaining) if task.status == "ready" else None
    saved_direct_question = question_question(task.remaining) if task.status == "ready" else None
    saved_question = task_evidence_question(task.remaining) if task.status == "ready" and saved_proposal is None else None
    if not task.agent or task.status == "draft":
        return PrimaryAction("assign", "选择执行者 a",
                             "仅本地保存的补证问题尚未发送；先选同目录 Pi 再核对。" if saved_question is not None else "")
    if task.status == "ready":
        if saved_proposal is not None:
            return PrimaryAction("start", "核对已存提案 s",
                                 "可审提案请求仅本地保存、尚未发送；核对原问题、Pi、目录和模型费用后才确认发送，按 b 可修改。")
        if saved_question is not None:
            return PrimaryAction("start", "核对已存追问 s" if saved_direct_question is not None else "核对已存补证 s",
                                 "仅本地保存，尚未发送；核对目录、Pi、问题与模型额度后才确认发送；按 b 可修改，不授权改动或试验。")
        return PrimaryAction("start", "开始任务 s")
    return PrimaryAction("activity", "查看记录")
