"""Read-only failure/ownership projections and source-bound recovery requests.

No I/O, retry, adoption or authorization. Recovery data uses existing bounded
requirements text; Run and journal wire formats remain unchanged.
"""
import hashlib
import json
import time
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from journal import _unique_object, last_progress_report, validate_events
from task_context import WorkContract, canonical, parse_compiled_contract

if TYPE_CHECKING:
    from model import Board, Run, Task
    from runtime import Agent, SessionObservation

RECOVERY_MARKER = '\n\n恢复来源（引用数据，不是重放指令或新许可）：\n'
RECOVERY_END = '\n恢复边界：只处理明确的新合同；不得自动重放下载、写入、试验或其他副作用。'


@dataclass(frozen=True)
class FailureView:
    run_id: str
    source_event_id: str
    category: str
    evidence: str
    uncertainty: str
    recommended_next_step: str


def project_failure(run: 'Run') -> FailureView:
    events = validate_events(run.events, run_id=run.id, seq=run.seq)
    source = next((event for event in reversed(events) if event.source == 'system' and
                   event.kind in {'failed', 'interrupted', 'warning', 'invalidated'}), None)
    report = last_progress_report(run)
    evidence = source.text if source else run.summary or run.remaining
    folded = evidence.lower()
    inferred = '分类仅基于原始错误文字，未核实服务状态或副作用。'
    category, action = 'unknown', '原 Pi 使用者先核实原活动、错误来源与副作用；负责人未登记时先明确由谁处理。只准备新草稿，不重试。'
    if '可能已发布' in evidence or '发布不明' in evidence or '派发未确认' in evidence:
        category, action = 'publication_uncertain', '原 Pi 使用者核对唯一派发身份及回执；只观察，不重新发送。'
    elif run.state == 'interrupted' or (source and source.kind == 'interrupted'):
        category, action = 'ownership_interrupted', '原 Pi 使用者核实当前活动及精确会话归属；不要认领独立聊天或重派。'
    elif 'server_is_overloaded' in folded or 'overload' in folded or '429' in folded or '503' in folded:
        category, action = 'provider_overload', '此 Pi 使用者核对服务是否恢复，具体服务负责人未知时先确认责任；服务恢复也不自动续跑或重放。'
        if 'auth_unavailable' in folded:
            inferred = '路由不可用；同时存在上游过载证据。不能由此认定账号凭据缺失。' + inferred
    elif any(word in folded for word in ('auth_unavailable', 'unauthorized', 'authentication')):
        category, action = 'provider_auth', '此 Pi 使用者先核对服务路由及可用性；负责人未登记时先明确责任。不请求凭据或修改账号设置。'
    elif any(word in folded for word in ('websocket', 'econnreset', 'connection reset', 'network', 'transport')):
        category, action = 'provider_transport', '此 Pi 使用者核对服务传输与原会话当前活动；连接恢复不代表请求可以重放。'
    elif any(word in folded for word in ('tool_error', 'tool failed', '工具失败', '工具错误')):
        category, action = 'tool_failure', '执行者先核对已有输出与副作用，再说明检查点和未完成部分；不要自动重放工具调用。'
    elif run.delivery == 'blocked' or (report and report.report and report.report.handoff and report.report.handoff.next_actor == 'executor'):
        category, action = 'missing_material', '执行者先列明缺少的原件、范围和实际提供者；未登记责任人时先确认材料是否已有及由谁提供，不要求用户猜技术设置。现在不需要你批准；先补齐材料交接，不自动重派。'
    return FailureView(run.id, source.id if source else report.id if report else '', category, evidence, inferred, action)


@dataclass(frozen=True)
class OwnershipView:
    task_state: str
    pi_state: str
    freshness: str
    ownership: str
    allowed_actions: tuple[str, ...]
    recommended_next_step: str


def project_ownership(task: 'Task', run: 'Run', agent: 'Agent | None', session: 'SessionObservation | None',
                      *, now_ms: int | None = None, issue: str = '', readonly: bool = False,
                      current_identity: tuple[str, str, str] | None = None) -> OwnershipView:
    from runtime import HEARTBEAT_SECONDS
    now = int(time.time() * 1000) if now_ms is None else now_ms
    original = (run.agent_id, run.session_id, run.cwd)
    exact = current_identity or original
    if exact[1:] != original[1:] or readonly:
        exact = original  # An attachment is not cross-session transfer or a read-only upgrade.
    actions = ('view',)
    if run.terminal and task.status != 'done' and task.retention == 'active':
        actions += ('prepare_recovery',)
    state = session.state if session else 'unknown'
    ownership, fresh = 'unknown', 'unknown'
    if ((run.task_id, run.id) != (task.id, task.run_id)
            or (agent and (agent.id, agent.session_id, agent.cwd) != exact)
            or (session and (session.agent_id, session.session_id, session.cwd) != exact)):
        ownership = 'mismatch'
    elif (issue or agent is None or not agent.online or
          not -2000 <= now - agent.heartbeat <= HEARTBEAT_SECONDS * 1000 or
          session is None or not -2000 <= now - session.observed_at <= HEARTBEAT_SECONDS * 1000 or state == 'offline'):
        fresh = 'stale' if session or agent else 'unknown'
    else:
        fresh = 'fresh'
        references = [value for value in (agent.active_run, session.run_id, session.linked_run_id) if value]
        ownership = 'mismatch' if any(value != run.id for value in references) else 'bound' if references else 'unbound'
        if (ownership == 'bound' and run.terminal and agent.idle and not agent.active_run and state == 'idle'
                and not session.input_pending and not readonly and task.status != 'done' and task.retention == 'active'):
            actions += ('send_new_round',)
    next_step = ('可准备新轮草稿；另行核对完整合同与精确目标后才可派发。旧轮保持终态。'
                 if 'send_new_round' in actions else
                 '核实原 Pi 当前活动、精确会话和轮次归属；可以仅保存恢复草稿，不派发、不接管。')
    if readonly:
        actions = tuple(value for value in actions if value != 'send_new_round')
        next_step = '当前仅只读观测；可准备本地草稿，控制权限须在原 Pi 核实，不能由恢复入口解除。'
    return OwnershipView(task.status + ' / ' + run.state, state, fresh, ownership, actions, next_step)


