"""Public presentation seams; fixture values only, no real mailbox or task authority."""
import json
import unittest
from dataclasses import FrozenInstanceError, asdict, replace
from pathlib import Path
from typing import cast
from unittest.mock import patch

from decision_presentation import (
    PresentedNeed,
    ProviderGroup,
    ReportViewSnapshot,
    capture_report_view,
    present_decision,
    report_view_event,
)
from decision_view import DecisionReview, DecisionSheet
from decisions import (
    Answer,
    Budget,
    BusinessAnswer,
    DecisionSpec,
    Source,
    Target,
    prepare_decision_intent,
    preview_decisions,
)
from handoff import Handoff
from journal import PREFIX, Brief, Report, encoded_event, parse_event
from store import encode
from test_decisions import decision_board, routing_payload, spec_record


class DecisionPresentationTests(unittest.TestCase):
    def test_legacy_complete_choice_does_not_infer_a_user_decision(self):
        spec = DecisionSpec.from_record(spec_record())
        before = asdict(spec)

        presentation = present_decision(spec, human_input=None)

        self.assertEqual(presentation.category, "legacy")
        self.assertEqual(presentation.question, "选择哪条路线？")
        self.assertEqual(presentation.why, "下阶段需要明确范围")
        self.assertIn("不推断", presentation.guidance)
        self.assertEqual(presentation.provider_groups, ())
        self.assertEqual(asdict(spec), before)

    def test_owner_gaps_keep_original_indices_and_sources_when_grouped(self):
        missing = ["核原结果", "核累计用量", "核代码版本"]
        route = routing_payload()
        route["evidence_needs"] = [
            {"gap": "核原结果", "owner": "Main", "source": "results.json"},
            {"gap": "核累计用量", "owner": "资源负责人", "source": "ledger.json"},
            {"gap": "核代码版本", "owner": "Main", "source": "worker.py（仅文本）"},
        ]
        spec = DecisionSpec.from_record(spec_record(missing=missing, routing=route))
        before = asdict(spec)

        presentation = present_decision(spec, human_input=None)

        self.assertEqual(presentation.category, "owner_evidence")
        self.assertEqual(presentation.provider_groups, (
            ProviderGroup("Main", (
                PresentedNeed(1, "核原结果", "Main", "results.json"),
                PresentedNeed(3, "核代码版本", "Main", "worker.py（仅文本）"),
            )),
            ProviderGroup("资源负责人", (
                PresentedNeed(2, "核累计用量", "资源负责人", "ledger.json"),
            )),
        ))
        self.assertIn("不批准", presentation.guidance)
        self.assertEqual(asdict(spec), before)

    def test_legacy_gaps_preserve_owner_and_mark_missing_sources(self):
        spec = DecisionSpec.from_record(spec_record(
            missing=["原结果待核", "历史用量待核"], owner="原报告负责人"))

        presentation = present_decision(spec, human_input=None)

        self.assertEqual(presentation.category, "legacy")
        self.assertEqual(presentation.provider_groups, (
            ProviderGroup("原报告负责人", (
                PresentedNeed(1, "原结果待核", "原报告负责人", None),
                PresentedNeed(2, "历史用量待核", "原报告负责人", None),
            )),
        ))

    def test_user_fact_uses_bound_business_question_not_the_technical_question(self):
        question = {"kind": "choice", "question": "是否必须提前反转？", "options": [
            {"key": "yes", "label": "需要"}, {"key": "no", "label": "不需要"}]}
        spec = DecisionSpec.from_record(spec_record(
            human_input=question, missing=["原始回执缺失"], routing=routing_payload("user_fact")))

        presentation = present_decision(spec, human_input=spec.human_input)

        self.assertEqual(presentation.category, "user_fact")
        self.assertEqual(presentation.question, "是否必须提前反转？")
        self.assertNotEqual(presentation.question, spec.question)
        self.assertIn("不批准", presentation.guidance)

    def test_supported_user_decision_is_display_only_not_an_approval(self):
        spec = DecisionSpec.from_record(spec_record(
            routing=routing_payload("user_decision", gap="", status="supported")))
        before = asdict(spec)

        presentation = present_decision(spec, human_input=None)

        self.assertEqual(presentation.category, "user_decision")
        self.assertEqual(presentation.question, "选择哪条路线？")
        self.assertIn("原校验", presentation.guidance)
        self.assertEqual(set(asdict(presentation)), {"category", "question", "why", "guidance", "provider_groups"})
        self.assertEqual(asdict(spec), before)

    def test_legacy_business_input_does_not_replace_the_other_technical_question(self):
        spec = DecisionSpec.from_record(spec_record(human_input={
            "kind": "text", "question": "你知道哪份原件？", "options": []}))

        presentation = present_decision(spec, human_input=spec.human_input)

        self.assertEqual(presentation.category, "legacy")
        self.assertEqual(presentation.question, "选择哪条路线？")
        self.assertIsNotNone(spec.human_input)

    def test_missing_or_replaced_bound_business_question_is_rejected(self):
        spec = DecisionSpec.from_record(spec_record(
            human_input={"kind": "text", "question": "你知道哪份原件？", "options": []},
            missing=["原始回执缺失"], routing=routing_payload("user_fact")))
        assert spec.human_input is not None
        for question in (None, replace(spec.human_input, question="另一个业务问题")):
            with self.subTest(question=question), self.assertRaises(ValueError):
                present_decision(spec, human_input=question)

    def test_unknown_explicit_audience_is_not_silently_made_legacy(self):
        spec = DecisionSpec.from_record(spec_record(
            missing=["原始回执缺失"], routing=routing_payload()))
        assert spec.routing is not None
        malformed = replace(spec, routing=replace(spec.routing, audience="invented_role"))

        with self.assertRaises(ValueError):
            present_decision(malformed, human_input=None)

    def test_grouping_does_not_normalize_or_guess_provider_identity(self):
        route = routing_payload()
        route["evidence_needs"] = [
            {"gap": "核原结果", "owner": "Main", "source": "results"},
            {"gap": "核版本", "owner": "Main ", "source": "registration"},
        ]
        spec = DecisionSpec.from_record(spec_record(missing=["核原结果", "核版本"], routing=route))

        presentation = present_decision(spec, human_input=None)

        self.assertEqual(tuple(group.owner for group in presentation.provider_groups), ("Main", "Main "))
        self.assertEqual(tuple(group.needs[0].index for group in presentation.provider_groups), (1, 2))

    def test_presentation_and_nested_values_are_immutable(self):
        spec = DecisionSpec.from_record(spec_record(missing=["原始回执缺失"], routing=routing_payload()))
        value = present_decision(spec, human_input=None)

        for target, field, changed in (
            (value, "category", "user_decision"),
            (value.provider_groups[0], "owner", "另一个人"),
            (value.provider_groups[0].needs[0], "source", "虚构来源"),
        ):
            with self.subTest(field=field), self.assertRaises(FrozenInstanceError):
                setattr(target, field, changed)
        self.assertIsInstance(value.provider_groups, tuple)
        self.assertIsInstance(value.provider_groups[0].needs, tuple)


