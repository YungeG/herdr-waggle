"""Task status is an observation, never a new execution or acceptance authority."""

import json
import unittest
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import patch
from uuid import uuid4

from activity import Activity
from app import Waggle
from delivery import detail_text, overview_text
from execution_view import status_text
from journal import PREFIX, Report, encoded_event
from model import Board, Run, Task
from runtime import Agent
from test_activity import activity_record


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.now = 1_800_000_000_000
        self.attempt = Run(str(uuid4()), 2, str(uuid4()), str(uuid4()), "/fixture/project", "检查历史", "原始范围", "派发要求", state="working", seq=4)
        self.task = Task(2, "检查历史", "项目", status="running", execution="pi", run="working", run_id=self.attempt.id, agent=self.attempt.agent_id)
        self.agent = Agent(self.attempt.agent_id, self.attempt.session_id, self.attempt.cwd, "核对助手", "model", 1, self.now, True, False, self.attempt.id)

    def view(self, **kwargs):
        return status_text(self.task, self.attempt, self.agent, now_ms=self.now, round_number=2, **kwargs).plain

    def test_working_has_identity_state_and_honest_activity_not_report_boilerplate(self):
        text = self.view()
        for value in ("第 2 轮", "核对助手", "连接正常", "已确认执行", "当前步骤未上报"):
            self.assertIn(value, text)
        for value in ("思考中", "卡死", "暂无简报", "验证通过", self.attempt.id):
            self.assertNotIn(value, text)

    def test_identity_and_freshness_override_live_claims(self):
        for agent in (None, replace(self.agent, heartbeat=self.now - 11_000),
                      replace(self.agent, heartbeat=self.now + 3000), replace(self.agent, online=False),
                      replace(self.agent, session_id=str(uuid4())), replace(self.agent, cwd="/fixture/other"),
                      replace(self.agent, active_run=str(uuid4())), replace(self.agent, idle=True, active_run="")):
            with self.subTest(agent=agent):
                text = status_text(self.task, self.attempt, agent, now_ms=self.now).plain
                self.assertNotIn("连接正常", text)
                self.assertIn("核实", text)
                self.assertNotIn("已停止", text)
        self.assertIn("读取异常", self.view(issue="do not expose internal path"))
        self.assertNotIn("do not expose", self.view(issue="do not expose internal path"))

    def test_cards_distinguish_unconfirmed_execution_without_changing_history(self):
        board = Board(live=True)
        board.tasks[self.task.id] = self.task
        board.runs[self.attempt.id] = self.attempt
        app = Waggle(board)
        before = deepcopy((self.task, self.attempt))
        cases = [(self.agent, "", False), (None, "", True),
                 (replace(self.agent, heartbeat=self.now - 11_000), "", True),
                 (replace(self.agent, heartbeat=self.now + 3000), "", True),
                 (replace(self.agent, online=False), "", True),
                 (replace(self.agent, session_id=str(uuid4())), "", True),
                 (replace(self.agent, active_run=str(uuid4())), "", True),
                 (replace(self.agent, idle=True), "", True),
                 (self.agent, "unreadable snapshot", True)]
        with patch("app.time.time", return_value=self.now / 1000):
            for agent, issue, unconfirmed in cases:
                with self.subTest(agent=agent, issue=issue):
                    app.agents = {self.agent.id: agent} if agent else {}
                    app.runtime_errors = {self.attempt.id: issue} if issue else {}
                    for compact in (False, True):
                        card = app.card(self.task, compact).plain
                        if unconfirmed:
                            self.assertIn("执行待核实", card)
                            self.assertNotIn("运行中", card)
                            self.assertNotIn("执行中", card)
                        else:
                            self.assertIn("执行中", card)
                            self.assertNotIn("执行待核实", card)
                    self.assertEqual((self.task, self.attempt), before)
        self.attempt.state = "failed"
        self.task.status, self.task.run = "blocked", "finished"
        self.assertNotIn("执行待核实", app.card(self.task).plain)

    def test_phase_matrix_does_not_change_task_or_run(self):
        for state, delivery, expected in (("queued", "", "等待接收"), ("waiting", "", "人工确认"),
                ("working", "full", "尚未完全结束"), ("finished", "partial", "阶段交付"),
                ("finished", "full", "核对交付"), ("failed", "", "执行失败"),
                ("interrupted", "", "归属中断"), ("stopped", "", "已确认停止")):
            with self.subTest(state=state, delivery=delivery):
                self.attempt.state, self.attempt.delivery = state, delivery
                self.assertIn(expected, self.view())
                self.assertEqual((self.attempt.state, self.attempt.delivery, self.task.status), (state, delivery, "running"))

    def test_activity_cannot_escape_run_binding_connection_or_terminal_guards(self):
        snapshot = Activity.from_record(activity_record(self.attempt, self.now), self.attempt)
        self.assertIn("工具 read", self.view(activity=snapshot))
        self.attempt.state = "queued"
        self.assertNotIn("工具 read", self.view(activity=snapshot))
        self.attempt.state = "working"
        self.assertNotIn("工具 read", self.view(activity=replace(snapshot, run_id=str(uuid4()))))
        original_agent = self.agent
        for agent in (replace(original_agent, active_run=str(uuid4())), replace(original_agent, heartbeat=0)):
            self.agent = agent
            text = self.view(activity=snapshot)
            self.assertNotIn("工具 read", text)
            self.assertIn("当前未知", text)
        self.agent = original_agent
        text = self.view(activity=replace(snapshot, bound=False))
        self.assertIn("最后记录", text)
        self.assertIn("归属未确认", text)
        self.attempt.state = "finished"
        self.assertNotIn("工具 read", self.view(activity=snapshot))
        self.task.status = "done"
        self.assertIn("已人工验收", self.view(activity=snapshot, activity_issue="read failed"))

    def test_stop_intent_is_not_confirmation_and_other_round_is_ignored(self):
        self.task.events = [encoded_event("stop_requested", "人请求停止", source="human", run_id=str(uuid4()), seq=4)]
        self.assertNotIn("已请求停止", self.view())
        self.task.events.append(encoded_event("stop_requested", "人请求停止", source="human", run_id=self.attempt.id, seq=4))
        text = self.view()
        self.assertIn("已请求停止", text)
        self.assertIn("尚未确认", text)
        self.assertNotIn("已确认停止", text)

    def test_terminal_or_returned_run_never_uses_current_agent_liveness(self):
        self.attempt.state, self.attempt.delivery = "finished", "full"
        text = status_text(self.task, self.attempt, None, issue="old read error", now_ms=self.now).plain
        self.assertNotIn("心跳", text)
        self.assertNotIn("读取异常", text)
        self.task.status = "done"
        self.assertIn("已人工验收", self.view())
        self.task.run = "none"
        self.task.status = "ready"
        text = self.view()
        self.assertIn("尚未派发", text)
        self.assertNotIn("已确认执行", text)

    def test_requirements_are_scope_not_reported_remaining_or_a_blocker(self):
        self.task.remaining = self.attempt.requirements
        compact = overview_text(self.task, self.attempt).plain
        expanded = detail_text(self.task, self.attempt).plain
        self.assertNotIn("有剩余", compact)
        self.assertNotIn("有阻塞", compact)
        self.assertNotIn("剩余工作尚未确认", expanded)
        self.assertIn("本轮补充要求", expanded)
        self.assertIn(self.attempt.requirements, expanded)
        self.assertNotIn("尚未提供交付正文", expanded)

    def test_missing_or_other_round_never_claims_not_started_or_live(self):
        text = status_text(self.task, None).plain
        self.assertIn("原轮次信息缺失", text)
        self.assertNotIn("尚未派发", text)
        self.attempt.id = str(uuid4())
        self.assertIn("轮次身份不匹配", self.view())
        self.assertNotIn("连接正常", self.view())

    def timed_event(self, kind, age_ms, seq, *, source="system", run_id=None, report=None):
        raw = encoded_event(kind, "实际记录", source=source, run_id=run_id or self.attempt.id, seq=seq, report=report)
        record = json.loads(raw[len(PREFIX):])
        record["at"] = datetime.fromtimestamp((self.now - age_ms) / 1000, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        return PREFIX + json.dumps(record, ensure_ascii=False)

    def test_elapsed_uses_dispatch_and_report_age_not_heartbeat_or_render_time(self):
        self.task.events = [self.timed_event("dispatched", 3_661_000, 0, source="human")]
        self.attempt.events = [self.timed_event("acknowledged", 3_000_000, 1),
                               self.timed_event("progress", 125_000, 2, source="executor", report=Report("已追踪来源", [], "待验证"))]
        before = deepcopy((self.task, self.attempt))
        text = self.view()
        self.assertIn("本轮历时 · 1小时 1分 1秒（自派发，含等待）", text)
        self.assertIn("距最近报告 · 2分 5秒", text)
        later = status_text(self.task, self.attempt, replace(self.agent, heartbeat=self.now + 1000), now_ms=self.now + 1000).plain
        self.assertIn("1小时 1分 2秒", later)
        self.assertIn("2分 6秒", later)
        self.assertEqual((self.task, self.attempt), before)

    def test_elapsed_fallback_missing_anchors_and_waits(self):
        self.assertIn("本轮历时 · 时间未知", self.view())
        self.assertIn("距最近报告 · 时间未知", self.view())
        self.attempt.events = [self.timed_event("acknowledged", 90_061_000, 1)]
        self.assertIn("1天 1小时 1分 1秒（自确认，含等待）", self.view())
        self.task.events = [self.timed_event("dispatched", 90_000, 0, source="human")]
        for state in ("queued", "waiting"):
            with self.subTest(state=state):
                self.attempt.state = state
                self.assertIn("1分 30秒（自派发，含等待）", self.view())
                self.assertNotIn("实际工作耗时", self.view())

    def test_timing_ignores_foreign_legacy_and_future_revision_records(self):
        other = str(uuid4())
        self.task.events = [self.timed_event("dispatched", 10_000, 0, source="human", run_id=other)]
        self.attempt.events = ["Pi 已确认对应任务消息 · 没有时间", "执行者报告 · progress · 旧记录",
                               self.timed_event("acknowledged", 10_000, 1, run_id=other),
                               self.timed_event("acknowledged", 10_000, 5),
                               self.timed_event("progress", 1000, 5, source="executor", report=Report("未来修订", [], "待验证"))]
        text = self.view()
        self.assertIn("本轮历时 · 时间未知", text)
        self.assertIn("距最近报告 · 时间未知", text)
        self.assertNotIn("自确认", text)

    def test_report_age_uses_latest_revision_even_when_clock_moves_backward(self):
        self.task.events = [self.timed_event("dispatched", 60_000, 0, source="human")]
        self.attempt.events = [self.timed_event("progress", 10_000, 2, source="executor", report=Report("旧报告", [], "待核对")),
                               self.timed_event("progress", 20_000, 3, source="executor", report=Report("新报告", [], "待核对"))]
        self.assertIn("距最近报告 · 20秒", self.view())
        self.attempt.events[-1] = self.timed_event("progress", 70_000, 3, source="executor", report=Report("时钟异常", [], "待核对"))
        self.assertIn("距最近报告 · 时间未知（时间异常）", self.view())
        self.attempt.events[-1] = self.timed_event("progress", -1000, 3, source="executor", report=Report("将来的时钟", [], "待核对"))
        self.assertIn("距最近报告 · 时间未知（时间异常）", self.view())

    def test_future_start_does_not_get_clamped_to_zero(self):
        self.task.events = [self.timed_event("dispatched", -1000, 0, source="human")]
        text = self.view()
        self.assertIn("本轮历时 · 时间未知（时间异常）", text)
        self.assertNotIn("0秒（自派发", text)

    def test_terminal_elapsed_freezes_at_matching_end_not_report_or_acceptance(self):
        self.task.events = [self.timed_event("dispatched", 100_000, 0, source="human")]
        for state, kind in (("finished", "settled"), ("failed", "failed"), ("stopped", "stopped"),
                            ("interrupted", "interrupted"), ("abandoned", "abandoned")):
            with self.subTest(state=state):
                self.attempt.state = state
                self.attempt.events = [self.timed_event(kind, 10_000, 4, source="human" if kind == "abandoned" else "system")]
                text = self.view()
                self.assertIn("本轮历时 · 1分 30秒", text)
                self.assertNotIn("距最近报告", text)
                later = status_text(self.task, self.attempt, None, now_ms=self.now + 60_000).plain
                self.assertIn("本轮历时 · 1分 30秒", later)
                if state == "abandoned":
                    self.assertIn("至人工解除", text)
        self.attempt.state = "finished"
        self.attempt.events = [self.timed_event("full", 10_000, 3, source="executor", report=Report("交付自报", ["证据"], "")),
                               self.timed_event("stop_requested", 10_000, 4, source="human"),
                               self.timed_event("accepted", 5000, 4, source="human")]
        self.assertIn("本轮历时 · 时间未知（缺少结束时间）", self.view())
        for age in (110_000, -1000):
            self.attempt.events = [self.timed_event("settled", age, 4)]
            self.assertIn("本轮历时 · 时间未知（时间异常）", self.view())

    def test_latest_key_update_uses_revision_and_original_time_not_heartbeat(self):
        for seq, kind, at in ((2, "acknowledged", "2026-01-02T00:00:00.000Z"), (3, "warning", "2001-01-01T00:00:00.000Z")):
            event = json.loads(encoded_event(kind, "实际事件", source="system", run_id=self.attempt.id, seq=seq)[len(PREFIX):])
            event["at"] = at
            self.attempt.events.append(PREFIX + json.dumps(event))
        text = self.view()
        self.assertIn("需要核实", text)
        self.assertIn("2001", text)
        self.assertIn("已确认执行", text)
        self.assertNotIn("执行失败", text)
