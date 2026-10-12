"""Progressive delivery and meaningful records through actual, disposable UI."""

import asyncio
import json
import time
import unittest
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from rich.console import Console
from rich.text import Text
from textual.containers import VerticalScroll
from textual.widgets import Button, OptionList, Static, TextArea, Tree

from app import Choice, Confirm, Note, Waggle
from decisions import (
    Answer,
    Target,
    prepare_decision_intent,
    preview_decisions,
    review_requirements,
)
from delivery import (
    decision_text,
    detail_text,
    next_round_reference,
    overview_text,
    round_timeline_text,
)
from journal import PREFIX, Brief, Report, encoded_event
from model import Board, Run, Task
from records import TaskRecords
from report_reader import ReportCode, ReportMarkdown, ReportReader, ReportTable
from runtime import Agent, Runtime, private_dir, publish
from store import Store
from test_activity import activity_record
from test_continuations import session_record
from test_decisions import decision_board
from test_live import wait_ui


class OverviewTextTests(unittest.TestCase):
    def test_decision_and_legacy_round_time_do_not_overclaim(self):
        run = Run(str(uuid4()), 1, str(uuid4()), str(uuid4()), "/virtual/project", "任务", "说明", "", state="finished",
                  delivery="full", summary="交付", checks=["自报依据"], events=["Pi agent_settled"])
        task = Task(1, "任务", "项目", status="review", run="finished", run_id=run.id, execution="pi",
                    full_delivery=True, checks=["自报依据"])
        decision = decision_text(task).plain
        self.assertIn("人工决定 · 可进入人工核对", decision)
        self.assertIn("依据 · 执行者自报 1 条；工作台未独立验证", decision)
        timeline = round_timeline_text(task, [run]).plain
        self.assertIn("第 1 轮 · 本轮 · 执行已结束", timeline)
        self.assertIn("时间未知", timeline)
        self.assertNotIn("202", timeline)

    def test_next_round_reference_never_relabels_old_formal_approval_as_feedback(self):
        board, task, run = decision_board()
        formal_note = "旧方案 A 的正式决定，仅在旧报告有效"
        prepare_decision_intent(board, preview_decisions(board, task.id,
            (Answer("choice", "a", comment=formal_note),), Target("saved")))
        before = deepcopy(board)
        hint, reference = next_round_reference(board, task, run)
        self.assertNotIn(formal_note, hint + reference)
        self.assertIn(run.remaining, reference)
        self.assertIn("执行者自报", reference)
        self.assertEqual(board.decisions, before.decisions)
        self.assertEqual(board.runs, before.runs)

    def test_partial_brief_puts_non_acceptance_and_result_before_dense_pending_list(self):
        brief = Brief(["核对旧记录"], "只读重开两个旧结果；均零成交。", "只核原库；新实验未运行。")
        run = Run(str(uuid4()), 2, str(uuid4()), str(uuid4()), "/fixture/project", "阶段报告", "原范围", "",
                  state="finished", seq=1, delivery="partial", summary="完整报告另看详情", checks=["自报核验"],
                  remaining="其余旧 ref 尚未核清。新经济输入和预算需要另行批准。")
        run.events = [encoded_event("partial", "报告", source="executor", run_id=run.id, seq=1,
                                    report=Report(run.summary, run.checks, run.remaining, brief))]
        task = Task(2, run.title, "项目", status="review", execution="pi", run="finished", run_id=run.id,
                    summary=run.summary, checks=list(run.checks), remaining=run.remaining)
        before = deepcopy((task, run))
        plain = overview_text(task, run).plain
        self.assertLess(plain.index("不能整任务验收"), plain.index("本轮结果（自报）"))
        self.assertLess(plain.index("本轮结果（自报）"), plain.index("下一步"))
        self.assertIn("只读重开两个旧结果", plain)
        self.assertIn("执行者", plain)
        self.assertIn("现在不需要你批准", plain)
        self.assertNotIn(run.remaining, plain)
        full = detail_text(task, run).plain
        for material in (run.remaining, brief.verification, run.summary):
            self.assertIn(material, full)
        self.assertFalse(task.can_accept)
        self.assertEqual((task, run), before)

    def test_finished_brief_separates_outcome_actions_and_verification_with_visual_weight(self):
        brief = Brief(["核对原始目标", "保留尚未完成的研究事项"],
                      "目标回顾完成；正式研究仍未完成。", "仅只读核对；本轮测试、实验未运行。")
        for kind in ("partial", "full"):
            with self.subTest(kind=kind):
                remaining = "后续范围仍须明确批准。" if kind == "partial" else ""
                run = Run(str(uuid4()), 2, str(uuid4()), str(uuid4()), "/fixture/project", "目标回顾", "原始范围", "本轮要求",
                          state="finished", seq=2, delivery=kind, summary="完整原文，不替换为简报", checks=["原始依据"], remaining=remaining)
                run.events = [encoded_event(kind, "执行者报告", source="executor", run_id=run.id, seq=1,
                                            report=Report(run.summary, run.checks, remaining, brief))]
                task = Task(2, run.title, "项目", status="review", execution="pi", run="finished", run_id=run.id,
                            summary=run.summary, checks=list(run.checks), remaining=remaining, full_delivery=kind == "full")
                before = deepcopy((task, run))
                view = overview_text(task, run)
                headings = ["本轮结果（自报）", "卡在这里", "下一步"] if kind == "partial" else ["本轮结果（自报）", "已做工作（自报）", "核验与限制（自报）"]
                positions = [view.plain.index(" " + heading + " ") for heading in headings]
                self.assertEqual(positions, sorted(positions))
                for heading in headings:
                    style = view.get_style_at_offset(Console(), view.plain.index(" " + heading + " "))
                    self.assertIs(style.bold, True)
                    self.assertIsNotNone(style.bgcolor)
                for phrase in ("目标回顾完成；", "正式研究仍未完成。"):
                    self.assertIn(phrase, view.plain)
                    self.assertIs(view.get_style_at_offset(Console(), view.plain.index(phrase)).bold, True)
                if kind == "partial":
                    self.assertIn("现在不需要你批准", view.plain)
                    self.assertIn("不能整任务验收", view.plain)
                    self.assertNotIn(remaining, view.plain)
                    self.assertFalse(task.can_accept)
                else:
                    for phrase in (*brief.actions, "本轮测试、实验未运行。"):
                        self.assertIn(phrase, view.plain)
                        self.assertIs(view.get_style_at_offset(Console(), view.plain.index(phrase)).bold, False)
                for material in (brief.outcome, brief.verification, *brief.actions, run.summary):
                    self.assertIn(material, detail_text(task, run).plain)
                self.assertEqual((task, run), before)

    def test_pending_items_are_listed_without_rewriting_reports(self):
        for remaining in ("数据尚未验收；OOS 未验证。", "核对 https://example.test/a.b?v=1.25；保留 3.14。\n不要部署",
                          "1. 核对数据；不自动运行。\n   保留补充说明。\n\n2. 用户确认范围。",
                          "- 核对数据。\n  保留缩进与换行。\n\n• 不授权部署。", "  \n " ):
            with self.subTest(remaining=remaining):
                run = Run(str(uuid4()), 4, str(uuid4()), str(uuid4()), "/fixture/project", "能力审计", "原始要求", "",
                          state="finished", delivery="partial", summary="仅完成审计", remaining=remaining)
                task = Task(4, run.title, "项目", status="review", execution="pi", run="finished", run_id=run.id,
                            summary=run.summary, remaining=remaining)
                before = deepcopy((task, run))
                view = overview_text(task, run).plain
                self.assertIn("执行者", view)
                self.assertIn("现在不需要你批准", view)
                if remaining.strip():
                    self.assertNotIn(remaining, view)
                    self.assertIn(remaining, detail_text(task, run).plain)
                else:
                    self.assertIn("尚未提供明确剩余事项", view)
                self.assertEqual((task, run), before)

    def test_accepted_sections_distinguish_headings_body_and_evidence_items(self):
        task = Task(1, "已验收任务", "项目", status="done", run="finished", execution="pi",
                    summary="完成历史核对。\n保留原始段落。", checks=["第一项依据。\n原始附注。", "第二项依据。"])
        view = overview_text(task)
        self.assertEqual(view.plain, " 已完成（自报） \n• 完成历史核对。\n• 保留原始段落。\n\n 验证依据 \n• "
                         + task.checks[0] + "\n• " + task.checks[1] + "\n\n")
        console = Console()
        body = view.get_style_at_offset(console, view.plain.index("完成历史核对。"))
        self.assertIs(body.bold, False)
        self.assertIsNone(body.bgcolor)
        for heading in ("已完成", "验证依据"):
            style = view.get_style_at_offset(console, view.plain.index(heading))
            self.assertIs(style.bold, True)
            self.assertIsNotNone(style.bgcolor)
            self.assertNotEqual(style.color, style.bgcolor)
            self.assertNotEqual(style.color, body.color)
        for check in task.checks:
            self.assertIs(view.get_style_at_offset(console, view.plain.index(check)).bold, False)
        self.assertIn(task.summary, detail_text(task).plain)

    def test_accepted_completed_list_preserves_existing_markers_and_literal_text(self):
        lines = ["- 已有项目。", "  - 子项目。", "* 星号项目。", "+ 加号项目。", "• 圆点项目。",
                 "1. 有序项目。", "2) 另一项。", "3、 中文序号。", "原样 [bold]标记[/bold]。"]
        task = Task(1, "已验收任务", "项目", status="done", run="finished", execution="pi",
                    summary="\n\n".join(lines) + "\n")
        before = deepcopy(task)
        compact = overview_text(task).plain
        self.assertIn("\n".join(lines[:-1]) + "\n• " + lines[-1], compact)
        self.assertNotIn("实现了什么", compact)
        self.assertIn(task.summary, detail_text(task).plain)
        self.assertEqual(task, before)

    def test_accepted_legacy_report_shows_result_and_saved_evidence(self):
        summary = "实现了历史查询与出处核对。\n" * 40 + "最终交付原文结尾。"
        checks = ["核对 docs/result.md；没有运行新实验。", "完整证据/" * 60 + "依据原文结尾。"]
        for execution in ("pi", "simulation"):
            with self.subTest(execution=execution):
                task = Task(1, "已验收任务", "项目", status="done", run="finished", execution=execution,
                            description="仅详情展示的执行范围。", summary=summary, checks=checks,
                            events=["不应进入概览的工具流水。"])
                before = deepcopy(task)
                compact = overview_text(task).plain
                self.assertIn("已完成", compact)
                self.assertIn("\n".join(f"• {line}" for line in summary.splitlines()), compact)
                self.assertIn(summary, detail_text(task).plain)
                self.assertIn("验证依据", compact)
                for check in checks:
                    self.assertIn(check, compact)
                for value in ("进展", "完整内容见详情", task.description, task.events[0]):
                    self.assertNotIn(value, compact)
                self.assertIn("自报" if execution == "pi" else "全部虚构", compact)
                self.assertEqual(task, before)

    def test_accepted_missing_report_does_not_invent_results(self):
        for summary in ("", " \n\t\n"):
            with self.subTest(summary=summary):
                task = Task(1, "已验收的旧任务", "项目", status="done", run="finished", execution="pi", summary=summary)
                compact = overview_text(task).plain
                self.assertIn("未保存最终交付正文", compact)
                self.assertIn("未保存验证依据", compact)
                self.assertIn("不能推断验证通过", compact)
                self.assertNotIn("无剩余项", compact)
                self.assertNotIn("已有报告", compact)
                self.assertIn("• 未保存最终交付正文。", compact)
                self.assertIn("• 未保存验证依据；", compact)

    def test_long_blockers_and_pending_requirements_are_flags_not_truncated_reports(self):
        reason = "需要核对权限及范围。\n" * 80 + "阻塞原文结尾。"
        for run in ("none", "blocked"):
            with self.subTest(run=run):
                task = Task(1, "核对任务", "项目", status="blocked", run=run, blocker=reason, remaining=reason)
                compact = overview_text(task).plain
                self.assertIn("有阻塞，需你核实", compact)
                self.assertNotIn("需要核对权限及范围", compact)
                self.assertLess(len(compact), 250)
                self.assertIn(reason, detail_text(task).plain)
                if run == "none":
                    self.assertIn("待派发的补充要求：有", compact)
        task = Task(2, "等待报告", "项目", run="working", status="running", execution="pi")
        compact = overview_text(task).plain
        self.assertNotIn("尚无执行者报告", compact)
        self.assertNotIn("剩余工作尚未确认", compact)
        self.assertNotIn("暂无简报", compact)


