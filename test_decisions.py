"""Human decisions: isolated snapshots, never real task authority or execution."""

import json
import sqlite3
import unittest
from contextlib import closing
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from decision_view import DecisionSheet
from decisions import (
    EVIDENCE_ONLY_PREFIX,
    REVIEW_SCOPE_PREFIX,
    Answer,
    Budget,
    BusinessAnswer,
    DecisionRecord,
    DecisionSpec,
    Intent,
    Target,
    decision_source,
    feedback_answers,
    feedback_question_key,
    historical_business_fact,
    prepare_decision_intent,
    preview_decisions,
    prior_answers,
    proposal_question,
    proposal_requirements,
    proposal_selected_keys,
    question_question,
    question_requirements,
    recover_feedback_draft,
    review_requirements,
    task_evidence_question,
    task_evidence_request,
    unchanged_evidence_keys,
)
from journal import Report, encoded_event, parse_event
from model import Board
from store import APPLICATION_ID, SCHEMA_VERSION, StorageError, Store, decode, encode


def spec_record(kind="choice", **changes):
    row = {
        "key": "direction",
        "kind": kind,
        "question": "选择哪条路线？",
        "why": "下阶段需要明确范围",
        "owner": "用户",
        "evidence": ["方案 A/B 已说明成本"],
        "missing": [],
        "scope": "仅临时目录内验证",
        "risk": "可能失败",
        "cost": "一次小样本",
        "options": [
            {
                "key": key,
                "label": label,
                "benefit": "可验证",
                "risk": "可能失败",
                "cost": "一次",
                "scope": "临时目录",
            }
            for key, label in [("a", "路线 A"), ("b", "路线 B")]
        ]
        if kind in {"choice", "preference"}
        else [],
        "recommendation": "a" if kind in {"choice", "preference"} else "先小样本",
        "reason": "成本最低",
        "exclusions": ["不改生产文件"],
        "budget": {
            "attempts": 1,
            "minutes": 5,
            "storage_mib": 10,
            "stop_conditions": ["出错即停"],
        }
        if kind == "budget"
        else None,
    }
    return row | changes


def decision_board(specs=None, *, live=False):
    board = Board(live=True)
    board.add_project("项目", "/virtual/decisions")
    task = board.create("分项决定", "项目", "方案研究")
    task.remaining = "本轮核查合同：\n交付物：方案研究\n允许的范围：仅研究\n验收依据：方案完成\n排除：不修改文件\n超出本轮范围的修改、试验和预算需另行明确审批；Pi 原有权限确认不被替代。"
    board.assign(task, str(uuid4()))
    run = deepcopy(
        board.queue_real(task, str(uuid4()), str(uuid4()), board.projects[task.project])
    )
    run.state, run.delivery, run.seq = (
        ("working", "progress", 1) if live else ("finished", "partial", 2)
    )
    run.summary, run.checks, run.remaining = (
        "等待人工决定",
        ["对照方案"],
        "决定路线后继续",
    )
    run.events = [
        encoded_event(
            run.delivery,
            "待决策报告",
            source="executor",
            run_id=run.id,
            seq=1,
            report=Report(
                run.summary,
                run.checks,
                run.remaining,
                decisions=tuple(specs or [DecisionSpec.from_record(spec_record())]),
            ),
        )
    ]
    board.ingest(run)
    return board, board.tasks[task.id], board.runs[run.id]


def selected(kind="choice", **changes):
    return Answer.from_record(
        {
            "kind": kind,
            "choice": "a" if kind in {"choice", "preference"} else "",
            "budget": None,
            "comment": "",
        }
        | changes
    )


def routing_payload(audience="owner_evidence", gap="原始回执缺失", status="insufficient"):
    return {"audience": audience,
            "evidence_needs": ([{"gap": gap, "owner": "回测负责人", "source": "原始成交回执"}] if gap else []),
            "advice": {"status": status, "value": "30" if status == "supported" else "",
                       "unit": "秒" if status == "supported" else "", "conditions": "旧单已撤销" if status == "supported" else "",
                       "evidence": ["方案 A/B 已说明成本"] if status == "supported" else [],
                       "uncertainty": "请核对风险", "owner_signed": status == "supported"}}


def repeated_evidence_board(*, fresh=False, save=None, cwd=None):
    spec = DecisionSpec.from_record(spec_record(missing=["原始回执缺失"], owner="回测负责人"))
    board, task, old = decision_board([spec])
    if cwd is not None:
        board.projects[task.project] = old.cwd = str(cwd)
    if save is not None:
        save(board)  # Persist the report before any new human intent, as Store requires.
    dispatched = prepare_decision_intent(board, preview_decisions(board, task.id, (Answer("request_info"),),
        Target("new_run", old.agent_id, old.session_id, old.cwd)))
    assert dispatched.run is not None
    if save is not None:
        save(board)  # The confirmed request and its queued Run are one transaction.
    incoming = deepcopy(dispatched.run)
    incoming.state, incoming.seq, incoming.delivery = "finished", 1, "partial"
    incoming.summary, incoming.checks, incoming.remaining = "负责人仍未提供回执", ["仅核对已有材料"], "等待负责人"
    updated = (DecisionSpec.from_record(spec_record(missing=["原始回执缺失"], owner="回测负责人",
        evidence=["方案 A/B 已说明成本", "新增核实的原始回执"])) if fresh else spec)
    incoming.events = [encoded_event("partial", "补证轮结果", source="executor", run_id=incoming.id, seq=1,
        report=Report(incoming.summary, incoming.checks, incoming.remaining, decisions=(updated,)))]
    board.ingest(incoming)
    return board, board.tasks[task.id], incoming


class TaskEvidenceRequestContracts(unittest.TestCase):
    def test_task_level_advice_is_one_evidence_only_run_without_old_permissions(self):
        board, task, old = decision_board()
        old.requirements = "历史自由文字：批准研究、修改和九次试验"
        request = task_evidence_request("请给可核对建议；缺原始回执就说明负责人")
        self.assertTrue(request.startswith(EVIDENCE_ONLY_PREFIX))
        self.assertNotIn(old.requirements, request)
        board.save_supplement(task, request, "next")
        self.assertEqual(len(board.runs), 1)
        board.assign(task, old.agent_id)
        queued = board.queue_real(task, str(uuid4()), old.session_id, old.cwd)
        self.assertEqual(queued.requirements, request)
        self.assertIn("缺原始回执", queued.requirements)
        self.assertNotIn("九次试验", queued.requirements)
        self.assertEqual(task_evidence_question(queued.requirements), "请给可核对建议；缺原始回执就说明负责人")
        self.assertIsNone(task_evidence_question(EVIDENCE_ONLY_PREFIX + "不是任务级问题"))
        incoming = deepcopy(queued)
        incoming.state, incoming.delivery = "finished", "full"
        with self.assertRaisesRegex(ValueError, "补证.*完整交付"):
            board.ingest(incoming)
        self.assertEqual(board.runs[queued.id].state, "queued")
        for value in ("", "  ", "x" * 6001):
            with self.subTest(value=len(value)), self.assertRaises(ValueError):
                task_evidence_request(value)


