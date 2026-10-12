"""Primary detail action stays a pure projection of known task/run facts."""
import unittest
from uuid import uuid4

from decisions import (
    Answer,
    DecisionSpec,
    Target,
    feedback_question_key,
    prepare_decision_intent,
    preview_decisions,
    proposal_requirements,
    question_requirements,
)
from model import Run, Task
from store import encode
from task_actions import DecisionItem, decision_items, project_primary_action
from test_decisions import decision_board, repeated_evidence_board, spec_record


class PrimaryActionTests(unittest.TestCase):
    def assert_action(self, task, run, action, label, **facts):
        actual = project_primary_action(task, run, **facts)
        self.assertEqual((actual.action, actual.label), (action, label))

    def test_unstarted_and_retained_tasks(self):
        self.assert_action(None, None, "new_task", "新建任务 n", has_directory=False)
        task = Task(1, "归档", "项目", retention="archived")
        self.assert_action(task, None, "activity", "查看记录", has_directory=True)
        self.assert_action(Task(1, "收集箱", "收集箱"), None, "edit_task", "补充项目目录 e", has_directory=False)
        self.assert_action(Task(1, "待安排", "项目"), None, "assign", "选择执行者 a", has_directory=True)
        self.assert_action(Task(1, "待执行", "项目", status="ready", agent="甲"), None, "start", "开始任务 s", has_directory=True)

    def test_simulation_and_delivery_states(self):
        self.assert_action(Task(1, "排队", "项目", status="ready", agent="甲", run="queued"), None,
                           "read_report", "查看派发状态", has_directory=True)
        self.assert_action(Task(1, "执行", "项目", status="running", agent="甲", run="working"), None,
                           "read_report", "查看进展", has_directory=True)
        self.assert_action(Task(1, "阶段", "项目", status="review", agent="甲", run="finished", remaining="还有"), None,
                           "read_report", "查看报告", has_directory=True)
        self.assert_action(Task(1, "完整", "项目", status="review", agent="甲", run="finished", full_delivery=True), None,
                           "review", "核对交付 r", has_directory=True)
        self.assert_action(Task(1, "完成", "项目", status="done", agent="甲", run="finished"), None,
                           "read_report", "查看成果", has_directory=True)

    def test_ended_blocked_report_offers_supplement_not_retry_or_acceptance(self):
        task = Task(1, "实现", "项目", status="blocked", run="finished", execution="pi", run_id="run")
        run = Run("run", 1, "agent", "session", "/fixture/project", "实现", "我们开始实现", "重试",
                  state="failed", delivery="blocked", remaining="请补充原方案、功能范围和验收标准。")
        self.assert_action(task, run, "prepare_recovery", "准备恢复 · 仅保存", has_directory=True)
        run.state = "working"
        self.assert_action(task, run, "read_report", "查看进展", has_directory=True)
        for state in ("failed", "abandoned", "stopped", "interrupted"):
            run.state = state
            run.delivery = "" if state == "failed" else "blocked"
            self.assert_action(task, run, "prepare_recovery", "准备恢复 · 仅保存", has_directory=True)
        run.state, run.delivery, run.id = "failed", "blocked", "other-run"
        self.assert_action(task, run, "read_report", "核实执行", has_directory=True)

    def test_question_missing_selected_answer_is_not_full_proposal_or_approval(self):
        source = str(uuid4()) + ":1"
        question = f'请解释批次\n旧反馈 {source}（仅作为本轮待答问题，不是许可）："原始来源"'
        task = Task(4, "术语", "项目", status="review", run="finished", execution="pi", run_id="question-run")
        run = Run("question-run", 4, "agent", "session", "/fixture/project", task.title, "", question_requirements(question),
                  state="finished", delivery="partial", remaining="所选问题未答")
        action = project_primary_action(task, run, has_directory=True)
        self.assertEqual((action.action, action.label), ("read_report", "答复未齐 · 查看报告"))
        self.assertNotIn("提案", action.reason)
        self.assertIn("旧问题", action.reason)

    def test_missing_proposal_is_agent_handoff_not_a_new_human_approval(self):
        task = Task(4, "审计", "项目", status="review", run="finished", execution="pi", run_id="proposal-run")
        run = Run("proposal-run", 4, "agent", "session", "/fixture/project", task.title, "", proposal_requirements("请列缺口"),
                  state="finished", delivery="partial", remaining="仍缺原始账本")
        action = project_primary_action(task, run, has_directory=True)
        self.assertEqual((action.action, action.label), ("read_report", "提案未交 · 查看报告"))
        self.assertIn("尚未交齐可审", action.reason)
        self.assertIn("现在不需要你批准", action.reason)

    def test_unrelated_or_legacy_decision_does_not_close_selected_old_question(self):
        source = str(uuid4()) + ":1"
        question = f"请交提案\n旧反馈 {source}（仅作为本轮待答问题，不是许可）：\"给已用量原账本\""
        task = Task(4, "审计", "项目", status="review", run="finished", execution="pi", run_id="proposal-run")
        run = Run("proposal-run", 4, "agent", "session", "/fixture/project", task.title, "", proposal_requirements(question),
                  state="finished", delivery="partial", remaining="旧账本缺失")
        unrelated = DecisionSpec.from_record(spec_record("approval", key="unrelated"))
        action = project_primary_action(task, run, has_directory=True,
            decisions=(DecisionItem(task.id, "report", unrelated, "待决定", True),))
        self.assertEqual(action.label, "提案未交 · 查看报告")
        self.assertIn("旧问题", action.reason)

    def test_malformed_source_in_user_proposal_fails_closed_without_crashing_workbench(self):
        forged = "a" * 36 + ":1"
        question = f"请交提案\n旧反馈 {forged}（仅作为本轮待答问题，不是许可）：\"误贴\""
        task = Task(4, "审计", "项目", status="review", run="finished", execution="pi", run_id="proposal-run")
        run = Run("proposal-run", 4, "agent", "session", "/fixture/project", task.title, "", proposal_requirements(question),
                  state="finished", delivery="partial", remaining="原件未齐")
        unrelated = DecisionSpec.from_record(spec_record(key="unrelated", missing=["负责人原件缺失"], routing={
            "audience": "owner_evidence", "evidence_needs": [{"gap": "负责人原件缺失", "owner": "研究负责人", "source": "原账本"}],
            "advice": {"status": "insufficient", "value": "", "unit": "", "conditions": "", "evidence": [],
                       "uncertainty": "原件未到", "owner_signed": False},
        }))
        action = project_primary_action(task, run, has_directory=True,
            decisions=(DecisionItem(task.id, "report", unrelated, "缺材料", True),))
        self.assertEqual(action.label, "提案未交 · 查看报告")
        self.assertNotEqual(action.action, "start")

    def test_selected_question_moves_from_owner_gap_to_review_only_when_same_key_is_supported(self):
        source = str(uuid4()) + ":1"
        key = feedback_question_key(source)
        req = proposal_requirements(f"请交提案\n旧反馈 {source}（仅作为本轮待答问题，不是许可）：\"清单\"")
        task = Task(4, "审计", "项目", status="review", run="finished", execution="pi", run_id="proposal-run")
        run = Run("proposal-run", 4, "agent", "session", "/fixture/project", task.title, "", req,
                  state="finished", delivery="partial", remaining="负责人尚缺原件")
        missing = DecisionSpec.from_record(spec_record(key=key, kind="approval", missing=["原账本缺失"], routing={
            "audience": "owner_evidence", "evidence_needs": [{"gap": "原账本缺失", "owner": "研究负责人", "source": "原账本"}],
            "advice": {"status": "insufficient", "value": "", "unit": "", "conditions": "", "evidence": [],
                       "uncertainty": "原件未到", "owner_signed": False},
        }))
        waiting = project_primary_action(task, run, has_directory=True,
            decisions=(DecisionItem(task.id, "report", missing, "缺材料", True),))
        self.assertIn("负责人", waiting.label)
        self.assertNotIn("逐项决定", waiting.label)
        supported = DecisionSpec.from_record(spec_record(key=key, kind="approval", missing=[], routing={
            "audience": "user_decision", "evidence_needs": [],
            "advice": {"status": "supported", "value": "方案A", "unit": "项", "conditions": "仅旧目录可用",
                       "evidence": ["方案 A/B 已说明成本"], "uncertainty": "仍须人工核实", "owner_signed": True},
        }))
        ready = project_primary_action(task, run, has_directory=True,
            decisions=(DecisionItem(task.id, "report", supported, "待决定", True),))
        self.assertIn("逐项决定", ready.label)
        self.assertNotEqual(ready.action, "start")

    def test_routed_owner_evidence_is_not_a_human_approval_request(self):
        spec = DecisionSpec.from_record(spec_record(key="owner", missing=["原始账本缺失"], routing={
            "audience": "owner_evidence", "evidence_needs": [{"gap": "原始账本缺失", "owner": "研究负责人", "source": "原始账本"}],
            "advice": {"status": "insufficient", "value": "", "unit": "", "conditions": "", "evidence": [],
                       "uncertainty": "原件未到", "owner_signed": False},
        }))
        task = Task(4, "审计", "项目", status="review", run="finished", execution="pi", run_id="r")
        run = Run("r", 4, "agent", "session", "/fixture/project", task.title, "", proposal_requirements("请交提案"),
                  state="finished", delivery="partial", remaining="缺原始账本")
        action = project_primary_action(task, run, has_directory=True,
            decisions=(DecisionItem(task.id, "report-id", spec, "缺材料", True),))
        self.assertEqual(action.action, "decisions")
        self.assertIn("负责人", action.label)
        self.assertIn("不需你批准", action.reason)

    def test_only_current_ended_partial_opens_next_round_without_execution(self):
        task = Task(4, "能力审计", "项目", status="review", run="finished", execution="pi", run_id="run")
        run = Run("run", 4, "agent", "session", "/fixture/project", task.title, "", "",
                  state="finished", delivery="partial", remaining="固定输入与验证范围")
        action = project_primary_action(task, run, has_directory=True)
        self.assertEqual((action.action, action.label), ("return_work", "索取可审提案 b"))
        self.assertIn("确认后仅补证", action.reason)
        self.assertIn("你不用批准", action.reason)
        for field, value in (("id", "other"), ("task_id", 9), ("state", "working"), ("state", "interrupted")):
            old = getattr(run, field)
            setattr(run, field, value)
            with self.subTest(field=field, value=value):
                self.assertNotEqual(project_primary_action(task, run, has_directory=True).action, "return_work")
            setattr(run, field, old)
        task.run, task.status = "none", "ready"
        self.assertEqual(project_primary_action(task, run, has_directory=True).label, "补齐核查范围 b")

    def test_decision_projection_is_readonly_and_tracks_formal_not_draft_answers(self):
        specs = [DecisionSpec.from_record(spec_record()),
                 DecisionSpec.from_record(spec_record("budget", key="limits")),
                 DecisionSpec.from_record(spec_record("approval", key="evidence", missing=["缺少对照"]))]
        board, task, run = decision_board(specs)
        before = encode(board)
        rows = decision_items(board, task)
        self.assertEqual([row.state for row in rows], ["待决定", "缺预算账本", "缺材料"])
        action = project_primary_action(task, run, has_directory=True, decisions=rows)
        self.assertEqual(action.action, "decisions")
        self.assertIn("3", action.label)
        self.assertIn("2 项缺材料", action.reason)
        self.assertEqual(encode(board), before)
        answers = (Answer("preference", "a"), Answer(), Answer("request_info"))
        prepare_decision_intent(board, preview_decisions(board, task.id, answers, Target("draft")))
        self.assertTrue(all(row.pending for row in decision_items(board, task)))
        prepare_decision_intent(board, preview_decisions(board, task.id, answers, Target("saved")))
        rows = decision_items(board, task)
        self.assertEqual([row.pending for row in rows], [False, True, True])
        self.assertEqual(rows[0].state, "已记录意见 · 仅本地保存（未发送）")
        self.assertEqual(len(board.runs), 1)

    def test_same_missing_material_after_one_request_is_visible_without_automatic_retry(self):
        board, task, run = repeated_evidence_board()
        before = encode(board)
        row, = decision_items(board, task)
        self.assertEqual(row.state, "等待负责人材料 · 无新证据")
        self.assertTrue(row.pending)
        primary = project_primary_action(task, run, has_directory=True, decisions=(row,))
        self.assertEqual(primary.action, "decisions")
        self.assertEqual(primary.state, "等待负责人材料")
        self.assertIn("不默认", primary.reason)
        self.assertEqual(encode(board), before)
        fresh, task, run = repeated_evidence_board(fresh=True)
        row, = decision_items(fresh, task)
        self.assertNotIn("等待负责人材料", row.state)

    def test_saved_request_stays_pending_and_shows_unsent_state(self):
        spec = DecisionSpec.from_record(spec_record(missing=["业务前提待明确", "原失败证据待核对"]))
        board, task, run = decision_board([spec])
        prepare_decision_intent(board, preview_decisions(board, task.id, (Answer("request_info"),), Target("saved")))
        row, = decision_items(board, task)
        self.assertTrue(row.pending)
        self.assertEqual(row.state, "已要求补齐 · 仅本地保存（未发送）")
        self.assertEqual(project_primary_action(task, run, has_directory=True, decisions=(row,)).action, "decisions")
        self.assertEqual(len(board.runs), 1)

    def test_undecided_comment_saved_locally_is_the_visible_next_action_not_a_decision(self):
        specs = (DecisionSpec.from_record(spec_record(missing=["原始回执缺失"])),
                 DecisionSpec.from_record(spec_record("budget", key="limits", missing=["旧账本缺失"])))
        board, task, run = decision_board(specs)
        prepare_decision_intent(board, preview_decisions(board, task.id,
            (Answer(comment="是"), Answer()), Target("saved")))
        before = encode(board)
        rows = decision_items(board, task)
        self.assertEqual(rows[0].state, "反馈仅保存 · 未发送")
        self.assertEqual(rows[1].state, "缺预算账本")
        self.assertTrue(rows[0].pending)
        primary = project_primary_action(task, run, has_directory=True, decisions=rows)
        self.assertEqual(primary.action, "decisions")
        self.assertIn("核对已存反馈", primary.label)
        self.assertIn("未发送", primary.reason)
        self.assertIn("负责人原始材料", primary.reason)
        self.assertEqual(len(board.runs), 1)
        self.assertEqual(encode(board), before)

    def test_submitted_evidence_request_is_not_labeled_delivered_or_complete(self):
        spec = DecisionSpec.from_record(spec_record(missing=["原始依据待核对"]))
        board, task, run = decision_board([spec], live=True)
        preview = preview_decisions(board, task.id, (Answer("request_info"),),
            Target("live_supplement", run.agent_id, run.session_id, run.cwd))
        prepare_decision_intent(board, preview)
        row, = decision_items(board, task)
        self.assertTrue(row.pending)
        self.assertEqual(row.state, "已要求补齐 · 已提交补证（接收待核实）")
        self.assertEqual(len(board.runs), 1)

    def test_decisions_never_override_unknown_waiting_stop_or_retention(self):
        board, task, run = decision_board(live=True)
        rows = decision_items(board, task)
        self.assertEqual(project_primary_action(task, run, has_directory=True, decisions=rows).action, "decisions")
        for needs_verification, stop_requested, label in [(True, False, "核实执行"), (False, True, "查看停止状态")]:
            self.assertEqual(project_primary_action(task, run, has_directory=True, decisions=rows,
                needs_verification=needs_verification, stop_requested=stop_requested).label, label)
        for state, label in [("waiting", "查看确认信息"), ("queued", "查看派发状态")]:
            run.state = state
            self.assertEqual(project_primary_action(task, run, has_directory=True, decisions=rows).label, label)
        self.assertEqual(project_primary_action(task, None, has_directory=True, decisions=rows).label, "核实执行")
        task.retention = "archived"
        self.assertEqual(project_primary_action(task, run, has_directory=True, decisions=rows).action, "activity")
        board, task, run = decision_board([DecisionSpec.from_record(spec_record(missing=["缺少依据"]))])
        self.assertEqual(project_primary_action(task, run, has_directory=True, decisions=decision_items(board, task)).label, "补齐决策材料（1）d")

    def test_real_run_facts_override_optimistic_labels(self):
        task = Task(1, "真实", "项目", status="running", agent="agent", run="working", execution="pi", run_id="run")
        run = Run("run", 1, "agent", "session", "/demo/project", "真实", "", "", state="working")
        self.assert_action(task, run, "read_report", "查看进展", has_directory=True)
        run.state = "waiting"
        self.assert_action(task, run, "read_report", "查看确认信息", has_directory=True)
        self.assert_action(task, run, "read_report", "核实执行", has_directory=True, needs_verification=True)
        run.state = "working"
        self.assert_action(task, run, "read_report", "查看停止状态", has_directory=True, stop_requested=True)
        self.assert_action(task, None, "read_report", "核实执行", has_directory=True)
