"""R3 exact legacy preservation and current source package; disposable state only."""
import hashlib
import json
import sqlite3
import subprocess
import sys
import tarfile
import unittest
from contextlib import closing
from dataclasses import asdict, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from budget_ledger import FORMAL_APPROVAL_ENABLED, REGISTRIES
from decisions import (
    Answer,
    Budget,
    DecisionRecord,
    DecisionSpec,
    Intent,
    Target,
    preview_decisions,
)
from release import build_package, source_files
from store import SCHEMA_VERSION, StorageError, Store, decode, encode
from test_decisions import decision_board, selected, spec_record
from test_r1_integration import old_database
from test_r2_integration import LEGACY_BACKUP_QUERIES, r2_legacy_payload

ROOT = Path(__file__).resolve().parent
BACKUP_QUERIES = LEGACY_BACKUP_QUERIES + ('SELECT revision,data FROM legacy_v11',)


class R3Migration(unittest.TestCase):
    def test_versions1_to11_keep_exact_revision_raw_reports_and_empty_budget_registries(self):
        with TemporaryDirectory(prefix='waggle-r3-migration-') as temporary:
            for version in range(1, 12):
                with self.subTest(version=version):
                    original, reports = r2_legacy_payload(version)
                    path = Path(temporary) / f'v{version}.db'
                    old_database(path, version, original)
                    with Store(path) as store:
                        board = store.load()
                        self.assertEqual(store.connection.execute('PRAGMA user_version').fetchone()[0], SCHEMA_VERSION)
                        self.assertEqual(store.revision, 9)
                        self.assertEqual(store.connection.execute(BACKUP_QUERIES[version - 1]).fetchall(), [(9, original)])
                        for name in REGISTRIES:
                            self.assertEqual(getattr(board, name), {})
                        if version > 1:
                            self.assertEqual(tuple(next(iter(board.runs.values())).events), reports)
                        store.save(board)
                        self.assertEqual(store.revision, 9)  # Idempotent load/save cannot cause a concurrent revision conflict.
                        self.assertEqual(store.connection.execute(BACKUP_QUERIES[version - 1]).fetchall(), [(9, original)])

    def assert_interrupted_migration(self, version):
        # Exit only the disposable child, never a host service or executor process.
        child = (
            "import os, sqlite3, sys\n"
            "from pathlib import Path\n"
            "from store import Store\n"
            "phase = sys.argv[2]\n"
            "if phase == 'before_commit':\n"
            "    connect = sqlite3.connect\n"
            "    def traced_connect(*args, **kwargs):\n"
            "        connection = connect(*args, **kwargs)\n"
            "        def interrupt(statement):\n"
            "            if statement == 'COMMIT':\n"
            "                print(phase, flush=True)\n"
            "                os._exit(73)\n"
            "        connection.set_trace_callback(interrupt)\n"
            "        return connection\n"
            "    sqlite3.connect = traced_connect\n"
            "Store(Path(sys.argv[1]))\n"
            "if phase == 'after_commit':\n"
            "    print(phase, flush=True)\n"
            "    os._exit(73)\n"
            "raise AssertionError('The expected migration interruption was not reached')\n"
        )
        with TemporaryDirectory(prefix='waggle-r3-interrupted-') as temporary:
            original, reports = r2_legacy_payload(version)
            for phase in ('before_commit', 'after_commit'):
                with self.subTest(version=version, phase=phase):
                    path = Path(temporary) / (phase + '.db')
                    old_database(path, version, original)
                    before = path.read_bytes()
                    result = subprocess.run([sys.executable, '-B', '-c', child, str(path), phase],
                                            cwd=ROOT, capture_output=True, text=True, timeout=10, check=False)
                    self.assertEqual(result.returncode, 73, result.stderr)
                    self.assertEqual(result.stdout.strip(), phase)
                    with closing(sqlite3.connect(path)) as connection:
                        self.assertEqual(connection.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                        current_version = connection.execute('PRAGMA user_version').fetchone()[0]
                        revision, data = connection.execute('SELECT revision,data FROM state').fetchone()
                        self.assertEqual(revision, 9)
                        if phase == 'before_commit':
                            self.assertEqual(current_version, version)
                            self.assertEqual(data, original)
                            self.assertFalse(connection.execute(
                                "SELECT name FROM sqlite_master WHERE name=?", (f'legacy_v{version}',)).fetchall())
                        else:
                            self.assertEqual(current_version, SCHEMA_VERSION)
                            self.assertEqual(connection.execute(BACKUP_QUERIES[version - 1]).fetchall(), [(9, original)])
                    if phase == 'before_commit':
                        self.assertEqual(path.read_bytes(), before)
                    # Recovery uses the same public Store constructor, not a manual repair.
                    with Store(path) as store:
                        board = store.load()
                        self.assertEqual(store.revision, 9)
                        self.assertEqual(store.connection.execute(BACKUP_QUERIES[version - 1]).fetchall(), [(9, original)])
                        for name in REGISTRIES:
                            self.assertFalse(getattr(board, name))
                        if version > 1:
                            self.assertEqual(tuple(next(iter(board.runs.values())).events), reports)
                        store.save(board)
                        self.assertEqual(store.revision, 9)

    def test_v11_process_exit_at_commit_preserves_complete_snapshot(self):
        self.assert_interrupted_migration(11)

    def test_versions1_to10_process_exit_at_commit_preserve_exact_backups(self):
        for version in range(1, 11):
            self.assert_interrupted_migration(version)

    def test_v11_legacy_budget_stays_readable_but_never_creates_new_scope_usage_or_grant(self):
        spec = DecisionSpec.from_record(spec_record('budget'))
        board, task, run = decision_board([spec])
        old = preview_decisions(board, task.id, (Answer(),), Target('saved'))
        answer = selected('budget', budget=asdict(Budget(5, 90, 100, ('Stop if reached',))))
        record = DecisionRecord(1, str(uuid4()), '2026-09-24T00:00:00.000Z',
                                replace(old, answers=(answer,), compiled_text='Historical cumulative intent only; usage unknown'), Intent('saved'))
        board.decisions[record.id] = record
        raw = json.loads(encode(board))
        for name in REGISTRIES:
            raw.pop(name)
        original = json.dumps(raw, ensure_ascii=False, indent=2)
        with TemporaryDirectory(prefix='waggle-r3-old-budget-') as temporary:
            path = Path(temporary) / 'v11.db'
            old_database(path, 11, original)
            with Store(path) as store:
                loaded = store.load()
                self.assertEqual(loaded.decisions[record.id], record)
                self.assertEqual(loaded.runs[run.id], run)
                self.assertEqual(store.connection.execute('SELECT revision,data FROM legacy_v11').fetchone(), (9, original))
                for name in REGISTRIES:
                    self.assertFalse(getattr(loaded, name))
                for target in (Target('saved'), Target('new_run', run.agent_id, run.session_id, run.cwd)):
                    with self.subTest(target=target.kind), self.assertRaises(ValueError):
                        preview_decisions(loaded, task.id, (answer,), target)
                self.assertFalse(FORMAL_APPROVAL_ENABLED)

    def test_v11_corrupt_or_sql_refused_migration_is_physical_noop(self):
        with TemporaryDirectory(prefix='waggle-r3-refused-') as temporary:
            valid, _ = r2_legacy_payload(11)
            malformed = json.loads(valid)
            malformed['tasks'][0]['id'] = True
            for label, original, denied in (('corrupt', json.dumps(malformed), False), ('sql-refused', valid, True)):
                path = Path(temporary) / (label + '.db')
                old_database(path, 11, original, deny=denied)
                before = path.read_bytes()
                with self.assertRaises(StorageError):
                    Store(path)
                self.assertEqual(path.read_bytes(), before)
                with closing(sqlite3.connect(path)) as connection:
                    self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], 11)
                    self.assertEqual(connection.execute('SELECT revision,data FROM state').fetchone(), (9, original))
                    self.assertFalse(connection.execute("SELECT name FROM sqlite_master WHERE name='legacy_v11'").fetchall())

    def test_schema12_requires_exact_budget_envelope_even_when_no_usage_exists(self):
        from model import Board
        original = json.loads(encode(Board(live=True)))
        for name in REGISTRIES:
            damaged = original.copy()
            damaged.pop(name)
            with self.subTest(name=name), self.assertRaises(StorageError):
                decode(json.dumps(damaged))


class R3SourcePackage(unittest.TestCase):
    def test_current_budget_modules_actual_bytes_hashes_and_no_private_state(self):
        selected = source_files(ROOT)
        self.assertTrue({'budget_ledger.py', 'budget_view.py', 'app.py', 'model.py', 'store.py',
                         'evidence.py', 'dependencies.py', 'task_context.py'}.issubset(selected))
        with TemporaryDirectory(prefix='waggle-r3-package-') as temporary:
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
            self.assertEqual(manifest['tests'], 'not_run')  # This checks the archive, not execution of its copies.
            self.assertEqual(set(manifest['source_files']), set(selected))
            for name in selected:
                self.assertEqual(payload[name], (ROOT / name).read_bytes())
                self.assertEqual(hashlib.sha256(payload[name]).hexdigest(), manifest['files'][name])
            self.assertFalse(any(name.endswith(('.db', '.sqlite3')) or name.startswith(('sessions/', 'bridges/', '.local/'))
                                 or name in {'auth.json', '.env'} for name in payload))


if __name__ == '__main__':
    unittest.main()
