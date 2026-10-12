"""Private N5 accounting smoke: no live state, tool controls or real grants."""
import unittest
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4

from budget_ledger import (
    BudgetScope,
    UsageObservation,
    grant_record,
    ledger_verification_record,
    ledger_view,
    now,
    observation_record,
    revoke_record,
    scope_record,
)
from decisions import Budget
from evidence import verification_record
from model import Board
from store import StorageError, Store, encode
from test_n4_inventory import add_report, inventory_board

ORIGIN = '2020-01-01T00:00:00.000Z'


class LedgerSmoke(unittest.TestCase):
    def test_no_usage_is_unknown_not_zero(self):
        board = Board(live=True)
        scope = scope_record(board, resource_key='fixture/resource', meter_id='fixture-meter',
                             definition='All fixture attempts, active minutes, cumulative written MiB across Tasks/Runs',
                             author='isolated-fixture', started_at=ORIGIN)
        board.save_budget_record(scope)
        view = ledger_view(board, scope.id)
        self.assertIsNone(view.used)
        self.assertIsNone(view.remaining)
        self.assertFalse(board.runs)
        self.assertFalse(board.decisions)

    def test_stable_resource_key_cannot_reset_in_new_task(self):
        board = Board(live=True)
        scope = scope_record(board, resource_key='fixture/resource', meter_id='fixture-meter',
                             definition='Cumulative fixture counters, no physical quota', author='fixture', started_at=ORIGIN)
        board.save_budget_record(scope)
        board.create('New Task is not a new budget', '收集箱')
        with self.assertRaises(ValueError):
            board.save_budget_record(replace(scope, id=str(uuid4())))
        self.assertEqual(tuple(board.budget_scopes), (scope.id,))

    def test_strict_scope_units_and_bool_version(self):
        board = Board(live=True)
        scope = scope_record(board, resource_key='fixture/resource', meter_id='fixture-meter',
                             definition='Explicit cumulative accounting, not per Run', author='fixture', started_at=ORIGIN)
        for mutation in ({'version': True}, {'units': ['attempts', 'minutes', 'GB']}, {'started_at': 'unknown'}):
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                BudgetScope.from_record(asdict(scope) | mutation)

    def test_receipt_without_saved_n4_evidence_is_refused(self):
        board = Board(live=True)
        scope = scope_record(board, resource_key='fixture/resource', meter_id='fixture-meter',
                             definition='Explicit cumulative accounting, not per Run', author='fixture', started_at=ORIGIN)
        board.save_budget_record(scope)
        with self.assertRaises(ValueError):
            observation_record(board, scope.id, receipt_id='fixture-receipt', meter_id=scope.meter_id,
                               revision=1, used=(1, 2, 3), observed_at=ORIGIN, source_kind='executor_self_report',
                               evidence_item_id=str(uuid4()))
        self.assertFalse(board.usage_observations)
        self.assertIsNotNone(UsageObservation)


