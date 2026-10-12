"""Adversarial Store probes from independent N6 review. Disposable state only."""
import json
import unittest
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from dependencies import dependency_record, now, receipt_record, release_record
from evidence import digest
from store import SCHEMA_VERSION, StorageError, Store, encode
from task_context import WorkContract
from test_n4_inventory import inventory_board
from test_n6_dependencies import attestation, expires
from test_r1_integration import old_database


class DependencyAdmission(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory(prefix='waggle-n6-admission-')))
        self.store = self.enterContext(Store(self.root / 'state.db'))
        self.store.load()
        self.board, self.source, self.owner, _, _ = inventory_board()
        self.receiver = str(uuid4())

    def save_dependency(self):
        self.store.save(self.board)
        dep = dependency_record(self.board, self.owner, self.source, kind='write-conflict', operations=['implementation'],
                                files=['src/shared.py'], condition='明确释放、独立接收；旧绑定不复活')
        self.board.add_dependency(self.owner, dep)
        self.store.save(self.board)
        return dep

    def handover(self):
        self.board.set_work_contract(self.owner, WorkContract('implementation', '原明确改动', 'src/shared.py', '隔离回归', '不改其他文件'))
        dep = self.save_dependency()
        release = release_record(self.board, dep.id, self.receiver, list(dep.files), expires_at=expires(), **attestation())
        self.board.release_ownership(self.owner, release)
        self.store.save(self.board)
        receipt = receipt_record(self.board, release.id, self.receiver, list(dep.files), verifier='独立接收人', method='明确核对范围', checked_at=now())
        self.board.receive_ownership(self.owner, receipt)
        self.store.save(self.board)
        return dep

    def test_retained_inventory_run_cannot_be_rewritten_into_implementation_without_release(self):
        self.save_dependency()
        self.board.assign(self.owner, self.receiver)
        run = self.board.queue_real(self.owner, str(uuid4()), str(uuid4()), self.board.projects[self.owner.project])
        self.store.save(self.board)
        previous, revision = encode(self.store.load()), self.store.revision
        candidate = deepcopy(self.board)
        candidate.runs[run.id].requirements = WorkContract('implementation', '未获释放的改动', 'src/shared.py', '伪造核对', '未知').compile()
        with self.assertRaises(StorageError):
            self.store.save(candidate)
        self.assertEqual(self.store.revision, revision)
        self.assertEqual(encode(self.store.load()), previous)

    def test_accepted_task_cannot_downgrade_to_rewrite_original_scope(self):
        self.store.save(self.board)  # No context/evidence/dependency reference hides the status-only bypass.
        previous, revision = encode(self.store.load()), self.store.revision
        physical = (self.root / 'state.db').read_bytes()
        for status in ('review', 'draft', 'ready', 'blocked', 'running'):
            candidate = deepcopy(self.board)
            candidate.tasks[self.source.id].status = status
            with self.subTest(status=status), self.assertRaisesRegex(StorageError, '已验收'):
                self.store.save(candidate)
            self.assertEqual(self.store.revision, revision)
            self.assertEqual(encode(self.store.load()), previous)
            self.assertEqual((self.root / 'state.db').read_bytes(), physical)
        candidate = deepcopy(self.board)
        candidate.tasks[self.source.id].description += '改写原始已验收目标'
        with self.assertRaisesRegex(StorageError, '已验收'):
            self.store.save(candidate)
        self.assertEqual(encode(self.store.load()), previous)

    def test_accepted_history_can_archive_restore_delete_project_and_purge_without_downgrade(self):
        self.store.save(self.board)
        old_run = deepcopy(asdict(self.board.runs[self.source.run_id]))
        for retention in ('archived', 'trashed'):
            self.board.retire(self.source, retention)
            self.store.save(self.board)
            self.board.restore(self.source)
            self.store.save(self.board)
            persisted = self.store.load()
            self.assertEqual(persisted.tasks[self.source.id].status, 'done')
            self.assertEqual(asdict(persisted.runs[self.source.run_id]), old_run)
        self.board.delete_project(self.source.project)
        self.store.save(self.board)
        self.assertEqual(self.store.load().tasks[self.source.id].status, 'done')
        self.board.purge(self.source)
        self.store.save(self.board)
        self.assertNotIn(self.source.id, self.store.load().tasks)
        self.assertFalse(self.store.load().runs)

    def test_saved_contract_change_then_exact_restore_never_resurrects_old_receipt(self):
        self.handover()
        original = self.owner.remaining
        self.owner.remaining = WorkContract('implementation', '新范围', 'src/other.py', '独立核对', '不沿用旧许可').compile()
        self.store.save(self.board)
        self.owner.remaining = original
        self.store.save(self.board)
        self.assertEqual(sum(r.result == 'invalidated' for r in self.board.dependency_checks.values()), 1)
        with self.assertRaises(ValueError):
            self.board.check_dependencies(self.owner, 'implementation', agent_id=self.receiver)
        with self.assertRaises(ValueError):
            loaded = self.store.load()
            loaded.assign(loaded.tasks[self.owner.id], self.receiver)

    def test_accepted_source_report_raw_rewrite_rejected_without_audit_or_database_change(self):
        self.handover()
        previous = encode(self.store.load())
        physical = (self.root / 'state.db').read_bytes()
        candidate = deepcopy(self.board)
        run = candidate.runs[self.source.run_id]
        run.events[0] = run.events[0].replace('样例方案', '改写旧报告')
        revision = self.store.revision
        with self.assertRaises(StorageError):
            self.store.save(candidate)
        self.assertEqual(self.store.revision, revision)
        self.assertEqual(encode(self.store.load()), previous)
        self.assertEqual((self.root / 'state.db').read_bytes(), physical)
        self.assertFalse(self.board.dependency_checks)

    def test_owner_goal_saved_change_then_restore_never_resurrects_receipt(self):
        dep = self.handover()
        old_goal = self.owner.description
        self.owner.description += '合法未启动草稿的新目标'
        self.store.save(self.board)
        self.owner.description = old_goal
        self.store.save(self.board)
        with self.assertRaisesRegex(ValueError, '永久失效'):
            self.board.assign(self.owner, self.receiver)
        self.assertTrue(any(r.dependency_id == dep.id and r.result == 'invalidated' for r in self.store.load().dependency_checks.values()))

    def test_denial_audit_immutable_failed_disk_does_not_publish_staged_invalidation(self):
        self.handover()
        candidate = deepcopy(self.board)
        candidate.tasks[self.owner.id].remaining = WorkContract('implementation', '另一个范围', 'src/other.py', '核对', '不沿用').compile()
        before = encode(candidate)
        self.store.connection.execute('CREATE TRIGGER deny_update BEFORE UPDATE ON state BEGIN SELECT RAISE(IGNORE); END')
        self.store.connection.commit()
        with self.assertRaises(StorageError):
            self.store.save(candidate)
        self.assertEqual(encode(candidate), before)
        self.assertFalse(self.store.load().dependency_checks)
        self.store.connection.execute('DROP TRIGGER deny_update')
        self.store.connection.commit()
        self.store.save(candidate)
        self.assertTrue(any(r.result == 'invalidated' for r in candidate.dependency_checks.values()))
        original = self.store.load()
        for action in ('delete', 'rewrite'):
            changed = deepcopy(original)
            key, record = next(iter(changed.dependency_checks.items()))
            if action == 'delete':
                del changed.dependency_checks[key]
            else:
                changed.dependency_checks[key] = replace(record, source_digest='0' * 64)
            revision = self.store.revision
            with self.subTest(action=action), self.assertRaises(StorageError):
                self.store.save(changed)
            self.assertEqual(self.store.revision, revision)

    def test_new_declaration_release_and_receipt_required_after_irreversible_change(self):
        old = self.handover()
        old_contract = self.owner.remaining
        self.owner.remaining = WorkContract('implementation', '曾扩大范围', 'src/other.py', '核对', '不沿用').compile()
        self.store.save(self.board)
        self.owner.remaining = old_contract
        self.store.save(self.board)
        with self.assertRaises(ValueError):
            release_record(self.board, old.id, self.receiver, list(old.files), expires_at=expires(), **attestation())
        new = dependency_record(self.board, self.owner, self.source, kind='write-conflict', operations=['implementation'],
                                files=['src/shared.py'], condition='重新明确同一文件范围，旧证明已经失效')
        self.board.add_dependency(self.owner, new)
        self.store.save(self.board)
        release = release_record(self.board, new.id, self.receiver, list(new.files), expires_at=expires(), **attestation())
        self.board.release_ownership(self.owner, release)
        self.store.save(self.board)
        with self.assertRaises(ValueError):
            self.board.assign(self.owner, self.receiver)
        receipt = receipt_record(self.board, release.id, self.receiver, list(new.files), verifier='重新接收者', method='独立重新核对', checked_at=now())
        self.board.receive_ownership(self.owner, receipt)
        self.store.save(self.board)
        self.board.assign(self.owner, self.receiver)
        self.store.save(self.board)
        self.assertNotEqual(new.id, old.id)

    def test_v10_exact_backup_preserves_old_proofs_but_cannot_infer_no_prior_invalidation(self):
        self.handover()
        raw = json.loads(encode(self.board))
        for field in ('budget_scopes', 'usage_observations', 'ledger_verifications', 'budget_grants'):
            raw.pop(field)  # Exact pre-N5 v10 envelope; assertions and retained proof checks unchanged.
        # Precise pre-guard v10 declaration codec; preserve old human proof links, not new permission.
        raw['task_dependencies'][0]['version'] = 1
        raw['ownership_releases'][0]['dependency_digest'] = digest(raw['task_dependencies'][0])
        raw['ownership_receipts'][0]['release_digest'] = digest(raw['ownership_releases'][0])
        original = json.dumps(raw, ensure_ascii=False, indent=2)
        path = self.root / 'v10.db'
        old_database(path, 10, original)
        with Store(path) as store:
            board = store.load()
            self.assertEqual(store.connection.execute('PRAGMA user_version').fetchone()[0], SCHEMA_VERSION)
            self.assertEqual(store.connection.execute('SELECT revision,data FROM legacy_v10').fetchone(), (9, original))
            self.assertEqual(asdict(next(iter(board.ownership_releases.values()))),
                             {**raw['ownership_releases'][0], 'files': tuple(raw['ownership_releases'][0]['files'])})
            self.assertEqual(board.runs[self.source.run_id].events, self.board.runs[self.source.run_id].events)
            self.assertFalse(board.dependency_checks)  # No guessed invalidation or new permission.
            with self.assertRaisesRegex(ValueError, '旧版本'):
                board.assign(board.tasks[self.owner.id], self.receiver)
        raw['task_dependencies'][0]['source_task_id'] = True
        path = self.root / 'corrupt-v10.db'
        old_database(path, 10, json.dumps(raw, ensure_ascii=False))
        before = path.read_bytes()
        with self.assertRaises(StorageError):
            Store(path)
        self.assertEqual(path.read_bytes(), before)