class HandoffContracts(unittest.TestCase):
    def test_saved_feedback_recovery_preserves_both_canonical_purposes(self):
        candidates = ((str(uuid4()) + ":1", "问题：批次\n旧答复：用户反馈\n来源：当前已保存来源\n状态：未发"),
                      (str(uuid4()) + ":2", "问题：期限\n旧答复：仍不理解\n来源：同一范围\n状态：未发"))
        separator = "\n\n本次明确选择的历史问题（未选择的不发送）：\n"
        def row(key, label):
            return (f"旧反馈 {key}（仅作为本轮待答问题，不是许可；请在 decisions 用 key={feedback_question_key(key)} 单独回应）："
                    + json.dumps(label, ensure_ascii=False))
        user_text = "请解释已有材料的术语。\n不要修改文件。"
        for purpose, compiler in (("question", question_requirements), ("proposal", proposal_requirements)):
            with self.subTest(purpose=purpose):
                plain = compiler(user_text)
                selected = compiler(user_text + separator + row(*candidates[1]))
                self.assertEqual(recover_feedback_draft(plain, candidates), (purpose, user_text, ()))
                self.assertEqual(recover_feedback_draft(selected, candidates), (purpose, user_text, (candidates[1][0],)))
                self.assertEqual(proposal_selected_keys(selected), (feedback_question_key(candidates[1][0]),))

    def test_saved_feedback_recovery_rejects_changed_unknown_duplicate_or_ambiguous_content(self):
        candidates = ((str(uuid4()) + ":1", "问题：批次\n旧答复：用户反馈\n来源：原始记录\n状态：未发"),
                      (str(uuid4()) + ":2", "问题：期限\n旧答复：用户反馈\n来源：原始记录\n状态：未发"))
        separator = "\n\n本次明确选择的历史问题（未选择的不发送）：\n"
        def row(key, label):
            return (f"旧反馈 {key}（仅作为本轮待答问题，不是许可；请在 decisions 用 key={feedback_question_key(key)} 单独回应）："
                    + json.dumps(label, ensure_ascii=False))
        rows = [row(*candidate) for candidate in candidates]
        valid = question_requirements("请解释已有术语" + separator + rows[0])
        invalid = {
            "duplicate": question_requirements("请解释已有术语" + separator + rows[0] + "\n" + rows[0]),
            "unknown": question_requirements("请解释已有术语" + separator + row(str(uuid4()) + ":1", candidates[0][1])),
            "changed_label": question_requirements("请解释已有术语" + separator + row(candidates[0][0], "来源内容已变化")),
            "out_of_order": question_requirements("请解释已有术语" + separator + rows[1] + "\n" + rows[0]),
            "ambiguous_separator": question_requirements("请解释已有术语" + separator + rows[0] + separator + rows[1]),
            "missing_frame": question_requirements("请解释已有术语\n" + rows[0]),
            "changed_header": valid.replace("本轮只核对已有材料", "本轮可以修改资料", 1),
            "trailing_bytes": valid + "\n",
        }
        for name, request in invalid.items():
            with self.subTest(case=name):
                self.assertIsNone(recover_feedback_draft(request, candidates))
        self.assertIsNone(recover_feedback_draft(valid, candidates + (candidates[0],)))
        self.assertIsNone(recover_feedback_draft(valid, ((candidates[0][0], "来源状态已变化"), candidates[1])))
        self.assertIsNone(recover_feedback_draft(valid, candidates[1:]))

    def test_question_round_cannot_form_formal_choice_even_from_complete_report(self):
        board, task, old = decision_board()
        before = asdict(old)
        board.save_supplement(task, question_requirements("请解释已有材料的术语"), "next")
        board.assign(task, old.agent_id)
        run = deepcopy(board.queue_real(task, str(uuid4()), old.session_id, old.cwd))
        run.state, run.seq, run.delivery = "finished", 1, "partial"
        run.summary, run.checks, run.remaining = "已解释术语", ["列了来源"], "可以再追问"
        spec = DecisionSpec.from_record(spec_record(key="explanation", routing=routing_payload("user_decision", "", "supported")))
        run.events = [encoded_event("partial", "解释现有材料", source="executor", run_id=run.id, seq=1,
            report=Report(run.summary, run.checks, run.remaining, decisions=(spec,)))]
        self.assertTrue(board.ingest(run))
        snapshot = encode(board)
        for target in (Target("draft"), Target("saved"), Target("new_run", old.agent_id, old.session_id, old.cwd)):
            with self.subTest(target=target.kind), self.assertRaisesRegex(ValueError, "追问.*正式"):
                preview_decisions(board, task.id, (Answer("choice", "a"),), target)
        feedback = preview_decisions(board, task.id, (Answer(comment="请进一步解释来源"),), Target("saved"))
        self.assertEqual(feedback.answers[0].comment, "请进一步解释来源")
        async def prepare(*args):
            raise AssertionError("Inspection must not submit anything")
        sheet = DecisionSheet(board, task.id, prepare)
        self.assertFalse(sheet.formal_available(spec))
        self.assertNotIn("formal", {key for _, key in sheet.intent_options(0)})
        self.assertEqual(encode(board), snapshot)
        self.assertEqual(asdict(board.runs[old.id]), before)

    def test_question_selected_keys_require_current_routed_answer(self):
        source = str(uuid4()) + ":1"
        key = feedback_question_key(source)
        text = f'请解释批次\n旧反馈 {source}（仅作为本轮待答问题，不是许可）："请解释来源"'
        request = question_requirements(text)
        self.assertEqual(question_question(request), text)
        self.assertIsNone(proposal_question(request))
        self.assertEqual(proposal_selected_keys(request), (key,))
        for matching in (False, True):
            with self.subTest(matching=matching):
                board, task, old = decision_board()
                before = asdict(old)
                board.save_supplement(task, request, "next")
                board.assign(task, old.agent_id)
                run = deepcopy(board.queue_real(task, str(uuid4()), old.session_id, old.cwd))
                run.state, run.seq, run.delivery = "finished", 1, "partial"
                run.summary, run.checks, run.remaining = "解释已有材料", ["有来源"], "原材料仍缺"
                spec = DecisionSpec.from_record(spec_record(
                    key=key if matching else "unrelated", missing=["原始回执缺失"],
                    routing=routing_payload()))
                run.events = [encoded_event("partial", "解释与来源", source="executor", run_id=run.id, seq=1,
                    report=Report(run.summary, run.checks, run.remaining, decisions=(spec,)))]
                self.assertTrue(board.ingest(run))
                if matching:
                    report = decision_source(board, task.id).report
                    assert report is not None and report.decisions is not None
                    self.assertEqual(report.decisions[0].key, key)
                else:
                    with self.assertRaisesRegex(ValueError, "追问.*旧问题"):
                        decision_source(board, task.id)
                self.assertEqual(asdict(board.runs[old.id]), before)

    def test_unanswered_selected_question_cannot_open_formal_decision_sheet(self):
        board, task, old = decision_board()
        question = f"请交提案\n旧反馈 {uuid4()}:1（仅作为本轮待答问题，不是许可）：\"给原件\""
        board.save_supplement(task, proposal_requirements(question), "next")
        board.assign(task, old.agent_id)
        run = deepcopy(board.queue_real(task, str(uuid4()), old.session_id, old.cwd))
        run.state, run.seq, run.delivery = "finished", 1, "partial"
        run.summary, run.checks, run.remaining = "只核旧原件", ["有一条无关问题"], "所选旧问题未答"
        unrelated = DecisionSpec.from_record(spec_record("approval", key="unrelated"))
        run.events = [encoded_event("partial", "提案未交齐", source="executor", run_id=run.id, seq=1,
            report=Report(run.summary, run.checks, run.remaining, decisions=(unrelated,)))]
        board.ingest(run)
        with self.assertRaisesRegex(ValueError, "提案.*旧问题"):
            decision_source(board, task.id)
        self.assertEqual(board.tasks[task.id].summary, "只核旧原件")
        self.assertEqual(board.runs[run.id], run)

    def test_matching_selected_question_with_owner_route_can_open_real_decision_source(self):
        board, task, old = decision_board()
        source = str(uuid4()) + ":1"
        expected_key = feedback_question_key(source)
        board.save_supplement(task, proposal_requirements(f"请交提案\n旧反馈 {source}（仅作为本轮待答问题，不是许可）：\"给账本\""), "next")
        board.assign(task, old.agent_id)
        run = deepcopy(board.queue_real(task, str(uuid4()), old.session_id, old.cwd))
        run.state, run.seq, run.delivery = "finished", 1, "partial"
        run.summary, run.checks, run.remaining = "核现有原件", ["原账本仍缺"], "等负责人"
        routed = DecisionSpec.from_record(spec_record(key=expected_key, kind="approval", missing=["原账本缺失"], routing={
            "audience": "owner_evidence", "evidence_needs": [{"gap": "原账本缺失", "owner": "研究负责人", "source": "原始账本"}],
            "advice": {"status": "insufficient", "value": "", "unit": "", "conditions": "", "evidence": [],
                       "uncertainty": "原件未到", "owner_signed": False},
        }))
        run.events = [encoded_event("partial", "按来源回答", source="executor", run_id=run.id, seq=1,
            report=Report(run.summary, run.checks, run.remaining, decisions=(routed,)))]
        board.ingest(run)
        event = decision_source(board, task.id)
        assert event.report and event.report.decisions
        self.assertEqual(event.report.decisions[0].key, expected_key)
        self.assertFalse(board.tasks[task.id].can_accept)

    def test_formal_decision_bypass_rejects_different_text_or_agent_session(self):
        spec = DecisionSpec.from_record(spec_record("approval"))
        board, task, old = decision_board([spec])
        preview = preview_decisions(board, task.id, (selected("approve"),),
            Target("new_run", old.agent_id, old.session_id, old.cwd))
        board.save_supplement(task, "继续", "next")
        board.assign(task, old.agent_id)
        with self.assertRaisesRegex(ValueError, "正式决定预览.*不匹配"):
            board.queue_real(task, str(uuid4()), old.session_id, old.cwd, decision=preview)
        board.save_supplement(task, preview.compiled_text, "pending")
        with self.assertRaisesRegex(ValueError, "正式决定预览.*不匹配"):
            board.queue_real(task, str(uuid4()), str(uuid4()), old.cwd, decision=preview)
        self.assertEqual(len(board.runs), 1)

    def test_review_requires_goal_scope_and_check_before_a_new_real_run(self):
        board, task, old = decision_board()
        board.save_supplement(task, "继续", "next")
        board.assign(task, old.agent_id)
        with self.assertRaisesRegex(ValueError, "交付物.*范围.*验收"):
            board.queue_real(task, str(uuid4()), old.session_id, old.cwd)
        self.assertEqual(len(board.runs), 1)
        for fields, field_name in ((("", "项目内", "核对回执"), "交付物"),
                                   (("核对回执", "", "核对回执"), "范围"),
                                   (("核对回执", "项目内", ""), "验收依据")):
            with self.subTest(fields=fields), self.assertRaisesRegex(ValueError, field_name):
                review_requirements(*fields)
        with self.assertRaisesRegex(ValueError, "4500|缩短"):
            proposal_requirements("x" * 4501)
        text = review_requirements("交可核对结果", "精确项目目录内旧回执", "列原件与核验结论")
        self.assertTrue(text.startswith(REVIEW_SCOPE_PREFIX))
        board.save_supplement(task, text, "pending")
        run = board.queue_real(task, str(uuid4()), old.session_id, old.cwd)
        self.assertIn("列原件与核验结论", run.requirements)
        self.assertEqual(board.runs[old.id], old)

    def test_proposal_is_one_non_authorizing_run_and_old_grants_are_not_inherited(self):
        board, task, old = decision_board()
        old.requirements = "旧许可：做九次付费试验并修改文件"
        question = "请列有据方案、缺口责任人、已用量来源与新预算建议；未知写未知"
        compiled = proposal_requirements(question)
        self.assertTrue(compiled.startswith(EVIDENCE_ONLY_PREFIX))
        self.assertEqual(proposal_question(compiled), question)
        self.assertNotIn(old.requirements, compiled)
        board.save_supplement(task, compiled, "next")
        board.assign(task, old.agent_id)
        sent = board.queue_real(task, str(uuid4()), old.session_id, old.cwd)
        self.assertEqual(sent.requirements, compiled)
        incoming = deepcopy(sent)
        incoming.state, incoming.delivery = "finished", "full"
        with self.assertRaisesRegex(ValueError, "补证.*完整交付"):
            board.ingest(incoming)


