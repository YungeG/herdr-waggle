"""Progressive delivery and meaningful records through actual, disposable UI."""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from textual.containers import VerticalScroll
from textual.widgets import Button, Static, Tree

from app import Confirm, TaskRoom
from journal import PREFIX, Brief, Report, encoded_event
from model import Board
from records import TaskRecords
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
        task.remaining = "本轮只核对历史出处，不修改文件。"
        result = deepcopy(board.queue_real(task, str(uuid4()), str(uuid4()), str(self.root)))
        result.state, result.seq, result.delivery = "finished", 40, "full"
        result.summary = "已恢复历史结论，未启动新实验。\n产物：本次答复，没有新建文件。"
        result.checks = ["依据：docs/retained-result.md；历史文档，不是本次回测。", "本次未运行测试，未验证策略收益。"]
        result.events = [
            encoded_event("progress", "执行者报告 · progress", source="executor", run_id=result.id, seq=38,
                          report=Report("早期报告原文。", ["早期出处。"], "仍待最终核对。")),
            encoded_event("full", "执行者报告 · full", source="executor", run_id=result.id, seq=39,
                          report=Report(result.summary, result.checks, "", Brief(["核对历史出处", "恢复已有结论"], "已恢复历史结论", "未运行测试；仅核对历史文档"))),
            encoded_event("settled", "Pi 已完全空闲，仍需人工验收。", source="system", run_id=result.id, seq=40),
        ]
        board.ingest(result)
        self.store.save(board)

    def app(self):
        return TaskRoom(store=self.store, runtime=self.runtime)

    @staticmethod
    def frame(app):
        return "\n".join(strip.text for strip in app.screen._compositor.render_strips())

    @staticmethod
    def content(app):
        return str(app.query_one("#detail-text", Static).content)

    async def test_overview_uses_explicit_brief_and_expand_shows_complete_scope(self):
        before = (self.root / "tasks.sqlite3").read_bytes()
        app = self.app()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.press("right")
            detail = self.content(app)
            self.assertIn("• 核对历史出处\n• 恢复已有结论", detail)
            self.assertIn("未运行测试；仅核对历史文档", detail)
            self.assertNotIn("补充范围说明", detail)
            self.assertNotIn("docs/retained-result.md", detail)
            self.assertNotIn("早期报告原文", detail)
            await pilot.click("#detail-toggle")
            self.assertTrue(app.detail_expanded)
            detail = self.content(app)
            sections = ["本轮交付正文", "产物与核实依据", "任务说明 / 验收范围", "本轮补充要求"]
            positions = [detail.index(section) for section in sections]
            self.assertEqual(positions, sorted(positions))
            self.assertIn("docs/retained-result.md", detail)
            self.assertIn("只查询已有结论，不运行实验。", detail)
            self.assertIn("本轮只核对历史出处，不修改文件。", detail)
            self.assertNotIn(app.board.tasks[1].run_id, detail)
            await pilot.press("r")
            assert isinstance(app.screen, Confirm)
            self.assertIn("docs/retained-result.md", app.screen.body)
            self.assertIn("只查询已有结论，不运行实验。", app.screen.body)
            self.assertIn("本轮只核对历史出处，不修改文件。", app.screen.body)
            self.assertNotIn("最近报告", app.screen.body)
            self.assertNotIn("下一步", app.screen.body)
            await pilot.press("enter")
            self.assertEqual(app.board.tasks[1].status, "review")
        self.assertEqual((self.root / "tasks.sqlite3").read_bytes(), before)
        self.assertFalse(self.runtime.root.exists())

    async def test_records_select_then_snapshot_and_close_preserves_detail(self):
        board = self.store.load()
        run = board.runs[board.tasks[1].run_id]
        run.events += ["工具开始 · read", "工具返回成功 · bash", "Pi · 一般发言"]
        self.store.save(board)
        before = (self.root / "tasks.sqlite3").read_bytes()
        app = self.app()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.press("right")
            await pilot.click("#detail-toggle")
            scroll = app.query_one("#detail-scroll", VerticalScroll)
            scroll.scroll_end(animate=False)
            scroll.focus()
            await pilot.pause()
            position, focused = scroll.scroll_y, app.focused
            app.action_activity()
            await pilot.pause()
            assert isinstance(app.screen, TaskRecords)
            records = app.screen
            tree = records.query_one("#records-tree", Tree)
            group = tree.root.children[0]
            self.assertTrue(group.is_expanded)
            self.assertEqual(len(group.children), 4)  # 3 bridge facts + bound dispatch, no tool/chatter nodes.
            progress = next(node for node in group.children if node.data and node.data[1] and node.data[1].kind == "progress")
            tree.select_node(progress)
            await pilot.pause()
            body = str(records.query_one("#record-body", Static).content)
            self.assertIn("早期报告原文", body)
            self.assertIn("早期出处", body)
            self.assertIn("仍待最终核对", body)
            self.assertNotIn("docs/retained-result.md", body)
            self.assertNotIn("已恢复历史结论", body)
            self.assertIn("执行者自报", body)
            self.assertNotIn("时间未知", body)
            app.board.runs[run.id].summary = "打开后发生的变化，不得污染快照"
            tree.select_node(progress)
            await pilot.pause()
            self.assertNotIn("打开后发生的变化", str(records.query_one("#record-body", Static).content))
            await pilot.press("escape")
            self.assertTrue(app.detail_expanded)
            self.assertEqual(scroll.scroll_y, position)
            self.assertIs(app.focused, focused)
        self.assertEqual((self.root / "tasks.sqlite3").read_bytes(), before)
        self.assertFalse(self.runtime.root.exists())

    async def test_round_groups_order_by_revision_not_clock_and_locator_is_read_only(self):
        board = self.store.load()
        task = board.tasks[1]
        previous = board.runs[task.run_id]
        original = self.root / "original.jsonl"
        original.write_text(json.dumps({"type": "session", "id": previous.session_id, "cwd": previous.cwd}) + "\n")
        previous.events.insert(0, encoded_event("origin", str(original), source="system", run_id=previous.id, seq=1))
        board.return_work(task, "只补录出处。")
        # Clock disagreement must not reorder a human action before the observed final report.
        returned = json.loads(task.events[-1][len(PREFIX):])
        returned["at"] = "2001-01-01T00:00:00.000Z"
        task.events[-1] = PREFIX + json.dumps(returned)
        next_run = board.queue_real(task, str(uuid4()), previous.session_id, previous.cwd)
        self.store.save(board)
        before = (self.root / "tasks.sqlite3").read_bytes(), original.read_bytes()
        app = self.app()
        async with app.run_test(size=(120, 30)) as pilot:
            app.action_activity()
            await pilot.pause()
            assert isinstance(app.screen, TaskRecords)
            records = app.screen
            tree = records.query_one("#records-tree", Tree)
            latest, older = tree.root.children[:2]
            assert latest.data
            self.assertEqual(latest.data[0], next_run.id)
            self.assertTrue(latest.is_expanded)
            self.assertFalse(older.is_expanded)
            returned_data, settled_data = older.children[0].data, older.children[1].data
            assert returned_data and settled_data
            self.assertEqual(returned_data[1].kind, "returned")
            self.assertEqual(settled_data[1].kind, "settled")
            self.assertNotIn("2001", str(older.children[1].label))
            tree.select_node(older)
            await pilot.pause()
            self.assertIn("该轮最后保存的结果", str(records.query_one("#record-body", Static).content))
            await pilot.resize_terminal(80, 24)
            await pilot.pause()
            for button in records.query("Button"):
                self.assertTrue(records.query_one("#dialog").content_region.contains_region(button.region))
            self.assertGreater(records.query_one("#record-body-scroll").size.height, 2)
            await pilot.click("#locate-session")
            await pilot.pause()
            assert isinstance(app.screen, Confirm)
            self.assertIn("已核对原会话", app.screen.body)
            self.assertIn(previous.id, app.screen.body)
            self.assertNotIn(next_run.id, app.screen.body)
            await pilot.press("enter")
            self.assertIs(app.screen, records)
            self.assertEqual(records.selected_run_id, previous.id)
            await pilot.press("escape")
        self.assertEqual(((self.root / "tasks.sqlite3").read_bytes(), original.read_bytes()), before)
        self.assertFalse(self.runtime.root.exists())

    async def test_legacy_reports_keep_only_saved_excerpt_and_never_invent_time_or_evidence(self):
        board = self.store.load()
        run = board.runs[board.tasks[1].run_id]
        run.events = ["较早进展已截断；完整工具记录请查看原 Pi 会话。", "执行者报告 · progress · 旧的片段\n原有换行", "工具开始 · read", "工具返回成功 · bash",
                      "Pi · 普通发言", "执行者报告 · unknown · 不支持的报告", "工具失败 · bash"]
        self.store.save(board)
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("right")
            self.assertIn("已有报告，展开查看", self.content(app))
            self.assertNotIn("已恢复历史结论", self.content(app))
            app.action_activity()
            await pilot.pause()
            assert isinstance(app.screen, TaskRecords)
            records = app.screen
            tree = records.query_one("#records-tree", Tree)
            legacy = next(node for node in tree.root.children[0].children if node.children)
            self.assertEqual(len(legacy.children), 3)
            self.assertIn("部分记录未保留", str(records.query_one("#records-notice", Static).content))
            report_node = next(node for node in legacy.children if node.data and node.data[1].kind == "progress")
            tree.select_node(report_node)
            await pilot.pause()
            body = str(records.query_one("#record-body", Static).content)
            self.assertIn("时间未知", body)
            self.assertIn("旧的片段\n原有换行", body)
            self.assertIn("未保存为快照", body)
            self.assertNotIn("docs/retained-result.md", body)
            self.assertNotIn("本次未运行测试", body)
            self.assertNotIn("已恢复历史结论", body)
            await pilot.press("ctrl+enter")
            self.assertEqual(app.board.tasks[1].status, "review")
            await pilot.press("ctrl+k", *"任务记录", "enter")
            self.assertIsInstance(app.screen, TaskRecords)
            await pilot.press("escape")

    async def test_returned_results_stay_historical_before_and_after_new_dispatch(self):
        board = self.store.load()
        task = board.tasks[1]
        previous_id = task.run_id
        board.return_work(task, "下一轮只补录出处，不重跑实验。")
        board.edit(task, task.title, task.project, "新的任务说明：列出待核对条目。")
        self.store.save(board)
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("right")
            self.assertIn("没有本轮交付", self.content(app))
            self.assertIn("上轮仅作历史", self.content(app))
            await pilot.click("#detail-toggle")
            detail = self.content(app)
            self.assertIn("新的任务说明", detail)
            self.assertIn("待派发的补充要求", detail)
            self.assertIn("下一轮只补录出处", detail)
            self.assertNotIn("已恢复历史结论", detail)
            self.assertNotIn("docs/retained-result.md", detail)
            self.assertNotIn("本轮只核对历史出处", detail)
            self.assertTrue(app.query_one("#accept-button", Button).disabled)
        result = deepcopy(board.queue_real(task, str(uuid4()), str(uuid4()), str(self.root)))
        result.state, result.seq, result.delivery = "working", 1, "progress"
        result.summary, result.remaining = "本轮已开始补录。", "仍需核对末项。"
        result.events = ["执行者报告 · progress · 本轮最新进展。"]
        board.ingest(result)
        self.store.save(board)
        app = self.app()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.press("right")
            self.assertIn("仍需核对末项", self.content(app))
            await pilot.click("#detail-toggle")
            detail = self.content(app)
            self.assertIn("本轮已开始补录", detail)
            self.assertIn("本轮补充要求 · 派发时固定\n下一轮只补录出处", detail)
            self.assertNotIn("已恢复历史结论", detail)
            self.assertNotIn(previous_id, detail)
            self.assertNotIn("docs/retained-result.md", detail)
        self.assertFalse(self.runtime.root.exists())

    async def test_report_before_idle_permission_wait_and_failure_never_offer_acceptance(self):
        board = self.store.load()
        task = board.tasks[1]
        board.return_work(task, "补全出处。")
        result = deepcopy(board.queue_real(task, str(uuid4()), str(uuid4()), str(self.root)))
        result.summary, result.checks = "完整内容已自报，但尚未空闲。", ["自报依据，未独立验证。"]
        result.events = [encoded_event("full", "执行者报告 · full", source="executor", run_id=result.id, seq=1,
                                      report=Report(result.summary, result.checks, "", Brief(["报告已提交"], "仅代表自报", "未独立核实")))]
        states = [("working", "full", "等待 Pi 完全空闲"), ("waiting", "full", "到 Pi 处理权限或交互确认"),
                  ("failed", "", "核实阻塞原因和执行状态")]
        for state, delivery, next_step in states:
            with self.subTest(state=state):
                result = deepcopy(result)
                result.state, result.delivery, result.seq = state, delivery, result.seq + 1
                if state == "failed":
                    result.summary = "执行失败，较早的完整报告不再有效。"
                board.ingest(result)
                self.store.save(board)
                before = (self.root / "tasks.sqlite3").read_bytes()
                app = self.app()
                async with app.run_test(size=(80, 24)) as pilot:
                    await pilot.press("right")
                    self.assertIn(next_step, self.content(app))
                    self.assertTrue(app.query_one("#accept-button", Button).disabled)
                    if state != "failed":
                        self.assertIn("心跳未知/失联", self.frame(app))
                        self.assertIn("• 报告已提交", self.content(app))
                        self.assertNotIn("补全出处", self.content(app))
                    else:
                        self.assertNotIn("仅代表自报", self.content(app))
                    await pilot.press("r")
                    self.assertNotIsInstance(app.screen, Confirm)
                    self.assertNotEqual(app.board.tasks[1].status, "review")
                self.assertEqual((self.root / "tasks.sqlite3").read_bytes(), before)
        self.assertFalse(self.runtime.root.exists())

    async def test_expanded_delivery_evidence_and_acceptance_are_never_clipped(self):
        board = self.store.load()
        task = board.tasks[1]
        run = board.runs[task.run_id]
        task.summary = run.summary = "完整正文。\n" * 80 + "交付全文结尾。"
        task.checks = run.checks = ["完整证据路径/" * 60 + "证据全文结尾。"]
        self.store.save(board)
        app = self.app()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.press("right")
            self.assertIn("已有报告，展开查看", self.content(app))  # Old brief no longer matches current result.
            self.assertNotIn("已恢复历史结论", self.content(app))
            await pilot.click("#detail-toggle")
            self.assertIn("交付全文结尾。", self.content(app))
            self.assertIn("证据全文结尾。", self.content(app))
            self.assertIn("完整正文。\n完整正文。", self.content(app))
            await pilot.press("r")
            assert isinstance(app.screen, Confirm)
            self.assertIn("交付全文结尾。", app.screen.body)
            self.assertIn("证据全文结尾。", app.screen.body)
            await pilot.press("enter")
            self.assertEqual(app.board.tasks[1].status, "review")

    async def test_simulation_is_explicit_and_remaining_is_not_mislabeled_as_input(self):
        board = Board()
        task = board.create("模拟分轮", "数据实验室", "模拟任务要求。")
        board.assign(task, "Pi · 数据助手（虚构）")
        board.queue(task)
        board.simulate(task, "ack")
        board.simulate(task, "partial")
        app = TaskRoom(board)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.press("right")
            self.assertIn("阶段交付", self.content(app))
            self.assertIn("第二阶段实现与验证尚未完成", self.content(app))
            self.assertNotIn("待派发的补充要求", self.content(app))
            self.assertIn("全部虚构", self.content(app))
        board.return_work(task, "补全第二阶段。")
        board.queue(task)
        board.simulate(task, "ack")
        board.simulate(task, "progress")
        app = TaskRoom(board)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.press("right")
            self.assertNotIn("阶段交付", self.content(app))
            self.assertIn("全部虚构", self.content(app))

    async def test_reopening_resets_to_compact_first_screen_with_actions_and_verification(self):
        for size in ((165, 51), (80, 24), (120, 30)):
            with self.subTest(size=size):
                app = self.app()
                async with app.run_test(size=size) as pilot:
                    await pilot.press("enter")
                    self.assertIn("已恢复历史结论", self.frame(app))
                    self.assertIn("未运行测试", self.frame(app))
                    await pilot.click("#detail-toggle")
                    scroll = app.query_one("#detail-scroll", VerticalScroll)
                    scroll.scroll_end(animate=False)
                    await pilot.pause()
                    self.assertGreater(scroll.scroll_y, 0)
                    await pilot.press("escape", "enter")
                    self.assertFalse(app.detail_expanded)
                    self.assertEqual(scroll.scroll_y, 0)
                    self.assertIn("已恢复历史结论", self.frame(app))
                    for button in app.query("#detail-actions Button"):
                        self.assertTrue(app.query_one("#inspector").content_region.contains_region(button.region))

    async def test_partial_delivery_and_live_refresh_keep_safety_and_expanded_scroll(self):
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
            await pilot.click("#detail-toggle")
            scroll = app.query_one("#detail-scroll", VerticalScroll)
            scroll.scroll_end(animate=False)
            await pilot.pause()
            old_scroll, focused = scroll.scroll_y, app.focused
            self.assertGreater(old_scroll, 0)
            app.refresh_tasks()
            await pilot.pause()
            self.assertTrue(app.detail_expanded)
            self.assertEqual(scroll.scroll_y, old_scroll)
            self.assertIs(app.focused, focused)
            await pilot.press("r")
            self.assertNotIsInstance(app.screen, Confirm)
            self.assertEqual(app.board.tasks[1].status, "review")


if __name__ == "__main__":
    unittest.main()
