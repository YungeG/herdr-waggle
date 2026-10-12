"""Two-stage deletion touches only disposable boards/databases, never real projects or Pi."""

import json
import sqlite3
import unittest
from contextlib import closing
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from textual.widgets import OptionList

from app import Confirm, Waggle
from journal import parse_event
from model import Board
from report_reader import ReportMarkdown
from runtime import Runtime
from store import APPLICATION_ID, SCHEMA_VERSION, StorageError, Store, decode, encode
from task_context import WorkContract
from test_retention import finished_board


class ProjectDeletionTests(unittest.TestCase):
    def test_project_moves_every_task_to_trash_and_keeps_history_and_other_projects(
        self,
    ):
        board, history = finished_board()
        board.accept(history)
        board.retire(history, "archived")
        draft = board.create("draft", "fixture")
        already_trashed = board.create("already deleted", "fixture")
        board.retire(already_trashed, "trashed")
        other = board.create("leave alone", "收集箱")
        original_runs, original_other = deepcopy(board.runs), deepcopy(other)
        board.delete_project("fixture")
        self.assertEqual(board.projects, {"收集箱": ""})
        for task in (history, draft, already_trashed):
            self.assertEqual((task.project, task.retention), ("收集箱", "trashed"))
            event = parse_event(task.events[-1])
            assert event is not None
            self.assertEqual(event.kind, "project_deleted")
            self.assertIn("fixture", event.text)
            self.assertIn("/virtual/project", event.text)
        self.assertEqual(board.runs, original_runs)
        self.assertEqual(other, original_other)
        self.assertEqual(history.status, "done")
        self.assertEqual(history.summary, "完整报告")
        self.assertEqual(decode(encode(board)).tasks, board.tasks)
        board.restore(history)
        self.assertEqual(
            (history.project, history.retention, history.status),
            ("收集箱", "active", "done"),
        )
        board.add_project("fixture", "/virtual/project")

    def test_empty_project_and_protected_or_unknown_names(self):
        board = Board(live=True)
        board.add_project("empty", "/not/accessed")
        board.delete_project("empty")
        for name in ("收集箱", "all", "missing", "empty"):
            before = encode(board)
            with self.subTest(name=name), self.assertRaises(ValueError):
                board.delete_project(name)
            self.assertEqual(encode(board), before)

    def test_running_or_uncertain_member_rejects_whole_project_without_partial_changes(
        self,
    ):
        for variant in (
            "queued",
            "working",
            "waiting",
            "missing",
            "foreign",
            "historical",
            "projection",
            "stop_requested",
        ):
            with self.subTest(variant=variant):
                board, task = finished_board()
                first = board.create("first movable", "fixture")
                board.tasks = {first.id: first, task.id: task}
                if variant in {"queued", "working", "waiting"}:
                    board.runs[task.run_id].state = variant
                elif variant == "missing":
                    del board.runs[task.run_id]
                elif variant == "foreign":
                    board.runs[task.run_id].task_id = first.id
                elif variant == "historical":
                    old = deepcopy(board.runs[task.run_id])
                    old.id = str(uuid4())
                    old.state = "working"
                    board.runs[old.id] = old
                elif variant == "projection":
                    task.run = "working"
                else:
                    board.runs[task.run_id].state = "working"
                    board.note(
                        task, "stop_requested", "still waiting", run_id=task.run_id
                    )
                before = encode(board)
                with self.assertRaises(ValueError):
                    board.delete_project("fixture")
                self.assertEqual(encode(board), before)

    def test_purge_requires_trash_and_removes_only_owned_history_without_id_reuse(self):
        board, task = finished_board()
        other = board.create("other", "收集箱")
        with self.assertRaises(ValueError):
            board.purge(task)
        board.retire(task, "archived")
        with self.assertRaises(ValueError):
            board.purge(task)
        board.retire(task, "trashed")
        board.purge(task)
        self.assertEqual(board.tasks, {other.id: other})
        self.assertEqual(board.runs, {})
        board.retire(other, "trashed")
        board.purge(other)
        self.assertEqual(board.create("new", "收集箱").id, 3)
        with self.assertRaises(ValueError):
            board.purge(task)

    def test_other_project_active_run_does_not_block_or_get_deleted(self):
        board, task = finished_board()
        board.add_project("other", "/virtual/other")
        other = board.create("working elsewhere", "other")
        board.assign(other, str(uuid4()))
        contract = WorkContract(
            purpose="implementation",
            deliverable="完成整任务交付",
            allowed_scope="项目目录",
            verification="回归测试通过",
            exclusions="不修改其他模块",
        )
        other.remaining = contract.compile()
        run = board.queue_real(other, str(uuid4()), str(uuid4()), "/virtual/other")
        original = deepcopy((other, run))
        board.delete_project("fixture")
        board.purge(task)
        self.assertEqual((board.tasks[other.id], board.runs[run.id]), original)
        self.assertEqual(set(board.runs), {run.id})
        self.assertEqual(board.projects["other"], "/virtual/other")

    def test_purge_and_restore_reject_uncertain_or_foreign_trash(self):
        for variant in ("running", "missing", "foreign", "historical"):
            board, task = finished_board()
            board.retire(task, "trashed")
            if variant == "running":
                board.runs[task.run_id].state = "working"
            elif variant == "missing":
                del board.runs[task.run_id]
            elif variant == "foreign":
                task = deepcopy(task)
            else:
                old = deepcopy(board.runs[task.run_id])
                old.id = str(uuid4())
                old.state = "waiting"
                board.runs[old.id] = old
            before = encode(board)
            for operation in (board.purge, board.restore):
                with (
                    self.subTest(variant=variant, operation=operation.__name__),
                    self.assertRaises(ValueError),
                ):
                    operation(task)
            self.assertEqual(encode(board), before)


class ProjectDeletionStorageTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="waggle-project-store-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "tasks.sqlite3"

    def v3(self, board):
        raw = json.loads(encode(board))
        raw.pop("next_task_id", None)
        raw.pop("supplements", None)
        raw.pop("decisions")
        raw.pop("task_contexts")
        raw.pop("evidence_items", None)
        raw.pop("evidence_verifications", None)
        for field in ("task_dependencies", "dependency_checks", "ownership_releases", "ownership_receipts",
                      "budget_scopes", "usage_observations", "ledger_verifications", "budget_grants"):
            raw.pop(field)
        for task in raw["tasks"]:
            task.pop("steps", None)
        original = json.dumps(raw, ensure_ascii=False)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(
                "CREATE TABLE state (singleton INTEGER PRIMARY KEY, revision INTEGER NOT NULL, data TEXT NOT NULL)"
            )
            connection.execute("INSERT INTO state VALUES (1, 9, ?)", (original,))
            connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
            connection.execute("PRAGMA user_version=3")
        return original

    def test_v3_upgrade_and_purge_reopen_preserve_high_water_and_backup(self):
        board, task = finished_board()
        original = self.v3(board)
        with Store(self.path) as store:
            board = store.load()
            self.assertEqual(
                store.connection.execute("PRAGMA user_version").fetchone()[0],
                SCHEMA_VERSION,
            )
            self.assertEqual(
                store.connection.execute("SELECT data FROM legacy_v3").fetchone()[0],
                original,
            )
            board.delete_project("fixture")
            store.save(board)
        with Store(self.path) as store:
            board = store.load()
            self.assertEqual(board.tasks[task.id].retention, "trashed")
            board.purge(board.tasks[task.id])
            store.save(board)
        with Store(self.path) as store:
            board = store.load()
            self.assertEqual((board.tasks, board.runs), ({}, {}))
            self.assertEqual(board.create("new", "收集箱").id, 2)
            self.assertEqual(
                store.connection.execute("SELECT data FROM legacy_v3").fetchone()[0],
                original,
            )
        before = self.path.read_bytes()
        with patch("store.SCHEMA_VERSION", 3), self.assertRaises(StorageError):
            Store(self.path)
        self.assertEqual(self.path.read_bytes(), before)

    def test_v3_migration_failure_is_atomic(self):
        board, _ = finished_board()
        self.v3(board)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(
                "CREATE TRIGGER refuse BEFORE UPDATE ON state BEGIN SELECT RAISE(IGNORE); END"
            )
        before = self.path.read_bytes()
        with self.assertRaises(StorageError):
            Store(self.path)
        self.assertEqual(self.path.read_bytes(), before)

    def test_counter_is_strict_and_cannot_move_back_after_purge(self):
        board = Board(live=True)
        board.create("one", "收集箱")
        for value in (True, "2", 0, 1):
            raw = json.loads(encode(board))
            raw["next_task_id"] = value
            with self.subTest(value=value), self.assertRaises(StorageError):
                decode(json.dumps(raw))
        raw = json.loads(encode(board))
        raw.pop("next_task_id")
        with self.assertRaises(StorageError):
            decode(json.dumps(raw))
        with Store(self.path) as store:
            store.load()
            store.save(board)
            board.retire(board.tasks[1], "trashed")
            store.save(board)
            board.purge(board.tasks[1])
            store.save(board)
            board.next_task_id = 1
            with self.assertRaises(StorageError):
                store.save(board)
            self.assertEqual(store.load().next_task_id, 2)

    def test_store_rejects_one_step_wipe_and_rolls_back_failed_commit(self):
        with Store(self.path) as store:
            board = store.load()
            task = board.create("keep", "收集箱")
            store.save(board)
            before = encode(board)
            candidate = deepcopy(board)
            candidate.tasks.clear()
            with self.assertRaises(StorageError):
                store.save(candidate)
            self.assertEqual(encode(store.load()), before)
            board.retire(task, "trashed")
            store.save(board)
            before = encode(board)
            candidate = deepcopy(board)
            candidate.purge(candidate.tasks[task.id])
            store.connection.set_authorizer(
                lambda action, arg1, *_: (
                    sqlite3.SQLITE_DENY
                    if action == sqlite3.SQLITE_TRANSACTION and arg1 == "COMMIT"
                    else sqlite3.SQLITE_OK
                )
            )
            with self.assertRaises(StorageError):
                store.save(candidate)
            store.connection.set_authorizer(None)
            self.assertFalse(store.connection.in_transaction)
            self.assertEqual(encode(store.load()), before)
            store.save(candidate)
            self.assertEqual(store.load().tasks, {})

    def test_failed_purge_and_stale_writer_preserve_task_and_history(self):
        with Store(self.path) as store:
            store.load()
            board, task = finished_board()
            board.retire(task, "trashed")
            store.save(board)
            before = encode(board)
            candidate = deepcopy(board)
            candidate.purge(candidate.tasks[task.id])
            store.connection.execute("PRAGMA query_only=ON")
            with self.assertRaises(StorageError):
                store.save(candidate)
            self.assertEqual(encode(store.load()), before)
            store.connection.execute("PRAGMA query_only=OFF")
            with Store(self.path) as stale:
                stale_board = stale.load()
                store.save(candidate)
                with self.assertRaises(StorageError):
                    stale.save(stale_board)
            self.assertEqual(store.load().tasks, {})


