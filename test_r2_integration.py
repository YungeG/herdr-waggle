"""R2 current-source integration; all databases, Pi processes and artifacts disposable."""
import hashlib
import json
import shutil
import sqlite3
import tarfile
import unittest
from contextlib import closing
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app import Waggle
from dependencies import dependency_record
from evidence import inventory_text
from release import ROOT, build_package, source_files
from runtime import read_json
from store import SCHEMA_VERSION, StorageError, Store, encode
from task_context import WorkContract
from test_bridge import PiFixture
from test_n4_inventory import add_report, inventory_board
from test_r1_integration import legacy_payload, old_database

LEGACY_BACKUP_QUERIES = (
    'SELECT revision,data FROM legacy_v1',
    'SELECT revision,data FROM legacy_v2',
    'SELECT revision,data FROM legacy_v3',
    'SELECT revision,data FROM legacy_v4',
    'SELECT revision,data FROM legacy_v5',
    'SELECT revision,data FROM legacy_v6',
    'SELECT revision,data FROM legacy_v7',
    'SELECT revision,data FROM legacy_v8',
    'SELECT revision,data FROM legacy_v9',
    'SELECT revision,data FROM legacy_v10',
)


def r2_legacy_payload(version):
    original, reports = legacy_payload(min(version, 7))
    if version >= 8:
        raw = json.loads(original)
        raw['task_contexts'] = []
        if version >= 9:
            raw.update(evidence_items=[], evidence_verifications=[])
        if version >= 10:
            raw.update(task_dependencies=[], dependency_checks=[], ownership_releases=[], ownership_receipts=[])
        original = json.dumps(raw, ensure_ascii=False, indent=2)
    return original, reports


class R2Migration(unittest.TestCase):
    def test_versions1_to10_keep_exact_backup_revision_and_raw_reports_without_guesses(self):
        with TemporaryDirectory(prefix='waggle-r2-migration-') as temporary:
            for version in range(1, 11):
                with self.subTest(version=version):
                    original, reports = r2_legacy_payload(version)
                    path = Path(temporary) / f'v{version}.db'
                    old_database(path, version, original)
                    with Store(path) as store:
                        board = store.load()
                        self.assertEqual(store.connection.execute('PRAGMA user_version').fetchone()[0], SCHEMA_VERSION)
                        self.assertEqual(store.revision, 9)
                        self.assertEqual(store.connection.execute(LEGACY_BACKUP_QUERIES[version - 1]).fetchall(), [(9, original)])
                        for name in ('task_contexts', 'evidence_items', 'evidence_verifications', 'task_dependencies',
                                     'dependency_checks', 'ownership_releases', 'ownership_receipts'):
                            self.assertFalse(getattr(board, name))
                        if version > 1:
                            self.assertEqual(tuple(next(iter(board.runs.values())).events), reports)
                        else:
                            self.assertFalse(board.runs)
                        store.save(board)
                    with Store(path) as store:
                        self.assertEqual(store.connection.execute(LEGACY_BACKUP_QUERIES[version - 1]).fetchall(), [(9, original)])

    def test_versions1_to10_corrupt_or_update_ignore_migration_preserve_physical_bytes(self):
        with TemporaryDirectory(prefix='waggle-r2-migration-denied-') as temporary:
            for version in range(1, 11):
                for denied in (False, True):
                    with self.subTest(version=version, denied=denied):
                        original, _ = r2_legacy_payload(version)
                        if not denied:
                            original = '{"unknown": true}'
                        path = Path(temporary) / f'v{version}-{denied}.db'
                        old_database(path, version, original, deny=denied)
                        before = path.read_bytes()
                        with self.assertRaises(StorageError):
                            Store(path)
                        self.assertEqual(path.read_bytes(), before)
                        with closing(sqlite3.connect(path)) as connection:
                            self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], version)
                            self.assertEqual(connection.execute('SELECT revision,data FROM state').fetchall(), [(9, original)])
                            self.assertFalse(connection.execute('SELECT name FROM sqlite_master WHERE name=?', (f'legacy_v{version}',)).fetchall())

    def test_conflicting_revision_preserves_committed_source_and_caller(self):
        with TemporaryDirectory(prefix='waggle-r2-conflict-') as temporary:
            path = Path(temporary) / 'state.db'
            with Store(path) as first, Store(path) as stale:
                board, outdated = first.load(), stale.load()
                board.create('committed fixture', '收集箱')
                first.save(board)
                outdated.create('stale fixture', '收集箱')
                caller, physical = encode(outdated), path.read_bytes()
                with self.assertRaisesRegex(StorageError, '另一窗口'):
                    stale.save(outdated)
                self.assertEqual(encode(outdated), caller)
                self.assertEqual(path.read_bytes(), physical)
                self.assertEqual(encode(first.load()), encode(board))