class RunningOverviewTests(unittest.TestCase):
    def setUp(self):
        self.attempt = Run(str(uuid4()), 2, str(uuid4()), str(uuid4()), "/fixture/project", "核对任务", "原始范围", "派发要求",
                           state="working", seq=4, summary="已核对第一项，结果通过。\n下一项仅完成源追踪，尚未修改代码。",
                           remaining="先冻结下一项方案，再验证。\n仍有未完成项，不能整任务验收。", checks=["证据路径一", "证据路径二"])
        self.task = Task(2, "核对任务", "项目", status="running", execution="pi", run="working", run_id=self.attempt.id,
                         summary=self.attempt.summary, remaining=self.attempt.remaining, checks=list(self.attempt.checks))

    def test_running_report_without_brief_shows_concrete_progress_and_remaining(self):
        self.task.summary = "不得冒充本轮进展的旧投影。"
        before = deepcopy((self.task, self.attempt))
        view = overview_text(self.task, self.attempt)
        for body in (self.attempt.summary, self.attempt.remaining):
            self.assertIn("\n".join(f"• {line}" for line in body.splitlines()), view.plain)
        for value in ("最近进展（执行者自报）", "剩余工作 / 下一步", "2 条", "未独立验证"):
            self.assertIn(value, view.plain)
        for value in (self.task.summary, "本轮报告已保存", "有剩余工作或限制", *self.attempt.checks):
            self.assertNotIn(value, view.plain)
        self.assertIs(view.get_style_at_offset(Console(), view.plain.index(self.attempt.summary.splitlines()[0])).bold, False)
        self.assertEqual((self.task, self.attempt), before)

    def test_valid_brief_keeps_outcome_verification_and_unabridged_problem(self):
        brief = Brief(["核对第一项", "追踪下一项来源"], "第一项已登记", "只做静态核对，未运行实验")
        self.attempt.delivery = "progress"
        self.attempt.events = [encoded_event("progress", "阶段报告", source="executor", run_id=self.attempt.id, seq=4,
                                            report=Report(self.attempt.summary, self.attempt.checks, self.attempt.remaining, brief))]
        self.task.blocker = "需要核对权限范围。\n" * 20 + "不能擅自扩大执行范围。"
        text = overview_text(self.task, self.attempt).plain
        for value in (*brief.actions, brief.outcome, brief.verification):
            self.assertIn(value, text)
        for body in (self.task.blocker, self.attempt.remaining):
            self.assertIn("\n".join(f"• {line}" for line in body.splitlines()), text)
        self.assertNotIn("派发要求", text)
        self.attempt.delivery = ""  # A retained but invalidated brief is not the current result.
        self.assertNotIn(brief.outcome, overview_text(self.task, self.attempt).plain)
        self.assertIn("\n".join(f"• {line}" for line in self.attempt.summary.splitlines()),
                      overview_text(self.task, self.attempt).plain)

    def test_ended_blocked_overview_prioritizes_request_and_preserves_actual_results(self):
        self.attempt.state, self.attempt.delivery = "failed", "blocked"
        self.task.status, self.task.run = "blocked", "finished"
        self.task.remaining = self.attempt.remaining = "请补充原方案、功能范围和验收标准。"
        self.task.blocker = self.attempt.summary * 30
        brief = Brief(["检查项目目录", "查找原方案", "未修改业务代码"], "需求缺失，不能实施", "测试、构建及功能验收均未运行")
        self.attempt.events = [encoded_event("blocked", "需求缺失", source="executor", run_id=self.attempt.id, seq=4,
                                            report=Report(self.attempt.summary, self.attempt.checks, self.attempt.remaining, brief))]
        before = deepcopy((self.task, self.attempt))
        rendered = overview_text(self.task, self.attempt)
        view = rendered.plain
        sections = ["本轮结果（自报）", "卡在这里", "下一步"]
        self.assertEqual([view.index(label) for label in sections], sorted(view.index(label) for label in sections))
        for label in sections:
            style = rendered.get_style_at_offset(Console(), view.index(" " + label + " "))
            self.assertIs(style.bold, True)
            self.assertIsNotNone(style.bgcolor)
        self.assertIn("执行者", view)
        self.assertIn("现在不需要你批准", view)
        self.assertIn(brief.outcome, view)
        for value in (self.attempt.remaining, *brief.actions, brief.verification, self.task.blocker):
            self.assertNotIn(value, view)
            self.assertIn(value, detail_text(self.task, self.attempt).plain)
        self.assertEqual((self.task, self.attempt), before)
        self.attempt.events, self.attempt.checks = [], []
        fallback = overview_text(self.task, self.attempt).plain
        for label in sections:
            self.assertIn(label, fallback)
        self.assertIn("下一步交接", fallback)
        self.assertIn("现在不需要你批准", fallback)
        self.assertNotIn(brief.verification, fallback)

    def test_no_report_or_evidence_is_not_inferred_from_requirements(self):
        self.attempt.summary, self.attempt.checks, self.attempt.remaining = "", [], ""
        self.task.remaining = self.attempt.requirements
        text = overview_text(self.task, self.attempt).plain
        for value in (self.task.summary, self.attempt.requirements, "验证通过", "无剩余项"):
            self.assertNotIn(value, text)
        self.attempt.summary = "仅完成源追踪。"
        text = overview_text(self.task, self.attempt).plain
        self.assertIn(self.attempt.summary, text)
        self.assertIn("• 未提供核实依据；\n• 不能推断验证通过。", text)
        self.assertNotIn("验证已通过", text)

    def test_wrong_or_historical_run_cannot_supply_a_running_overview(self):
        for field, value in (("id", str(uuid4())), ("task_id", 99)):
            with self.subTest(field=field):
                attempt = deepcopy(self.attempt)
                setattr(attempt, field, value)
                text = overview_text(self.task, attempt).plain
                self.assertIn("轮次身份不匹配", text)
                self.assertNotIn(attempt.summary, text)
                self.assertNotIn(attempt.remaining, text)
        self.task.run = "none"
        text = overview_text(self.task, self.attempt).plain
        self.assertIn("没有本轮交付", text)
        self.assertNotIn(self.attempt.summary, text)