class FeedbackContracts(unittest.TestCase):
    def test_feedback_only_compiles_explicit_text_not_a_solution_or_authorization(self):
        specs = (DecisionSpec.from_record(spec_record(missing=["原始回执缺失"])),
                 DecisionSpec.from_record(spec_record(key="other", missing=["规则签收缺失"])))
        board, task, run = decision_board(specs)
        old = encode(board)
        answers = feedback_answers(specs, (Answer(comment="请据已核资料给出建议；否则说明缺谁的回执"), Answer()))
        self.assertEqual(tuple(a.kind for a in answers), ("request_info", "undecided"))
        target = Target("new_run", run.agent_id, run.session_id, run.cwd)
        preview = preview_decisions(board, task.id, answers, target)
        self.assertTrue(preview.compiled_text.startswith("仅补证请求："))
        self.assertIn("请据已核资料给出建议", preview.compiled_text)
        self.assertNotIn("选择：a", preview.compiled_text)
        self.assertEqual(old, encode(board))
        self.assertFalse(prior_answers(board, task.id))
        with self.assertRaisesRegex(ValueError, "先写反馈"):
            feedback_answers(specs, (Answer(), Answer()))
        for kind in ("choice", "preference", "approve", "defer"):
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, "不能夹带"):
                feedback_answers(specs, (selected(kind), Answer(comment="请给证据")))
        self.assertEqual(old, encode(board))

    def test_saved_feedback_on_complete_question_is_not_labeled_an_approved_decision(self):
        board, task, _ = decision_board()
        preview = preview_decisions(board, task.id, (Answer(comment="请基于证据解释差异"),), Target("saved"))
        self.assertIn("反馈", preview.compiled_text)
        self.assertIn("不批准", preview.compiled_text)
        self.assertNotIn("已确认决定", preview.compiled_text)
        record = prepare_decision_intent(board, preview).record
        self.assertEqual(record.version, 1)
        self.assertEqual(decode(encode(board)).decisions[record.id], record)
        self.assertFalse(prior_answers(board, task.id))
        prepare_decision_intent(board, preview_decisions(board, task.id, (selected(),), Target("saved")))
        later = preview_decisions(board, task.id, (Answer(comment="仅问旧值是否适用"),), Target("saved"))
        self.assertEqual(later.prior, ("",))
        self.assertNotIn("此前已确认", later.compiled_text)
        self.assertNotIn("选择：a", later.compiled_text)