class R2SourcePackage(unittest.TestCase):
    def test_current_modules_all_selected_bytes_hashes_and_no_private_state(self):
        selected = source_files(ROOT)
        required = {'evidence.py', 'evidence_view.py', 'dependencies.py', 'dependency_view.py',
                    'task_context.py', 'task_actions.py', 'store.py', 'model.py', 'decisions.py', 'app.py', 'style.tcss'}
        self.assertTrue(required.issubset(selected))
        with TemporaryDirectory(prefix='waggle-r2-package-') as temporary:
            archive = build_package(ROOT, Path(temporary))
            with tarfile.open(archive) as bundle:
                payload = {}
                for member in bundle.getmembers():
                    self.assertTrue(member.isfile())
                    stream = bundle.extractfile(member)
                    assert stream is not None
                    with stream:
                        payload[member.name.removeprefix('waggle/')] = stream.read()
            manifest = json.loads(payload['SOURCE-MANIFEST.json'])
            self.assertEqual(manifest['tests'], 'not_run')  # Archive execution is not claimed.
            self.assertEqual(set(manifest['source_files']), set(selected))
            for name in selected:
                self.assertEqual(payload[name], (ROOT / name).read_bytes())
                self.assertEqual(hashlib.sha256(payload[name]).hexdigest(), manifest['files'][name])
            self.assertFalse(any(name.endswith(('.db', '.sqlite3')) or name.startswith(('sessions/', 'bridges/', '.local/'))
                                 or name in {'auth.json', '.env'} for name in payload))