class ReportViewSnapshotTests(unittest.TestCase):
    def fixture(self, *, brief=True, handoff=True, specs=True, actor="executor"):
        spec = DecisionSpec.from_record(spec_record())
        report = Report(
            "仅有已留报告；[候选文件](/virtual/unread/result.json)尚未核", ["仅核对文字"], "仍待负责人原件",
            Brief(["核对旧报告"], "已留材料", "未运行实验") if brief else None,
            (spec,) if specs else None,
            Handoff("已列候选材料", "原件未核", "executor", "Main先明确核查范围", "none") if handoff else None,
        )
        run_id = "10000000-0000-4000-8000-000000000001"
        raw = encoded_event("partial", "隔离报告", source=actor, run_id=run_id, seq=3, report=report)
        event = parse_event(raw)
        assert event is not None
        return Source(run_id, event.id, 3), raw

    def test_capture_preserves_original_envelope_and_complete_report(self):
        source, raw = self.fixture()

        snapshot = capture_report_view(source, raw)
        event = report_view_event(snapshot)

        self.assertEqual(snapshot.source, source)
        self.assertEqual(snapshot.source_bytes, raw)
        self.assertEqual((event.run_id, event.id, event.seq), (source.run_id, source.event_id, 3))
        report = event.report
        assert report is not None and report.brief is not None and report.handoff is not None
        self.assertEqual(report.summary, "仅有已留报告；[候选文件](/virtual/unread/result.json)尚未核")
        self.assertEqual(report.checks, ["仅核对文字"])
        self.assertEqual(report.remaining, "仍待负责人原件")
        self.assertEqual(report.brief.actions, ["核对旧报告"])
        self.assertEqual(report.brief.verification, "未运行实验")
        self.assertEqual(report.handoff.next_action, "Main先明确核查范围")
        self.assertIsNotNone(report.decisions)
        self.assertEqual(snapshot.source_bytes, raw)

    def test_capture_rejects_a_different_run_event_or_revision(self):
        source, raw = self.fixture()
        for changed in (
            replace(source, run_id="10000000-0000-4000-8000-000000000002"),
            replace(source, event_id="20000000-0000-4000-8000-000000000001"),
            replace(source, seq=4),
        ):
            with self.subTest(source=changed), self.assertRaises(ValueError):
                capture_report_view(changed, raw)

    def test_capture_rejects_simulation_as_an_executor_source(self):
        source, raw = self.fixture(actor="simulation")

        with self.assertRaises(ValueError):
            capture_report_view(source, raw)

    def test_invalid_snapshot_values_raise_value_error_instead_of_being_coerced(self):
        source, raw = self.fixture()
        for invalid_source, invalid_raw in (
            (cast(Source, None), raw),
            (source, cast(str, None)),
            (replace(source, seq=3.0), raw),
        ):
            with self.subTest(source=invalid_source, raw_type=type(invalid_raw)), self.assertRaises(ValueError):
                capture_report_view(invalid_source, invalid_raw)

    def test_capture_requires_a_real_run_and_positive_revision(self):
        source, raw = self.fixture()
        for changes in ({"run_id": ""}, {"seq": 0}):
            with self.subTest(changes=changes):
                row = json.loads(raw[len(PREFIX):])
                row.update(changes)
                altered = PREFIX + json.dumps(row, ensure_ascii=False)
                with self.assertRaises(ValueError):
                    capture_report_view(replace(source, **changes), altered)

    def test_old_reports_without_optional_brief_or_handoff_are_not_filled_in(self):
        source, raw = self.fixture(brief=False, handoff=False)
        row = json.loads(raw[len(PREFIX):])
        self.assertNotIn("handoff", row["report"])

        snapshot = capture_report_view(source, raw)
        event = report_view_event(snapshot)

        assert event.report is not None
        self.assertIsNone(event.report.handoff)
        self.assertIsNone(event.report.brief)
        self.assertEqual(event.report.checks, ["仅核对文字"])
        self.assertEqual(snapshot.source_bytes, raw)

    def test_each_reader_gets_fresh_mutable_report_lists(self):
        source, raw = self.fixture()
        snapshot = capture_report_view(source, raw)
        first = report_view_event(snapshot)
        assert first.report is not None and first.report.brief is not None
        first.report.checks.append("消费者私有文字")
        first.report.brief.actions.append("消费者私有动作")

        second = report_view_event(snapshot)

        assert second.report is not None and second.report.brief is not None
        self.assertEqual(second.report.checks, ["仅核对文字"])
        self.assertEqual(second.report.brief.actions, ["核对旧报告"])
        self.assertEqual(snapshot.source_bytes, raw)

    def test_capture_keeps_existing_whitespace_and_identity_bytes(self):
        source, raw = self.fixture()
        spaced = PREFIX + json.dumps(json.loads(raw[len(PREFIX):]), ensure_ascii=False, indent=2)

        snapshot = capture_report_view(source, spaced)

        self.assertEqual(snapshot.source_bytes, spaced)
        self.assertNotEqual(snapshot.source_bytes, raw)
        self.assertEqual(snapshot.source, source)
        self.assertEqual(report_view_event(snapshot).id, source.event_id)

    def test_snapshot_cannot_be_reassigned_and_direct_values_are_rechecked(self):
        source, raw = self.fixture()
        snapshot = capture_report_view(source, raw)
        with self.assertRaises(FrozenInstanceError):
            snapshot.source_bytes = "替代记录"  # pyright: ignore[reportAttributeAccessIssue] -- intentional frozen-field rejection
        wrong_source = replace(source, seq=4)
        invalid = replace(snapshot, source=wrong_source)
        with self.assertRaises(ValueError):
            report_view_event(invalid)
        with self.assertRaises(ValueError):
            report_view_event(cast(ReportViewSnapshot, None))

    def test_invalid_envelopes_or_absent_decisions_cannot_create_a_snapshot(self):
        source, raw = self.fixture(specs=False)
        for value in ("历史普通文字", "waggle:event:2 {}", PREFIX + "{bad", raw):
            with self.subTest(value=value[:40]), self.assertRaises(ValueError):
                capture_report_view(source, value)

    def test_display_never_opens_a_report_link_or_evidence_source(self):
        source, raw = self.fixture()
        spec = DecisionSpec.from_record(spec_record(missing=["原始回执缺失"], routing=routing_payload()))
        with patch("builtins.open", side_effect=AssertionError("No display I/O")), patch.object(
                Path, "open", side_effect=AssertionError("No link following")):
            snapshot = capture_report_view(source, raw)
            event = report_view_event(snapshot)
            presentation = present_decision(spec, human_input=None)
        self.assertIsNotNone(event.report)
        self.assertEqual(presentation.provider_groups[0].needs[0].source, "原始成交回执")

    def test_projection_and_snapshot_do_not_change_existing_preview_or_fingerprint(self):
        board, task, run = decision_board()
        answers, target = (Answer("request_info"),), Target("draft")
        before = preview_decisions(board, task.id, answers, target)
        source, raw = before.source, run.events[0]
        before_run = asdict(run)

        snapshot = capture_report_view(source, raw)
        event = report_view_event(snapshot)
        assert event.report is not None and event.report.decisions is not None
        for spec in event.report.decisions:
            present_decision(spec, human_input=spec.human_input)
        event.report.checks.append("不写回原轮次")
        after = preview_decisions(board, task.id, answers, target)

        self.assertEqual(after, before)
        self.assertEqual(after.fingerprint, before.fingerprint)
        self.assertEqual(after.compiled_text, before.compiled_text)
        self.assertEqual(asdict(run), before_run)
        self.assertFalse(board.decisions)


