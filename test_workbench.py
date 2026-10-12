"""Action-first main workbench; scratch data only, never a real Pi connection."""
import asyncio
import json
import os
import time
import unittest
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import patch
from uuid import uuid4

from textual.containers import VerticalScroll
from textual.widgets import (
    Button,
    Collapsible,
    OptionList,
    Select,
    Static,
    TextArea,
)

from app import Choice, Confirm, Note, Waggle
from decision_view import DecisionSheet
from decisions import DecisionSpec, review_requirements
from delivery import detail_text, overview_text
from journal import Brief, Report, current_report, encoded_event
from records import TaskRecords
from report_reader import ReportReader
from runtime import Agent, Runtime, private_dir
from store import Store, encode
from task_actions import decision_items
from task_context import WorkContract
from test_capabilities import capability_record
from test_continuations import session_record
from test_decisions import spec_record
from test_live import wait_ui
from test_observation import files


class WorkbenchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory(prefix="waggle-workbench-")))
        self.store = self.enterContext(Store(self.root / "tasks.sqlite3"))
        self.runtime = Runtime(self.root / "bridges")
        board = self.store.load()
        board.add_project("研究", str(self.root))
        for title, kind in (("能力审计", "partial"), ("需要明确范围", "blocked"), ("完整交付", "full")):
            task = board.create(title, "研究", "原始要求不改写。")
            board.assign(task, str(uuid4()))
            contract = WorkContract(
                purpose="implementation",
                deliverable="完成整任务交付",
                allowed_scope="项目目录",
                verification="回归测试通过",
                exclusions="不修改其他模块",
            )
            task.remaining = contract.compile()
            run = deepcopy(board.queue_real(task, str(uuid4()), str(uuid4()), str(self.root)))
            run.state, run.seq, run.delivery = "failed" if kind == "blocked" else "finished", 2, kind
            run.summary, run.checks = "本轮完成能力审计，不代表策略通过。", ["静态检查记录；未运行正式实验。"]
            run.remaining = "" if kind == "full" else "需固定信号定义和输入范围。\n" + "完整性、可用性和样本隔离尚未核验。\n" * 12 + "独立 OOS 未运行；不要直接部署。"
            brief = Brief(["核查数据", "检查公共入口"], "能力审计完成，正式验证未完成。", "仅静态核查，无实验结论。")
            run.events = [encoded_event(kind, "自报", source="executor", run_id=run.id, seq=1,
                                       report=Report(run.summary, run.checks, run.remaining, brief)),
                          encoded_event("settled" if kind != "blocked" else "failed", "结束", source="system", run_id=run.id, seq=2)]
            board.ingest(run)
        self.store.save(board)
        self.before = deepcopy(board)

    def bind_idle_owner(self, task_id=1):
        board = self.store.load()
        run = board.runs[board.tasks[task_id].run_id]
        agent = Agent(run.agent_id, run.session_id, run.cwd, "隔离所有权夹具", "offline/fixture",
                      os.getpid(), int(time.time() * 1000), True, True, "")
        endpoint = self.runtime.root / agent.id
        for path in (self.runtime.root, endpoint, endpoint / "commands", endpoint / "runs"):
            private_dir(path, create=True)
        session_path = endpoint / "session.json"
        revision = json.loads(session_path.read_text())["seq"] if session_path.exists() else 0
        for name, row in (("agent.json", {"version": 1, **asdict(agent)}),
                          ("session.json", session_record(run, seq=revision + 1)),
                          ("report-capabilities.json", capability_record(agent))):
            path = endpoint / name
            path.write_text(json.dumps(row), encoding="utf-8")
            path.chmod(0o600)
        self.runtime.check_new_round(run)
        return files(self.runtime.root)

    def new_terminal_report(self, *, specs=(), remaining=None, detail="新夹具报告"):
        board = self.store.load()
        task = board.tasks[1]
        previous = deepcopy(board.runs[task.run_id])
        board.save_supplement(task, previous.requirements, "next")
        board.assign(task, previous.agent_id)
        run = deepcopy(board.queue_real(task, str(uuid4()), previous.session_id, previous.cwd))
        self.store.save(board)  # Persist the genuine queued identity before its terminal report.
        run.state, run.seq, run.delivery = "finished", 2, "partial"
        run.summary, run.checks = previous.summary, list(previous.checks)
        run.remaining = previous.remaining if remaining is None else remaining
        run.events = [encoded_event("partial", detail, source="executor", run_id=run.id, seq=1,
                                    report=Report(run.summary, run.checks, run.remaining, decisions=specs or None)),
                      encoded_event("settled", "新轮结束", source="system", run_id=run.id, seq=2)]
        self.assertTrue(board.ingest(run))
        self.store.save(board)
        self.assertEqual(board.runs[previous.id], previous)
        return board, task, run

    @staticmethod
    def frame(app):
        return "\n".join(strip.text for strip in app.screen._compositor.render_strips())

    async def test_partial_first_screen_prioritizes_result_scope_and_no_disabled_pi_button(self):
        app = Waggle(store=self.store, runtime=self.runtime)
        async with app.run_test(size=(40, 20)) as pilot:
            await pilot.press("right")
            await pilot.pause()
            frame = self.frame(app)
            self.assertEqual(str(app.query_one("#accept-button", Button).label), "准备恢复 · 仅保存")
            self.assertEqual(app.primary_action().action, "prepare_recovery")
            self.assertFalse(app.query_one("#return-button", Button).display)
            guidance = str(app.query_one("#next-action", Static).content)
            for value in ("核实原 Pi", "精确会话", "轮次归属", "仅保存", "不派发", "不接管"):
                self.assertIn(value, guidance)
            self.assertFalse(app.query_one("#next-action", Static).has_class("needs-attention"))
            self.assertEqual(encode(self.store.load()), encode(self.before))
            self.assertIn("不能整任务验收", frame)
            self.assertIn("本轮结果（自报）", frame)
            self.assertIn("卡在这里", frame)
            self.assertLess(frame.index("本轮结果（自报）"), frame.index("卡在这里"))
            self.assertNotIn("选择/新建 Pi", frame)
            self.assertFalse(self.runtime.root.exists())

    async def test_bound_idle_partial_first_screen_keeps_proposal_guidance_without_execution(self):
        state_before = self.bind_idle_owner()
        before = encode(self.store.load())
        app = Waggle(store=self.store, runtime=self.runtime)
        async with app.run_test(size=(40, 20)) as pilot:
            await wait_ui(pilot, lambda: app.primary_action().label == "索取可审提案 b")
            await pilot.press("right")
            await pilot.pause()
            frame = self.frame(app)
            self.assertEqual(str(app.query_one("#accept-button", Button).label), "索取可审提案 b")
            self.assertFalse(app.query_one("#return-button", Button).display)
            guidance = str(app.query_one("#next-action", Static).content)
            for value in ("你不用批准", "依据/负责人", "模型额度"):
                self.assertIn(value, guidance)
            self.assertTrue(app.query_one("#next-action", Static).has_class("needs-attention"))
            self.assertIn("不能整任务验收", frame)
            self.assertLess(frame.index("本轮结果（自报）"), frame.index("卡在这里"))
            self.assertNotIn("选择/新建 Pi", frame)
            self.assertEqual(encode(self.store.load()), before)
            self.assertEqual(files(self.runtime.root), state_before)
            self.assertFalse(list(self.runtime.root.glob("*/commands/*.json")))

    async def test_partial_main_view_exposes_next_action_and_full_remaining(self):
        for width, height in ((80, 24), (120, 30), (180, 40)):
            with self.subTest(width=width):
                state_before = self.bind_idle_owner()
                app = Waggle(store=self.store, runtime=self.runtime)
                async with app.run_test(size=(width, height)) as pilot:
                    if width < 100:
                        self.assertIn("索取可审提案", self.frame(app))
                        await pilot.press("right")
                    await pilot.pause()  # Settle the toolbar's resize-driven CSS before measuring geometry.
                    heading = app.query_one("#detail-heading", Static)
                    self.assertIn("D001", heading.render_line(0).text)
                    self.assertIn("下一步", self.frame(app))
                    self.assertIn("仅保存", self.frame(app))
                    self.assertIn("待执行者交提案", str(app.query_one("#detail-state", Static).content))
                    self.assertNotIn("待验收", app.card(app.board.tasks[1]).plain)
                    primary = app.query_one("#accept-button", Button)
                    self.assertEqual(primary.label, "索取可审提案 b")
                    scroll = app.query_one("#detail-scroll", VerticalScroll)
                    self.assertLess(primary.region.bottom, scroll.region.y + 1)
                    if width >= 100:
                        self.assertGreater(app.query_one("#inspector").size.width, app.query_one("#task-region").size.width)
                    self.assertTrue(app.query_one("#runtime-details", Collapsible).collapsed)
                    content = str(app.query_one("#detail-text", Static).content)
                    self.assertIn("执行者", content)
                    self.assertIn("现在不需要你批准", content)
                    self.assertNotIn("需固定信号定义", content)
                    self.assertLess(content.index("本轮结果"), content.index("下一步"))
                    self.assertIn(app.board.tasks[1].remaining, detail_text(app.board.tasks[1], app.board.runs[app.board.tasks[1].run_id]).plain)
                    app.action_toggle_detail()
                    await pilot.pause()
                    position = primary.region
                    scroll.scroll_end(animate=False)
                    await pilot.pause()
                    if width < 180:
                        self.assertGreater(scroll.scroll_y, 0)
                    else:
                        self.assertIn("不要直接部署", detail_text(app.board.tasks[1], app.board.runs[app.board.tasks[1].run_id]).plain)
                    self.assertEqual(primary.region, position)
                    self.assertIn("索取可审提案", self.frame(app))
                    await pilot.click("#accept-button")
                    await wait_ui(pilot, lambda app=app: isinstance(app.screen, Choice))
                    choices = app.screen.query_one("#choices", OptionList)
                    choices.highlighted = next(index for index in range(choices.option_count)
                                               if choices.get_option_at_index(index).id == "review")
                    await pilot.press("enter")  # Explicitly choose next-round review.
                    await wait_ui(pilot, lambda app=app: isinstance(app.screen, Note))
                    note = app.screen
                    assert isinstance(note, Note)
                    self.assertIn("下一轮", note.title_text)
                    # Reported remaining work is not an approved execution instruction.
                    self.assertEqual(note.query_one(TextArea).text, "")
                    self.assertEqual(note.query_one("#submit", Button).label, "提交并核对")
                    self.assertEqual(note.query_one("#save-only", Button).label, "仅保存")
                    await pilot.press("escape")
                    app.board.tasks[1].remaining = "另一个修订"
                    with patch.object(app, "action_return_work") as action:
                        app.action_primary()
                        action.assert_not_called()
                self.assertEqual(self.store.load().tasks, self.before.tasks)
                self.assertEqual(files(self.runtime.root), state_before)
                self.assertFalse(list(self.runtime.root.glob("*/commands/*.json")))
        self.assertEqual(self.store.load().tasks, self.before.tasks)
        self.assertEqual(files(self.runtime.root), state_before)
        self.assertFalse(list(self.runtime.root.glob("*/commands/*.json")))

    async def test_action_and_tools_have_clear_order_without_fake_next_step(self):
        before = self.store.load()
        for width, height in ((80, 24), (120, 30), (180, 40)):
            with self.subTest(width=width):
                state_before = self.bind_idle_owner()
                app = Waggle(store=self.store, runtime=self.runtime)
                async with app.run_test(size=(width, height)) as pilot:
                    if width < 100:
                        await pilot.press("right")
                    await pilot.pause()
                    primary, next_step = (app.query_one(selector, Button) for selector in ("#accept-button", "#return-button"))
                    tools = list(app.query("#task-tools Button").results(Button))
                    self.assertEqual([button.id for button in tools], ["report-button", "more-button"])
                    self.assertEqual(str(primary.label), "索取可审提案 b")
                    self.assertFalse(next_step.display, "未确定下一轮要求时不要展示不可点击的 Pi 预告")
                    self.assertNotIn(str(primary.label), str(app.query_one("#next-action", Static).content))
                    if width < 130:
                        self.assertLess(primary.region.y, tools[0].region.y)
                        self.assertEqual(len({button.region.y for button in tools}), 1)
                    else:
                        self.assertEqual(len({button.region.y for button in (primary, *tools)}), 1)
                        self.assertLess(primary.region.x, tools[0].region.x)
                    self.assertEqual(primary.variant, "primary")
                    await pilot.click("#report-button")
                    self.assertIsInstance(app.screen, ReportReader)
                    await pilot.press("escape")
                    app.selected_id = 3
                    app.refresh_tasks()
                    await pilot.pause()
                    self.assertEqual(str(primary.label), "核对交付 r")
                    self.assertEqual(str(next_step.label), "核对报告")
                    self.assertFalse(next_step.disabled)
                    self.assertTrue(await pilot.click("#return-button"))
                    self.assertIsInstance(app.screen, ReportReader)
                    await pilot.press("escape")
                self.assertEqual(self.store.load().tasks, before.tasks)
                self.assertEqual(files(self.runtime.root), state_before)
                self.assertFalse(list(self.runtime.root.glob("*/commands/*.json")))
        self.assertEqual(self.store.load().tasks, before.tasks)
        self.assertEqual(files(self.runtime.root), state_before)
        self.assertFalse(list(self.runtime.root.glob("*/commands/*.json")))

    async def test_reopened_ready_task_without_observed_ownership_stays_save_only(self):
        state_before = self.bind_idle_owner()
        board = self.store.load()
        board.save_supplement(board.tasks[1], "下一轮仅整理材料", "next")
        self.store.save(board)
        before = encode(board)
        app = Waggle(store=self.store, runtime=self.runtime)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            self.assertEqual(app.primary_action().action, "prepare_recovery")
            self.assertEqual(str(app.query_one("#accept-button", Button).label), "准备恢复 · 仅保存")
            self.assertFalse(app.query_one("#return-button", Button).display)
            self.assertEqual(encode(self.store.load()), before)
            self.assertEqual(files(self.runtime.root), state_before)
            self.assertFalse(list(self.runtime.root.glob("*/commands/*.json")))

    async def test_ready_task_can_switch_agent_but_stale_secondary_cannot_assign(self):
        state_before = self.bind_idle_owner()
        app = Waggle(store=self.store, runtime=self.runtime)
        async with app.run_test(size=(120, 30)) as pilot:
            await wait_ui(pilot, lambda: app.primary_action().label == "索取可审提案 b")
            # Save-only transition occurs after the app actually observed the original ownership.
            app.apply_change(lambda board: board.save_supplement(board.tasks[1], "下一轮仅整理材料", "next"))
            app.refresh_tasks()
            before = encode(self.store.load())
            secondary = app.query_one("#return-button", Button)
            self.assertEqual(app.query_one("#accept-button", Button).label, "补齐核查范围 b")
            self.assertEqual(str(secondary.label), "更换执行者 a")
            self.assertFalse(secondary.disabled)
            app.board.tasks[1].remaining = "确认期间范围已变化"
            with patch.object(app, "action_assign") as assign:
                app.on_button_pressed(Button.Pressed(secondary))
                assign.assert_not_called()
            app.board.tasks[1].remaining = "下一轮仅整理材料"
            app.refresh_tasks()
            await pilot.pause()
            self.assertTrue(secondary.display and not secondary.disabled, (secondary.region, secondary.label))
            self.assertTrue(await pilot.click("#return-button"), (secondary.region, app.screen))
            await wait_ui(pilot, lambda: isinstance(app.screen, Choice))
            self.assertEqual(encode(app.board), before)
            await pilot.press("escape")
        self.assertEqual(encode(self.store.load()), before)
        self.assertEqual(files(self.runtime.root), state_before)
        self.assertFalse(list(self.runtime.root.glob("*/commands/*.json")))

    async def test_agent_selection_ignores_late_result_for_another_task(self):
        self.bind_idle_owner()
        board = self.store.load()
        board.save_supplement(board.tasks[1], "仅此任务的要求", "next")
        self.store.save(board)
        app = Waggle(store=self.store, runtime=self.runtime)
        entered, release = Event(), Event()
        def delayed_discover():
            entered.set()
            if not release.wait(3):
                raise TimeoutError("isolation probe timed out")
            return []
        async with app.run_test(size=(120, 30)) as pilot:
            with patch.object(self.runtime, "discover", side_effect=delayed_discover):
                try:
                    app.action_assign()
                    self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                    app.selected_id = 2
                    app.refresh_tasks()
                finally:
                    release.set()
                await pilot.pause(0.2)
            self.assertEqual(len(app.screen_stack), 1)
            self.assertEqual(app.board.tasks, board.tasks)
            self.assertFalse(list(self.runtime.root.glob("*/commands/*.json")))

    async def test_unknown_execution_shows_warning_not_a_fake_execute_button(self):
        board = self.store.load()
        task = board.tasks[1]
        board.save_supplement(task, review_requirements("明确的下一轮要求", "仅隔离测试目录", "可核对的原始回执"), "next")
        board.queue_real(task, str(uuid4()), str(uuid4()), str(self.root))
        self.store.save(board)
        before = encode(board)
        app = Waggle(store=self.store, runtime=self.runtime)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("right")
            primary = app.query_one("#accept-button", Button)
            self.assertEqual(str(primary.label), "查看状态")
            self.assertEqual(primary.variant, "default")
            self.assertFalse(app.query_one("#return-button", Button).display)
            self.assertIn("需核实执行", str(app.query_one("#next-action", Static).content))
            self.assertTrue(app.query_one("#context-hint", Static).display)
            self.assertIn("下一步 · 到原 Pi 核实", app.card(app.board.tasks[1]).plain)
            await pilot.press("ctrl+k")
            self.assertIsInstance(app.screen, Choice)
            assert isinstance(app.screen, Choice)
            self.assertIn("日常 · 主要操作 · 查看状态", app.screen.options.values())
            await pilot.press("escape")
            self.assertTrue(await pilot.click("#accept-button"))
            self.assertIsInstance(app.screen, ReportReader)
            await pilot.press("escape")
        self.assertEqual(encode(self.store.load()), before)
        self.assertFalse(self.runtime.root.exists())

    async def test_unified_toolbar_stays_above_guidance_and_wraps_without_clipping(self):
        board = self.store.load()
        task = board.create("任务标题不应被按钮挤掉", "收集箱")
        self.store.save(board)
        expected = ["accept-button", "return-button", "report-button", "more-button"]
        for width, height, rows in ((80, 24, 2), (120, 30, 2), (129, 30, 1), (130, 30, 1),
                                    (139, 40, 1), (140, 40, 1), (180, 40, 1)):
            with self.subTest(width=width):
                app = Waggle(store=self.store, runtime=self.runtime)
                app.selected_id = task.id
                async with app.run_test(size=(width, height)) as pilot:
                    if width < 100:
                        await pilot.press("right")
                    await pilot.pause()
                    bar = app.query_one("#detail-actions")
                    buttons = list(bar.query("Button").results(Button))
                    self.assertEqual([button.id for button in buttons], expected)
                    self.assertEqual(len({button.region.y for button in buttons}), rows)
                    self.assertLessEqual(app.query_one("#detail-header").region.bottom, bar.region.y)
                    self.assertLessEqual(bar.region.bottom, app.query_one("#next-action").region.y)
                    self.assertIn(task.title, app.query_one("#detail-heading", Static).render_line(0).text)
                    for button in buttons:
                        assert button.parent is not None
                        self.assertIn(button.parent.id, {"task-operations", "task-tools"})
                        self.assertIs(button.parent.parent, bar)
                        self.assertEqual(button.region.height, 1)
                        self.assertEqual(button.styles.text_wrap, "nowrap")
                        self.assertEqual(button.styles.text_overflow, "ellipsis")
                        self.assertTrue(bar.content_region.contains_region(button.region))
                        self.assertIn(str(button.label), button.render_line(0).text)
                    positions = [button.region for button in buttons]
                    app.query_one("#detail-scroll", VerticalScroll).scroll_end(animate=False)
                    await pilot.pause()
                    self.assertEqual([button.region for button in buttons], positions)
                    await pilot.click("#report-button")
                    self.assertIsInstance(app.screen, ReportReader)
                    await pilot.press("escape")
                    await pilot.click("#more-button")
                    menu = app.screen
                    assert isinstance(menu, Choice)
                    menu.query_one("#choices", OptionList).highlighted = list(menu.options).index("steps")
                    menu.action_submit()
                    await wait_ui(pilot, lambda app=app: isinstance(app.screen, Note))
                    await pilot.press("escape")
                    if width == 80:
                        app.action_sidebar()
                        await pilot.pause()
                        self.assertFalse(app.query_one("#sidebar").display, "全屏详情不旁开侧栏")
                        self.assertEqual(len({button.region.y for button in buttons}), 2)
                        for button in buttons:
                            self.assertTrue(bar.content_region.contains_region(button.region))
                            self.assertIn(str(button.label), button.render_line(0).text)
        self.assertEqual(self.store.load().tasks, board.tasks)
        self.assertFalse(self.runtime.root.exists())

    def with_decisions(self):
        specs = (DecisionSpec.from_record(spec_record(question="选择实施路线")),
                 DecisionSpec.from_record(spec_record("budget", key="limits", question="填写小样本预算")),
                 DecisionSpec.from_record(spec_record("approval", key="proof", question="核对验证依据", missing=["缺少独立验证"])) )
        board, _, _ = self.new_terminal_report(specs=specs, detail="待逐项决定")
        self.bind_idle_owner()
        return encode(board)

    async def test_home_agenda_opens_exact_question_without_selecting_or_saving(self):
        before = self.with_decisions()
        for size in ((80, 24), (180, 40)):
            with self.subTest(size=size):
                state_before = self.bind_idle_owner()
                app = Waggle(store=self.store, runtime=self.runtime)
                async with app.run_test(size=size) as pilot:
                    if size[0] < 100:
                        await pilot.press("right")
                    await pilot.pause()
                    self.assertEqual(app.primary_action().action, "decisions")
                    self.assertIn("待补齐决策材料", str(app.query_one("#detail-state", Static).content))
                    self.assertIn("补齐决策材料", str(app.query_one("#accept-button", Button).label))
                    self.assertEqual(str(app.query_one("#return-button", Button).label), "决定后继续")
                    self.assertTrue(app.query_one("#return-button", Button).disabled)
                    self.assertIn("3 项待处理", str(app.query_one("#next-action", Static).content))
                    items = app.query_one("#decision-items", OptionList)
                    self.assertEqual(items.option_count, 3)
                    self.assertIn("选择实施路线", self.frame(app))
                    self.assertIn("缺少独立验证", str(items.get_option_at_index(2).prompt))
                    first = items.get_option_at_index(0)
                    app.show_detail()
                    self.assertIs(items.get_option_at_index(0), first, "unchanged refresh must not rebuild focus/scroll")
                    items.highlighted = 1
                    items.focus()
                    await pilot.press("enter")
                    await wait_ui(pilot, lambda app=app: isinstance(app.screen, DecisionSheet) and app.focused is not None and app.focused.id == "decision-overview")
                    form = app.screen
                    assert isinstance(form, DecisionSheet)
                    self.assertEqual(form.focus_key, "limits")
                    self.assertEqual(form.selected_index, 1)
                    self.assertTrue(form.query_one("#decision-item-1").display)
                    self.assertEqual(form.query_one("#answer-1", Select).value, "undecided")
                    self.assertFalse(form.query("#budget-1-attempts"))
                    self.assertIn("累计已用：未知", str(form.query_one("#budget-facts-1", Static).content))
                    self.assertIn("填写小样本预算", self.frame(app))
                    await pilot.press("escape")
                    app.query_one("#detail-scroll", VerticalScroll).scroll_home(animate=False)
                    await pilot.pause()
                    self.assertTrue(await pilot.click("#decision-items", offset=(2, 0)))
                    await wait_ui(pilot, lambda app=app: isinstance(app.screen, DecisionSheet) and app.focused is not None and app.focused.id == "decision-overview")
                    assert isinstance(app.screen, DecisionSheet)
                    self.assertEqual(app.screen.focus_key, "direction")
                    self.assertEqual(app.screen.selected_index, 0)
                    self.assertEqual(app.screen.query_one("#answer-0", Select).value, "undecided")
                    await pilot.press("escape")
                    self.assertEqual(encode(app.board), before)
                self.assertEqual(encode(self.store.load()), before)
                self.assertEqual(files(self.runtime.root), state_before)
                self.assertFalse(list(self.runtime.root.glob("*/commands/*.json")))
        self.assertEqual(encode(self.store.load()), before)
        self.assertEqual(files(self.runtime.root), state_before)
        self.assertFalse(list(self.runtime.root.glob("*/commands/*.json")))

    async def test_phone_two_row_toolbar_hides_unavailable_secondary_and_shows_primary(self):
        app = Waggle(store=self.store, runtime=self.runtime)
        app.selected_id = 1
        before = encode(self.store.load())
        async with app.run_test(size=(40, 20)) as pilot:
            await pilot.press("right")
            await pilot.pause()
            bar = app.query_one("#detail-actions")
            primary = app.query_one("#accept-button", Button)
            secondary = app.query_one("#return-button", Button)
            report = app.query_one("#report-button", Button)
            more = app.query_one("#more-button", Button)
            self.assertEqual(bar.region.height, 2)
            self.assertFalse(secondary.display)
            self.assertEqual([b.region.height for b in (primary, report, more)], [1, 1, 1])
            self.assertLess(primary.region.y, report.region.y)
            self.assertEqual(report.region.y, more.region.y)
            self.assertIn(str(primary.label), self.frame(app))
            self.assertEqual(encode(self.store.load()), before)
            self.assertFalse(self.runtime.root.exists())

    async def test_phone_single_primary_keeps_report_and_more_on_second_row(self):
        board = self.store.load()
        task = board.tasks[3]
        board.accept(task)
        self.store.save(board)
        app = Waggle(store=self.store, runtime=self.runtime)
        app.selected_id = 3
        async with app.run_test(size=(40, 20)) as pilot:
            await pilot.press("right")
            await pilot.pause()
            bar = app.query_one("#detail-actions")
            primary = app.query_one("#accept-button", Button)
            secondary = app.query_one("#return-button", Button)
            report = app.query_one("#report-button", Button)
            more = app.query_one("#more-button", Button)
            self.assertFalse(secondary.display)
            self.assertFalse(app.query_one("#next-action", Static).has_class("needs-attention"))
            self.assertEqual(bar.region.height, 2)
            self.assertEqual([b.region.height for b in (primary, report, more)], [1, 1, 1])
            self.assertEqual(report.region.y, more.region.y)
            self.assertLess(primary.region.y, report.region.y)
            self.assertIn(str(primary.label), self.frame(app))
            self.assertFalse(self.runtime.root.exists())

    async def test_more_menu_preserves_primary_and_accesses_local_steps(self):
        app = Waggle(store=self.store, runtime=self.runtime)
        before = encode(self.store.load())
        async with app.run_test(size=(40, 20)) as pilot:
            await pilot.press("right")
            self.assertEqual([button.id for button in app.query("#task-tools Button").results(Button)],
                             ["report-button", "more-button"])
            self.assertTrue(app.query_one("#accept-button", Button).display)
            self.assertTrue(app.query_one("#report-button", Button).display)
            self.assertTrue(await pilot.click("#more-button"))
            self.assertIsInstance(app.screen, Choice)
            menu = app.screen
            assert isinstance(menu, Choice)
            self.assertEqual(set(menu.options), {"activity", "toggle_detail", "steps", "evidence",
                                                 "dependencies", "budget", "prepare_recovery"})
            await pilot.press("escape")
            self.assertEqual(len(app.screen_stack), 1)
            self.assertTrue(await pilot.click("#more-button"))
            menu = app.screen
            assert isinstance(menu, Choice)
            choices = menu.query_one("#choices", OptionList)
            choices.highlighted = list(menu.options).index("steps")
            menu.action_submit()
            await wait_ui(pilot, lambda: isinstance(app.screen, Note))
            await pilot.press("escape")
            self.assertEqual(encode(self.store.load()), before)
            self.assertFalse(self.runtime.root.exists())

    async def test_more_menu_activity_toggle_and_stale_selection(self):
        app = Waggle(store=self.store, runtime=self.runtime)
        before = encode(self.store.load())
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("right")
            self.assertTrue(await pilot.click("#more-button"))
            menu = app.screen
            assert isinstance(menu, Choice)
            menu.query_one("#choices", OptionList).highlighted = list(menu.options).index("activity")
            menu.action_submit()
            await wait_ui(pilot, lambda: isinstance(app.screen, TaskRecords))
            await pilot.press("escape")
            self.assertFalse(app.detail_expanded)
            self.assertTrue(await pilot.click("#more-button"))
            menu = app.screen
            assert isinstance(menu, Choice)
            menu.query_one("#choices", OptionList).highlighted = list(menu.options).index("toggle_detail")
            menu.action_submit()
            await wait_ui(pilot, lambda: app.detail_expanded)
            self.assertTrue(await pilot.click("#more-button"))
            menu = app.screen
            assert isinstance(menu, Choice)
            self.assertIn("收起详情", menu.options["toggle_detail"])
            app.selected_id = 2  # A late task selection must not run a menu opened for D001.
            menu.query_one("#choices", OptionList).highlighted = list(menu.options).index("steps")
            menu.action_submit()
            await wait_ui(pilot, lambda: len(app.screen_stack) == 1)
            self.assertEqual(encode(self.store.load()), before)
            self.assertFalse(self.runtime.root.exists())

    async def test_compact_chrome_two_columns_and_opt_in_navigation(self):
        for size in ((80, 24), (120, 30), (180, 40)):
            with self.subTest(size=size):
                app = Waggle(store=self.store, runtime=self.runtime)
                async with app.run_test(size=size) as pilot:
                    await pilot.pause()
                    self.assertLessEqual(app.query_one("#content").region.y, 3)
                    self.assertFalse(app.query_one("#sidebar").display)
                    self.assertTrue(app.query_one("#sidebar-button").display)
                    if size[0] >= 100:
                        self.assertEqual(app.query_one("#task-region").size.width, 32)
                        self.assertGreater(app.query_one("#inspector").size.width, 60)
                    await pilot.click("#sidebar-button")
                    self.assertTrue(app.query_one("#sidebar").display)
                    await pilot.press("escape")
                    self.assertFalse(app.query_one("#sidebar").display)
                    await pilot.press("/")
                    assert app.focused is not None
                    self.assertEqual(app.focused.id, "search")
        self.assertEqual(self.store.load().tasks, self.before.tasks)

    async def test_stale_agenda_and_primary_are_not_retargeted(self):
        self.with_decisions()
        app = Waggle(store=self.store, runtime=self.runtime)
        async with app.run_test(size=(120, 30)) as pilot:
            items = app.query_one("#decision-items", OptionList)
            await pilot.pause()
            event = OptionList.OptionSelected(items, items.get_option_at_index(1), 1)
            run = app.board.runs[app.board.tasks[1].run_id]
            run.events = [encoded_event("partial", "材料已更换", source="executor", run_id=run.id, seq=1,
                report=Report(run.summary, run.checks, run.remaining, decisions=(DecisionSpec.from_record(spec_record(question="不同的决定")),)))]
            with patch.object(app, "action_decisions") as open_sheet:
                app.action_primary()
                app.select_option(event)
                open_sheet.assert_not_called()
                app.selected_id = 2
                app.select_option(event)
                open_sheet.assert_not_called()
            self.assertEqual(len(app.screen_stack), 1)
            self.assertFalse(app.board.decisions)

    async def test_saved_material_request_keeps_last_round_questions_visible_readonly(self):
        board, task, run = self.new_terminal_report(
            remaining="请确认可写目录和精确文件；接口兼容与旧证据处置须由 owner 核对；运行前明确次数、总时长、新增存储和停止条件。",
            detail="普通报告中的待确认事项",
        )
        request = "只基于已有材料整理决策单，不授权新的运行或修改。"
        board.return_work(task, request)
        self.store.save(board)
        before = encode(board)
        for size in ((80, 24), (180, 40)):
            with self.subTest(size=size):
                app = Waggle(store=self.store, runtime=self.runtime)
                async with app.run_test(size=size) as pilot:
                    if size[0] < 100:
                        await pilot.press("right")
                    await pilot.pause()
                    text = str(app.query_one("#detail-text", Static).content)
                    self.assertIn("上轮交接缺口", self.frame(app))
                    for value in ("可写目录和精确文件", "接口兼容与旧证据处置", "次数、总时长、新增存储和停止条件"):
                        self.assertNotIn(value, text)
                        self.assertIn(value, app.board.runs[run.id].remaining)
                    self.assertIn("不是批准", text)
                    self.assertIn("尚未发送", text)
                    self.assertEqual(app.board.tasks[1].remaining, request)
                    self.assertIsNone(current_report(app.board.tasks[1], app.board.runs[run.id]))
                    self.assertFalse(decision_items(app.board, app.board.tasks[1]))
                    self.assertFalse(app.board.decisions)
                    self.assertEqual(encode(app.board), before)
        self.assertEqual(encode(self.store.load()), before)
        self.assertFalse(self.runtime.root.exists())

    def test_historical_pending_items_do_not_cross_identity_or_execution_boundaries(self):
        board = deepcopy(self.before)
        task = board.tasks[1]
        run = board.runs[task.run_id]
        board.return_work(task, "仅保存请求")
        self.assertIn("上轮交接缺口", overview_text(task, run).plain)
        for field, value in (("id", str(uuid4())), ("task_id", 99), ("state", "working"), ("delivery", "full")):
            other = deepcopy(run)
            setattr(other, field, value)
            with self.subTest(field=field):
                text = overview_text(task, other).plain
                self.assertNotIn("上轮交接缺口", text)
                self.assertNotIn("独立 OOS 未运行", text)
        self.assertNotIn(run.summary, overview_text(task, run).plain)
        self.assertIsNone(current_report(task, run))
        self.assertFalse(decision_items(board, task))

    def test_missing_remaining_never_invents_next_round_scope(self):
        task = deepcopy(self.before.tasks[1])
        run = deepcopy(self.before.runs[task.run_id])
        task.remaining = run.remaining = ""
        text = overview_text(task, run).plain
        self.assertIn("尚未提供明确剩余事项", text)
        self.assertNotIn("原始要求不改写", text)
        self.assertNotIn("无剩余项", text)
        self.assertFalse(task.can_accept)

    async def test_unknown_queued_execution_keeps_verification_guidance(self):
        board = self.store.load()
        task = board.tasks[1]
        board.return_work(task, review_requirements("明确的新范围", "仅隔离测试目录", "可核对的原始回执"))
        board.queue_real(task, str(uuid4()), str(uuid4()), str(self.root))
        self.store.save(board)
        app = Waggle(store=self.store, runtime=self.runtime)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            text = str(app.query_one("#next-action", Static).content)
            self.assertIn("核实执行", text)
            self.assertIn("核实原 Pi", text)
            self.assertIn("不要重派", text)
            self.assertNotIn("等待接收回执", text)
            self.assertFalse(self.runtime.root.exists())

    def test_ineligible_full_report_is_not_presented_as_acceptable(self):
        app = Waggle(store=self.store, runtime=self.runtime)
        task = app.board.tasks[3]
        task.full_delivery = False  # A full self-report can still lack acceptance conditions.
        self.assertEqual(app.task_display(task)[0], "blocked")
        self.assertNotIn("待验收", app.card(task).plain)
        self.assertNotEqual(app.primary_action(task).action, "review")
        self.assertEqual(self.store.load().tasks, self.before.tasks)

    async def test_filters_reserve_acceptance_for_complete_deliveries(self):
        app = Waggle(store=self.store, runtime=self.runtime)
        async with app.run_test(size=(180, 40)) as pilot:
            app.set_filter("review")
            await pilot.pause()
            self.assertEqual([task.id for task in app.visible_tasks()], [3])
            self.assertEqual(app.query_one("#accept-button", Button).label, "核对交付 r")
            await pilot.click("#accept-button")
            self.assertIsInstance(app.screen, Confirm)
            await pilot.press("enter")
            self.assertEqual(app.board.tasks[3].status, "review")
            app.set_filter("blocked")
            await pilot.pause()
            self.assertEqual([task.id for task in app.visible_tasks()], [1, 2])
            app.action_view()
            await pilot.pause()
            self.assertEqual(app.selected_id, 1)
            assert app.focused is not None
            self.assertEqual(app.focused.id, "lane-running")
        self.assertEqual(self.store.load().tasks, self.before.tasks)
