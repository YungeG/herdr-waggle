"""Explicit cumulative human accounting. No I/O, metering, dispatch or hard quota.

Receipt numbers are claims until complete human coverage is recorded against exact
saved N4 evidence. Formal approval rollout is deliberately closed; pure saved-only
grant mechanics are not a runtime permission or an authenticated executor receipt.
"""
from copy import deepcopy
from dataclasses import asdict, dataclass, fields
from typing import TYPE_CHECKING, Any, TypeVar, cast
from uuid import UUID, uuid4, uuid5

from decisions import Budget
from evidence import (
    _digest,
    _id,
    _positive,
    _text,
    _time,
    digest,
    now,
    validate_item,
    validate_verification,
)

if TYPE_CHECKING:
    from model import Board

UNITS = ('attempts', 'minutes', 'storage_mib')
REGISTRIES = ('budget_scopes', 'usage_observations', 'ledger_verifications', 'budget_grants')
FORMAL_APPROVAL_ENABLED = False  # Independent real-evidence/rollout gate, not a UI preference.
MAX_SCOPES = 64
MAX_ROWS = 128  # Per scope, per registry. Never prune old authority or receipts.
DENIAL_AUTHOR = 'system:waggle-budget-binding'
DENIAL_METHOD = 'Saved source binding changed; denial only, no inferred usage or authority.'
DENIAL_GAPS = '原核验来源绑定已变化；恢复旧文字不能恢复旧授权。'


def _row(value: object, cls) -> dict[str, Any]:
    if type(value) is not dict or set(value) != {field.name for field in fields(cls)}:
        raise ValueError('累计账本字段缺失或不兼容；不会猜测旧用量。')
    if type(value['version']) is not int or value['version'] != 1:
        raise ValueError('累计账本记录版本无效。')
    _id(value['id'])
    return cast(dict[str, Any], value)  # Shape / exact keys / UUID / version were validated above.


def _units(value: object) -> tuple[str, str, str]:
    if not isinstance(value, (list, tuple)) or type(value) not in (list, tuple) or tuple(value) != UNITS:
        raise ValueError('资源单位必须精确为次数 / 分钟 / MiB；不转换 GB 或其他单位。')
    return UNITS


def _amounts(value: object) -> tuple[int, int, int]:
    if (not isinstance(value, (list, tuple)) or type(value) not in (list, tuple) or len(value) != 3
            or any(type(item) is not int or not 0 <= item <= 2**31 - 1 for item in value)):
        raise ValueError('累计已用必须是三个有单位的非负整数；不接受布尔、小数或推测值。')
    return value[0], value[1], value[2]


def _key(value: object) -> str:
    import re
    value = _text(value, 180)
    if not re.fullmatch(r'[a-z0-9][a-z0-9._:/-]{0,179}', value):
        raise ValueError('请填写稳定小写资源 / 计量标识，不把 Task 或 Run 身份当作清零依据。')
    return value


@dataclass(frozen=True)
class BudgetScope:
    version: int
    id: str
    resource_key: str
    meter_id: str
    definition: str
    units: tuple[str, str, str]
    started_at: str
    author: str
    created_at: str

    @classmethod
    def from_record(cls, value: object) -> 'BudgetScope':
        row = _row(value, cls).copy()
        for name in ('resource_key', 'meter_id'):
            _key(row[name])
        _text(row['definition'], 1000)
        _text(row['author'], 120)
        for name in ('started_at', 'created_at'):
            _time(row[name])
        if row['started_at'] > row['created_at']:
            raise ValueError('累计范围起点不能在创建时间之后。')
        row['units'] = _units(row['units'])
        return cls(**row)