class EvidenceWaitingContracts(unittest.TestCase):
    def test_same_gap_without_new_evidence_waits_for_owner_but_does_not_auto_dispatch(self):
        board, task, _ = repeated_evidence_board()
        before = encode(board)
        self.assertEqual(unchanged_evidence_keys(board, task.id), ("direction",))
        self.assertEqual(encode(board), before)
        fresh, task, _ = repeated_evidence_board(fresh=True)
        self.assertEqual(unchanged_evidence_keys(fresh, task.id), ())
        self.assertEqual(unchanged_evidence_keys(decision_board()[0], 1), ())


class HistoricalBusinessFactContracts(unittest.TestCase):
    def test_same_question_and_task_scope_is_only_readonly_background_after_new_report(self):
        question = {"kind": "choice", "question": "旧单未结清时必须反转？",
                    "options": [{"key": "yes", "label": "需要"}, {"key": "no", "label": "不需要"}]}
        spec = DecisionSpec.from_record(spec_record(human_input=question))
        board, task, run = decision_board([spec])
        prepare_decision_intent(board, preview_decisions(board, task.id,
            (Answer(business=BusinessAnswer("yes")),), Target("saved")))
        updated = DecisionSpec.from_record(spec_record(human_input=question, cost="新成本需复核"))
        run.seq += 1
        run.events.append(encoded_event("partial", "更新方案", source="executor", run_id=run.id, seq=run.seq,
            report=Report(run.summary, run.checks, run.remaining, decisions=(updated,))))
        self.assertFalse(prior_answers(board, task.id))
        fact = historical_business_fact(board, task.id, updated.key)
        self.assertIsNotNone(fact)
        assert fact is not None
        self.assertEqual(fact[1].value, "yes")
        self.assertEqual(preview_decisions(board, task.id, (Answer(),), Target("saved")).answers[0].business, None)
        self.assertNotIn("选择：a", preview_decisions(board, task.id, (Answer(),), Target("saved")).compiled_text)
        changed_question = DecisionSpec.from_record(spec_record(human_input={**question, "question": "另一业务问题"}, cost="新成本需复核"))
        run.events[-1] = encoded_event("partial", "不同问题", source="executor", run_id=run.id, seq=run.seq,
            report=Report(run.summary, run.checks, run.remaining, decisions=(changed_question,)))
        self.assertIsNone(historical_business_fact(board, task.id, changed_question.key))
        task.description = "另一任务范围"
        self.assertIsNone(historical_business_fact(board, task.id, updated.key))


class DecisionRoutingContracts(unittest.TestCase):
    def test_owner_evidence_is_not_a_user_approval_and_new_records_are_v3(self):
        spec = DecisionSpec.from_record(spec_record(missing=["原始回执缺失"], routing=routing_payload()))
        board, task, run = decision_board([spec])
        with self.assertRaisesRegex(ValueError, "负责人|不能批准"):
            preview_decisions(board, task.id, (selected(),), Target("saved"))
        target = Target("new_run", run.agent_id, run.session_id, run.cwd)
        preview = preview_decisions(board, task.id, (Answer("request_info"),), target)
        self.assertTrue(preview.compiled_text.startswith("仅补证请求："))
        record = prepare_decision_intent(board, preview).record
        self.assertEqual(record.version, 3)
        self.assertEqual(decode(encode(board)).decisions[record.id], record)

    def test_complete_owner_only_proposal_still_cannot_be_formally_selected(self):
        spec = DecisionSpec.from_record(spec_record(routing=routing_payload(gap="", status="supported")))
        board, task, _ = decision_board([spec])
        with self.assertRaisesRegex(ValueError, "负责人|不能批准"):
            preview_decisions(board, task.id, (selected(),), Target("saved"))
        accepted = DecisionSpec.from_record(spec_record(routing=routing_payload("user_decision", "", "supported")))
        board, task, _ = decision_board([accepted])
        self.assertEqual(preview_decisions(board, task.id, (selected(),), Target("saved")).answers[0].kind, "choice")

    def test_v3_business_fact_cannot_lose_its_question_provenance(self):
        question = {"kind": "choice", "question": "需要反转？", "options": [
            {"key": "yes", "label": "需要"}, {"key": "no", "label": "不需要"}]}
        spec = DecisionSpec.from_record(spec_record(missing=["原始回执缺失"], human_input=question,
                                                 routing=routing_payload("user_fact")))
        board, task, _ = decision_board([spec])
        preview = preview_decisions(board, task.id, (Answer(business=BusinessAnswer("yes")),), Target("saved"))
        record = prepare_decision_intent(board, preview).record
        self.assertEqual(record.version, 3)
        row = json.loads(encode(board))["decisions"][0]
        row["preview"]["answers"][0]["business"]["question"] = None
        with self.assertRaisesRegex(ValueError, "缺少原问题"):
            DecisionRecord.from_record(row)


