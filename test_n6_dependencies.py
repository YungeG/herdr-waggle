"""N6 operation-scoped dependency sentinels; no live state or writer takeover."""
import json
import unittest
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from dependencies import (
    DependencyCheck,
    OwnershipReceipt,
    OwnershipRelease,
    TaskDependency,
    check_record,
    dependency_record,
    dependency_text,
    now,
    receipt_record,
    release_record,
)
from evidence import verification_record
from store import SCHEMA_VERSION, StorageError, Store, decode, encode
from task_context import WorkContract
from test_n4_inventory import add_report, inventory_board
from test_r1_integration import old_database


class DependencySentinels(unittest.TestCase):
    def write_dependency(self, board, owner, source):
        from dependencies import dependency_record
        record = dependency_record(board, owner, source, kind='write-conflict', operations=['implementation'],
                                   files=['src/shared.py'], condition='原写者明确释放且接续者独立接收精确文件范围')
        board.add_dependency(owner, record)
        return record

    def test_inventory_not_blocked_by_unrelated_implementation_writer_dependency(self):
        board, source, owner, _, _ = inventory_board()
        self.write_dependency(board, owner, source)
        board.check_dependencies(owner, 'inventory')
        board.assign(owner, str(uuid4()))
        preview = board.compiled_requirements(owner)
        run = board.queue_real(owner, str(uuid4()), str(uuid4()), board.projects[owner.project])
        self.assertEqual(run.requirements, preview)
        self.assertNotEqual(run.task_id, source.id)

    def test_accepted_or_offline_old_task_is_not_release_or_receiver_acceptance(self):
        board, source, owner, _, _ = inventory_board()
        board.set_work_contract(owner, WorkContract('implementation', '改动明确文件', 'src/shared.py', '隔离回归', '不接管其他文件'))
        self.write_dependency(board, owner, source)
        self.assertEqual(source.status, 'done')
        self.assertTrue(board.runs[source.run_id].terminal)
        before = encode(board)
        with self.assertRaises(ValueError):
            board.assign(owner, str(uuid4()))
        self.assertEqual(encode(board), before)
        with self.assertRaises(ValueError):
            board.check_dependencies(owner, 'implementation')
        self.assertEqual(encode(board), before)

    def test_arbitrary_readonly_or_evidence_prefix_never_designates_shared_writer(self):
        from decisions import EVIDENCE_ONLY_PREFIX
        for text in ('只读盘点，不改文件', EVIDENCE_ONLY_PREFIX + '自由文字冒充只读'):
            with self.subTest(text=text):
                board, source, owner, _, _ = inventory_board()
                owner.remaining = text
                self.write_dependency(board, owner, source)
                before = encode(board)
                with self.assertRaises(ValueError):
                    board.assign(owner, str(uuid4()))
                self.assertEqual(encode(board), before)

    def test_cycle_rejects_atomically_without_burning_registry_identity(self):
        from dependencies import dependency_record
        board, source, owner, _, _ = inventory_board()
        self.write_dependency(board, owner, source)
        before = encode(board)
        reverse = dependency_record(board, source, owner, kind='contract', operations=['implementation'],
                                    files=[], condition='需先核对后续接口')
        with self.assertRaises(ValueError):
            board.add_dependency(source, reverse)
        self.assertEqual(encode(board), before)


def expires():
    return (datetime.now(UTC) + timedelta(minutes=5)).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def attestation():
    return dict(verifier='明确人工核对者（未认证）', method='核对原写者提供的精确材料', limits='仅明确文件范围',
                gaps='平台未探测文件和人工身份', next_owner='已明确接续者；超范围重新核对', checked_at=now())


