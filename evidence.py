"""Explicit selected historical evidence and append-only human attestations. No I/O."""
import hashlib
import json
import re
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from decisions import BusinessAnswer, business_input, decision_record_wire
from journal import parse_event, validate_events
from task_context import EVIDENCE_MARKER, MAX_SNAPSHOT_LENGTH, canonical, source_payload

if TYPE_CHECKING:
    from model import Board, Task

MAX_ITEMS = 16
MAX_VERIFICATIONS = 16


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def now() -> str:
    return datetime.now(UTC).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def _text(value: object, limit: int, *, empty: bool = False) -> str:
    if type(value) is not str or len(value) > limit or (not empty and not value.strip()):
        raise ValueError('库存字段为空、类型无效或超过明示上限；不会截断。')
    value.encode('utf-8')
    return value


def _id(value: object, *, empty: bool = False) -> None:
    if empty and value == '':
        return
    if type(value) is not str or str(UUID(value)) != value:
        raise ValueError('库存来源 UUID 无效。')


def _positive(value: object) -> None:
    if type(value) is not int or not 0 < value <= 2**53 - 1:
        raise ValueError('库存任务身份或修订必须是正整数。')


def _digest(value: object) -> None:
    if type(value) is not str or not re.fullmatch(r'[0-9a-f]{64}', value):
        raise ValueError('库存来源摘要无效。')


def _time(value: object) -> None:
    if (type(value) is not str or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z', value)
            or datetime.fromisoformat(value).isoformat(timespec='milliseconds').replace('+00:00', 'Z') != value):
        raise ValueError('人工核验时间需明确 UTC 毫秒格式；不会猜测时间。')


def task_scope(board: 'Board', task: 'Task') -> str:
    if (type(task.id) is not int or board.tasks.get(task.id) is not task or task.execution != 'pi'
            or not board.projects.get(task.project, '').startswith('/')):
        raise ValueError('库存只绑定存在的真实任务与精确项目目录。')
    return digest([task.id, task.title, task.description, task.project, task.execution, board.projects[task.project]])


@dataclass(frozen=True)
class EvidenceItem:
    version: int
    id: str
    owner_task_id: int
    owner_scope_digest: str
    source_task_id: int
    source_kind: str
    source_run_id: str
    source_report_event_id: str
    source_report_seq: int
    acceptance_event_id: str
    decision_record_id: str
    fact_key: str
    selected_refs: tuple[str, ...]
    cwd: str
    source_digest: str
    source_snapshot: str
    name: str
    locator: str
    limits: str
    gaps: str
    next_owner: str
    created_at: str

    @classmethod
    def from_record(cls, row: object) -> 'EvidenceItem':
        if type(row) is not dict or set(row) != {f.name for f in fields(cls)}:
            raise ValueError('库存记录字段不兼容。')
        if type(row['version']) is not int or row['version'] != 1:
            raise ValueError('库存记录版本无效。')
        for name in ('owner_task_id', 'source_task_id', 'source_report_seq'):
            _positive(row[name])
        for name in ('id', 'source_run_id', 'source_report_event_id'):
            _id(row[name])
        for name in ('acceptance_event_id', 'decision_record_id'):
            _id(row[name], empty=True)
        if type(row['source_kind']) is not str or row['source_kind'] not in {'accepted_report', 'business_fact'}:
            raise ValueError('请选择规范已验收报告或有人工确认来源的业务事实。')
        refs = row['selected_refs']
        if type(refs) not in (list, tuple) or any(type(v) is not str for v in refs) or len(set(refs)) != len(refs):
            raise ValueError('库存引用需明确、不重复的数据字段。')
        _text(row['fact_key'], 80, empty=True)
        if row['source_kind'] == 'accepted_report':
            if (not row['acceptance_event_id'] or row['decision_record_id'] or row['fact_key']
                    or not refs or any(v not in {'summary', 'checks', 'description'} for v in refs)):
                raise ValueError('报告库存必须明确选定字段与人工验收锚点。')
        elif not row['decision_record_id'] or row['acceptance_event_id'] or not row['fact_key'] or refs:
            raise ValueError('事实库存必须绑定人工事实答案，不混入报告或技术批准。')
        for name in ('owner_scope_digest', 'source_digest'):
            _digest(row[name])
        _text(row['cwd'], 4096)
        if not row['cwd'].startswith('/'):
            raise ValueError('库存来源目录无效。')
        _text(row['source_snapshot'], MAX_SNAPSHOT_LENGTH)
        for name, limit in {'name': 200, 'locator': 1000, 'limits': 500, 'gaps': 500, 'next_owner': 300}.items():
            _text(row[name], limit)
        _time(row['created_at'])
        return cls(**{**row, 'selected_refs': tuple(refs)})