class ProjectDeletionUI(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="waggle-project-ui-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.marker = self.project / "keep.txt"
        self.marker.write_text("shared files stay")

    async def test_project_cancel_delete_restore_and_purge_at_both_widths(self):
        for size in ((80, 24), (140, 36)):
            with (
                self.subTest(size=size),
                Store(self.root / f"{size[0]}.sqlite3") as store,
            ):
                board = store.load()
                board.add_project("fixture", str(self.project))
                task = board.create("delete me", "fixture")
                store.save(board)
                app = Waggle(store=store, runtime=Runtime(self.root / "mailbox"))
                async with app.run_test(size=size) as pilot:
                    app.set_project("fixture")
                    await pilot.press("ctrl+k", *"删除项目", "enter")
                    self.assertIsInstance(app.screen, Confirm)
                    assert app.focused is not None
                    self.assertEqual(app.focused.id, "cancel")
                    assert isinstance(app.screen, Confirm)
                    self.assertIn(str(self.project), app.screen.body)
                    before = encode(app.board)
                    await pilot.press("enter")
                    self.assertEqual(encode(app.board), before)
                    app.action_delete_project()
                    await pilot.pause()
                    await pilot.press("ctrl+enter")
                    self.assertNotIn("fixture", app.board.projects)
                    nav = app.query_one("#nav-project", OptionList)
                    self.assertNotIn(
                        "fixture",
                        [
                            nav.get_option_at_index(i).id
                            for i in range(nav.option_count)
                        ],
                    )
                    self.assertEqual(app.status_filter, "trashed")
                    self.assertEqual(
                        [item.id for item in app.visible_tasks()], [task.id]
                    )
                    await pilot.press("ctrl+k", *"永久删除", "enter")
                    self.assertIsInstance(app.screen, Confirm)
                    assert app.focused is not None
                    self.assertEqual(app.focused.id, "cancel")
                    await pilot.press("escape")
                    self.assertIn(task.id, app.board.tasks)
                    app.action_restore()
                    await pilot.pause()
                    await pilot.press("ctrl+enter")
                    self.assertEqual(app.board.tasks[task.id].project, "收集箱")
                    self.assertEqual(app.board.tasks[task.id].retention, "active")
                    app.action_retire()
                    await pilot.pause()
                    await pilot.press("ctrl+enter", "f", *"回收站", "enter")
                    app.action_purge()
                    await pilot.pause()
                    await pilot.press("ctrl+enter")
                    self.assertEqual(app.board.tasks, {})
                    self.assertIsNone(app.selected_id)
                    self.assertEqual(app.board.create("next", "收集箱").id, 2)
                self.assertEqual(store.load().tasks, {})
                self.assertEqual(self.marker.read_text(), "shared files stay")
                self.assertFalse((self.root / "mailbox").exists())

    async def test_delete_project_picker_works_from_all_projects(self):
        board = Board(live=True)
        board.add_project("empty", str(self.project))
        app = Waggle(board, runtime=Runtime(self.root / "mailbox"))
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("ctrl+k", *"删除项目", "enter")
            await pilot.press(*"empty", "enter")
            self.assertIsInstance(app.screen, Confirm)
            await pilot.press("ctrl+enter")
            self.assertEqual(app.board.projects, {"收集箱": ""})
            self.assertEqual(app.project_name, "all")
            self.assertEqual(self.marker.read_text(), "shared files stay")

    async def test_project_stale_confirmation_and_busy_member_keep_all_records(self):
        with Store(self.root / "stale.sqlite3") as store:
            board = store.load()
            board.add_project("fixture", str(self.project))
            task = board.create("first", "fixture")
            board.assign(task, str(uuid4()))
            store.save(board)
            app = Waggle(store=store, runtime=Runtime(self.root / "mailbox"))
            async with app.run_test(size=(80, 24)) as pilot:
                app.set_project("fixture")
                app.action_delete_project()
                await pilot.pause()
                app.apply_change(
                    lambda candidate: candidate.create(
                        "arrived during confirmation", "fixture"
                    )
                )
                before = encode(app.board)
                await pilot.press("ctrl+enter")
                self.assertEqual(encode(app.board), before)
                app.apply_change(
                    lambda candidate: (
                        setattr(candidate.tasks[task.id], "remaining", WorkContract(
                            purpose="implementation",
                            deliverable="完成整任务交付",
                            allowed_scope="项目目录",
                            verification="回归测试通过",
                            exclusions="不修改其他模块",
                        ).compile()),
                        candidate.queue_real(
                            candidate.tasks[task.id],
                            str(uuid4()),
                            str(uuid4()),
                            str(self.project),
                        )
                    )[-1]
                )
                app.action_delete_project()
                await pilot.pause()
                self.assertNotIsInstance(app.screen, Confirm)
                self.assertIn("fixture", app.board.projects)
                self.assertTrue(
                    all(item.retention == "active" for item in app.board.tasks.values())
                )
                self.assertEqual(self.marker.read_text(), "shared files stay")

    async def test_failed_saves_keep_project_filters_and_purge_history_until_retry(
        self,
    ):
        with Store(self.root / "failed.sqlite3") as store:
            store.load()
            board, task = finished_board()
            store.save(board)
            app = Waggle(store=store, runtime=Runtime(self.root / "mailbox"))
            async with app.run_test(size=(80, 24)) as pilot:
                app.set_project("fixture")
                before = encode(app.board)
                store.connection.execute("PRAGMA query_only=ON")
                app.action_delete_project()
                await pilot.pause()
                await pilot.press("ctrl+enter")
                self.assertEqual(encode(app.board), before)
                self.assertEqual(app.project_name, "fixture")
                self.assertEqual(encode(store.load()), before)
                store.connection.execute("PRAGMA query_only=OFF")
                app.action_delete_project()
                await pilot.pause()
                await pilot.press("ctrl+enter")
                app.action_toggle_detail()
                await pilot.pause()
                self.assertIn(
                    "完整报告", app.query_one("#detail-markdown", ReportMarkdown).source
                )
                before = encode(app.board)
                store.connection.execute("PRAGMA query_only=ON")
                app.action_purge()
                await pilot.pause()
                await pilot.press("ctrl+enter")
                self.assertEqual(encode(app.board), before)
                self.assertEqual(encode(store.load()), before)
                store.connection.execute("PRAGMA query_only=OFF")
                app.action_purge()
                await pilot.pause()
                await pilot.press("ctrl+enter")
                self.assertNotIn(task.id, app.board.tasks)
                self.assertEqual(app.board.runs, {})

    async def test_historical_task_can_delete_from_current_or_archived_view(self):
        for archived in (False, True):
            board, task = finished_board()
            if archived:
                board.retire(task, "archived")
            app = Waggle(board, runtime=Runtime(self.root / "mailbox"))
            async with app.run_test(size=(80, 24)) as pilot:
                if archived:
                    app.set_filter("archived")
                await pilot.press("ctrl+k", *"删除任务", "enter")
                self.assertIsInstance(app.screen, Confirm)
                await pilot.press("ctrl+enter", "f", *"回收站", "enter")
                self.assertEqual(app.board.tasks[task.id].retention, "trashed")
                self.assertEqual(app.board.runs[task.run_id].summary, "完整报告")
                await pilot.press("ctrl+k", *"永久删除", "enter")
                assert isinstance(app.screen, Confirm)
                self.assertIn("1 个历史轮次", app.screen.body)
                self.assertTrue(await pilot.click("#submit"))
                self.assertEqual((app.board.tasks, app.board.runs), ({}, {}))

    async def test_purge_rechecks_retained_history_after_confirmation(self):
        board, task = finished_board()
        board.retire(task, "trashed")
        app = Waggle(board, runtime=Runtime(self.root / "mailbox"))
        async with app.run_test(size=(80, 24)) as pilot:
            app.set_filter("trashed")
            app.action_purge()
            await pilot.pause()
            app.board.runs[task.run_id].summary = "changed after confirmation"
            await pilot.press("ctrl+enter")
            self.assertIn(task.id, app.board.tasks)
            self.assertIn(task.run_id, app.board.runs)
            app.action_purge()
            await pilot.pause()
            await pilot.press("ctrl+enter")
            self.assertEqual(app.board.tasks, {})
