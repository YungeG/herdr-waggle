"""Persistent UI behavior and failure handling, only against temporary databases."""
from copy import deepcopy
from contextlib import redirect_stderr
import io
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from textual import events
from textual.widgets import Button, Input, Static, TextArea

from app import Confirm, Note, TaskForm, TaskRoom, main
from model import AGENTS, Board, sample_board
from store import Store


class PersistentUITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="taskroom-ui-test-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "task data" / "tasks.sqlite3"
        self.enterContext(patch.dict(os.environ, {"TASKROOM_BRIDGE_DIR": str(Path(temporary.name) / "bridges")}))

    def assert_saved(self, app):
        with Store(self.path) as observer:
            self.assertEqual(observer.load().tasks, app.board.tasks)

    async def test_keyboard_lifecycle_survives_each_restart_without_auto_execution(self):
        with Store(self.path) as store:
            store.load()
            store.save(Board())  # Explicit empty simulation fixture; production starts live and empty.
            app = TaskRoom(store=store)
            async with app.run_test(size=(80, 24)) as pilot:
                self.assertEqual(app.board.tasks, {})
                await pilot.press("p", *"终端工作台", "enter", "n", *"重启恢复", "enter", "e")
                app.screen.query_one(TextArea).focus()
                app.post_message(events.Paste("完整要求，保留中文。\n第二行 [literal]"))
                await pilot.pause()
                await pilot.press("ctrl+enter", "a", "enter", "s", "ctrl+enter")
                self.assertEqual(app.board.tasks[1].run, "queued")
                self.assertEqual(app.selected_id, 1)
                self.assert_saved(app)
                expected = deepcopy(app.board.tasks)
        with Store(self.path) as store:
            app = TaskRoom(store=store)
            async with app.run_test(size=(120, 30)) as pilot:
                self.assertEqual(app.board.tasks, expected)
                await pilot.pause()
                self.assertEqual(app.board.tasks[1].run, "queued")
                await pilot.press("g", "enter", "g", *"阻塞", "enter", *"等待补充", "ctrl+enter")
                self.assertEqual(app.board.tasks[1].blocker, "等待补充")
                self.assertEqual(app.board.tasks[1].status, "blocked")
                self.assert_saved(app)
        with Store(self.path) as store:
            app = TaskRoom(store=store)
            async with app.run_test(size=(80, 24)) as pilot:
                self.assertEqual(app.board.tasks[1].status, "blocked")
                await pilot.press("b", *"继续第二阶段", "ctrl+enter")
                self.assertEqual(app.board.tasks[1].run, "none")
                await pilot.press("s", "ctrl+enter", "g", "enter", "g", *"阶段", "enter")
                self.assertFalse(app.board.tasks[1].can_accept)
                self.assert_saved(app)
        with Store(self.path) as store:
            app = TaskRoom(store=store)
            async with app.run_test(size=(180, 40)) as pilot:
                self.assertEqual(app.board.tasks[1].status, "review")
                self.assertFalse(app.board.tasks[1].can_accept)
                await pilot.press("r")
                self.assertNotIsInstance(app.screen, Confirm)
                await pilot.press("b", *"完成余下工作", "ctrl+enter", "s", "ctrl+enter", "g", "enter", "g", *"完整", "enter")
                self.assertTrue(app.board.tasks[1].can_accept)
                self.assert_saved(app)
                await pilot.press("r", "ctrl+enter")
                self.assertEqual(app.board.tasks[1].status, "done")
                self.assertEqual(app.board.tasks[1].description, "完整要求，保留中文。\n第二行 [literal]")
                self.assert_saved(app)
                expected = deepcopy(app.board.tasks)
        with Store(self.path) as store:
            app = TaskRoom(store=store)
            self.assertEqual(app.board.tasks, expected)
            self.assertGreater(len(app.board.tasks[1].events), 10)

    async def test_failed_create_preserves_form_text_focus_and_disk_then_retries(self):
        with Store(self.path) as store:
            app = TaskRoom(store=store)
            async with app.run_test(size=(80, 24)) as pilot:
                original = app.board
                store.connection.execute("PRAGMA query_only=ON")
                await pilot.press("n", *"不能丢的标题", "enter")
                await pilot.pause()
                self.assertIsInstance(app.screen, TaskForm)
                self.assertEqual(app.screen.query_one(Input).value, "不能丢的标题")
                self.assertEqual(getattr(app.focused, "id", None), "task-title")
                self.assertIs(app.board, original)
                self.assertEqual(app.board.tasks, {})
                self.assert_saved(app)
                rendered = "\n".join(strip.text for strip in app.screen._compositor.render_strips())
                self.assertIn("未保存", rendered)
                store.connection.execute("PRAGMA query_only=OFF")
                await pilot.press("ctrl+enter")
                self.assertNotIsInstance(app.screen, TaskForm)
                self.assertEqual(app.board.tasks[1].title, "不能丢的标题")
                self.assert_saved(app)

    async def test_failed_edit_and_command_leave_original_snapshot_unchanged(self):
        with Store(self.path) as store:
            store.load()
            board = Board()  # This retained journey intentionally exercises simulation callbacks.
            task = board.create("原始标题", "终端工作台", "原始说明")
            board.assign(task, AGENTS[0])
            store.save(board)
            app = TaskRoom(store=store)
            async with app.run_test(size=(120, 30)) as pilot:
                original = app.board
                expected = deepcopy(app.board.tasks)
                store.connection.execute("PRAGMA query_only=ON")
                await pilot.press("s", "ctrl+enter")
                self.assertIs(app.board, original)
                self.assertEqual(app.board.tasks, expected)
                await pilot.press("e", "home", "shift+end", *"尚未保存的改名")
                area = app.screen.query_one(TextArea)
                area.focus()
                app.post_message(events.Paste("保留输入，原来的说明也还在。"))
                await pilot.pause()
                text = area.text
                await pilot.press("ctrl+enter")
                self.assertIsInstance(app.screen, TaskForm)
                self.assertEqual(area.text, text)
                self.assertEqual(app.screen.query_one(Input).value, "尚未保存的改名")
                self.assertEqual(app.board.tasks, expected)
                self.assert_saved(app)
                store.connection.execute("PRAGMA query_only=OFF")
                await pilot.press("ctrl+enter")
                self.assertEqual(app.board.tasks[1].title, "尚未保存的改名")
                self.assertEqual(app.board.tasks[1].description, text)
                self.assert_saved(app)

    async def test_failed_note_keeps_reason_and_controls_visible(self):
        with Store(self.path) as store:
            store.load()
            store.save(sample_board())  # Explicit temporary test fixture, never a startup seed.
            app = TaskRoom(store=store)
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.press("slash", *"D003", "escape", "b", *"关键补充不能丢失")
                original = app.board
                store.connection.execute("PRAGMA query_only=ON")
                await pilot.press("ctrl+enter")
                await pilot.pause()
                self.assertIsInstance(app.screen, Note)
                self.assertEqual(app.screen.query_one(TextArea).text, "关键补充不能丢失")
                self.assertIs(app.board, original)
                self.assert_saved(app)
                dialog = app.screen.query_one("#dialog")
                for button in app.screen.query(Button):
                    self.assertTrue(dialog.content_region.contains_region(button.region), f"clipped error control {button.id}")
                store.connection.execute("PRAGMA query_only=OFF")
                await pilot.press("ctrl+enter")
                self.assertEqual(app.board.tasks[3].run, "none")
                self.assertEqual(app.board.tasks[3].remaining, "关键补充不能丢失")
                self.assert_saved(app)

    async def test_stale_ui_cannot_overwrite_and_cancel_creates_nothing(self):
        with Store(self.path) as first, Store(self.path) as second:
            app = TaskRoom(store=first)
            newer = second.load()
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.press("n", *"取消", "escape")
                self.assertEqual(app.board.tasks, {})
                self.assert_saved(app)
                await pilot.press("n", *"保留旧窗口输入")
                newer.create("另一个窗口已保存", "收集箱")
                second.save(newer)
                await pilot.press("enter")
                self.assertIsInstance(app.screen, TaskForm)
                self.assertEqual(app.screen.query_one(Input).value, "保留旧窗口输入")
                self.assertEqual(app.board.tasks, {})
                with Store(self.path) as observer:
                    self.assertEqual(observer.load().tasks, newer.tasks)