@dataclass(frozen=True)
class EvidenceVerification:
    version: int
    id: str
    item_id: str
    item_digest: str
    verifier: str
    method: str
    result: str
    limits: str
    gaps: str
    next_owner: str
    checked_at: str
    recorded_at: str

    @classmethod
    def from_record(cls, row: object) -> 'EvidenceVerification':
        if type(row) is not dict or set(row) != {f.name for f in fields(cls)}:
            raise ValueError('人工核验记录字段不兼容。')
        if type(row['version']) is not int or row['version'] != 1:
            raise ValueError('人工核验记录版本无效。')
        for name in ('id', 'item_id'):
            _id(row[name])
        _digest(row['item_digest'])
        if type(row['result']) is not str or row['result'] not in {'verified', 'unverified', 'conflict'}:
            raise ValueError('请明确人工核验结果：已核验、仍未核实或冲突。')
        for name, limit in {'verifier': 120, 'method': 500, 'limits': 500, 'gaps': 500, 'next_owner': 300}.items():
            _text(row[name], limit)
        for name in ('checked_at', 'recorded_at'):
            _time(row[name])
        if row['checked_at'] > row['recorded_at']:
            raise ValueError('不能以未来核验时间形成事实。')
        return cls(**row)


def fact_payload(board: 'Board', record_id: str, fact_key: str) -> dict:
    _id(record_id)
    _text(fact_key, 80)
    record = board.decisions.get(record_id)
    if not record or not record.confirmed_at or record.intent.kind == 'draft':
        raise ValueError('事实必须来自真实人工确认记录；草稿、备注和执行者声称不算。')
    # Validate the stored question and answer, not a caller-supplied imitation.
    record = type(record).from_record(decision_record_wire(record))
    parent = board.tasks.get(record.task_id)
    run = board.runs.get(record.preview.source.run_id)
    if not parent or not run or run.task_id != parent.id:
        raise ValueError('事实来源任务或轮次缺失。')
    task_scope(board, parent)
    context = record.preview.context
    if ((parent.id, parent.title, parent.description, parent.project, board.projects[parent.project], parent.execution)
            != (context.task_id, context.title, context.description, context.project, context.cwd, context.execution)
            or (run.agent_id, run.cwd, run.title, run.description, run.requirements)
            != (context.agent_id, context.cwd, context.title, context.description, context.requirements)):
        raise ValueError('事实问题的原任务、原范围或目录已变化；不能串用。')
    validate_events(run.events, run_id=run.id, seq=run.seq)
    event = next((e for raw in run.events if (e := parse_event(raw)) and e.id == record.preview.source.event_id), None)
    pair = next(((s, a) for s, a in zip(record.specs, record.preview.answers, strict=True) if s.key == fact_key), None)
    if (not event or event.source != 'executor' or not event.report or event.seq != record.preview.source.seq
            or not event.report.decisions or not pair or pair[0] not in event.report.decisions or not pair[1].business):
        raise ValueError('指定来源没有同一问题的人工业务事实答案；技术批准不可转为事实。')
    question = business_input(pair[0], parent.id, event.id, fact_key)
    answer = pair[1].business
    if question is None or answer.question != question:
        raise ValueError('事实的问题与原报告不同；不会从相似文字推断。')
    BusinessAnswer.from_record(asdict(answer)).check(question)
    snapshot = canonical({'业务问题': asdict(question), '人工事实答案': {'value': answer.value, 'detail': answer.detail},
                          '人工确认时间': record.confirmed_at, '原问题范围摘要': digest(context.scope),
                          '原任务及适用范围（历史，不自动适用新目标）': [parent.id, context.title, context.description, context.cwd]})
    if len(snapshot) > MAX_SNAPSHOT_LENGTH:
        raise ValueError('人工事实来源超过 4000 字；不能截断范围。')
    return {'decision_record': decision_record_wire(record), 'source_identity': list(run.identity),
            'source_report_raw': next(raw for raw in run.events if (e := parse_event(raw)) and e.id == event.id),
            'fact_key': fact_key, 'source_snapshot': snapshot, 'cwd': context.cwd}


