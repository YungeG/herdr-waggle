"""Retention keeps identities/evidence, never stops or forgets uncertain execution."""

import json
import sqlite3
import unittest
from contextlib import closing
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from journal import parse_event
from model import AGENTS, Board
from store import APPLICATION_ID, SCHEMA_VERSION, StorageError, Store, decode, encode


def finished_board():
    board = Board(live=True)
    board.add_project("fixture", "/virtual/project")
    task = board.create("保留报告", "fixture", "原要求")
    task.remaining = "本轮核查合同：\n交付物：完整报告\n允许的范围：原要求范围\n验收依据：报告完成\n排除：不修改文件\n超出本轮范围的修改、试验和预算需另行明确审批；Pi 原有权限确认不被替代。"
    board.assign(task, str(uuid4()))
    run = board.queue_real(task, str(uuid4()), str(uuid4()), "/virtual/project")
    result = deepcopy(run)
    result.seq, result.state, result.delivery = 1, "finished", "full"
    result.summary, result.checks = "完整报告", ["自报证据，需人核实"]
    board.ingest(result)
    return board, task


class RetentionTests(unittest.TestCase):
    def test_delete_restore_and_new_ids_never_reuse_retained_identity(self):
        board = Board(live=True)
        task = board.create("误建任务", "收集箱")
        self.assertEqual(board.retirement_target(task), "trashed")
        board.retire(task, "trashed")
        self.assertEqual(
            (task.retention, task.status, task.run), ("trashed", "draft", "none")
        )
        self.assertEqual(board.create("下一条", "收集箱").id, 2)
        board.restore(task)
        self.assertEqual(task.retention, "active")
        events = [parse_event(raw) for raw in task.events[-2:]]
        self.assertTrue(all(event is not None for event in events))
        self.assertEqual(
            [event.kind for event in events if event is not None],
            ["trashed", "restored"],
        )
        self.assertEqual(set(board.tasks), {1, 2})

    def test_history_can_archive_or_trash_without_changing_reports(self):
        for destination in ("archived", "trashed"):
            with self.subTest(destination=destination):
                board, task = finished_board()
                board.accept(task)
                original_task, original_run = (
                    asdict(task),
                    deepcopy(board.runs[task.run_id]),
                )
                self.assertEqual(board.retirement_target(task), "archived")
                board.retire(task, destination)
                self.assertEqual(task.status, "done")
                board.restore(task)
                for name in set(original_task) - {"events", "retention"}:
                    self.assertEqual(asdict(task)[name], original_task[name])
                self.assertEqual(board.runs[task.run_id], original_run)

    def test_returned_round_and_simulated_history_are_still_history(self):
        board, task = finished_board()
        board.return_work(task, "另作安排")
        self.assertEqual(task.run, "none")
        self.assertEqual(board.retirement_target(task), "archived")
        demo = Board()
        sample = demo.create("sample", "个人事务")
        demo.assign(sample, AGENTS[0])
        demo.queue(sample)
        demo.simulate(sample, "ack")
        demo.simulate(sample, "partial")
        demo.return_work(sample, "稍后处理")
        self.assertEqual(demo.retirement_target(sample), "archived")

    def test_active_missing_foreign_and_historical_active_run_block_retention(self):
        for state in ("queued", "working", "waiting"):
            with self.subTest(state=state):
                board, task = finished_board()
                board.runs[task.run_id].state = state
                before = encode(board)
                with self.assertRaises(ValueError):
                    board.retire(task, "archived")
                self.assertEqual(encode(board), before)
        for variant in ("missing", "foreign", "historical", "projection"):
            with self.subTest(variant=variant):
                board, task = finished_board()
                if variant == "missing":
                    del board.runs[task.run_id]
                elif variant == "foreign":
                    board.runs[task.run_id].task_id = 2
                elif variant == "historical":
                    old = deepcopy(board.runs[task.run_id])
                    old.id = str(uuid4())
                    old.state = "working"
                    board.runs[old.id] = old
                else:
                    task.run = "working"
                with self.assertRaises(ValueError):
                    board.retirement_target(task)

    def test_legacy_execution_evidence_is_not_mistaken_for_a_never_dispatched_task(
        self,
    ):
        board = Board(live=True)
        task = board.create("missing run", "收集箱")
        task.events.append("派发意图已保存 · 旧记录")
        with self.assertRaises(ValueError):
            board.retirement_target(task)
        demo = Board()
        sample = demo.create("legacy simulation", "个人事务")
        sample.events.append("模拟阶段交付 · 仍有工作")
        self.assertEqual(demo.retirement_target(sample), "archived")

    def test_same_agent_other_task_does_not_block_archiving_finished_task(self):
        board, task = finished_board()
        other = board.create("另一个任务", task.project)
        other.remaining = "本轮核查合同：\n交付物：另一个任务交付\n允许的范围：项目范围\n验收依据：任务完成\n排除：不修改其他文件\n超出本轮范围的修改、试验和预算需另行明确审批；Pi 原有权限确认不被替代。"
        board.assign(other, task.agent)
        board.queue_real(other, str(uuid4()), str(uuid4()), "/virtual/project")
        board.retire(task, "archived")
        self.assertEqual(task.retention, "archived")
        self.assertEqual(other.run, "queued")

    def test_hidden_tasks_cannot_edit_assign_dispatch_accept_or_rework(self):
        board, task = finished_board()
        board.retire(task, "archived")
        before = encode(board)
        for operation in (
            lambda: board.edit(task, "change", task.project, ""),
            lambda: board.accept(task),
            lambda: board.return_work(task, "retry"),
            lambda: board.abandon(task, "stop"),
        ):
            with self.assertRaises(ValueError):
                operation()
        self.assertEqual(encode(board), before)
        draft = board.create("hidden", task.project)
        board.assign(draft, str(uuid4()))
        board.retire(draft, "trashed")
        for operation in (
            lambda: board.assign(draft, str(uuid4())),
            lambda: board.queue_real(
                draft, str(uuid4()), str(uuid4()), "/virtual/project"
            ),
        ):
            with self.assertRaises(ValueError):
                operation()


class RetentionStorageTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(prefix="waggle-retention-store-")
        self.addCleanup(temp.cleanup)
        self.file = Path(temp.name) / "tasks.sqlite3"

    def v2(self, board):
        raw = json.loads(encode(board))
        raw.pop("next_task_id")
        raw.pop("supplements", None)
        raw.pop("decisions", None)
        raw.pop("task_contexts", None)
        raw.pop("evidence_items", None)
        raw.pop("evidence_verifications", None)
        for field in ("task_dependencies", "dependency_checks", "ownership_releases", "ownership_receipts",
                      "budget_scopes", "usage_observations", "ledger_verifications", "budget_grants"):
            raw.pop(field)
        for task in raw["tasks"]:
            task.pop("retention", None)
            task.pop("steps", None)
        original = json.dumps(raw, ensure_ascii=False)
        with closing(sqlite3.connect(self.file)) as connection, connection:
            connection.execute(
                "CREATE TABLE state (singleton INTEGER PRIMARY KEY, revision INTEGER NOT NULL, data TEXT NOT NULL)"
            )
            connection.execute("INSERT INTO state VALUES (1, 7, ?)", (original,))
            connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
            connection.execute("PRAGMA user_version=2")
        return original

    def test_v2_upgrade_retains_original_and_all_execution_evidence(self):
        board, task = finished_board()
        original = self.v2(board)
        with Store(self.file) as store:
            restored = store.load()
            self.assertEqual(restored.tasks, board.tasks)
            self.assertEqual(restored.runs, board.runs)
            self.assertEqual(
                store.connection.execute(
                    "SELECT revision, data FROM legacy_v2"
                ).fetchall(),
                [(7, original)],
            )
            self.assertEqual(
                store.connection.execute("PRAGMA user_version").fetchone()[0],
                SCHEMA_VERSION,
            )
            restored.retire(restored.tasks[task.id], "archived")
            store.save(restored)
        with Store(self.file) as reopened:
            self.assertEqual(reopened.load().tasks[task.id].retention, "archived")
            self.assertEqual(
                reopened.connection.execute("SELECT data FROM legacy_v2").fetchone()[0],
                original,
            )

    def test_corrupt_v2_or_denied_migration_rolls_back(self):
        board = Board(live=True)
        board.create("keep", "收集箱")
        self.v2(board)
        with closing(sqlite3.connect(self.file)) as connection, connection:
            connection.execute(
                "CREATE TRIGGER refuse BEFORE UPDATE ON state BEGIN SELECT RAISE(IGNORE); END"
            )
        before = self.file.read_bytes()
        with self.assertRaises(StorageError):
            Store(self.file)
        self.assertEqual(self.file.read_bytes(), before)
        with closing(sqlite3.connect(self.file)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertFalse(
                connection.execute(
                    "SELECT name FROM sqlite_master WHERE name='legacy_v2'"
                ).fetchall()
            )

    def test_corrupt_v2_and_commit_failure_preserve_original_database(self):
        board, _ = finished_board()
        original = self.v2(board)
        with closing(sqlite3.connect(self.file)) as connection, connection:
            connection.execute("UPDATE state SET data='{}'")
        before = self.file.read_bytes()
        with self.assertRaises(StorageError):
            Store(self.file)
        self.assertEqual(self.file.read_bytes(), before)
        with closing(sqlite3.connect(self.file)) as connection, connection:
            connection.execute("UPDATE state SET data=?", (original,))
        before = self.file.read_bytes()
        connection = sqlite3.connect(self.file)
        connection.set_authorizer(
            lambda action, arg1, *_: (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_TRANSACTION and arg1 == "COMMIT"
                else sqlite3.SQLITE_OK
            )
        )
        with (
            patch("store.sqlite3.connect", return_value=connection),
            self.assertRaises(StorageError),
        ):
            Store(self.file)
        self.assertEqual(self.file.read_bytes(), before)
        with closing(sqlite3.connect(self.file)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertFalse(
                connection.execute(
                    "SELECT name FROM sqlite_master WHERE name='legacy_v2'"
                ).fetchall()
            )

    def test_prior_v1_backup_survives_v2_upgrade_and_old_writer_is_rejected(self):
        board, _ = finished_board()
        self.v2(board)
        with closing(sqlite3.connect(self.file)) as connection, connection:
            connection.execute("CREATE TABLE legacy_v1 (revision INTEGER, data TEXT)")
            connection.execute("INSERT INTO legacy_v1 VALUES (2, 'original v1 bytes')")
        with Store(self.file) as store:
            self.assertEqual(
                store.connection.execute("SELECT * FROM legacy_v1").fetchall(),
                [(2, "original v1 bytes")],
            )
        before = self.file.read_bytes()
        with patch("store.SCHEMA_VERSION", 2), self.assertRaises(StorageError):
            Store(self.file)
        self.assertEqual(self.file.read_bytes(), before)

    def test_current_schema_is_strict_and_cannot_hide_active_execution(self):
        board, task = finished_board()
        for invalid in ("missing", True, "unknown"):
            raw = json.loads(encode(board))
            if invalid == "missing":
                raw["tasks"][0].pop("retention")
            else:
                raw["tasks"][0]["retention"] = invalid
            with self.assertRaises(StorageError):
                decode(json.dumps(raw))
        task.retention = "archived"
        board.runs[task.run_id].state = "working"
        with self.assertRaises(StorageError):
            decode(encode(board))

    def test_saved_trash_survives_restart_conflict_and_restores_without_reusing_id(
        self,
    ):
        with Store(self.file) as first, Store(self.file) as stale:
            board, previous = first.load(), stale.load()
            task = board.create("trash", "收集箱")
            board.retire(task, "trashed")
            first.save(board)
            previous.create("stale", "收集箱")
            with self.assertRaises(StorageError):
                stale.save(previous)
        with Store(self.file) as reopened:
            board = reopened.load()
            self.assertEqual(board.create("new", "收集箱").id, 2)
            board.restore(board.tasks[1])
            reopened.save(board)
        with Store(self.file) as reopened:
            self.assertEqual(reopened.load().tasks[1].retention, "active")