@dataclass(frozen=True)
class UsageObservation:
    version: int
    id: str
    scope_id: str
    scope_digest: str
    receipt_id: str
    meter_id: str
    units: tuple[str, str, str]
    revision: int
    used: tuple[int, int, int]
    coverage_start: str
    coverage_end: str
    source_kind: str
    evidence_item_id: str
    evidence_digest: str
    recorded_at: str

    @classmethod
    def from_record(cls, value: object) -> 'UsageObservation':
        row = _row(value, cls).copy()
        for name in ('scope_id', 'evidence_item_id'):
            _id(row[name])
        for name in ('scope_digest', 'evidence_digest'):
            _digest(row[name])
        _text(row['receipt_id'], 180)
        if row['receipt_id'] != row['receipt_id'].strip() or any(ord(c) < 32 for c in row['receipt_id']):
            raise ValueError('回执身份必须稳定且不能含空白边界或控制字符。')
        _key(row['meter_id'])
        _positive(row['revision'])
        if type(row['source_kind']) is not str or row['source_kind'] not in {'human_receipt', 'executor_self_report'}:
            raise ValueError('请明确来源是人工回执还是执行者自报；两者都不自动证明用量。')
        for name in ('coverage_start', 'coverage_end', 'recorded_at'):
            _time(row[name])
        if not row['coverage_start'] <= row['coverage_end'] <= row['recorded_at']:
            raise ValueError('回执覆盖时间无效或在未来。')
        row['units'], row['used'] = _units(row['units']), _amounts(row['used'])
        return cls(**row)


@dataclass(frozen=True)
class LedgerVerification:
    version: int
    id: str
    scope_id: str
    scope_digest: str
    head_digest: str
    observation_id: str
    coverage_start: str
    coverage_end: str
    complete_coverage: bool
    result: str
    verifier: str
    method: str
    limits: str
    gaps: str
    next_owner: str
    checked_at: str
    recorded_at: str

    @classmethod
    def from_record(cls, value: object) -> 'LedgerVerification':
        row = _row(value, cls).copy()
        for name in ('scope_id', 'observation_id'):
            _id(row[name])
        for name in ('scope_digest', 'head_digest'):
            _digest(row[name])
        if type(row['complete_coverage']) is not bool:
            raise ValueError('必须明确说明是否核对从起点至核验时刻的全部累计用量。')
        if type(row['result']) is not str or row['result'] not in {'verified', 'unknown', 'conflict', 'invalidated'}:
            raise ValueError('账本核验结果无效。')
        for name, limit in (('verifier', 120), ('method', 1000), ('limits', 500), ('next_owner', 300)):
            _text(row[name], limit)
        _text(row['gaps'], 500, empty=True)
        if row['result'] == 'verified' and (not row['complete_coverage'] or row['gaps']):
            raise ValueError('存在缺口或未完整覆盖时，已用量与余额仍为 unknown。')
        if row['result'] != 'verified' and (row['complete_coverage'] or not row['gaps'].strip()):
            raise ValueError('未知、冲突或失效核验不能宣称完整覆盖，且必须说明缺口。')
        for name in ('coverage_start', 'coverage_end', 'checked_at', 'recorded_at'):
            _time(row[name])
        if not row['coverage_start'] <= row['coverage_end'] == row['checked_at'] <= row['recorded_at']:
            raise ValueError('核验必须明确覆盖到实际核验时刻，不能引用未来核验。')
        return cls(**row)


@dataclass(frozen=True)
class BudgetGrant:
    version: int
    id: str
    scope_id: str
    scope_digest: str
    action: str
    previous_id: str
    verification_id: str
    verification_digest: str
    head_digest: str
    cap: Budget | None
    author: str
    reason: str
    confirmed_at: str

    @classmethod
    def from_record(cls, value: object) -> 'BudgetGrant':
        row = _row(value, cls).copy()
        _id(row['scope_id'])
        _digest(row['scope_digest'])
        _id(row['previous_id'], empty=True)
        _text(row['author'], 120)
        _text(row['reason'], 1000)
        _time(row['confirmed_at'])
        _digest(row['head_digest'])
        if type(row['action']) is not str or row['action'] not in {'grant', 'revoke'}:
            raise ValueError('累计授权动作无效。')
        cap = Budget.from_record(row['cap']) if row['cap'] is not None else None
        if row['action'] == 'grant':
            _id(row['verification_id'])
            _digest(row['verification_digest'])
            if cap is None:
                raise ValueError('新累计授权必须明确总帽及停止条件。')
        elif (not row['previous_id'] or cap is not None or row['verification_id'] != '' or row['verification_digest'] != ''):
            raise ValueError('撤销须指向最后一项授权，且不能夹带新总帽或核验。')
        row['cap'] = cap
        return cls(**row)