def validate_item(board: 'Board', item: EvidenceItem) -> None:
    EvidenceItem.from_record(asdict(item))  # Strict types before any registry lookup.
    owner, parent = board.tasks.get(item.owner_task_id), board.tasks.get(item.source_task_id)
    if not owner or task_scope(board, owner) != item.owner_scope_digest or not parent:
        raise ValueError('库存所属任务、目标范围或来源已变化；请重新核对。')
    if item.source_kind == 'accepted_report':
        payload = source_payload(board, parent, item.source_report_event_id, item.acceptance_event_id, item.selected_refs)
        run_id, seq = parent.run_id, payload['source_report_seq']
    else:
        payload = fact_payload(board, item.decision_record_id, item.fact_key)
        record = board.decisions[item.decision_record_id]
        if record.task_id != parent.id:
            raise ValueError('人工事实不得跨来源任务串用。')
        run_id, seq = record.preview.source.run_id, record.preview.source.seq
        if record.preview.source.event_id != item.source_report_event_id:
            raise ValueError('人工事实报告身份不符。')
    if ((item.source_run_id, item.source_report_seq, item.cwd, item.source_snapshot, item.source_digest)
            != (run_id, seq, payload['cwd'], payload['source_snapshot'], digest(payload))):
        raise ValueError('库存原始报告、验收、问题、范围或来源摘要已变化；未保存、未派发。')


def report_reference(board: 'Board', owner: 'Task', parent: 'Task', report_id: str, acceptance_id: str,
                     selected_refs: list[str], *, name: str, locator: str, limits: str, gaps: str, next_owner: str) -> EvidenceItem:
    _id(report_id)
    _id(acceptance_id)
    payload = source_payload(board, parent, report_id, acceptance_id, selected_refs)
    item = EvidenceItem(1, str(uuid4()), owner.id, task_scope(board, owner), parent.id, 'accepted_report', parent.run_id,
                        report_id, payload['source_report_seq'], acceptance_id, '', '', tuple(selected_refs),
                        payload['cwd'], digest(payload), payload['source_snapshot'], name, locator, limits, gaps, next_owner, now())
    validate_item(board, item)
    return item


def fact_reference(board: 'Board', owner: 'Task', record_id: str, fact_key: str, *, name: str, locator: str,
                   limits: str, gaps: str, next_owner: str) -> EvidenceItem:
    payload = fact_payload(board, record_id, fact_key)
    record = board.decisions[record_id]
    item = EvidenceItem(1, str(uuid4()), owner.id, task_scope(board, owner), record.task_id, 'business_fact',
                        record.preview.source.run_id, record.preview.source.event_id, record.preview.source.seq,
                        '', record_id, fact_key, (), payload['cwd'], digest(payload), payload['source_snapshot'],
                        name, locator, limits, gaps, next_owner, now())
    validate_item(board, item)
    return item


def verification_record(board: 'Board', item_id: str, *, verifier: str, method: str, result: str, limits: str,
                        gaps: str, next_owner: str, checked_at: str) -> EvidenceVerification:
    _id(item_id)
    item = board.evidence_items.get(item_id)
    if not item:
        raise ValueError('只能核验已保存的明确库存项。')
    validate_item(board, item)
    record = EvidenceVerification(1, str(uuid4()), item.id, digest(asdict(item)), verifier, method, result,
                                  limits, gaps, next_owner, checked_at, now())
    EvidenceVerification.from_record(asdict(record))
    return record


def validate_verification(board: 'Board', record: EvidenceVerification) -> None:
    EvidenceVerification.from_record(asdict(record))
    item = board.evidence_items.get(record.item_id)
    if not item or record.item_digest != digest(asdict(item)) or record.recorded_at < item.created_at:
        raise ValueError('核验记录与精确库存项不符；不能移用其他核验。')
    validate_item(board, item)


def check_evidence(board: 'Board') -> None:
    counts: dict[int, int] = {}
    for key, item in board.evidence_items.items():
        validate_item(board, item)
        if key != item.id:
            raise ValueError('库存记录字典身份不一致。')
        counts[item.owner_task_id] = counts.get(item.owner_task_id, 0) + 1
        if counts[item.owner_task_id] > MAX_ITEMS:
            raise ValueError('每个任务最多 16 项显式引用。')
    verification_counts: dict[str, int] = {}
    previous: dict[str, str] = {}
    for key, record in board.evidence_verifications.items():
        validate_verification(board, record)
        if key != record.id or record.recorded_at < previous.get(record.item_id, ''):
            raise ValueError('人工核验审计身份或保存顺序无效。')
        verification_counts[record.item_id] = verification_counts.get(record.item_id, 0) + 1
        if verification_counts[record.item_id] > MAX_VERIFICATIONS:
            raise ValueError('每项最多 16 份人工核验记录；不会覆写历史。')
        previous[record.item_id] = record.recorded_at


