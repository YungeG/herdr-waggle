"""Behavioral acceptance for the disposable prototype, not for Herdr/real agents."""
import unittest

from textual import events
from textual.containers import VerticalScroll
from textual.widgets import Button, Input, OptionList, Select, Static, TextArea

from app import Choice, Confirm, Note, TaskForm, TaskRoom
from model import AGENTS, PROJECTS, Board, sample_board


class ModelTests(unittest.TestCase):
    def test_assignment_queue_ack_delivery_accept_are_separate(self):
        board = Board()
        task = board.create("测试", "终端工作台")
        self.assertEqual((task.status, task.run), ("draft", "none"))
        with self.assertRaises(ValueError):
            board.queue(task)
        board.assign(task, AGENTS[0])
        self.assertEqual((task.status, task.run), ("ready", "none"))
        board.queue(task)
        self.assertEqual((task.status, task.run), ("ready", "queued"))
        with self.assertRaises(ValueError):
            board.queue(task)
        with self.assertRaises(ValueError):
            board.assign(task, AGENTS[1])
        with self.assertRaises(ValueError):
            board.simulate(task, "full")
        board.simulate(task, "ack")
        board.simulate(task, "full")
        self.assertEqual(task.status, "review")
        board.accept(task)
        self.assertEqual(task.status, "done")

    def test_partial_delivery_cannot_accept_and_return_does_not_restart(self):
        board = sample_board()
        task = board.tasks[12]
        with self.assertRaises(ValueError):
            board.accept(task)
        with self.assertRaises(ValueError):
            board.return_work(task, " ")
        board.return_work(task, "完成第二阶段")
        self.assertEqual((task.status, task.run), ("ready", "none"))
        self.assertEqual(task.remaining, "完成第二阶段")
        self.assertFalse(task.can_accept)

    def test_inbox_unknown_project_and_blank_title_are_safe(self):
        board = Board()
        for title, project in [(" ", "收集箱"), ("test", "/real/project")]:
            with self.assertRaises(ValueError):
                board.create(title, project)
        task = board.create("[bold]这是字面文本[/bold]", "收集箱")
        board.assign(task, AGENTS[0])
        with self.assertRaises(ValueError):
            board.queue(task)
        self.assertEqual(task.run, "none")

    def test_active_execution_cannot_change_directory(self):
        board = sample_board()
        task = board.tasks[2]
        before = (task.title, task.project)
        with self.assertRaises(ValueError):
            board.edit(task, "changed", "个人事务", "")
        self.assertEqual((task.title, task.project), before)
        board.edit(task, "补充标题", task.project, "说明")
        self.assertEqual(task.status, "running")

    def test_fixture_is_fresh_and_every_directory_is_fictional(self):
        first, second = sample_board(), sample_board()
        first.tasks[1].events.append("修改")
        self.assertNotIn("修改", second.tasks[1].events)
        self.assertEqual(len(second.tasks), 12)
        self.assertTrue(all(path.startswith("/demo/") for key, path in PROJECTS.items() if key != "收集箱"))


