"""Existing scratch UI seam: readable limits and truthful submission states."""
import re
import threading
import time
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from textual.containers import VerticalScroll
from textual.widgets import Button, Input, Select, SelectionList, Static, TextArea

import delivery
import test_supplement_start as fixtures
import test_supplements_ui as supplement_fixtures
import test_task_context as context_fixtures
from app import Choice, Confirm, DispatchNote, Note, RequestReview, Waggle
from decision_view import DecisionSheet
from decisions import (
    Answer,
    BusinessAnswer,
    DecisionSpec,
    Target,
    prepare_decision_intent,
    preview_decisions,
    question_requirements,
    review_requirements,
)
from journal import Report, current_report, encoded_event, parse_event
from runtime import BridgeError, Runtime
from store import Store
from task_context import create_followup_context
from test_decisions import spec_record


class DialogExperienceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = fixtures.SupplementStartTests()
        fixture = self.fixture
        fixture.root = Path(self.enterContext(TemporaryDirectory(prefix="waggle-dialog-experience-")))
        fixture.project = fixture.root / "中文 project"
        fixture.project.mkdir()
        fixture.runtime = Runtime(fixture.root / "bridges")
        fixture.store = self.enterContext(Store(fixture.root / "tasks.sqlite3"))
        fixture.board = fixture.store.load()
        fixture.board.add_project("项目", str(fixture.project))
        fixture.task = fixture.board.create("核对范围后继续", "项目", "原始说明；不能扩大工作目录。")
        fixture.store.save(fixture.board)

    async def open_question(self, app, pilot):
        await pilot.press("b")
        await fixtures.wait_ui(pilot, lambda: isinstance(app.screen, Choice))
        await pilot.press("enter")
        await fixtures.wait_ui(pilot, lambda: isinstance(app.screen, DispatchNote))
        form = app.screen
        assert isinstance(form, DispatchNote)
        return form

    def compiled_count(self, form):
        label = str(form.query_one("#note-length", Static).content)
        match = re.search(r"完整请求 (\d+)/6000", label)
        self.assertIsNotNone(match, label)
        assert match is not None
        return int(match.group(1))

    async def test_linked_question_cards_resize_without_changing_common_text_or_source(self):
        self.fixture.task.title = "核对批次与保留期限的原始依据" * 5
        _, history, source = self.fixture.saved_feedback_successor()
        original = {row.id: asdict(row) for row in (history, source)}
        revision = self.fixture.store.revision
        app = Waggle(store=self.fixture.store, runtime=self.fixture.runtime)
        async with app.run_test(size=(80, 24)) as pilot:
            form = await self.open_question(app, pilot)
            picker = form.query_one("#prior-feedback", SelectionList)
            for key, _ in form.feedback_options:
                picker.select(key)
            text = "请分别说明批次标识与保留期限的已有原件和来源。" * 20
            form.query_one("#note", TextArea).text = text
            for size in ((80, 24), (40, 18), (120, 40)):
                with self.subTest(size=size):
                    await pilot.resize_terminal(*size)
                    await pilot.pause()
                    await pilot.press("ctrl+enter")
                    await fixtures.wait_ui(pilot, lambda: isinstance(app.screen, RequestReview))
                    review = app.screen
                    assert isinstance(review, RequestReview)
                    assert review.focused is not None
                    self.assertEqual(review.focused.id, "cancel")
                    self.assertEqual(review.query_one("#request-raw", TextArea).text, review.request)
                    self.assertEqual(review.request.count(text), 1)
                    self.assertIn(self.fixture.task.title, str(review.query_one("#request-task", Static).content))
                    for index, question in enumerate(("批次标识是什么意思？", "旧编号保留期限是什么意思？"), 1):
                        card = review.query_one(f"#request-card-{index}")
                        self.assertIn(question, str(card.query_one(f"#request-history-{index}", Static).content))
                        identity = "request-user-text" if index == 1 else f"request-reply-text-{index}"
                        self.assertEqual(str(card.query_one("#" + identity, Static).content), text)
                    dialog = review.query_one("#dialog")
                    for button in review.query(".dialog-buttons Button"):
                        self.assertTrue(dialog.content_region.contains_region(button.region),
                                        (dialog.content_region, button.region))
                    body = review.query_one("#request-review-body", VerticalScroll)
                    self.assertGreaterEqual(body.content_region.height, 3)
                    body.focus()
                    await pilot.press("end")
                    await fixtures.wait_ui(pilot, lambda: body.scroll_y == body.max_scroll_y)
                    await pilot.press("ctrl+enter")
                    await pilot.pause()
                    self.assertIs(app.screen, review)
                    self.assertEqual(self.fixture.commands(), [])
                    await pilot.press("escape")
                    await fixtures.wait_ui(pilot, lambda: app.screen is form and not form._saving)
                    self.assertEqual(form.query_one("#note", TextArea).text, text)
            self.assertEqual(self.fixture.store.revision, revision)
            self.assertEqual(self.fixture.commands(), [])
            self.assertTrue(all(asdict(app.board.runs[key]) == row for key, row in original.items()))

    async def test_question_capacity_matches_preview_including_selected_history(self):
        _, history, source = self.fixture.saved_feedback_successor()
        original = {row.id: asdict(row) for row in (history, source)}
        app = Waggle(store=self.fixture.store, runtime=self.fixture.runtime)
        revision = self.fixture.store.revision
        async with app.run_test(size=(80, 24)) as pilot:
            form = await self.open_question(app, pilot)
            editor = form.query_one("#note", TextArea)
            editor.text = "术语"
            await pilot.pause()
            before = self.compiled_count(form)
            selections = form.query_one("#prior-feedback", SelectionList)
            selections.select(form.feedback_options[0][0])
            await pilot.pause()
            after = self.compiled_count(form)
            self.assertGreater(after, before)
            self.assertIn("输入 2/4500", str(form.query_one("#note-length", Static).content))
            self.assertIn("旧问题", str(form.query_one("#note-length", Static).content))
            await pilot.press("ctrl+enter")
            await fixtures.wait_ui(pilot, lambda: isinstance(app.screen, RequestReview))
            self.assertEqual(after, len(app.screen.query_one("#request-raw", TextArea).text))
            await pilot.press("escape")
            await fixtures.wait_ui(pilot, lambda: app.screen is form and not form._saving)
            editor.text = "问" * 4490
            await pilot.pause()
            error = str(form.query_one("#note-validation", Static).content)
            self.assertIn("4500", error)
            self.assertIn("旧问题", error)
            await pilot.press("ctrl+enter")
            await pilot.pause()
            self.assertIs(app.screen, form)
            self.assertEqual(editor.text, "问" * 4490)
            self.assertEqual(self.fixture.commands(), [])
            self.assertEqual(self.fixture.store.revision, revision)
            self.assertTrue(all(asdict(app.board.runs[key]) == value for key, value in original.items()))

    async def test_capacity_controls_and_full_reason_at_all_sizes(self):
        self.fixture.saved_feedback_successor()
        app = Waggle(store=self.fixture.store, runtime=self.fixture.runtime)
        async with app.run_test(size=(80, 24)) as pilot:
            form = await self.open_question(app, pilot)
            editor = form.query_one("#note", TextArea)
            form.query_one("#prior-feedback", SelectionList).select(form.feedback_options[0][0])
            editor.text = "术语"
            await pilot.pause()
            for width, height in ((80, 24), (40, 18), (120, 40)):
                with self.subTest(size=(width, height)):
                    await pilot.resize_terminal(width, height)
                    await pilot.pause()
                    dialog = form.query_one("#dialog")
                    self.assertGreaterEqual(editor.content_region.height, 3)
                    for identity in ("note", "note-length", "submit", "save-only", "cancel"):
                        widget = form.query_one("#" + identity)
                        self.assertTrue(dialog.content_region.contains_region(widget.region),
                                        (identity, dialog.content_region, widget.region))
                    editor.text = "问" * 4490
                    await pilot.press("ctrl+enter")
                    await pilot.pause()
                    self.assertIs(app.screen, form)
                    reason = form.query_one("#note-validation", Static)
                    self.assertIn("旧问题", str(reason.content))
                    self.assertTrue(reason.can_focus)
                    self.assertEqual(editor.text, "问" * 4490)
                    self.assertGreaterEqual(editor.content_region.height, 3)
                    for button in form.query(".dialog-buttons Button"):
                        self.assertTrue(dialog.content_region.contains_region(button.region),
                                        (button.id, dialog.content_region, button.region))
                    editor.text = "术语"
                    await pilot.pause()
            self.assertEqual(self.fixture.commands(), [])

    async def test_queued_intent_needs_exact_publication_evidence_and_restart_is_unknown(self):
        _, old = self.fixture.ended()
        board = self.fixture.store.load()
        task = board.tasks[old.task_id]
        board.return_work(task, review_requirements("核对本轮材料", str(self.fixture.project), "原件与断言"))
        run = board.queue_real(task, str(uuid4()), old.session_id, old.cwd)
        unknown = delivery.submission_status(board, task)
        self.assertIn("发布未确认", unknown)
        self.assertNotIn("请求已发布", unknown)
        known = delivery.submission_status(board, task, published_runs={run.identity})
        self.assertIn("请求已发布", known)
        self.assertIn("等待接收", known)
        self.assertNotIn("已接收", known)
        self.assertNotIn("已完成", known)
        board.runs[run.id] = replace(run, requirements=review_requirements(
            "另一次明确核查", str(self.fixture.project), "另一份原件"))
        self.assertIn("发布未确认", delivery.submission_status(board, task, published_runs={run.identity}))
        board.runs[run.id] = run
        self.assertIn("发布未确认", delivery.submission_status(board, task))
        self.assertEqual(self.fixture.commands(), [])

    async def test_only_bound_system_ack_is_receipt_and_report_is_not_acceptance(self):
        _, old = self.fixture.ended()
        board = self.fixture.store.load()
        task = board.tasks[old.task_id]
        board.return_work(task, review_requirements("核查", old.cwd, "原件"))
        queued = board.queue_real(task, str(uuid4()), old.session_id, old.cwd)
        for events in (
                ["Pi 已确认对应任务消息 · 仅旧文字"],
                [encoded_event("progress", "自称收到", source="executor", run_id=queued.id, seq=1,
                               report=Report("自称收到", [], "仍需核对"))],
                [encoded_event("acknowledged", "别轮收到", source="system", run_id=str(uuid4()), seq=1)],
                [encoded_event("acknowledged", "未来修订", source="system", run_id=queued.id, seq=2)]):
            with self.subTest(events=events):
                board.runs[queued.id] = replace(queued, state="working", seq=1, events=events)
                self.assertNotIn("Pi 已接收", delivery.submission_status(board, task))
        board.runs[queued.id] = queued
        ack = encoded_event("acknowledged", "Pi 已确认对应任务消息", source="system", run_id=queued.id, seq=1)
        working = replace(queued, state="working", seq=1, events=[ack])
        self.assertTrue(board.ingest(working))
        received = delivery.submission_status(board, task)
        self.assertIn("Pi 已接收", received)
        self.assertNotIn("已完成", received)
        full = replace(working, state="finished", seq=3, delivery="full", summary="执行者自报交齐",
                       checks=["执行者自报检查"], remaining="", events=[ack,
                           encoded_event("full", "完整自报", source="executor", run_id=queued.id, seq=2,
                                         report=Report("执行者自报交齐", ["执行者自报检查"], "")),
                           encoded_event("settled", "空闲", source="system", run_id=queued.id, seq=3)])
        self.assertTrue(board.ingest(full))
        report = delivery.submission_status(board, task)
        self.assertIn("执行者自报", report)
        self.assertIn("尚未人工验收", report)
        board.accept(task)
        self.assertIn("已人工验收", delivery.submission_status(board, task))
        self.assertEqual(self.fixture.commands(), [])

    async def test_fact_confirmation_and_new_draft_are_not_sent(self):
        spec = DecisionSpec.from_record(spec_record(human_input={
            "kind": "choice", "question": "归档条件？",
            "options": [{"key": "conditional", "label": "有条件"}, {"key": "no", "label": "不需要"}]}))
        _, source = self.fixture.ended(specs=(spec,))
        board = self.fixture.store.load()
        task = board.tasks[source.task_id]
        prepare_decision_intent(board, preview_decisions(board, task.id,
            (Answer(business=BusinessAnswer("conditional", "同批次重复时")),), Target("saved")))
        status = delivery.submission_status(board, task)
        self.assertIn("业务答案已确认保存", status)
        self.assertIn("未发送", status)
        self.assertIn("不批准", status)
        self.assertNotIn("正式决定已确认", status)
        board.return_work(task, question_requirements("另一个现有材料问题"))
        draft = delivery.submission_status(board, task)
        self.assertIn("草稿已保存", draft)
        self.assertIn("未发送", draft)
        self.assertEqual(self.fixture.commands(), [])

    async def test_supplement_publication_receipt_and_coverage_are_distinct(self):
        _, old = self.fixture.ended()
        board = self.fixture.store.load()
        task = board.tasks[old.task_id]
        board.return_work(task, review_requirements("核查", old.cwd, "原件"))
        queued = board.queue_real(task, str(uuid4()), old.session_id, old.cwd)
        working = replace(queued, state="working", seq=1, events=[encoded_event(
            "acknowledged", "Pi 已确认对应任务消息", source="system", run_id=queued.id, seq=1)])
        self.assertTrue(board.ingest(working))
        note = board.prepare_supplement(task, str(uuid4()), "请核对现有原件", int(time.time() * 1000) + 30000)
        self.assertIn("补充意图已保存", delivery.submission_status(board, task))
        self.assertIn("投递未确认", delivery.submission_status(board, task))
        witness = {(working.identity, note.identity)}
        published = delivery.submission_status(board, task, published_supplements=witness)
        self.assertIn("补充请求已发布", published)
        self.assertIn("等待接收", published)
        board.supplements[note.id] = replace(note, text="不同的补充原件")
        self.assertIn("投递未确认", delivery.submission_status(board, task, published_supplements=witness))
        board.supplements[note.id] = note
        self.assertTrue(board.apply_supplement(replace(note, status="received", seq=1, run_seq=1, detail="已接收")))
        received = delivery.submission_status(board, task)
        self.assertIn("Pi 已接收补充", received)
        self.assertIn("等待报告覆盖", received)
        self.assertTrue(board.apply_supplement(replace(note, status="covered", seq=2, run_seq=1, detail="自报覆盖")))
        covered = delivery.submission_status(board, task)
        self.assertIn("补充已自报覆盖", covered)
        self.assertIn("尚未人工验收", covered)
        self.assertEqual(self.fixture.commands(), [])

    async def test_publish_stage_then_persistent_result_and_restart_unknown(self):
        _, source = self.fixture.ended()
        original = asdict(source)
        app = Waggle(store=self.fixture.store, runtime=self.fixture.runtime)
        entered, release = threading.Event(), threading.Event()
        original_dispatch = self.fixture.runtime.dispatch

        def blocked_dispatch(run):
            entered.set()
            if not release.wait(5):
                raise BridgeError("测试等待超时")
            original_dispatch(run)

        with patch.object(self.fixture.runtime, "dispatch", side_effect=blocked_dispatch) as dispatch:
            async with app.run_test(size=(80, 24)) as pilot:
                form = await self.open_question(app, pilot)
                form.query_one("#note", TextArea).text = "请解释现有材料"
                await pilot.press("ctrl+enter")
                await fixtures.wait_ui(pilot, lambda: isinstance(app.screen, RequestReview))
                try:
                    await pilot.click("#submit")
                    await fixtures.wait_ui(pilot, entered.is_set)
                    stage = form.query_one("#note-stage", Static)
                    self.assertIn("发布请求", str(stage.content))
                    self.assertIn("意图已保存", str(stage.content))
                    self.assertEqual(len(self.fixture.store.load().runs), 2)
                    self.assertEqual(self.fixture.commands(), [])
                finally:
                    release.set()
                await fixtures.wait_ui(pilot, lambda: len(app.screen_stack) == 1)
                result = app.query_one("#submission-status", Static)
                self.assertIn("请求已发布", str(result.content))
                self.assertIn("等待接收", str(result.content))
                self.assertNotIn("已完成", str(result.content))
                dispatch.assert_called_once()
                self.assertEqual(len(self.fixture.commands()), 1)
                self.assertEqual(asdict(app.board.runs[source.id]), original)
        restarted = Waggle(store=self.fixture.store, runtime=self.fixture.runtime)
        async with restarted.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            result = restarted.query_one("#submission-status", Static)
            self.assertIn("发布未确认", str(result.content))
            self.assertNotIn("请求已发布", str(result.content))
            self.assertEqual(len(self.fixture.commands()), 1)

    async def test_publish_exception_retains_unknown_result_without_retry(self):
        _, source = self.fixture.ended()
        original = asdict(source)
        app = Waggle(store=self.fixture.store, runtime=self.fixture.runtime)
        original_dispatch = self.fixture.runtime.dispatch

        def published_then_unknown(run):
            original_dispatch(run)
            raise OSError("写入后结果未知")

        with patch.object(self.fixture.runtime, "dispatch", side_effect=published_then_unknown) as dispatch:
            async with app.run_test(size=(80, 24)) as pilot:
                form = await self.open_question(app, pilot)
                form.query_one("#note", TextArea).text = "核对原有材料"
                await pilot.press("ctrl+enter")
                await fixtures.wait_ui(pilot, lambda: isinstance(app.screen, RequestReview))
                await pilot.click("#submit")
                await fixtures.wait_ui(pilot, lambda: len(app.screen_stack) == 1)
                result = app.query_one("#submission-status", Static)
                self.assertIn("发布未确认", str(result.content))
                self.assertIn("不要重复派发", str(result.content))
                self.assertNotIn("请求已发布", str(result.content))
                self.assertEqual(len(app.board.runs), 2)
                self.assertEqual(len(self.fixture.commands()), 1)
                dispatch.assert_called_once()
                self.assertEqual(asdict(app.board.runs[source.id]), original)

    async def test_decision_preflight_stage_and_full_error_preserve_formal_fields(self):
        self.fixture.ended(specs=(DecisionSpec.from_record(spec_record()),))
        app = Waggle(store=self.fixture.store, runtime=self.fixture.runtime)
        entered, release = threading.Event(), threading.Event()

        original_capability = self.fixture.runtime.check_report_capabilities

        def blocked_capability(agent, requirements="", *, structured=False, endpoint=None):
            if not structured:
                return original_capability(agent, requirements, structured=structured, endpoint=endpoint)
            entered.set()
            if not release.wait(5):
                raise BridgeError("测试等待超时")
            raise BridgeError("原报告能力变化，尚未保存或派发")

        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("d")
            await fixtures.wait_ui(pilot, lambda: isinstance(app.screen, DecisionSheet))
            form = app.screen
            assert isinstance(form, DecisionSheet)
            form.query_one("#intent-0", Select).value = "formal"
            await pilot.pause()
            form.query_one("#answer-0", Select).value = "choice:a"
            await pilot.pause()
            with patch.object(self.fixture.runtime, "check_report_capabilities", side_effect=blocked_capability):
                try:
                    await pilot.click("#decision-continue")
                    await fixtures.wait_ui(pilot, entered.is_set)
                    stage = form.query_one("#decision-stage", Static)
                    self.assertIn("核对", str(stage.content))
                    self.assertTrue(form.busy)
                    self.assertEqual(self.fixture.commands(), [])
                finally:
                    release.set()
                await fixtures.wait_ui(pilot, lambda: not form.busy)
            reason = form.query_one("#decision-error", Static)
            self.assertIn("原报告能力变化", str(reason.content))
            self.assertTrue(reason.can_focus)
            assert form.focused is not None
            self.assertEqual(form.focused.id, "decision-error")
            self.assertEqual(form.query_one("#answer-0", Select).value, "choice:a")
            self.assertEqual(self.fixture.commands(), [])

    async def test_live_supplement_stage_and_exact_publication_hint_restart_unknown(self):
        fixture = supplement_fixtures.SupplementsUI()
        fixture.root = self.fixture.project
        fixture.runtime = self.fixture.runtime
        fixture.store = self.fixture.store
        fixture.board = fixture.store.load()
        fixture.task = fixture.board.tasks[self.fixture.task.id]
        fixture.task.remaining = review_requirements("核对本轮现有材料", str(fixture.root), "原始资料和明确报告")
        endpoint = fixture.live()
        fixture.store.save(fixture.board)
        app = Waggle(store=fixture.store, runtime=fixture.runtime)
        entered, release = threading.Event(), threading.Event()
        original_send = fixture.runtime.send_supplement

        def blocked_send(run, note):
            entered.set()
            if not release.wait(5):
                raise BridgeError("测试等待超时")
            original_send(run, note)

        with patch.object(fixture.runtime, "send_supplement", side_effect=blocked_send) as send:
            async with app.run_test(size=(80, 24)) as pilot:
                await app.poll_runtime()
                await pilot.press("b")
                await fixtures.wait_ui(pilot, lambda: isinstance(app.screen, Note))
                form = app.screen
                assert isinstance(form, Note)
                form.query_one(TextArea).text = "请核对本轮现有材料"
                await pilot.press("ctrl+enter")
                await fixtures.wait_ui(pilot, lambda: isinstance(app.screen, Confirm))
                try:
                    await pilot.click("#submit")
                    await fixtures.wait_ui(pilot, entered.is_set)
                    self.assertIn("发布补充", str(form.query_one("#note-stage", Static).content))
                    self.assertEqual(len(fixture.store.load().supplements), 1)
                    self.assertEqual(list((endpoint / "supplements/requests").glob("*.json")), [])
                finally:
                    release.set()
                await fixtures.wait_ui(pilot, lambda: len(app.screen_stack) == 1)
                result = str(app.query_one("#submission-status", Static).content)
                self.assertIn("补充请求已发布", result)
                self.assertIn("等待接收", result)
                self.assertEqual(len(list((endpoint / "supplements/requests").glob("*.json"))), 1)
                send.assert_called_once()
        restarted = Waggle(store=fixture.store, runtime=fixture.runtime)
        async with restarted.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            result = str(restarted.query_one("#submission-status", Static).content)
            self.assertIn("投递未确认", result)
            self.assertNotIn("补充请求已发布", result)
            self.assertEqual(len(list((endpoint / "supplements/requests").glob("*.json"))), 1)

    async def test_preflight_stage_and_full_async_error_after_live_input_error(self):
        _, source = self.fixture.ended()
        original = asdict(source)
        revision = self.fixture.store.revision
        app = Waggle(store=self.fixture.store, runtime=self.fixture.runtime)
        entered, release = threading.Event(), threading.Event()

        def blocked_check(agent, requirements, *, structured=False):
            entered.set()
            if not release.wait(5):
                raise BridgeError("测试等待超时，不应投递")
            raise BridgeError("目标能力已变化，未保存、未派发；请重新核对")

        async with app.run_test(size=(80, 24)) as pilot:
            form = await self.open_question(app, pilot)
            editor = form.query_one("#note", TextArea)
            editor.text = "长" * 4501
            await pilot.press("ctrl+enter")
            await pilot.pause()
            self.assertFalse(form.query_one("#form-error", Static).display)
            editor.text = "请解释现有材料中的术语"
            await pilot.pause()
            with patch.object(self.fixture.runtime, "check_dispatch", side_effect=blocked_check):
                try:
                    await pilot.press("ctrl+enter")
                    await fixtures.wait_ui(pilot, entered.is_set)
                    stage = form.query_one("#note-stage", Static)
                    self.assertTrue(stage.display)
                    self.assertIn("核对目标", str(stage.content))
                    self.assertTrue(editor.read_only)
                    self.assertTrue(form.query_one("#save-only", Button).disabled)
                    self.assertEqual(self.fixture.commands(), [])
                finally:
                    release.set()
                await fixtures.wait_ui(pilot, lambda: not form._saving)
            reason = form.query_one("#note-validation", Static)
            self.assertTrue(reason.display)
            self.assertIn("目标能力已变化", str(reason.content))
            assert form.focused is not None
            self.assertEqual(form.focused.id, "note-validation")
            self.assertFalse(editor.read_only)
            self.assertEqual(editor.text, "请解释现有材料中的术语")
            self.assertEqual(self.fixture.commands(), [])
            self.assertEqual(self.fixture.store.revision, revision)
            self.assertEqual(asdict(app.board.runs[source.id]), original)

    async def test_empty_note_error_keeps_the_missing_field_ready_for_typing(self):
        self.fixture.store.save(self.fixture.board)
        app = Waggle(store=self.fixture.store, runtime=self.fixture.runtime)
        saved = []
        async with app.run_test(size=(80, 24)) as pilot:
            form = Note("核实说明", "请填写核实情况", save=saved.append, live=True)
            await app.push_screen(form)
            await pilot.pause()
            await pilot.press("ctrl+enter")
            await pilot.pause()
            self.assertIn("请填写", str(form.query_one("#form-error", Static).content))
            self.assertIs(form.focused, form.query_one("#note", TextArea))
            await pilot.press(*"隔离核实说明", "ctrl+enter")
            await fixtures.wait_ui(pilot, lambda: len(app.screen_stack) == 1)
            self.assertEqual(saved, ["隔离核实说明"])
            self.assertEqual(self.fixture.commands(), [])

    async def test_generic_note_full_error_is_keyboard_readable_without_hiding_controls(self):
        _, source = self.fixture.ended()
        original = asdict(source)
        revision = self.fixture.store.revision
        message = "来源与能力核对失败。" * 60 + "通用错误完整尾标"

        async def reject(text):
            raise BridgeError(message)

        app = Waggle(store=self.fixture.store, runtime=self.fixture.runtime)
        async with app.run_test(size=(80, 24)) as pilot:
            form = Note("隔离界面检查", "本次问题", async_save=reject)
            await app.push_screen(form)
            form.query_one("#note", TextArea).text = "请解释已有材料"
            await pilot.press("ctrl+enter")
            await fixtures.wait_ui(pilot, lambda: not form._saving and "通用错误完整尾标" in str(
                form.query_one("#form-error", Static).content))
            for size in ((80, 24), (40, 18), (120, 40)):
                with self.subTest(size=size):
                    await pilot.resize_terminal(*size)
                    await pilot.pause()
                    error = form.query_one("#form-error", Static)
                    self.assertTrue(error.can_focus)
                    self.assertIn("通用错误完整尾标", str(error.content))
                    scope = form.query_one("#note-scope", VerticalScroll)
                    scope.focus()
                    await pilot.press("end")
                    await fixtures.wait_ui(pilot, lambda: scope.scroll_y == scope.max_scroll_y)
                    dialog = form.query_one("#dialog")
                    self.assertGreaterEqual(form.query_one("#note", TextArea).content_region.height, 3)
                    for button in form.query(".dialog-buttons Button"):
                        self.assertTrue(dialog.content_region.contains_region(button.region))
            self.assertEqual(self.fixture.commands(), [])
            self.assertEqual(self.fixture.store.revision, revision)
            self.assertEqual(asdict(app.board.runs[source.id]), original)

    async def test_long_preflight_error_and_stage_survive_resize_with_keyboard_scroll(self):
        self.fixture.project = self.fixture.root / ("中文边界" * 15) / ("精确项目" * 12)
        self.fixture.project.mkdir(parents=True)
        self.fixture.board.add_project("长目录项目", str(self.fixture.project))
        self.fixture.task = self.fixture.board.create("长路径核对", "长目录项目", "仅核对当前项目已有材料")
        _, source = self.fixture.ended()
        original = asdict(source)
        revision = self.fixture.store.revision
        app = Waggle(store=self.fixture.store, runtime=self.fixture.runtime)
        app.selected_id = source.task_id
        entered, release = threading.Event(), threading.Event()
        message = "目标能力需重新核对。" * 60 + "完整错误尾标；未保存、未派发"

        def blocked_check(agent, requirements, *, structured=False):
            entered.set()
            if not release.wait(8):
                raise BridgeError("测试等待超时")
            raise BridgeError(message)

        async with app.run_test(size=(80, 24)) as pilot:
            form = await self.open_question(app, pilot)
            editor = form.query_one("#note", TextArea)
            editor.text = "请解释已有材料中的术语"
            with patch.object(self.fixture.runtime, "check_dispatch", side_effect=blocked_check):
                try:
                    await pilot.press("ctrl+enter")
                    await fixtures.wait_ui(pilot, entered.is_set)
                    for size in ((80, 24), (40, 18), (120, 40)):
                        with self.subTest(stage_size=size):
                            await pilot.resize_terminal(*size)
                            await pilot.pause()
                            self.assertIn("核对目标", str(form.query_one("#note-stage", Static).content))
                            self.assertTrue(editor.read_only)
                            dialog = form.query_one("#dialog")
                            self.assertGreaterEqual(editor.content_region.height, 3)
                            for button in form.query(".dialog-buttons Button"):
                                self.assertTrue(dialog.content_region.contains_region(button.region))
                            scope = form.query_one("#note-scope", VerticalScroll)
                            scope.focus()
                            await pilot.press("end")
                            await fixtures.wait_ui(pilot, lambda: scope.scroll_y == scope.max_scroll_y)
                            self.assertIn(source.cwd, str(form.query_one("#note-scope-text", Static).content))
                finally:
                    release.set()
                await fixtures.wait_ui(pilot, lambda: not form._saving)
            reason = form.query_one("#note-validation", Static)
            self.assertIn("完整错误尾标", str(reason.content))
            for size in ((80, 24), (40, 18), (120, 40)):
                with self.subTest(error_size=size):
                    await pilot.resize_terminal(*size)
                    await pilot.pause()
                    self.assertTrue(reason.display)
                    self.assertIn("完整错误尾标", str(reason.content))
                    self.assertTrue(reason.can_focus)
                    scope = form.query_one("#note-scope", VerticalScroll)
                    scope.focus()
                    await pilot.press("end")
                    await fixtures.wait_ui(pilot, lambda: scope.scroll_y == scope.max_scroll_y)
                    self.assertTrue(form.query_one("#dialog").content_region.contains_region(editor.region))
                    self.assertEqual(editor.text, "请解释已有材料中的术语")
            editor.text += "，请核对原件"
            await pilot.pause()
            self.assertNotIn("完整错误尾标", str(reason.content))
            self.assertEqual(self.fixture.commands(), [])
            self.assertEqual(self.fixture.store.revision, revision)
            self.assertEqual(asdict(app.board.runs[source.id]), original)

    async def test_persistent_result_is_accessible_at_all_sizes(self):
        _, source = self.fixture.ended()
        original = asdict(source)
        app = Waggle(store=self.fixture.store, runtime=self.fixture.runtime)
        async with app.run_test(size=(80, 24)) as pilot:
            for size in ((80, 24), (40, 18), (120, 40)):
                with self.subTest(size=size):
                    await pilot.resize_terminal(*size)
                    app.focus_tasks()
                    await pilot.press("enter")
                    await pilot.pause()
                    self.assertTrue(app.detail_open)
                    result = app.query_one("#submission-status", Static)
                    self.assertTrue(result.display)
                    self.assertTrue(result.can_focus)
                    self.assertIn("执行者自报", str(result.content))
                    self.assertIn("尚未人工验收", str(result.content))
                    self.assertGreater(result.region.width, 0)
                    detail = app.query_one("#detail-scroll", VerticalScroll)
                    self.assertGreater(detail.content_region.height, 0)
                    detail.focus()
                    await pilot.press("end")
                    await fixtures.wait_ui(pilot, lambda: detail.scroll_y == detail.max_scroll_y)
                    await pilot.press("home")
                    await fixtures.wait_ui(pilot, lambda: detail.scroll_y == 0)
            self.assertEqual(self.fixture.commands(), [])
            self.assertEqual(asdict(app.board.runs[source.id]), original)

    async def test_authenticated_context_is_counted_and_full_request_overflow_preserves_input(self):
        board = self.fixture.board
        self.fixture.project = self.fixture.root / "前序后续项目"
        self.fixture.project.mkdir()
        board.add_project("数据实验室", str(self.fixture.project))
        parent = context_fixtures._make_accepted_parent(board)
        parent_run = board.runs[parent.run_id]
        report = current_report(parent, parent_run)
        accepted = next(event for raw in parent.events if (event := parse_event(raw)) and event.kind == "accepted")
        assert report is not None
        context = create_followup_context(board, parent, report.id, accepted.id, ["summary", "checks"],
                                          "核对前序术语", "只解释前序材料中的术语")
        child = board.create_followup(parent.id, context, "核对前序术语", "只解释前序材料中的术语")
        parent_before, run_before = asdict(parent), asdict(parent_run)
        self.fixture.task = child
        self.fixture.store.save(board)
        _, source = self.fixture.ended()
        original = asdict(source)
        revision = self.fixture.store.revision
        app = Waggle(store=self.fixture.store, runtime=self.fixture.runtime)
        app.selected_id = child.id
        async with app.run_test(size=(80, 24)) as pilot:
            form = await self.open_question(app, pilot)
            editor = form.query_one("#note", TextArea)
            editor.text = "请解释术语"
            await pilot.pause()
            plain = question_requirements(editor.text)
            exact = app.board.compiled_requirements(app.board.tasks[child.id], plain)
            self.assertGreater(len(exact), len(plain))
            self.assertEqual(self.compiled_count(form), len(exact))
            await pilot.press("ctrl+enter")
            await fixtures.wait_ui(pilot, lambda: isinstance(app.screen, RequestReview))
            self.assertEqual(app.screen.query_one("#request-raw", TextArea).text, exact)
            await pilot.press("escape")
            await fixtures.wait_ui(pilot, lambda: app.screen is form and not form._saving)
            await self.fixture.discard_note(app, pilot)
            review = await self.fixture.open_note(app, pilot)
            assert isinstance(review, DispatchNote)
            review.query_one("#note", TextArea).text = "交" * 1800
            review.query_one("#review-scope", Input).value = "范" * 1800
            review.query_one("#review-verification", Input).value = "验" * 1800
            await pilot.pause()
            reason = str(review.query_one("#note-validation", Static).content)
            self.assertIn("6000", reason)
            self.assertIn("选定来源", reason)
            self.assertIn("暂不可核对", str(review.query_one("#note-length", Static).content))
            with patch.object(self.fixture.runtime, "check_dispatch", wraps=self.fixture.runtime.check_dispatch) as check:
                await pilot.press("ctrl+enter")
                await pilot.pause()
                self.assertIs(app.screen, review)
                check.assert_not_called()
            self.assertEqual(review.query_one("#note", TextArea).text, "交" * 1800)
            self.assertEqual(self.fixture.store.revision, revision)
            self.assertEqual(self.fixture.commands(), [])
            self.assertEqual(asdict(app.board.runs[source.id]), original)
            self.assertEqual(asdict(app.board.tasks[parent.id]), parent_before)
            self.assertEqual(asdict(app.board.runs[parent_run.id]), run_before)

    async def test_review_fields_have_exact_local_capacity_without_extra_layout_rows(self):
        _, source = self.fixture.ended()
        original = asdict(source)
        revision = self.fixture.store.revision
        app = Waggle(store=self.fixture.store, runtime=self.fixture.runtime)
        async with app.run_test(size=(80, 24)) as pilot:
            form = await self.fixture.open_note(app, pilot)
            assert isinstance(form, DispatchNote)
            editor = form.query_one("#note", TextArea)
            scope = form.query_one("#review-scope", Input)
            verification = form.query_one("#review-verification", Input)
            editor.text = "交付当前材料清单"
            scope.value = "范围" * 900 + "边"
            verification.value = "核对原件"
            await pilot.pause()
            label = form.query_one("#review-scope-length", Static)
            self.assertIn("1801/1800", str(label.content))
            self.assertIn("超出", str(label.content))
            self.assertIn("允许的范围", str(scope.tooltip))
            await pilot.press("ctrl+enter")
            await pilot.pause()
            self.assertIn("允许的范围", str(form.query_one("#note-validation", Static).content))
            self.assertEqual(scope.value, "范围" * 900 + "边")
            scope.value = "仅当前项目"
            verification.value = ""
            await pilot.pause()
            self.assertIn("0/1800", str(form.query_one("#review-verification-length", Static).content))
            self.assertIn("验收依据", str(verification.tooltip))
            verification.value = "核对原件"
            await pilot.pause()
            for size in ((80, 24), (40, 18), (120, 40)):
                with self.subTest(size=size):
                    await pilot.resize_terminal(*size)
                    await pilot.pause()
                    dialog = form.query_one("#dialog")
                    for identity in ("note", "note-length", "review-scope-length", "review-scope",
                                     "review-verification-length", "review-verification", "submit", "save-only", "cancel"):
                        widget = form.query_one("#" + identity)
                        self.assertTrue(dialog.content_region.contains_region(widget.region),
                                        (identity, dialog.content_region, widget.region))
            self.assertEqual(self.fixture.store.revision, revision)
            self.assertEqual(self.fixture.commands(), [])
            self.assertEqual(asdict(app.board.runs[source.id]), original)

    async def test_unbound_terminal_report_and_invalid_rejection_revision_remain_unknown(self):
        _, source = self.fixture.ended()
        board = self.fixture.store.load()
        task = board.tasks[source.task_id]
        self.assertIn("自报报告已到达", delivery.submission_status(board, task))
        report = Report(source.summary, list(source.checks), source.remaining)
        for events in (
                [encoded_event("settled", "结束", source="system", run_id=source.id, seq=2)],
                [encoded_event("partial", "未来报告", source="executor", run_id=source.id, seq=3, report=report)],
                [encoded_event("partial", "别轮报告", source="executor", run_id=str(uuid4()), seq=1, report=report)]):
            with self.subTest(events=events):
                board.runs[source.id] = replace(source, events=events)
                status = delivery.submission_status(board, task)
                self.assertIn("报告原记录待核对", status)
                self.assertNotIn("自报报告已到达", status)
        board.runs[source.id] = source
        board.return_work(task, review_requirements("核查", source.cwd, "原件"))
        queued = board.queue_real(task, str(uuid4()), source.session_id, source.cwd)
        working = replace(queued, state="working", seq=1, events=[encoded_event(
            "acknowledged", "收到", source="system", run_id=queued.id, seq=1)])
        self.assertTrue(board.ingest(working))
        note = board.prepare_supplement(task, str(uuid4()), "核对原件", int(time.time() * 1000) + 30000)
        self.assertTrue(board.apply_supplement(replace(note, status="rejected", seq=1, run_seq=0, detail="接收前拒绝")))
        self.assertIn("补充未被接收", delivery.submission_status(board, task))
        for seq, run_seq in ((1, 2), (0, 0)):
            with self.subTest(seq=seq, run_seq=run_seq):
                board.supplements[note.id] = replace(note, status="rejected", seq=seq, run_seq=run_seq)
                status = delivery.submission_status(board, task)
                self.assertIn("补充回执修订待核对", status)
                self.assertNotIn("补充未被接收", status)
        self.assertEqual(self.fixture.commands(), [])

    async def test_result_binding_and_acceptance_revision_fail_closed(self):
        _, old = self.fixture.ended()
        board = self.fixture.store.load()
        task = board.tasks[old.task_id]
        board.return_work(task, review_requirements("核查", old.cwd, "原件"))
        run = board.queue_real(task, str(uuid4()), old.session_id, old.cwd)
        for field in ("title", "description"):
            with self.subTest(field=field):
                original = getattr(task, field)
                setattr(task, field, original + "（已变化）")
                status = delivery.submission_status(board, task, published_runs={run.identity})
                self.assertIn("来源待核对", status)
                self.assertNotIn("请求已发布", status)
                setattr(task, field, original)
        finished = replace(run, state="finished", seq=2, delivery="full", summary="完整自报",
                           checks=["原件"], remaining="", events=[
                               encoded_event("full", "完整自报", source="executor", run_id=run.id, seq=1,
                                             report=Report("完整自报", ["原件"], "")),
                               encoded_event("settled", "结束", source="system", run_id=run.id, seq=2)])
        self.assertTrue(board.ingest(finished))
        board.accept(task)
        self.assertIn("已人工验收", delivery.submission_status(board, task))
        task.events = [raw for raw in task.events
                       if not ((event := parse_event(raw)) and event.kind == "accepted" and event.run_id == run.id)]
        task.events.append(encoded_event("accepted", "未来修订的验收", source="human", run_id=run.id, seq=3))
        self.assertIn("人工验收原记录待核对", delivery.submission_status(board, task))
        self.assertNotIn("本任务已人工验收", delivery.submission_status(board, task))
        self.assertEqual(self.fixture.commands(), [])

    async def test_decision_feedback_has_local_length_reason_without_losing_input(self):
        self.fixture.ended(specs=(DecisionSpec.from_record(spec_record()),))
        app = Waggle(store=self.fixture.store, runtime=self.fixture.runtime)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("d")
            await fixtures.wait_ui(pilot, lambda: isinstance(app.screen, DecisionSheet))
            form = app.screen
            assert isinstance(form, DecisionSheet)
            form.query_one("#intent-0", Select).value = "feedback"
            await pilot.pause()
            editor = form.query_one("#comment-0", TextArea)
            editor.text = "文" * 501
            await pilot.pause()
            length = form.query_one("#comment-length-0", Static)
            self.assertIn("501/500", str(length.content))
            self.assertIn("超出", str(length.content))
            self.assertTrue(length.can_focus)
            self.assertTrue(form.query_one("#decision-saved", Button).disabled)
            self.assertIn("500", str(form.query_one("#decision-saved", Button).tooltip))
            self.assertEqual(editor.text, "文" * 501)
            editor.text = "请解释现有资料中的含义"
            await pilot.pause()
            self.assertIn(str(len(editor.text)) + "/500", str(length.content))
            self.assertFalse(form.query_one("#decision-saved", Button).disabled)
            self.assertEqual(self.fixture.commands(), [])
