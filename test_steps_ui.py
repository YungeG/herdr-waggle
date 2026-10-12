"""Steps UI only touches owned temporary databases and never a live Pi mailbox."""
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory

from textual.widgets import Button, OptionList, Select, Static, TextArea

from app import Choice, Note, Waggle
from runtime import Runtime
from step_view import StepFlow, StepForm
from store import Store, encode
from test_live import wait_ui
from test_steps import update


class StepUITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory(prefix="waggle-step-ui-")))
        self.store = self.enterContext(Store(self.root / "tasks.sqlite3"))
        self.board = self.store.load()
        self.task = self.board.create("明确任务", "收集箱")
        self.runtime = Runtime(self.root / "mailbox")
        self.store.save(self.board)

    def app(self):
        return Waggle(store=self.store, runtime=self.runtime)

    async def open_steps(self, app, pilot):
        self.assertTrue(await pilot.click("#more-button"))
        menu = app.screen
        assert isinstance(menu, Choice)
        menu.query_one("#choices", OptionList).highlighted = list(menu.options).index("steps")
        menu.action_submit()
        await wait_ui(pilot, lambda: not isinstance(app.screen, Choice))

    async def test_define_cancel_save_and_record_current_step_without_dispatch(self):
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("right")
            self.assertIn("未定义步骤", str(app.query_one("#detail-state", Static).content))
            self.assertFalse(app.query_one(StepFlow).display)
            await self.open_steps(app, pilot)
            self.assertIsInstance(app.screen, Note)
            app.screen.query_one(TextArea).text = "需求\n实现\n验证"
            await pilot.press("escape")
            self.assertEqual(self.store.load().tasks[1].steps, [])
            await self.open_steps(app, pilot)
            app.screen.query_one(TextArea).text = "需求\n实现\n验证"
            await pilot.press("ctrl+enter")
            self.assertEqual([s.status for s in app.board.tasks[1].steps], ["pending"] * 3)
            flow = app.query_one(StepFlow)
            self.assertIn("当前位置未指定", str(flow.content))
            flow.focus()
            await pilot.press("enter")
            self.assertIsInstance(app.screen, StepForm)
            self.assertTrue(app.screen.query_one("#cancel", Button).has_focus)
            app.screen.query_one("#step-status", Select).value = "doing"
            app.screen.query_one("#step-goal", TextArea).text = "核对用户明确范围"
            app.screen.query_one("#step-next_action", TextArea).text = "人工整理核对记录"
            await pilot.press("ctrl+enter")
            self.assertEqual(len(app.screen_stack), 1)
            self.assertEqual(self.store.load().tasks[1].steps[0].status, "doing")
            self.assertIn("当前位置 · 需求", str(flow.content))
            self.assertEqual((app.board.tasks[1].status, app.board.tasks[1].run, app.board.runs), ("draft", "none", {}))
            self.assertFalse(self.runtime.root.exists())

    async def test_flow_click_keyboard_resize_and_default_cancel_preserve_model(self):
        self.board.add_steps(self.task, ["需求", "实现", "验证"])
        update(self.board, self.task, status="done", output="明确范围", evidence="人工核对记录")
        update(self.board, self.task, 1, status="blocked", blocker="没有明确数据授权", next_action="请用户确认数据来源")
        self.store.save(self.board)
        app = self.app()
        async with app.run_test(size=(139, 50)) as pilot:
            await pilot.press("right")
            flow = app.query_one(StepFlow)
            before = encode(app.board)
            for width, height in [(139, 50), (80, 24), (180, 40)]:
                await pilot.resize_terminal(width, height)
                await pilot.pause()
                self.assertIn("当前位置 · 实现（阻塞）", str(flow.content))
                self.assertIn("没有明确数据授权", str(flow.content))
                self.assertIn("请用户确认数据来源", str(flow.content))
                self.assertIn("↓" if width == 80 else "→", str(flow.content))
                self.assertLessEqual(app.query_one("#detail-actions").region.bottom, height - 2)
            target = app.board.tasks[1].steps[1].id
            cells = [(x, y) for y in range(flow.size.height) for x in range(flow.size.width)
                     if flow.get_style_at(x, y).meta.get("step_id") == target]
            self.assertTrue(cells, "Node must have an actual rendered hit target")
            await pilot.click(flow, offset=cells[0])
            form = app.screen
            assert isinstance(form, StepForm)
            self.assertEqual(form.step.id, target)
            self.assertTrue(any("人工保存" in str(widget.content) for widget in form.query(".muted").results(Static)))
            await pilot.press("enter")  # Default button closes; no update timestamp on read.
            flow.focus()
            await pilot.press("up", "enter")
            form = app.screen
            assert isinstance(form, StepForm)
            self.assertEqual(form.step.id, app.board.tasks[1].steps[0].id)
            await pilot.press("escape")
            self.assertEqual(encode(app.board), before)
            self.assertEqual(encode(self.store.load()), before)

    async def test_validation_failed_save_and_changed_task_keep_form_text(self):
        self.board.add_steps(self.task, ["实现", "核对"])
        self.store.save(self.board)
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            app.open_step(1, app.board.tasks[1].steps[0].id)
            await pilot.pause()
            form = app.screen
            assert isinstance(form, StepForm)
            form.query_one("#step-status", Select).value = "done"
            form.query_one("#step-output", TextArea).text = "保留未提交文字"
            await pilot.press("ctrl+enter")
            self.assertIs(app.screen, form)
            self.assertIn("依据", str(form.query_one("#step-error", Static).content))
            self.assertEqual(app.board.tasks[1].steps[0].status, "pending")
            form.query_one("#step-evidence", TextArea).text = "只读核对说明"
            self.store.connection.execute("PRAGMA query_only=ON")
            await pilot.press("ctrl+enter")
            self.assertIs(app.screen, form)
            self.assertEqual(self.store.load().tasks[1].steps[0].status, "pending")
            self.assertEqual(form.query_one("#step-output", TextArea).text, "保留未提交文字")
            self.store.connection.execute("PRAGMA query_only=OFF")
            app.board.tasks[1].title = "表单期间任务已变"
            await pilot.press("ctrl+enter")
            self.assertIn("已有更新", str(form.query_one("#step-error", Static).content))
            self.assertEqual(form.query_one("#step-output", TextArea).text, "保留未提交文字")
            await pilot.press("escape")
            self.assertFalse(self.runtime.root.exists())

    async def test_retained_steps_are_readonly_and_old_selection_cannot_open(self):
        self.board.add_steps(self.task, ["检查"])
        self.store.save(self.board)  # Save the plan while editable, before retiring it.
        self.board.retire(self.task, "trashed")
        self.store.save(self.board)
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            app.set_filter("trashed")
            await pilot.pause()
            app.action_steps()
            await pilot.pause()
            self.assertIsInstance(app.screen, Choice)
            await pilot.press("enter")
            form = app.screen
            assert isinstance(form, StepForm)
            self.assertIsNone(form.save)
            self.assertFalse(list(form.query("#submit")))
            before = encode(app.board)
            await pilot.press("ctrl+enter", "escape")
            self.assertEqual(encode(app.board), before)
            app.selected_id = None
            app.open_step(1, self.task.steps[0].id)
            self.assertEqual(len(app.screen_stack), 1)

    async def test_append_plan_from_palette_is_explicit_and_preserves_previous_ids(self):
        self.board.add_steps(self.task, ["检查"])
        self.store.save(self.board)
        app = self.app()
        async with app.run_test(size=(120, 30)) as pilot:
            before = deepcopy(app.board.tasks[1].steps)
            await pilot.press("ctrl+k", *"任务步骤", "enter")
            self.assertIsInstance(app.screen, Choice)
            await pilot.press(*"追加", "enter")
            self.assertIsInstance(app.screen, Note)
            app.screen.query_one(TextArea).text = "用户确认的后续"
            await pilot.press("ctrl+enter")
            self.assertEqual(len(app.board.tasks[1].steps), 2)
            self.assertEqual(app.board.tasks[1].steps[:1], before)
            self.assertEqual(app.board.runs, {})