class DecisionContracts(unittest.TestCase):
    def test_three_levels_and_explicit_nonexecuting_saved_intent(self):
        board, task, run = decision_board()
        original_task = deepcopy(task)
        answers = (selected(),)
        draft = prepare_decision_intent(
            board, preview_decisions(board, task.id, answers, Target("draft"))
        )
        self.assertEqual(prior_answers(board, task.id), {})
        self.assertEqual(draft.record.intent.kind, "draft")
        self.assertFalse(draft.record.confirmed_at)
        saved = prepare_decision_intent(
            board, preview_decisions(board, task.id, answers, Target("saved"))
        )
        self.assertEqual(saved.record.intent.kind, "saved")
        self.assertTrue(saved.record.confirmed_at)
        self.assertEqual(board.tasks[task.id], original_task)
        self.assertEqual(len(board.runs), 1)
        self.assertFalse(board.supplements)
        self.assertEqual(prior_answers(board, task.id)["direction"][1], selected())
        self.assertEqual(encode(decode(encode(board))), encode(board))

    def test_exact_preview_is_queued_and_ledger_survives_report_pruning(self):
        board, task, run = decision_board()
        target = Target("new_run", run.agent_id, run.session_id, run.cwd)
        preview = preview_decisions(board, task.id, (selected(),), target)
        result = prepare_decision_intent(board, preview)
        assert result.run is not None
        self.assertEqual(result.run.requirements, preview.compiled_text)
        self.assertEqual(result.record.intent.run_id, result.run.id)
        self.assertEqual(result.record.specs, preview.specs)
        self.assertIn("不改生产文件", result.run.requirements)
        board.runs[run.id].events = []
        self.assertEqual(
            decode(encode(board)).decisions[result.record.id], result.record
        )

    def test_report_and_scope_changes_reject_even_when_task_equal(self):
        for change in ("report", "scope", "lifecycle", "ledger"):
            with self.subTest(change=change):
                board, task, run = decision_board()
                preview = preview_decisions(
                    board, task.id, (selected(),), Target("saved")
                )
                if change == "report":
                    run.events = [
                        encoded_event(
                            "partial",
                            "changed",
                            source="executor",
                            run_id=run.id,
                            seq=1,
                            report=Report(
                                run.summary,
                                run.checks,
                                run.remaining,
                                decisions=(
                                    DecisionSpec.from_record(
                                        spec_record(cost="更高成本")
                                    ),
                                ),
                            ),
                        )
                    ]
                elif change == "scope":
                    board.projects[task.project] = "/other"
                elif change == "lifecycle":
                    task.retention = "archived"
                else:
                    prepare_decision_intent(
                        board,
                        preview_decisions(board, task.id, (Answer(),), Target("draft")),
                    )
                before = encode(board)
                with self.assertRaises(ValueError):
                    prepare_decision_intent(board, preview)
                self.assertEqual(before, encode(board))

    def test_incomplete_material_cannot_grant_and_budget_never_accepts_bool(self):
        for kind, answer in [
            ("choice", selected()),
            ("approval", selected("approve")),
            (
                "budget",
                selected("budget", budget=asdict(Budget(1, 5, 10, ("出错即停",)))),
            ),
        ]:
            board, task, _ = decision_board(
                [DecisionSpec.from_record(spec_record(kind, missing=["缺少证据"]))]
            )
            with self.assertRaises(ValueError):
                preview_decisions(board, task.id, (answer,), Target("saved"))
            for response in (
                Answer(),
                selected("request_info"),
                selected("defer"),
                selected("reject"),
            ):
                preview_decisions(board, task.id, (response,), Target("saved"))
        for value in (True, -1, 2**53, 1.1):
            with self.assertRaises(ValueError):
                Budget.from_record(
                    {
                        "attempts": value,
                        "minutes": 5,
                        "storage_mib": 10,
                        "stop_conditions": ["出错即停"],
                    }
                )

    def test_incomplete_choice_can_record_only_non_authorizing_preference(self):
        spec = DecisionSpec.from_record(spec_record(missing=["原始证据待核实"]))
        board, task, run = decision_board([spec])
        with self.assertRaises(ValueError):
            preview_decisions(board, task.id, (selected(),), Target("saved"))
        answer = selected("preference", comment="倾向先讨论 A；不许可任何试验")
        preview = preview_decisions(board, task.id, (answer,), Target("saved"))
        self.assertIn("偏好，不是授权", preview.compiled_text)
        self.assertIn("原始证据待核实", preview.compiled_text)
        before = len(board.runs)
        saved = prepare_decision_intent(board, preview)
        self.assertEqual(saved.record.preview.answers, (answer,))
        self.assertEqual(len(board.runs), before)
        self.assertEqual(decode(encode(board)).decisions[saved.record.id], saved.record)
        target = Target("new_run", run.agent_id, run.session_id, run.cwd)
        with self.assertRaisesRegex(ValueError, "补证"):
            preview_decisions(board, task.id, (Answer(),), target)
        request = preview_decisions(board, task.id, (answer,), target)
        self.assertIn("仅补", request.compiled_text)
        self.assertIn("不构成授权", request.compiled_text)
        complete = DecisionSpec.from_record(spec_record(key="other", question="完整问题"))
        board, task, run = decision_board([complete, spec])
        with self.assertRaisesRegex(ValueError, "补证"):
            preview_decisions(board, task.id, (selected(), selected("request_info")),
                              Target("new_run", run.agent_id, run.session_id, run.cwd))

    def test_request_for_explanation_asks_agent_for_plain_user_input(self):
        spec = DecisionSpec.from_record(spec_record(missing=["旧证明需核对"]))
        board, task, run = decision_board([spec])
        preview = preview_decisions(board, task.id, (Answer("request_info"),),
                                    Target("new_run", run.agent_id, run.session_id, run.cwd))
        self.assertIn("仅补证请求", preview.compiled_text)
        for phrase in ("用不带术语的话", "用户需要回答什么", "执行者需要补什么依据", "不要替用户决定"):
            self.assertIn(phrase, preview.compiled_text)
        self.assertNotIn("批准所述范围", preview.compiled_text)
        self.assertEqual(len(board.runs), 1)

    def test_business_answer_is_separate_from_solution_and_non_authorizing(self):
        from decisions import BusinessAnswer, business_input

        board, task, run = decision_board([DecisionSpec.from_record(spec_record(
            key="koru_r1f_lifecycle", missing=["经济规则待核实"]))])
        # Current D002 source is the only legacy report with a manually reviewed question.
        info = business_input(None, 2, "7db8453b-95ae-401d-ba8d-8cb8c6088b8a", "koru_r1f_lifecycle")
        assert info is not None
        self.assertEqual(info.kind, "choice")
        run.events = [encoded_event("partial", "报告", source="executor", run_id=run.id, seq=1,
            report=Report(run.summary, run.checks, run.remaining, decisions=(DecisionSpec.from_record(
                spec_record(missing=["经济规则待核实"], human_input={"kind": "choice", "question": "旧单未结清时是否必须反转？", "options": [
                    {"key": "yes", "label": "需要"}, {"key": "no", "label": "不需要"},
                    {"key": "conditional", "label": "有条件"}, {"key": "unknown", "label": "暂不确定"}] })),)))]
        # A user can answer yes without selecting either technical solution.
        answer = Answer(business=BusinessAnswer("yes", "最多等30秒，失败则停止"))
        target = Target("new_run", run.agent_id, run.session_id, run.cwd)
        preview = preview_decisions(board, task.id, (answer,), target)
        self.assertEqual(preview.answers[0].kind, "undecided")
        self.assertIn("需要", preview.compiled_text)
        self.assertIn("不构成技术方案或执行授权", preview.compiled_text)
        self.assertIn("仅补证请求", preview.compiled_text)
        self.assertNotIn("选择：a", preview.compiled_text)
        saved = prepare_decision_intent(board, preview)
        self.assertEqual(saved.record.version, 2)
        self.assertEqual(decode(encode(board)).decisions[saved.record.id], saved.record)
        with self.assertRaises(ValueError):
            preview_decisions(board, task.id, (Answer(business=BusinessAnswer("not_an_option")),), target)
        with self.assertRaises(ValueError):
            preview_decisions(board, task.id, (Answer(business=BusinessAnswer("conditional")),), target)

    def test_complete_request_and_empty_continue_never_grant_new_execution(self):
        board, task, run = decision_board()
        target = Target("new_run", run.agent_id, run.session_id, run.cwd)
        self.assertEqual(preview_decisions(board, task.id, (Answer("request_info"),), target).compiled_text[:6], "仅补证请求：")
        with self.assertRaisesRegex(ValueError, "没有.*决定|补证"):
            preview_decisions(board, task.id, (Answer(),), target)
        self.assertEqual(len(board.runs), 1)

    def test_business_answer_v2_rejects_tampering_and_stale_question(self):
        spec = DecisionSpec.from_record(spec_record(missing=["证据待核"], human_input={"kind": "choice", "question": "必须反转？", "options": [
            {"key": "yes", "label": "需要"}, {"key": "no", "label": "不需要"}]}))
        board, task, run = decision_board([spec])
        preview = preview_decisions(board, task.id, (Answer(business=BusinessAnswer("yes")),), Target("saved"))
        record = prepare_decision_intent(board, preview).record
        valid = json.loads(encode(board))
        self.assertEqual(valid["decisions"][0]["version"], 2)
        for name, mutate in (
            ("bad value", lambda r: r["preview"]["answers"][0]["business"].update(value="not_allowed")),
            ("missing question", lambda r: r["preview"]["answers"][0]["business"].pop("question")),
            ("missing answer", lambda r: r["preview"]["answers"][0].pop("business")),
            ("downgrade", lambda r: r.update(version=1)),
        ):
            bad = deepcopy(valid)
            mutate(bad["decisions"][0])
            with self.subTest(name=name), self.assertRaises((ValueError, StorageError)):
                decode(json.dumps(bad, ensure_ascii=False))
        self.assertEqual(decode(encode(board)).decisions[record.id], record)

    def test_v2_answer_carries_its_question_for_future_readers(self):
        spec = DecisionSpec.from_record(spec_record(missing=["技术核对中"], human_input={"kind": "choice", "question": "必须在旧单未结清时反转？", "options": [
            {"key": "yes", "label": "需要"}, {"key": "no", "label": "不需要"}]}))
        board, task, run = decision_board([spec])
        prepared = prepare_decision_intent(board, preview_decisions(board, task.id, (Answer(business=BusinessAnswer("yes")),), Target("saved")))
        row = json.loads(encode(board))["decisions"][0]
        self.assertEqual(row["preview"]["answers"][0]["business"]["question"]["question"], "必须在旧单未结清时反转？")
        # Legacy reports may not contain human_input (as with current D002); the v2 answer must stand alone.
        row["preview"]["specs"][0].pop("human_input")
        restored = DecisionRecord.from_record(row)
        business = restored.preview.answers[0].business
        assert business is not None and business.question is not None
        self.assertEqual(business.question.question, "必须在旧单未结清时反转？")
        self.assertEqual(prepared.record.version, 2)
        assert spec.human_input is not None
        stale = Answer(business=BusinessAnswer("yes", question=replace(spec.human_input, question="changed")))
        with self.assertRaisesRegex(ValueError, "业务问题已变化"):
            preview_decisions(board, task.id, (stale,), Target("saved"))

    def test_business_answer_is_saved_without_new_run_and_stays_pending(self):
        from task_actions import decision_items

        spec = DecisionSpec.from_record(spec_record(missing=["经济规则待补"], human_input={"kind": "choice", "question": "旧订单未结清是否反转？", "options": [
            {"key": "yes", "label": "需要"}, {"key": "no", "label": "不需要"}]}))
        board, task, run = decision_board([spec])
        result = prepare_decision_intent(board, preview_decisions(board, task.id,
            (Answer(business=BusinessAnswer("yes")),), Target("saved")))
        self.assertEqual(result.record.intent.kind, "saved")
        self.assertEqual(len(board.runs), 1)
        restored = decode(encode(board))
        item, = decision_items(restored, restored.tasks[task.id])
        self.assertTrue(item.pending)
        self.assertIn("业务答案已记录", item.state)
        self.assertIn("未发送", item.state)

    def test_legacy_v1_records_stay_byte_compatible(self):
        board, task, run = decision_board()
        record = prepare_decision_intent(board, preview_decisions(board, task.id, (selected(),), Target("saved"))).record
        self.assertEqual(record.version, 1)
        raw = json.loads(encode(board))["decisions"][0]
        self.assertNotIn("business", raw["preview"]["answers"][0])
        self.assertNotIn("human_input", raw["preview"]["specs"][0])
        self.assertEqual(json.loads(encode(decode(encode(board))))["decisions"][0], raw)

    def test_mixed_material_cannot_be_mislabelled_as_non_authorizing_saved_decision(self):
        complete = DecisionSpec.from_record(spec_record())
        missing = DecisionSpec.from_record(spec_record("budget", key="limits", missing=["账本旧总帽和用量未知"]))
        board, task, run = decision_board([complete, missing])
        with self.assertRaisesRegex(ValueError, "不能.*正式批准"):
            preview_decisions(board, task.id, (selected(), Answer("request_info")), Target("saved"))
        saved_only = preview_decisions(board, task.id, (Answer(), Answer("request_info")), Target("saved"))
        self.assertNotIn("选择：a", saved_only.compiled_text)
        self.assertIn("不批准", saved_only.compiled_text)

    def test_evidence_only_round_cannot_claim_full_task_delivery(self):
        spec = DecisionSpec.from_record(spec_record(missing=["仍需核实旧账本"]))
        board, task, old = decision_board([spec])
        result = prepare_decision_intent(board, preview_decisions(board, task.id, (Answer("request_info"),),
            Target("new_run", old.agent_id, old.session_id, old.cwd)))
        assert result.run is not None
        incoming = deepcopy(result.run)
        incoming.state, incoming.seq, incoming.delivery = "finished", 1, "full"
        incoming.summary, incoming.checks, incoming.remaining = "只整理了补证材料", ["模型自报"], ""
        incoming.events = [encoded_event("full", "incorrect full", source="executor", run_id=incoming.id, seq=1,
            report=Report(incoming.summary, incoming.checks, incoming.remaining))]
        with self.assertRaisesRegex(ValueError, "补证.*完整交付"):
            board.ingest(incoming)
        tampered = deepcopy(board)
        tampered.runs[incoming.id] = incoming
        with self.assertRaisesRegex(StorageError, "补证.*完整交付"):
            decode(encode(tampered))
        self.assertFalse(task.can_accept)
        self.assertEqual(board.runs[result.run.id].state, "queued")

    def test_request_only_drops_old_execution_permissions_from_new_run(self):
        proposal = DecisionSpec.from_record(spec_record(missing=["旧授权仍需审查"]))
        board, task, run = decision_board([proposal])
        run.requirements = "历史批准：允许运行经济试验最多九次"
        task.remaining = run.remaining = "旧要求：继续经济回测"
        run.events = [encoded_event("partial", "new evidence request", source="executor", run_id=run.id, seq=1,
            report=Report(run.summary, run.checks, run.remaining, decisions=(proposal,)))]
        preview = preview_decisions(board, task.id, (selected("request_info"),),
                                    Target("new_run", run.agent_id, run.session_id, run.cwd))
        self.assertEqual(preview.context.requirements, run.requirements)
        self.assertIn("仅补证请求", preview.compiled_text)
        for unsafe in ("历史批准：允许运行", "旧要求：继续经济回测"):
            self.assertNotIn(unsafe, preview.compiled_text)
        result = prepare_decision_intent(board, preview)
        assert result.run is not None
        self.assertEqual(result.run.requirements, preview.compiled_text)
        self.assertEqual(board.runs[run.id].requirements, "历史批准：允许运行经济试验最多九次")

    def test_legacy_budget_record_survives_but_cannot_supply_new_cap(self):
        spec = DecisionSpec.from_record(spec_record("budget"))
        board, task, run = decision_board([spec])
        old = preview_decisions(board, task.id, (Answer(),), Target("saved"))
        prior_budget = selected("budget", budget=asdict(Budget(5, 90, 100, ("超限即停",))))
        historic = DecisionRecord(1, str(uuid4()), "2026-09-24T00:00:00.000Z",
            replace(old, answers=(prior_budget,), compiled_text="历史预算仅供核对；用量未知"),
            Intent("saved"))
        board.decisions[historic.id] = historic
        self.assertEqual(decode(encode(board)).decisions[historic.id], historic)
        self.assertEqual(prior_answers(board, task.id)[spec.key][1], prior_budget)
        new = preview_decisions(board, task.id, (Answer("request_info"),),
                                Target("new_run", run.agent_id, run.session_id, run.cwd))
        self.assertEqual(new.prior, ("",))
        self.assertNotIn(historic.id, new.compiled_text)
        self.assertNotIn("预算上限：5", new.compiled_text)

    def test_unknown_budget_use_blocks_formal_budget_even_when_agent_claims_complete(self):
        spec = DecisionSpec.from_record(spec_record("budget"))
        board, task, run = decision_board([spec])
        approved = selected("budget", budget=asdict(Budget(9, 360, 1024, ("超限即停",))))
        for target in (Target("saved"), Target("new_run", run.agent_id, run.session_id, run.cwd)):
            with self.subTest(target=target.kind), self.assertRaisesRegex(ValueError, "旧总帽|累计已用"):
                preview_decisions(board, task.id, (approved,), target)
        request = preview_decisions(board, task.id, (selected("request_info"),),
                                    Target("new_run", run.agent_id, run.session_id, run.cwd))
        self.assertIn("仅补证请求", request.compiled_text)
        self.assertNotIn("预算上限：9 次", request.compiled_text)

    def test_request_only_never_replays_existing_authorization(self):
        original = DecisionSpec.from_record(spec_record())
        board, task, run = decision_board([original])
        saved = prepare_decision_intent(board, preview_decisions(board, task.id, (selected(),), Target("saved")))
        missing = DecisionSpec.from_record(spec_record("budget", key="limits", missing=["旧账本缺失"]))
        run.events = [encoded_event("partial", "new question", source="executor", run_id=run.id, seq=1,
            report=Report(run.summary, run.checks, run.remaining, decisions=(original, missing)))]
        self.assertIn(original.key, prior_answers(board, task.id))
        target = Target("new_run", run.agent_id, run.session_id, run.cwd)
        preview = preview_decisions(board, task.id, (Answer(), selected("request_info")), target)
        self.assertEqual(preview.prior, ("", ""))
        self.assertNotIn(saved.record.id, preview.compiled_text)
        self.assertNotIn("选择：a", preview.compiled_text)
        self.assertIn("仅补证请求", preview.compiled_text)

    def test_old_confirmations_are_visible_not_new_grants_or_defaults(self):
        board, task, run = decision_board()
        saved = prepare_decision_intent(
            board, preview_decisions(board, task.id, (selected(),), Target("saved"))
        )
        preview = preview_decisions(board, task.id, (Answer(),), Target("saved"))
        self.assertEqual(preview.answers, (Answer(),))
        self.assertNotIn(saved.record.id, preview.compiled_text)
        self.assertEqual(preview.prior, ("",))
        self.assertIn(saved.record.id, [record.id for record, _ in prior_answers(board, task.id).values()])
        self.assertIn("未决定，不授权", preview.compiled_text)
        run.events = [
            encoded_event(
                "partial",
                "changed",
                source="executor",
                run_id=run.id,
                seq=1,
                report=Report(
                    run.summary,
                    run.checks,
                    run.remaining,
                    decisions=(DecisionSpec.from_record(spec_record(scope="新范围")),),
                ),
            )
        ]
        self.assertFalse(prior_answers(board, task.id))

    def test_live_text_bound_and_preference_is_not_permission(self):
        board, task, run = decision_board(
            [DecisionSpec.from_record(spec_record("preference"))], live=True
        )
        target = Target("live_supplement", run.agent_id, run.session_id, run.cwd)
        preview = preview_decisions(board, task.id, (selected("preference"),), target)
        self.assertIn("偏好，不是授权", preview.compiled_text)
        result = prepare_decision_intent(board, preview)
        assert result.note is not None
        self.assertEqual(result.note.text, preview.compiled_text)
        self.assertEqual(result.record.intent.supplement_id, result.note.id)
        board, task, run = decision_board(live=True)
        task.remaining = run.remaining = "x" * 5990
        run.events = [
            encoded_event(
                "progress",
                "report",
                source="executor",
                run_id=run.id,
                seq=1,
                report=Report(
                    run.summary,
                    run.checks,
                    run.remaining,
                    decisions=(DecisionSpec.from_record(spec_record()),),
                ),
            )
        ]
        # Live payload does not repeat the already-running input; new-run payload must include it.
        run.state = "finished"
        task.status, task.run = "review", "finished"
        with self.assertRaises(ValueError):
            preview_decisions(
                board,
                task.id,
                (selected(comment="x" * 500),),
                Target("new_run", run.agent_id, run.session_id, run.cwd),
            )

    def test_changed_execution_context_requires_new_explicit_approval(self):
        for change in ("requirements", "remaining"):
            with self.subTest(change=change):
                specs = [DecisionSpec.from_record(spec_record("approval"))]
                board, task, run = decision_board(specs, live=change == "remaining")
                saved = prepare_decision_intent(
                    board,
                    preview_decisions(
                        board, task.id, (selected("approve"),), Target("saved")
                    ),
                )
                self.assertTrue(prior_answers(board, task.id))
                if change == "requirements":
                    board.save_supplement(
                        task, review_requirements("新增数据处理要求，尚未获得批准",
                            "仅隔离测试目录的已存材料", "新要求须有可核对原件"), "next"
                    )
                    changed = deepcopy(
                        board.queue_real(task, str(uuid4()), run.session_id, run.cwd)
                    )
                    changed.state, changed.delivery, changed.seq = (
                        "finished",
                        "partial",
                        2,
                    )
                    changed.summary, changed.checks, changed.remaining = (
                        run.summary,
                        run.checks,
                        run.remaining,
                    )
                else:
                    changed = deepcopy(run)
                    changed.seq += 1
                    changed.remaining = "新增未批准的工作"
                changed.events = [
                    encoded_event(
                        changed.delivery,
                        "same proposal, changed context",
                        source="executor",
                        run_id=changed.id,
                        seq=changed.seq,
                        report=Report(
                            changed.summary,
                            changed.checks,
                            changed.remaining,
                            decisions=tuple(specs),
                        ),
                    )
                ]
                board.ingest(changed)
                self.assertFalse(prior_answers(board, task.id))
                preview = preview_decisions(
                    board, task.id, (Answer(),), Target("saved")
                )
                self.assertEqual(preview.prior, ("",))
                self.assertNotIn(saved.record.id, preview.compiled_text)
                self.assertNotIn("批准所述范围", preview.compiled_text)
                self.assertIn("未决定，不授权", preview.compiled_text)
                explicit = preview_decisions(
                    board, task.id, (selected("approve"),), Target("saved")
                )
                self.assertIn("批准所述范围", explicit.compiled_text)

    def test_legacy_event_bytes_omit_absent_decisions(self):
        raw = encoded_event(
            "progress",
            "old",
            source="executor",
            run_id=str(uuid4()),
            seq=1,
            report=Report("summary", [], "remaining"),
        )
        self.assertNotIn('"decisions"', raw)
        event = parse_event(raw)
        assert event and event.report
        self.assertIsNone(event.report.decisions)