BudgetRecord = BudgetScope | UsageObservation | LedgerVerification | BudgetGrant
BudgetRecordT = TypeVar('BudgetRecordT', bound=BudgetRecord)
CLASSES = (BudgetScope, UsageObservation, LedgerVerification, BudgetGrant)


def scope_record(board: 'Board', *, resource_key: str, meter_id: str, definition: str,
                 author: str, started_at: str) -> BudgetScope:
    record = BudgetScope(1, str(uuid4()), resource_key, meter_id, definition, UNITS, started_at, author, now())
    validate_new_record(board, record)
    return record


def _scope(board: 'Board', scope_id: str) -> BudgetScope:
    _id(scope_id)
    scope = board.budget_scopes.get(scope_id)
    if scope is None:
        raise ValueError('请明确选择已保存的稳定累计资源范围。')
    BudgetScope.from_record(asdict(scope))
    return scope


def _rows(board: 'Board', name: str, scope_id: str) -> list:
    return [row for row in getattr(board, name).values() if row.scope_id == scope_id]


def _receipt_payload(record: UsageObservation) -> dict:
    return {key: value for key, value in asdict(record).items() if key not in {'id', 'recorded_at'}}


def _observations(board: 'Board', scope_id: str) -> list[UsageObservation]:
    unique: dict[str, UsageObservation] = {}
    for record in _rows(board, 'usage_observations', scope_id):
        unique.setdefault(digest(_receipt_payload(record)), record)
    return list(unique.values())


def _conflict(board: 'Board', scope_id: str) -> bool:
    seen_receipts: set[str] = set()
    seen_revisions: set[int] = set()
    previous = None
    for row in _observations(board, scope_id):
        if (row.receipt_id in seen_receipts or row.revision in seen_revisions or (previous and
                (row.revision < previous.revision or row.coverage_end < previous.coverage_end
                 or any(a < b for a, b in zip(row.used, previous.used, strict=True))))):
            return True
        seen_receipts.add(row.receipt_id)
        seen_revisions.add(row.revision)
        previous = row
    return False


def _source_binding(board: 'Board', row: UsageObservation) -> dict:
    item = board.evidence_items.get(row.evidence_item_id)
    if item is None:
        return {'item': row.evidence_item_id, 'state': 'missing'}
    owner = board.tasks.get(item.owner_task_id)
    parent = board.tasks.get(item.source_task_id)
    def scope(task):
        return ([task.id, task.title, task.description, task.project, task.execution, board.projects.get(task.project)]
                if task else None)
    run = board.runs.get(item.source_run_id)
    verifications = [asdict(v) for v in board.evidence_verifications.values() if v.item_id == item.id]
    return {'item': asdict(item), 'owner_scope': scope(owner), 'source_scope': scope(parent),
            'source_state': [parent.status, parent.run_id, parent.events] if parent else None,
            'source_run': asdict(run) if run else None, 'verifications': verifications}


def _bindings(board: 'Board', rows: list[UsageObservation]) -> str:
    return digest([_source_binding(board, row) for row in rows])


def ledger_head(board: 'Board', scope_id: str) -> str:
    scope = _scope(board, scope_id)
    rows = _rows(board, 'usage_observations', scope_id)
    return digest({'scope': asdict(scope), 'observations': [asdict(row) for row in rows],
                   'sources': _bindings(board, rows)})


