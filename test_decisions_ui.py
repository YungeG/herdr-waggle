"""Decision sheet behavior against scratch SQLite and a private fake mailbox."""

import asyncio
import json
import os
import time
import unittest
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import patch
from uuid import uuid4

from rich.console import Console
from rich.table import Table
from textual.containers import VerticalScroll
from textual.widgets import (
    Button,
    Collapsible,
    Input,
    OptionList,
    Select,
    Static,
    TextArea,
)
from textual.widgets._select import InvalidSelectValueError

import decision_view
from app import Choice, Confirm, DispatchNote, NewAgentForm, Waggle
from decision_view import DecisionReview, DecisionSheet, DiscardChanges
from decisions import (
    Answer,
    BusinessAnswer,
    DecisionSpec,
    Target,
    prepare_decision_intent,
    preview_decisions,
    prior_answers,
)
from handoff import Handoff
from journal import Brief, Report, encoded_event, parse_event
from launch import prepare_launch
from report_reader import ReportReader
from runtime import Agent, Runtime, SessionObservation, private_dir, publish
from store import StorageError, Store, encode
from test_capabilities import capability_record
from test_decisions import (
    decision_board,
    repeated_evidence_board,
    routing_payload,
    spec_record,
)
from test_live import wait_ui


def select_intent(form, index, intent):
    """Explicit disposable operator choice, not a production inference/default."""
    form.query_one(f"#intent-{index}", Select).value = intent
    form.change_intent(index)
    form.query_one(f"#decision-other-reply-{index}", Collapsible).collapsed = False


def select_feedback_if_blank(form, index):
    if form.query_one(f"#intent-{index}", Select).value == "":
        select_intent(form, index, "feedback")


