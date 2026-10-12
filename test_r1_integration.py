"""Release integration against exact disposable legacy bytes and current source."""
import hashlib
import json
import sqlite3
import tarfile
import unittest
from contextlib import closing
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory

from journal import PREFIX, Report, encoded_event
from model import sample_board
from release import ROOT, build_package, source_files
from store import APPLICATION_ID, SCHEMA_VERSION, StorageError, Store, encode
from test_runtime import prepared_board


def legacy_payload(version):
    if version == 1:
        rows = [{key: value for key, value in asdict(task).items()
                 if key not in {'execution', 'run_id', 'retention', 'steps'}} for task in sample_board().tasks.values()]
        return json.dumps(rows, ensure_ascii=False, indent=2), ()
    board, _, source = prepared_board('/virtual/disposable-legacy')
    final = deepcopy(source)
    final.state, final.delivery, final.seq = 'finished', 'partial', 2
    final.summary, final.checks, final.remaining = '旧原文，不是验收', ['现有回执'], '仍缺原件'
    raw = encoded_event('partial', 'Exact legacy report · 中文\nNo new authority', source='executor',
                        run_id=final.id, seq=1, report=Report(final.summary, final.checks, final.remaining))
    # Noncanonical spacing/order is still original source data and must survive exactly.
    final.events = [PREFIX + json.dumps(json.loads(raw[len(PREFIX):]), ensure_ascii=False, indent=2)]
    board.ingest(final)
    payload = json.loads(encode(board))
    names = {'live', 'projects', 'tasks', 'runs'}
    if version >= 4:
        names.add('next_task_id')
    if version >= 5:
        names.add('supplements')
    if version >= 7:
        names.add('decisions')
    payload = {key: value for key, value in payload.items() if key in names}
    for task in payload['tasks']:
        if version <= 2:
            task.pop('retention')
        if version <= 5:
            task.pop('steps')
    return json.dumps(payload, ensure_ascii=False, indent=2), tuple(final.events)


def old_database(path, version, original, *, deny=False):
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute('CREATE TABLE state (singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision INTEGER NOT NULL, data TEXT NOT NULL)')
        connection.execute('INSERT INTO state VALUES (1, 9, ?)', (original,))
        connection.execute(f'PRAGMA application_id={APPLICATION_ID}')
        connection.execute(f'PRAGMA user_version={version}')
        if deny:
            connection.execute('CREATE TRIGGER refuse_update BEFORE UPDATE ON state BEGIN SELECT RAISE(IGNORE); END')


class LegacyMatrix(unittest.TestCase):
    def test_every_v1_to_v7_has_exact_backup_and_no_invented_context(self):
        with TemporaryDirectory(prefix='waggle-r1-migrations-') as temporary:
            for version in range(1, 8):
                with self.subTest(version=version):
                    original, report_bytes = legacy_payload(version)
                    path = Path(temporary) / f'v{version}.sqlite3'
                    old_database(path, version, original)
                    with Store(path) as store:
                        board = store.load()
                        self.assertEqual(store.connection.execute('PRAGMA user_version').fetchone()[0], SCHEMA_VERSION)
                        self.assertEqual(store.revision, 9)
                        self.assertEqual(store.connection.execute(f'SELECT revision,data FROM legacy_v{version}').fetchall(), [(9, original)])
                        self.assertFalse(board.task_contexts)
                        self.assertFalse(board.decisions)
                        if version == 1:
                            self.assertTrue(all(task.execution == 'simulation' for task in board.tasks.values()))
                            self.assertFalse(board.runs)
                        else:
                            self.assertEqual(tuple(next(iter(board.runs.values())).events), report_bytes)
                            self.assertFalse(next(iter(board.tasks.values())).can_accept)
                        store.save(board)
                    with Store(path) as store:
                        self.assertEqual(store.connection.execute(f'SELECT data FROM legacy_v{version}').fetchone()[0], original)
                        self.assertFalse(store.load().task_contexts)

    def test_all_legacy_corrupt_or_rejected_migrations_preserve_database_bytes(self):
        with TemporaryDirectory(prefix='waggle-r1-rejected-') as temporary:
            for version in range(1, 8):
                for denied in (False, True):
                    with self.subTest(version=version, denied=denied):
                        original, _ = legacy_payload(version)
                        if not denied:
                            original = '{"unknown": true}'
                        path = Path(temporary) / f'v{version}-{denied}.sqlite3'
                        old_database(path, version, original, deny=denied)
                        before = path.read_bytes()
                        with self.assertRaises(StorageError):
                            Store(path)
                        self.assertEqual(path.read_bytes(), before)
                        with closing(sqlite3.connect(path)) as connection:
                            self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], version)
                            self.assertEqual(connection.execute('SELECT revision,data FROM state').fetchall(), [(9, original)])
                            self.assertFalse(connection.execute('SELECT name FROM sqlite_master WHERE name=?', (f'legacy_v{version}',)).fetchall())


class CurrentSourcePackage(unittest.TestCase):
    def test_actual_current_source_package_has_r1_modules_exact_bytes_and_no_private_state(self):
        selected = source_files(ROOT)
        required = {'task_context.py', 'failure_view.py', 'decision_view.py', 'decisions.py', 'runtime.py', 'style.tcss'}
        self.assertTrue(required.issubset(selected))
        with TemporaryDirectory(prefix='waggle-r1-package-') as temporary:
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
            self.assertEqual(manifest['tests'], 'not_run')  # Packaging does not fabricate acceptance evidence.
            self.assertEqual(set(manifest['source_files']), set(selected))
            for name in selected:
                self.assertEqual(payload[name], (ROOT / name).read_bytes())
                self.assertEqual(hashlib.sha256(payload[name]).hexdigest(), manifest['files'][name])
            self.assertTrue(required.issubset(payload))
            self.assertFalse(any(name.endswith(('.sqlite3', '.db')) or name.startswith(('sessions/', 'bridges/', '.local/'))
                                 or name in {'auth.json', '.env'} for name in payload))


if __name__ == '__main__':
    unittest.main()