class EntryTests(unittest.TestCase):
    def test_demo_never_constructs_store(self):
        with patch("app.Store") as storage, patch("app.Runtime") as runtime, patch("app.TaskRoom.run"):
            self.assertEqual(main(["--demo"]), 0)
            storage.assert_not_called()
            runtime.assert_not_called()

    def test_local_empty_start_and_invalid_database_failure_are_explicit(self):
        with TemporaryDirectory(prefix="taskroom-cli-test-") as directory:
            path = Path(directory) / "path with spaces" / "tasks.sqlite3"
            with patch("app.TaskRoom.run") as run:
                self.assertEqual(main(["--db", str(path)]), 0)
                run.assert_called_once()
            with Store(path) as store:
                self.assertEqual(store.load().tasks, {})
            bad = Path(directory) / "invalid.sqlite3"
            bad.write_bytes(b"do not overwrite")
            stderr = io.StringIO()
            with patch("app.TaskRoom.run") as run, redirect_stderr(stderr):
                self.assertEqual(main(["--db", str(bad)]), 1)
                run.assert_not_called()
            self.assertIn("启动失败", stderr.getvalue())
            self.assertEqual(bad.read_bytes(), b"do not overwrite")
            with patch.dict(os.environ, {"XDG_STATE_HOME": directory}), patch("app.TaskRoom.run"):
                self.assertEqual(main([]), 0)
                with Store(Path(directory) / "taskroom/tasks.sqlite3") as store:
                    self.assertEqual(store.load().tasks, {})

    def test_demo_and_database_arguments_are_mutually_exclusive(self):
        with patch("app.Store") as storage, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                main(["--demo", "--db", "/must-not-create.sqlite3"])
            self.assertEqual(error.exception.code, 2)
            storage.assert_not_called()


if __name__ == "__main__":
    unittest.main()