def _source_verified(board: 'Board', row: UsageObservation) -> None:
    item = board.evidence_items.get(row.evidence_item_id)
    if item is None or digest(asdict(item)) != row.evidence_digest:
        raise ValueError('回执原始库存来源缺失或摘要已变化；累计已用 unknown。')
    validate_item(board, item)
    records = [v for v in board.evidence_verifications.values() if v.item_id == item.id]
    if not records or records[-1].result != 'verified':
        raise ValueError('缺少该精确库存的最新人工原件核验；自报或已验收报告不等于用量证明。')
    validate_verification(board, records[-1])


def observation_record(board: 'Board', scope_id: str, *, receipt_id: str, meter_id: str, revision: int,
                       used: tuple[int, int, int], observed_at: str, source_kind: str,
                       evidence_item_id: str) -> UsageObservation:
    scope = _scope(board, scope_id)
    _id(evidence_item_id)
    item = board.evidence_items.get(evidence_item_id)
    if item is None:
        raise ValueError('只能引用已保存的 N4 成果证据；不读取路径或猜测旧用量。')
    record = UsageObservation(1, str(uuid4()), scope.id, digest(asdict(scope)), receipt_id, meter_id, UNITS,
                              revision, used, scope.started_at, observed_at, source_kind, item.id,
                              digest(asdict(item)), now())
    UsageObservation.from_record(asdict(record))
    duplicate = next((r for r in _rows(board, 'usage_observations', scope.id)
                      if _receipt_payload(r) == _receipt_payload(record)), None)
    if duplicate:
        return duplicate  # Even a full audit remains idempotent; no new claim or capacity is needed.
    validate_new_record(board, record)
    return record


def ledger_verification_record(board: 'Board', scope_id: str, *, observation_id: str, result: str,
                               complete_coverage: bool, verifier: str, method: str, limits: str, gaps: str,
                               next_owner: str, checked_at: str) -> LedgerVerification:
    scope = _scope(board, scope_id)
    record = LedgerVerification(1, str(uuid4()), scope.id, digest(asdict(scope)), ledger_head(board, scope.id),
                                observation_id, scope.started_at, checked_at, complete_coverage, result,
                                verifier, method, limits, gaps, next_owner, checked_at, now())
    validate_new_record(board, record)
    return record


def _verified(board: 'Board', scope_id: str) -> LedgerVerification:
    _scope(board, scope_id)
    rows = _observations(board, scope_id)
    if not rows:
        raise ValueError('没有可核验累计回执：已用量及余额 unknown，不是零。')
    if _conflict(board, scope_id):
        raise ValueError('累计回执身份、并发修订或单调用量冲突：余额 unknown；不选择任意来源。')
    verifications = _rows(board, 'ledger_verifications', scope_id)
    if not verifications or verifications[-1].result != 'verified':
        raise ValueError('尚无当前完整人工核验，或最新核验未知 / 冲突 / 已失效：余额 unknown。')
    current = verifications[-1]
    if current.head_digest != ledger_head(board, scope_id) or current.observation_id != rows[-1].id:
        raise ValueError('核验后的回执或来源已变化：旧核验不能批准继续用量。')
    for row in rows:
        _source_verified(board, row)
    return current


def grant_record(board: 'Board', scope_id: str, *, cap: Budget, author: str, reason: str) -> BudgetGrant:
    """Pure saved-only mechanics, NOT the closed formal UI/runtime approval entry."""
    scope = _scope(board, scope_id)
    verification = _verified(board, scope_id)
    previous = _rows(board, 'budget_grants', scope_id)
    record = BudgetGrant(1, str(uuid4()), scope.id, digest(asdict(scope)), 'grant', previous[-1].id if previous else '',
                         verification.id, digest(asdict(verification)), verification.head_digest, cap, author, reason, now())
    validate_new_record(board, record)
    return record


def revoke_record(board: 'Board', scope_id: str, *, author: str, reason: str) -> BudgetGrant:
    scope = _scope(board, scope_id)
    previous = _rows(board, 'budget_grants', scope_id)
    if not previous or previous[-1].action != 'grant':
        raise ValueError('没有最后一项未撤销的累计授权；不会猜测撤销对象。')
    record = BudgetGrant(1, str(uuid4()), scope.id, digest(asdict(scope)), 'revoke', previous[-1].id,
                         '', '', ledger_head(board, scope.id), None, author, reason, now())
    validate_new_record(board, record)
    return record