class DependencyIntegration(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory(prefix='waggle-n6-')))
        self.store = self.enterContext(Store(self.root / 'state.db'))
        self.store.load()
        self.board, self.source, self.owner, self.report, self.acceptance = inventory_board()
        self.board.set_work_contract(self.owner, WorkContract('implementation', '明确文件改动', 'src/shared.py', '隔离回归', '不接管其他文件'))
        self.receiver = str(uuid4())
        self.store.save(self.board)

    def add(self, kind='write-conflict', operations=None, files=None, proof=None):
        dep = dependency_record(self.board, self.owner, self.source, kind=kind,
                                operations=operations or ['implementation'], files=files if files is not None else ['src/shared.py'] if kind == 'write-conflict' else [],
                                condition='只核对明确条件；共享写者须单独释放和接收', proof=proof)
        self.board.add_dependency(self.owner, dep)
        self.store.save(self.board)
        return dep

    def release(self, dep):
        record = release_record(self.board, dep.id, self.receiver, list(dep.files), expires_at=expires(), **attestation())
        self.board.release_ownership(self.owner, record)
        self.store.save(self.board)
        return record

    def receive(self, release):
        record = receipt_record(self.board, release.id, self.receiver, list(release.files), verifier='独立接收人（未认证）',
                                method='单独核对原写者释放、文件定位和接续者', checked_at=now())
        self.board.receive_ownership(self.owner, record)
        self.store.save(self.board)
        return record

    def handover(self):
        dep = self.add()
        release = self.release(dep)
        receipt = self.receive(release)
        return dep, release, receipt

    def test_release_alone_denies_and_separate_persisted_receipt_enables_exact_receiver_one_run(self):
        source_before = deepcopy(self.source), deepcopy(self.board.runs[self.source.run_id])
        dep, release = self.add(), None
        release = self.release(dep)
        before = encode(self.board)
        with self.assertRaisesRegex(ValueError, '尚未交接'):
            self.board.assign(self.owner, self.receiver)
        self.assertEqual(encode(self.board), before)
        self.assertIn('尚未交接', dependency_text(self.board, self.owner, durable=True))
        receipt = self.receive(release)
        with self.assertRaises(ValueError):
            self.board.assign(self.owner, str(uuid4()))
        self.board.assign(self.owner, self.receiver)
        self.store.save(self.board)
        preview = self.board.compiled_requirements(self.owner)
        run = self.board.queue_real(self.owner, str(uuid4()), str(uuid4()), self.board.projects[self.owner.project])
        self.store.save(self.board)
        self.assertEqual(run.requirements, preview)
        for key in (dep.id, release.id, receipt.id, release.source_run_id, release.source_session_id):
            self.assertIn(key, run.requirements)
        self.assertEqual((self.source, self.board.runs[self.source.run_id]), source_before)
        self.assertEqual(self.store.load().runs[run.id].requirements, preview)

    def test_expiry_stays_readable_but_fails_closed_and_unpersisted_never_handed_over(self):
        _, release, _ = self.handover()
        with patch('dependencies.now', return_value=release.expires_at):
            loaded = decode(encode(self.board))
            with self.assertRaises(ValueError):
                loaded.check_dependencies(loaded.tasks[self.owner.id], 'implementation')
            self.assertIn('待核对', dependency_text(loaded, loaded.tasks[self.owner.id], durable=True))
        self.assertIn('尚未交接', dependency_text(self.board, self.owner, durable=False))

    def test_raw_source_goal_contract_directory_and_file_expansion_require_renewal(self):
        dep, _, _ = self.handover()
        source_run = self.board.runs[self.source.run_id]
        raw = source_run.events[0]
        source_run.events[0] = raw.replace('样例方案', '漂移方案')
        with self.assertRaises(ValueError):
            self.board.check_dependencies(self.owner, 'implementation')
        source_run.events[0] = raw
        self.source.description += '改变目标'
        with self.assertRaises(ValueError):
            self.board.check_dependencies(self.owner, 'implementation')
        self.source.description = source_run.description
        directory = self.board.projects[self.source.project]
        self.board.projects[self.source.project] += '/changed'
        with self.assertRaises(ValueError):
            self.board.check_dependencies(self.owner, 'implementation')
        self.board.projects[self.source.project] = directory
        self.board.set_work_contract(self.owner, WorkContract('implementation', '更多文件改动', 'src/shared.py 与 src/more.py', '独立核对', '不接管其他文件'))
        self.store.save(self.board)  # Pending input may be saved; not implicitly authorized.
        with self.assertRaises(ValueError):
            self.board.assign(self.owner, self.receiver)
        replacement = self.add(files=['src/shared.py', 'src/more.py'])
        self.assertNotEqual(replacement.id, dep.id)
        with self.assertRaisesRegex(ValueError, '缺明确原写者释放'):
            self.board.assign(self.owner, self.receiver)

    def test_strict_types_and_exact_file_locators_before_registry_lookup(self):
        dep = self.add()
        for key, value in [('version', True), ('owner_task_id', True), ('source_task_id', False), ('id', ''),
                           ('owner_scope_digest', 'x'), ('operations', [True]), ('created_at', 'today'),
                           ('files', ['../shared.py']), ('files', ['/tmp/shared.py']), ('files', ['src/*']),
                           ('files', ['src//shared.py']), ('files', ['src/shared.py', 'src/shared.py'])]:
            with self.subTest(key=key, value=value), self.assertRaises((ValueError, TypeError)):
                TaskDependency.from_record({**asdict(dep), key: value})
        release = self.release(dep)
        for cls, record, key in [(OwnershipRelease, release, 'receiver_agent_id'), (OwnershipRelease, release, 'source_run_id')]:
            with self.assertRaises(ValueError):
                cls.from_record({**asdict(record), key: True})
        receipt = self.receive(release)
        with self.assertRaises(ValueError):
            OwnershipReceipt.from_record({**asdict(receipt), 'files': ['src/other.py', '..']})
        with self.assertRaises(ValueError):
            self.board.release_ownership(self.owner, replace(release, id=str(uuid4()), files=('src/other.py',)))

    def test_contract_condition_not_inferred_from_done_and_latest_blocked_check_wins(self):
        dep = self.add(kind='contract', operations=['inventory'])
        with self.assertRaises(ValueError):
            self.board.check_dependencies(self.owner, 'inventory')
        check = check_record(self.board, dep.id, result='satisfied', **attestation())
        self.board.check_dependency(self.owner, check)
        self.store.save(self.board)
        self.board.check_dependencies(self.owner, 'inventory')
        blocked = check_record(self.board, dep.id, result='blocked', **attestation())
        self.board.check_dependency(self.owner, blocked)
        self.store.save(self.board)
        with self.assertRaises(ValueError):
            self.board.check_dependencies(self.owner, 'inventory')
        with self.assertRaises(ValueError):
            DependencyCheck.from_record({**asdict(check), 'result': True})

    def test_evidence_proof_level_requires_explicit_saved_item_and_latest_human_original_check(self):
        item = add_report(self.board, self.source, self.owner, self.report, self.acceptance)
        self.store.save(self.board)
        dep = self.add(kind='evidence', operations=['evidence'], proof='human_original_verified')
        with self.assertRaisesRegex(ValueError, '原件'):
            check_record(self.board, dep.id, evidence_item_id=item.id, result='satisfied', **attestation())
        verification = verification_record(self.board, item.id, verifier='明确核验人', method='查看原件并比对来源',
                                           result='verified', limits='仅样例范围', gaps='工具层无文件探测', next_owner='接续者重新核对新范围', checked_at=now())
        self.board.verify_evidence(self.owner, verification)
        self.store.save(self.board)
        check = check_record(self.board, dep.id, evidence_item_id=item.id, result='satisfied', **attestation())
        self.board.check_dependency(self.owner, check)
        self.store.save(self.board)
        self.board.check_dependencies(self.owner, 'evidence')
        conflict = verification_record(self.board, item.id, verifier='另一核验人', method='发现原件冲突', result='conflict',
                                       limits='原范围', gaps='需要重新核对原件', next_owner='原件保管人身份未知', checked_at=now())
        self.board.verify_evidence(self.owner, conflict)
        self.store.save(self.board)
        self.assertTrue(any(r.dependency_id == dep.id and r.result == 'invalidated' for r in self.board.dependency_checks.values()))
        with self.assertRaisesRegex(ValueError, '永久失效'):
            self.board.check_dependencies(self.owner, 'evidence')

    def test_release_receipt_cannot_be_co_saved_and_store_rejects_direct_assignment_bypass(self):
        dep = self.add()
        candidate = deepcopy(self.board)
        owner = candidate.tasks[self.owner.id]
        release = release_record(candidate, dep.id, self.receiver, list(dep.files), expires_at=expires(), **attestation())
        candidate.release_ownership(owner, release)
        receipt = receipt_record(candidate, release.id, self.receiver, list(release.files), verifier='接续人', method='独立核对', checked_at=now())
        candidate.receive_ownership(owner, receipt)
        with self.assertRaisesRegex(StorageError, '分开持久化'):
            self.store.save(candidate)
        loaded = self.store.load()
        self.assertFalse(loaded.ownership_releases)
        self.assertFalse(loaded.ownership_receipts)
        loaded.tasks[self.owner.id].agent, loaded.tasks[self.owner.id].status = self.receiver, 'ready'
        with self.assertRaises(StorageError):
            self.store.save(loaded)
        self.assertFalse(self.store.load().tasks[self.owner.id].agent)

    def test_audit_rewrite_delete_reorder_and_new_release_cannot_resurrect_old_receipt(self):
        dep, release, _ = self.handover()
        new_release = self.release(dep)
        with self.assertRaisesRegex(ValueError, '尚未交接'):
            self.board.assign(self.owner, self.receiver)
        for mutation in ('rewrite', 'delete', 'reorder'):
            candidate = deepcopy(self.board)
            if mutation == 'rewrite':
                candidate.ownership_releases[release.id] = replace(release, method='篡改核对方法')
            elif mutation == 'delete':
                del candidate.ownership_releases[new_release.id]
            else:
                candidate.ownership_releases = dict(reversed(list(candidate.ownership_releases.items())))
            revision = self.store.revision
            with self.subTest(mutation=mutation), self.assertRaises(StorageError):
                self.store.save(candidate)
            self.assertEqual(self.store.revision, revision)
        self.receive(new_release)
        self.board.check_dependencies(self.owner, 'implementation', agent_id=self.receiver)

    def test_source_purge_and_project_deletion_guard_owner_cleanup(self):
        self.handover()
        with self.assertRaises(ValueError):
            self.board.delete_project(self.source.project)
        self.board.retire(self.source, 'trashed')
        self.store.save(self.board)
        with self.assertRaises(ValueError):
            self.board.purge(self.source)
        self.board.retire(self.owner, 'trashed')
        self.store.save(self.board)
        self.board.purge(self.owner)
        self.store.save(self.board)
        self.assertFalse(self.board.task_dependencies)
        self.assertFalse(self.board.ownership_releases)
        self.assertFalse(self.board.ownership_receipts)
        self.board.purge(self.source)
        self.store.save(self.board)

    def test_historical_quote_prefix_revalidated_exactly_and_new_audit_regenerated_once(self):
        from task_context import DEPENDENCY_MARKER, source_base
        dep, _, _ = self.handover()
        self.board.assign(self.owner, self.receiver)
        run = self.board.queue_real(self.owner, str(uuid4()), str(uuid4()), self.board.projects[self.owner.project])
        old = run.requirements
        self.store.save(self.board)
        run.state = 'finished'
        self.owner.run, self.owner.status = 'finished', 'review'
        self.store.save(self.board)
        release = self.release(dep)
        receipt = self.receive(release)
        inherited = self.board.inherited_requirements(self.owner, run)
        compiled = self.board.compiled_requirements(self.owner, inherited)
        self.assertEqual(compiled.count(DEPENDENCY_MARKER), 1)
        self.assertIn(receipt.id, compiled)
        self.assertEqual(run.requirements, old)
        run.requirements = old.replace(dep.condition, '伪造条件')
        with self.assertRaises(ValueError):
            self.board.inherited_requirements(self.owner, run)
        run.requirements = old
        with self.assertRaises(ValueError):
            self.board.compiled_requirements(self.owner, source_base(old) + DEPENDENCY_MARKER + '{}')

    def test_genuine_confirmed_structured_feedback_can_new_run_without_unrelated_writer_release(self):
        from decisions import Answer, Target, prepare_decision_intent, preview_decisions
        from test_decisions import decision_board
        board, task, old = decision_board()
        source = board.create('明确其他写者任务', task.project, '共享文件实施来源，尚无释放')
        with Store(self.root / 'feedback.db') as store:
            store.load()
            store.save(board)
            dep = dependency_record(board, task, source, kind='write-conflict', operations=['implementation'],
                                    files=['src/shared.py'], condition='只阻塞实际实施，补证不接管写者')
            board.add_dependency(task, dep)
            store.save(board)
            preview = preview_decisions(board, task.id, (Answer('request_info'),), Target('new_run', old.agent_id, old.session_id, old.cwd))
            prepared = prepare_decision_intent(board, preview)
            store.save(board)
            assert prepared.run is not None
            self.assertEqual(prepared.run.requirements, preview.compiled_text)
            self.assertTrue(prepared.run.requirements.startswith('仅补证请求：'))
            self.assertFalse(board.ownership_releases)
            self.assertEqual(store.load().runs[prepared.run.id].requirements, preview.compiled_text)

    def test_v9_migration_exact_evidence_backup_and_corrupt_rollback_no_guessed_dependency(self):
        item = add_report(self.board, self.source, self.owner, self.report, self.acceptance)
        raw = json.loads(encode(self.board))
        for name in ('task_dependencies', 'dependency_checks', 'ownership_releases', 'ownership_receipts',
                     'budget_scopes', 'usage_observations', 'ledger_verifications', 'budget_grants'):
            raw.pop(name)  # Exact v9 envelope; retain the original backup / corruption assertions.
        original = json.dumps(raw, ensure_ascii=False, indent=2)
        path = self.root / 'legacy9.db'
        old_database(path, 9, original)
        with Store(path) as store:
            loaded = store.load()
            self.assertEqual(store.connection.execute('PRAGMA user_version').fetchone()[0], SCHEMA_VERSION)
            self.assertEqual(store.connection.execute('SELECT revision, data FROM legacy_v9').fetchone(), (9, original))
            self.assertEqual(loaded.evidence_items[item.id], item)
            self.assertFalse(loaded.task_dependencies)
            self.assertEqual(loaded.runs[self.source.run_id], self.board.runs[self.source.run_id])
        raw['evidence_items'][0]['owner_task_id'] = True
        path = self.root / 'bad9.db'
        old_database(path, 9, json.dumps(raw, ensure_ascii=False))
        before = path.read_bytes()
        with self.assertRaises(StorageError):
            Store(path)
        self.assertEqual(path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