class KeyboardTests(unittest.IsolatedAsyncioTestCase):
    async def test_horizontal_arrows_open_detail_and_restore_task_position(self):
        for width, board_view in ((80, False), (139, False), (180, False), (180, True), (80, True)):
            with self.subTest(width=width, board=board_view):
                board = Board()
                for index in range(20):
                    board.create(f"任务 {index + 1}", "终端工作台", "较长的任务说明\n" * 50)
                app = TaskRoom(board)
                async with app.run_test(size=(width, 24)) as pilot:
                    if board_view:
                        await pilot.press("v")
                    selector = "#lane-draft" if board_view and width >= 112 else "#task-list"
                    cards = app.query_one(selector, OptionList)
                    await pilot.press(*["down"] * 8)
                    await pilot.pause()
                    self.assertEqual(app.selected_id, 9)
                    position = cards.scroll_y
                    self.assertGreater(position, 0)
                    await pilot.press("right")
                    detail = app.query_one("#detail-scroll", VerticalScroll)
                    self.assertTrue(detail.has_focus)
                    self.assertTrue(app.query_one("#inspector").display)
                    self.assertEqual(app.selected_id, 9)
                    await pilot.click("#detail-toggle")
                    self.assertTrue(app.detail_expanded)
                    await pilot.press("down", "down", "down")
                    await pilot.pause()
                    self.assertGreater(detail.scroll_y, 0)
                    scrolled = detail.scroll_y
                    await pilot.press("up")
                    await pilot.pause()
                    self.assertLess(detail.scroll_y, scrolled)
                    self.assertEqual(app.selected_id, 9)
                    await pilot.press("left")
                    self.assertTrue(cards.has_focus)
                    self.assertEqual(cards.highlighted, 8)
                    self.assertEqual(cards.scroll_y, position)
                    await pilot.press("down")
                    self.assertEqual(app.selected_id, 10)
                    await pilot.press("enter", "escape")
                    self.assertTrue(cards.has_focus)
                    self.assertEqual(app.selected_id, 10)
                    self.assertTrue(all(task.run == "none" for task in board.tasks.values()))

    async def test_horizontal_arrows_do_not_leave_text_inputs_or_modals(self):
        app = TaskRoom()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("right", "slash", *"D001", "left")
            search = app.query_one("#search", Input)
            self.assertTrue(search.has_focus)
            self.assertEqual(search.cursor_position, 3)
            await pilot.press("right")
            self.assertEqual(search.cursor_position, 4)
            self.assertTrue(app.detail_open)
            await pilot.press("escape", "right", "n", *"ab", "left")
            form = app.screen
            self.assertIsInstance(form, TaskForm)
            title = form.query_one("#task-title", Input)
            self.assertEqual(title.cursor_position, 1)
            await pilot.press("right")
            self.assertEqual(title.cursor_position, 2)
            area = form.query_one(TextArea)
            area.focus()
            await pilot.press(*"ab", "left")
            self.assertEqual(area.cursor_location, (0, 1))
            await pilot.press("right")
            self.assertEqual(area.cursor_location, (0, 2))
            self.assertIs(app.screen, form)
            self.assertTrue(app.detail_open)
            await pilot.press("escape", "f", "down")
            picker = app.screen
            self.assertIsInstance(picker, Choice)
            choices = picker.query_one("#choices", OptionList)
            highlighted = choices.highlighted
            await pilot.press("left", "right")
            self.assertIs(app.screen, picker)
            self.assertTrue(choices.has_focus)
            self.assertEqual(choices.highlighted, highlighted)
            self.assertEqual(app.status_filter, "all")
            self.assertTrue(app.detail_open)
            await pilot.press("escape", "r")
            confirmation = app.screen
            self.assertIsInstance(confirmation, Confirm)
            await pilot.press("left", "right")
            self.assertIs(app.screen, confirmation)
            self.assertTrue(confirmation.query_one("#cancel").has_focus)
            self.assertTrue(app.detail_open)
            self.assertEqual(app.board.tasks[1].status, "review")
            await pilot.press("escape", "left")
            self.assertTrue(app.query_one("#task-list").has_focus)
            self.assertEqual(len(app.board.tasks), 12)

    async def test_left_returns_from_detail_actions_without_closing_sidebar(self):
        app = TaskRoom()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.click("#sidebar-button")
            nav = app.query_one("#nav-status", OptionList)
            await pilot.press("down", "right", "left")
            self.assertTrue(nav.has_focus)
            self.assertEqual(app.status_filter, "all")
            self.assertTrue(app.sidebar_open)
            cards = app.query_one("#task-list", OptionList)
            cards.focus()
            await pilot.press("right", "tab")
            self.assertTrue(app.query_one("#accept-button").has_focus)
            await pilot.press("left")
            self.assertTrue(cards.has_focus)
            self.assertEqual(app.selected_id, 1)
            self.assertFalse(app.detail_open)
            self.assertTrue(app.sidebar_open)

    async def test_modal_buttons_visible_and_keyboard_scroll_can_submit_at_80x24(self):
        app = TaskRoom()
        async with app.run_test(size=(80, 24)) as pilot:
            for key in ("n", "s", "ctrl+k", "b"):
                await pilot.press(key)
                await pilot.pause()
                dialog = app.screen.query_one("#dialog")
                for button in app.screen.query(Button):
                    self.assertTrue(dialog.content_region.contains_region(button.region), f"{key}: clipped {button.id}")
                await pilot.press("escape")
            await pilot.press("n", *"键盘滚动")
            area = app.screen.query_one(TextArea)
            area.focus()
            # Pilot 8.2.8 drops full-width punctuation in press(); paste models it exactly.
            app.post_message(events.Paste("Tab 也能提交，不依赖 Ctrl+Enter"))
            await pilot.pause()
            await pilot.press("tab", "tab", "enter")
            self.assertEqual(app.board.tasks[13].description, "Tab 也能提交，不依赖 Ctrl+Enter")

    async def test_render_keeps_last_card_line_and_prioritizes_delivery(self):
        app = TaskRoom()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            rendered = "\n".join(strip.text for strip in app.screen._compositor.render_strips())
            self.assertIn("模拟 · 本轮结束，验收独立确认", rendered)
            self.assertIn("阶段交付，仍有剩余工作", app.card(app.board.tasks[12]).plain)
            self.assertIn("人工已验收", app.card(app.board.tasks[9]).plain)
            self.assertIn("已有报告，展开查看", rendered)
            await pilot.click("#detail-toggle")
            await pilot.pause()
            rendered = "\n".join(strip.text for strip in app.screen._compositor.render_strips())
            self.assertIn("样例方案和回归说明已整理", rendered)
            await pilot.press("slash", *"D003", "escape", "enter")
            await pilot.pause()
            rendered = "\n".join(strip.text for strip in app.screen._compositor.render_strips())
            self.assertIn("需要你确认样例口径", rendered)

    async def test_board_tab_changes_target_and_transition_follows_same_task(self):
        app = TaskRoom()
        async with app.run_test(size=(180, 40)) as pilot:
            await pilot.press("v", "shift+tab")
            self.assertEqual(getattr(app.focused, "id", None), "lane-running")
            self.assertEqual(app.selected_id, 2)
            app.mutate(lambda board: board.simulate(board.tasks[2], "full"))
            await pilot.pause()
            self.assertEqual(app.selected_id, 2)
            self.assertEqual(getattr(app.focused, "id", None), "lane-review")
            await pilot.press("r")
            self.assertIsInstance(app.screen, Confirm)

    async def test_empty_results_escape_then_new_and_initial_empty_board(self):
        for board in (Board(), sample_board()):
            app = TaskRoom(board)
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.press("slash", *"不存在的任务", "escape", "left", "right", "n")
                self.assertIsInstance(app.screen, TaskForm)

    async def test_full_keyboard_journey_with_partial_return_and_accept(self):
        app = TaskRoom()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.press("p", *"终端工作台", "enter", "n")
            self.assertIsInstance(app.screen, TaskForm)
            self.assertEqual(app.screen.query_one(Select).value, "终端工作台")
            title = "键盘旅程 nvasfg / 中文标题"
            await pilot.press(*title, "enter")
            task = app.board.tasks[13]
            self.assertEqual(task.title, title)
            self.assertEqual((task.status, task.run, app.selected_id), ("draft", "none", 13))
            await pilot.press("a", "enter")
            self.assertEqual((task.status, task.run), ("ready", "none"))
            await pilot.press("s", "enter")  # Default is cancel, not destructive/implicit confirmation.
            self.assertEqual(task.run, "none")
            await pilot.press("s", "tab", "enter")
            self.assertEqual(task.run, "queued")
            await pilot.press("g", "enter")
            self.assertEqual(task.status, "running")
            await pilot.press("g", *"阶段", "enter")
            self.assertEqual(task.status, "review")
            self.assertFalse(task.can_accept)
            await pilot.press("r")
            self.assertNotIsInstance(app.screen, Confirm)
            self.assertEqual(task.status, "review")
            await pilot.press("b", *"补齐第二阶段 nvasfg", "ctrl+enter")
            self.assertEqual(task.remaining, "补齐第二阶段 nvasfg")
            self.assertEqual(task.run, "none")
            await pilot.press("s", "ctrl+enter", "g", "enter", "g", *"完整", "enter")
            self.assertTrue(task.can_accept)
            self.assertEqual(task.status, "review")
            await pilot.press("r", "ctrl+enter")
            self.assertEqual(task.status, "done")
            self.assertEqual(app.selected_id, 13)

    async def test_search_palette_empty_results_and_typing_shortcuts(self):
        app = TaskRoom()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("slash", *"nvasfg")
            self.assertEqual(app.query_one("#search", Input).value, "nvasfg")
            self.assertEqual(len(app.board.tasks), 12)
            self.assertEqual(app.view_mode, "list")
            self.assertIsNone(app.selected_id)
            self.assertTrue(app.query_one("#empty").display)
            await pilot.press("home", "shift+end", "backspace", *"D003", "escape", "enter")
            self.assertEqual(app.selected_id, 3)
            self.assertTrue(app.query_one("#inspector").display)
            self.assertFalse(app.query_one("#task-region").display)
            await pilot.press("escape", "ctrl+k", *"补充", "enter")
            self.assertIsInstance(app.screen, Note)
            await pilot.press("ctrl+enter")  # Required reason.
            self.assertIsInstance(app.screen, Note)
            await pilot.press(*"按事件时间", "enter", *"保留原始字段", "ctrl+enter")
            self.assertEqual(app.board.tasks[3].status, "ready")
            self.assertEqual(app.board.tasks[3].run, "none")

    async def test_filter_project_and_escape_do_not_clear_search(self):
        app = TaskRoom()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("p", *"数据实验室", "enter", "f", *"阻塞", "enter")
            self.assertEqual([task.id for task in app.visible_tasks()], [3])
            await pilot.press("slash", *"口径", "escape", "v")
            self.assertEqual(app.query_one("#search", Input).value, "口径")
            self.assertEqual(app.selected_id, 3)
            self.assertEqual(app.view_mode, "board")

    async def test_resize_same_data_selection_and_scroll(self):
        app = TaskRoom()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("down")
            self.assertEqual(app.selected_id, 2)
            await pilot.press("enter")
            await pilot.click("#detail-toggle")
            await pilot.press("pagedown")
            await pilot.pause()
            self.assertGreater(app.query_one("#detail-scroll", VerticalScroll).scroll_y, 0)
            for width, height in [(120, 30), (180, 40), (80, 24)]:
                await pilot.resize_terminal(width, height)
                await pilot.pause()
                self.assertEqual(app.selected_id, 2)
                self.assertTrue(app.query_one("#inspector").display)
                self.assertGreater(app.query_one("#detail-scroll").size.height, 3)
                self.assertTrue(app.query_one("#detail-actions").region.bottom <= height - 2)
            await pilot.press("escape", "v")
            for width, height in [(120, 30), (180, 40), (80, 24)]:
                await pilot.resize_terminal(width, height)
                await pilot.pause()
                self.assertEqual(app.selected_id, 2)
                self.assertEqual(app.query_one("#board").display, width >= 112)
            self.assertEqual(len(app.board.tasks), 12)

    async def test_simulated_update_preserves_task_id_editor_text_and_focus(self):
        app = TaskRoom()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.press("down", "e")
            form = app.screen
            self.assertIsInstance(form, TaskForm)
            title = form.query_one("#task-title", Input)
            await pilot.press("home", "shift+end", *"编辑未提交 nvasfg")
            focused = app.focused
            app.mutate(lambda board: board.simulate(board.tasks[11], "ack"))
            await pilot.pause()
            self.assertEqual(app.selected_id, 2)
            self.assertEqual(title.value, "编辑未提交 nvasfg")
            self.assertIs(app.focused, focused)
            await pilot.press("escape", "slash", *"数据")
            selected = app.selected_id
            app.mutate(lambda board: board.simulate(board.tasks[2], "progress"))
            await pilot.pause()
            self.assertEqual(app.selected_id, selected)
            self.assertEqual(app.query_one("#search", Input).value, "数据")
            self.assertIsInstance(app.focused, Input)

    async def test_board_movement_and_update_preserve_identity(self):
        app = TaskRoom()
        async with app.run_test(size=(180, 40)) as pilot:
            await pilot.press("v")
            lane = app.query_one("#lane-review", OptionList)
            lane.focus()
            await pilot.press("down")
            self.assertEqual(app.selected_id, 12)
            app.mutate(lambda board: board.simulate(board.tasks[2], "full"))
            await pilot.pause()
            self.assertEqual(app.selected_id, 12)
            assert lane.highlighted is not None
            self.assertEqual(lane.get_option_at_index(lane.highlighted).id, "task-12")
            await pilot.press("enter")
            self.assertTrue(app.query_one("#inspector").display)
            self.assertTrue(app.query_one("#accept-button", Button).disabled)

    async def test_creation_validation_multiline_markup_literal_and_cancel(self):
        app = TaskRoom()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("n", "enter")
            self.assertIsInstance(app.screen, TaskForm)
            await pilot.press(*"[bold]中文长标题[/bold]")
            area = app.screen.query_one(TextArea)
            area.focus()
            await pilot.press(*"第一行nvasfg", "enter", *"第二行", "ctrl+enter")
            task = app.board.tasks[13]
            self.assertEqual(task.project, "收集箱")
            self.assertEqual(task.description, "第一行nvasfg\n第二行")
            self.assertIn("[bold]", app.card(task).plain)
            await pilot.press("n", *"不要保存", "escape")
            self.assertEqual(len(app.board.tasks), 13)