def validate_new_record(board: 'Board', record: BudgetRecord) -> None:
    if type(record) not in CLASSES:
        raise ValueError('未知累计账本记录类型。')
    type(record).from_record(asdict(record))
    if isinstance(record, BudgetScope):
        if len(board.budget_scopes) >= MAX_SCOPES or any(r.resource_key == record.resource_key for r in board.budget_scopes.values()):
            raise ValueError('稳定资源范围已存在或达到上限；新任务 / 会话不能重置旧账本。')
        if record.created_at > now():
            raise ValueError('不能以未来范围形成事实。')
        return
    scope = _scope(board, record.scope_id)
    if record.scope_digest != digest(asdict(scope)):
        raise ValueError('计量范围已变化；不得把旧回执转到其他资源范围。')
    name = REGISTRIES[CLASSES.index(type(record))]
    rows = _rows(board, name, scope.id)
    human_rows = [r for r in rows if not isinstance(r, LedgerVerification) or r.result != 'invalidated']
    if len(human_rows) >= MAX_ROWS:
        raise ValueError('累计审计已达到明示上限；不会清除旧用量或授权。')
    if isinstance(record, UsageObservation):
        if record.meter_id != scope.meter_id or record.units != scope.units or record.coverage_start != scope.started_at:
            raise ValueError('并发来源、单位或覆盖起点不匹配；不接纳为同一账本。')
        item = board.evidence_items.get(record.evidence_item_id)
        if item is None or record.evidence_digest != digest(asdict(item)):
            raise ValueError('回执必须引用已持久化精确 N4 库存，不能同次发明来源。')
        validate_item(board, item)
        if record.recorded_at > now() or record.recorded_at < max(scope.created_at, item.created_at):
            raise ValueError('回执保存时间无效。')
    elif isinstance(record, LedgerVerification):
        if record.result == 'invalidated':
            raise ValueError('SYSTEM 失效记录只能由持久化来源变化产生，不能人工填写。')
        observation = board.usage_observations.get(record.observation_id)
        if (observation is None or observation.scope_id != scope.id or record.coverage_start != scope.started_at
                or record.head_digest != ledger_head(board, scope.id) or record.coverage_end < observation.coverage_end
                or record.recorded_at > now() or record.recorded_at < observation.recorded_at):
            raise ValueError('核验来源、完整覆盖、摘要或保存时间不匹配。')
        if record.result == 'verified':
            observations = _observations(board, scope.id)
            if _conflict(board, scope.id) or observations[-1].id != record.observation_id:
                raise ValueError('冲突或非最新累计回执不能核验为完整。')
            for row in observations:
                _source_verified(board, row)
    else:
        previous = _rows(board, 'budget_grants', scope.id)
        if record.previous_id != (previous[-1].id if previous else '') or record.confirmed_at > now():
            raise ValueError('累计授权顺序已变化或确认时间无效；不能回退 / 复活旧 grant。')
        if record.action == 'grant':
            verification = _verified(board, scope.id)
            observation = board.usage_observations[verification.observation_id]
            if (record.verification_id != verification.id or record.verification_digest != digest(asdict(verification))
                    or record.head_digest != verification.head_digest or record.confirmed_at < verification.recorded_at):
                raise ValueError('新累计授权须绑定最后一份完整核验与当前用量，而非旧文字。')
            assert record.cap is not None
            cap = (record.cap.attempts, record.cap.minutes, record.cap.storage_mib)
            if any(a < b for a, b in zip(cap, observation.used, strict=True)):
                raise ValueError('新总帽不能小于已核验累计用量；总帽不是剩余额度。')
        elif not previous or previous[-1].action != 'grant' or record.head_digest != ledger_head(board, scope.id):
            raise ValueError('撤销对象或当前账本已变化。')
    stamp = record.confirmed_at if isinstance(record, BudgetGrant) else record.recorded_at
    if rows:
        last = rows[-1]
        old_stamp = last.confirmed_at if isinstance(last, BudgetGrant) else last.recorded_at
        if stamp < old_stamp:
            raise ValueError('累计审计保存时间不能倒退。')


