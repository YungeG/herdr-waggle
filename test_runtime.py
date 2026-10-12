"""Pure run/migration contracts and fail-closed mailbox boundaries, no real user state."""
import json
import os
import sqlite3
import time
import unittest
from contextlib import closing
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from decisions import review_requirements
from journal import encoded_event
from model import Board, Run, sample_board
from runtime import (
    Agent,
    BridgeError,
    Runtime,
    bridge_root,
    canonical_directory,
    install_bridge,
    private_dir,
    publish,
    read_json,
)
from store import APPLICATION_ID, StorageError, Store, decode, default_path, encode
from task_context import WorkContract
from test_capabilities import capability_record


def prepared_board(cwd="/tmp/exact/project"):
    board = Board(live=True)
    board.add_project("真实目录", cwd)
    task = board.create("整任务", "真实目录", "中文，完整说明")
    board.assign(task, str(uuid4()))
    contract = WorkContract(
        purpose="implementation",
        deliverable="完成整任务交付",
        allowed_scope="项目目录",
        verification="回归测试通过",
        exclusions="不修改其他模块",
    )
    task.remaining = contract.compile()
    run = board.queue_real(task, str(uuid4()), str(uuid4()), cwd)
    return board, task, run


class RunTests(unittest.TestCase):
    def test_ack_report_settled_accept_and_retry_are_distinct(self):
        board, task, original = prepared_board()
        self.assertEqual((task.status, task.run), ("ready", "queued"))
        incoming = deepcopy(original)
        incoming.seq, incoming.state = 2, "working"
        incoming.delivery, incoming.summary, incoming.checks = "full", "自报成果", ["自报测试；不是独立验证"]
        board.ingest(incoming)
        self.assertFalse(task.can_accept)
        with self.assertRaises(ValueError):
            board.accept(task)
        with self.assertRaises(ValueError):
            board.return_work(task, "还在执行")
        with self.assertRaises(ValueError):
            board.queue_real(task, str(uuid4()), original.session_id, original.cwd)
        settled = deepcopy(incoming)
        settled.seq, settled.state = 3, "finished"
        board.ingest(settled)
        self.assertTrue(task.can_accept)
        requirements = review_requirements("补齐一项模拟成果", "仅 Board 夹具状态", "下一轮身份不同，旧结果不得覆盖")
        board.return_work(task, requirements)
        next_run = board.queue_real(task, str(uuid4()), original.session_id, original.cwd)
        self.assertNotEqual(next_run.id, original.id)
        self.assertEqual(next_run.requirements, requirements)
        late = deepcopy(settled)
        late.seq = 99
        self.assertFalse(board.ingest(late))
        self.assertEqual(task.run_id, next_run.id)
        self.assertFalse(task.can_accept)

    def test_partial_failure_waiting_unknown_and_orphan_resolution(self):
        for state, delivery in [("finished", "partial"), ("failed", ""), ("stopped", ""), ("interrupted", ""), ("waiting", "")]:
            with self.subTest(state=state):
                board, task, original = prepared_board()
                incoming = deepcopy(original)
                incoming.seq, incoming.state, incoming.delivery = 1, state, delivery
                incoming.summary, incoming.remaining = "尚未整任务完成", "第二阶段"
                board.ingest(incoming)
                self.assertFalse(task.can_accept)
                if state == "waiting":
                    with self.assertRaises(ValueError):
                        board.return_work(task, "继续")
                    board.abandon(task, "已在旧 Pi 窗口核实进程停止")
                    self.assertEqual(board.runs[original.id].state, "abandoned")
                board.return_work(task, "人工补充")
                self.assertEqual((task.status, task.run), ("ready", "none"))
                self.assertEqual(task.remaining, "人工补充")

    def test_identity_sequence_directory_and_one_active_run_per_agent(self):
        board, task, original = prepared_board()
        for key, value in [("id", str(uuid4())), ("task_id", 999), ("cwd", "/tmp/other"), ("session_id", str(uuid4())), ("title", "different")]:
            incoming = deepcopy(original)
            incoming.seq = 1
            setattr(incoming, key, value)
            with self.assertRaises(ValueError):
                board.ingest(incoming)
        self.assertFalse(board.ingest(deepcopy(original)))
        other = board.create("另一项", "真实目录")
        board.assign(other, task.agent)
        with self.assertRaises(ValueError):
            board.queue_real(other, str(uuid4()), original.session_id, original.cwd)
        with self.assertRaises(ValueError):
            board.edit(task, "changed", "真实目录", "new")
        with self.assertRaises(ValueError):
            board.simulate(task, "ack")
        self.assertEqual(decode(encode(board)).runs, board.runs)

    def test_strict_run_links_and_all_field_types(self):
        board, _, run = prepared_board()
        envelope = json.loads(encode(board))
        for key, value in [("task_id", True), ("seq", -1), ("events", [None]), ("id", "../bad"), ("state", "unknown"), ("delivery", "full")]:
            with self.subTest(key=key):
                row = {**asdict(run), key: value}
                with self.assertRaises(ValueError):
                    Run.from_record(row)
        for change in ("duplicate", "unknown_task", "missing_run", "unknown_current"):
            raw = deepcopy(envelope)
            if change == "duplicate":
                raw["runs"].append(asdict(run))
            elif change == "unknown_task":
                raw["runs"][0]["task_id"] = 999
            elif change == "missing_run":
                raw["runs"] = []
            else:
                raw["tasks"][0]["run_id"] = str(uuid4())
            with self.assertRaises(StorageError):
                decode(json.dumps(raw))


