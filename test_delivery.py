"""Delivery-first UI regressions, using only disposable local data."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from textual.containers import VerticalScroll
from textual.widgets import Button, Static

from app import Choice, Confirm, TaskRoom
from model import Board
from runtime import Runtime
from store import Store


class DeliveryUI(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="taskroom-delivery-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.runtime = Runtime(self.root / "bridges")
        self.store = self.enterContext(Store(self.root / "tasks.sqlite3"))
        board = self.store.load()
        board.add_project("临时研究", str(self.root))
        task = board.create("历史查询", "临时研究", "只查询已有结论，不运行实验。\n" + "补充范围说明。\n" * 45)
        board.assign(task, str(uuid4()))
        queued = board.queue_real(task, str(uuid4()), str(uuid4()), str(self.root))
        result = deepcopy(queued)
        result.state, result.seq, result.delivery = "finished", 40, "full"
        result.summary = "已恢复历史结论，未启动新实验。\n产物：本次答复，没有新建文件。"
        result.checks = ["依据：docs/retained-result.md；历史文档，不是本次回测。", "本次未运行测试，未验证策略收益。"]
        result.events = [f"工具开始 · read · {index}" for index in range(35)] + ["Pi agent_settled"]
        board.ingest(result)
        self.store.save(board)

    def app(self):
        return TaskRoom(store=self.store, runtime=self.runtime)

    @staticmethod
    def frame(app):
        return "\n".join(strip.text for strip in app.screen._compositor.render_strips())

    async def test_reopening_same_review_starts_at_actual_delivery(self):
        for size in ((165, 51), (80, 24), (120, 30)):
            with self.subTest(size=size):
                app = self.app()
                async with app.run_test(size=size) as pilot:
                    await pilot.press("enter")
                    scroll = app.query_one("#detail-scroll", VerticalScroll)
                    scroll.scroll_end(animate=False)
                    await pilot.pause()
                    self.assertGreater(scroll.scroll_y, 0)
                    await pilot.press("escape", "enter")
                    await pilot.pause()
                    self.assertEqual(scroll.scroll_y, 0, "Explicit reopen must not retain the old log position.")
                    self.assertIn("已恢复历史结论", self.frame(app))
                    self.assertEqual(app.board.tasks[1].status, "review")
                    for button in app.query("#detail-actions Button"):
                        self.assertTrue(app.query_one("#inspector").content_region.contains_region(button.region))

    async def test_tools_are_in_readonly_records_not_delivery_and_confirmation_has_scope(self):
        app = self.app()
        before = (self.root / "tasks.sqlite3").read_bytes()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.press("enter")
            detail = str(app.query_one("#detail-text", Static).content)
            self.assertIn("已恢复历史结论", detail)
            self.assertIn("docs/retained-result.md", detail)
            self.assertIn("自报", detail)
            self.assertNotIn("工具开始", detail)
            await pilot.click("#activity-button")
            self.assertIsInstance(app.screen, Confirm)
            assert isinstance(app.screen, Confirm)
            self.assertEqual(app.screen.title_text, "执行记录 · 不代表交付成果")
            self.assertIn("工具开始 · read", app.screen.body)
            await pilot.press("ctrl+enter")
            self.assertEqual(app.board.tasks[1].status, "review")
            await pilot.press("r")
            assert isinstance(app.screen, Confirm)
            self.assertIn("已恢复历史结论", app.screen.body)
            self.assertIn("只查询已有结论，不运行实验。", app.screen.body)
            self.assertIn("docs/retained-result.md", app.screen.body)
            self.assertIn("剩余", app.screen.body)
            self.assertNotIn("工具开始", app.screen.body)
            await pilot.press("enter")  # The existing default cancel remains safe.
            self.assertEqual(app.board.tasks[1].status, "review")
            await pilot.press("ctrl+k", *"执行记录", "enter")
            self.assertNotIsInstance(app.screen, Choice)
            self.assertIsInstance(app.screen, Confirm)
            await pilot.press("escape")
        self.assertEqual((self.root / "tasks.sqlite3").read_bytes(), before)
        self.assertFalse(self.runtime.root.exists())

    async def test_partial_delivery_and_live_refresh_keep_safety_and_scroll(self):
        board = self.store.load()
        task = board.tasks[1]
        run = board.runs[task.run_id]
        task.full_delivery, run.delivery = False, "partial"
        task.remaining = run.remaining = "尚未核对第二阶段，不能验收整任务。"
        self.store.save(board)
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("enter")
            self.assertIn("尚未核对第二阶段", self.frame(app))
            self.assertTrue(app.query_one("#accept-button", Button).disabled)
            scroll = app.query_one("#detail-scroll", VerticalScroll)
            scroll.scroll_end(animate=False)
            await pilot.pause()
            old_scroll, focused = scroll.scroll_y, app.focused
            self.assertGreater(old_scroll, 0)
            app.refresh_tasks()  # Ordinary updates must not jump while the human is reading.
            await pilot.pause()
            self.assertEqual(scroll.scroll_y, old_scroll)
            self.assertIs(app.focused, focused)
            await pilot.press("r")
            self.assertNotIsInstance(app.screen, Confirm)
            self.assertEqual(app.board.tasks[1].status, "review")


if __name__ == "__main__":
    unittest.main()