def append_record(board: 'Board', record: BudgetRecordT) -> BudgetRecordT:
    if type(record) not in CLASSES:
        raise ValueError('未知累计账本记录类型。')
    name = REGISTRIES[CLASSES.index(type(record))]
    registry = getattr(board, name)
    existing = registry.get(record.id)
    if existing == record:
        return existing
    if existing is not None or any(record.id in getattr(board, key) for key in REGISTRIES):
        raise ValueError('累计记录身份重复或试图改写旧记录。')
    record = cast(BudgetRecordT, type(record).from_record(asdict(record)))  # Same validated record class; freeze nested lists too.
    if isinstance(record, UsageObservation):
        duplicate = next((r for r in _rows(board, name, record.scope_id)
                          if _receipt_payload(r) == _receipt_payload(record)), None)
        if duplicate:
            return duplicate
    validate_new_record(board, record)
    registry[record.id] = record
    return record


def check_budget_structure(board: 'Board') -> None:
    keys: set[str] = set()
    resources: set[str] = set()
    for name, cls in zip(REGISTRIES, CLASSES, strict=True):
        counts: dict[str, int] = {}
        last_times: dict[str, str] = {}
        prior_grants: dict[str, str] = {}
        for key, record in getattr(board, name).items():
            cls.from_record(asdict(record))
            if type(record) is not cls or key != record.id or key in keys:
                raise ValueError('累计账本字典、身份或类型冲突。')
            keys.add(key)
            if isinstance(record, BudgetScope):
                if record.resource_key in resources or len(resources) >= MAX_SCOPES:
                    raise ValueError('累计资源范围重复或超过上限；不能清零。')
                resources.add(record.resource_key)
                continue
            scope = _scope(board, record.scope_id)
            if record.scope_digest != digest(asdict(scope)):
                raise ValueError('累计审计不属于精确稳定资源范围。')
            is_system = isinstance(record, LedgerVerification) and record.result == 'invalidated'
            counts[scope.id] = counts.get(scope.id, 0) + (0 if is_system else 1)
            if counts[scope.id] > MAX_ROWS:
                raise ValueError('累计审计超过上限。')
            stamp = record.confirmed_at if isinstance(record, BudgetGrant) else record.recorded_at
            if stamp < max(scope.created_at, last_times.get(scope.id, '')):
                raise ValueError('累计审计时间顺序无效。')
            last_times[scope.id] = stamp
            if isinstance(record, UsageObservation):
                if record.meter_id != scope.meter_id or record.coverage_start != scope.started_at:
                    raise ValueError('计量来源或累计起点不匹配。')
            elif isinstance(record, LedgerVerification):
                observation = board.usage_observations.get(record.observation_id)
                if (observation is None or observation.scope_id != scope.id or record.coverage_start != scope.started_at
                        or record.coverage_end < observation.coverage_end or stamp < observation.recorded_at):
                    raise ValueError('核验与已保存累计回执不兼容。')
                if is_system and not _denial_fields(record):
                    raise ValueError('SYSTEM 拒绝记录不能伪装成人工核验。')
            else:
                if record.previous_id != prior_grants.get(scope.id, ''):
                    raise ValueError('累计授权 / 撤销顺序不能回退。')
                if record.action == 'grant':
                    verification = board.ledger_verifications.get(record.verification_id)
                    if (verification is None or verification.scope_id != scope.id or verification.result != 'verified'
                            or record.verification_digest != digest(asdict(verification))
                            or record.head_digest != verification.head_digest or stamp < verification.recorded_at):
                        raise ValueError('累计授权与精确完整核验不兼容。')
                elif not record.previous_id or board.budget_grants[record.previous_id].action != 'grant':
                    raise ValueError('重复撤销或缺失撤销对象。')
                prior_grants[scope.id] = key