class DeliveryUI(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="waggle-delivery-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.runtime = Runtime(self.root / "bridges")
        self.store = self.enterContext(Store(self.root / "tasks.sqlite3"))
        board = self.store.load()
        board.add_project("临时研究", str(self.root))
        task = board.create("历史查询", "临时研究", "只查询已有结论，不运行实验。\n" + "补充范围说明。\n" * 45)
        board.assign(task, str(uuid4()))
        task.remaining = review_requirements(
            "本轮只核对历史出处",
            "仅隔离临时项目；不修改文件，不运行实验",
            "列出可核对出处及未运行核验",
        )
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
        return Waggle(store=self.store, runtime=self.runtime)

    @staticmethod
    def frame(app):
        return "\n".join(strip.text for strip in app.screen._compositor.render_strips())

    @staticmethod
    def content(app):
        markdown = app.query_one("#detail-markdown", ReportMarkdown)
        rendered = (str(app.query_one("#detail-markdown-heading", Static).content) + markdown.source
                    if markdown.display else "")
        return str(app.query_one("#detail-text", Static).content) + rendered + str(app.query_one("#detail-tail", Static).content)

    async def toggle_detail(self, app, pilot):
        app.action_toggle_detail()
        await pilot.pause()  # Direct action does not await the deferred Markdown render.

    async def test_blocked_request_is_primary_at_both_widths_without_weakening_save_guards(self):
        board = self.store.load()
        task = board.tasks[1]
        run = board.runs[task.run_id]
        task.title = run.title = "实现"
        task.description = run.description = "我们开始实现"
        task.status, task.full_delivery = "blocked", False
        run.state, run.delivery = "failed", "blocked"
        task.summary = task.blocker = run.summary = "需求上下文缺失。仅检查目录，未修改业务代码。"
        task.remaining = run.remaining = "全部功能实现及测试验收仍待完成。请补充具体功能需求／原方案和验收标准，或提供其在指定项目内的路径；当前不能安全确定实现范围。"
        brief = Brief(["核对指定项目目录和 Git 状态", "搜索项目内需求、源码与任务上下文", "提交需求缺失的阻塞报告，未改业务代码"], "需求缺失，不能实施", "测试、构建和功能验收均未运行")
        run.events = [encoded_event("blocked", "需求缺失", source="executor", run_id=run.id, seq=40,
                                   report=Report(run.summary, run.checks, run.remaining, brief))]
        self.store.save(board)
        before = deepcopy((board.tasks, board.runs))
        for width in (80, 180):
            with self.subTest(width=width):
                app = self.app()
                async with app.run_test(size=(width, 24)) as pilot:
                    await pilot.press("right")
                    primary = app.query_one("#accept-button", Button)
                    self.assertEqual(str(primary.label), "选择本轮目的 b")
                    self.assertLess(primary.region.bottom, 25)
                    self.assertIn("补充需求", str(app.query_one("#detail-state", Static).content))
                    self.assertNotIn("验收", app.card(app.board.tasks[1]).plain)
                    status = app.query_one("#execution-status", Static)
                    self.assertNotIn("执行失败", self.frame(app))
                    self.assertEqual(status.styles.border_bottom[0], "solid")
                    self.assertTrue(app.query_one("#runtime-details").has_class("-collapsed"))
                    text = self.content(app)
                    self.assertLess(text.index("本轮结果"), text.index("下一步"))
                    self.assertIn(brief.verification, detail_text(task, run).plain)
                    self.assertIn("未改业务代码", detail_text(task, run).plain)
                    self.assertIn("执行者", self.frame(app))
                    scroll = app.query_one("#detail-scroll", VerticalScroll)
                    scroll.scroll_end(animate=False)
                    await pilot.pause()
                    self.assertIn("展开详情", self.content(app))
                    self.assertLess(primary.region.bottom, 25)
                    scroll.scroll_home(animate=False)
                    await pilot.pause()
                    await pilot.click("#accept-button")
                    await wait_ui(pilot, lambda app=app: isinstance(app.screen, Choice))
                    app.screen.query_one("#choices", OptionList).highlighted = 1
                    await pilot.press("enter")  # Explicit next-round review, not evidence-only or approval.
                    await wait_ui(pilot, lambda app=app: isinstance(app.screen, Note))
                    assert isinstance(app.screen, Note)
                    self.assertIn("下一轮", app.screen.title_text)
                    self.assertIn("交付物", app.screen.placeholder)
                    self.assertEqual(app.screen.query_one("#submit", Button).label, "提交并核对")
                    self.assertEqual(app.screen.query_one("#save-only", Button).label, "仅保存")
                    self.assertIn(task.description, str(app.screen.query_one("#note-scope-text", Static).content))
                    await pilot.press("escape")
                    adjacent = app.query_one("#return-button", Button)
                    self.assertEqual(str(adjacent.label), "选择/新建 Pi a")
                    self.assertTrue(adjacent.disabled)
                    await pilot.press("d")
                    self.assertIsInstance(app.screen, Confirm)
                    assert isinstance(app.screen, Confirm)
                    self.assertIn("补齐材料", app.screen.title_text)
                    await pilot.press("escape")
                    await pilot.click("#report-button")
                    self.assertIsInstance(app.screen, ReportReader)
                    assert isinstance(app.screen, ReportReader)
                    self.assertIn(run.summary, app.screen.body)
                    await pilot.press("escape")
                    with patch.object(app, "action_return_work") as supplement:
                        app.board.tasks[1].remaining = "changed after primary render"
                        app.action_primary()
                        supplement.assert_not_called()
        self.assertEqual((self.store.load().tasks, self.store.load().runs), before)
        # A stale Pi snapshot must still block saving; the new primary action is not a bypass.
        endpoint = self.runtime.root / run.agent_id
        for path in (self.runtime.root, endpoint, endpoint / "runs"):
            private_dir(path, create=True)
        publish(endpoint / "session.json", session_record(run, observed_at=1))
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("right")
            await pilot.click("#accept-button")
            await wait_ui(pilot, lambda: isinstance(app.screen, Choice))
            app.screen.query_one("#choices", OptionList).highlighted = 1
            await pilot.press("enter")
            await wait_ui(pilot, lambda: isinstance(app.screen, Note))
            app.screen.query_one("#note", TextArea).text = "补充明确的功能范围"
            await pilot.press("ctrl+enter")
            self.assertIsInstance(app.screen, Note)
            self.assertIn("过期", str(app.screen.query_one("#form-error", Static).content))
            self.assertEqual(app.board.tasks[1].run, "finished")
        self.assertEqual((self.store.load().tasks, self.store.load().runs), before)
        self.assertFalse(list(self.runtime.root.glob("*/commands/*.json")))

    async def test_selected_overview_groups_are_distinct_at_narrow_and_wide_sizes(self):
        before = self.store.load()
        for size in ((80, 36), (139, 50)):
            with self.subTest(size=size):
                app = self.app()
                async with app.run_test(size=size) as pilot:
                    await pilot.press("right")
                    self.assertFalse(app.detail_expanded)
                    frame = self.frame(app).splitlines()
                    rows = [next(index for index, row in enumerate(frame) if heading in row)
                            for heading in ("本轮结果", "已做工作", "核验与限制")]
                    self.assertEqual(rows, sorted(set(rows)))
                    content = self.content(app)
                    self.assertIn("已恢复历史结论", content)
                    self.assertIn("核对历史出处", content)
                    self.assertIn("• 未运行测试；\n• 仅核对历史文档", content)
                    self.assertNotIn("早期报告原文", content)
                    self.assertEqual(app.board.tasks, before.tasks)
                    self.assertEqual(app.board.runs, before.runs)
        self.assertEqual(self.store.load().tasks, before.tasks)
        self.assertFalse(self.runtime.root.exists())

    async def test_overview_uses_explicit_brief_and_expand_shows_complete_scope(self):
        before = (self.root / "tasks.sqlite3").read_bytes()
        app = self.app()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.press("right")
            detail = self.content(app)
            self.assertIn("• 核对历史出处\n• 恢复已有结论", detail)
            self.assertIn("• 未运行测试；\n• 仅核对历史文档", detail)
            self.assertNotIn("补充范围说明", detail)
            self.assertNotIn("docs/retained-result.md", detail)
            self.assertNotIn("早期报告原文", detail)
            await self.toggle_detail(app, pilot)
            await pilot.pause()
            self.assertTrue(app.detail_expanded)
            rendered = app.query_one("#detail-markdown", ReportMarkdown)
            self.assertTrue(rendered.display)
            self.assertEqual(rendered.source, app.board.tasks[1].summary)
            detail = self.content(app)
            self.assertFalse(detail.startswith("历史查询\n"))
            self.assertIn("人工决定", detail)
            self.assertIn("依据 · 执行者自报", detail)
            self.assertIn("工作台未独立验证", detail)
            sections = ["人工决定", "本轮结论（自报）", "本轮做了什么", "本轮交付正文", "产物与核实依据", "任务说明 / 验收范围", "本轮补充要求", "\n安排\n", "轮次与关键时间", "第 1 轮 · 本轮"]
            positions = [detail.index(section) for section in sections]
            self.assertEqual(positions, sorted(positions))
            self.assertIn("已恢复历史结论", detail)
            self.assertIn("docs/retained-result.md", detail)
            self.assertIn("只查询已有结论，不运行实验。", detail)
            self.assertIn("本轮只核对历史出处", detail)
            self.assertIn("仅隔离临时项目", detail)
            self.assertNotIn(app.board.tasks[1].run_id, detail)
            await pilot.press("r")
            assert isinstance(app.screen, Confirm)
            self.assertIn("docs/retained-result.md", app.screen.body)
            self.assertIn("只查询已有结论，不运行实验。", app.screen.body)
            self.assertIn("本轮只核对历史出处", app.screen.body)
            self.assertIn("仅隔离临时项目", app.screen.body)
            self.assertNotIn("最近报告", app.screen.body)
            self.assertNotIn("下一步", app.screen.body)
            await pilot.press("enter")
            self.assertEqual(app.board.tasks[1].status, "review")
        self.assertEqual((self.root / "tasks.sqlite3").read_bytes(), before)
        self.assertFalse(self.runtime.root.exists())

    async def test_expanded_detail_renders_current_markdown_without_enabling_links(self):
        board = self.store.load()
        task = board.tasks[1]
        run = board.runs[task.run_id]
        task.summary = run.summary = "# 结论\n\n- 事项一\n\n| 列 | 值 |\n| --- | --- |\n| A | B |\n\n```text\nCODE_COPY\n```\n\n[不打开](https://example.invalid)"
        self.store.save(board)
        for size in ((80, 24), (120, 30)):
            with self.subTest(size=size):
                app = self.app()
                async with app.run_test(size=size) as pilot:
                    await pilot.press("right")
                    await self.toggle_detail(app, pilot)
                    await pilot.pause()
                    markdown = app.query_one("#detail-markdown", ReportMarkdown)
                    self.assertTrue(markdown.display)
                    self.assertEqual(markdown.source, task.summary)
                    self.assertTrue(list(markdown.query(ReportTable)))
                    self.assertTrue(list(markdown.query(ReportCode)))
                    self.assertNotIn("CODE_COPY", str(app.query_one("#detail-text", Static).content))
                    self.assertIn("CODE_COPY", self.content(app))
                    scroll = app.query_one("#detail-scroll", VerticalScroll)
                    code = markdown.query_one(ReportCode)
                    scroll.scroll_to(y=code.region.y - scroll.region.y, animate=False)
                    await pilot.pause()
                    self.assertIn("CODE_COPY", self.frame(app))
                    self.assertLessEqual(app.query_one("#detail-actions").region.bottom, size[1] - 2)

    async def test_late_detail_markdown_never_reappears_for_a_reselected_task(self):
        board = self.store.load()
        task = board.tasks[1]
        run = board.runs[task.run_id]
        task.summary = run.summary = "# 慢报告\n\n内容"
        other = board.create("切换目标", "临时研究")
        self.store.save(board)
        app = self.app()
        gate = asyncio.Event()
        original = ReportMarkdown.update

        async def delayed(markdown, body):
            if body == task.summary:
                await gate.wait()
            await original(markdown, body)

        with patch.object(ReportMarkdown, "update", delayed):
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.press("right")
                await self.toggle_detail(app, pilot)
                await asyncio.sleep(0)
                app.selected_id = other.id
                app.refresh_tasks()
                gate.set()
                await pilot.pause()
                self.assertEqual(app.selected_id, other.id)
                self.assertFalse(app.query_one("#detail-markdown", ReportMarkdown).display)
                self.assertFalse(app.query_one("#detail-markdown-heading", Static).display)

    async def test_expanded_detail_groups_current_and_historical_rounds_by_saved_order(self):
        board = self.store.load()
        task = board.tasks[1]
        previous = board.runs[task.run_id]
        board.return_work(task, review_requirements("只补录出处。", "仅隔离临时项目；保持只读，不运行新试验或修改文件", "列出可核对出处及未运行核验"))
        board.queue_real(task, str(uuid4()), previous.session_id, previous.cwd)
        returned = json.loads(task.events[-2][len(PREFIX):])
        returned["at"] = "2001-01-01T00:00:00.000Z"
        task.events[-2] = PREFIX + json.dumps(returned)
        self.store.save(board)
        app = self.app()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.press("right")
            await self.toggle_detail(app, pilot)
            detail = self.content(app)
            current = "第 2 轮 · 本轮 · 等待确认"
            history = "第 1 轮 · 历史 · 执行已结束"
            self.assertLess(detail.index("\n安排\n"), detail.index("轮次与关键时间"))
            self.assertLess(detail.index(current), detail.index(history))
            self.assertNotIn("轮次与关键时间", str(app.query_one("#detail-text", Static).content))
            self.assertTrue(detail.endswith(round_timeline_text(task, list(board.runs.values())).plain))
            self.assertLess(detail.index("执行空闲（非验收）"), detail.index("补充 / 退回"))
            self.assertRegex(detail, r"20\d\d-\d\d-\d\d \d\d:\d\d:\d\d [+-]\d{4}")
            self.assertIn("完整逐项记录见“记录”", detail)
            self.assertNotIn("时间未知", detail)

    async def test_acceptance_and_reopening_show_final_results_with_or_without_brief(self):
        original = self.store.load()
        for kind in ("brief", "without_brief", "legacy", "stale_brief"):
            with self.subTest(kind=kind):
                board = deepcopy(original)
                task = board.tasks[1]
                run = board.runs[task.run_id]
                if kind == "legacy":
                    run.events = ["执行者报告 · full · " + run.summary, "Pi agent_settled"]
                elif kind != "brief":
                    report = json.loads(run.events[1][len(PREFIX):])
                    if kind == "without_brief":
                        report["report"]["brief"] = None
                    else:
                        report["report"]["summary"] = "不匹配的旧报告。"
                        report["report"]["brief"]["outcome"] = "不应显示的旧成果。"
                    run.events[1] = PREFIX + json.dumps(report, ensure_ascii=False)
                self.store.save(board)
                app = self.app()
                async with app.run_test(size=(80, 24)) as pilot:
                    await pilot.press("right", "r")
                    self.assertIsInstance(app.screen, Confirm)
                    self.assertEqual(app.board.tasks[1].status, "review")
                    await pilot.click("#submit")
                    self.assertEqual(app.board.tasks[1].status, "done")
                    self.assertFalse(app.detail_expanded)
                    compact = self.content(app)
                    self.assertIn("已完成", compact)
                    self.assertNotIn("实现了什么", compact)
                    self.assertIn("验证依据", compact)
                    self.assertNotIn("进展（自报）", compact)
                    self.assertNotIn("不应显示的旧成果", compact)
                    self.assertNotIn("补充范围说明", compact)
                    self.assertEqual(app.query_one("#accept-button", Button).label, "查看成果")
                    if kind == "brief":
                        self.assertIn(" 交付结果（自报） \n• 已恢复历史结论\n\n 已完成 \n", compact)
                        self.assertIn("• 核对历史出处\n• 恢复已有结论\n\n 验证依据 \n", compact)
                        self.assertIn("• 未运行测试；\n• 仅核对历史文档", compact)
                        self.assertNotIn("docs/retained-result.md", compact)
                        headings = ("交付结果", "已完成", "验证依据")
                        bodies = ("已恢复历史结论", "核对历史出处", "未运行测试")
                    else:
                        self.assertIn("\n".join(f"• {line}" for line in run.summary.splitlines()), compact)
                        for check in run.checks:
                            self.assertIn(check, compact)
                        headings = ("已完成", "验证依据")
                        bodies = (*run.summary.splitlines(), *run.checks)
                    rendered = app.query_one("#detail-text", Static).content
                    assert isinstance(rendered, Text)
                    console = Console()
                    for heading in headings:
                        style = rendered.get_style_at_offset(console, compact.index(heading))
                        self.assertIs(style.bold, True)
                        self.assertIsNotNone(style.bgcolor)
                    for body in bodies:
                        style = rendered.get_style_at_offset(console, compact.index(body))
                        self.assertIs(style.bold, False)
                        self.assertIsNone(style.bgcolor)
                    await self.toggle_detail(app, pilot)
                    await pilot.pause()  # Markdown is populated after the detail render.
                    for value in (run.summary, *run.checks, "补充范围说明"):
                        self.assertIn(value, self.content(app))
                saved = (self.root / "tasks.sqlite3").read_bytes()
                reopened = self.app()
                async with reopened.run_test(size=(120, 30)) as pilot:
                    await pilot.press("right")
                    self.assertFalse(reopened.detail_expanded)
                    self.assertEqual(self.content(reopened), compact)
                    self.assertEqual(reopened.board.runs[run.id], run)
                    self.assertEqual(reopened.board.tasks[1].status, "done")
                self.assertEqual((self.root / "tasks.sqlite3").read_bytes(), saved)
        self.assertFalse(self.runtime.root.exists())

    async def test_accepted_long_report_keeps_section_spacing_and_narrow_scroll(self):
        board = self.store.load()
        task = board.tasks[1]
        run = board.runs[task.run_id]
        task.summary = run.summary = "结果正文起始。\n" + "保留完整报告段落。\n" * 25 + "结果正文结尾。"
        task.checks = run.checks = ["第一项证据。\n原始说明。", "完整证据路径/" * 35 + "最后一项证据结尾。"]
        board.accept(task)
        self.store.save(board)
        saved = (self.root / "tasks.sqlite3").read_bytes()
        for size in ((80, 24), (120, 30)):
            with self.subTest(size=size):
                app = self.app()
                async with app.run_test(size=size) as pilot:
                    await pilot.press("right")
                    compact = self.content(app)
                    self.assertIn("\n".join(f"• {line}" for line in task.summary.splitlines()) + "\n\n 验证依据 \n", compact)
                    self.assertIn(task.checks[0] + "\n• " + task.checks[1], compact)
                    self.assertIn("结果正文起始", self.frame(app))
                    scroll = app.query_one("#detail-scroll", VerticalScroll)
                    scroll.scroll_end(animate=False)
                    await pilot.pause()
                    self.assertGreater(scroll.scroll_y, 0)
                    self.assertIn("最后一项证据结尾", self.frame(app))
                    await self.toggle_detail(app, pilot)
                    await pilot.pause()
                    for value in (task.summary, *task.checks):
                        self.assertIn(value, self.content(app))
                    await self.toggle_detail(app, pilot)
                    await pilot.pause()
                    self.assertEqual(self.content(app), compact)
                    self.assertEqual(app.board.tasks, board.tasks)
                    self.assertEqual(app.board.runs, board.runs)
                self.assertEqual((self.root / "tasks.sqlite3").read_bytes(), saved)
        self.assertFalse(self.runtime.root.exists())

    async def test_running_overview_keeps_full_progress_and_remaining_scrollable(self):
        board = self.store.load()
        task = board.tasks[1]
        board.return_work(task, review_requirements("继续核对，保持只读。", "仅隔离临时项目；不运行新试验或修改文件", "列出可核对出处及未运行核验"))
        result = deepcopy(board.queue_real(task, str(uuid4()), str(uuid4()), str(self.root)))
        result.state, result.seq, result.delivery = "working", 1, "progress"
        result.summary = "执行报告原文。\n" * 80 + "报告全文结尾。"
        result.remaining = "尚待核对多个节点；检查与独立复核未完成。\n" * 80 + "限制全文结尾。"
        listed_summary = "\n".join(f"• {line}" for line in result.summary.splitlines())
        listed_remaining = "\n".join(["• 尚待核对多个节点；", "• 检查与独立复核未完成。"] * 80 + ["• 限制全文结尾。"])
        result.events = ["执行者报告 · progress · 旧报告没有结构化简报"]
        board.ingest(result)
        self.store.save(board)
        before = (self.root / "tasks.sqlite3").read_bytes()
        error = "读取异常完整原因。\n" * 50 + "异常全文结尾。"
        agent = Agent(result.agent_id, result.session_id, result.cwd, "核对助手", "provider/model-detail",
                      1, 0, False, False, result.id)
        for size in ((120, 30), (80, 24)):
            with self.subTest(size=size):
                app = self.app()
                async with app.run_test(size=size) as pilot:
                    await pilot.press("right")
                    self.assertNotIn(task.agent, app.card(app.board.tasks[1]).plain)
                    # Inspect one cached frame synchronously, before the next real poll.
                    app.agents[agent.id] = agent
                    app.runtime_errors[result.id] = error
                    app.refresh_tasks()
                    compact = str(app.query_one("#execution-status", Static).content) + "\n" + self.content(app)
                    self.assertIn("最近进展（执行者自报）", compact)
                    self.assertIn("剩余工作 / 下一步", compact)
                    self.assertIn(listed_summary, compact)
                    self.assertIn(listed_remaining, compact)
                    self.assertIn("心跳未知/失联", compact)
                    self.assertIn("进展读取异常", compact)
                    self.assertNotIn("本轮报告已保存", compact)
                    self.assertNotIn("等待执行者报告", compact)
                    card = app.card(app.board.tasks[1]).plain
                    self.assertNotIn(agent.name, card)
                    self.assertIn(agent.name, compact)
                    for value in (agent.id, agent.id[:8], agent.model, "忙碌/失联"):
                        self.assertNotIn(value, card)
                        self.assertNotIn(value, compact)
                    self.assertNotIn("读取异常完整原因", compact)
                    self.assertEqual(app.query_one("#accept-button", Button).label, "查看状态")
                    self.assertIn("需核实执行", str(app.query_one("#next-action", Static).content))
                    scroll = app.query_one("#detail-scroll", VerticalScroll)
                    scroll.scroll_end(animate=False)
                    await pilot.pause()
                    self.assertGreater(scroll.scroll_y, 0)
                    self.assertIn("限制全文结尾", self.frame(app))
                    for button in app.query("#detail-actions Button"):
                        if button.display:
                            self.assertTrue(app.query_one("#inspector").content_region.contains_region(button.region), (button.id, button.region))
                    await self.toggle_detail(app, pilot)
                    await pilot.pause()
                    expanded = self.content(app)
                    for value in (result.summary, result.remaining, error, agent.id, agent.model):
                        self.assertIn(value, expanded)
                    await self.toggle_detail(app, pilot)
                    self.assertFalse(app.detail_expanded)
                    self.assertIn(listed_summary, self.content(app))
                    self.assertIn(listed_remaining, self.content(app))
                    self.assertEqual(app.board.runs[result.id].state, "working")
        self.assertEqual((self.root / "tasks.sqlite3").read_bytes(), before)
        self.assertFalse(self.runtime.root.exists())

    async def test_live_status_refresh_preserves_report_scroll_focus_and_storage(self):
        board = self.store.load()
        task = board.tasks[1]
        board.return_work(task, review_requirements("下一轮仅核对出处。", "仅隔离临时项目；保持只读，不运行新试验或修改文件", "列出可核对出处及未运行核验"))
        run = deepcopy(board.queue_real(task, str(uuid4()), str(uuid4()), str(self.root)))
        run.state, run.seq = "working", 1
        run.events = [encoded_event("acknowledged", "接收本轮", source="system", run_id=run.id, seq=1)]
        board.ingest(run)
        self.store.save(board)
        endpoint = self.runtime.root / run.agent_id
        for directory in (self.runtime.root, endpoint, endpoint / "runs"):
            private_dir(directory, create=True)
        publish(endpoint / "runs" / f"{run.id}.json", asdict(run))
        metadata = {"version": 1, "id": run.agent_id, "session_id": run.session_id, "cwd": run.cwd,
                    "name": "核对助手", "model": "offline", "pid": 1, "heartbeat": int(time.time() * 1000),
                    "online": True, "idle": False, "active_run": run.id}
        publish(endpoint / "agent.json", metadata)
        before = (self.root / "tasks.sqlite3").read_bytes()
        for size in ((120, 30), (80, 24)):
            with self.subTest(size=size):
                observation = activity_record(run, int(time.time() * 1000))
                (endpoint / "activity.json").unlink(missing_ok=True)
                publish(endpoint / "activity.json", observation)
                app = self.app()
                async with app.run_test(size=size) as pilot:
                    await pilot.press("right")
                    await app.poll_runtime()
                    self.assertIn("核对助手", str(app.query_one("#execution-status", Static).content))
                    self.assertIn("已确认执行", str(app.query_one("#execution-status", Static).content))
                    self.assertIn("工具 read", str(app.query_one("#execution-status", Static).content))
                    self.assertNotIn("剩余工作尚未确认", self.content(app))
                    await self.toggle_detail(app, pilot)
                    scroll = app.query_one("#detail-scroll", VerticalScroll)
                    scroll.scroll_end(animate=False)
                    scroll.focus()
                    await pilot.pause()
                    body, position, focus = self.content(app), scroll.scroll_y, app.focused
                    buttons = [(button.id, button.region) for button in app.query("#detail-actions Button")]
                    status = app.query_one("#execution-status", Static)
                    before_tick = str(status.content)
                    with patch("execution_view.time.time", return_value=time.time() + 2):
                        app.refresh_status()
                    self.assertIn("本轮历时", str(status.content))
                    self.assertNotEqual(str(status.content), before_tick)
                    self.assertEqual((self.content(app), scroll.scroll_y, app.focused), (body, position, focus))
                    (endpoint / "agent.json").unlink()
                    metadata["heartbeat"] = int(time.time() * 1000)
                    publish(endpoint / "agent.json", metadata)
                    await app.poll_runtime()
                    await pilot.pause()
                    self.assertEqual((self.content(app), scroll.scroll_y, app.focused), (body, position, focus))
                    self.assertEqual([(button.id, button.region) for button in app.query("#detail-actions Button")], buttons)
                    self.assertEqual(app.query_one("#accept-button", Button).label, "查看进展")
                    self.assertGreater(scroll.size.height, 2)
                    observation.update(seq=2, observed_at=int(time.time() * 1000),
                                       tools=[{"id": "call-2", "name": "bash", "started_at": int(time.time() * 1000)}])
                    (endpoint / "activity.json").unlink()
                    publish(endpoint / "activity.json", observation)
                    await app.poll_runtime()
                    await pilot.pause()
                    self.assertIn("工具 bash", str(app.query_one("#execution-status", Static).content))
                    self.assertEqual((self.content(app), scroll.scroll_y, app.focused), (body, position, focus))
                    observation["seq"] = 1
                    (endpoint / "activity.json").unlink()
                    publish(endpoint / "activity.json", observation)
                    await app.poll_runtime()
                    self.assertIn("当前未知", str(app.query_one("#execution-status", Static).content))
                    self.assertNotIn("工具 bash", str(app.query_one("#execution-status", Static).content))
                    self.assertEqual((app.board.runs[run.id].state, self.content(app)), ("working", body))
                    app.board.tasks[task.id].events.append(encoded_event("stop_requested", "停止意图", source="human", run_id=run.id, seq=1))
                    app.refresh_status()
                    await pilot.pause()
                    self.assertIn("停止请求尚未确认", self.frame(app))
                    app.board.runs[run.id].state = "waiting"
                    app.refresh_status()
                    await pilot.pause()
                    self.assertIn("Pi 等待人工确认", self.frame(app))
                    self.assertEqual((self.content(app), scroll.scroll_y, app.focused), (body, position, focus))
                    self.assertFalse((endpoint / "commands").exists())
                    app.action_activity()
                    await pilot.pause()
                    records = app.screen
                    assert isinstance(records, TaskRecords)
                    saved = deepcopy(records.task_snapshot)
                    await app.poll_runtime()
                    self.assertIs(app.screen, records)
                    self.assertEqual(records.task_snapshot, saved)
                    await pilot.press("escape")
        self.assertEqual((self.root / "tasks.sqlite3").read_bytes(), before)

    async def test_records_select_then_snapshot_and_close_preserves_detail(self):
        board = self.store.load()
        run = board.runs[board.tasks[1].run_id]
        run.events += ["工具开始 · read", "工具返回成功 · bash", "Pi · 一般发言"]
        self.store.save(board)
        before = (self.root / "tasks.sqlite3").read_bytes()
        app = self.app()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.press("right")
            await self.toggle_detail(app, pilot)
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

    async def test_record_groups_toggle_once_by_mouse_and_keyboard(self):
        before = (self.root / "tasks.sqlite3").read_bytes()
        app = self.app()
        async with app.run_test(size=(120, 30)) as pilot:
            app.action_activity()
            await pilot.pause()
            assert isinstance(app.screen, TaskRecords)
            tree = app.screen.query_one("#records-tree", Tree)
            group = tree.root.children[0]
            for size in ((120, 30), (80, 24)):
                await pilot.resize_terminal(*size)
                for control in ("arrow", "label", "enter", "space"):
                    group.collapse()
                    tree.move_cursor(group)
                    tree.focus()
                    for expanded in (True, False):
                        with self.subTest(size=size, control=control, expanded=expanded):
                            await pilot.pause()
                            if control in ("arrow", "label"):
                                # Hit the rendered arrow/title, not a guessed screen coordinate.
                                positions = [(x, y) for y in range(tree.size.height)
                                             for x in range(tree.size.width)
                                             if (meta := tree.get_style_at(x, y).meta).get("line") == group.line
                                             and bool(meta.get("toggle")) == (control == "arrow")]
                                self.assertTrue(positions, f"Missing {control} click target")
                                self.assertTrue(await pilot.click(tree, offset=positions[0]))
                            else:
                                await pilot.press(control)
                            await pilot.pause()
                            self.assertEqual(group.is_expanded, expanded)
            await pilot.press("escape")
            self.assertIs(app.screen, app.screen_stack[0])
        self.assertEqual((self.root / "tasks.sqlite3").read_bytes(), before)
        self.assertFalse(self.runtime.root.exists())

    async def test_round_groups_order_by_revision_not_clock_and_locator_is_read_only(self):
        board = self.store.load()
        task = board.tasks[1]
        previous = board.runs[task.run_id]
        original = self.root / "original.jsonl"
        original.write_text(json.dumps({"type": "session", "id": previous.session_id, "cwd": previous.cwd}) + "\n")
        previous.events.insert(0, encoded_event("origin", str(original), source="system", run_id=previous.id, seq=1))
        board.return_work(task, review_requirements("只补录出处。", "仅隔离临时项目；保持只读，不运行新试验或修改文件", "列出可核对出处及未运行核验"))
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
            self.assertIn("本轮报告已保存", self.content(app))
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
        board.return_work(task, review_requirements("下一轮只补录出处，不重跑实验。", "仅隔离临时项目；保持只读，不运行新试验或修改文件", "列出可核对出处及未运行核验"))
        board.edit(task, task.title, task.project, "新的任务说明：列出待核对条目。")
        self.store.save(board)
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("right")
            self.assertIn("没有本轮交付", self.content(app))
            self.assertIn("核对投递", self.content(app))
            await self.toggle_detail(app, pilot)
            detail = self.content(app)
            self.assertIn("新的任务说明", detail)
            self.assertIn("待派发的补充要求", detail)
            self.assertIn("下一轮只补录出处", detail)
            self.assertNotIn("已恢复历史结论", detail)
            self.assertNotIn("docs/retained-result.md", detail)
            self.assertNotIn("本轮只核对历史出处", detail)
            self.assertNotEqual(app.query_one("#accept-button", Button).label, "核对交付 r")
        result = deepcopy(board.queue_real(task, str(uuid4()), str(uuid4()), str(self.root)))
        result.state, result.seq, result.delivery = "working", 1, "progress"
        result.summary, result.remaining = "本轮已开始补录。", "仍需核对末项。"
        result.events = ["执行者报告 · progress · 本轮最新进展。"]
        board.ingest(result)
        self.store.save(board)
        app = self.app()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.press("right")
            self.assertIn("剩余工作 / 下一步", self.content(app))
            self.assertIn("仍需核对末项", self.content(app))
            await self.toggle_detail(app, pilot)
            detail = self.content(app)
            self.assertIn("仍需核对末项", detail)
            self.assertIn("本轮已开始补录", detail)
            self.assertIn("本轮补充要求 · 派发时固定", detail)
            self.assertIn("下一轮只补录出处", detail)
            self.assertNotIn("已恢复历史结论", detail)
            self.assertNotIn(previous_id, detail)
            self.assertNotIn("docs/retained-result.md", detail)
        self.assertFalse(self.runtime.root.exists())

    async def test_report_before_idle_permission_wait_and_failure_never_offer_acceptance(self):
        board = self.store.load()
        task = board.tasks[1]
        board.return_work(task, review_requirements("补全出处。", "仅隔离临时项目；保持只读，不运行新试验或修改文件", "列出可核对出处及未运行核验"))
        result = deepcopy(board.queue_real(task, str(uuid4()), str(uuid4()), str(self.root)))
        result.summary, result.checks = "完整内容已自报，但尚未空闲。", ["自报依据，未独立验证。"]
        result.events = [encoded_event("full", "执行者报告 · full", source="executor", run_id=result.id, seq=1,
                                      report=Report(result.summary, result.checks, "", Brief(["报告已提交"], "仅代表自报", "未独立核实")))]
        states = [("working", "full", "执行尚未完全结束"), ("waiting", "full", "等待人工确认"),
                  ("failed", "", "执行失败")]
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
                    self.assertIn(next_step, str(app.query_one("#execution-status", Static).content))
                    self.assertNotEqual(app.query_one("#accept-button", Button).label, "核对交付 r")
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
            self.assertIn("本轮报告已保存", self.content(app))  # Old brief no longer matches current result.
            self.assertNotIn("已恢复历史结论", self.content(app))
            await self.toggle_detail(app, pilot)
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
        app = Waggle(board)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.press("right")
            self.assertIn("阶段交付", self.content(app))
            self.assertIn("有剩余工作或限制", self.content(app))
            self.assertNotIn("第二阶段实现与验证尚未完成", self.content(app))
            self.assertNotIn("待派发的补充要求", self.content(app))
            self.assertIn("全部虚构", self.content(app))
            await self.toggle_detail(app, pilot)
            self.assertIn("第二阶段实现与验证尚未完成", self.content(app))
        board.return_work(task, review_requirements("补全第二阶段。", "仅隔离临时项目的已确认第二阶段", "列出隔离阶段交付及未运行核验"))
        board.queue(task)
        board.simulate(task, "ack")
        board.simulate(task, "progress")
        app = Waggle(board)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.press("right")
            self.assertNotIn("阶段交付", self.content(app))
            self.assertIn("全部虚构", self.content(app))

    async def test_reopening_resets_to_compact_overview_with_reachable_verification(self):
        for size in ((165, 51), (80, 24), (120, 30)):
            with self.subTest(size=size):
                app = self.app()
                async with app.run_test(size=size) as pilot:
                    await pilot.press("enter")
                    self.assertIn("已恢复历史结论", self.frame(app))
                    self.assertIn("未运行测试", self.content(app))
                    scroll = app.query_one("#detail-scroll", VerticalScroll)
                    # Section spacing may put verification below the fold, never omit it.
                    scroll.scroll_end(animate=False)
                    await pilot.pause()
                    self.assertIn("未运行测试", self.frame(app))
                    await self.toggle_detail(app, pilot)
                    await pilot.pause()  # Let expanded Markdown establish scroll height.
                    scroll.scroll_end(animate=False)
                    await pilot.pause()
                    self.assertGreater(scroll.scroll_y, 0)
                    await pilot.press("escape", "enter")
                    self.assertFalse(app.detail_expanded)
                    self.assertEqual(scroll.scroll_y, 0)
                    self.assertIn("已恢复历史结论", self.frame(app))
                    for button in app.query("#detail-actions Button"):
                        if button.display:
                            self.assertTrue(app.query_one("#inspector").content_region.contains_region(button.region), (button.id, button.region))

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
            self.assertIn("索取可审提案", self.frame(app))
            self.assertIn("现在不需要你批准", self.content(app))
            self.assertIn("尚未核对第二阶段", detail_text(task, run).plain)
            self.assertEqual(app.query_one("#accept-button", Button).label, "索取可审提案 b")
            await self.toggle_detail(app, pilot)
            self.assertIn("尚未核对第二阶段", self.content(app))
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