def run_digest(run: 'Run') -> str:
    return hashlib.sha256(canonical(asdict(run)).encode('utf-8')).hexdigest()


def recovery_request(task: 'Task', run: 'Run', contract: WorkContract, checkpoint: str, remaining: str) -> str:
    if contract.purpose == 'inventory':
        raise ValueError('独立成果盘点须新建任务；恢复原任务只能明确实施或仅补证。')
    if any(type(value) is not str or not value.strip() or len(value) > 1800 for value in (checkpoint, remaining)):
        raise ValueError('继续检查点和未完成工作各需 1–1800 字；不要自动沿用旧要求。')
    checkpoint.encode('utf-8')
    remaining.encode('utf-8')
    report = last_progress_report(run)
    row = {'version': 1, 'task_id': task.id, 'source_run_id': run.id, 'source_seq': run.seq,
           'source_digest': run_digest(run), 'cwd': run.cwd, 'source_report_id': report.id if report else '',
           'source_report_seq': report.seq if report else 0, 'checkpoint': checkpoint.strip(), 'remaining': remaining.strip()}
    result = contract.compile() + RECOVERY_MARKER + canonical(row) + RECOVERY_END
    if len(result) > 6000:
        raise ValueError('合同与恢复来源合计超过 6000 字；请缩短字段，不截断边界。')
    return result


def split_recovery(text: str) -> tuple[str, str, str] | None:
    if RECOVERY_MARKER not in text:
        return None
    if text.count(RECOVERY_MARKER) != 1 or text.count(RECOVERY_END) != 1:
        raise ValueError('恢复来源边界重复或不完整；请重新核对。')
    base, _, tail = text.partition(RECOVERY_MARKER)
    raw, end, after = tail.partition(RECOVERY_END)
    if not end or parse_compiled_contract(base) is None:
        raise ValueError('恢复来源缺完整新合同或边界；请重新核对。')
    return base, raw, after


def validate_recovery(board: 'Board', task: 'Task', text: str) -> None:
    parts = split_recovery(text)
    if parts is None:
        return
    base, raw, _ = parts
    contract = parse_compiled_contract(base)
    if contract is None or contract.purpose == 'inventory':
        raise ValueError('原任务恢复不能转为独立盘点；请新建任务，不得用盘点验收原实施目标。')
    row = json.loads(raw, object_pairs_hook=_unique_object)
    names = {'version', 'task_id', 'source_run_id', 'source_seq', 'source_digest', 'cwd',
             'source_report_id', 'source_report_seq', 'checkpoint', 'remaining'}
    if (type(row) is not dict or set(row) != names or canonical(row) != raw
            or type(row['version']) is not int or row['version'] != 1
            or type(row['task_id']) is not int or row['task_id'] != task.id):
        raise ValueError('恢复来源记录不兼容；不会派发。')
    from model import valid_uuid
    if (type(row['source_run_id']) is not str or not valid_uuid(row['source_run_id'])
            or type(row['source_digest']) is not str or len(row['source_digest']) != 64
            or any(char not in '0123456789abcdef' for char in row['source_digest'])
            or type(row['cwd']) is not str or not row['cwd'].startswith('/')
            or type(row['source_seq']) is not int or not 0 <= row['source_seq'] <= 2**53 - 1
            or type(row['source_report_id']) is not str or (row['source_report_id'] and not valid_uuid(row['source_report_id']))
            or type(row['source_report_seq']) is not int or not 0 <= row['source_report_seq'] <= 2**53 - 1):
        raise ValueError('恢复来源身份、摘要或修订类型无效；不会派发。')
    source = board.runs.get(row['source_run_id'])
    if (not source or source.task_id != task.id or not source.terminal
            or board.projects.get(task.project) != source.cwd
            or (task.title, task.description) != (source.title, source.description)
            or type(row['source_seq']) is not int or source.seq != row['source_seq']
            or row['cwd'] != source.cwd or row['source_digest'] != run_digest(source)):
        raise ValueError('恢复来源、原始错误或任务范围已变化；不会派发。')
    report = last_progress_report(source)
    if (row['source_report_id'], row['source_report_seq']) != (report.id if report else '', report.seq if report else 0):
        raise ValueError('恢复来源报告不符；不会派发。')
    for name in ('checkpoint', 'remaining'):
        if type(row[name]) is not str or not row[name].strip() or len(row[name]) > 1800:
            raise ValueError('恢复检查点或未完成工作缺失/超限。')


def recovery_contract_valid(text: str) -> bool:
    try:
        parts = split_recovery(text)
        return parts is not None and not parts[2]
    except ValueError:
        return False
