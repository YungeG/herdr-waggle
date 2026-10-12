"""Mouse copying through real Textual events, capturing OSC 52 rather than the OS clipboard."""
import base64
import unittest
from copy import deepcopy
from unittest.mock import patch

from rich.cells import cell_len
from textual import events
from textual.containers import VerticalScroll
from textual.widgets import Input, Static, TextArea

from app import Confirm, Waggle
from model import Board
from records import TaskRecords
from report_reader import ReportCode, ReportReader


def osc52(text):
    return "\x1b]52;c;" + base64.b64encode(text.encode()).decode() + "\a"


def clipboard_writes(write):
    return [call.args[0] for call in write.call_args_list if call.args[0].startswith("\x1b]52;")]


async def drag(pilot, widget, start, end):
    await pilot.mouse_down(widget, offset=start)
    await pilot.hover(widget, offset=end)
    await pilot.mouse_up(widget, offset=end)
    await pilot.pause()


class CopyingTests(unittest.IsolatedAsyncioTestCase):
    def app(self):
        board = Board()
        board.create("COPY_ME 中文🙂", "收集箱", "编辑文字，不自动复制")
        return Waggle(board=board)

    async def test_detail_drag_copies_only_selected_unicode_on_release_at_two_widths(self):
        for size in ((80, 24), (140, 36)):
            with self.subTest(size=size):
                app = self.app()
                before = deepcopy(vars(app.board))
                async with app.run_test(size=size) as pilot:
                    app.action_toggle_detail()
                    await pilot.pause()
                    assert app._driver is not None
                    with patch.object(app._driver, "write") as write:
                        start, end = cell_len("D001 · "), cell_len("D001 · COPY_ME 中文🙂") - 1
                        await pilot.mouse_down("#detail-heading", offset=(start, 0))
                        await pilot.hover("#detail-heading", offset=(end, 0))
                        self.assertEqual(clipboard_writes(write), [], "Dragging must not stream intermediate selections.")
                        await pilot.mouse_up("#detail-heading", offset=(end, 0))
                        await pilot.pause()
                        self.assertEqual(app.screen.get_selected_text(), "COPY_ME 中文🙂")
                        self.assertEqual(app.clipboard, "COPY_ME 中文🙂")
                        self.assertEqual(clipboard_writes(write), [osc52("COPY_ME 中文🙂")])
                        # Copy the same text again even if another program could have changed the OS clipboard.
                        await drag(pilot, "#detail-heading", (start, 0), (end, 0))
                        self.assertEqual(len(clipboard_writes(write)), 2)
                        app.refresh_status()
                        await pilot.pause()
                        self.assertEqual(len(clipboard_writes(write)), 2)
                        await pilot.press("ctrl+c")
                        self.assertEqual(len(clipboard_writes(write)), 3)
                    self.assertEqual(vars(app.board), before)

    async def test_records_and_confirmation_body_share_copy_behavior(self):
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            assert app._driver is not None
            with patch.object(app._driver, "write") as write:
                app.action_activity()
                await pilot.pause()
                self.assertIsInstance(app.screen, TaskRecords)
                record = app.screen.query_one("#record-body", Static)
                expected = str(record.content).splitlines()[0][:3]
                await drag(pilot, record, (0, 0), (cell_len(expected[:-1]), 0))
                self.assertEqual(clipboard_writes(write), [osc52(expected)])
                await pilot.press("escape")
                app.push_screen(Confirm("只读核对", "BODY_COPY 不应整段复制", "返回"))
                await pilot.pause()
                body = app.screen.query_one(".form-body Static", Static)
                await drag(pilot, body, (0, 0), (8, 0))
                self.assertEqual(clipboard_writes(write)[-1], osc52("BODY_COPY"))
                self.assertEqual(len(clipboard_writes(write)), 2)

    async def test_markdown_and_scrolled_code_drag_keep_position_and_copy_exact_text(self):
        app = self.app()
        body = "PARA_COPY 仅复制片段\n\n" + "段落\n\n" * 8 + "```text\n" + "x" * 100 + "CODE_COPY 末尾\n```"
        async with app.run_test(size=(80, 24)) as pilot:
            reader = ReportReader("复制", body)
            app.push_screen(reader)
            await pilot.pause()
            assert app._driver is not None
            with patch.object(app._driver, "write") as write:
                paragraph = reader.query_one("MarkdownParagraph", Static)
                await drag(pilot, paragraph, (0, 0), (8, 0))
                self.assertEqual(clipboard_writes(write), [osc52("PARA_COPY")])
                scroll = reader.query_one("#report-rendered", VerticalScroll)
                code = reader.query_one(ReportCode)
                scroll.scroll_to(y=10, animate=False)
                code.scroll_to(x=code.max_scroll_x, animate=False)
                await pilot.pause()
                label = code.query_one("Label", Static)
                # Use screen coordinates: the original line begins off-screen after horizontal scrolling.
                start = (label.region.x + label.gutter.left + 100, label.region.y + label.gutter.top)
                position = scroll.scroll_y
                await drag(pilot, None, start, (start[0] + 8, start[1]))
                self.assertEqual(scroll.scroll_y, position, "Focusing code by mouse must not move the selection underneath it.")
                self.assertEqual(clipboard_writes(write)[-1], osc52("CODE_COPY"))
                self.assertEqual(reader.body, body)

    async def test_readonly_source_copies_selection_and_full_copy_preserves_every_character(self):
        app = self.app()
        body = "COPY_RAW 中文🙂\n\n| 列 | 值 |\n| --- | --- |\n| A | B |\n\n" + "完整数据\t" * 200 + "END\n"
        before = deepcopy(vars(app.board))
        async with app.run_test(size=(80, 24)) as pilot:
            reader = ReportReader("只读", body)
            app.push_screen(reader)
            await pilot.pause()
            assert app._driver is not None
            with patch.object(app._driver, "write") as write:
                await pilot.click("#report-toggle")
                source = reader.query_one(TextArea)
                start = (source.gutter.left + source.gutter_width, source.gutter.top)
                await drag(pilot, source, start, (start[0] + 8, start[1]))
                self.assertEqual(source.selected_text, "COPY_RAW")
                self.assertEqual(clipboard_writes(write), [osc52("COPY_RAW")])
                await pilot.press("ctrl+c")
                self.assertEqual(clipboard_writes(write)[-1], osc52("COPY_RAW"))
                self.assertTrue(await pilot.click("#report-copy"))
                self.assertEqual(clipboard_writes(write)[-1], osc52(body))
                self.assertEqual(source.text, body)
                self.assertIn("原文", str(reader.query_one("#report-notice", Static).content))
                self.assertEqual(vars(app.board), before)

    async def test_raw_drag_released_outside_editor_copies_once_and_releases_capture(self):
        app = self.app()
        body = "第一行\n第二行\n第三行"
        async with app.run_test(size=(80, 24)) as pilot:
            app.push_screen(ReportReader("边界", body))
            await pilot.pause()
            await pilot.click("#report-toggle")
            source = app.screen.query_one(TextArea)
            assert app._driver is not None
            with patch.object(app._driver, "write") as write:
                start = (source.gutter.left + source.gutter_width, source.gutter.top)
                await pilot.mouse_down(source, offset=start)
                # The pointer is over the toolbar; TextArea still owns the drag.
                await pilot.hover(offset=(40, 22))
                await pilot.mouse_up(offset=(40, 22))
                await pilot.pause()
                self.assertTrue(source.selected_text)
                self.assertEqual(clipboard_writes(write), [osc52(source.selected_text)])
                self.assertIsNone(app.mouse_captured)
                self.assertEqual(source.text, body)

    async def test_editable_forms_do_not_auto_copy_but_ctrl_c_still_works(self):
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            app.action_edit_task()
            await pilot.pause()
            source = app.screen.query_one(TextArea)
            source.load_text("EDIT_ONLY")
            source.scroll_visible(top=True, animate=False)
            await pilot.pause()
            assert app._driver is not None
            with patch.object(app._driver, "write") as write:
                start = (source.gutter.left + source.gutter_width, source.gutter.top)
                await drag(pilot, source, start, (start[0] + 4, start[1]))
                self.assertEqual(source.selected_text, "EDIT")
                self.assertEqual(clipboard_writes(write), [])
                await pilot.press("ctrl+c")
                self.assertEqual(clipboard_writes(write), [osc52("EDIT")])
                title = app.screen.query_one(Input)
                title.scroll_visible(top=True, animate=False)
                await pilot.pause()
                await drag(pilot, title, (title.gutter.left, title.gutter.top), (title.gutter.left + 4, title.gutter.top))
                self.assertEqual(clipboard_writes(write), [osc52("EDIT")])
                self.assertEqual(source.text, "EDIT_ONLY")

    async def test_click_controls_scrollbars_and_secondary_mouse_do_not_copy(self):
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            app.action_toggle_detail()
            await pilot.pause()
            assert app._driver is not None
            with patch.object(app._driver, "write") as write:
                start, end = cell_len("D001 · "), cell_len("D001 · COPY_ME") - 1
                await drag(pilot, "#detail-heading", (start, 0), (end, 0))
                self.assertEqual(clipboard_writes(write), [osc52("COPY_ME")])
                scroll = app.query_one("#detail-scroll", VerticalScroll)
                self.assertTrue(scroll.show_vertical_scrollbar)
                await pilot.click(scroll.vertical_scrollbar)
                self.assertEqual(len(clipboard_writes(write)), 1)
                scroll.scroll_home(animate=False)
                await pilot.pause()
                await pilot.click("#detail-text", offset=(0, 0))
                self.assertEqual(len(clipboard_writes(write)), 1)
                await pilot.mouse_down("#detail-text", offset=(0, 0), button=3)
                await pilot.hover("#detail-text", offset=(6, 0))
                await pilot._post_mouse_events([events.MouseUp], "#detail-text", offset=(6, 0), button=3)
                await pilot.pause()
                self.assertEqual(len(clipboard_writes(write)), 1)
                await pilot.click("#report-button")
                await pilot.click("#report-close")
                self.assertEqual(len(clipboard_writes(write)), 1)


if __name__ == "__main__":
    unittest.main()