class MouseTests(unittest.IsolatedAsyncioTestCase):
    async def test_clicking_back_from_detail_keeps_task_focus_after_resize(self):
        app = TaskRoom()
        async with app.run_test(size=(139, 50)) as pilot:
            await pilot.press("down", "enter")
            self.assertTrue(app.query_one("#detail-scroll").has_focus)
            await pilot.click("#task-list", offset=(4, 1))
            self.assertEqual(app.selected_id, 1)
            self.assertTrue(app.query_one("#task-list").has_focus)
            await pilot.resize_terminal(140, 50)
            self.assertTrue(app.query_one("#task-list").has_focus)
            self.assertFalse(app.detail_open)
            await pilot.press("down")
            self.assertEqual(app.selected_id, 2)

    async def test_status_button_uses_all_three_toolbar_rows(self):
        app = TaskRoom()
        async with app.run_test(size=(139, 50)) as pilot:
            for row in (0, 1, 2):
                with self.subTest(row=row):
                    await pilot.click("#search")
                    self.assertTrue(await pilot.click("#filter-button", offset=(3, row)))
                    self.assertIsInstance(app.screen, Choice)
                    await pilot.click("#choices", offset=(3, 2))
                    self.assertEqual(app.status_filter, "ready")
                    self.assertEqual(len(app.screen_stack), 1)

    async def test_narrow_sidebar_can_open_filter_and_close_without_losing_resize_focus(self):
        app = TaskRoom()
        async with app.run_test(size=(80, 24)) as pilot:
            sidebar = app.query_one("#sidebar")
            toggle = app.query_one("#sidebar-button", Button)
            self.assertFalse(sidebar.display)
            self.assertTrue(toggle.display)
            await pilot.click("#sidebar-button")
            self.assertTrue(sidebar.display)
            self.assertTrue(app.query_one("#nav-status").has_focus)
            await pilot.click("#nav-status", offset=(2, 1))
            self.assertEqual(app.status_filter, "review")
            await pilot.click("#nav-project", offset=(2, 3))
            self.assertEqual(app.project_name, "终端工作台")
            selected = app.selected_id
            await pilot.click("#sidebar-button")
            self.assertFalse(sidebar.display)
            self.assertTrue(toggle.has_focus)
            # Textual ignores a second click during the button's pressed effect.
            await pilot.pause(toggle.active_effect_duration)
            await pilot.click("#sidebar-button")
            self.assertTrue(sidebar.display)
            await pilot.press("escape")
            self.assertFalse(sidebar.display)
            self.assertTrue(toggle.has_focus)
            self.assertEqual(app.selected_id, selected)
            await pilot.resize_terminal(140, 24)
            self.assertTrue(sidebar.display)
            self.assertFalse(toggle.display)
            self.assertTrue(app.query_one("#nav-status").has_focus)
            await pilot.resize_terminal(139, 24)
            self.assertFalse(sidebar.display)
            self.assertTrue(toggle.display)
            self.assertTrue(toggle.has_focus)
            self.assertEqual(app.status_filter, "review")
            self.assertEqual(app.project_name, "终端工作台")

    async def test_single_click_selects_and_keeps_task_focus(self):
        for width, board_view in ((80, False), (139, False), (180, False), (180, True), (80, True)):
            with self.subTest(width=width, board=board_view):
                board = Board()
                board.create("第一项", "终端工作台")
                board.create("第二项", "终端工作台")
                app = TaskRoom(board)
                async with app.run_test(size=(width, 24)) as pilot:
                    if board_view:
                        await pilot.press("v")
                    selector = "#lane-draft" if board_view and width >= 112 else "#task-list"
                    cards = app.query_one(selector)
                    await pilot.press("down")
                    self.assertEqual(app.selected_id, 2)
                    await pilot.click("#search")
                    await pilot.click(selector, offset=(4, 1))
                    self.assertEqual(app.selected_id, 1)
                    self.assertTrue(cards.has_focus)
                    self.assertFalse(app.detail_open)
                    await pilot.press("down")
                    self.assertEqual(app.selected_id, 2)
                    await pilot.click(selector, offset=(4, 1), button=3)
                    await pilot.click(selector, offset=(4, 10))  # Blank space below both cards.
                    self.assertEqual(app.selected_id, 2)
                    self.assertFalse(app.detail_open)
                    await pilot.resize_terminal(width + 1, 24)
                    self.assertEqual(app.selected_id, 2)
                    self.assertTrue(cards.has_focus)
                    await pilot.press("enter")
                    self.assertTrue(app.detail_open)
                    self.assertTrue(app.query_one("#detail-scroll").has_focus)
                    await pilot.press("escape")
                    self.assertTrue(cards.has_focus)
                    await pilot.double_click(selector, offset=(4, 1))
                    self.assertEqual(app.selected_id, 1)
                    self.assertTrue(app.detail_open)
                    self.assertTrue(app.query_one("#detail-scroll").has_focus)
                    self.assertTrue(all(task.run == "none" for task in board.tasks.values()))


if __name__ == "__main__":
    unittest.main()
