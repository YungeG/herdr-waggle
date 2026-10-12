"""All storage tests use disposable databases, never the user's task records."""
import json
import os
import sqlite3
import unittest
from contextlib import closing
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from model import Board, sample_board
from store import (
    APPLICATION_ID,
    SCHEMA_VERSION,
    StorageError,
    Store,
    decode,
    default_path,
    encode,
)


class StoreTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="waggle-store-test-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "state" / "tasks.sqlite3"

    def test_empty_then_all_fields_roundtrip_and_new_identity(self):
        board = sample_board()
        board.create("中文，标点。[literal]", "收集箱", "第一行\n第二行\t引号\" and emoji 🐟")
        with Store(self.path) as store:
            self.assertEqual(store.load().tasks, {})
            store.save(board)
        with Store(self.path) as reopened:
            loaded = reopened.load()
            self.assertEqual(loaded.tasks, board.tasks)
            self.assertEqual(loaded.create("下一条", "收集箱").id, 14)
            reopened.save(loaded)
        with Store(self.path) as reopened:
            self.assertEqual(len(reopened.load().tasks), 14)

    @unittest.skipUnless(os.name == "posix", "POSIX mode bits")
    def test_new_storage_is_private_and_existing_permissions_unchanged(self):
        with Store(self.path):
            pass
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)
        self.path.chmod(0o640)
        with Store(self.path):
            pass
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o640)

    def test_stale_writer_cannot_overwrite_other_window(self):
        with Store(self.path) as first, Store(self.path) as second:
            newer, stale = first.load(), second.load()
            newer.create("先保存", "收集箱")
            first.save(newer)
            stale.create("旧快照", "收集箱")
            with self.assertRaisesRegex(StorageError, "另一窗口"):
                second.save(stale)
            self.assertEqual(second.revision, 0)
        with Store(self.path) as reopened:
            self.assertEqual(reopened.load().tasks, newer.tasks)

    def test_query_only_and_busy_fail_without_changing_disk(self):
        with Store(self.path) as store:
            board = store.load()
            board.create("不能丢的任务", "收集箱")
            store.save(board)
            candidate = deepcopy(board)
            candidate.create("失败的写入", "收集箱")
            store.connection.execute("PRAGMA query_only=ON")
            with self.assertRaises(StorageError):
                store.save(candidate)
            self.assertEqual(store.revision, 1)
            store.connection.execute("PRAGMA query_only=OFF")
            with closing(sqlite3.connect(self.path)) as locker, locker:
                locker.execute("BEGIN IMMEDIATE")
                with self.assertRaises(StorageError):
                    store.save(candidate)
            self.assertEqual(store.load().tasks, board.tasks)
            store.save(candidate)
        with Store(self.path) as reopened:
            self.assertEqual(reopened.load().tasks, candidate.tasks)

    def test_silent_update_rejection_is_not_reported_as_saved(self):
        with Store(self.path) as store:
            board = store.load()
            board.create("未写入", "收集箱")
            store.connection.execute("CREATE TRIGGER refuse_update BEFORE UPDATE ON state BEGIN SELECT RAISE(IGNORE); END")
            with self.assertRaises(StorageError):
                store.save(board)
            self.assertEqual(store.revision, 0)
        with Store(self.path) as reopened:
            self.assertEqual(reopened.load().tasks, {})

    def test_commit_failure_rolls_back_and_retry_can_succeed(self):
        with Store(self.path) as store:
            board = store.load()
            board.create("提交失败前", "收集箱")
            store.save(board)
            candidate = deepcopy(board)
            candidate.create("尚未提交", "收集箱")
            def deny_commit(action, arg1, *_):
                return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_TRANSACTION and arg1 == "COMMIT" else sqlite3.SQLITE_OK
            store.connection.set_authorizer(deny_commit)
            with self.assertRaises(StorageError):
                store.save(candidate)
            self.assertFalse(store.connection.in_transaction)
            self.assertEqual(store.revision, 1)
            store.connection.set_authorizer(None)
            self.assertEqual(store.load().tasks, board.tasks)
            store.save(candidate)
        with Store(self.path) as reopened:
            self.assertEqual(reopened.load().tasks, candidate.tasks)

    def test_foreign_database_and_unsupported_version_are_not_modified(self):
        self.path.parent.mkdir()
        with closing(sqlite3.connect(self.path)) as foreign, foreign:
            foreign.execute("CREATE TABLE unrelated (value TEXT)")
            foreign.execute("INSERT INTO unrelated VALUES ('keep exactly')")
        before = self.path.read_bytes()
        with self.assertRaises(StorageError):
            Store(self.path)
        self.assertEqual(self.path.read_bytes(), before)
        newer = self.path.with_name("newer.sqlite3")
        with Store(newer):
            pass
        with closing(sqlite3.connect(newer)) as connection, connection:
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
        before = newer.read_bytes()
        with self.assertRaises(StorageError):
            Store(newer)
        self.assertEqual(newer.read_bytes(), before)

    def test_malformed_file_and_missing_state_fail_closed(self):
        self.path.parent.mkdir()
        self.path.write_bytes(b"not a database; do not replace")
        before = self.path.read_bytes()
        with self.assertRaises(StorageError):
            Store(self.path)
        self.assertEqual(self.path.read_bytes(), before)
        missing = self.path.with_name("missing.sqlite3")
        with Store(missing) as store:
            store.connection.execute("DELETE FROM state")
            store.connection.commit()
            before = missing.read_bytes()
            with self.assertRaises(StorageError):
                store.load()
            self.assertEqual(missing.read_bytes(), before)

    def test_malformed_payloads_are_never_defaulted_or_overwritten(self):
        row = asdict(sample_board().tasks[1])
        envelope = json.loads(encode(sample_board()))
        def payload(rows):
            return json.dumps({**envelope, "tasks": rows})
        invalid = ["not json", "{}", payload([row, row])]
        for key, value in [("id", True), ("id", 0), ("title", " "), ("project", "unknown"), ("status", "unknown"), ("run", "unknown"), ("agent", "unknown"), ("full_delivery", 1), ("checks", [False]), ("events", "not a list"), ("execution", "unknown"), ("run_id", "unknown")]:
            invalid.append(payload([{**row, key: value}]))
        invalid.extend([payload([{key: value for key, value in row.items() if key != "remaining"}]), payload([{**row, "unknown": "no silent dropping"}])])
        with Store(self.path) as store:
            board = store.load()
            for data in invalid:
                with self.subTest(data=data[:60]):
                    with self.assertRaises(StorageError):
                        decode(data)
                    store.connection.execute("UPDATE state SET data=?", (data,))
                    store.connection.commit()
                    with self.assertRaises(StorageError):
                        store.load()
                    with self.assertRaises(StorageError):
                        store.save(board)
                    self.assertEqual(store.connection.execute("SELECT data FROM state").fetchone()[0], data)

    def test_save_requires_loaded_revision_and_matching_task_ids(self):
        with Store(self.path) as store:
            with self.assertRaises(StorageError):
                store.save(Board())
            store.load()
            board = sample_board()
            board.tasks[77] = board.tasks.pop(1)
            with self.assertRaises(StorageError):
                store.save(board)
            self.assertEqual(store.load().tasks, {})
            self.assertEqual(store.connection.execute("PRAGMA application_id").fetchone()[0], APPLICATION_ID)

    def test_bad_path_and_xdg_defaults(self):
        self.path.parent.mkdir()
        with self.assertRaises(StorageError):
            Store(self.path.parent)
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(self.path.parent)}):
            self.assertEqual(default_path(), self.path.parent / "waggle" / "tasks.sqlite3")
        for state in ("", "relative/path"):
            with patch.dict(os.environ, {"XDG_STATE_HOME": state}), patch("store.Path.home", return_value=self.path.parent):
                self.assertEqual(default_path(), self.path.parent / ".local/state/waggle/tasks.sqlite3")


if __name__ == "__main__":
    unittest.main()