def _denial_id(scope_id: str, verification_id: str) -> str:
    return str(uuid5(UUID(scope_id), 'waggle-budget-binding-invalidation:' + verification_id))


def _denial_fields(record: LedgerVerification) -> bool:
    return (record.verifier == DENIAL_AUTHOR and record.method == DENIAL_METHOD and record.limits == 'denial only'
            and record.gaps == DENIAL_GAPS and record.next_owner == '重新明确核验责任'
            and not record.complete_coverage and record.coverage_end == record.checked_at == record.recorded_at)


def stage_ledger_invalidations(previous: 'Board', candidate: 'Board') -> 'Board':
    """Persist irreversible denials on a copy; caller input changes only after commit."""
    result = candidate
    for scope_id in previous.budget_scopes:
        checks = _rows(previous, 'ledger_verifications', scope_id)
        if not checks or checks[-1].result != 'verified':
            continue
        old = checks[-1]
        rows = _rows(previous, 'usage_observations', scope_id)
        if _bindings(previous, rows) == _bindings(candidate, rows):
            continue
        denial_id = _denial_id(scope_id, old.id)
        if denial_id in candidate.ledger_verifications:
            continue
        if result is candidate:
            result = deepcopy(candidate)
        at = now()
        record = LedgerVerification(1, denial_id, scope_id, old.scope_digest, ledger_head(result, scope_id),
                                    old.observation_id, old.coverage_start, at, False, 'invalidated', DENIAL_AUTHOR,
                                    DENIAL_METHOD, 'denial only', DENIAL_GAPS, '重新明确核验责任', at, at)
        result.ledger_verifications[record.id] = record
    return result


def check_budget_after(previous: 'Board', board: 'Board') -> None:
    human: list[BudgetRecord] = []
    for name in REGISTRIES:
        old, current = getattr(previous, name), getattr(board, name)
        if list(current)[:len(old)] != list(old) or any(current.get(key) != row for key, row in old.items()):
            raise ValueError('累计范围、用量、核验和撤销审计不能删除、替换、重排或回退，任务删除也不例外。')
        for key in current.keys() - old.keys():
            record = current[key]
            if isinstance(record, LedgerVerification) and record.result == 'invalidated':
                checks = _rows(previous, 'ledger_verifications', record.scope_id)
                rows = _rows(previous, 'usage_observations', record.scope_id)
                if (not checks or checks[-1].result != 'verified' or record.id != _denial_id(record.scope_id, checks[-1].id)
                        or not _denial_fields(record) or record.observation_id != checks[-1].observation_id
                        or record.head_digest != ledger_head(board, record.scope_id) or record.recorded_at > now()
                        or _bindings(previous, rows) == _bindings(board, rows)):
                    raise ValueError('SYSTEM 失效审计只能来自本次持久化的明确来源绑定变化。')
            else:
                human.append(record)
                validate_new_record(previous, record)
                if isinstance(record, UsageObservation) and any(_receipt_payload(row) == _receipt_payload(record)
                        for row in _rows(previous, 'usage_observations', record.scope_id)):
                    raise ValueError('相同回执是幂等操作；不得用新 ID 追加重复累计用量。')
    if len(human) > 1:
        raise ValueError('每次独立人工确认最多保存一份累计账本记录；来源、核验、授权必须分别保存。')
    if human and any(value != board.__dict__.get(key) for key, value in previous.__dict__.items() if key not in REGISTRIES):
        raise ValueError('累计账本确认只能保存审计；不得同时派发、分配、修改来源或形成业务决定。')


@dataclass(frozen=True)
class LedgerView:
    scope_id: str
    used: tuple[int, int, int] | None
    cap: Budget | None
    remaining: tuple[int, int, int] | None
    reason: str
    verification_id: str = ''
    grant_id: str = ''