class PresentationPreviewEquivalenceTests(unittest.TestCase):
    @staticmethod
    def cases():
        human = {"kind": "choice", "question": "业务上是否必须提前处理虚构编号？", "options": [
            {"key": "conditional", "label": "有条件"}, {"key": "unknown", "label": "不确定"}]}
        legacy = DecisionSpec.from_record(spec_record())
        dual = DecisionSpec.from_record(spec_record(human_input=human))
        owner = DecisionSpec.from_record(spec_record(key="materials", missing=["原始回执缺失"], routing=routing_payload()))
        fact = DecisionSpec.from_record(spec_record(key="fact", human_input=human, missing=["原始回执缺失"],
            routing=routing_payload("user_fact")))
        decision = DecisionSpec.from_record(spec_record(key="unselected", question="UNSELECTED technical proposal",
            routing=routing_payload("user_decision", gap="", status="supported")))
        approval = DecisionSpec.from_record(spec_record("approval", key="approval",
            routing=routing_payload("user_decision", gap="", status="supported")))
        complete_owner = DecisionSpec.from_record(spec_record(
            routing=routing_payload("owner_evidence", gap="", status="supported")))
        budget = DecisionSpec.from_record(spec_record("budget"))
        return [
            ("legacy formal", (legacy,), (Answer("choice", "b"),), False),
            ("legacy dual business", (dual,), (Answer(business=BusinessAnswer("conditional", "仅在原件核对完成后")),), False),
            ("legacy dual formal", (dual,), (Answer("choice", "a"),), False),
            ("owner evidence", (owner,), (Answer("request_info"),), False),
            ("routed fact unknown", (fact,), (Answer(business=BusinessAnswer("unknown")),), False),
            ("routed formal", (decision,), (Answer("choice", "b"),), False),
            ("routed mixed audience with unselected item", (owner, fact, decision),
                (Answer("request_info"), Answer(business=BusinessAnswer("conditional", "不重做实验")), Answer()), False),
            ("two explicit formal answers", (decision, approval), (Answer("choice", "a"), Answer("approve")), False),
            ("owner preference is not permission", (complete_owner,), (Answer("preference", "a"),), False),
            ("budget evidence request", (budget,), (Answer("request_info"),), False),
            ("old formal confirmation not reused", (legacy,), (Answer("request_info", comment="只核对已有证据"),), True),
        ]

    def assert_equivalence(self, case, kind):
        name, specs, answers, history = case
        board, task, run = decision_board(specs, live=kind == "live_supplement")
        old_id = ""
        if history:
            old = prepare_decision_intent(board, preview_decisions(board, task.id, (Answer("choice", "a"),), Target("saved")))
            old_id = old.record.id
        target = Target(kind, run.agent_id, run.session_id, run.cwd) if kind in {"new_run", "live_supplement"} else Target(kind)
        before_board, before_run = encode(board), asdict(run)
        expected = preview_decisions(board, task.id, answers, target)

        async def no_submit(*_):
            raise AssertionError("Equivalence tests may present values but must never submit")

        form = DecisionSheet(board, task.id, no_submit)
        self.assertEqual(form.report_view.source_bytes, run.events[0])
        review = DecisionReview(expected, "隔离同参数核对，不发送", no_submit, report_view=form.report_view)
        summary = review.summary()
        report = report_view_event(review.report_view).report
        assert report is not None
        report.checks.append("消费者私有列表，不写入 Board")
        actual = preview_decisions(board, task.id, answers, target)
        self.assertEqual(actual, expected, (name, kind))
        self.assertEqual(actual.fingerprint, expected.fingerprint)
        self.assertEqual(actual.compiled_text, expected.compiled_text)
        self.assertEqual(actual.answers, expected.answers)
        self.assertEqual(encode(board), before_board)
        self.assertEqual(asdict(run), before_run)
        self.assertEqual(form.initial, tuple(Answer() for _ in specs), "History never becomes a new input default")
        if old_id:
            self.assertEqual(actual.prior, ("",))
            self.assertNotIn(old_id, actual.compiled_text)
        if name == "routed mixed audience with unselected item":
            self.assertIn("未选 1 项；未选项不发送", summary)
            self.assertNotIn('未选 1 项，本次不发送', summary)
            self.assertNotIn("UNSELECTED technical proposal", summary)
            if kind in {"new_run", "live_supplement"}:
                self.assertNotIn("UNSELECTED technical proposal", actual.compiled_text)
            self.assertFalse(form.formal_available(specs[2]), "Whole-sheet evidence guard remains intact")
        if name == "budget evidence request":
            self.assertFalse(form.formal_available(specs[0]))
            self.assertNotIn("新增预算", summary)

    def test_one_representative_source_bound_new_run_preview_is_exact(self):
        self.assert_equivalence(self.cases()[6], "new_run")

    def test_all_four_targets_across_audiences_legacy_history_budget_and_unselected_items(self):
        for case in self.cases():
            for kind in ("draft", "saved", "new_run", "live_supplement"):
                with self.subTest(case=case[0], target=kind):
                    self.assert_equivalence(case, kind)

    def test_display_cannot_enable_budget_mixed_formal_owner_approval_or_empty_send(self):
        for kind in ("draft", "saved", "new_run", "live_supplement"):
            budget = DecisionSpec.from_record(spec_record("budget"))
            owner = DecisionSpec.from_record(spec_record(routing=routing_payload("owner_evidence", gap="", status="supported")))
            for name, spec, answer in (
                ("budget", budget, Answer("budget", budget=Budget(1, 5, 10, ("出错即停",)))),
                ("owner approval", owner, Answer("choice", "a")),
            ):
                with self.subTest(case=name, target=kind):
                    board, task, run = decision_board((spec,), live=kind == "live_supplement")
                    target = Target(kind, run.agent_id, run.session_id, run.cwd) if kind in {"new_run", "live_supplement"} else Target(kind)
                    before = encode(board)
                    form = DecisionSheet(board, task.id, self.no_submit)
                    self.assertFalse(form.formal_available(spec))
                    with self.assertRaises(ValueError):
                        preview_decisions(board, task.id, (answer,), target)
                    self.assertEqual(encode(board), before)
            if kind != "draft":
                specs = (DecisionSpec.from_record(spec_record()), DecisionSpec.from_record(spec_record(key="other")))
                board, task, run = decision_board(specs, live=kind == "live_supplement")
                target = Target(kind, run.agent_id, run.session_id, run.cwd) if kind in {"new_run", "live_supplement"} else Target(kind)
                before = encode(board)
                DecisionSheet(board, task.id, self.no_submit)
                with self.assertRaisesRegex(ValueError, "拆分|分别"):
                    preview_decisions(board, task.id, (Answer("choice", "a"), Answer("request_info")), target)
                if kind in {"new_run", "live_supplement"}:
                    with self.assertRaises(ValueError):
                        preview_decisions(board, task.id, (Answer(), Answer()), target)
                    with self.assertRaisesRegex(ValueError, "没有正式方案决定或补证请求"):
                        preview_decisions(board, task.id, (Answer(comment="原始备注不会自动变成发送许可"), Answer()), target)
                self.assertEqual(encode(board), before)

    @staticmethod
    async def no_submit(*_):
        raise AssertionError("Domain refusal tests must never submit")


if __name__ == "__main__":
    unittest.main()
