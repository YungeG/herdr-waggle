"""Full-pane report reading, using only disposable UI, storage and runtime fixtures."""

import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from markdown_it import MarkdownIt
from textual import events
from textual.containers import VerticalScroll
from textual.widgets import Button, DataTable, Markdown, Static, TextArea

from app import Choice, TaskList, Waggle
from delivery import detail_text, round_timeline_text
from report_reader import ReportCode, ReportMarkdown, ReportReader, _reading_markdown
from runtime import Runtime
from store import Store
from task_context import WorkContract
from test_retention import finished_board

HEADERS = [f"指标{index:02}中文" for index in range(12)]
ROWS = [[f"第{row:02}行第{column:02}列🙂" for column in range(12)] for row in range(8)]
ROWS[0][0] = "[bold]原样[/bold]"
TABLE = "| " + " | ".join(HEADERS) + " |\n| " + " | ".join(["---"] * 12) + " |\n"
TABLE += "\n".join("| " + " | ".join(row) + " |" for row in ROWS)
CODE = "data_value_" * 30 + "CODE_END"
REPORT = ("段落自动折行。" * 30 + "\n\n" + TABLE + "\n\n```text\n" + CODE + "\n```\n\n"
          "<b>标签只是文字</b>\n\n[外部链接](https://example.invalid/report)\n\n"
          "[本地链接](file:///not-a-real-report.md)\n\n报告末尾完整保留。\n")


class ReadingFormatTests(unittest.TestCase):
    def test_labels_render_as_strong_and_do_not_drop_limits_or_unicode(self):
        source = "总体目标：保留原始结论\u2028和限制。\n未运行/限制：本轮没有执行测试，不代表通过。\n"
        rendered = _reading_markdown(source)
        parser = MarkdownIt("gfm-like", {"html": False})
        labels = []
        for token in parser.parse(rendered):
            children = token.children or []
            for index, child in enumerate(children[:-1]):
                if child.type == "strong_open":
                    labels.append(children[index + 1].content)
        self.assertIn("总体目标：", labels)
        self.assertIn("未运行/限制：", labels)
        self.assertIn("保留原始结论\u2028和限制。", rendered)
        self.assertIn("本轮没有执行测试，不代表通过。", rendered)

    def test_existing_markdown_code_quotes_tables_and_urls_are_not_rewritten(self):
        source = ("### 本轮交付正文\n\n**总体目标：** 已有粗体保持原样。\n\n"
                  + "`小目标一：行内代码`\n\n> 总体目标：引用原文\n\n"
                  + "- 小目标一：已有列表\n  - 小目标二：嵌套列表\n\n"
                  + "    总体目标：缩进代码\n\n```text\n本轮交付正文\n• 不是列表\n```\n\n"
                  + "| 小目标一： | 状态： |\n| --- | --- |\n| 未运行 | 未验证 |\n\n"
                  + "https://example.invalid/report\n\nC:\\reports\\raw.txt\n")
        self.assertEqual(_reading_markdown(source), source)


class ReportReaderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = Path(self.enterContext(TemporaryDirectory(prefix="waggle-reader-test-")))
        self.runtime = Runtime(self.temp / "bridges")

    def board(self):
        board, task = finished_board()
        task.summary = board.runs[task.run_id].summary = REPORT
        return board, task

    async def test_full_pane_paragraphs_tables_and_code_at_two_sizes(self):
        for size in ((80, 24), (140, 36)):
            with self.subTest(size=size):
                board, task = self.board()
                before = deepcopy(board)
                app = Waggle(board=board, runtime=self.runtime)
                async with app.run_test(size=size) as pilot:
                    await pilot.press("right")
                    self.assertTrue(await pilot.click("#report-button"))
                    await pilot.pause()
                    reader = app.screen
                    assert isinstance(reader, ReportReader)
                    self.assertEqual((reader.size.width, reader.size.height), size)
                    self.assertEqual(reader.body, detail_text(task, board.runs[task.run_id]).plain)
                    self.assertIn(REPORT, reader.body)
                    paragraphs = [widget for widget in reader.query("MarkdownParagraph")
                                  if isinstance(widget, Static) and "段落自动折行" in str(widget.content)]
                    self.assertTrue(paragraphs)
                    self.assertGreater(paragraphs[0].size.height, 1)
                    self.assertEqual(paragraphs[0].max_scroll_x, 0)
                    table = reader.query_one(DataTable)
                    self.assertEqual(table.row_count, len(ROWS))
                    self.assertEqual([cell.plain for cell in table.get_row_at(0)], ROWS[0])
                    self.assertEqual(table.get_row_at(7)[11].plain, ROWS[7][11])
                    self.assertGreater(table.max_scroll_x, 0)
                    table.focus()
                    await pilot.press(*["right"] * 11)
                    self.assertGreater(table.scroll_x, 0)
                    table.scroll_to(x=table.max_scroll_x, animate=False)
                    await pilot.pause()
                    self.assertEqual(table.scroll_x, table.max_scroll_x)
                    self.assertIn(ROWS[7][11], "\n".join(strip.text for strip in reader._compositor.render_strips()))
                    code = reader.query_one(ReportCode)
                    self.assertEqual(code.code, CODE)
                    self.assertGreater(code.max_scroll_x, 0)
                    code.focus()
                    await pilot.press("right", "right")
                    await pilot.pause()
                    self.assertGreater(code.scroll_x, 0)
                    code.scroll_to(x=code.max_scroll_x, animate=False)
                    await pilot.pause()
                    self.assertIn("CODE_END", "\n".join(strip.text for strip in reader._compositor.render_strips()))
                    await pilot.press("left")
                    self.assertIs(app.screen, reader, "Left scrolls code, not the underlying detail pane.")
                    await pilot.press("escape")
                    self.assertEqual(len(app.screen_stack), 1)
                self.assertEqual(vars(board), vars(before))
        self.assertFalse(self.runtime.root.exists())

    async def test_plain_report_gets_headings_labels_and_lists_without_changing_source(self):
        code = "总体目标：这是代码，保留原样\n• 不是列表\n"
        indented = "小目标一：缩进代码，保留原样\n"
        plain = ("总体目标：" + "得到可信结论，不把工程进展当成验证通过。" * 8
                 + "\n小目标一：核对数据与范围。\n小目标二：取得明确批准后才能执行。\n"
                 + "已完成：\n• 恢复原始目标\n• 保留未验证项\n\n"
                 + "```text\n" + code + "```\n\n    " + indented + "\n"
                 + "### 已有 Markdown 标题\n\n- **已有列表**，不要改写\n\n" + REPORT)
        for size in ((80, 24), (140, 36)):
            with self.subTest(size=size):
                board, task = self.board()
                task.summary = board.runs[task.run_id].summary = plain
                before = deepcopy(board)
                app = Waggle(board=board, runtime=self.runtime)
                async with app.run_test(size=size) as pilot:
                    app.action_read_report()
                    await pilot.pause()
                    reader = app.screen
                    assert isinstance(reader, ReportReader)
                    original = reader.body
                    markdown = reader.query_one(ReportMarkdown)
                    headings = [str(item.content) for item in markdown.query("MarkdownH3") if isinstance(item, Static)]
                    self.assertIn("本轮交付正文", headings)
                    self.assertIn("产物与核实依据 · 自报，不是独立验证", headings)
                    self.assertIn("已有 Markdown 标题", headings)
                    paragraphs = [item for item in markdown.query("MarkdownParagraph")
                                  if isinstance(item, Static)]
                    goal = next(item for item in paragraphs if "总体目标：" in str(item.content))
                    self.assertNotIn("小目标一：", str(goal.content))
                    self.assertGreater(goal.size.height, 1)
                    self.assertIn("**总体目标：**", markdown.source)
                    self.assertIn("**小目标一：**", markdown.source)
                    self.assertIn("- 恢复原始目标\n- 保留未验证项", markdown.source)
                    self.assertIn("### 已有 Markdown 标题\n\n- **已有列表**，不要改写", markdown.source)
                    codes = [item.code for item in reader.query(ReportCode)]
                    self.assertIn(code.rstrip("\n"), codes)  # Textual omits the terminal display newline.
                    self.assertIn(indented.rstrip("\n"), codes)
                    self.assertIn("```text\n" + code + "```", markdown.source)
                    self.assertIn("    " + indented, markdown.source)
                    table = reader.query_one(DataTable)
                    self.assertEqual(table.row_count, len(ROWS))
                    self.assertEqual(table.get_row_at(0)[0].plain, ROWS[0][0])
                    with patch.object(app, "copy_to_clipboard") as copy:
                        await pilot.click("#report-copy")
                        copy.assert_called_once_with(original)
                    await pilot.click("#report-toggle")
                    self.assertEqual(reader.query_one("#report-source", TextArea).text, original)
                    self.assertIn(plain, original)
                    self.assertEqual(reader.body, original)
                self.assertEqual(vars(board), vars(before))
        self.assertFalse(self.runtime.root.exists())

    async def test_multiround_snapshot_labels_current_history_and_saved_times(self):
        board, task = self.board()
        previous = board.runs[task.run_id]
        board.return_work(task, "下一轮只核对出处。")
        contract = WorkContract(
            purpose="implementation",
            deliverable="完成整任务交付",
            allowed_scope="项目目录",
            verification="回归测试通过",
            exclusions="不修改其他模块",
        )
        task.remaining = contract.compile()
        current = board.queue_real(task, str(uuid4()), previous.session_id, previous.cwd)
        app = Waggle(board=board, runtime=self.runtime)
        async with app.run_test(size=(120, 30)) as pilot:
            app.action_read_report()
            await pilot.pause()
            reader = app.screen
            assert isinstance(reader, ReportReader)
            self.assertTrue(reader.body.startswith("## 当前轮报告\n\n" + detail_text(task, current).plain))
            self.assertIn("连接提示 · 打开时状态", reader.body)
            self.assertLess(reader.body.index("## 当前轮报告"), reader.body.index("## 轮次与关键时间"))
            self.assertLess(reader.body.index("连接提示 · 打开时状态"), reader.body.index("## 轮次与关键时间"))
            self.assertTrue(reader.body.endswith(round_timeline_text(task, [previous, current], include_heading=False).plain))
            self.assertLess(reader.body.index("第 2 轮 · 本轮 · 等待确认"), reader.body.index("第 1 轮 · 历史 · 执行已结束"))
            self.assertRegex(reader.body, r"20\d\d-\d\d-\d\d \d\d:\d\d:\d\d [+-]\d{4}")
            self.assertIn("完整逐项记录见“记录”", reader.body)
            self.assertNotIn(previous.summary, reader.body)
            self.assertIn("当前轮报告", "\n".join(strip.text for strip in reader._compositor.render_strips()))

    async def test_large_table_keeps_last_row_and_oversize_cell_reachable(self):
        board, task = self.board()
        last_cell = "一格很长的数据" * 30 + "最后一个值"
        report = "| 行号 | 结果 |\n| --- | --- |\n"
        report += "\n".join(f"| {number} | {last_cell if number == 79 else '普通值'} |" for number in range(80))
        task.summary = board.runs[task.run_id].summary = report
        app = Waggle(board=board, runtime=self.runtime)
        async with app.run_test(size=(80, 24)) as pilot:
            app.action_read_report()
            await pilot.pause()
            reader = app.screen
            assert isinstance(reader, ReportReader)
            table = reader.query_one(DataTable)
            self.assertEqual(table.row_count, 80)
            self.assertEqual(table.get_row_at(79)[1].plain, last_cell)
            self.assertGreater(table.max_scroll_y, 0)
            self.assertGreater(table.max_scroll_x, 0)
            table.focus()
            await pilot.press("right", "right", "down")
            self.assertGreater(table.scroll_x, 0)
            table.scroll_to(x=table.max_scroll_x, y=table.max_scroll_y, animate=False)
            await pilot.pause()
            self.assertEqual((table.scroll_x, table.scroll_y), (table.max_scroll_x, table.max_scroll_y))
            self.assertIn("最后一个值", "\n".join(strip.text for strip in reader._compositor.render_strips()))
            self.assertIn(report, reader.body)

    async def test_source_is_readonly_wrap_is_optional_and_links_never_open(self):
        board, task = self.board()
        app = Waggle(board=board, runtime=self.runtime)
        before = deepcopy(board)
        with patch.object(app, "open_url") as open_url, patch.object(Markdown, "load", new_callable=AsyncMock) as load:
            async with app.run_test(size=(80, 24)) as pilot:
                app.action_read_report()
                await pilot.pause()
                reader = app.screen
                assert isinstance(reader, ReportReader)
                markdown = reader.query_one(ReportMarkdown)
                for href in ("https://example.invalid/report", "file:///not-a-real-report.md"):
                    markdown.post_message(Markdown.LinkClicked(markdown, href))
                await pilot.pause()
                open_url.assert_not_called()
                load.assert_not_called()
                await pilot.click("#report-toggle")
                source = reader.query_one(TextArea)
                self.assertIs(app.focused, source)
                self.assertEqual(source.text, reader.body)
                self.assertIn(REPORT, source.text)
                self.assertTrue(source.read_only)
                self.assertFalse(source.soft_wrap)
                self.assertGreater(source.max_scroll_x, 0)
                await pilot.press("ctrl+a", "n", "r", "s", "b", "delete", "ctrl+enter", "ctrl+k")
                source.post_message(events.Paste("不允许修改的内容"))
                await pilot.pause()
                self.assertEqual(source.text, reader.body)
                self.assertIs(app.screen, reader)
                await pilot.click("#report-wrap")
                self.assertTrue(source.soft_wrap)
                self.assertEqual(source.max_scroll_x, 0)
                self.assertEqual(source.text, reader.body)
                await pilot.pause(0.3)  # Textual ignores repeated clicks during the button's active effect.
                await pilot.click("#report-wrap")
                self.assertFalse(source.soft_wrap)
                await pilot.click("#report-toggle")
                self.assertFalse(source.display)
                self.assertTrue(reader.query_one("#report-rendered").display)
                self.assertTrue(reader.query_one("#report-wrap", Button).disabled)
                await pilot.click("#report-close")
                self.assertEqual(len(app.screen_stack), 1)
        self.assertEqual(vars(board), vars(before))
        self.assertEqual(task.status, "review")

    async def test_return_preserves_selection_layout_focus_and_scroll_from_palette(self):
        board, task = self.board()
        task.description = "原有任务说明\n" * 80
        for number in range(25):
            board.create(f"其他任务{number}", task.project)
        app = Waggle(board=board, runtime=self.runtime)
        async with app.run_test(size=(140, 36)) as pilot:
            app.action_toggle_detail()
            await pilot.pause()
            tasks = app.query_one("#task-list", TaskList)
            detail = app.query_one("#detail-scroll", VerticalScroll)
            tasks.scroll_to(y=8, animate=False)
            detail.scroll_to(y=10, animate=False)
            app.query_one("#report-button", Button).focus()
            await pilot.pause()
            saved = (app.selected_id, app.view_mode, app.project_name, app.status_filter,
                     app.detail_open, app.detail_expanded, tasks.scroll_y, detail.scroll_y, app.focused)
            self.assertGreater(tasks.scroll_y, 0)
            self.assertGreater(detail.scroll_y, 0)
            for close in ("keyboard", "mouse"):
                await pilot.press("ctrl+k", *"全屏", "enter")
                await pilot.pause()
                self.assertIsInstance(app.screen, ReportReader)
                if close == "keyboard":
                    await pilot.press("escape")
                else:
                    self.assertTrue(await pilot.click("#report-close"))
                await pilot.pause()
                restored = (app.selected_id, app.view_mode, app.project_name, app.status_filter,
                            app.detail_open, app.detail_expanded, tasks.scroll_y, detail.scroll_y, app.focused)
                self.assertEqual(restored, saved)

    async def test_snapshot_survives_underlying_updates_and_resize(self):
        board, task = self.board()
        app = Waggle(board=board, runtime=self.runtime)
        async with app.run_test(size=(80, 24)) as pilot:
            app.action_read_report()
            await pilot.pause()
            reader = app.screen
            assert isinstance(reader, ReportReader)
            original = reader.body
            scroll = reader.query_one("#report-rendered", VerticalScroll)
            scroll.scroll_to(y=6, animate=False)
            await pilot.pause()
            position = scroll.scroll_y
            # A new saved result may refresh the underlying board, never the open snapshot.
            task.summary = board.runs[task.run_id].summary = "新的报告，仅重新打开才展示。"
            app.refresh_tasks()
            app.refresh_status()
            await pilot.pause()
            self.assertEqual(reader.body, original)
            self.assertEqual(scroll.scroll_y, position)
            await pilot.resize_terminal(140, 36)
            await pilot.pause()
            self.assertEqual(reader.size.width, 140)
            self.assertEqual(reader.body, original)
            self.assertIn("打开时快照", str(reader.query_one("#report-notice", Static).content))
            await pilot.press("escape")
            app.action_read_report()
            await pilot.pause()
            latest = app.screen
            assert isinstance(latest, ReportReader)
            self.assertIn(task.summary, latest.body)
            self.assertNotIn(REPORT, latest.body)

    async def test_reading_never_saves_accepts_dispatches_or_changes_retention(self):
        board, task = self.board()
        board.retire(task, "archived")
        path = self.temp / "tasks.sqlite3"
        with Store(path) as store:
            store.load()
            store.save(board)
            original_bytes = path.read_bytes()
            original = deepcopy(board)
            app = Waggle(store=store, runtime=self.runtime)
            app.status_filter = "archived"
            with patch.object(store, "save", wraps=store.save) as save, patch.object(self.runtime, "dispatch") as dispatch:
                async with app.run_test(size=(80, 24)) as pilot:
                    app.action_read_report()
                    await pilot.pause()
                    self.assertIsInstance(app.screen, ReportReader)
                    await pilot.press("r", "s", "b", "n", "x", "ctrl+enter")
                    self.assertIsInstance(app.screen, ReportReader)
                    await pilot.click("#report-close")
                    self.assertEqual(vars(app.board), vars(original))
                    save.assert_not_called()
                    dispatch.assert_not_called()
            self.assertEqual(path.read_bytes(), original_bytes)
            self.assertEqual(vars(store.load()), vars(original))
        self.assertFalse(self.runtime.root.exists())

    async def test_empty_selection_and_no_current_report_are_explicit(self):
        board, task = self.board()
        previous = task.summary
        board.return_work(task, "下一轮要求，不能冒充上一轮成果")
        app = Waggle(board=board, runtime=self.runtime)
        async with app.run_test(size=(80, 24)) as pilot:
            app.action_read_report()
            await pilot.pause()
            reader = app.screen
            assert isinstance(reader, ReportReader)
            self.assertIn("尚未派发本轮执行", reader.body)
            self.assertIn("上轮仅作历史", reader.body)
            self.assertNotIn(previous, reader.body)
            await pilot.press("escape")
            app.status_filter = "done"
            app.refresh_tasks()
            self.assertIsNone(app.selected_task)
            self.assertTrue(app.query_one("#report-button", Button).disabled)
            app.action_read_report()
            self.assertEqual(len(app.screen_stack), 1)
            app.action_palette()
            await pilot.pause()
            self.assertIsInstance(app.screen, Choice)


if __name__ == "__main__":
    unittest.main()