class DecisionUI(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = Path(
            self.enterContext(TemporaryDirectory(prefix="waggle-decision-ui-"))
        )
        self.project = self.root / "project"
        self.project.mkdir()
        self.store = self.enterContext(Store(self.root / "tasks.sqlite3"))
        self.store.load()
        self.board, self.task, self.attempt = decision_board()
        self.board.projects[self.task.project] = str(self.project)
        self.attempt.cwd = str(self.project)
        # Tests may finish constructing the initial report before its first persistence.
        self.runtime = Runtime(self.root / "bridges")
        self.agent = Agent(
            self.attempt.agent_id,
            self.attempt.session_id,
            self.attempt.cwd,
            "隔离执行者",
            "offline/test",
            os.getpid(),
            int(time.time() * 1000),
            True,
            True,
            "",
        )
        self.publish_agent(self.agent)

    def publish_agent(self, agent):
        endpoint = self.runtime.root / agent.id
        for folder in (
            self.runtime.root,
            endpoint,
            endpoint / "commands",
            endpoint / "runs",
        ):
            private_dir(folder, create=True)
        publish(endpoint / "agent.json", {"version": 1, **asdict(agent)})
        (endpoint / "report-capabilities.json").unlink(missing_ok=True)
        publish(endpoint / "report-capabilities.json", capability_record(agent))
        session = SessionObservation(agent.id, agent.session_id, agent.cwd, 1, agent.heartbeat,
            "idle" if agent.idle else "working", self.attempt.id, "", False, False)
        (endpoint / "session.json").unlink(missing_ok=True)
        publish(endpoint / "session.json", {"version": 1, **asdict(session)})

    def seed_fixture(self):
        """Persist the complete initial source once, never replace saved history."""
        if self.store.revision == 0:
            self.store.save(self.board)

    def saved_snapshot(self):
        self.seed_fixture()
        return encode(self.store.load())

    def app(self):
        self.seed_fixture()
        app = Waggle(store=self.store, runtime=self.runtime)
        app.agents[self.agent.id] = self.agent
        session = self.runtime.read_session(self.attempt)
        if session is not None:
            app.sessions[self.attempt.id] = session
        self.enterContext(patch.object(app, "schedule_poll"))
        self.enterContext(patch.object(self.runtime, "check_current"))
        return app

    async def sheet(self, app, pilot):
        app.action_decisions()
        await wait_ui(pilot, lambda: isinstance(app.screen, DecisionSheet))
        form = app.screen
        assert isinstance(form, DecisionSheet)
        return form

    async def review(self, app, pilot, kind):
        await pilot.pause()  # Let newly selected intent fields finish their layout before scrolling.
        if kind.startswith("feedback-"):
            app.screen.query_one("#decision-" + kind, Button).scroll_visible(top=True, animate=False)
            await pilot.pause()
        await pilot.click("#decision-" + kind)
        await wait_ui(pilot, lambda: isinstance(app.screen, DecisionReview))
        review = app.screen
        assert isinstance(review, DecisionReview)
        return review

    async def confirmed(self, app, pilot):
        review = app.screen
        assert isinstance(review, DecisionReview)
        await wait_ui(
            pilot,
            lambda: (
                not review.busy
                and not review.query_one("#decision-confirm", Button).disabled
            ),
        )
        await pilot.pause(review.query_one("#decision-confirm", Button).active_effect_duration)
        self.assertTrue(await pilot.click("#decision-confirm"))
        try:
            await wait_ui(pilot, lambda: len(app.screen_stack) == 1)
        except TimeoutError:
            self.fail(
                f"review did not close: busy={review.busy}, records={len(app.board.decisions)}, "
                f"error={review.query_one('#decision-error', Static).content}, runtime={app.runtime_errors}"
            )

    async def check_source_bound_preparation(self, target_kind):
        if target_kind == 'live_supplement':
            self.use_live_board()
        app = self.app()
        before_disk = self.saved_snapshot()
        before_run = deepcopy(asdict(self.attempt))
        revision = self.store.revision
        with patch.object(self.runtime, 'dispatch') as dispatch, patch.object(self.runtime, 'send_supplement') as send:
            async with app.run_test(size=(80, 24)) as pilot:
                form = await self.sheet(app, pilot)
                select_intent(form, 0, 'formal')
                form.query_one('#answer-0', Select).value = 'choice:b'
                await pilot.pause()
                target = (Target(target_kind, self.agent.id, self.agent.session_id, str(self.project))
                          if target_kind in {'new_run', 'live_supplement'} else Target(target_kind))
                expected = preview_decisions(app.board, self.task.id, form.answers(), target)
                original = form.report_view
                action = 'continue' if target_kind in {'new_run', 'live_supplement'} else target_kind
                review = await self.review(app, pilot, action)
                self.assertEqual(review.preview, expected)
                self.assertEqual(review.preview.fingerprint, expected.fingerprint)
                self.assertEqual(review.preview.compiled_text, expected.compiled_text)
                self.assertEqual(str(review.query_one('#decision-preview-text', Static).content), expected.compiled_text)
                self.assertIs(review.report_view, original)
                self.assertEqual(review.report_view.source_bytes, self.attempt.events[0])
                self.assertEqual(encode(app.board), before_disk)
                self.assertEqual(encode(self.store.load()), before_disk)
                self.assertEqual(self.store.revision, revision)
                self.assertEqual(asdict(app.board.runs[self.attempt.id]), before_run)
                dispatch.assert_not_called()
                send.assert_not_called()
                await pilot.press('escape')
                await wait_ui(pilot, lambda: app.screen is form and not form.busy)
                self.assertEqual(form.query_one('#answer-0', Select).value, 'choice:b')
                self.assertEqual(form.report_view, original)
                self.assertFalse(list(self.runtime.root.glob('*/commands/*.json')))
                self.assertFalse(list(self.runtime.root.glob('*/supplements/requests/*.json')))

    async def test_source_bound_actual_draft_preparation_equals_original_preview(self):
        await self.check_source_bound_preparation('draft')

    async def test_source_bound_actual_saved_preparation_equals_original_preview(self):
        await self.check_source_bound_preparation('saved')

    async def test_source_bound_actual_new_run_preparation_equals_original_preview(self):
        await self.check_source_bound_preparation('new_run')

    async def test_source_bound_actual_live_preparation_equals_original_preview(self):
        await self.check_source_bound_preparation('live_supplement')

    async def check_late_source_drift(self, stage, *, live=False):
        from journal import PREFIX
        if live:
            self.use_live_board()
        app = self.app()
        before_disk = self.saved_snapshot()
        entered, release = Event(), Event()
        async with app.run_test(size=(80, 24)) as pilot:
            form = await self.sheet(app, pilot)
            select_intent(form, 0, 'feedback')
            form.query_one('#comment-0', TextArea).text = '仅核对已有结果；等待来源，不扩权'
            await pilot.pause()
            original = form.report_view
            if stage == 'submit':
                review = await self.review(app, pilot, 'continue')
            method = 'check_report_capabilities' if stage == 'prepare' else 'check_supplement' if live else 'agent'
            check = getattr(self.runtime, method)

            def delayed(*args, **kwargs):
                entered.set()
                if not release.wait(5):
                    raise TimeoutError('isolated source-drift barrier not released')
                return check(*args, **kwargs)

            with patch.object(self.runtime, method, side_effect=delayed), \
                    patch.object(self.runtime, 'dispatch') as dispatch, \
                    patch.object(self.runtime, 'send_supplement') as send:
                try:
                    if stage == 'prepare':
                        form.request('continue')
                    else:
                        await pilot.click('#decision-confirm')
                    self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                    row = json.loads(original.source_bytes[len(PREFIX):])
                    changed = PREFIX + json.dumps(row, ensure_ascii=False, indent=2)
                    self.assertEqual(parse_event(changed), parse_event(original.source_bytes))
                    self.assertNotEqual(changed, original.source_bytes)
                    app.board.runs[self.attempt.id].events = [changed]
                    before_changed = encode(app.board)
                finally:
                    release.set()
                page = form if stage == 'prepare' else review
                await wait_ui(pilot, lambda: not page.busy and '未提交' in str(page.query_one('#decision-error', Static).content))
                self.assertEqual(encode(app.board), before_changed)
                self.assertEqual(encode(self.store.load()), before_disk)
                self.assertEqual(form.report_view, original)
                self.assertEqual(form.query_one('#decision-report-envelope', TextArea).text, original.source_bytes)
                self.assertEqual(form.query_one('#comment-0', TextArea).text, '仅核对已有结果；等待来源，不扩权')
                dispatch.assert_not_called()
                send.assert_not_called()
                self.assertFalse(list(self.runtime.root.glob('*/commands/*.json')))
                self.assertFalse(list(self.runtime.root.glob('*/supplements/requests/*.json')))

    async def test_prepare_new_run_late_identical_event_reencoding_is_rejected(self):
        await self.check_late_source_drift('prepare')

    async def test_prepare_live_late_identical_event_reencoding_is_rejected(self):
        await self.check_late_source_drift('prepare', live=True)

    async def test_submit_new_run_late_identical_event_reencoding_is_rejected(self):
        await self.check_late_source_drift('submit')

    async def test_submit_live_late_identical_event_reencoding_is_rejected(self):
        await self.check_late_source_drift('submit', live=True)

    async def check_presentation_owner(self, size):
        route = routing_payload()
        route["evidence_needs"] = [
            {"gap": "原结果待核", "owner": "Main", "source": "fixture/result.json"},
            {"gap": "累计用量待核", "owner": "资源负责人", "source": "fixture/ledger.json"},
            {"gap": "代码版本待核", "owner": "Main", "source": "fixture/version.txt"},
        ]
        specs = (DecisionSpec.from_record(spec_record(missing=[need["gap"] for need in route["evidence_needs"]],
                    routing=route)),
                 DecisionSpec.from_record(spec_record(key="other", missing=["原始回执缺失"], routing=routing_payload())))
        handoff = Handoff("仅提出合成核查草案，尚未核验", "缺原始结果", "executor", "全局唯一动作：核对已留材料", "none")
        run = self.attempt
        raw = encoded_event("partial", "隔离展示样例", source="executor", run_id=run.id, seq=1,
            report=Report(run.summary, run.checks, run.remaining,
                Brief(["列出合成资料"], "隔离结果", "尚未独立核验"), specs, handoff))
        run.events = [raw]
        before = self.saved_snapshot()
        app = self.app()
        async with app.run_test(size=size) as pilot:
            form = await self.sheet(app, pilot)
            self.assertEqual(form.report_view.source_bytes, raw)
            assert form.basis is not None
            self.assertEqual(form.report_view.source, form.basis.source)
            self.assertEqual(form.query_one("#decision-report-envelope", TextArea).text, raw)
            self.assertEqual(form.answers(), (Answer(), Answer()))
            self.assertTrue(form.query_one("#decision-other-reply-0", Collapsible).collapsed)
            self.assertIn("待补材料", str(form.query_one("#decision-item-0 .decision-first", Static).content))
            self.assertIn("全局唯一动作", str(form.query_one("#decision-global-handoff", Static).content))
            for section in form.query(".decision-item"):
                self.assertNotIn("全局唯一动作", " ".join(str(w.content) for w in section.query(Static)))
            for index, gap in enumerate(specs[0].missing, 1):
                self.assertEqual(str(form.query_one(f"#decision-gap-0-{index}", Static).content), f"{index}. {gap}")
                self.assertIn(route["evidence_needs"][index-1]["source"],
                    str(form.query_one(f"#decision-need-source-0-{index}", Static).content))
            jump = form.query_one("#decision-owner-request-0", Button)
            jump.scroll_visible(top=True, animate=False)
            await pilot.pause()
            self.assertTrue(await pilot.click("#decision-owner-request-0"))
            await pilot.pause()
            self.assertFalse(form.query_one("#decision-other-reply-0", Collapsible).collapsed)
            reply = form.query_one("#answer-0", Select)
            self.assertTrue(reply.has_focus)
            viewport = form.query_one("#decision-body", VerticalScroll).content_region
            frame = "\n".join(strip.text for strip in form._compositor.render_strips())
            self.assertTrue(viewport.contains_region(reply.region), f"reply={reply.region}, viewport={viewport}\n{frame}")
            self.assertIn("请解释并补齐材料", "".join(frame.split()))
            answers = form.answers()
            expected = preview_decisions(app.board, self.task.id, answers, Target("saved"))
            review = await self.review(app, pilot, "saved")
            self.assertIs(review.report_view, form.report_view)
            self.assertEqual(review.preview, expected)
            self.assertEqual(encode(self.store.load()), before)
            self.assertFalse(list(self.runtime.root.glob("*/commands/*.json")))

    async def test_presentation_owner_smoke_at_80_columns(self):
        await self.check_presentation_owner((80, 24))

    async def test_presentation_owner_at_40_columns(self):
        await self.check_presentation_owner((40, 20))

    async def test_presentation_owner_at_wide_size(self):
        await self.check_presentation_owner((130, 50))

    async def test_reply_layout_callback_does_not_focus_an_inactive_item(self):
        specs = tuple(DecisionSpec.from_record(spec_record(key=key)) for key in ('first', 'second'))
        run = self.attempt
        run.events = [encoded_event('partial', '合成双项', source='executor', run_id=run.id, seq=1,
            report=Report(run.summary, run.checks, run.remaining, decisions=specs))]
        before = self.saved_snapshot()
        app = self.app()
        async with app.run_test(size=(40, 20)) as pilot:
            form = await self.sheet(app, pilot)
            form.query_one('#intent-0', Select).value = 'feedback'
            await pilot.pause()
            overview = form.query_one('#decision-overview', OptionList)
            overview.focus()
            await pilot.pause()
            original = form.report_view
            form.reveal_reply(0, 'comment-0')
            overview.highlighted = 1
            form.select_item(1)
            await pilot.pause()
            self.assertTrue(overview.has_focus)
            self.assertEqual(form.selected_index, 1)
            self.assertEqual(form.answers(), (Answer(), Answer()))
            self.assertFalse(form.query_one('#decision-other-reply-0', Collapsible).collapsed)
            self.assertTrue(form.query_one('#decision-other-reply-1', Collapsible).collapsed)
            await pilot.resize_terminal(130, 40)
            await pilot.pause()
            self.assertEqual(form.report_view, original)
            self.assertEqual(encode(self.store.load()), before)
            self.assertFalse(list(self.runtime.root.glob('*/commands/*.json')))

    async def test_report_view_forgery_rejected_at_prepare_and_submit_without_side_effects(self):
        from decision_presentation import capture_report_view
        from journal import PREFIX
        app = self.app()
        before = self.saved_snapshot()
        async with app.run_test(size=(80, 24)) as pilot:
            form = await self.sheet(app, pilot)
            select_intent(form, 0, "feedback")
            form.query_one("#comment-0", TextArea).text = "保留这次反馈，不授权执行"
            await pilot.pause()
            original = form.report_view
            row = json.loads(original.source_bytes[len(PREFIX):])
            row["report"]["summary"] = "同身份但不是用户原来看的报告"
            forged = capture_report_view(original.source, PREFIX + json.dumps(row, ensure_ascii=False))
            forged_event = parse_event(forged.source_bytes)
            assert forged_event is not None
            self.assertEqual(forged_event.id, original.source.event_id)
            form.report_view = forged
            form.request("saved")
            await wait_ui(pilot, lambda: "展示" in str(form.query_one("#decision-error", Static).content))
            self.assertIs(app.screen, form)
            self.assertEqual(form.query_one("#comment-0", TextArea).text, "保留这次反馈，不授权执行")
            self.assertEqual(encode(app.board), before)
            form.report_view = original
            review = await self.review(app, pilot, "saved")
            review.report_view = forged
            await pilot.click("#decision-confirm")
            await wait_ui(pilot, lambda: "展示" in str(review.query_one("#decision-error", Static).content))
            self.assertEqual(encode(app.board), before)
            self.assertEqual(encode(self.store.load()), before)
            self.assertFalse(list(self.runtime.root.glob("*/commands/*.json")))
            await pilot.press("escape")
            await wait_ui(pilot, lambda: app.screen is form)
            self.assertEqual(form.query_one("#comment-0", TextArea).text, "保留这次反馈，不授权执行")

    async def test_same_event_reencoding_rejects_without_replacing_display_snapshot(self):
        from journal import PREFIX
        for stage in ("prepare", "submit"):
            with self.subTest(stage=stage):
                app = self.app()
                before_disk = self.saved_snapshot()
                async with app.run_test(size=(80, 24)) as pilot:
                    form = await self.sheet(app, pilot)
                    select_intent(form, 0, "feedback")
                    form.query_one("#comment-0", TextArea).text = "仅请核对已有结果"
                    await pilot.pause()
                    original = form.report_view
                    if stage == "submit":
                        review = await self.review(app, pilot, "saved")
                        self.assertIs(review.report_view, original)
                    current = app.board.runs[self.attempt.id]
                    row = json.loads(original.source_bytes[len(PREFIX):])
                    changed = PREFIX + json.dumps(row, ensure_ascii=False, indent=1)
                    self.assertNotEqual(changed, original.source_bytes)
                    self.assertEqual(parse_event(changed), parse_event(original.source_bytes))
                    current.events = [changed]
                    before = encode(app.board)
                    if stage == "prepare":
                        form.request("saved")
                    else:
                        await pilot.click("#decision-confirm")
                    await wait_ui(pilot, lambda app=app: "未提交" in str(app.screen.query_one("#decision-error", Static).content))
                    self.assertEqual(form.report_view, original)
                    self.assertEqual(form.query_one("#decision-report-envelope", TextArea).text, original.source_bytes)
                    self.assertEqual(encode(app.board), before)
                    self.assertEqual(encode(self.store.load()), before_disk)
                    self.assertFalse(list(self.runtime.root.glob("*/commands/*.json")))

    async def test_global_handoff_fallbacks_do_not_guess_actor_from_actions_or_summary(self):
        for brief in (None, Brief(["不要从动作猜责任"], "简报结果", "简报核验尚未完成")):
            with self.subTest(brief=brief):
                run = self.attempt
                self.task.summary = run.summary = "不能截取首句代替结论。完整原文第二句。"
                self.task.checks = run.checks = ["原检查一", "原检查二"]
                run.events = [encoded_event("partial", "旧报告", source="executor", run_id=run.id, seq=1,
                    report=Report("不能截取首句代替结论。完整原文第二句。", ["原检查一", "原检查二"], run.remaining,
                        brief, (DecisionSpec.from_record(spec_record()),)))]
                # Independent disposable initial report, not a persisted-history overwrite.
                async def no_submit(*_):
                    raise AssertionError("Read-only fallback test must not submit")
                form = DecisionSheet(self.board, self.task.id, no_submit)
                app = Waggle(board=self.board)
                async with app.run_test(size=(80, 24)) as pilot:
                    app.push_screen(form)
                    await wait_ui(pilot, lambda app=app, form=form: app.screen is form and form.is_mounted)
                    text = str(form.query_one("#decision-global-handoff", Static).content)
                    self.assertIn("全局下一责任未提供", text)
                    self.assertNotIn("不要从动作猜责任", text)
                    self.assertNotIn("不能截取首句", text)
                    if brief:
                        self.assertIn(brief.outcome, text)
                        self.assertIn(brief.verification, text)
                    else:
                        self.assertIn("结论见完整原报告", text)
                    report = str(form.query_one("#decision-original-report", Static).content)
                    for original in ("不能截取首句代替结论。完整原文第二句。", "原检查一", "原检查二", run.remaining):
                        self.assertIn(original, report)
                    if brief:
                        self.assertIn(brief.actions[0], report)

    async def test_frequent_decision_action_leads_but_final_review_defaults_to_back(self):
        before = self.saved_snapshot()
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            form = await self.sheet(app, pilot)
            controls = list(form.query(".decision-controls Button").results(Button))
            self.assertEqual([button.id for button in controls],
                ["decision-continue", "decision-saved", "decision-draft", "decision-close"])
            self.assertEqual(len({button.region.y for button in controls}), 1)
            self.assertEqual([button.region.x for button in controls], sorted(button.region.x for button in controls))
            self.assertFalse(app.board.decisions)
            self.assertIn("旧报告未明确受众", str(form.query_one("#decision-action-guide-0", Static).content))
            self.assertFalse(form.query("#decision-details-0"))
            self.assertFalse(form.query_one("#decision-technical-0").display)
            select_intent(form, 0, "formal")
            await pilot.pause()
            self.assertLess(form.query_one("#comparison-wide-0", Static).region.y, form.query_one("#answer-0", Select).region.y)
            form.query_one("#answer-0", Select).value = "choice:a"
            await pilot.pause()
            self.assertIn("未确认 · 方案待确认", str(form.query_one("#decision-overview", OptionList).get_option_at_index(0).prompt))
            review = await self.review(app, pilot, "continue")
            self.assertEqual([button.id for button in review.query(".decision-controls Button").results(Button)],
                             ["decision-confirm", "decision-back"])
            self.assertTrue(review.query_one("#decision-back", Button).has_focus)
            self.assertEqual(encode(self.store.load()), before)
            await pilot.press("enter")
            await wait_ui(pilot, lambda: app.screen is form)
            self.assertFalse(app.board.decisions)
            self.assertEqual(encode(self.store.load()), before)
            self.assertFalse(list(self.runtime.root.glob("*/commands/*.json")))

    async def test_narrow_decision_primary_is_first_row_and_review_still_defaults_to_back(self):
        before = self.saved_snapshot()
        app = self.app()
        async with app.run_test(size=(60, 24)) as pilot:
            form = await self.sheet(app, pilot)
            await pilot.pause()
            buttons = list(form.query(".decision-controls Button").results(Button))
            self.assertEqual([b.id for b in buttons],
                             ["decision-continue", "decision-saved", "decision-draft", "decision-close"])
            self.assertEqual(buttons[0].region.y, buttons[1].region.y)
            self.assertLess(buttons[0].region.y, buttons[2].region.y)
            for button in buttons:
                self.assertTrue(form.query_one("#decision-sheet").content_region.contains_region(button.region))
            select_intent(form, 0, "formal")
            form.query_one("#answer-0", Select).value = "choice:a"
            review = await self.review(app, pilot, "continue")
            self.assertTrue(review.query_one("#decision-back", Button).has_focus)
            await pilot.press("escape")
        self.assertEqual(encode(self.store.load()), before)
        self.assertFalse(list(self.runtime.root.glob("*/commands/*.json")))

    async def test_formal_review_exposes_risk_and_cost_before_confirm(self):
        app = self.app()
        before = self.saved_snapshot()
        async with app.run_test(size=(80, 24)) as pilot:
            form = await self.sheet(app, pilot)
            select_intent(form, 0, "formal")
            form.query_one("#answer-0", Select).value = "choice:a"
            review = await self.review(app, pilot, "continue")
            body = str(review.query_one("#decision-review-summary", Static).content)
            for fragment in ("正式选择", "风险：可能失败", "代价：一次小样本", "排除：不改生产文件"):
                self.assertIn(fragment, body)
            self.assertTrue(review.query_one("#decision-review-source", Collapsible).collapsed)
            self.assertTrue(review.query_one("#decision-back", Button).has_focus)
            await pilot.press("enter")
            await wait_ui(pilot, lambda app=app, form=form: app.screen is form)
            self.assertEqual(encode(self.store.load()), before)
            self.assertFalse(list(self.runtime.root.glob("*/commands/*.json")))

    async def test_phone_item_position_and_review_remaining_hint(self):
        specs = tuple(DecisionSpec.from_record(spec_record(key=f"item-{i}", missing=["待负责人核对"],
            question=f"第{i+1}项应如何安排？")) for i in range(4))
        run = self.attempt
        run.events = [encoded_event("partial", "四项待决定", source="executor", run_id=run.id, seq=1,
            report=Report(run.summary, run.checks, run.remaining, decisions=specs))]
        self.store.save(self.board)
        before = self.saved_snapshot()
        app = self.app()
        async with app.run_test(size=(40, 20)) as pilot:
            form = await self.sheet(app, pilot)
            navigation = form.query_one("#decision-navigation", Static)
            self.assertIn("第 1/4 项", str(navigation.content))
            await pilot.press("down")
            await wait_ui(pilot, lambda: form.selected_index == 1)
            self.assertIn("第 2/4 项", str(navigation.content))
            form.query_one("#decision-owner-request-1", Button).scroll_visible(top=True, animate=False)
            await pilot.pause()
            self.assertTrue(await pilot.click("#decision-owner-request-1"))
            review = await self.review(app, pilot, "continue")
            hint = review.query_one("#decision-review-progress", Static)
            self.assertIn("共 4 项", str(hint.content))
            self.assertIn("↓看其余", str(hint.content))
            self.assertIn("↓看其余", "\n".join(strip.text for strip in review._compositor.render_strips()))
            self.assertTrue(review.query_one("#decision-back", Button).has_focus)
            await pilot.press("enter")
            await wait_ui(pilot, lambda app=app, form=form: app.screen is form)
            self.assertEqual(encode(self.store.load()), before)

    async def test_d002_like_yes_enables_sticky_primary_as_explicit_feedback_at_40_and_80(self):
        specs = tuple(DecisionSpec.from_record(spec_record(key=f"decision-{i}", missing=["负责人回执待核"]))
                      for i in range(4))
        run = self.attempt
        run.events = [encoded_event("partial", "四项等待负责人", source="executor", run_id=run.id, seq=1,
            report=Report(run.summary, run.checks, run.remaining, decisions=specs))]
        self.store.save(self.board)
        before = self.saved_snapshot()
        for size in ((80, 24), (40, 20)):
            with self.subTest(size=size):
                app = self.app()
                async with app.run_test(size=size) as pilot:
                    form = await self.sheet(app, pilot)
                    primary = form.query_one("#decision-continue", Button)
                    self.assertTrue(primary.disabled)
                    self.assertEqual(form.query_one("#answer-0", Select).value, "undecided")
                    select_feedback_if_blank(form, 0)
                    form.query_one("#comment-0", TextArea).text = "是"
                    await pilot.pause()
                    self.assertFalse(primary.disabled, "输入‘是’后不得要求先选专业方案或另找滚动按钮")
                    self.assertEqual(str(primary.label), "核对本单1项")
                    self.assertTrue(form.query_one("#decision-sheet").content_region.contains_region(primary.region))
                    self.assertTrue(await pilot.click("#decision-continue"))
                    await wait_ui(pilot, lambda app=app: isinstance(app.screen, DecisionReview))
                    review = app.screen
                    assert isinstance(review, DecisionReview)
                    self.assertEqual(review.preview.answers[0].kind, "request_info")
                    self.assertEqual(review.preview.answers[0].comment, "是")
                    self.assertTrue(review.preview.compiled_text.startswith("仅补证请求："))
                    self.assertTrue(review.query_one("#decision-back", Button).has_focus)
                    self.assertEqual(encode(self.store.load()), before)
                    self.assertFalse(list(self.runtime.root.glob("*/commands/*.json")))
                    await pilot.press("enter")
                    await wait_ui(pilot, lambda app=app, form=form: app.screen is form)
                    self.assertEqual(form.query_one("#comment-0", TextArea).text, "是")
                    self.assertEqual(encode(self.store.load()), before)

    async def test_bottom_feedback_with_prior_formal_decision_never_reuses_old_approval(self):
        self.seed_fixture()
        prepare_decision_intent(self.board, preview_decisions(self.board, self.task.id,
            (Answer("choice", "a"),), Target("saved")))
        self.store.save(self.board)
        before = self.saved_snapshot()
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            form = await self.sheet(app, pilot)
            self.assertEqual(form.query_one("#answer-0", Select).value, "undecided")
            select_feedback_if_blank(form, 0)
            form.query_one("#comment-0", TextArea).text = "是"
            await pilot.pause()
            primary = form.query_one("#decision-continue", Button)
            self.assertFalse(primary.disabled)
            self.assertEqual(str(primary.label), "核对本单1项")
            review = await self.review(app, pilot, "continue")
            self.assertEqual(review.preview.prior, ("",))
            self.assertEqual(review.preview.answers[0].kind, "request_info")
            self.assertTrue(review.preview.compiled_text.startswith("仅补证请求："))
            self.assertNotIn("此前已确认", review.preview.compiled_text)
            self.assertNotIn("选择：a", review.preview.compiled_text)
            self.assertEqual(encode(self.store.load()), before)
            self.assertFalse(list(self.runtime.root.glob("*/commands/*.json")))

    async def test_explicit_feedback_needs_no_technical_selection_at_40_and_80_columns(self):
        specs = tuple(DecisionSpec.from_record(spec_record(key=f"item-{i}", missing=["负责人原始回执待核"])) for i in range(4))
        run = self.attempt
        run.events = [encoded_event("partial", "四项需负责人核对", source="executor", run_id=run.id, seq=1,
            report=Report(run.summary, run.checks, run.remaining, decisions=specs))]
        self.store.save(self.board)
        before = self.saved_snapshot()
        for size in ((80, 24), (40, 20)):
            with self.subTest(size=size):
                app = self.app()
                async with app.run_test(size=size) as pilot:
                    form = await self.sheet(app, pilot)
                    self.assertEqual(form.query_one("#answer-0", Select).value, "undecided")
                    self.assertTrue(form.query_one("#decision-continue", Button).disabled)
                    select_feedback_if_blank(form, 0)
                    form.query_one("#comment-0", TextArea).text = "请根据已核资料推荐等待时间；若不足说明缺谁的回执"
                    await pilot.pause()
                    self.assertFalse(form.query_one("#decision-continue", Button).disabled)
                    self.assertEqual(str(form.query_one("#decision-continue", Button).label), "核对本单1项")
                    self.assertIn("发送须最后核对", str(form.query_one("#decision-flow", Static).content))
                    self.assertIn("确认不发送", str(form.query_one("#decision-saved", Button).label))
                    feedback = form.query_one("#decision-feedback-0", Button)
                    feedback.scroll_visible(top=True, animate=False)
                    await pilot.pause()
                    self.assertTrue(await pilot.click("#decision-feedback-0"))
                    await wait_ui(pilot, lambda app=app: isinstance(app.screen, DecisionReview))
                    review = app.screen
                    assert isinstance(review, DecisionReview)
                    self.assertIn("反馈", review.summary())
                    self.assertIn("仅补证请求", review.preview.compiled_text)
                    self.assertEqual([a.kind for a in review.preview.answers],
                                     ["request_info", "undecided", "undecided", "undecided"])
                    self.assertEqual(review.preview.answers[0].comment, "请根据已核资料推荐等待时间；若不足说明缺谁的回执")
                    self.assertNotIn("正式选择", review.preview.compiled_text)
                    self.assertEqual(str(review.query_one("#decision-confirm", Button).label),
                                     "确认并发送反馈·仅补证")
                    self.assertTrue(review.query_one("#decision-back", Button).has_focus)
                    self.assertEqual(encode(self.store.load()), before)
                    await pilot.press("enter")
                    await wait_ui(pilot, lambda app=app, form=form: app.screen is form)
                    self.assertEqual(form.query_one("#answer-0", Select).value, "undecided")
                    self.assertEqual(form.query_one("#comment-0", TextArea).text, review.preview.answers[0].comment)
                    self.assertFalse(list(self.runtime.root.glob("*/commands/*.json")))
                self.assertEqual(encode(self.store.load()), before)

    async def test_feedback_draft_restore_and_one_confirmed_evidence_only_dispatch(self):
        spec = DecisionSpec.from_record(spec_record(missing=["负责人原始回执待核"]))
        run = self.attempt
        run.events = [encoded_event("partial", "待补证", source="executor", run_id=run.id, seq=1,
            report=Report(run.summary, run.checks, run.remaining, decisions=(spec,)))]
        self.store.save(self.board)
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            form = await self.sheet(app, pilot)
            select_feedback_if_blank(form, 0)
            form.query_one("#comment-0", TextArea).text = "我不知道该选什么，请给有依据的建议"
            await self.review(app, pilot, "draft")
            await self.confirmed(app, pilot)
            self.assertEqual(len(app.board.runs), 1)
            self.assertFalse(list(self.runtime.root.glob("*/commands/*.json")))
            form = await self.sheet(app, pilot)
            self.assertTrue(form.has_draft)
            self.assertEqual(form.query_one("#answer-0", Select).value, "undecided")
            self.assertEqual(form.query_one("#comment-0", TextArea).text, "我不知道该选什么，请给有依据的建议")
            review = await self.review(app, pilot, "feedback-0")
            self.assertEqual(review.preview.answers[0].kind, "request_info")
            self.assertEqual(len(app.board.runs), 1)
            await self.confirmed(app, pilot)
            self.assertEqual(len(app.board.runs), 2)
            record = list(app.board.decisions.values())[-1]
            self.assertEqual(record.intent.kind, "new_run")
            self.assertEqual(record.preview.compiled_text, app.board.runs[record.intent.run_id].requirements)
            self.assertEqual(record.preview.answers[0].kind, "request_info")
            self.assertEqual(len(list(self.runtime.root.glob("*/commands/*.json"))), 1)

    async def test_recommendation_without_text_can_be_saved_locally_then_sent_explicitly(self):
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            form = await self.sheet(app, pilot)
            self.assertEqual(form.query_one("#comment-0", TextArea).text, "")
            select_intent(form, 0, "feedback")
            await pilot.pause()
            recommend = form.query_one("#decision-recommend-0", Button)
            recommend.scroll_visible(top=True, animate=False)
            await pilot.pause()
            self.assertTrue(await pilot.click("#decision-recommend-0"))
            request = form.query_one("#comment-0", TextArea).text
            self.assertIn("有来源的建议", request)
            self.assertIn("若依据不足", request)
            self.assertEqual(form.query_one("#answer-0", Select).value, "undecided")
            review = await self.review(app, pilot, "saved")
            self.assertEqual(review.preview.answers[0].kind, "undecided")
            self.assertEqual(str(review.query_one("#decision-confirm", Button).label), "确认仅保存反馈")
            self.assertIn("反馈", review.target_label)
            await self.confirmed(app, pilot)
            self.assertEqual(len(app.board.runs), 1)
            self.assertEqual(list(app.board.decisions.values())[-1].intent.kind, "saved")
            self.assertFalse(list(self.runtime.root.glob("*/commands/*.json")))
            form = await self.sheet(app, pilot)
            self.assertEqual(form.query_one("#comment-0", TextArea).text, request)
            self.assertIn("反馈仅保存", str(form.query_one("#decision-live-0", Static).content))
            self.assertEqual(form.query_one("#answer-0", Select).value, "undecided")
            review = await self.review(app, pilot, "feedback-0")
            self.assertEqual(review.preview.answers[0].kind, "request_info")
            self.assertIn(request, review.preview.compiled_text)
            self.assertTrue(review.query_one("#decision-back", Button).has_focus)
            self.assertEqual(len(app.board.runs), 1)
            self.assertFalse(list(self.runtime.root.glob("*/commands/*.json")))
            await self.confirmed(app, pilot)
            self.assertEqual(len(app.board.runs), 2)
            self.assertEqual(len(list(self.runtime.root.glob("*/commands/*.json"))), 1)
            self.assertEqual(list(app.board.decisions.values())[-1].intent.kind, "new_run")

    async def test_feedback_to_live_run_adds_only_one_evidence_note_without_new_run(self):
        self.use_live_board()
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            form = await self.sheet(app, pilot)
            select_feedback_if_blank(form, 0)
            form.query_one("#comment-0", TextArea).text = "只查现有回执，给出单位和不确定性"
            review = await self.review(app, pilot, "feedback-0")
            self.assertEqual(review.preview.target.kind, "live_supplement")
            self.assertTrue(review.preview.compiled_text.startswith("仅补证请求："))
            await self.confirmed(app, pilot)
            record = list(app.board.decisions.values())[-1]
            self.assertEqual(len(app.board.runs), 1)
            self.assertEqual(record.intent.kind, "live_supplement")
            self.assertEqual(record.preview.compiled_text, app.board.supplements[record.intent.supplement_id].text)
            self.assertEqual(len(list(self.runtime.root.glob("*/supplements/requests/*.json"))), 1)

    async def test_routed_owner_evidence_shows_provider_and_no_setting_or_formal_choice(self):
        spec = DecisionSpec.from_record(spec_record(missing=["原始回执缺失"], routing=routing_payload()))
        run = self.attempt
        run.events = [encoded_event("partial", "待负责人", source="executor", run_id=run.id, seq=1,
            report=Report(run.summary, run.checks, run.remaining, decisions=(spec,)))]
        self.store.save(self.board)
        before = self.saved_snapshot()
        for size in ((80, 24), (40, 20)):
            with self.subTest(size=size):
                app = self.app()
                async with app.run_test(size=size) as pilot:
                    form = await self.sheet(app, pilot)
                    self.assertIn("提供方（报告原称）：回测负责人", " ".join(str(w.content) for w in form.query(".decision-meta").results(Static)))
                    self.assertIn("原始成交回执", str(form.query_one("#decision-need-source-0-1", Static).content))
                    self.assertEqual(str(form.query_one("#decision-gap-0-1", Static).content), "1. 原始回执缺失")
                    self.assertIn("暂不能推荐具体设置值", str(form.query_one("#decision-recommendation-0", Static).content))
                    with self.assertRaises(InvalidSelectValueError):
                        select_intent(form, 0, "formal")
                        form.query_one("#answer-0", Select).value = "choice:a"
                    select_feedback_if_blank(form, 0)
                    form.query_one("#comment-0", TextArea).text = "请说明缺哪份回执"
                    review = await self.review(app, pilot, "feedback-0")
                    self.assertIn("负责人：回测负责人；来源：原始成交回执", review.preview.compiled_text)
                    self.assertNotIn("选择：a", review.preview.compiled_text)
                    await pilot.press("enter")
                    self.assertEqual(encode(self.store.load()), before)

    async def test_feedback_rejects_formal_selection_and_stale_preview_without_dispatch(self):
        app = self.app()
        before = self.saved_snapshot()
        async with app.run_test(size=(80, 24)) as pilot:
            form = await self.sheet(app, pilot)
            select_feedback_if_blank(form, 0)
            form.query_one("#comment-0", TextArea).text = "请解释并给证据"
            select_intent(form, 0, "formal")
            form.query_one("#answer-0", Select).value = "choice:a"
            form.request("feedback")
            await wait_ui(pilot, lambda: "不能夹带" in str(form.query_one("#decision-error", Static).content))
            self.assertEqual(encode(self.store.load()), before)
            self.assertFalse(list(self.runtime.root.glob("*/commands/*.json")))
            select_intent(form, 0, "feedback")
            form.query_one("#answer-0", Select).value = "undecided"
            review = await self.review(app, pilot, "feedback-0")
            app.board.runs[self.attempt.id].events = [encoded_event("partial", "已变更", source="executor",
                run_id=self.attempt.id, seq=1, report=Report("已变更", self.attempt.checks, self.attempt.remaining,
                    decisions=(DecisionSpec.from_record(spec_record(cost="变更")),)))]
            before_change = encode(app.board)
            await pilot.click("#decision-confirm")
            await wait_ui(pilot, lambda: "未提交" in str(review.query_one("#decision-error", Static).content))
            self.assertEqual(encode(app.board), before_change)
            self.assertFalse(list(self.runtime.root.glob("*/commands/*.json")))
            await pilot.click("#decision-back")
            await wait_ui(pilot, lambda: app.screen is form)
            self.assertEqual(form.query_one("#comment-0", TextArea).text, "请解释并给证据")

    async def test_owner_evidence_shortcut_and_blank_continue_gate(self):
        spec = DecisionSpec.from_record(spec_record("approval", missing=["负责人须提供原始账本"], owner="实验负责人提供证据"))
        run = self.attempt
        run.events = [encoded_event("partial", "尚缺材料", source="executor", run_id=run.id, seq=1,
            report=Report(run.summary, run.checks, run.remaining, decisions=(spec,)))]
        self.store.save(self.board)
        before = self.saved_snapshot()
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            form = await self.sheet(app, pilot)
            button = form.query_one("#decision-continue", Button)
            self.assertTrue(button.disabled)
            self.assertIn("未选问题不发送", str(form.query_one("#decision-flow", Static).content))
            form.query_one('#decision-owner-request-0', Button).scroll_visible(top=True, animate=False)
            await pilot.pause()
            self.assertTrue(await pilot.click("#decision-owner-request-0"))
            await pilot.pause()
            self.assertEqual(form.query_one("#answer-0", Select).value, "request_info")
            self.assertFalse(button.disabled)
            self.assertIn("负责人补证", str(form.query_one("#decision-overview", OptionList).get_option_at_index(0).prompt))
            self.assertFalse(app.board.decisions)
            self.assertFalse(list(self.runtime.root.glob("*/commands/*.json")))
            select_intent(form, 0, "feedback")
            form.query_one("#answer-0", Select).value = "undecided"
            await pilot.pause()
            self.assertTrue(button.disabled)
            self.assertEqual(encode(self.store.load()), before)

    async def test_old_business_fact_is_readonly_after_changed_report_at_phone_width(self):
        question = {"kind": "choice", "question": "旧单未结清时必须反转？",
                    "options": [{"key": "yes", "label": "需要"}, {"key": "no", "label": "不需要"}]}
        old = DecisionSpec.from_record(spec_record(human_input=question))
        run = self.attempt
        run.events = [encoded_event("partial", "旧问题", source="executor", run_id=run.id, seq=1,
            report=Report(run.summary, run.checks, run.remaining, decisions=(old,)))]
        self.store.save(self.board)
        prepare_decision_intent(self.board, preview_decisions(self.board, self.task.id,
            (Answer(business=BusinessAnswer("yes")),), Target("saved")))
        self.store.save(self.board)
        old_snapshot = deepcopy(asdict(run))
        updated = DecisionSpec.from_record(spec_record(human_input=question, cost="待重新核价"))
        # A changed report belongs to a genuinely new round, not the immutable ended Run.
        self.board.return_work(self.task, run.requirements)
        self.board.assign(self.task, run.agent_id)
        queued = self.board.queue_real(self.task, str(uuid4()), run.session_id, run.cwd)
        self.store.save(self.board)
        incoming = deepcopy(queued)
        incoming.state, incoming.delivery, incoming.seq = "finished", "partial", 1
        incoming.summary, incoming.checks, incoming.remaining = run.summary, list(run.checks), run.remaining
        incoming.events = [encoded_event("partial", "新方案", source="executor", run_id=incoming.id, seq=1,
            report=Report(incoming.summary, incoming.checks, incoming.remaining, decisions=(updated,)))]
        self.assertTrue(self.board.ingest(incoming))
        self.attempt = self.board.runs[incoming.id]
        self.store.save(self.board)
        persisted = self.store.load()
        self.assertEqual(asdict(persisted.runs[run.id]), old_snapshot)
        self.assertEqual(self.attempt.requirements, run.requirements)
        self.assertEqual(len(persisted.runs), 2)
        self.assertEqual(len(persisted.decisions), 1)
        session = SessionObservation(self.agent.id, self.agent.session_id, self.agent.cwd, 2,
            int(time.time() * 1000), "idle", self.attempt.id, "", False, False)
        path = self.runtime.root / self.agent.id / "session.json"
        path.write_text(json.dumps({"version": 1, **asdict(session)}), encoding="utf-8")
        path.chmod(0o600)
        before = self.saved_snapshot()
        for size in ((80, 24), (40, 20)):
            with self.subTest(size=size):
                app = self.app()
                async with app.run_test(size=size) as pilot:
                    form = await self.sheet(app, pilot)
                    self.assertEqual(form.query_one("#business-choice-0", Select).value, "")
                    note = str(form.query_one("#decision-business-history-0", Static).content)
                    self.assertIn("需要", note)
                    self.assertIn("仅供核对", note)
                    self.assertIn("不自动沿用", note)
                    self.assertIsNone(form.answers()[0].business)
                    self.assertEqual(encode(self.store.load()), before)

    async def test_same_missing_material_waits_for_owner_at_40_and_80_columns(self):
        self.board, self.task, self.attempt = repeated_evidence_board(save=self.store.save, cwd=self.project)
        self.store.save(self.board)
        before = self.saved_snapshot()
        for size in ((80, 24), (40, 20)):
            with self.subTest(size=size):
                app = self.app()
                async with app.run_test(size=size) as pilot:
                    form = await self.sheet(app, pilot)
                    self.assertIn("等待负责人材料", form.item_status(0))
                    self.assertIn("没有新证据", str(form.query_one("#decision-waiting-0", Static).content))
                    self.assertIn("不会自动派发", str(form.query_one("#decision-waiting-0", Static).content))
                    self.assertEqual(encode(self.store.load()), before)
                    self.assertFalse(list(self.runtime.root.glob("*/commands/*.json")))

    async def test_business_answers_and_technical_options_are_independent(self):
        binary = {"kind": "choice", "question": "旧单未结清是否要反转？", "options": [
            {"key": "yes", "label": "需要"}, {"key": "no", "label": "不需要"},
            {"key": "conditional", "label": "有条件"}, {"key": "unknown", "label": "暂不确定"}]}
        path = {"kind": "text", "question": "未来允许看哪些确切目录？", "options": []}
        specs = (DecisionSpec.from_record(spec_record(key="business", missing=["费用规范未核", "旧成交待核"], human_input=binary)),
                 DecisionSpec.from_record(spec_record(key="scope", missing=["来源待核"], human_input=path)))
        run = self.attempt
        run.events = [encoded_event("partial", "两项业务输入", source="executor", run_id=run.id, seq=1,
            report=Report(run.summary, run.checks, run.remaining, decisions=specs))]
        self.store.save(self.board)
        before = self.saved_snapshot()
        for size in ((80, 24), (40, 20)):
            with self.subTest(size=size):
                app = self.app()
                async with app.run_test(size=size) as pilot:
                    form = await self.sheet(app, pilot)
                    self.assertEqual(form.query_one("#business-choice-0", Select).value, "")
                    self.assertEqual(form.query_one("#answer-0", Select).value, "undecided")
                    select_intent(form, 0, "fact")
                    form.query_one("#business-choice-0", Select).value = "yes"
                    form.query_one("#business-detail-0", Input).value = "最多等30秒；失败就停"
                    await pilot.pause()
                    self.assertIn("未确认 · 已答业务问题", str(form.query_one("#decision-overview", OptionList).get_option_at_index(0).prompt))
                    self.assertEqual(form.query_one("#answer-0", Select).value, "undecided")
                    form.select_item(1)
                    select_intent(form, 1, "fact")
                    form.query_one("#business-text-1", Input).value = "/future/read-only"
                    await pilot.pause()
                    self.assertEqual(form.query_one("#answer-1", Select).value, "undecided")
                    review = await self.review(app, pilot, "continue")
                    self.assertIn("仅补证请求", review.preview.compiled_text)
                    self.assertIn("需要", review.preview.compiled_text)
                    self.assertIn("/future/read-only", review.preview.compiled_text)
                    self.assertIn("不授权本轮读取所填路径", review.preview.compiled_text)
                    self.assertNotIn("选择：a", review.preview.compiled_text)
                    self.assertTrue(review.query_one("#decision-back", Button).has_focus)
                    await pilot.press("enter")
                    await wait_ui(pilot, lambda form=form, app=app: app.screen is form)
                    self.assertEqual(encode(self.store.load()), before)
                    self.assertFalse(list(self.runtime.root.glob("*/commands/*.json")))

    async def test_business_choice_visible_and_condition_validates_inline(self):
        spec = DecisionSpec.from_record(spec_record(missing=["仍需负责人核对"], human_input={"kind": "choice",
            "question": "未结清时必须反向？", "options": [{"key": "yes", "label": "需要"}, {"key": "no", "label": "不需要"},
                {"key": "conditional", "label": "有条件"}, {"key": "unknown", "label": "暂不确定"}]}))
        run = self.attempt
        run.events = [encoded_event("partial", "业务问题", source="executor", run_id=run.id, seq=1,
            report=Report(run.summary, run.checks, run.remaining, decisions=(spec,)))]
        self.store.save(self.board)
        before = self.saved_snapshot()
        for size in ((80, 24), (40, 20)):
            with self.subTest(size=size):
                app = self.app()
                async with app.run_test(size=size) as pilot:
                    form = await self.sheet(app, pilot)
                    self.assertFalse(form.query_one("#decision-fact-0").display)
                    select_intent(form, 0, "fact")
                    await pilot.pause()
                    body = form.query_one("#decision-body", VerticalScroll)
                    choice = form.query_one("#business-choice-0", Select)
                    choice.scroll_visible(top=True, animate=False)
                    await pilot.pause()
                    if size[0] == 80:
                        self.assertTrue(body.content_region.contains_region(choice.region), "80 columns should expose the business answer without a jump")
                    else:
                        self.assertTrue(body.content_region.contains_region(choice.region))
                    detail = form.query_one("#business-detail-0", Input)
                    self.assertFalse(detail.display)
                    choice.value = "unknown"
                    await pilot.pause()
                    self.assertIn("暂不确定", str(form.query_one("#decision-overview", OptionList).get_option_at_index(0).prompt))
                    self.assertFalse(detail.display)
                    choice.value = "conditional"
                    await pilot.pause()
                    self.assertTrue(detail.display)
                    error = form.query_one("#business-error-0", Static)
                    self.assertTrue(error.display)
                    self.assertIn("请写明条件", str(error.content))
                    detail.value = "等待不超过30秒；失败停止"
                    await pilot.pause()
                    self.assertFalse(error.display)
                    business = form.answers()[0].business
                    assert business is not None
                    self.assertEqual(business.value, "conditional")
                    self.assertEqual(encode(self.store.load()), before)
                    self.assertFalse(list(self.runtime.root.glob("*/commands/*.json")))

    async def test_current_review_pairs_authoritative_business_or_formal_question_with_response(self):
        business_question = "归档前是否必须注销旧编号？"
        formal_question = "本轮应选择哪条归档实现路线？"
        spec = DecisionSpec.from_record(spec_record(question=formal_question, human_input={
            "kind": "choice", "question": business_question,
            "options": [{"key": "conditional", "label": "有条件"}, {"key": "no", "label": "不需要"}]}))
        run = self.attempt
        run.events = [encoded_event("partial", "明确业务问题和技术问题", source="executor", run_id=run.id, seq=1,
                                   report=Report(run.summary, run.checks, run.remaining, decisions=(spec,)))]
        self.store.save(self.board)
        before = self.saved_snapshot()
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            form = await self.sheet(app, pilot)
            select_intent(form, 0, "fact")
            form.query_one("#business-choice-0", Select).value = "conditional"
            form.query_one("#business-detail-0", Input).value = "同批次ID重复时"
            review = await self.review(app, pilot, "saved")
            summary = str(review.query_one("#decision-review-summary", Static).content)
            self.assertIn("所属任务：D001", summary)
            self.assertIn(self.task.title, summary)
            self.assertIn("针对的问题：" + business_question, summary)
            self.assertIn("问题来源：D001 · 当前报告第1项 · 版本1", summary)
            self.assertIn("本次答复：", summary)
            self.assertIn("业务：有条件；条件：同批次ID重复时", summary)
            self.assertLess(summary.index(business_question), summary.index("问题来源："))
            self.assertLess(summary.index("问题来源："), summary.index("本次答复："))
            self.assertNotIn(formal_question, summary)
            self.assertEqual(review.preview.source.run_id, run.id)
            await pilot.press("escape")
            await wait_ui(pilot, lambda: app.screen is form and not form.busy)
            select_intent(form, 0, "formal")
            form.query_one("#answer-0", Select).value = "choice:a"
            review = await self.review(app, pilot, "saved")
            summary = str(review.query_one("#decision-review-summary", Static).content)
            self.assertIn("针对的问题：" + formal_question, summary)
            self.assertIn("本次答复：", summary)
            self.assertIn("正式选择：路线 A", summary)
            self.assertNotIn(business_question, summary)
            self.assertNotIn("同批次ID重复时", summary, "隐藏业务字段不能伪装成此次正式答复")
            self.assertIn("风险：", summary)
            self.assertIn("代价：", summary)
            self.assertEqual(encode(self.store.load()), before)
            self.assertFalse(list(self.runtime.root.glob("*/commands/*.json")))

    async def test_review_summarizes_answer_before_collapsed_original(self):
        spec = DecisionSpec.from_record(spec_record(missing=["负责人仍需核对"], human_input={"kind": "choice",
            "question": "必须反转？", "options": [{"key": "yes", "label": "需要"}, {"key": "no", "label": "不需要"}]}))
        run = self.attempt
        run.events = [encoded_event("partial", "等业务输入", source="executor", run_id=run.id, seq=1,
            report=Report(run.summary, run.checks, run.remaining, decisions=(spec,)))]
        self.store.save(self.board)
        before = self.saved_snapshot()
        for size in ((80, 24), (40, 20)):
            with self.subTest(size=size):
                app = self.app()
                async with app.run_test(size=size) as pilot:
                    form = await self.sheet(app, pilot)
                    select_intent(form, 0, "fact")
                    form.query_one("#business-choice-0", Select).value = "yes"
                    review = await self.review(app, pilot, "saved")
                    summary = str(review.query_one("#decision-review-summary", Static).content)
                    self.assertIn("业务：需要", summary)
                    self.assertNotIn("方案：未选择", summary)
                    self.assertNotIn("本项无独立业务问题", summary)
                    self.assertIn("仅本地保存", summary)
                    self.assertTrue(review.query_one("#decision-review-source", Collapsible).collapsed)
                    self.assertIn("业务答案", str(review.query_one("#decision-confirm", Button).label))
                    frame = "\n".join(strip.text for strip in review._compositor.render_strips())
                    self.assertIn("需要", frame)
                    self.assertNotIn("报告修订", frame)
                    self.assertTrue(review.query_one("#decision-back", Button).has_focus)
                    await pilot.press("enter")
                    await wait_ui(pilot, lambda app=app, form=form: app.screen is form)
                    self.assertEqual(encode(self.store.load()), before)
                    self.assertFalse(list(self.runtime.root.glob("*/commands/*.json")))

    async def test_complete_business_answer_alone_is_still_only_evidence_request(self):
        human = {"kind": "choice", "question": "必须在旧单未结清时反向？", "options": [
            {"key": "yes", "label": "需要"}, {"key": "no", "label": "不需要"}]}
        spec = DecisionSpec.from_record(spec_record(human_input=human))
        run = self.attempt
        run.events = [encoded_event("partial", "已备选项", source="executor", run_id=run.id, seq=1,
            report=Report(run.summary, run.checks, run.remaining, decisions=(spec,)))]
        self.store.save(self.board)
        before = self.saved_snapshot()
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            form = await self.sheet(app, pilot)
            self.assertEqual(form.query_one("#answer-0", Select).value, "undecided")
            select_intent(form, 0, "fact")
            form.query_one("#business-choice-0", Select).value = "yes"
            await pilot.pause()
            self.assertIn("核对本单", str(form.query_one("#decision-continue", Button).label))
            review = await self.review(app, pilot, "continue")
            self.assertIn("仅补证请求", review.preview.compiled_text)
            self.assertEqual(str(review.query_one("#decision-confirm", Button).label), "确认并发送补证请求")
            self.assertTrue(review.query_one("#decision-back", Button).has_focus)
            await pilot.press("enter")
            await wait_ui(pilot, lambda: app.screen is form)
            self.assertEqual(encode(self.store.load()), before)
            self.assertFalse(list(self.runtime.root.glob("*/commands/*.json")))

    async def test_incomplete_reply_one_click_before_collapsed_technical_detail(self):
        spec = DecisionSpec.from_record(spec_record(question="旧订单还没结束，是否反向？", missing=["旧状态需核对"] ))
        run = self.attempt
        run.events = [encoded_event("partial", "未满足材料", source="executor", run_id=run.id, seq=1,
            report=Report(run.summary, run.checks, run.remaining, decisions=(spec,)))]
        self.store.save(self.board)
        before = self.saved_snapshot()
        for size in ((80, 24), (40, 20)):
            with self.subTest(size=size):
                app = self.app()
                async with app.run_test(size=size) as pilot:
                    form = await self.sheet(app, pilot)
                    await pilot.pause()
                    body = form.query_one("#decision-body", VerticalScroll)
                    jump = form.query_one("#decision-reply-0", Button)
                    jump.scroll_visible(top=True, animate=False)
                    await pilot.pause()
                    self.assertTrue(body.content_region.contains_region(jump.region), f"{size}: reply jump must be reachable without opening details")
                    self.assertTrue(await pilot.click("#decision-reply-0"))
                    await pilot.pause()
                    self.assertTrue(form.query_one("#answer-0", Select).has_focus)
                    for name, kind in (("answer", Select), ("comment", TextArea)):
                        widget = form.query_one(f"#{name}-0", kind)
                        widget.scroll_visible(top=True, animate=False)
                        await pilot.pause()
                        self.assertTrue(body.content_region.contains_region(widget.region),
                                        f"{size}: {name} must be reachable by scrolling")
                    form.query_one("#answer-0", Select).focus()
                    form.query_one("#answer-0", Select).scroll_visible(top=True, animate=False)
                    await pilot.pause()
                    await pilot.press("enter", "down", "enter")
                    await wait_ui(pilot, lambda form=form: form.query_one("#answer-0", Select).value == "request_info")
                    self.assertIn("未确认 · 待解释", str(form.query_one("#decision-overview", OptionList).get_option_at_index(0).prompt))
                    details = form.query_one("#decision-details-0", Collapsible)
                    self.assertTrue(details.collapsed)
                    self.assertIn("查看", str(details.title))
                    self.assertIn("旧状态需核对", str(form.query_one("#decision-gap-0-1", Static).content))
                    self.assertFalse(any(isinstance(ancestor, Collapsible) and ancestor.collapsed
                                         for ancestor in form.query_one("#decision-gap-0-1", Static).ancestors))
                    self.assertTrue(form.query_one("#decision-sheet").content_region.contains_region(
                        form.query_one("#decision-continue", Button).region))
                    self.assertEqual(form.query_one("#answer-0", Select).value, "request_info")
                    self.assertEqual(encode(self.store.load()), before)
                    self.assertFalse(list(self.runtime.root.glob("*/commands/*.json")))

    async def test_three_levels_default_draft_resume_saved_then_one_atomic_dispatch(
        self,
    ):
        app = self.app()
        calls = []
        publish_dispatch = self.runtime.dispatch

        def dispatched(run):
            with Store(self.store.path) as observer:
                persisted = observer.load()
            records = list(persisted.decisions.values())
            self.assertEqual(records[-1].intent.run_id, run.id)
            self.assertEqual(records[-1].preview.compiled_text, run.requirements)
            calls.append(run.id)
            publish_dispatch(run)

        with patch.object(self.runtime, "dispatch", side_effect=dispatched):
            async with app.run_test(size=(80, 24)) as pilot:
                form = await self.sheet(app, pilot)
                self.assertEqual(form.query_one("#answer-0", Select).value, "undecided")
                for button in form.query(".decision-controls Button"):
                    self.assertTrue(
                        form.query_one(
                            "#decision-sheet"
                        ).content_region.contains_region(button.region)
                    )
                select_intent(form, 0, "formal")
                form.query_one("#answer-0", Select).value = "choice:a"
                await self.review(app, pilot, "draft")
                await self.confirmed(app, pilot)
                self.assertEqual(len(app.board.runs), 1)
                self.assertFalse(prior_answers(app.board, self.task.id))
                self.assertFalse(calls)
                form = await self.sheet(app, pilot)
                self.assertTrue(form.has_draft)
                self.assertEqual(form.query_one("#answer-0", Select).value, "choice:a")
                await self.review(app, pilot, "saved")
                await self.confirmed(app, pilot)
                self.assertEqual(len(app.board.runs), 1)
                self.assertEqual(app.board.tasks[self.task.id], self.task)
                self.assertFalse(calls)
                form = await self.sheet(app, pilot)
                self.assertEqual(form.query_one("#answer-0", Select).value, "undecided")
                self.assertTrue(form.previous)
                self.assertTrue(form.query_one("#decision-continue", Button).disabled)
                select_intent(form, 0, "formal")
                form.query_one("#answer-0", Select).value = "choice:a"
                review = await self.review(app, pilot, "continue")
                self.assertEqual(review.preview.prior, ("",))
                self.assertIn("选择：a", review.preview.compiled_text)
                self.assertIn("风险：", str(review.query_one("#decision-review-summary", Static).content))
                self.assertIn("代价：", str(review.query_one("#decision-review-summary", Static).content))
                await self.confirmed(app, pilot)
                self.assertEqual(len(app.board.runs), 2)
                self.assertEqual(len(calls), 1, app.runtime_errors)
                self.assertEqual(
                    len(list(self.runtime.root.glob("*/commands/*.json"))), 1
                )
                self.assertNotEqual(app.board.tasks[self.task.id].status, "done")

    async def test_changed_report_after_preview_rejects_with_input_retained(self):
        app = self.app()
        async with app.run_test(size=(100, 32)) as pilot:
            form = await self.sheet(app, pilot)
            select_intent(form, 0, "formal")
            form.query_one("#answer-0", Select).value = "choice:a"
            await self.review(app, pilot, "saved")
            run = app.board.runs[self.attempt.id]
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
                            DecisionSpec.from_record(spec_record(cost="changed")),
                        ),
                    ),
                )
            ]
            before = encode(app.board)
            await pilot.click("#decision-confirm")
            await wait_ui(
                pilot,
                lambda: (
                    "未提交"
                    in str(app.screen.query_one("#decision-error", Static).content)
                ),
            )
            self.assertEqual(encode(app.board), before)
            self.assertFalse(app.board.decisions)
            await pilot.click("#decision-back")
            await wait_ui(pilot, lambda: app.screen is form)
            self.assertEqual(form.query_one("#answer-0", Select).value, "choice:a")

    async def test_storage_failure_and_publication_uncertainty_never_retry(self):
        app = self.app()
        async with app.run_test(size=(100, 32)) as pilot:
            form = await self.sheet(app, pilot)
            select_intent(form, 0, "formal")
            form.query_one("#answer-0", Select).value = "choice:a"
            await self.review(app, pilot, "continue")
            before = encode(app.board)
            with (
                patch.object(
                    self.store, "save", side_effect=StorageError("disk failure")
                ),
                patch.object(self.runtime, "dispatch") as send,
            ):
                await pilot.click("#decision-confirm")
                await wait_ui(
                    pilot,
                    lambda: (
                        "disk failure"
                        in str(app.screen.query_one("#decision-error", Static).content)
                    ),
                )
                self.assertEqual(encode(app.board), before)
                send.assert_not_called()
            with patch.object(
                self.runtime, "dispatch", side_effect=OSError("publication uncertain")
            ) as send:
                await self.confirmed(app, pilot)
                self.assertEqual(len(app.board.decisions), 1)
                self.assertEqual(len(app.board.runs), 2)
                send.assert_called_once()
                await pilot.pause()
                send.assert_called_once()

    def test_d002_plain_cards_match_only_the_current_report(self):
        current = "7db8453b-95ae-401d-ba8d-8cb8c6088b8a"
        examples = {
            "koru_r1f_lifecycle": "旧订单还没结束",
            "koru_native_identity": "新功能要怎样与旧功能区分",
            "koru_candidate_scope": "允许看哪里",
            "koru_r1f_total_budget": "增加额度",
        }
        for key, title in examples.items():
            card = decision_view._d002_plain_help(2, current, key)
            self.assertIsNotNone(card)
            assert card is not None
            self.assertIn(title, card[0])
            self.assertIn("你", card[1])
            self.assertIn("负责人", card[2])
            self.assertIn(card[3], {"等你回答", "等负责人补证"})
            self.assertIsNone(decision_view._d002_plain_help(2, "changed-report", key))
            self.assertIsNone(decision_view._d002_plain_help(9, current, key))
        self.assertIsNone(decision_view._d002_plain_help(2, current, "unrelated"))

    async def test_incomplete_item_explains_owner_gaps_and_saved_is_unsent(self):
        spec = DecisionSpec.from_record(spec_record(
            key="old_order", question="旧单未结清时需要反向吗？",
            why="旧单20:00未成交，21:00出现反向信号。",
            owner="用户界定业务需求；经济owner核对规则",
            missing=["是否真的需要提前反转及容许的等待/失败", "取消确证、费用及重放规则", "原失败ref和四行样例"],
        ))
        run = self.attempt
        run.events = [encoded_event("partial", "尚需核对", source="executor", run_id=run.id, seq=1,
            report=Report(run.summary, run.checks, run.remaining, decisions=(spec,)))]
        self.store.save(self.board)  # The report exists before the separate human confirmation.
        prepare_decision_intent(self.board, preview_decisions(self.board, self.task.id, (Answer("request_info"),), Target("saved")))
        self.store.save(self.board)
        before = self.saved_snapshot()
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            item = app.query_one("#decision-items", OptionList).get_option_at_index(0)
            self.assertIn("已要求补齐 · 仅本地保存（未发送）", str(item.prompt))
            form = await self.sheet(app, pilot)
            self.assertIn("本项责任与后续", str(form.query_one("#decision-action-0", Static).content))
            self.assertIn("旧报告未明确受众", str(form.query_one("#decision-action-guide-0", Static).content))
            self.assertIn("用户界定业务需求；经济owner核对规则", str(form.query_one("#decision-owner-0", Static).content))
            self.assertIn("旧单20:00未成交", str(form.query_one("#decision-context-0", Static).content))
            self.assertIn('3 项', str(form.query_one('#decision-gaps-0', Static).content))
            for number, missing in enumerate(spec.missing, 1):
                self.assertEqual(str(form.query_one(f'#decision-gap-0-{number}', Static).content), f'{number}. {missing}')
                self.assertIn('原报告未逐项提供来源', str(form.query_one(f'#decision-need-source-0-{number}', Static).content))
            self.assertIn("仅本地保存，未发送", str(form.query_one("#decision-previous-0", Static).content))
            self.assertTrue(form.query_one("#decision-continue", Button).disabled)
            self.assertIn("未选问题不发送", str(form.query_one("#decision-flow", Static).content))
            self.assertEqual(form.query_one("#answer-0", Select).value, "undecided")
            form.query_one("#decision-explain-0", Button).scroll_visible(top=True, animate=False)
            await pilot.pause()
            self.assertTrue(await pilot.click("#decision-explain-0"))
            await pilot.pause()
            self.assertIn("未确认 · 待解释", str(form.query_one("#decision-overview", OptionList).get_option_at_index(0).prompt))
            self.assertFalse(form.query_one("#decision-continue", Button).disabled)
            self.assertIn("发送须最后核对", str(form.query_one("#decision-flow", Static).content))
            await pilot.press("escape")
            await wait_ui(pilot, lambda: isinstance(app.screen, DiscardChanges))
            await pilot.click("#discard-edit")
            await wait_ui(pilot, lambda: len(app.screen_stack) == 1)
            form = await self.sheet(app, pilot)
            self.assertIn("已存未发", str(form.query_one("#decision-overview", OptionList).get_option_at_index(0).prompt))
            self.assertEqual(encode(self.store.load()), before)
            self.assertFalse(list(self.runtime.root.glob("*/commands/*.json")))

    async def test_four_item_phone_sheet_keeps_action_and_submit_visible(self):
        first = DecisionSpec.from_record(spec_record(key="old_order",
            question="未结清旧单产生反向信号时是否要切换？",
            why="20:00旧单未成交，21:00出现反向信号，历史失败不应覆盖。",
            owner="用户确认业务需要；经济owner核对完整规则",
            missing=["明确反转场景和延迟/失败容忍", "取消确认、费用和重放规范", "四行样例与旧失败引用"]))
        specs = (first,) + tuple(DecisionSpec.from_record(spec_record(key=f"other-{n}",
            question=f"另一项需补材料{n}", missing=["原始依据待核对"])) for n in range(3))
        run = self.attempt
        run.events = [encoded_event("partial", "四项待补证", source="executor", run_id=run.id, seq=1,
            report=Report(run.summary, run.checks, run.remaining, decisions=specs))]
        self.store.save(self.board)
        app = self.app()
        async with app.run_test(size=(40, 20)) as pilot:
            form = await self.sheet(app, pilot)
            await pilot.pause()
            viewport = form.query_one("#decision-sheet").content_region
            self.assertTrue(viewport.contains_region(form.query_one("#decision-continue", Button).region),
                            "40-column sheet must expose the primary action without scrolling the whole dialog")
            self.assertTrue(form.query_one("#decision-continue", Button).disabled)
            self.assertTrue(form.query_one("#decision-other-reply-0", Collapsible).collapsed)
            form.query_one("#decision-owner-request-0", Button).scroll_visible(top=True, animate=False)
            await pilot.pause()
            frame = "\n".join(strip.text for strip in form._compositor.render_strips())
            self.assertIn("请负责人补证本项", frame)
            self.assertIn("核对本单", frame)
            self.assertTrue(viewport.contains_region(form.query_one("#decision-owner-request-0", Button).region))
            self.assertTrue(await pilot.click("#decision-owner-request-0"))
            await pilot.pause()
            self.assertEqual(form.query_one("#answer-0", Select).value, "request_info")
            self.assertFalse(form.query_one("#decision-continue", Button).disabled)
            details = form.query_one("#decision-details-0", Collapsible)
            self.assertTrue(details.collapsed)
            details.collapsed = False
            await pilot.pause()  # Expansion changes geometry; scroll only after layout settles.
            form.query_one("#decision-gap-0-2", Static).scroll_visible(top=True, animate=False)
            await pilot.pause()
            later = "\n".join(strip.text for strip in form._compositor.render_strips())
            self.assertIn("取消确认", later)
            review = await self.review(app, pilot, "continue")
            self.assertIn("仅补证请求", review.preview.compiled_text)
            self.assertTrue(review.query_one("#decision-review").content_region.contains_region(
                review.query_one("#decision-confirm", Button).region), "40-column review must keep final consent visible")
            self.assertIn("确认并发送补证请求", "\n".join(strip.text for strip in review._compositor.render_strips()))
            self.assertEqual(str(review.query_one("#decision-confirm", Button).label), "确认并发送补证请求")
            self.assertTrue(review.query_one("#decision-back", Button).has_focus)
            await pilot.press("enter")
            await wait_ui(pilot, lambda: app.screen is form)
            self.assertFalse(list(self.runtime.root.glob("*/commands/*.json")))

    async def test_two_option_comparison_is_a_responsive_field_table(self):
        spec = DecisionSpec.from_record(spec_record(missing=["业务条件待核对"],
            options=[{"key": "a", "label": "仅用新身份fixture检验现有有限SDK并保留旧失败结果", "benefit": "可逆", "cost": "一次核对", "risk": "不能证实反转", "scope": "隔离区"},
                     {"key": "b", "label": "另立取消终态及重规划契约待审", "benefit": "覆盖更多目标", "cost": "独立评审", "risk": "改变经济语义", "scope": "独立方案"}]))
        run = self.attempt
        run.events = [encoded_event("partial", "对照方案", source="executor", run_id=run.id, seq=1,
            report=Report(run.summary, run.checks, run.remaining, decisions=(spec,)))]
        self.store.save(self.board)
        before = self.saved_snapshot()
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            form = await self.sheet(app, pilot)
            wide = form.query_one("#comparison-wide-0", Static)
            self.assertTrue(wide.display)
            table_renderable = wide.content
            assert isinstance(table_renderable, Table)
            console = Console(width=72, color_system=None)
            with console.capture() as output:
                console.print(table_renderable)
            table = output.get()
            for value in ("旧失败结果", "待审", "收益", "代价", "风险", "范围", "可逆", "覆盖更多目标", "不能证实反转", "改变经济语义"):
                self.assertIn(value, table)
            self.assertTrue(any(all(value in line for value in ("收益", "可逆", "覆盖更多目标")) for line in table.splitlines()),
                            "same field must align both options on one row")
            self.assertTrue(table_renderable.show_lines)
            self.assertFalse(form.query_one("#comparison-compact-0-0", Static).display)
            await pilot.resize_terminal(40, 20)
            await pilot.pause()
            self.assertFalse(wide.display)
            for number, option in enumerate(spec.options):
                compact = form.query_one(f"#comparison-compact-0-{number}", Static)
                self.assertTrue(compact.display)
                self.assertIsInstance(compact.content, Table)
                console = Console(width=32, color_system=None)
                with console.capture() as output:
                    console.print(compact.content)
                table = output.get()
                for value in (option.label[-2:], option.benefit, option.cost, option.risk, option.scope):
                    self.assertIn(value, table)
            self.assertTrue(form.query_one("#decision-sheet").content_region.contains_region(
                form.query_one("#decision-continue", Button).region))
            self.assertEqual(form.query_one("#answer-0", Select).value, "undecided")
            self.assertEqual(encode(self.store.load()), before)
            self.assertFalse(list(self.runtime.root.glob("*/commands/*.json")))

    async def test_three_options_use_individually_readable_tables(self):
        options = [{"key": key, "label": label, "benefit": f"收益-{key}", "cost": f"代价-{key}",
                    "risk": f"风险-{key}", "scope": f"范围-{key}"} for key, label in
                   (("a", "方案 A"), ("b", "方案 B"), ("c", "方案 C"))]
        spec = DecisionSpec.from_record(spec_record(options=options))
        run = self.attempt
        run.events = [encoded_event("partial", "三项方案", source="executor", run_id=run.id, seq=1,
            report=Report(run.summary, run.checks, run.remaining, decisions=(spec,)))]
        self.store.save(self.board)
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            form = await self.sheet(app, pilot)
            self.assertFalse(form.query("#comparison-wide-0"))
            for width in (80, 40):
                if width == 40:
                    await pilot.resize_terminal(40, 20)
                    await pilot.pause()
                for number, option in enumerate(options):
                    widget = form.query_one(f"#comparison-compact-0-{number}", Static)
                    self.assertTrue(widget.display)
                    console = Console(width=32, color_system=None)
                    with console.capture() as output:
                        console.print(widget.content)
                    text = output.get()
                    self.assertIn(option["label"], text)
                    self.assertIn(option["risk"], text)
            self.assertEqual(form.query_one("#answer-0", Select).value, "undecided")
            self.assertFalse(list(self.runtime.root.glob("*/commands/*.json")))

    async def test_one_decision_has_scannable_sections_without_hiding_full_option_facts(self):
        spec = DecisionSpec.from_record(spec_record(missing=["用户确认目标", "owner补齐负例"],
            why="需要明确旧方案范围", evidence=["旧失败记录原文", "owner审阅记录"],
            options=[{"key": "a", "label": "路线 A", "benefit": "可逆", "risk": "不能证实真实反转", "cost": "一次核对", "scope": "仅隔离区"},
                     {"key": "b", "label": "路线 B", "benefit": "可覆盖更多目标", "risk": "改变经济语义", "cost": "重新评审", "scope": "独立方案"}]))
        run = self.attempt
        run.events = [encoded_event("partial", "材料尚未齐", source="executor", run_id=run.id, seq=1,
            report=Report(run.summary, run.checks, run.remaining, decisions=(spec,)))]
        self.store.save(self.board)
        before = self.saved_snapshot()
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            form = await self.sheet(app, pilot)
            item = form.query_one("#decision-item-0")
            sections = list(item.query(".decision-section-heading").results(Static))
            self.assertEqual([str(section.content).split(" · ")[0] for section in sections],
                ["待处理事项（旧报告）", "本项责任与后续", "你的回应", "本次答复类型", "核对边界与建议", "方案对照"])
            table = item.query_one("#comparison-wide-0", Static)
            self.assertIsInstance(table.content, Table)
            console = Console(width=72, color_system=None)
            with console.capture() as output:
                console.print(table.content)
            comparison = output.get()
            for text in ("路线 A", "可逆", "不能证实真实反转", "一次核对", "仅隔离区",
                         "路线 B", "可覆盖更多目标", "改变经济语义", "重新评审", "独立方案"):
                self.assertIn(text, comparison)
            headings = [section.styles.background for section in sections]
            self.assertTrue(all(color is not None for color in headings))
            self.assertTrue(item.query_one("#decision-other-reply-0", Collapsible).collapsed)
            self.assertIn("待处理事项", "\n".join(strip.text for strip in app.screen._compositor.render_strips()))
            self.assertEqual(form.query_one("#answer-0", Select).value, "undecided")
            evidence = item.query_one("#decision-details-0", Collapsible)
            self.assertTrue(evidence.collapsed)
            form.query_one("#decision-gap-0-1", Static).scroll_visible(top=True, animate=False)
            await pilot.pause()
            self.assertIn("用户确认目标", "\n".join(strip.text for strip in app.screen._compositor.render_strips()))
            self.assertTrue(evidence.collapsed, 'Gaps must be readable without expanding technical background')
            evidence.collapsed = False
            await pilot.pause()
            original = evidence.query_one(Collapsible)
            original.collapsed = False
            await pilot.pause()
            self.assertTrue(any("owner审阅记录" in str(w.content) and "完整决策责任：用户" in str(w.content)
                                for w in original.query("Static").results(Static)))
            self.assertEqual(encode(self.store.load()), before)
            self.assertFalse(list(self.runtime.root.glob("*/commands/*.json")))

    async def test_plain_help_marks_each_item_live_without_persisting(self):
        specs = (DecisionSpec.from_record(spec_record(key="first", missing=["用户要求不明确"])),
                 DecisionSpec.from_record(spec_record(key="second", missing=["证据待核对"])))
        run = self.attempt
        run.events = [encoded_event("partial", "两项需解释", source="executor", run_id=run.id, seq=1,
            report=Report(run.summary, run.checks, run.remaining, decisions=specs))]
        self.store.save(self.board)
        before = self.saved_snapshot()
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            form = await self.sheet(app, pilot)
            overview = form.query_one("#decision-overview", OptionList)
            self.assertIn("待补证", str(overview.get_option_at_index(0).prompt))
            self.assertIn("待补证", str(overview.get_option_at_index(1).prompt))
            self.assertIn("旧报告未明确受众", str(form.query_one("#decision-action-guide-0", Static).content))
            form.query_one("#decision-explain-0", Button).scroll_visible(top=True, animate=False)
            await pilot.pause()
            self.assertTrue(await pilot.click("#decision-explain-0"))
            await wait_ui(pilot, lambda: form.query_one("#answer-0", Select).value == "request_info")
            self.assertIn("未确认 · 待解释", str(overview.get_option_at_index(0).prompt))
            self.assertIn("未确认", str(form.query_one("#decision-live-0", Static).content))
            self.assertIn("待补证", str(overview.get_option_at_index(1).prompt))
            select_feedback_if_blank(form, 0)
            form.query_one("#comment-0", TextArea).text = "请用普通话说我需要回答什么"
            await pilot.pause()
            self.assertIn("未确认 · 待解释", str(overview.get_option_at_index(0).prompt))
            overview.highlighted = 1
            overview.action_select()
            await wait_ui(pilot, lambda: form.selected_index == 1)
            select_feedback_if_blank(form, 1)
            form.query_one("#comment-1", TextArea).text = "我只知道想避免失败"
            await pilot.pause()
            self.assertIn("未确认 · 已写反馈（非授权）", str(overview.get_option_at_index(1).prompt))
            select_intent(form, 1, "feedback")
            form.query_one("#answer-1", Select).value = "preference:a"
            await pilot.pause()
            self.assertIn("未确认 · 倾向（非授权）", str(overview.get_option_at_index(1).prompt))
            overview.highlighted = 0
            overview.action_select()
            await wait_ui(pilot, lambda: form.selected_index == 0)
            self.assertEqual(form.query_one("#answer-0", Select).value, "request_info")
            self.assertEqual(overview.highlighted, 0)
            review = await self.review(app, pilot, "continue")
            self.assertIn("用户需要回答什么", review.preview.compiled_text)
            self.assertIn("仅补证请求", review.preview.compiled_text)
            self.assertTrue(review.query_one("#decision-back", Button).has_focus)
            self.assertFalse(list(self.runtime.root.glob("*/commands/*.json")))
            await pilot.press("enter")
            await wait_ui(pilot, lambda: app.screen is form)
            self.assertIn("未确认 · 待解释", str(overview.get_option_at_index(0).prompt))
            await pilot.press("escape")
            await wait_ui(pilot, lambda: isinstance(app.screen, DiscardChanges))
            await pilot.click("#discard-edit")
            await wait_ui(pilot, lambda: len(app.screen_stack) == 1)
            self.assertEqual(encode(self.store.load()), before)
            self.assertFalse(list(self.runtime.root.glob("*/commands/*.json")))

    async def test_focused_detail_switches_items_with_real_arrows_and_mouse(self):
        specs = tuple(DecisionSpec.from_record(spec_record(key=key, question=label))
                      for key, label in (("first", "第一个问题"), ("second", "第二个问题"), ("third", "第三个问题")))
        run = self.attempt
        run.events = [encoded_event("partial", "三个待核对问题", source="executor", run_id=run.id, seq=1,
            report=Report(run.summary, run.checks, run.remaining, decisions=specs))]
        self.store.save(self.board)
        before = self.saved_snapshot()
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            app.action_decisions("first")  # The main-page row opens the detail on this key.
            await wait_ui(pilot, lambda: isinstance(app.screen, DecisionSheet))
            form = app.screen
            assert isinstance(form, DecisionSheet)
            overview = form.query_one("#decision-overview", OptionList)
            self.assertEqual(form.selected_index, 0)
            self.assertTrue(form.query_one("#decision-item-0").display)
            await pilot.press("down")
            await pilot.pause()
            keyboard_switched = form.selected_index == 1 and form.query_one("#decision-item-1").display
            # Text now wraps; target the native row-line map rather than assuming one line per item.
            row_y = overview._index_to_line[2] - int(overview.scroll_y)
            self.assertTrue(await pilot.click("#decision-overview", offset=(2, row_y)))
            await pilot.pause()
            mouse_switched = form.selected_index == 2 and form.query_one("#decision-item-2").display
            self.assertTrue(keyboard_switched, f"down did not switch: focus={getattr(app.focused, 'id', None)}")
            self.assertTrue(mouse_switched, f"click did not switch: focus={getattr(app.focused, 'id', None)}")
            self.assertTrue(overview.has_focus)
            select_intent(form, 2, "feedback")
            form.query_one("#answer-2", Select).value = "preference:a"
            await pilot.press("up")
            await wait_ui(pilot, lambda: form.selected_index == 1)
            await pilot.press("down")
            await wait_ui(pilot, lambda: form.selected_index == 2)
            self.assertEqual(form.query_one("#answer-2", Select).value, "preference:a")
            self.assertEqual(encode(self.store.load()), before)
            self.assertFalse(list(self.runtime.root.glob("*/commands/*.json")))

    async def test_major_section_spacing_only_when_dialog_has_room(self):
        before = self.saved_snapshot()
        for size, spaced in (((80, 24), False), ((180, 50), True)):
            with self.subTest(size=size):
                app = self.app()
                async with app.run_test(size=size) as pilot:
                    form = await self.sheet(app, pilot)
                    await pilot.pause()
                    self.assertEqual(form.has_class("decision-spaced"), spaced)
                    headings = list(form.query_one("#decision-item-0").query(".decision-section-heading").results(Static))
                    self.assertEqual(len(headings), 6)
                    self.assertEqual(headings[0].styles.margin.top, 0)
                    self.assertEqual(headings[1].styles.margin.top, 1 if spaced else 0)
                    self.assertEqual(form.query_one("#answer-0", Select).value, "undecided")
        self.assertEqual(encode(self.store.load()), before)
        self.assertFalse(list(self.runtime.root.glob("*/commands/*.json")))

    async def test_four_item_overview_preserves_non_authorizing_input_and_unknown_budget(self):
        specs = tuple(DecisionSpec.from_record(spec_record(kind, key=key, question=question, missing=["原始依据待核对", "缺口责任人未逐项标注"],
            budget={"attempts": 0, "minutes": 0, "storage_mib": 0, "stop_conditions": ["出错停止"]} if kind == "budget" else None))
            for kind, key, question in (("choice", "goal", "选择目标范围"), ("choice", "identity", "确认接口身份"),
                                        ("choice", "scope", "明确交付写集"), ("budget", "limits", "核对总预算")))
        run = self.attempt
        run.events = [encoded_event("partial", "待逐项答复", source="executor", run_id=run.id, seq=1,
            report=Report(run.summary, run.checks, run.remaining, decisions=specs))]
        self.store.save(self.board)
        before = self.saved_snapshot()
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            form = await self.sheet(app, pilot)
            summary = form.query_one("#decision-overview", OptionList)
            self.assertEqual(summary.option_count, 4)
            self.assertTrue(form.query_one("#decision-item-0").display)
            self.assertFalse(form.query_one("#decision-item-3").display)
            self.assertIn("缺口 2 项", str(summary.get_option_at_index(0).prompt))
            self.assertIn("核对本单", str(form.query_one("#decision-continue", Button).label))
            select_intent(form, 0, "feedback")
            form.query_one("#answer-0", Select).value = "preference:a"
            select_feedback_if_blank(form, 0)
            form.query_one("#comment-0", TextArea).text = "先讨论 A，绝非试验授权"
            summary.highlighted = 3
            summary.action_select()
            await wait_ui(pilot, lambda: form.query_one("#decision-item-3").display)
            self.assertFalse(form.query_one("#decision-item-0").display)
            self.assertEqual(form.query_one("#answer-0", Select).value, "preference:a")
            self.assertIn("原上限：未知", str(form.query_one("#budget-facts-3", Static).content))
            self.assertIn("累计已用：未知", str(form.query_one("#budget-facts-3", Static).content))
            self.assertIn("建议新上限（未采纳）：0", str(form.query_one("#budget-facts-3", Static).content))
            self.assertFalse(form.query("#budget-3-attempts"), "缺账本时不能显示正式预算批准输入")
            full = form.query_one("#decision-details-3", Collapsible)
            self.assertTrue(full.collapsed)
            full.collapsed = False
            await pilot.pause()
            self.assertTrue(any("方案 A/B 已说明成本" in str(widget.content)
                                for widget in full.query("Static").results(Static)))
            review = await self.review(app, pilot, "continue")
            self.assertIn("仅补证请求", review.preview.compiled_text)
            self.assertIn("偏好，不是授权", review.preview.compiled_text)
            self.assertIn("确认并发送补证请求", str(review.query_one("#decision-confirm", Button).label))
            await pilot.press("escape")
            self.assertEqual(encode(self.store.load()), before)
            self.assertFalse(list(self.runtime.root.glob("*/commands/*.json")))

    async def test_incomplete_request_needs_explicit_non_authorizing_answer_and_one_dispatch(self):
        spec = DecisionSpec.from_record(spec_record(missing=["原始依据待核对"]))
        run = self.attempt
        run.events = [encoded_event("partial", "只待补证", source="executor", run_id=run.id, seq=1,
            report=Report(run.summary, run.checks, run.remaining, decisions=(spec,)))]
        self.store.save(self.board)
        before = self.saved_snapshot()
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            form = await self.sheet(app, pilot)
            form.request("continue")
            await wait_ui(pilot, lambda: "补证请求" in str(form.query_one("#decision-error", Static).content))
            await wait_ui(pilot, lambda: not form.busy)
            self.assertTrue(form.query_one("#decision-continue", Button).disabled)
            self.assertEqual(encode(self.store.load()), before)
            self.assertFalse(list(self.runtime.root.glob("*/commands/*.json")))
            select_intent(form, 0, "feedback")
            form.query_one("#answer-0", Select).value = "preference:a"
            await pilot.pause()
            review = await self.review(app, pilot, "continue")
            self.assertIn("仅补证请求", review.preview.compiled_text)
            self.assertIn("确认并发送补证请求", str(review.query_one("#decision-confirm", Button).label))
            self.assertEqual(encode(self.store.load()), before)
            await self.confirmed(app, pilot)
            self.assertEqual(len(app.board.runs), 2)
            self.assertEqual(len(app.board.decisions), 1)
            self.assertEqual(len(list(self.runtime.root.glob("*/commands/*.json"))), 1)
            record = next(iter(app.board.decisions.values()))
            self.assertEqual(record.preview.answers[0].kind, "preference")
            self.assertEqual(record.intent.kind, "new_run")
            new_run = app.board.runs[record.intent.run_id]
            self.assertEqual(new_run.requirements, record.preview.compiled_text)
            self.assertIn("仅补证请求", new_run.requirements)
            self.assertNotIn("历史要求", new_run.requirements)
            self.assertEqual(encode(self.store.load()), encode(app.board))

    async def test_missing_material_disallows_positive_option_and_budget_has_no_default(
        self,
    ):
        specs = (
            DecisionSpec.from_record(spec_record(missing=["没有对照结果"])),
            DecisionSpec.from_record(spec_record("budget", key="limits")),
        )
        self.attempt.events = [
            encoded_event(
                "partial",
                "report",
                source="executor",
                run_id=self.attempt.id,
                seq=1,
                report=Report(
                    self.attempt.summary,
                    self.attempt.checks,
                    self.attempt.remaining,
                    decisions=specs,
                ),
            )
        ]
        self.store.save(self.board)
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            form = await self.sheet(app, pilot)
            with self.assertRaises(InvalidSelectValueError):
                select_intent(form, 0, "formal")
                form.query_one("#answer-0", Select).value = "choice:a"
            self.assertFalse(form.query("#budget-1-attempts"), "累计已用未知时不开放预算批准输入")
            with self.assertRaises(InvalidSelectValueError):
                select_intent(form, 1, "formal")
                form.query_one("#answer-1", Select).value = "budget"
            self.assertIn("累计已用：未知", str(form.query_one("#budget-facts-1", Static).content))
            self.assertFalse(app.board.decisions)

    async def test_legacy_report_requests_materials_not_synthetic_approvals(self):
        self.attempt.events = [
            encoded_event(
                "partial",
                "legacy",
                source="executor",
                run_id=self.attempt.id,
                seq=1,
                report=Report(
                    self.attempt.summary, self.attempt.checks, self.attempt.remaining
                ),
            )
        ]
        self.store.save(self.board)
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            app.action_decisions()
            await wait_ui(pilot, lambda: isinstance(app.screen, Confirm))
            assert isinstance(app.screen, Confirm)
            self.assertIn("不批准研究", app.screen.body)
            await pilot.click("#submit")
            await wait_ui(pilot, lambda: isinstance(app.screen, DispatchNote))
            assert isinstance(app.screen, DispatchNote)
            self.assertIn("decisions", app.screen.initial)
            self.assertIn("不授权新的", app.screen.initial)
            self.assertFalse(app.board.decisions)
            self.assertFalse(list(self.runtime.root.glob("*/commands/*.json")))

    def use_live_board(self):
        self.store = self.enterContext(Store(self.root / "live.sqlite3"))
        self.store.load()
        self.board, self.task, self.attempt = decision_board(live=True)
        self.board.projects[self.task.project] = self.attempt.cwd = str(self.project)
        self.store.save(self.board)
        self.agent = replace(
            self.agent,
            id=self.attempt.agent_id,
            session_id=self.attempt.session_id,
            idle=False,
            active_run=self.attempt.id,
        )
        self.publish_agent(self.agent)
        endpoint = self.runtime.root / self.agent.id
        for folder in (endpoint / "supplements", endpoint / "supplements/requests"):
            private_dir(folder, create=True)
        publish(
            endpoint / "capabilities.json",
            {
                "version": 1,
                "agent_id": self.agent.id,
                "session_id": self.agent.session_id,
                "cwd": self.agent.cwd,
                "supplement": 1,
            },
        )

    async def test_progress_entry_requires_real_material_and_d_opens_without_execution(
        self,
    ):
        self.use_live_board()
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            self.assertTrue(app.query_one("#decision-status", Static).display)
            self.assertEqual(
                str(app.query_one("#return-button", Button).label), "决定后继续"
            )
            self.assertTrue(app.query_one("#return-button", Button).disabled)
            await pilot.press("d")
            await wait_ui(pilot, lambda: isinstance(app.screen, DecisionSheet))
            await pilot.press("escape")
            run = app.board.runs[self.attempt.id]
            run.events = [
                encoded_event(
                    "progress",
                    "ordinary progress",
                    source="executor",
                    run_id=run.id,
                    seq=run.seq,
                    report=Report(run.summary, run.checks, run.remaining),
                )
            ]
            app.refresh_tasks()
            await pilot.pause()
            self.assertFalse(app.query_one("#decision-status", Static).display)
            self.assertIn("暂不需要你操作", str(app.query_one("#next-action", Static).content))
            self.assertFalse(app.query_one("#context-hint", Static).display)
            self.assertEqual(
                str(app.query_one("#return-button", Button).label), "补充说明 b"
            )
            self.assertFalse(app.board.decisions)
            self.assertFalse(list(self.runtime.root.glob("*/commands/*.json")))

    async def test_pi_confirmation_is_a_status_not_an_executable_next_step(self):
        self.use_live_board()
        before = self.saved_snapshot()
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("right")
            app.board.runs[self.attempt.id].state = "waiting"
            app.refresh_tasks()
            await pilot.pause()
            primary = app.query_one("#accept-button", Button)
            self.assertEqual(str(primary.label), "查看状态")
            self.assertEqual(primary.variant, "default")
            self.assertFalse(app.query_one("#return-button", Button).display)
            self.assertIn("原 Pi", str(app.query_one("#next-action", Static).content))
            self.assertIn("到原 Pi", app.card(app.board.tasks[self.task.id]).plain)
            self.assertTrue(app.query_one("#context-hint", Static).display)
            self.assertTrue(await pilot.click("#accept-button"))
            self.assertIsInstance(app.screen, ReportReader)
            await pilot.press("escape")
            self.assertFalse(list(self.runtime.root.glob("*/commands/*.json")))
        self.assertEqual(encode(self.store.load()), before)

    async def test_live_decision_commits_before_one_exact_supplement_without_new_run(
        self,
    ):
        self.use_live_board()
        app = self.app()
        calls = []
        publish_note = self.runtime.send_supplement

        def delivered(run, note):
            with Store(self.store.path) as observer:
                persisted = observer.load()
            record = next(iter(persisted.decisions.values()))
            self.assertEqual(record.intent.supplement_id, note.id)
            self.assertEqual(record.preview.compiled_text, note.text)
            self.assertEqual(persisted.supplements[note.id], note)
            calls.append(note.id)
            publish_note(run, note)

        with patch.object(self.runtime, "send_supplement", side_effect=delivered):
            async with app.run_test(size=(80, 24)) as pilot:
                form = await self.sheet(app, pilot)
                select_intent(form, 0, "formal")
                form.query_one("#answer-0", Select).value = "choice:b"
                review = await self.review(app, pilot, "continue")
                self.assertEqual(review.preview.target.kind, "live_supplement")
                await self.confirmed(app, pilot)
                self.assertEqual(len(app.board.runs), 1)
                self.assertEqual(len(calls), 1, app.supplement_errors)
                requests = list(self.runtime.root.glob("*/supplements/requests/*.json"))
                self.assertEqual(len(requests), 1)
                self.assertEqual(
                    json.loads(requests[0].read_text())["text"],
                    review.preview.compiled_text,
                )
                self.assertFalse(list(self.runtime.root.glob("*/commands/*.json")))

    async def test_late_live_preflight_after_review_closed_must_not_commit(self):
        self.use_live_board()
        app = self.app()
        entered, release = Event(), Event()
        check = self.runtime.check_supplement

        def delayed(run):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test preflight release timed out")
            check(run)

        async with app.run_test(size=(80, 24)) as pilot:
            form = await self.sheet(app, pilot)
            select_intent(form, 0, "formal")
            form.query_one("#answer-0", Select).value = "choice:a"
            review = await self.review(app, pilot, "continue")
            before = encode(app.board)
            with patch.object(self.runtime, "check_supplement", side_effect=delayed):
                try:
                    await pilot.click("#decision-confirm")
                    self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                    app.pop_screen()
                finally:
                    release.set()
                await wait_ui(pilot, lambda: not review.busy)
            self.assertEqual(encode(app.board), before)
            self.assertFalse(
                list(self.runtime.root.glob("*/supplements/requests/*.json"))
            )

    def offline_saved_feedback(self):
        spec = DecisionSpec.from_record(spec_record(missing=["负责人原始回执缺失"]))
        run = self.attempt
        run.events = [encoded_event("partial", "仍缺原始回执", source="executor", run_id=run.id, seq=1,
            report=Report(run.summary, run.checks, run.remaining, decisions=(spec,)))]
        self.store.save(self.board)
        prepare_decision_intent(self.board, preview_decisions(self.board, self.task.id,
            (Answer(comment="是"),), Target("saved")))
        self.store.save(self.board)
        old = replace(self.agent, online=False, idle=False, heartbeat=int(time.time() * 1000) - 60000)
        (self.runtime.root / old.id / "agent.json").unlink()  # Scratch mailbox already had the online fixture.
        candidate = replace(self.agent, id=str(uuid4()), session_id=str(uuid4()), name="同目录可用 Pi")
        self.publish_agent(old)
        self.publish_agent(candidate)
        session = {"version": 1, "agent_id": old.id, "session_id": old.session_id, "cwd": old.cwd,
                   "seq": 1, "observed_at": int(time.time() * 1000) - 60000, "state": "offline",
                   "run_id": run.id, "linked_run_id": "", "following": False, "input_pending": False}
        (self.runtime.root / old.id / "session.json").unlink()
        publish(self.runtime.root / old.id / "session.json", session)
        batch = self.runtime.read_updates([deepcopy(run)], [], None)
        issue = batch.errors[run.id]
        self.assertIn("离线/心跳过期", issue)
        app = Waggle(store=self.store, runtime=self.runtime)
        app.agents[old.id] = old
        app.runtime_errors[run.id] = issue
        self.enterContext(patch.object(app, "schedule_poll"))  # No fake poll racing with user intent.
        return app, candidate, session

    async def test_terminal_offline_source_can_save_but_cannot_choose_or_dispatch(self):
        app, _, _ = self.offline_saved_feedback()
        before = self.saved_snapshot()
        async with app.run_test(size=(80, 24)) as pilot:
            form = await self.sheet(app, pilot)
            self.assertTrue(form.has_saved_feedback)
            self.assertEqual(form.query_one("#comment-0", TextArea).text, "是")
            await pilot.click("#decision-continue")
            await wait_ui(pilot, lambda: "未提交" in str(form.query_one("#decision-error", Static).content))
            self.assertIs(app.screen, form)
            self.assertEqual(encode(self.store.load()), before)
            self.assertEqual(len(app.board.runs), 1)
            self.assertFalse(list(self.runtime.root.glob("*/commands/*.json")))
            self.assertEqual(form.query_one("#comment-0", TextArea).text, "是")

    async def test_source_session_changed_after_review_rejects_before_any_new_intent(self):
        app, _, _ = self.offline_saved_feedback()
        # The pre-review source is explicitly restored only inside this disposable fixture.
        agent = replace(self.agent, online=True, idle=True, heartbeat=int(time.time() * 1000))
        (self.runtime.root / agent.id / "agent.json").unlink()
        self.publish_agent(agent)
        app.agents[agent.id] = agent
        app.runtime_errors.clear()
        before = self.saved_snapshot()
        async with app.run_test(size=(80, 24)) as pilot:
            await self.sheet(app, pilot)
            review = await self.review(app, pilot, "continue")
            path = self.runtime.root / agent.id / "session.json"
            session = json.loads(path.read_text())
            path.unlink()
            publish(path, session | {"input_pending": True, "seq": 2})
            await pilot.click("#decision-confirm")
            await wait_ui(pilot, lambda: "未提交" in str(review.query_one("#decision-error", Static).content))
            self.assertEqual(encode(self.store.load()), before)
            self.assertFalse(list(self.runtime.root.glob("*/commands/*.json")))

    async def test_offline_old_pi_with_unverified_successor_edge_cannot_choose_or_dispatch(self):
        app, _, _ = self.offline_saved_feedback()
        before = self.saved_snapshot()
        publish(self.runtime.root / self.agent.id / "runs" / f"{self.attempt.id}.next.json", {"version": 1})
        async with app.run_test(size=(80, 24)) as pilot:
            form = await self.sheet(app, pilot)
            await pilot.click("#decision-continue")
            await wait_ui(pilot, lambda form=form: "未提交" in str(form.query_one("#decision-error", Static).content))
            self.assertIs(app.screen, form)
            self.assertEqual(encode(self.store.load()), before)
            self.assertFalse(list(self.runtime.root.glob("*/commands/*.json")))

    async def test_terminal_offline_exception_never_ignores_another_error_or_ambiguous_session(self):
        app, _, session = self.offline_saved_feedback()
        before = self.saved_snapshot()
        for label, change in (("unrelated issue", None), ("input pending", {"input_pending": True}),
                              ("new linked run", {"following": True, "linked_run_id": str(uuid4())})):
            with self.subTest(label=label):
                (self.runtime.root / self.agent.id / "session.json").unlink()  # Reset only the scratch snapshot.
                if change:
                    publish(self.runtime.root / self.agent.id / "session.json", session | change)
                else:
                    publish(self.runtime.root / self.agent.id / "session.json", session)
                current = Waggle(store=self.store, runtime=self.runtime)
                current.agents[self.agent.id] = replace(self.agent, online=False, heartbeat=int(time.time() * 1000) - 60000)
                current.runtime_errors[self.attempt.id] = (
                    "执行回执身份不可核验" if change is None else app.runtime_errors[self.attempt.id])
                with patch.object(current, "schedule_poll"):
                    async with current.run_test(size=(80, 24)) as pilot:
                        form = await self.sheet(current, pilot)
                        await pilot.click("#decision-continue")
                        await wait_ui(pilot, lambda form=form: "未提交" in str(form.query_one("#decision-error", Static).content))
                        self.assertIs(current.screen, form)
                        self.assertEqual(encode(self.store.load()), before)
                        self.assertFalse(list(self.runtime.root.glob("*/commands/*.json")))

    async def test_missing_agent_cancel_and_selection_do_not_assign_before_confirmation(
        self,
    ):
        self.task.agent = str(uuid4())  # Unavailable NEW target; original source Pi stays fresh, bound and idle.
        self.store.save(self.board)
        candidate = replace(
            self.agent, id=str(uuid4()), session_id=str(uuid4()), name="新候选者"
        )
        self.publish_agent(candidate)
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            form = await self.sheet(app, pilot)
            select_intent(form, 0, "formal")
            form.query_one("#answer-0", Select).value = "choice:b"
            before = encode(app.board)
            await pilot.click("#decision-continue")
            await wait_ui(pilot, lambda: isinstance(app.screen, Choice))
            await pilot.press("escape")
            await wait_ui(pilot, lambda: app.screen is form and not form.busy)
            self.assertEqual(encode(app.board), before)
            self.assertEqual(form.query_one("#answer-0", Select).value, "choice:b")
            await pilot.click("#decision-continue")
            await wait_ui(pilot, lambda: isinstance(app.screen, Choice))
            app.screen.query_one("#choice-query", Input).value = candidate.name
            await pilot.pause()
            await pilot.press("enter")
            await wait_ui(pilot, lambda: isinstance(app.screen, DecisionReview))
            assert isinstance(app.screen, DecisionReview)
            self.assertEqual(app.screen.preview.target.agent_id, candidate.id)
            self.assertEqual(encode(app.board), before)
            await self.confirmed(app, pilot)
            self.assertEqual(app.board.tasks[self.task.id].agent, candidate.id)
            self.assertEqual(len(app.board.decisions), 1)
            self.assertEqual(len(list(self.runtime.root.glob("*/commands/*.json"))), 1)

    async def test_new_agent_connects_then_returns_to_decision_review_without_dispatch(
        self,
    ):
        self.task.agent = str(uuid4())  # Missing new target does not erase original-session evidence.
        self.store.save(self.board)

        def create(runtime, directory, name, model, identity):
            launch = prepare_launch(runtime, directory, name, model, identity)
            self.publish_agent(
                replace(
                    self.agent, id=launch.id, session_id=launch.session_id, name=name
                )
            )
            launch.state, launch.tab_id, launch.pane_id = (
                "starting",
                "fixture-tab",
                "fixture-pane",
            )
            return launch

        app = self.app()
        with patch("app.create_pi", side_effect=create) as creation:
            async with app.run_test(size=(80, 24)) as pilot:
                form = await self.sheet(app, pilot)
                select_intent(form, 0, "formal")
                form.query_one("#answer-0", Select).value = "choice:a"
                before = encode(app.board)
                await pilot.click("#decision-continue")
                await wait_ui(pilot, lambda: isinstance(app.screen, Choice))
                app.screen.query_one("#choice-query", Input).value = "新建 Pi"
                await pilot.pause()
                await pilot.press("enter")
                await wait_ui(
                    pilot,
                    lambda: (
                        isinstance(app.screen, NewAgentForm)
                        and bool(app.screen.query("#submit"))
                    ),
                )
                new_agent = app.screen
                assert isinstance(new_agent, NewAgentForm)
                self.assertFalse(new_agent.query("#create-start"))
                self.assertTrue(await pilot.click("#submit"))
                await wait_ui(pilot, lambda: new_agent.connected)
                self.assertEqual(encode(app.board), before)
                self.assertFalse(list(self.runtime.root.glob("*/commands/*.json")))
                await pilot.pause(new_agent.query_one("#submit", Button).active_effect_duration)
                self.assertTrue(await pilot.click("#submit"))
                try:
                    await wait_ui(pilot, lambda: isinstance(app.screen, DecisionReview))
                except TimeoutError:
                    self.fail(f"new agent did not return to review: screen={app.screen}, "
                              f"form error={new_agent.query_one('#form-error', Static).content}, "
                              f"sheet error={form.query_one('#decision-error', Static).content}")
                self.assertEqual(encode(app.board), before)
                self.assertFalse(list(self.runtime.root.glob("*/commands/*.json")))
                creation.assert_called_once()
                await self.confirmed(app, pilot)
                self.assertEqual(len(app.board.runs), 2)
                self.assertEqual(len(app.board.decisions), 1)
                self.assertEqual(
                    len(list(self.runtime.root.glob("*/commands/*.json"))), 1
                )

    async def test_resolved_report_opens_readonly_history_instead_of_requesting_new_decisions(
        self,
    ):
        self.seed_fixture()
        prepare_decision_intent(
            self.board,
            preview_decisions(
                self.board, self.task.id, (Answer("choice", "a"),), Target("saved")
            ),
        )
        self.store.save(self.board)
        target = Target("new_run", self.agent.id, self.agent.session_id, self.agent.cwd)
        prepared = prepare_decision_intent(
            self.board, preview_decisions(self.board, self.task.id, (Answer("choice", "a"),), target)
        )
        self.store.save(self.board)
        assert prepared.run is not None
        final = deepcopy(prepared.run)
        final.state, final.delivery, final.seq = "finished", "full", 1
        final.summary, final.checks, final.remaining = (
            "已完成已选路线",
            ["隔离核对"],
            "",
        )
        final.events = [
            encoded_event(
                "full",
                "resolved",
                source="executor",
                run_id=final.id,
                seq=1,
                report=Report(final.summary, final.checks, ""),
            )
        ]
        self.board.ingest(final)
        self.store.save(self.board)
        before = self.saved_snapshot()
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            self.assertFalse(app.query_one("#decision-status", Static).display)
            await pilot.press("d")
            await wait_ui(pilot, lambda: isinstance(app.screen, Choice))
            await pilot.press("enter")
            await wait_ui(pilot, lambda: isinstance(app.screen, Confirm))
            assert isinstance(app.screen, Confirm)
            self.assertIn("历史只读", app.screen.body)
            self.assertIn("不等于收到、执行或完成", app.screen.body)
            self.assertEqual(encode(self.store.load()), before)
            self.assertFalse(list(self.runtime.root.glob("*/commands/*.json")))

    async def test_partial_without_new_decision_items_shows_only_history_not_a_new_decision_gate(self):
        self.seed_fixture()
        prepare_decision_intent(self.board, preview_decisions(self.board, self.task.id,
            (Answer("choice", "a"),), Target("saved")))
        self.store.save(self.board)
        result = prepare_decision_intent(self.board, preview_decisions(self.board, self.task.id,
            (Answer("choice", "a"),), Target("new_run", self.agent.id, self.agent.session_id, self.agent.cwd)))
        self.store.save(self.board)
        assert result.run is not None
        incoming = deepcopy(result.run)
        incoming.state, incoming.delivery, incoming.seq = "finished", "partial", 1
        incoming.summary, incoming.checks, incoming.remaining = "只读结果已核", ["已有依据"], "尚有待核范围"
        incoming.events = [encoded_event("partial", "本轮无新决策单", source="executor", run_id=incoming.id, seq=1,
            report=Report(incoming.summary, incoming.checks, incoming.remaining))]
        self.board.ingest(incoming)
        self.store.save(self.board)
        before = self.saved_snapshot()
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            self.assertFalse(app.query_one("#decision-status", Static).display)
            await pilot.press("d")
            await wait_ui(pilot, lambda: isinstance(app.screen, Choice))
            await pilot.press("enter")
            await wait_ui(pilot, lambda: isinstance(app.screen, Confirm))
            assert isinstance(app.screen, Confirm)
            self.assertIn("历史只读", app.screen.body)
            self.assertEqual(encode(self.store.load()), before)
            self.assertFalse(list(self.runtime.root.glob("*/commands/*.json")))

    async def test_hidden_task_decisions_are_readonly(self):
        self.seed_fixture()
        self.board.retire(self.task, "archived")
        self.store.save(self.board)
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            app.status_filter = "archived"
            app.refresh_tasks()
            form = await self.sheet(app, pilot)
            self.assertTrue(form.readonly)
            self.assertTrue(form.query_one("#decision-saved", Button).disabled)
            form.request("saved")
            self.assertFalse(app.board.decisions)


if __name__ == "__main__":
    unittest.main()