def ledger_view(board: 'Board', scope_id: str) -> LedgerView:
    try:
        verification = _verified(board, scope_id)
    except ValueError as error:
        return LedgerView(scope_id, None, None, None, str(error))
    used = board.usage_observations[verification.observation_id].used
    grants = _rows(board, 'budget_grants', scope_id)
    if not grants:
        return LedgerView(scope_id, used, None, None, '累计已用已人工核验；无本次累计授权，余额 unknown。', verification.id)
    grant = grants[-1]
    if grant.action == 'revoke':
        return LedgerView(scope_id, used, None, None, '最后一项累计授权已明确撤销；旧文字不能复活。', verification.id, grant.id)
    if grant.verification_id != verification.id or grant.verification_digest != digest(asdict(verification)):
        return LedgerView(scope_id, used, None, None, '核验已修订；旧授权失效，需独立核对新累计总帽。', verification.id, grant.id)
    assert grant.cap is not None
    cap = (grant.cap.attempts, grant.cap.minutes, grant.cap.storage_mib)
    if any(a < b for a, b in zip(cap, used, strict=True)):
        return LedgerView(scope_id, used, None, None, '累计已用超过记录总帽；停止，不能继续。', verification.id, grant.id)
    return LedgerView(scope_id, used, grant.cap, tuple(a - b for a, b in zip(cap, used, strict=True)),
                      '仅截至人工核验时刻的已存累计额度；不是派发许可、实际用量或执行器硬限制。', verification.id, grant.id)


def ledger_text(board: 'Board', *, durable: bool) -> str:
    lines = ['累计资源账本 · 跨 Task / Run / 重连保留，不自动计量或清零。',
             '正式预算批准入口关闭：独立真实证据 / 启用门尚未通过。业务答案、建议和旧倾向不是授权。',
             '单位：次数 / 分钟 / MiB；范围由人明确说明，不保证全机器、全部费用或文件系统硬限额。',
             '人工身份未认证；数字仅截至明确核验时刻，不保证此后未发生用量。保存授权、派发许可、实际用量是三件事。',
             '本地持久化审计。' if durable else '仅本会话，尚未持久化；不能声称核验或授权已保存。']
    if not board.budget_scopes:
        lines.append('尚无稳定计量范围与可核验旧用量：已用及余额 unknown，不是零。')
    for scope in board.budget_scopes.values():
        view = ledger_view(board, scope.id)
        def fmt(values):
            return f'{values[0]} 次 / {values[1]} 分钟 / {values[2]} MiB' if values is not None else 'unknown（未知，不是零）'
        cap = (view.cap.attempts, view.cap.minutes, view.cap.storage_mib) if view.cap else None
        lines.extend([f'\n{scope.resource_key} · 稳定范围 {scope.id}', f'计量来源：{scope.meter_id}；起点：{scope.started_at}',
                      scope.definition, '已核验累计已用：' + fmt(view.used), '已存累计总帽：' + fmt(cap),
                      '记录余额：' + fmt(view.remaining), view.reason])
        for row in _rows(board, 'usage_observations', scope.id):
            lines.append(f'回执 {row.receipt_id} · 修订 {row.revision} · {row.source_kind}（自报 / 人工记录，非自动可信） · '
                         + fmt(row.used) + f'；覆盖 {row.coverage_start} → {row.coverage_end}；来源 {row.evidence_item_id}')
        for row in _rows(board, 'ledger_verifications', scope.id):
            lines.append(f'核验 {row.id} · {row.result} · {row.verifier} · {row.checked_at}；方法 {row.method}；'
                         f'边界 {row.limits}；缺口 {row.gaps or "明确无缺口（人工陈述）"}；下一责任 {row.next_owner}')
        for row in _rows(board, 'budget_grants', scope.id):
            lines.append(f'累计授权审计 {row.id} · {row.action} · 取代 / 撤销 {row.previous_id or "无"} · '
                         f'{row.author} · {row.confirmed_at}；{row.reason}；停止条件：'
                         + ('；'.join(row.cap.stop_conditions) if row.cap else '已撤销，不能继续'))
    return '\n'.join(lines)
