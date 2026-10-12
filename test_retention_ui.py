"""Default-cancel, recoverable task retention; only disposable stores/mailboxes."""
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from textual.widgets import Button, Static

from app import Confirm, Waggle
from runtime import Runtime
from store import Store, encode
from task_context import WorkContract
from test_retention import finished_board


class RetentionUI(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="waggle-retention-ui-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    async def test_trash_cancel_restore_counts_and_id_persistence_at_both_widths(self):
        for size in ((80, 24), (120, 30)):
            with self.subTest(size=size), Store(self.root / f"{size[0]}.sqlite3") as store:
                board = store.load(); board.create("未派发", "收集箱"); store.save(board)
                app = Waggle(store=store, runtime=Runtime(self.root / "mailbox"))
                async with app.run_test(size=size) as pilot:
                    before = encode(app.board)
                    await pilot.press("ctrl+k", *"删除", "enter")
                    self.assertIsInstance(app.screen, Confirm)
                    assert app.focused is not None
                    self.assertEqual(app.focused.id, "cancel")
                    await pilot.press("enter")
                    self.assertEqual(encode(app.board), before)
                    app.action_retire(); await pilot.pause(); await pilot.press("ctrl+enter")
                    self.assertEqual(app.board.tasks[1].retention, "trashed")
                    self.assertEqual(app.visible_tasks(), [])
                    self.assertIsNone(app.selected_id)
                    await pilot.press("f", *"回收站", "enter")
                    self.assertEqual([task.id for task in app.visible_tasks()], [1])
                    self.assertEqual(app.query_one("#accept-button", Button).label, "查看记录")
                    self.assertIn("回收站", str(app.query_one("#context-hint", Static).content))
                    await pilot.press("ctrl+k", *"恢复任务", "enter")
                    self.assertIsInstance(app.screen, Confirm)
                    assert app.focused is not None
                    self.assertEqual(app.focused.id, "cancel")
                    await pilot.press("ctrl+enter")
                    self.assertEqual(app.board.tasks[1].retention, "active")
                    self.assertEqual(app.status_filter, "all")
                    self.assertEqual(app.selected_id, 1)
                loaded = store.load()
                self.assertEqual(loaded.tasks[1].retention, "active")
                self.assertEqual(loaded.create("下一条", "收集箱").id, 2)
                self.assertFalse((self.root / "mailbox").exists())

    async def test_archive_keeps_acceptance_evidence_and_read_only_actions(self):
        with Store(self.root / "archive.sqlite3") as store:
            store.load(); board, task = finished_board(); board.accept(task); store.save(board)
            original = deepcopy(board.runs[task.run_id])
            app = Waggle(store=store, runtime=Runtime(self.root / "mailbox"))
            async with app.run_test(size=(80, 24)) as pilot:
                app.action_retire(); await pilot.pause()
                self.assertIn("归档", str(app.screen.query_one(".dialog-title", Static).content))
                await pilot.press("ctrl+enter", "f", *"已归档", "enter")
                self.assertEqual(app.board.tasks[task.id].status, "done")
                self.assertEqual(app.board.runs[task.run_id], original)
                self.assertIn("完整报告", str(app.query_one("#detail-text", Static).content))
                for key in ("e", "a", "s", "g", "r", "b", "x"):
                    await pilot.press(key)
                    self.assertEqual(len(app.screen_stack), 1, key)
                self.assertEqual(app.board.tasks[task.id].retention, "archived")
                app.action_restore(); await pilot.pause(); await pilot.press("ctrl+enter")
                self.assertEqual(app.board.tasks[task.id].status, "done")
                self.assertEqual(app.board.runs[task.run_id], original)
            self.assertEqual(store.load().tasks[task.id].status, "done")

    async def test_busy_or_changed_task_cannot_be_hidden_by_stale_confirmation(self):
        with Store(self.root / "changed.sqlite3") as store:
            board = store.load(); board.add_project("fixture", str(self.root))
            task = board.create("original", "fixture"); board.assign(task, str(uuid4())); store.save(board)
            app = Waggle(store=store, runtime=Runtime(self.root / "mailbox"))
            async with app.run_test(size=(80, 24)) as pilot:
                app.action_retire(); await pilot.pause()
                app.board.tasks[task.id].title = "changed while dialog was open"
                await pilot.press("ctrl+enter")
                self.assertEqual(app.board.tasks[task.id].retention, "active")
                def set_contract_and_queue(candidate):
                    contract = WorkContract(
                        purpose="implementation",
                        deliverable="完成整任务交付",
                        allowed_scope="项目目录",
                        verification="回归测试通过",
                        exclusions="不修改其他模块",
                    )
                    candidate.tasks[task.id].remaining = contract.compile()
                    return candidate.queue_real(candidate.tasks[task.id], str(uuid4()), str(uuid4()), str(self.root))
                app.apply_change(set_contract_and_queue)
                app.action_retire(); await pilot.pause()
                self.assertNotIsInstance(app.screen, Confirm)
                self.assertEqual(app.board.tasks[task.id].retention, "active")
                self.assertFalse((self.root / "mailbox").exists())

    async def test_failed_save_keeps_task_visible_and_original_store_then_retries(self):
        with Store(self.root / "failure.sqlite3") as store:
            board = store.load(); board.create("keep", "收集箱"); store.save(board)
            app = Waggle(store=store, runtime=Runtime(self.root / "mailbox"))
            async with app.run_test(size=(120, 30)) as pilot:
                before = encode(app.board)
                store.connection.execute("PRAGMA query_only=ON")
                app.action_retire(); await pilot.pause(); await pilot.press("ctrl+enter")
                self.assertEqual(encode(app.board), before)
                self.assertEqual([task.id for task in app.visible_tasks()], [1])
                self.assertEqual(encode(store.load()), before)
                store.connection.execute("PRAGMA query_only=OFF")
                app.action_retire(); await pilot.pause(); await pilot.press("ctrl+enter")
                self.assertEqual(app.board.tasks[1].retention, "trashed")
