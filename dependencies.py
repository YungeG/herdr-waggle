"""Operation-scoped human dependency declarations. No filesystem or runtime I/O."""
import json
import re
from copy import deepcopy
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4, uuid5

from evidence import digest, now, task_scope, validate_item, validate_verification
from journal import _unique_object
from task_context import (
    DEPENDENCY_MARKER,
    canonical,
    parse_compiled_contract,
    source_base,
)

if TYPE_CHECKING:
    from model import Board, Task

OPERATIONS = {'inventory', 'evidence', 'implementation'}
REGISTRIES = ('task_dependencies', 'dependency_checks', 'ownership_releases', 'ownership_receipts')
MAX_DEPENDENCIES = 8
MAX_AUDITS = 8


def _time(value: object) -> None:
    if (type(value) is not str or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z', value)
            or datetime.fromisoformat(value).isoformat(timespec='milliseconds').replace('+00:00', 'Z') != value):
        raise ValueError('依赖时间必须是明确 UTC 毫秒时间。')


def _files(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or type(value) not in (list, tuple) or len(value) > 16:
        raise ValueError('文件范围应为至多 16 个明确相对定位，不是目录或通配规则。')
    for path in value:
        if (type(path) is not str or not 0 < len(path) <= 240 or path != path.strip()
                or path.startswith('/') or any(c in path for c in '\\:*?[]\x00\r\n')
                or any(part in {'', '.', '..'} for part in path.split('/'))):
            raise ValueError('文件定位必须明确相对项目目录；不允许上跳、绝对路径或通配符。')
        path.encode('utf-8')
    if len(set(value)) != len(value):
        raise ValueError('文件范围不能重复。')
    return tuple(value)


def _record(cls: type, row: object) -> dict:
    if type(row) is not dict or set(row) != {field.name for field in fields(cls)}:
        raise ValueError('依赖记录字段不兼容；不会补造来源。')
    result = dict(row)
    limits = {'condition': 500, 'verifier': 120, 'method': 500, 'limits': 300,
              'gaps': 300, 'next_owner': 200, 'kind': 30, 'proof': 30, 'result': 30}
    for name, value in row.items():
        if name == 'version':
            if type(value) is not int or value not in ((1, 2) if cls.__name__ == 'TaskDependency' else (1,)):
                raise ValueError('依赖记录版本无效。')
        elif name.endswith('_task_id'):
            if type(value) is not int or not 0 < value <= 2**53 - 1:
                raise ValueError('依赖任务身份应为正整数，不接受布尔值。')
        elif name == 'id' or name.endswith('_id'):
            if name == 'evidence_item_id' and value == '':
                continue
            if type(value) is not str or str(UUID(value)) != value:
                raise ValueError('依赖 UUID 无效。')
        elif name.endswith('_digest'):
            if type(value) is not str or not re.fullmatch(r'[0-9a-f]{64}', value):
                raise ValueError('依赖摘要无效。')
        elif name.endswith('_at'):
            _time(value)
        elif name == 'files':
            result[name] = _files(value)
        elif name == 'operations':
            if (type(value) not in (list, tuple) or not 1 <= len(value) <= 3
                    or any(type(op) is not str or op not in OPERATIONS for op in value)
                    or len(set(value)) != len(value)):
                raise ValueError('必须明确选择阻塞操作；不推断全文或锁住整项任务。')
            result[name] = tuple(value)
        elif type(value) is not str or not value.strip() or len(value) > limits[name]:
            raise ValueError('依赖说明字段为空、无效或超限；不会截断。')
        else:
            value.encode('utf-8')
    return result


@dataclass(frozen=True)
class TaskDependency:
    version: int
    id: str
    owner_task_id: int
    owner_scope_digest: str
    owner_contract_digest: str
    source_task_id: int
    source_scope_digest: str
    kind: str
    operations: tuple[str, ...]
    files: tuple[str, ...]
    proof: str
    condition: str
    created_at: str

    @classmethod
    def from_record(cls, row: object) -> 'TaskDependency':
        record = cls(**_record(cls, row))
        allowed = {'contract': {'human_condition'}, 'evidence': {'accepted_report', 'human_original_verified'},
                   'write-conflict': {'release_receipt'}}
        if record.kind not in allowed or record.proof not in allowed[record.kind]:
            raise ValueError('依赖类型与证明等级不匹配。')
        if record.owner_task_id == record.source_task_id:
            raise ValueError('任务不能依赖自己。')
        if record.kind == 'write-conflict':
            if record.operations != ('implementation',) or not record.files:
                raise ValueError('写冲突只阻塞明确实施操作，必须选择具体文件范围。')
        elif record.files:
            raise ValueError('契约/证据依赖不能伪装成文件写入权。')
        return record


@dataclass(frozen=True)
class DependencyCheck:
    version: int
    id: str
    dependency_id: str
    dependency_digest: str
    source_digest: str
    evidence_item_id: str
    result: str
    verifier: str
    method: str
    limits: str
    gaps: str
    next_owner: str
    checked_at: str
    recorded_at: str

    @classmethod
    def from_record(cls, row: object) -> 'DependencyCheck':
        record = cls(**_record(cls, row))
        if record.result not in {'satisfied', 'blocked', 'invalidated'} or record.checked_at > record.recorded_at:
            raise ValueError('依赖核对结果或实际核对时间无效。')
        return record


@dataclass(frozen=True)
class OwnershipRelease:
    version: int
    id: str
    dependency_id: str
    dependency_digest: str
    source_run_id: str
    source_agent_id: str
    source_session_id: str
    source_digest: str
    receiver_agent_id: str
    files: tuple[str, ...]
    verifier: str
    method: str
    limits: str
    gaps: str
    next_owner: str
    checked_at: str
    expires_at: str
    recorded_at: str

    @classmethod
    def from_record(cls, row: object) -> 'OwnershipRelease':
        record = cls(**_record(cls, row))
        if not record.files or not record.checked_at <= record.recorded_at < record.expires_at:
            raise ValueError('释放范围或到期时间无效；不能补造过去/未来的核对。')
        return record


@dataclass(frozen=True)
class OwnershipReceipt:
    version: int
    id: str
    release_id: str
    release_digest: str
    receiver_agent_id: str
    files: tuple[str, ...]
    verifier: str
    method: str
    checked_at: str
    recorded_at: str

    @classmethod
    def from_record(cls, row: object) -> 'OwnershipReceipt':
        record = cls(**_record(cls, row))
        if not record.files or record.checked_at > record.recorded_at:
            raise ValueError('接收记录范围或实际核对时间无效。')
        return record


def operation(requirements: str) -> str:
    from decisions import EVIDENCE_ONLY_PREFIX
    from failure_view import split_recovery
    base = source_base(requirements)
    recovery = split_recovery(base)
    contract = parse_compiled_contract(recovery[0] if recovery else base)
    if contract:
        return contract.purpose
    from task_context import has_valid_contract
    return 'evidence' if base.startswith(EVIDENCE_ONLY_PREFIX) and has_valid_contract(base) else 'implementation'


def request_for(board: 'Board', task: 'Task') -> str:
    run = board.runs.get(task.run_id)
    return run.requirements if task.run != 'none' and run else task.remaining


def latest_dependencies(board: 'Board', task_id: int) -> list[TaskDependency]:
    latest = {}
    for record in board.task_dependencies.values():
        if record.owner_task_id == task_id:
            latest[(record.source_task_id, record.kind)] = record
    return list(latest.values())


def _fresh(board: 'Board', dep: TaskDependency, requirements: str | None = None) -> None:
    TaskDependency.from_record(asdict(dep))  # Strict types before lookup.
    if dep.version != 2:
        raise ValueError('旧版本依赖缺不可逆失效审计；范围/释放是否曾变化未知，必须重新明确声明。')
    if any(r.dependency_id == dep.id and r.result == 'invalidated' for r in board.dependency_checks.values()):
        raise ValueError('旧绑定曾变化，核对/释放/接收已永久失效；恢复原文不恢复许可，请重新声明。')
    owner, source = board.tasks.get(dep.owner_task_id), board.tasks.get(dep.source_task_id)
    if (not owner or not source or task_scope(board, owner) != dep.owner_scope_digest
            or task_scope(board, source) != dep.source_scope_digest
            or digest(source_base(request_for(board, owner) if requirements is None else requirements)) != dep.owner_contract_digest):
        raise ValueError('目标、目录或合同范围已变化；依赖未知，需重新明确同类型来源与文件范围。')
    if dep.kind == 'write-conflict' and board.projects[owner.project] != board.projects[source.project]:
        raise ValueError('写冲突必须绑定同一精确目录；不推测跨目录文件归属。')


def source_digest(board: 'Board', dep: TaskDependency, *, writer: bool = False) -> str:
    _fresh(board, dep)
    source = board.tasks[dep.source_task_id]
    run = board.runs.get(source.run_id)
    if writer and (not run or not run.terminal or run.task_id != source.id
                   or (source.title, source.description, board.projects[source.project]) != (run.title, run.description, run.cwd)):
        raise ValueError('原写者执行身份或结束事实未能核对；验收/离线/空闲不是释放。')
    return digest({'scope': dep.source_scope_digest, 'task_events': source.events, 'status': source.status,
                   'run_state': source.run, 'run_id': source.run_id, 'run': asdict(run) if run else None,
                   'contract': source_base(request_for(board, source))})


def dependency_record(board: 'Board', owner: 'Task', source: 'Task', *, kind: str, operations: list[str],
                      files: list[str], condition: str, proof: str | None = None) -> TaskDependency:
    default = {'contract': 'human_condition', 'evidence': 'human_original_verified', 'write-conflict': 'release_receipt'}
    dep = TaskDependency(2, str(uuid4()), owner.id, task_scope(board, owner), digest(source_base(request_for(board, owner))),
                         source.id, task_scope(board, source), kind, tuple(operations), tuple(files), proof or default.get(kind, ''), condition, now())
    dep = TaskDependency.from_record(asdict(dep))
    _fresh(board, dep)
    return dep


def _selected(board: 'Board', dep_id: str) -> TaskDependency:
    # Callers must not accept bool/integer aliases as registry keys.
    if type(dep_id) is not str or str(UUID(dep_id)) != dep_id:
        raise ValueError('必须明确选择有效 UUID 的已保存依赖。')
    dep = board.task_dependencies.get(dep_id)
    if dep is None or dep not in latest_dependencies(board, dep.owner_task_id):
        raise ValueError('依赖未保存或已被新声明替代；旧核对不能恢复写入权。')
    _fresh(board, dep)
    return dep


def _evidence_digest(board: 'Board', dep: TaskDependency, item_id: str) -> str:
    if type(item_id) is not str or str(UUID(item_id)) != item_id:
        raise ValueError('必须明确选择有效 UUID 的已保存库存项。')
    item = board.evidence_items.get(item_id)
    if not item or (item.owner_task_id, item.source_task_id) != (dep.owner_task_id, dep.source_task_id):
        raise ValueError('证据依赖必须明确选择本任务已保存且匹配来源的库存项。')
    validate_item(board, item)
    if dep.proof == 'accepted_report' and item.source_kind != 'accepted_report':
        raise ValueError('选定项不是明确已验收报告；不从事实答案猜测报告验收。')
    history = [r for r in board.evidence_verifications.values() if r.item_id == item_id]
    latest = history[-1] if history else None
    if dep.proof == 'human_original_verified':
        if latest is None or latest.result != 'verified':
            raise ValueError('所需原件仍未人工核实或冲突；平台验收不等于原件核验。')
        validate_verification(board, latest)
    return digest([asdict(item), asdict(latest) if latest else None])


def check_record(board: 'Board', dep_id: str, *, evidence_item_id: str = '', **values: str) -> DependencyCheck:
    dep = _selected(board, dep_id)
    record = DependencyCheck(1, str(uuid4()), dep.id, digest(asdict(dep)),
                             digest([source_digest(board, dep), _evidence_digest(board, dep, evidence_item_id)])
                             if dep.kind == 'evidence' and values.get('result') == 'satisfied' else source_digest(board, dep),
                             evidence_item_id, recorded_at=now(), **values)
    validate_check(board, record)
    return record


def validate_check(board: 'Board', record: DependencyCheck) -> None:
    DependencyCheck.from_record(asdict(record))
    if record.result == 'invalidated':
        raise ValueError('不可逆绑定失效只由持久化门记录，不是人工满足条件或释放。')
    dep = _selected(board, record.dependency_id)
    if dep.kind == 'write-conflict' or record.dependency_digest != digest(asdict(dep)):
        raise ValueError('写冲突必须独立释放并接收；普通核对不是写入权。')
    expected = source_digest(board, dep)
    if dep.kind == 'evidence' and record.result == 'satisfied':
        expected = digest([expected, _evidence_digest(board, dep, record.evidence_item_id)])
    elif record.evidence_item_id:
        raise ValueError('此核对不能移用无关库存证明。')
    if record.source_digest != expected or record.recorded_at > now():
        raise ValueError('核对来源已变化或时间无效；输入保留，未保存。')


def release_record(board: 'Board', dep_id: str, receiver_agent_id: str, files: list[str], **values: str) -> OwnershipRelease:
    dep = _selected(board, dep_id)
    anchor = source_digest(board, dep, writer=True)
    run = board.runs[board.tasks[dep.source_task_id].run_id]
    record = OwnershipRelease(1, str(uuid4()), dep.id, digest(asdict(dep)), run.id, run.agent_id, run.session_id,
                              anchor, receiver_agent_id, tuple(files), recorded_at=now(), **values)
    validate_release(board, record)
    return record


def validate_release(board: 'Board', record: OwnershipRelease) -> None:
    OwnershipRelease.from_record(asdict(record))
    dep = _selected(board, record.dependency_id)
    anchor = source_digest(board, dep, writer=True)
    run = board.runs[board.tasks[dep.source_task_id].run_id]
    if (dep.kind != 'write-conflict' or record.dependency_digest != digest(asdict(dep)) or record.files != dep.files
            or record.source_digest != anchor or (record.source_run_id, record.source_agent_id, record.source_session_id) != (run.id, run.agent_id, run.session_id)
            or record.recorded_at > now() or record.expires_at <= now()):
        raise ValueError('释放必须绑定精确原写者/来源/文件/接续者和有效期限；不能猜测交接。')


def receipt_record(board: 'Board', release_id: str, receiver_agent_id: str, files: list[str], **values: str) -> OwnershipReceipt:
    if type(release_id) is not str or str(UUID(release_id)) != release_id:
        raise ValueError('必须选择有效 UUID 的已保存释放记录。')
    release = board.ownership_releases.get(release_id)
    if release is None:
        raise ValueError('必须选择已经单独保存的释放记录。')
    record = OwnershipReceipt(1, str(uuid4()), release.id, digest(asdict(release)), receiver_agent_id,
                              tuple(files), recorded_at=now(), **values)
    validate_receipt(board, record)
    return record


def validate_receipt(board: 'Board', record: OwnershipReceipt) -> None:
    OwnershipReceipt.from_record(asdict(record))
    release = board.ownership_releases.get(record.release_id)
    if release is None:
        raise ValueError('释放未持久化；不显示已交接。')
    validate_release(board, release)
    history = [r for r in board.ownership_releases.values() if r.dependency_id == release.dependency_id]
    if (not history or history[-1] != release or record.release_digest != digest(asdict(release))
            or record.receiver_agent_id != release.receiver_agent_id or record.files != release.files
            or not release.recorded_at <= record.checked_at <= record.recorded_at < release.expires_at
            or record.recorded_at > now()):
        raise ValueError('接收必须独立核对最新释放的接续者、相同文件范围和期限。')


def check_dependencies(board: 'Board', task: 'Task', purpose: str, *, agent_id: str | None = None,
                       requirements: str | None = None) -> None:
    if type(purpose) is not str or purpose not in OPERATIONS or board.tasks.get(task.id) is not task:
        raise ValueError('依赖操作或任务身份无效。')
    for dep in latest_dependencies(board, task.id):
        if purpose not in dep.operations:
            continue
        try:
            _fresh(board, dep, requirements)
            if dep.kind == 'write-conflict':
                releases = [r for r in board.ownership_releases.values() if r.dependency_id == dep.id]
                if not releases:
                    raise ValueError('缺明确原写者释放；已验收、离线或空闲都不能接管。')
                release = releases[-1]
                validate_release(board, release)
                receipts = [r for r in board.ownership_receipts.values() if r.release_id == release.id]
                if not receipts:
                    raise ValueError('释放已记录，但缺接续者单独持久化的接收事实；尚未交接。')
                validate_receipt(board, receipts[-1])
                if agent_id is not None and agent_id != release.receiver_agent_id:
                    raise ValueError('分配/派发对象不是明确接收的接续者。')
            else:
                checks = [r for r in board.dependency_checks.values() if r.dependency_id == dep.id]
                if not checks or checks[-1].result != 'satisfied':
                    raise ValueError('缺明确条件核对，或最新核对仍阻塞；任务完成不自动满足条件。')
                validate_check(board, checks[-1])
        except ValueError as error:
            raise ValueError(f'D{dep.source_task_id:03} · {dep.kind} · {dep.condition}：{error}') from error


def check_dependencies_structure(board: 'Board') -> None:
    counts = {}
    edges: dict[int, set[int]] = {}
    for key, dep in board.task_dependencies.items():
        TaskDependency.from_record(asdict(dep))
        if key != dep.id or dep.owner_task_id not in board.tasks or dep.source_task_id not in board.tasks:
            raise ValueError('依赖来源或所属任务不存在。')
        counts[dep.owner_task_id] = counts.get(dep.owner_task_id, 0) + 1
        if counts[dep.owner_task_id] > MAX_DEPENDENCIES:
            raise ValueError('每任务最多 8 份依赖声明；不覆写历史。')
        edges.setdefault(dep.owner_task_id, set()).add(dep.source_task_id)
    nodes = set(edges) | {source for sources in edges.values() for source in sources}
    degrees = {node: len(edges.get(node, set())) for node in nodes}
    receivers: dict[int, set[int]] = {}
    for owner, sources in edges.items():
        for source in sources:
            receivers.setdefault(source, set()).add(owner)
    ready = [node for node, degree in degrees.items() if degree == 0]
    visited = 0
    while ready:
        source = ready.pop()
        visited += 1
        for owner in receivers.get(source, set()):
            degrees[owner] -= 1
            if degrees[owner] == 0:
                ready.append(owner)
    if visited != len(nodes):
        raise ValueError('循环依赖被拒绝；未保存任何交接或执行意图。')
    for name, cls in (('dependency_checks', DependencyCheck), ('ownership_releases', OwnershipRelease), ('ownership_receipts', OwnershipReceipt)):
        count, times = {}, {}
        for key, record in getattr(board, name).items():
            cls.from_record(asdict(record))
            if key != record.id:
                raise ValueError('交接审计 ID 不符。')
            if name == 'ownership_receipts':
                release = board.ownership_releases.get(record.release_id)
                if (not release or record.release_digest != digest(asdict(release)) or record.receiver_agent_id != release.receiver_agent_id
                        or record.files != release.files or record.checked_at < release.recorded_at or record.recorded_at >= release.expires_at):
                    raise ValueError('接收审计与释放来源、文件或时间不符。')
                parent = release.dependency_id
            else:
                dep = board.task_dependencies.get(record.dependency_id)
                if not dep or record.dependency_digest != digest(asdict(dep)) or record.checked_at < dep.created_at:
                    raise ValueError('交接/核对审计与原依赖声明不符。')
                parent = dep.id
                if name == 'ownership_releases':
                    run = board.runs.get(record.source_run_id)
                    if (dep.kind != 'write-conflict' or record.files != dep.files or not run
                            or (run.task_id, run.agent_id, run.session_id) != (dep.source_task_id, record.source_agent_id, record.source_session_id)):
                        raise ValueError('释放审计原写者/执行/文件不符。')
                elif (dep.kind == 'write-conflict' and record.result != 'invalidated') or (record.evidence_item_id and record.evidence_item_id not in board.evidence_items):
                    raise ValueError('条件核对类型或库存来源不符。')
            is_invalidation = name == 'dependency_checks' and record.result == 'invalidated'
            if is_invalidation:
                if record != _invalidation_record(dep, record.source_digest, record.recorded_at):
                    raise ValueError('平台失效记录不能伪造人工确认或改写绑定来源。')
                counter = parent + ':invalidation'
            else:
                counter = parent
            count[counter] = count.get(counter, 0) + 1
            if count[counter] > (1 if is_invalidation else MAX_AUDITS) or record.recorded_at < times.get(parent, ''):
                raise ValueError('交接审计超限或保存顺序无效。')
            times[parent] = record.recorded_at


def check_dependencies_after(previous: 'Board', board: 'Board') -> None:
    additions = []
    for name in REGISTRIES:
        old, current = getattr(previous, name), getattr(board, name)
        def owner_id(record) -> int:
            if name == 'task_dependencies':
                return record.owner_task_id
            dep_id = previous.ownership_releases[record.release_id].dependency_id if name == 'ownership_receipts' else record.dependency_id
            return previous.task_dependencies[dep_id].owner_task_id
        retained = [key for key, record in old.items() if owner_id(record) in board.tasks]
        if list(current)[:len(retained)] != retained or any(current.get(key) != old[key] for key in retained):
            raise ValueError('保留任务的依赖/释放/接收审计不可删除、重排或改写。')
        additions.extend((name, current[key]) for key in current.keys() - old.keys())
    manual = [(name, record) for name, record in additions if not (name == 'dependency_checks' and record.result == 'invalidated')]
    if len(manual) > 1:
        raise ValueError('一次仅保存一份依赖记录；释放和接收必须分开持久化。')
    expected_invalidations = _changed_bindings(previous, board)
    for name, record in additions:
        if name == 'dependency_checks' and record.result == 'invalidated':
            if (record.dependency_id not in expected_invalidations
                    or record != _invalidation_record(previous.task_dependencies[record.dependency_id],
                                                       expected_invalidations[record.dependency_id], record.recorded_at)
                    or record.recorded_at > now()):
                raise ValueError('绑定失效必须对应已持久化来源的实际变化；不接受伪造平台记录。')
            continue
        if name == 'task_dependencies':
            _fresh(previous, record)
            _fresh(board, record)
            owner = record.owner_task_id
            timestamp = record.created_at
        else:
            {'dependency_checks': validate_check, 'ownership_releases': validate_release,
             'ownership_receipts': validate_receipt}[name](previous, record)
            dep_id = previous.ownership_releases[record.release_id].dependency_id if name == 'ownership_receipts' else record.dependency_id
            owner = previous.task_dependencies[dep_id].owner_task_id
            timestamp = record.recorded_at
        if (previous.tasks[owner].retention != 'active' or board.tasks[owner].retention != 'active' or timestamp > now()
                or any(getattr(previous.tasks[owner], field) != getattr(board.tasks[owner], field) for field in ('agent', 'status', 'run', 'run_id'))
                or previous.runs != board.runs or previous.supplements != board.supplements or previous.decisions != board.decisions):
            raise ValueError('交接只保存明确记录；不能同时指派、启动或变更执行意图。')
    for task_id, task in board.tasks.items():
        old = previous.tasks.get(task_id)
        if task.execution == 'pi' and task.agent and old and (old.agent != task.agent or (old.status != 'ready' and task.status == 'ready')):
            purpose = _verified_purpose(previous, board, request_for(board, task), run_id=task.run_id)
            check_dependencies(previous, old, purpose, agent_id=task.agent, requirements=request_for(board, task))
            check_dependencies(board, task, purpose, agent_id=task.agent)
    for note_id in board.supplements.keys() - previous.supplements.keys():
        note = board.supplements[note_id]
        run = board.runs[note.run_id]
        task = board.tasks[run.task_id]
        purpose = _verified_purpose(previous, board, note.text, supplement_id=note.id)
        if task.id in previous.tasks:
            check_dependencies(previous, previous.tasks[task.id], purpose, agent_id=run.agent_id, requirements=note.text)
        check_dependencies(board, task, purpose, agent_id=run.agent_id, requirements=note.text)
    for run_id in board.runs.keys() - previous.runs.keys():
        run = board.runs[run_id]
        purpose = _verified_purpose(previous, board, run.requirements, run_id=run.id)
        if run.task_id in previous.tasks:
            check_dependencies(previous, previous.tasks[run.task_id], purpose, agent_id=run.agent_id, requirements=run.requirements)
        check_dependencies(board, board.tasks[run.task_id], purpose, agent_id=run.agent_id, requirements=run.requirements)


def _binding_stamp(board: 'Board', dep: TaskDependency, evidence_ids: set[str]) -> str:
    owner, source = board.tasks[dep.owner_task_id], board.tasks[dep.source_task_id]
    run = board.runs.get(source.run_id)
    return digest({'owner_scope': task_scope(board, owner), 'owner_contract': source_base(request_for(board, owner)),
                   'source_scope': task_scope(board, source), 'source_status': source.status, 'source_run': source.run,
                   'source_events': source.events, 'source_run_data': asdict(run) if run else None,
                   'source_contract': source_base(request_for(board, source)),
                   'evidence': [[key, asdict(board.evidence_items[key]) if key in board.evidence_items else None,
                                 [asdict(r) for r in board.evidence_verifications.values() if r.item_id == key]]
                                for key in sorted(evidence_ids)]})


def _changed_bindings(previous: 'Board', board: 'Board') -> dict[str, str]:
    changes = {}
    for dep in previous.task_dependencies.values():
        if (dep.version != 2 or dep.owner_task_id not in board.tasks or dep.id not in board.task_dependencies
                or any(r.dependency_id == dep.id and r.result == 'invalidated' for r in previous.dependency_checks.values())):
            continue
        checks = [r for r in previous.dependency_checks.values() if r.dependency_id == dep.id]
        if not any(r.result == 'satisfied' for r in checks) and not any(r.dependency_id == dep.id for r in previous.ownership_releases.values()):
            continue  # No positive proof to resurrect; first explicit proof still requires fresh binding.
        evidence_ids = {r.evidence_item_id for r in checks if r.evidence_item_id}
        before, current = _binding_stamp(previous, dep, evidence_ids), _binding_stamp(board, dep, evidence_ids)
        if before != current:
            changes[dep.id] = current
    return changes


def _invalidation_record(dep: TaskDependency, stamp: str, timestamp: str) -> DependencyCheck:
    return DependencyCheck(1, str(uuid5(UUID(dep.id), 'waggle-binding-invalidation')), dep.id, digest(asdict(dep)), stamp, '',
                           'invalidated', '平台绑定保护（不是人工确认）',
                           '已持久化目标/合同/来源/证据发生变化；旧证明不可恢复，须重新声明',
                           '只核对平台绑定字节，不探测文件或人工身份', '原件/派生进程状态仍需明确核对',
                           '明确新声明及释放/接收来源', timestamp, timestamp)


def stage_binding_invalidations(previous: 'Board', board: 'Board') -> 'Board':
    changes = _changed_bindings(previous, board)
    if not changes:
        return board
    staged = deepcopy(board)
    timestamp = now()
    owners = set()
    for dep_id, stamp in changes.items():
        dep = previous.task_dependencies[dep_id]
        record = _invalidation_record(dep, stamp, timestamp)
        staged.dependency_checks[record.id] = record
        owners.add(dep.owner_task_id)
    for owner_id in owners:
        task = staged.tasks[owner_id]
        base = source_base(task.remaining)
        if base != task.remaining:
            task.remaining = base + staged._source_exports(task)
            if len(task.remaining) > 6000:
                raise ValueError('完整失效审计与请求超限；范围变化未保存，不截断历史。')
    check_dependencies_structure(staged)
    return staged

def _verified_purpose(previous: 'Board', board: 'Board', text: str, *, run_id: str = '', supplement_id: str = '') -> str:
    from decisions import request_only_decisions, validate_preview
    for key in board.decisions.keys() - previous.decisions.keys():
        record = board.decisions[key]
        if (record.preview.compiled_text == text and
                ((run_id and record.intent.kind == 'new_run' and record.intent.run_id == run_id)
                 or (supplement_id and record.intent.kind == 'live_supplement' and record.intent.supplement_id == supplement_id))):
            validate_preview(previous, record.preview)
            if request_only_decisions(record.preview.specs, record.preview.answers):
                return 'evidence'
    return operation(text)

def _export_data(board: 'Board', task_id: int) -> dict:
    deps = [asdict(r) for r in board.task_dependencies.values() if r.owner_task_id == task_id]
    dep_ids = {r['id'] for r in deps}
    releases = [asdict(r) for r in board.ownership_releases.values() if r.dependency_id in dep_ids]
    release_ids = {r['id'] for r in releases}
    return {'version': 1, 'dependencies': deps,
            'checks': [asdict(r) for r in board.dependency_checks.values() if r.dependency_id in dep_ids],
            'releases': releases, 'receipts': [asdict(r) for r in board.ownership_receipts.values() if r.release_id in release_ids]}


def dependency_export(board: 'Board', task: 'Task') -> str:
    data = _export_data(board, task.id)
    return DEPENDENCY_MARKER + canonical(data) if data['dependencies'] else ''


def historical_quote_valid(board: 'Board', task: 'Task', quote: str) -> bool:
    if not quote:
        return True
    if not quote.startswith(DEPENDENCY_MARKER):
        return False
    text = quote[len(DEPENDENCY_MARKER):]
    try:
        old = json.loads(text, object_pairs_hook=_unique_object)
        current = _export_data(board, task.id)
        # Canonical JSON normalizes tuple/list without relaxing arbitrary historical fields.
        current = json.loads(canonical(current))
        if type(old) is not dict or set(old) != set(current) or type(old['version']) is not int or old['version'] != 1 or not old['dependencies']:
            return False
        for name in ('dependencies', 'checks', 'releases', 'receipts'):
            if type(old[name]) is not list or old[name] != current[name][:len(old[name])]:
                return False
        deps, releases = {r['id'] for r in old['dependencies']}, {r['id'] for r in old['releases']}
        return (all(r['dependency_id'] in deps for r in old['checks'] + old['releases'])
                and all(r['release_id'] in releases for r in old['receipts']) and canonical(old) == text)
    except (ValueError, TypeError, KeyError):
        return False


def dependency_text(board: 'Board', task: 'Task', *, durable: bool) -> str:
    rows = ['局部依赖 · 仅展示、核对与派发前规则；不是文件系统锁或自动调度。',
            '人工陈述身份未认证，平台未探测文件/符号链接；Pi 空闲、离线、旧任务已验收都不是释放。',
            '本地已持久化记录；释放还需独立接收及派发前旧 Pi 空闲归属核对。' if durable else '仅本会话未持久化；尚未交接，不能真实指派/派发。']
    for dep in latest_dependencies(board, task.id):
        try:
            if not durable:
                raise ValueError('尚未持久化，不能显示已交接。')
            for purpose in dep.operations:
                check_dependencies(board, task, purpose)
            state = '明确条件记录可用（仍须运行时门）'
        except ValueError as error:
            state = '待核对 / 不可接管：' + str(error)
        rows.append(f'\nD{dep.source_task_id:03} → D{task.id:03} · {dep.kind}\n依赖 {dep.id}\n只阻塞：{", ".join(dep.operations)}\n文件：{", ".join(dep.files) or "不涉及写入权"}\n条件：{dep.condition}\n证明等级：{dep.proof}\n{state}')
        checks = [r for r in board.dependency_checks.values() if r.dependency_id == dep.id]
        releases = [r for r in board.ownership_releases.values() if r.dependency_id == dep.id]
        for record in checks + releases:
            rows.append(canonical(asdict(record)))
        if releases:
            for receipt in board.ownership_receipts.values():
                if receipt.release_id == releases[-1].id:
                    rows.append('接收事实（独立人工陈述）：' + canonical(asdict(receipt)))
    if not latest_dependencies(board, task.id):
        rows.append('尚无明确依赖；不代表没有共享写者，平台不会扫描或猜测。')
    return '\n'.join(rows)