def check_evidence_after(previous: 'Board', board: 'Board') -> None:
    """Store admission: no rewrite/reordering, no same-transaction fabricated source."""
    for key, item in previous.evidence_items.items():
        if item.owner_task_id in board.tasks and board.evidence_items.get(key) != item:
            raise ValueError('保留任务的库存引用不可删除或改写；变更需保留审计。')
    retained = [key for key, item in previous.evidence_items.items() if item.owner_task_id in board.tasks]
    if list(board.evidence_items)[:len(retained)] != retained:
        raise ValueError('库存来源保存顺序不可改写。')
    for key, record in previous.evidence_verifications.items():
        item = previous.evidence_items[record.item_id]
        if item.owner_task_id in board.tasks and board.evidence_verifications.get(key) != record:
            raise ValueError('保留库存的人工核验审计不可删除或改写。')
    retained = [key for key, record in previous.evidence_verifications.items()
                if previous.evidence_items[record.item_id].owner_task_id in board.tasks]
    if list(board.evidence_verifications)[:len(retained)] != retained:
        raise ValueError('人工核验审计保存顺序不可改写。')
    new_items = board.evidence_items.keys() - previous.evidence_items.keys()
    new_records = board.evidence_verifications.keys() - previous.evidence_verifications.keys()
    if len(new_items) > 1 or len(new_records) > 1:
        raise ValueError('一次人工确认最多新增一项引用或一份核验记录。')
    for key in new_items:
        item = board.evidence_items[key]
        validate_item(previous, item)
        old_owner = previous.tasks[item.owner_task_id]
        if old_owner.retention != 'active' or board.tasks[item.owner_task_id].retention != 'active' or item.created_at > now():
            raise ValueError('仅当前任务可显式选择来源，不能伪造未来库存。')
    for key in new_records:
        record = board.evidence_verifications[key]
        validate_verification(previous, record)
        owner_id = previous.evidence_items[record.item_id].owner_task_id
        if (previous.tasks[owner_id].retention != 'active' or board.tasks[owner_id].retention != 'active'
                or record.recorded_at > now()):
            raise ValueError('人工核验必须绑定已持久化库存与真实保存时间。')

def _conflicts(items: list[EvidenceItem]) -> list[list[str]]:
    seen: dict[str, list[tuple[str, str]]] = {}
    conflicts: list[list[str]] = []
    for item in items:
        if item.source_kind != 'business_fact':
            continue
        data = json.loads(item.source_snapshot)
        key = canonical([item.source_task_id, item.fact_key, data['业务问题'], data['原问题范围摘要']])
        value = canonical(data['人工事实答案'])
        for prior_id, prior in seen.get(key, []):
            if prior != value:
                conflicts.append([prior_id, item.id])
        seen.setdefault(key, []).append((item.id, value))
    return conflicts


def evidence_export(board: 'Board', task: 'Task', *, extra_item: EvidenceItem | None = None,
                    extra_verification: EvidenceVerification | None = None) -> str:
    items = [i for i in board.evidence_items.values() if i.owner_task_id == task.id]
    if extra_item is not None:
        validate_item(board, extra_item)
        if extra_item.owner_task_id != task.id or extra_item.id in board.evidence_items:
            raise ValueError('新库存项所属任务或身份不符。')
        items.append(extra_item)
    if not items:
        return ''
    if len(items) > MAX_ITEMS:
        raise ValueError('每个任务最多 16 项显式引用。')
    rows = []
    for item in items:
        validate_item(board, item)
        history = [v for v in board.evidence_verifications.values() if v.item_id == item.id]
        if extra_verification and extra_verification.item_id == item.id:
            validate_verification(board, extra_verification)
            if extra_verification.id in board.evidence_verifications:
                raise ValueError('人工核验身份不能重复。')
            if history and extra_verification.recorded_at < history[-1].recorded_at:
                raise ValueError('人工核验保存时间不能倒退。')
            history.append(extra_verification)
        if len(history) > MAX_VERIFICATIONS:
            raise ValueError('每项最多 16 份人工核验记录。')
        for record in history:
            validate_verification(board, record)
        row = asdict(item)
        row['selected_data'] = json.loads(row.pop('source_snapshot'))
        row['locator_status'] = '平台未探测路径/原件；仅人工填写的定位，不是访问许可。'
        row['verification_history'] = [asdict(v) for v in history]
        if item.source_kind == 'accepted_report':
            parent = board.tasks[item.source_task_id]
            accepted = next(e for raw in parent.events if (e := parse_event(raw)) and e.id == item.acceptance_event_id)
            row['acceptance_at'] = accepted.at
        rows.append(row)
    return EVIDENCE_MARKER + canonical({'version': 1, 'items': rows, 'conflicting_fact_refs': _conflicts(items),
                                       'boundary': '历史自报、任务人工验收、原件人工核验是不同证据等级；一切引用都不是新许可，均不是新成果、实施、路径或预算许可。事实仅属于原问题及原范围，不自动沿用技术批准。'})