class LedgerAdmission(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory(prefix='waggle-n5-ledger-')))
        self.store = self.enterContext(Store(self.root / 'state.db'))
        self.store.load()
        self.board, self.parent, self.owner, report, acceptance = inventory_board()
        self.store.save(self.board)
        self.item = add_report(self.board, self.parent, self.owner, report, acceptance)
        self.store.save(self.board)
        self.n4_check('verified')
        self.scope = scope_record(self.board, resource_key='fixture/cumulative', meter_id='fixture-meter',
                                  definition='All fixture counters since origin across Tasks/Runs/providers; no hardware quota',
                                  author='isolated test author', started_at=ORIGIN)
        self.append(self.scope)

    def append(self, record):
        result = self.board.save_budget_record(record)
        self.store.save(self.board)
        return result

    def n4_check(self, result):
        record = verification_record(self.board, self.item.id, verifier='isolated N4 verifier',
                                     method='Explicit private fixture receipt comparison, not machine attestation',
                                     result=result, limits='Only fixture source', gaps='No measured real usage; fixture only',
                                     next_owner='fixture test', checked_at=now())
        self.board.verify_evidence(self.owner, record)
        self.store.save(self.board)
        return record

    def observe(self, *, receipt='receipt-1', revision=1, used=(2, 10, 20), source_kind='human_receipt'):
        record = observation_record(self.board, self.scope.id, receipt_id=receipt, meter_id=self.scope.meter_id,
                                    revision=revision, used=used, observed_at=now(), source_kind=source_kind,
                                    evidence_item_id=self.item.id)
        return self.append(record)

    def verify(self, observation, **changes):
        values: dict[str, Any] = dict(observation_id=observation.id, result='verified', complete_coverage=True,
                      verifier='explicit fixture human', method='All receipts and idle interval since origin checked',
                      limits='Fixture counters only, unauthenticated identity', gaps='', next_owner='fixture only', checked_at=now())
        return self.append(ledger_verification_record(self.board, self.scope.id, **(values | changes)))

    def grant(self, cap=None):
        return self.append(grant_record(self.board, self.scope.id,
                                       cap=cap or Budget(5, 30, 100, ('Stop at any cap or missing receipt',)),
                                       author='isolated fixture author', reason='Saved-only cumulative fixture grant, no dispatch'))

    def denied(self, candidate):
        old = encode(self.store.load())
        revision = self.store.revision
        disk = (self.root / 'state.db').read_bytes()
        inputs = encode(candidate)
        with self.assertRaises(StorageError):
            self.store.save(candidate)
        self.assertEqual(encode(candidate), inputs)
        self.assertEqual(self.store.revision, revision)
        self.assertEqual(encode(self.store.load()), old)
        self.assertEqual((self.root / 'state.db').read_bytes(), disk)

    def test_saved_grant_is_total_minus_used_and_zero_execution(self):
        before = (deepcopy(self.board.tasks), deepcopy(self.board.runs), deepcopy(self.board.supplements), deepcopy(self.board.decisions))
        observation = self.observe()
        self.verify(observation)
        grant = self.grant()
        view = ledger_view(self.store.load(), self.scope.id)
        self.assertEqual(view.used, (2, 10, 20))
        self.assertEqual(view.remaining, (3, 20, 80))
        self.assertEqual(view.grant_id, grant.id)
        self.assertEqual((self.board.tasks, self.board.runs, self.board.supplements, self.board.decisions), before)

    def test_exact_duplicate_receipt_is_idempotent(self):
        observation = self.observe()
        duplicate = observation_record(self.board, self.scope.id, receipt_id=observation.receipt_id,
                                       meter_id=observation.meter_id, revision=observation.revision, used=observation.used,
                                       observed_at=observation.coverage_end, source_kind=observation.source_kind,
                                       evidence_item_id=observation.evidence_item_id)
        self.assertEqual(duplicate, observation)
        self.assertEqual(self.board.save_budget_record(duplicate), observation)
        self.assertEqual(len(self.board.usage_observations), 1)
        self.verify(observation)
        self.grant()
        self.assertEqual(ledger_view(self.board, self.scope.id).remaining, (3, 20, 80))
        candidate = deepcopy(self.board)
        duplicate = replace(observation, id=str(uuid4()), recorded_at=now())
        candidate.usage_observations[duplicate.id] = duplicate
        self.denied(candidate)

    def test_conflicting_receipt_or_parallel_revision_is_permanently_unknown(self):
        first = self.observe()
        self.verify(first)
        self.grant()
        self.observe(receipt='receipt-1', revision=1, used=(3, 10, 20))
        self.assertIsNone(ledger_view(self.store.load(), self.scope.id).remaining)
        corrected = self.observe(receipt='receipt-new', revision=2, used=(3, 12, 24))
        with self.assertRaises(ValueError):
            self.verify(corrected)
        self.assertEqual(len(self.board.usage_observations), 3)
        with self.assertRaises(ValueError):
            self.grant()

    def test_same_revision_new_source_and_decreasing_counters_fail_closed(self):
        for mutation in ({'receipt': 'other', 'revision': 1, 'used': (2, 10, 20)},
                         {'receipt': 'other', 'revision': 2, 'used': (1, 10, 20)}):
            with self.subTest(mutation=mutation):
                board = deepcopy(self.board)
                first = observation_record(board, self.scope.id, receipt_id='first', meter_id=self.scope.meter_id,
                                           revision=1, used=(2, 10, 20), observed_at=now(), source_kind='human_receipt', evidence_item_id=self.item.id)
                board.save_budget_record(first)
                second = observation_record(board, self.scope.id, receipt_id=mutation['receipt'], meter_id=self.scope.meter_id,
                                            revision=mutation['revision'], used=mutation['used'], observed_at=now(),
                                            source_kind='human_receipt', evidence_item_id=self.item.id)
                board.save_budget_record(second)
                with self.assertRaises(ValueError):
                    ledger_verification_record(board, self.scope.id, observation_id=second.id, result='verified',
                                               complete_coverage=True, verifier='fixture', method='not enough', limits='fixture',
                                               gaps='', next_owner='fixture', checked_at=now())
                self.assertIsNone(ledger_view(board, self.scope.id).used)

    def test_unknown_or_noncovering_sources_never_supply_remaining(self):
        observation = self.observe(source_kind='executor_self_report')
        self.assertIsNone(ledger_view(self.board, self.scope.id).used)
        with self.assertRaises(ValueError):
            self.grant()
        for changes in ({'complete_coverage': False}, {'gaps': 'missing historical usage'}, {'checked_at': ORIGIN}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.verify(observation, **changes)
        self.n4_check('unverified')
        with self.assertRaises(ValueError):
            self.verify(observation)
        self.verify(observation, result='unknown', complete_coverage=False, gaps='Original usage not independently checked')
        self.assertIsNone(ledger_view(self.store.load(), self.scope.id).remaining)

    def test_wrong_units_meter_types_and_bool_counters_are_atomically_denied(self):
        observation = self.observe()
        for changes in ({'units': ('attempts', 'minutes', 'GB')}, {'meter_id': 'concurrent-other-meter'},
                        {'used': (True, 10, 20)}, {'revision': True}, {'coverage_start': now()}):
            candidate = deepcopy(self.board)
            row = replace(observation, id=str(uuid4()), receipt_id='invalid', recorded_at=now(), **changes)
            candidate.usage_observations[row.id] = row
            with self.subTest(changes=changes):
                self.denied(candidate)

    def test_new_observation_requires_new_verification_and_new_explicit_grant(self):
        first = self.observe()
        self.verify(first)
        old = self.grant()
        second = self.observe(receipt='receipt-2', revision=2, used=(3, 12, 24))
        self.assertIsNone(ledger_view(self.board, self.scope.id).remaining)
        self.verify(second)
        self.assertEqual(ledger_view(self.board, self.scope.id).used, (3, 12, 24))
        self.assertIsNone(ledger_view(self.board, self.scope.id).remaining)
        new = self.grant()
        self.assertEqual(new.previous_id, old.id)
        self.assertEqual(ledger_view(self.board, self.scope.id).remaining, (2, 18, 76))

    def test_source_revision_and_restore_cannot_revive_old_grant(self):
        observation = self.observe()
        self.verify(observation)
        old = self.grant()
        self.n4_check('unverified')
        persisted = self.store.load()
        last = list(persisted.ledger_verifications.values())[-1]
        self.assertEqual(last.result, 'invalidated')
        self.assertIsNone(ledger_view(persisted, self.scope.id).remaining)
        self.n4_check('verified')
        self.assertEqual(list(self.store.load().ledger_verifications.values())[-1], last)
        self.assertIsNone(ledger_view(self.board, self.scope.id).remaining)
        self.verify(observation)
        self.assertIsNone(ledger_view(self.board, self.scope.id).remaining)
        new = self.grant()
        self.assertNotEqual(new.id, old.id)
        self.assertEqual(new.previous_id, old.id)
        self.assertEqual(self.board.budget_grants[old.id], old)

    def test_revocation_and_verification_revision_cannot_roll_back(self):
        observation = self.observe()
        self.verify(observation)
        old = self.grant()
        revocation = self.append(revoke_record(self.board, self.scope.id, author='fixture author', reason='Stop cumulative permission'))
        self.assertEqual(revocation.previous_id, old.id)
        self.assertIsNone(ledger_view(self.board, self.scope.id).remaining)
        with self.assertRaises(ValueError):
            revoke_record(self.board, self.scope.id, author='fixture', reason='Cannot double revoke')
        for registry in ('usage_observations', 'ledger_verifications', 'budget_grants', 'budget_scopes'):
            candidate = deepcopy(self.board)
            getattr(candidate, registry).pop(next(iter(getattr(candidate, registry))))
            with self.subTest(registry=registry):
                self.denied(candidate)
        new = self.grant()
        self.assertEqual(new.previous_id, revocation.id)
        self.assertEqual(ledger_view(self.board, self.scope.id).remaining, (3, 20, 80))

    def test_cap_cannot_be_below_used_or_be_remaining_increment(self):
        observation = self.observe()
        self.verify(observation)
        for cap in (Budget(1, 30, 100, ('Stop',)), Budget(5, 9, 100, ('Stop',)), Budget(5, 30, 19, ('Stop',))):
            with self.subTest(cap=cap), self.assertRaises(ValueError):
                self.grant(cap)
        self.grant(Budget(2, 10, 20, ('Nothing further permitted',)))
        self.assertEqual(ledger_view(self.board, self.scope.id).remaining, (0, 0, 0))

    def test_task_creation_reconnect_and_purge_do_not_reset_stable_audit(self):
        observation = self.observe()
        self.verify(observation)
        old = self.grant()
        stable = deepcopy(self.board.budget_scopes)
        new = self.board.create('New Task/provider is not new authority', '收集箱')
        self.store.save(self.board)
        self.assertEqual(ledger_view(self.board, self.scope.id).remaining, (3, 20, 80))
        self.board.retire(new, 'trashed')
        self.store.save(self.board)
        self.board.purge(new)
        self.store.save(self.board)
        self.assertEqual(self.board.budget_scopes, stable)
        self.board.retire(self.owner, 'trashed')
        self.store.save(self.board)
        self.board.purge(self.owner)
        self.store.save(self.board)
        loaded = self.store.load()
        self.assertEqual(loaded.budget_scopes, stable)
        self.assertEqual(loaded.budget_grants[old.id], old)
        self.assertEqual(loaded.usage_observations[observation.id], observation)
        self.assertIsNone(ledger_view(loaded, self.scope.id).remaining)
        with self.assertRaises(ValueError):
            scope_record(loaded, resource_key=self.scope.resource_key, meter_id=self.scope.meter_id,
                         definition='Attempted reset after deleting owner', author='fixture', started_at=now())

    def test_same_transaction_source_proof_or_execution_changes_are_refused(self):
        scope = scope_record(self.board, resource_key='fixture/second', meter_id='fixture-2',
                             definition='Explicit second physical resource, not an alias to clear prior use', author='fixture', started_at=ORIGIN)
        candidate = deepcopy(self.board)
        candidate.save_budget_record(scope)
        candidate.create('Implicit source or execution change', '收集箱')
        self.denied(candidate)
        observation = self.observe()
        verification = ledger_verification_record(self.board, self.scope.id, observation_id=observation.id, result='verified',
                                                  complete_coverage=True, verifier='fixture', method='All receipts', limits='fixture',
                                                  gaps='', next_owner='fixture', checked_at=now())
        candidate = deepcopy(self.board)
        candidate.save_budget_record(verification)
        candidate.save_budget_record(grant_record(candidate, self.scope.id, cap=Budget(5, 30, 100, ('Stop',)),
                                                 author='fixture', reason='Same transaction must fail'))
        self.denied(candidate)

    def test_saved_audit_cannot_rewrite_or_reorder_even_with_identical_caps(self):
        observation = self.observe()
        self.verify(observation)
        old = self.grant()
        self.grant()
        candidate = deepcopy(self.board)
        candidate.budget_grants[old.id] = replace(old, reason='Same cap but forged earlier authority')
        self.denied(candidate)
        candidate = deepcopy(self.board)
        candidate.budget_grants = dict(reversed(list(candidate.budget_grants.items())))
        self.denied(candidate)

    def test_task_round_and_new_agent_session_never_increase_recorded_balance(self):
        observation = self.observe()
        self.verify(observation)
        self.grant()
        self.board.assign(self.owner, str(uuid4()))
        self.store.save(self.board)
        run = self.board.queue_real(self.owner, str(uuid4()), str(uuid4()), self.board.projects[self.owner.project])
        self.store.save(self.board)
        self.assertEqual(ledger_view(self.store.load(), self.scope.id).remaining, (3, 20, 80))
        self.assertEqual(len(self.board.budget_scopes), 1)
        self.assertEqual(len(self.board.usage_observations), 1)
        self.assertEqual(self.board.budget_scopes[self.scope.id], self.scope)
        self.assertNotIn(self.scope.id, run.requirements)  # Local ledger audit is not executable permission.

    def test_exact_retry_at_audit_limit_still_returns_original(self):
        from budget_ledger import MAX_ROWS
        first = self.observe()
        for revision in range(2, MAX_ROWS + 1):
            row = replace(first, id=str(uuid4()), receipt_id=f'capacity-{revision}', revision=revision)
            self.board.save_budget_record(row)
        self.assertEqual(len(self.board.usage_observations), MAX_ROWS)
        retry = observation_record(self.board, self.scope.id, receipt_id=first.receipt_id,
                                   meter_id=first.meter_id, revision=first.revision, used=first.used,
                                   observed_at=first.coverage_end, source_kind=first.source_kind, evidence_item_id=first.evidence_item_id)
        self.assertEqual(retry, first)
        self.assertEqual(self.board.save_budget_record(replace(first, id=str(uuid4()), recorded_at=now())), first)
        self.assertEqual(len(self.board.usage_observations), MAX_ROWS)
        with self.assertRaises(ValueError):
            self.board.save_budget_record(replace(first, id=str(uuid4()), receipt_id='capacity-overflow', revision=MAX_ROWS + 1))

    def test_exact_retry_save_preserves_revision_and_physical_bytes(self):
        observation = self.observe()
        previous = encode(self.store.load())
        revision = self.store.revision
        disk = (self.root / 'state.db').read_bytes()
        retry = observation_record(self.board, self.scope.id, receipt_id=observation.receipt_id,
                                   meter_id=observation.meter_id, revision=observation.revision, used=observation.used,
                                   observed_at=observation.coverage_end, source_kind=observation.source_kind,
                                   evidence_item_id=observation.evidence_item_id)
        self.append(retry)
        self.assertEqual(self.store.revision, revision)
        self.assertEqual(encode(self.store.load()), previous)
        self.assertEqual((self.root / 'state.db').read_bytes(), disk)

    def test_sql_failure_leaves_candidate_and_disk_unchanged(self):
        observation = self.observe()
        self.verify(observation)
        self.grant()
        candidate = deepcopy(self.board)
        new = verification_record(candidate, self.item.id, verifier='fixture', method='Changed source', result='unverified',
                                  limits='fixture', gaps='No verification', next_owner='fixture', checked_at=now())
        candidate.verify_evidence(candidate.tasks[self.owner.id], new)
        self.store.connection.execute('CREATE TRIGGER budget_ignore BEFORE UPDATE ON state BEGIN SELECT RAISE(IGNORE); END')
        self.store.connection.commit()
        self.denied(candidate)
        self.assertEqual(list(candidate.ledger_verifications.values())[-1].result, 'verified')
        self.store.connection.execute('DROP TRIGGER budget_ignore')
        self.store.connection.commit()
        self.store.save(candidate)
        self.assertEqual(list(candidate.ledger_verifications.values())[-1].result, 'invalidated')

    def test_noop_commit_denial_is_not_reported_as_saved_or_revision_advanced(self):
        import sqlite3
        self.observe()
        candidate = deepcopy(self.board)
        def deny_commit(action, arg1, *_):
            return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_TRANSACTION and arg1 == 'COMMIT' else sqlite3.SQLITE_OK
        self.store.connection.set_authorizer(deny_commit)
        try:
            self.denied(candidate)
            self.assertFalse(self.store.connection.in_transaction)
        finally:
            self.store.connection.set_authorizer(None)
        revision = self.store.revision
        self.store.save(candidate)
        self.assertEqual(self.store.revision, revision)

    def test_commit_denial_does_not_mirror_system_invalidations(self):
        import sqlite3
        observation = self.observe()
        self.verify(observation)
        self.grant()
        candidate = deepcopy(self.board)
        record = verification_record(candidate, self.item.id, verifier='fixture', method='Changed independent source', result='unverified',
                                     limits='fixture only', gaps='Source incomplete', next_owner='fixture', checked_at=now())
        candidate.verify_evidence(candidate.tasks[self.owner.id], record)
        def deny_commit(action, arg1, *_):
            return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_TRANSACTION and arg1 == 'COMMIT' else sqlite3.SQLITE_OK
        self.store.connection.set_authorizer(deny_commit)
        try:
            self.denied(candidate)
            self.assertFalse(self.store.connection.in_transaction)
            self.assertEqual(list(candidate.ledger_verifications.values())[-1].result, 'verified')
        finally:
            self.store.connection.set_authorizer(None)
        self.store.save(candidate)
        self.assertEqual(list(candidate.ledger_verifications.values())[-1].result, 'invalidated')

    def test_revision_conflict_is_physical_noop(self):
        second = self.enterContext(Store(self.root / 'state.db'))
        candidate = second.load()
        scope = scope_record(candidate, resource_key='fixture/stale-window', meter_id='fixture-other',
                             definition='Independent declared resource', author='fixture', started_at=ORIGIN)
        candidate.save_budget_record(scope)
        self.board.create('Concurrent harmless saved change', '收集箱')
        self.store.save(self.board)
        before = (self.root / 'state.db').read_bytes()
        inputs = encode(candidate)
        with self.assertRaises(StorageError):
            second.save(candidate)
        self.assertEqual((self.root / 'state.db').read_bytes(), before)
        self.assertEqual(encode(candidate), inputs)


if __name__ == '__main__':
    unittest.main()