class SessionLocationTests(unittest.TestCase):
    def test_original_session_header_is_verified_without_reading_or_changing_transcript(self):
        from runtime import locate_session
        with TemporaryDirectory(prefix="waggle-session-location-") as directory:
            root = Path(directory)
            _, _, run = prepared_board(str(root))
            file = root / "original.jsonl"
            header = {"type": "session", "version": 3, "id": run.session_id, "cwd": str(root)}
            body = json.dumps(header).encode() + b"\n" + b"not a transcript to parse\n" * 50000
            file.write_bytes(body)
            run.seq = 1
            run.events = [encoded_event("origin", str(file), source="system", run_id=run.id, seq=1)]
            before = deepcopy(run)
            result = locate_session(run)
            self.assertIn("已核对原会话", result)
            self.assertIn(str(file), result)
            self.assertIn(run.session_id, result)
            self.assertIn(f"RUN_ID={run.id}", result)
            self.assertIn("不会切换", result)
            self.assertEqual(file.read_bytes(), body)
            self.assertEqual(run, before)

    def test_missing_legacy_ephemeral_and_unsafe_locations_never_guess_or_create(self):
        from runtime import locate_session
        with TemporaryDirectory(prefix="waggle-session-reject-") as directory:
            root = Path(directory)
            _, _, run = prepared_board(str(root))
            self.assertIn("未保存原会话路径", locate_session(run))
            valid = root / "valid.jsonl"
            valid.write_text(json.dumps({"type": "session", "id": run.session_id, "cwd": str(root)}) + "\n")
            link = root / "link.jsonl"
            link.symlink_to(valid)
            fifo = root / "pipe"
            os.mkfifo(fifo)
            invalid = root / "invalid.jsonl"
            invalid.write_text("x" * 17000 + "\n")
            wrong = root / "wrong.jsonl"
            wrong.write_text(json.dumps({"type": "session", "id": str(uuid4()), "cwd": str(root)}) + "\n")
            wrong_cwd = root / "wrong-cwd.jsonl"
            wrong_cwd.write_text(json.dumps({"type": "session", "id": run.session_id, "cwd": str(root.parent)}) + "\n")
            for path in ("", "relative.jsonl", str(root / "missing.jsonl"), str(link), str(fifo), str(root), str(invalid), str(wrong), str(wrong_cwd)):
                with self.subTest(path=path):
                    run.seq = 1
                    run.events = [encoded_event("origin", path, source="system", run_id=run.id, seq=1)]
                    result = locate_session(run)
                    self.assertNotIn("已核对原会话", result)
                    self.assertIn(run.session_id, result)
                    self.assertIn(run.id, result)
            self.assertFalse((root / "missing.jsonl").exists())
            self.assertFalse((root / "bridges").exists())


class MigrationTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="waggle-migration-test-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "v1.sqlite3"
        self.original = json.dumps([{key: value for key, value in asdict(task).items() if key not in ("execution", "run_id", "retention", "steps")} for task in sample_board().tasks.values()], ensure_ascii=False)

    def legacy(self, data=None, trigger=False):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("CREATE TABLE state (singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision INTEGER NOT NULL, data TEXT NOT NULL)")
            connection.execute("INSERT INTO state VALUES (1, 7, ?)", (self.original if data is None else data,))
            connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
            connection.execute("PRAGMA user_version=1")
            if trigger:
                connection.execute("CREATE TRIGGER refuse_update BEFORE UPDATE ON state BEGIN SELECT RAISE(IGNORE); END")

    def test_v1_migrates_atomically_with_exact_backup_and_no_live_conversion(self):
        self.legacy()
        with Store(self.path) as store:
            board = store.load()
            self.assertTrue(board.live)
            self.assertEqual(board.tasks, sample_board().tasks)
            self.assertEqual(board.runs, {})
            self.assertEqual(store.revision, 7)
            self.assertEqual(store.connection.execute("SELECT revision, data FROM legacy_v1").fetchall(), [(7, self.original)])
            self.assertEqual(board.create("新任务", "收集箱").execution, "pi")
            store.save(board)
        with Store(self.path) as store:
            self.assertEqual(len(store.load().tasks), 13)
            self.assertEqual(store.connection.execute("SELECT data FROM legacy_v1").fetchone()[0], self.original)

    def test_corrupt_v1_and_rejected_migration_keep_original_bytes(self):
        for corrupt in (True, False):
            with self.subTest(corrupt=corrupt):
                self.legacy(data="[{\"id\":1}]" if corrupt else None, trigger=not corrupt)
                before = self.path.read_bytes()
                with self.assertRaises(StorageError):
                    Store(self.path)
                self.assertEqual(self.path.read_bytes(), before)
                with closing(sqlite3.connect(self.path)) as connection:
                    self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
                    self.assertFalse(connection.execute("SELECT name FROM sqlite_master WHERE name='legacy_v1'").fetchall())
                self.path.unlink()


class MailboxTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="waggle-mailbox-test-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)
        self.project = self.path / "非 Git 中文" / "sub directory"
        self.project.mkdir(parents=True)
        self.runtime = Runtime(self.path / "bridges")
        self.board, self.task, self.attempt = prepared_board(str(self.project))
        private_dir(self.runtime.root, create=True)
        endpoint = self.runtime.root / self.attempt.agent_id
        private_dir(endpoint, create=True)
        for name in ("commands", "runs"):
            private_dir(endpoint / name, create=True)
        self.metadata = {"version": 1, "id": self.attempt.agent_id, "session_id": self.attempt.session_id,
                         "cwd": self.attempt.cwd, "name": "Test Pi", "model": "offline", "pid": os.getpid(),
                         "heartbeat": int(time.time() * 1000), "online": True, "idle": True, "active_run": ""}
        self.metadata_file = endpoint / "agent.json"
        publish(self.metadata_file, self.metadata)
        publish(endpoint / "report-capabilities.json", capability_record(Agent.from_record(self.metadata)))

    def test_waggle_default_namespace_and_explicit_mailbox_are_read_only(self):
        state = self.path / "new-state"
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(state), "WAGGLE_BRIDGE_DIR": ""}):
            self.assertEqual(default_path(), state / "waggle/tasks.sqlite3")
            self.assertEqual(bridge_root(), state / "waggle/bridges")
            self.assertEqual(Runtime().root, bridge_root())
            self.assertFalse(state.exists(), "Choosing new defaults must not create or migrate data.")
            explicit = self.path / "explicit-mailbox"
            with patch.dict(os.environ, {"WAGGLE_BRIDGE_DIR": str(explicit)}):
                self.assertEqual(Runtime().root, explicit)
                self.assertEqual(default_path(), state / "waggle/tasks.sqlite3")
                self.assertFalse(explicit.exists())

    def replace_metadata(self, **changes):
        self.metadata_file.unlink()
        publish(self.metadata_file, {**self.metadata, **changes})

    def test_exact_non_git_unicode_directory_no_home_fallback_and_install_opt_in(self):
        self.assertEqual(canonical_directory(str(self.project)), str(self.project))
        link = self.path / "directory-link"
        link.symlink_to(self.project, target_is_directory=True)
        self.assertEqual(canonical_directory(str(link)), str(self.project))
        for value in ("", "relative", str(self.path / "missing"), str(self.metadata_file)):
            with self.assertRaises(ValueError):
                canonical_directory(value)
        self.assertFalse((self.project / ".pi").exists())
        target = install_bridge(str(self.project))
        self.assertEqual(target, self.project / ".pi/extensions/waggle.ts")
        self.assertIn("/waggle connect", target.read_text())
        self.assertEqual(install_bridge(str(self.project)), target)
        target.write_text("keep existing extension exactly")
        with self.assertRaises(BridgeError):
            install_bridge(str(self.project))
        self.assertEqual(target.read_text(), "keep existing extension exactly")
        with patch.dict(os.environ, {"WAGGLE_BRIDGE_DIR": "relative"}):
            with self.assertRaises(BridgeError):
                bridge_root()

    def test_dispatch_requires_fresh_exact_identity_and_is_not_ack(self):
        self.assertEqual(len(self.runtime.discover()), 1)
        self.runtime.dispatch(self.attempt)
        self.assertIsNone(self.runtime.read_run(self.attempt))
        self.assertEqual(self.attempt.seq, 0)
        files = list((self.runtime.endpoint(self.attempt.agent_id) / "commands").iterdir())
        self.assertEqual(len(files), 1)
        self.assertEqual(read_json(files[0])["run"], asdict(self.attempt))
        self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)
        for changes in ({"idle": False}, {"online": False}, {"heartbeat": 1}, {"session_id": str(uuid4())}, {"cwd": str(self.path)}):
            with self.subTest(changes=changes):
                self.replace_metadata(**changes)
                with self.assertRaises(BridgeError):
                    self.runtime.dispatch(self.attempt)
        self.assertEqual(len(list(files[0].parent.iterdir())), 1)

    def test_private_bounded_records_and_symlinks_fail_closed(self):
        self.metadata_file.chmod(0o644)
        with self.assertRaises(BridgeError):
            self.runtime.discover()
        self.metadata_file.chmod(0o600)
        with self.assertRaises(FileExistsError):
            publish(self.metadata_file, self.metadata)
        destination = self.path / "foreign.json"
        self.metadata_file.rename(destination)
        self.metadata_file.symlink_to(destination)
        with self.assertRaises(OSError):
            self.runtime.agent(self.attempt.agent_id)
        self.metadata_file.unlink()
        self.metadata_file.write_bytes(b"x" * (1024 * 1024 + 1))
        self.metadata_file.chmod(0o600)
        with self.assertRaises(BridgeError):
            self.runtime.agent(self.attempt.agent_id)
        self.assertEqual(destination.read_text(), json.dumps(self.metadata, ensure_ascii=False))

    def test_result_identity_and_schema_are_checked(self):
        file = self.runtime.endpoint(self.attempt.agent_id) / "runs" / f"{self.attempt.id}.json"
        wrong = {**asdict(self.attempt), "cwd": "/another/project"}
        publish(file, wrong)
        with self.assertRaises(BridgeError):
            self.runtime.read_run(self.attempt)
        with self.assertRaises(BridgeError):
            Agent.from_record({**self.metadata, "pid": True})


if __name__ == "__main__":
    unittest.main()