def historical_quote_valid(board: 'Board', task: 'Task', quote: str) -> bool:
    """An older exact append-only inventory prefix is data, not arbitrary hidden text."""
    if not quote:
        return True  # Inventory may have been selected after this canonical Run.
    if not quote.startswith(EVIDENCE_MARKER):
        return False
    try:
        old = json.loads(quote[len(EVIDENCE_MARKER):])
        current_export = evidence_export(board, task)
        if not current_export:
            return False
        current = json.loads(current_export[len(EVIDENCE_MARKER):])
        if (type(old) is not dict or set(old) != set(current) or type(old['items']) is not list
                or not old['items'] or len(old['items']) > len(current['items'])):
            return False
        expected_items = []
        ids = set()
        for old_item, saved in zip(old['items'], current['items'], strict=False):
            if type(old_item) is not dict or type(old_item.get('verification_history')) is not list:
                return False
            count = len(old_item['verification_history'])
            if count > len(saved['verification_history']):
                return False
            expected_items.append({**saved, 'verification_history': saved['verification_history'][:count]})
            ids.add(saved['id'])
        expected = {**current, 'items': expected_items,
                    'conflicting_fact_refs': [pair for pair in current['conflicting_fact_refs'] if set(pair) <= ids]}
        return EVIDENCE_MARKER + canonical(expected) == quote
    except (ValueError, KeyError, TypeError):
        return False


@dataclass(frozen=True)
class EvidenceSource:
    kind: str
    task_id: int
    run_id: str
    event_id: str
    seq: int
    acceptance_id: str
    record_id: str
    fact_key: str
    source_digest: str
    label: str
    detail: str


def inventory_candidates(board: 'Board') -> dict[str, EvidenceSource]:
    """Existing canonical records only; no chat/FS search or guessed parent."""
    candidates: dict[str, EvidenceSource] = {}
    for task in board.tasks.values():
        if task.status != 'done' or task.execution != 'pi':
            continue
        run = board.runs.get(task.run_id)
        from journal import current_report
        event = current_report(task, run)
        accepted = next((e for raw in reversed(task.events) if (e := parse_event(raw))
                         and e.kind == 'accepted' and e.run_id == task.run_id), None)
        if not run or not event or not accepted:
            continue
        try:
            payload = source_payload(board, task, event.id, accepted.id, ['summary'])
        except ValueError:
            continue
        key = f'report:{task.id}:{event.id}'
        detail = (f'D{task.id:03} · {task.title}\nRun {run.id}\n报告 {event.id} / 修订 {event.seq}'
                  f'\n人工验收 {accepted.id} / 时间 {accepted.at}\n精确目录 {payload["cwd"]}'
                  '\n这是执行者自报与任务验收锚点，不证明原件可访问、已核验或新目标已完成。')
        candidates[key] = EvidenceSource('accepted_report', task.id, run.id, event.id, event.seq, accepted.id,
                                         '', '', digest(payload), f'D{task.id:03} · 已验收报告 · {task.title}', detail)
    for record in board.decisions.values():
        if not record.confirmed_at:
            continue
        for spec, answer in zip(record.specs, record.preview.answers, strict=True):
            if not answer.business:
                continue
            try:
                payload = fact_payload(board, record.id, spec.key)
            except ValueError:
                continue
            key = f'fact:{record.id}:{spec.key}'
            detail = (f'D{record.task_id:03} · {record.preview.context.title}\n人工确认 {record.id} / 时间 {record.confirmed_at}'
                      f'\nRun {record.preview.source.run_id}\n报告 {record.preview.source.event_id} / 修订 {record.preview.source.seq}'
                      f'\n精确目录 {payload["cwd"]}\n{payload["source_snapshot"]}'
                      '\n仅引用原问题/原范围的历史事实，不沿用技术批准或自动适用新目标。')
            candidates[key] = EvidenceSource('business_fact', record.task_id, record.preview.source.run_id,
                                             record.preview.source.event_id, record.preview.source.seq, '', record.id,
                                             spec.key, digest(payload), f'D{record.task_id:03} · 人工事实 · {spec.question}', detail)
    return candidates