@unittest.skipUnless(shutil.which('pi'), 'installed Pi required')
class R2ActualOffline(unittest.IsolatedAsyncioTestCase):
    async def inventory_roundtrip(self, *, fabric):
        with TemporaryDirectory(prefix='waggle-r2-offline-') as temporary:
            root = Path(temporary)
            fixture = PiFixture(root)
            try:
                await fixture.start(fabric=fabric)
                agent = await fixture.connect()
                assert agent is not None
                proof = read_json(fixture.runtime.endpoint(agent.id) / 'report-capabilities.json')
                self.assertEqual((proof['version'], proof['transport'], proof['ref']),
                                 (2, 'fabric' if fabric else 'native', 'extensions.waggle_report' if fabric else 'waggle_report'))
                self.assertTrue(proof['callable'])
                with Store(root / 'tasks.db') as store:
                    store.load()
                    board, source, owner, report, acceptance = inventory_board()
                    board.projects[source.project] = str(fixture.project)
                    board.runs[source.run_id].cwd = str(fixture.project)
                    owner.title = 'INVENTORY_R2'
                    store.save(board)
                    item = add_report(board, source, owner, report, acceptance)
                    store.save(board)
                    dep = dependency_record(board, owner, source, kind='write-conflict', operations=['implementation'],
                                            files=['src/shared.py'], condition='旧写者未释放；只阻塞对应写入，不阻塞盘点')
                    board.add_dependency(owner, dep)
                    store.save(board)
                    self.assertIn('未核实', inventory_text(board, owner))
                    self.assertFalse(board.ownership_releases)
                    self.assertFalse(board.ownership_receipts)
                    old_source, old_run = deepcopy(asdict(source)), deepcopy(asdict(board.runs[source.run_id]))
                    board.assign(owner, agent.id)
                    store.save(board)
                    writer = board.create('unreleased shared writer', source.project, '仅验证未释放路径的拒绝')
                    board.set_work_contract(writer, WorkContract('implementation', '共享文件改动', 'src/shared.py', '隔离核对', '没有释放就不执行'))
                    store.save(board)
                    blocked = dependency_record(board, writer, source, kind='write-conflict', operations=['implementation'],
                                                files=['src/shared.py'], condition='必须明确释放并独立接收')
                    board.add_dependency(writer, blocked)
                    store.save(board)
                    app = Waggle(store=store, runtime=fixture.runtime)
                    with patch.object(app, 'schedule_poll'):
                        async with app.run_test(size=(80, 24)):
                            before = encode(store.load())
                            with patch.object(fixture.runtime, 'check_dispatch') as preflight:
                                with self.assertRaises(ValueError):
                                    app.apply_change(lambda candidate: candidate.assign(candidate.tasks[writer.id], agent.id))
                                preflight.assert_not_called()
                            self.assertEqual(encode(store.load()), before)
                            current = app.board.tasks[owner.id]
                            expected = app.board.compiled_requirements(current)
                            with patch.object(fixture.runtime, 'dispatch', wraps=fixture.runtime.dispatch) as dispatch:
                                self.assertTrue(await app.dispatch_task(deepcopy(current), agent, expected_requirements=expected))
                                self.assertEqual(dispatch.call_count, 1)
                            run = app.board.runs[app.board.tasks[owner.id].run_id]
                            self.assertEqual(run.requirements, expected)
                            for anchor in (report.id, acceptance.id, item.source_digest, dep.id):
                                self.assertIn(anchor, expected)
                            result = await fixture.result(run)
                            self.assertEqual((result.state, result.delivery, result.requirements), ('finished', 'full', expected))
                            response = await fixture.request('get_messages')
                            self.assertTrue(response['success'], response)
                            messages = response['data']['messages']
                            task_messages = [message for message in messages if message['role'] == 'custom'
                                and message.get('customType') == 'waggle-task' and message.get('details', {}).get('runId') == run.id]
                            self.assertEqual(len(task_messages), 1)
                            self.assertIn(expected, task_messages[0]['content'])
                            # Independent digest of the exact model-context request, not just the saved Run.
                            self.assertIn('Fixture received request sha256=' + hashlib.sha256(expected.encode()).hexdigest(), result.checks)
                            self.assertFalse(any(message.get('toolName') == 'waggle_fixture_effect' for message in messages))
                            self.assertNotIn('extensions.waggle_fixture_effect', json.dumps(messages, ensure_ascii=False))
                            self.assertFalse((fixture.project / 'fixture-result.json').exists())
                            app.apply_change(lambda candidate: candidate.ingest(result))
                            persisted = store.load()
                            self.assertEqual(asdict(persisted.tasks[source.id]), old_source)
                            self.assertEqual(asdict(persisted.runs[source.run_id]), old_run)
                            self.assertEqual(len(persisted.runs), 2)
                            self.assertEqual(persisted.tasks[owner.id].status, 'review')
                            self.assertFalse(persisted.ownership_releases)
                            self.assertFalse(persisted.ownership_receipts)
            finally:
                await fixture.close()

    async def test_native_pi_receives_exact_n4_n6_quotes_without_inventory_write_or_implicit_release(self):
        await self.inventory_roundtrip(fabric=False)

    async def test_fabric_pi_receives_exact_n4_n6_quotes_without_inventory_write_or_implicit_release(self):
        await self.inventory_roundtrip(fabric=True)