class DecisionStorage(unittest.TestCase):
    def test_v6_backup_immutability_lifecycle_and_purge(self):
        board, task, run = decision_board()
        legacy = json.loads(encode(board))
        legacy.pop("decisions")
        legacy.pop("task_contexts", None)
        legacy.pop("evidence_items", None)
        legacy.pop("evidence_verifications", None)
        for field in ("task_dependencies", "dependency_checks", "ownership_releases", "ownership_receipts",
                      "budget_scopes", "usage_observations", "ledger_verifications", "budget_grants"):
            legacy.pop(field)
        with TemporaryDirectory() as temp:
            path = Path(temp) / "test.sqlite3"
            with closing(sqlite3.connect(path)) as conn, conn:
                conn.execute(
                    "CREATE TABLE state (singleton INTEGER PRIMARY KEY, revision INTEGER, data TEXT)"
                )
                conn.execute(
                    "INSERT INTO state VALUES (1, 4, ?)", (json.dumps(legacy),)
                )
                conn.execute(f"PRAGMA application_id={APPLICATION_ID}")
                conn.execute("PRAGMA user_version=6")
            with Store(path) as store:
                board = store.load()
                task = board.tasks[task.id]
                self.assertEqual(
                    store.connection.execute("PRAGMA user_version").fetchone()[0],
                    SCHEMA_VERSION,
                )
                self.assertEqual(
                    json.loads(
                        store.connection.execute(
                            "SELECT data FROM legacy_v6"
                        ).fetchone()[0]
                    ),
                    legacy,
                )
                preview = preview_decisions(
                    board, task.id, (selected(),), Target("saved")
                )
                record = prepare_decision_intent(board, preview).record
                store.save(board)
                for operation in ("delete", "mutate"):
                    candidate = deepcopy(board)
                    if operation == "delete":
                        candidate.decisions.clear()
                    else:
                        candidate.decisions[record.id] = replace(
                            record, confirmed_at=""
                        )
                    with self.assertRaises(StorageError):
                        store.save(candidate)
                board.retire(task, "trashed")
                store.save(board)
                board.purge(task)
                store.save(board)
                self.assertFalse(store.load().decisions)


if __name__ == "__main__":
    unittest.main()