def reference_from_candidate(board: 'Board', owner: 'Task', source: EvidenceSource, refs: list[str],
                             *, name: str, locator: str, limits: str, gaps: str, next_owner: str) -> EvidenceItem:
    _positive(source.task_id)
    _id(source.run_id)
    _id(source.event_id)
    _positive(source.seq)
    _digest(source.source_digest)
    if source.kind == 'accepted_report':
        _id(source.acceptance_id)
        parent = board.tasks.get(source.task_id)
        if not parent:
            raise ValueError('选定报告来源已删除。')
        payload = source_payload(board, parent, source.event_id, source.acceptance_id, ['summary'])
        if digest(payload) != source.source_digest or parent.run_id != source.run_id or payload['source_report_seq'] != source.seq:
            raise ValueError('选定报告或验收在填写期间已变化；输入保留，请重新核对。')
        return report_reference(board, owner, parent, source.event_id, source.acceptance_id, refs, name=name,
                                locator=locator, limits=limits, gaps=gaps, next_owner=next_owner)
    if source.kind != 'business_fact':
        raise ValueError('库存来源类型无效。')
    payload = fact_payload(board, source.record_id, source.fact_key)
    if digest(payload) != source.source_digest:
        raise ValueError('选定人工事实在填写期间已变化；输入保留，请重新核对。')
    return fact_reference(board, owner, source.record_id, source.fact_key, name=name, locator=locator,
                          limits=limits, gaps=gaps, next_owner=next_owner)


def inventory_text(board: 'Board', task: 'Task') -> str:
    items = [i for i in board.evidence_items.values() if i.owner_task_id == task.id]
    lines = ['成果库存 · 仅显式选定来源；不会扫描目录或搜索 Pi 聊天。']
    if not items:
        lines.append('尚无选定引用。执行者自报 / 平台任务验收 / 原件核验必须分开。')
    for item in items:
        try:
            validate_item(board, item)
            status = '来源锚点一致；原件及路径未核实（平台未探测）'
        except ValueError as error:
            status = f'来源变化 · 证据状态 unknown，不能派发：{error}'
        lines.extend([f'\n{item.name} · 来源 D{item.source_task_id:03}', f'库存 {item.id}',
                      f'Run {item.source_run_id}', f'报告 {item.source_report_event_id} / 修订 {item.source_report_seq}',
                      f'人工验收 {item.acceptance_event_id}' if item.acceptance_event_id else f'人工事实记录 {item.decision_record_id} / 问题 {item.fact_key}',
                      f'来源摘要 {item.source_digest}', f'定位 {item.locator}', status,
                      f'适用边界 {item.limits}', f'缺口 {item.gaps}', f'下一责任 {item.next_owner}', item.source_snapshot])
        for record in board.evidence_verifications.values():
            if record.item_id == item.id:
                labels = {'verified': '人工声称原件已核验（非平台探测）', 'unverified': '仍未核实', 'conflict': '冲突，需重新核对'}
                lines.append(f'人工核验记录 {record.id} · {labels[record.result]}\n核验人 {record.verifier} / 方法 {record.method}'
                             f'\n核验时间 {record.checked_at} / 保存时间 {record.recorded_at}\n边界 {record.limits}'
                             f'\n缺口 {record.gaps} / 下一责任 {record.next_owner}')
    # Only well-formed validated data can participate in conflict comparison.
    try:
        for pair in _conflicts(items):
            lines.append('人工事实答案冲突 · ' + ' ↔ '.join(pair) + '；保留审计，不自动套用或批准。')
    except (ValueError, KeyError, TypeError):
        lines.append('旧事实来源不能核对，状态 unknown。')
    lines.append('\n旧收益、名单和测试不是新成果；所有引用/人工核验都不构成新许可，不发送也不执行。')
    return '\n'.join(lines)
