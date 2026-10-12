"""Supplement + explicit start uses only scratch databases and private mailboxes."""

import json
import os
import shutil
import time
import unittest
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from textual.widgets import (
    Button,
    Collapsible,
    Input,
    OptionList,
    SelectionList,
    Static,
    TextArea,
)

from app import Choice, Confirm, DispatchNote, NewAgentForm, Note, RequestReview, Waggle
from decision_view import DiscardChanges
from decisions import (
    EVIDENCE_ONLY_PREFIX,
    Answer,
    DecisionSpec,
    Target,
    feedback_answers,
    feedback_question_key,
    prepare_decision_intent,
    preview_decisions,
    proposal_question,
    proposal_requirements,
    proposal_selected_keys,
    question_requirements,
    review_fields,
    review_requirements,
)
from delivery import prior_feedback_candidates
from journal import Brief, Report, encoded_event
from launch import prepare_launch
from runtime import Agent, Runtime, private_dir, publish
from store import StorageError, Store, encode
from task_context import WorkContract
from test_bridge import PiFixture
from test_capabilities import capability_record
from test_continuations import session_record
from test_decisions import spec_record
from test_live import wait_ui


@unittest.skipUnless(shutil.which("pi"), "installed Pi required")
class SupplementStartOffline(unittest.IsolatedAsyncioTestCase):
    async def test_submit_executes_once_and_reports_through_real_offline_pi(self):
        with TemporaryDirectory(prefix="waggle-submit-offline-") as directory:
            fixture = PiFixture(Path(directory))
            launch = prepare_launch(fixture.runtime, str(fixture.project), "Offline submit", "", str(uuid4()))
            try:
                await fixture.start(launch=launch)
                agent = fixture.runtime.agent(launch.id)
                with Store(Path(directory) / "tasks.sqlite3") as store:
                    board = store.load()
                    board.add_project("离线项目", str(fixture.project))
                    task = board.create("FULL", "离线项目", "只运行临时目录内的离线计数器。")
                    board.assign(task, agent.id)
                    store.save(board)
                    app = Waggle(store=store, runtime=fixture.runtime)
                    with patch.object(fixture.runtime, "dispatch", wraps=fixture.runtime.dispatch) as dispatch:
                        async with app.run_test(size=(80, 24)) as pilot:
                            await wait_ui(pilot, lambda: fixture.runtime.agent(launch.id).available)
                            await pilot.press("b")
                            await wait_ui(pilot, lambda: isinstance(app.screen, Note))
                            app.screen.query_one(TextArea).text = "补充即提交，但只执行这一次"
                            app.screen.query_one("#review-scope", Input).value = str(fixture.project)
                            app.screen.query_one("#review-verification", Input).value = "离线计数器恰好一次且有 full 自报，仍须人工验收"
                            form = app.screen
                            assert isinstance(form, Note)
                            await pilot.pause()  # Settle the explicit scope fields before mouse coordinates.
                            self.assertFalse((fixture.project / "fixture-result.json").exists())
                            self.assertTrue(await pilot.click("#submit"))
                            await wait_ui(pilot, lambda: app.screen is not form or
                                          bool(str(form.query_one("#form-error", Static).content)))
                            self.assertIsInstance(app.screen, Confirm)
                            self.assertFalse((fixture.project / "fixture-result.json").exists())
                            await pilot.click("#submit")
                            await wait_ui(pilot, lambda: bool(app.board.runs) and len(app.screen_stack) == 1)
                            run = app.board.runs[app.board.tasks[task.id].run_id]
                            result = await fixture.result(run)
                            self.assertEqual((result.state, result.delivery), ("finished", "full"))
                            await wait_ui(pilot, lambda: app.board.tasks[task.id].status == "review")
                            self.assertEqual(json.loads((fixture.project / "fixture-result.json").read_text())["counter"], 1)
                            self.assertEqual(run.requirements, review_requirements(
                                "补充即提交，但只执行这一次", str(fixture.project), "离线计数器恰好一次且有 full 自报，仍须人工验收"))
                            self.assertEqual(len(store.load().runs), 1)
                            self.assertNotEqual(app.board.tasks[task.id].status, "done")
                            dispatch.assert_called_once()
            finally:
                await fixture.close()


class SupplementStartTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory(prefix="waggle-supplement-start-")))
        self.project = self.root / "中文 project"
        self.project.mkdir()
        self.runtime = Runtime(self.root / "bridges")
        self.store = self.enterContext(Store(self.root / "tasks.sqlite3"))
        self.board = self.store.load()
        self.board.add_project("项目", str(self.project))
        self.task = self.board.create("核对范围后继续", "项目", "原始说明；不能扩大工作目录。")
        self.store.save(self.board)

    def agent(self, *, identity=None, session=None, name="隔离执行者"):
        agent = Agent(identity or str(uuid4()), session or str(uuid4()), str(self.project), name, "offline/fixture",
                      os.getpid(), int(time.time() * 1000), True, True, "")
        endpoint = self.runtime.root / agent.id
        for directory in (self.runtime.root, endpoint, endpoint / "commands", endpoint / "runs"):
            private_dir(directory, create=True)
        publish(endpoint / "agent.json", {"version": 1, **asdict(agent)})
        publish(endpoint / "report-capabilities.json", capability_record(agent))
        self.assertTrue(self.runtime.agent(agent.id).available)
        return agent

    def assign(self):
        agent = self.agent()
        self.board.assign(self.task, agent.id)
        self.store.save(self.board)
        return agent

    def ended(self, *, specs=()):
        self.task.remaining = WorkContract(
            "implementation", "完成当前隔离任务", str(self.project),
            "原件与测试断言可核对", "不扩大目录，不批准新预算或试验",
        ).compile()
        agent = self.assign()
        run = deepcopy(self.board.queue_real(self.task, str(uuid4()), agent.session_id, agent.cwd))
        run.state, run.seq, run.delivery = "finished", 2, "partial"
        run.summary, run.checks, run.remaining = "原轮成果", ["原轮自报依据"], "原轮尚未完成的工作"
        run.events = [encoded_event("partial", "原报告", source="executor", run_id=run.id, seq=1,
                                   report=Report(run.summary, run.checks, run.remaining,
                                                 Brief(["原核查"], "仅部分完成", "本次未验证"), decisions=specs or None)),
                      encoded_event("settled", "原轮结束", source="system", run_id=run.id, seq=2)]
        self.board.ingest(run)
        self.store.save(self.board)
        self.write_session(run)
        return agent, run

    def write_session(self, run):
        path = self.runtime.root / run.agent_id / "session.json"
        revision = json.loads(path.read_text())["seq"] if path.exists() else 0
        path.write_text(json.dumps(session_record(run, seq=revision + 1)), encoding="utf-8")
        path.chmod(0o600)
        self.runtime.check_new_round(run)

    async def open_note(self, app, pilot):
        await pilot.press("b")
        await wait_ui(pilot, lambda: isinstance(app.screen, (Choice, Note)))
        if isinstance(app.screen, Choice):
            self.assertEqual(set(app.screen.options), {"question", "advice", "review", "approval"})
            choices = app.screen.query_one("#choices", OptionList)
            choices.highlighted = next(index for index in range(choices.option_count)
                                       if choices.get_option_at_index(index).id == "review")
            await pilot.press("enter")
            await wait_ui(pilot, lambda: isinstance(app.screen, Note))
        form = app.screen
        assert isinstance(form, Note)
        if form.query("#review-scope"):
            form.query_one("#review-scope", Input).value = "仅当前隔离测试项目的已存材料"
            form.query_one("#review-verification", Input).value = "列出可核对的原件与验收结果"
            await pilot.pause()  # Let the extra fields settle before mouse button coordinates are measured.
            dialog = form.query_one("#dialog")
            for button in form.query(".dialog-buttons Button").results(Button):
                self.assertTrue(dialog.content_region.contains_region(button.region), (dialog.content_region, button.region))
        self.assertEqual(str(form.query_one("#submit", Button).label), "提交并核对")
        self.assertEqual(form.query_one("#save-only", Button).label, "仅保存")
        return form

    async def select_purpose(self, app, pilot, purpose):
        chooser = app.screen
        assert isinstance(chooser, Choice)
        choices = chooser.query_one("#choices", OptionList)
        choices.highlighted = next(index for index in range(choices.option_count)
                                   if choices.get_option_at_index(index).id == purpose)
        await pilot.press("enter")
        await wait_ui(pilot, lambda: isinstance(app.screen, Note))

    async def discard_note(self, app, pilot):
        await pilot.press("escape")
        await wait_ui(pilot, lambda: isinstance(app.screen, DiscardChanges) or len(app.screen_stack) == 1)
        if isinstance(app.screen, DiscardChanges):
            await pilot.click("#discard-edit")
            await wait_ui(pilot, lambda: len(app.screen_stack) == 1)

    def commands(self):
        return list(self.runtime.root.glob("*/commands/*.json"))

    def saved_feedback_successor(self):
        specs = tuple(DecisionSpec.from_record(spec_record(key=key, question=question))
                      for key, question in (("batch", "批次标识是什么意思？"), ("retention", "旧编号保留期限是什么意思？")))
        agent, history = self.ended(specs=specs)
        prepare_decision_intent(self.board, preview_decisions(self.board, self.task.id, (
            Answer(comment="请解释已有材料中的批次标识"), Answer(comment="请解释已有材料中的旧编号保留期限"),
        ), Target("saved")))
        self.store.save(self.board)
        self.board.return_work(self.task, review_requirements("核对已有隔离材料", str(self.project), "列出已有来源"))
        queued = self.board.queue_real(self.task, str(uuid4()), agent.session_id, agent.cwd)
        self.store.save(self.board)
        incoming = deepcopy(queued)
        incoming.state, incoming.seq, incoming.delivery = "finished", 2, "partial"
        incoming.summary, incoming.checks, incoming.remaining = "已有材料术语未解释", ["列出已有材料"], "待用户追问"
        incoming.events = [encoded_event("partial", "材料交接", source="executor", run_id=incoming.id, seq=1,
                                         report=Report(incoming.summary, incoming.checks, incoming.remaining)),
                           encoded_event("settled", "交接结束", source="system", run_id=incoming.id, seq=2)]
        self.assertTrue(self.board.ingest(incoming))
        self.store.save(self.board)
        successor = self.board.runs[incoming.id]
        self.write_session(successor)
        return agent, history, successor

    async def test_four_purposes_question_starts_with_only_blank_user_input(self):
        _, old = self.ended()
        before = asdict(old)
        app = Waggle(store=self.store, runtime=self.runtime)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("b")
            await wait_ui(pilot, lambda: isinstance(app.screen, Choice))
            choices = app.screen
            assert isinstance(choices, Choice)
            self.assertEqual(set(choices.options), {"question", "advice", "review", "approval"})
            self.assertEqual(choices.query_one("#choices", OptionList).get_option_at_index(0).id, "question")
            await pilot.press("enter")
            await wait_ui(pilot, lambda: isinstance(app.screen, DispatchNote))
            form = app.screen
            assert isinstance(form, DispatchNote)
            self.assertIn("追问已有材料", form.title_text)
            self.assertEqual(form.query_one("#note", TextArea).text, "")
            self.assertFalse(form.query("#review-scope"))
            self.assertIn("已有材料", form.scope)
            self.assertEqual(asdict(app.board.runs[old.id]), before)
            self.assertEqual(len(app.board.runs), 1)
            self.assertEqual(self.commands(), [])

    async def test_unlinked_question_preview_does_not_guess_matching_history(self):
        _, history, source = self.saved_feedback_successor()
        original = {row.id: asdict(row) for row in (history, source)}
        before = encode(self.store.load())
        app = Waggle(store=self.store, runtime=self.runtime)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("b")
            await wait_ui(pilot, lambda: isinstance(app.screen, Choice))
            await pilot.press("enter")
            await wait_ui(pilot, lambda: isinstance(app.screen, DispatchNote))
            form = app.screen
            assert isinstance(form, DispatchNote)
            self.assertEqual(form.query_one("#prior-feedback", SelectionList).selected, [])
            text = "批次标识是什么意思？"
            form.query_one("#note", TextArea).text = text
            await pilot.press("ctrl+enter")
            await wait_ui(pilot, lambda: isinstance(app.screen, RequestReview))
            review = app.screen
            assert isinstance(review, RequestReview)
            self.assertIn("独立新追问", str(review.query_one("#request-question-context", Static).content))
            self.assertIn("未关联旧问题", str(review.query_one("#request-question-context", Static).content))
            self.assertIn("D001", str(review.query_one("#request-task", Static).content))
            self.assertIn(self.task.title, str(review.query_one("#request-task", Static).content))
            self.assertEqual(str(review.query_one("#request-user-text", Static).content), text)
            self.assertEqual(review.selected_feedback, ())
            self.assertFalse(review.query("#request-history-1"))
            raw = review.query_one("#request-raw", TextArea).text
            self.assertEqual(raw, question_requirements(text))
            self.assertNotIn("本次明确选择的历史问题", raw)
            self.assertEqual(self.commands(), [])
            self.assertEqual(encode(self.store.load()), before)
            self.assertTrue(all(asdict(app.board.runs[key]) == row for key, row in original.items()))

    async def test_linked_preview_pairs_each_original_source_and_shared_current_question(self):
        _, history, source = self.saved_feedback_successor()
        original = {row.id: asdict(row) for row in (history, source)}
        before = encode(self.store.load())
        app = Waggle(store=self.store, runtime=self.runtime)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("b")
            await wait_ui(pilot, lambda: isinstance(app.screen, Choice))
            await pilot.press("enter")
            await wait_ui(pilot, lambda: isinstance(app.screen, DispatchNote))
            form = app.screen
            assert isinstance(form, DispatchNote)
            picker = form.query_one("#prior-feedback", SelectionList)
            for key, _ in form.feedback_options:
                picker.select(key)
            text = "关于这两项，我只想确认已有依据在哪里。\n请分别说明来源。"
            form.query_one("#note", TextArea).text = text
            await pilot.press("ctrl+enter")
            await wait_ui(pilot, lambda: isinstance(app.screen, RequestReview))
            review = app.screen
            assert isinstance(review, RequestReview)
            self.assertIn("明确关联 2 项", str(review.query_one("#request-question-context", Static).content))
            questions = ("批次标识是什么意思？", "旧编号保留期限是什么意思？")
            old_comments = ("请解释已有材料中的批次标识", "请解释已有材料中的旧编号保留期限")
            for index, (question, comment) in enumerate(zip(questions, old_comments, strict=True), 1):
                with self.subTest(item=index):
                    card = review.query_one(f"#request-card-{index}")
                    source_text = str(card.query_one(f"#request-history-{index}", Static).content)
                    self.assertIn("针对的问题", source_text)
                    self.assertIn(question, source_text)
                    self.assertIn("问题来源：", source_text)
                    self.assertIn("D001", source_text)
                    self.assertIn(self.task.title, source_text)
                    self.assertIn("报告修订1", source_text)
                    self.assertIn(f"第{index}项", source_text)
                    self.assertNotIn("此前保存的用户反馈", source_text, "旧反馈不能夹在原问题与当前回复之间")
                    self.assertIn("此前保存的用户反馈：" + comment,
                                  str(card.query_one(f"#request-prior-feedback-{index}", Static).content))
                    self.assertTrue(card.query_one(f"#request-prior-details-{index}", Collapsible).collapsed)
                    reply_id = "request-user-text" if index == 1 else f"request-reply-text-{index}"
                    self.assertEqual(str(card.query_one("#" + reply_id, Static).content), text)
                    self.assertIn("共同", str(card.query_one(f"#request-reply-label-{index}", Static).content))
                    self.assertIn("非逐项独立答复", str(card.query_one(f"#request-reply-label-{index}", Static).content))
            raw = review.query_one("#request-raw", TextArea).text
            self.assertEqual(raw, review.request)
            self.assertEqual(raw.count(text), 1, "显示逐项对应不能在实际请求里重复用户文字")
            self.assertEqual(set(proposal_selected_keys(raw)), {feedback_question_key(key) for key, _ in form.feedback_options})
            self.assertEqual(self.commands(), [])
            self.assertEqual(encode(self.store.load()), before)
            self.assertTrue(all(asdict(app.board.runs[key]) == row for key, row in original.items()))

    async def test_saved_primary_review_keeps_original_history_item_not_display_position(self):
        _, history, source = self.saved_feedback_successor()
        original = {row.id: asdict(row) for row in (history, source)}
        app = Waggle(store=self.store, runtime=self.runtime)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("b")
            await wait_ui(pilot, lambda: isinstance(app.screen, Choice))
            await pilot.press("enter")
            await wait_ui(pilot, lambda: isinstance(app.screen, DispatchNote))
            form = app.screen
            assert isinstance(form, DispatchNote)
            selected_key = form.feedback_options[1][0]
            form.query_one("#prior-feedback", SelectionList).select(selected_key)
            text = "请解释保留期限。\n只核对现有原件。"
            form.query_one("#note", TextArea).text = text
            await pilot.click("#save-only")
            await wait_ui(pilot, lambda: len(app.screen_stack) == 1)
            saved = encode(self.store.load())
            exact = app.board.compiled_requirements(app.board.tasks[self.task.id])
            await pilot.press("s")
            await wait_ui(pilot, lambda: isinstance(app.screen, RequestReview))
            review = app.screen
            assert isinstance(review, RequestReview)
            card = review.query_one("#request-card-1")
            source_text = str(card.query_one("#request-history-1", Static).content)
            self.assertIn("旧编号保留期限是什么意思？", source_text)
            self.assertIn("报告修订1 / 第2项", source_text)
            self.assertNotIn("批次标识是什么意思？", source_text)
            self.assertEqual(str(card.query_one("#request-user-text", Static).content), text)
            self.assertEqual(review.query_one("#request-raw", TextArea).text, exact)
            self.assertEqual(proposal_selected_keys(exact), (feedback_question_key(selected_key),))
            self.assertEqual(review.unselected_feedback, 1)
            self.assertIn(self.task.title, str(review.query_one("#request-task", Static).content))
            await pilot.press("ctrl+enter")
            await pilot.pause()
            self.assertIs(app.screen, review)
            self.assertEqual(encode(self.store.load()), saved)
            self.assertEqual(self.commands(), [])
            self.assertTrue(all(asdict(app.board.runs[key]) == row for key, row in original.items()))

    async def test_question_review_is_human_readable_and_requires_explicit_final_action(self):
        agent, old = self.ended()
        before = asdict(old)
        app = Waggle(store=self.store, runtime=self.runtime)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("b")
            await wait_ui(pilot, lambda: isinstance(app.screen, Choice))
            await pilot.press("enter")
            await wait_ui(pilot, lambda: isinstance(app.screen, DispatchNote))
            form = app.screen
            assert isinstance(form, DispatchNote)
            question = "什么是一个批次？\n请解释已有材料的来源。"
            form.query_one("#note", TextArea).text = question
            await pilot.press("ctrl+enter")
            await wait_ui(pilot, lambda: isinstance(app.screen, Confirm))
            review = app.screen
            self.assertEqual(type(review).__name__, "RequestReview")
            assert isinstance(review, Confirm)
            self.assertEqual(review.query_one("#request-user-text", Static).content, question)
            self.assertIn("追问已有材料", str(review.query_one("#request-purpose", Static).content))
            self.assertIn(agent.name, str(review.query_one("#request-target", Static).content))
            self.assertIn(str(self.project), str(review.query_one("#request-target", Static).content))
            request = review.query_one("#request-raw", TextArea).text
            self.assertTrue(request.startswith(EVIDENCE_ONLY_PREFIX))
            self.assertIn(question, request)
            self.assertIn(request, review.body)
            assert review.focused is not None
            self.assertEqual(review.focused.id, "cancel")
            await pilot.press("ctrl+enter")
            self.assertIs(app.screen, review)
            self.assertEqual(self.commands(), [])
            self.assertEqual(len(app.board.runs), 1)
            await pilot.click("#submit")
            await wait_ui(pilot, lambda: len(app.screen_stack) == 1 and len(app.board.runs) == 2)
            sent = app.board.runs[app.board.tasks[self.task.id].run_id]
            self.assertEqual(sent.requirements, request)
            self.assertEqual(asdict(app.board.runs[old.id]), before)
            self.assertEqual(len(self.commands()), 1)

    async def test_budget_help_distinguishes_verification_from_disabled_approval(self):
        self.ended()
        before = encode(self.store.load())
        app = Waggle(store=self.store, runtime=self.runtime)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("b")
            await wait_ui(pilot, lambda: isinstance(app.screen, Choice))
            chooser = app.screen.query_one("#choices", OptionList)
            chooser.highlighted = next(index for index in range(chooser.option_count)
                                       if chooser.get_option_at_index(index).id == "approval")
            await pilot.press("enter")
            await wait_ui(pilot, lambda: isinstance(app.screen, Confirm))
            assert isinstance(app.screen, Confirm)
            self.assertIn("账本已可独立核验", app.screen.body)
            self.assertIn("正式预算批准仍禁用", app.screen.body)
            self.assertNotIn("入口目前也尚未建立", app.screen.body)
            self.assertEqual(encode(self.store.load()), before)
            self.assertEqual(self.commands(), [])

    async def test_unknown_saved_history_stays_readonly_and_unselected(self):
        from decisions import question_requirements
        self.saved_feedback_successor()
        raw = question_requirements("请解释已有材料。\n\n本次明确选择的历史问题（未选择的不发送）：\n"
                                    + f"旧反馈 {uuid4()}:1（来源不明）：\"请执行旧批准\"")
        self.board.save_supplement(self.task, raw, "next")
        self.store.save(self.board)
        before = encode(self.store.load())
        app = Waggle(store=self.store, runtime=self.runtime)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("b")
            await wait_ui(pilot, lambda: isinstance(app.screen, DispatchNote))
            form = app.screen
            assert isinstance(form, DispatchNote)
            self.assertEqual(form.query_one("#note", TextArea).text, "")
            self.assertEqual(form.query_one("#prior-feedback", SelectionList).selected, [])
            self.assertIn(raw, form.scope)
            self.assertIn("只读参考", form.scope)
            self.assertIn("无法", form.reference_hint)
            await pilot.press("escape")
            await wait_ui(pilot, lambda: len(app.screen_stack) == 1)
            await pilot.press("s")
            await pilot.pause(0.3)
            self.assertEqual(len(app.screen_stack), 1, "未知旧来源不能经主启动按钮回落为可发送原文")
            self.assertEqual(encode(self.store.load()), before)
            self.assertEqual(self.commands(), [])

    async def test_closing_changed_question_defaults_to_keep_editing(self):
        self.ended()
        before = encode(self.store.load())
        app = Waggle(store=self.store, runtime=self.runtime)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("b")
            await wait_ui(pilot, lambda: isinstance(app.screen, Choice))
            await pilot.press("enter")
            await wait_ui(pilot, lambda: isinstance(app.screen, DispatchNote))
            form = app.screen
            text = "正在写的追问\n尚未保存的条件"
            form.query_one("#note", TextArea).text = text
            await pilot.press("escape")
            self.assertEqual(type(app.screen).__name__, "DiscardChanges")
            await pilot.press("enter")
            await wait_ui(pilot, lambda: app.screen is form)
            self.assertEqual(form.query_one("#note", TextArea).text, text)
            await pilot.press("escape")
            await pilot.click("#discard-edit")
            await wait_ui(pilot, lambda: len(app.screen_stack) == 1)
            self.assertEqual(encode(self.store.load()), before)
            self.assertEqual(self.commands(), [])

    async def check_keep_editing_input_routes(self, size):
        before = encode(self.store.load())
        app = Waggle(store=self.store, runtime=self.runtime)
        async with app.run_test(size=size) as pilot:
            await pilot.press('b')
            await wait_ui(pilot, lambda: isinstance(app.screen, Choice))
            await pilot.press('enter')
            await wait_ui(pilot, lambda: isinstance(app.screen, DispatchNote))
            form = app.screen
            text = '未保存的追问\n不保存、不发送、不执行'
            form.query_one('#note', TextArea).text = text
            await pilot.pause()
            for route in ('enter', 'escape', 'ctrl+enter', 'mouse'):
                with self.subTest(size=size, route=route):
                    await pilot.press('escape')
                    await wait_ui(pilot, lambda: isinstance(app.screen, DiscardChanges))
                    modal = app.screen
                    self.assertIsNotNone(modal.focused)
                    assert modal.focused is not None
                    self.assertEqual(modal.focused.id, 'keep-editing')
                    await pilot.press('tab')
                    self.assertIsNotNone(modal.focused)
                    assert modal.focused is not None
                    self.assertEqual(modal.focused.id, 'discard-edit')
                    await pilot.press('shift+tab')
                    self.assertIsNotNone(modal.focused)
                    assert modal.focused is not None
                    self.assertEqual(modal.focused.id, 'keep-editing')
                    if route == 'mouse':
                        self.assertTrue(await pilot.click('#keep-editing'))
                    else:
                        await pilot.press(route)
                    await wait_ui(pilot, lambda: app.screen is form)
                    self.assertEqual(form.query_one('#note', TextArea).text, text)
                    self.assertIsNotNone(form.focused)
                    assert form.focused is not None
                    self.assertEqual(form.focused.id, 'note')
                    self.assertEqual(encode(self.store.load()), before)
                    self.assertEqual(self.commands(), [])
            await pilot.press('escape')
            self.assertIsInstance(app.screen, DiscardChanges)
            self.assertTrue(await pilot.click('#discard-edit'))
            await wait_ui(pilot, lambda: len(app.screen_stack) == 1)
        self.assertEqual(encode(self.store.load()), before)

    async def test_keep_editing_input_routes_reset_guard_at_standard_size(self):
        self.ended()
        await self.check_keep_editing_input_routes((80, 24))

    async def test_keep_editing_input_routes_at_compact_and_large_sizes(self):
        self.ended()
        for size in ((40, 18), (120, 40)):
            await self.check_keep_editing_input_routes(size)

    async def test_history_picker_is_compact_and_summarizes_current_selection(self):
        self.saved_feedback_successor()
        before = encode(self.store.load())
        app = Waggle(store=self.store, runtime=self.runtime)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("b")
            await wait_ui(pilot, lambda: isinstance(app.screen, Choice))
            await pilot.press("enter")
            await wait_ui(pilot, lambda: isinstance(app.screen, DispatchNote))
            form = app.screen
            assert isinstance(form, DispatchNote)
            picker = form.query_one("#prior-feedback", SelectionList)
            self.assertEqual(picker.selected, [])
            self.assertEqual(str(picker.get_option_at_index(0).prompt), "批次标识是什么意思？")
            self.assertIn("0/2", str(form.query_one("#selected-feedback-summary", Static).content))
            self.assertIn("此前用户反馈", str(form.query_one("#feedback-source-text", Static).content))
            selected = form.feedback_options[0][0]
            picker.select(selected)
            await pilot.pause()
            summary = str(form.query_one("#selected-feedback-summary", Static).content)
            self.assertIn("1/2", summary)
            self.assertIn("批次标识", summary)
            self.assertNotIn("decisions", summary)
            form.query_one("#note", TextArea).text = "请说明已有材料中的批次标识。"
            await pilot.press("ctrl+enter")
            await wait_ui(pilot, lambda: isinstance(app.screen, Confirm))
            review = app.screen
            self.assertIn("批次标识", str(review.query_one("#request-history-1", Static).content))
            self.assertIn("其余 1", str(review.query_one("#request-selection-status", Static).content))
            self.assertNotIn("旧编号保留期限", review.query_one("#request-raw", TextArea).text)
            self.assertEqual(self.commands(), [])
            self.assertEqual(encode(self.store.load()), before)

    async def test_saved_history_reopens_user_question_and_explicit_saved_selection(self):
        self.saved_feedback_successor()
        before = {key: asdict(run) for key, run in self.board.runs.items()}
        app = Waggle(store=self.store, runtime=self.runtime)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("b")
            await wait_ui(pilot, lambda: isinstance(app.screen, Choice))
            await pilot.press("enter")
            await wait_ui(pilot, lambda: isinstance(app.screen, DispatchNote))
            form = app.screen
            assert isinstance(form, DispatchNote)
            picker = form.query_one("#prior-feedback", SelectionList)
            self.assertEqual(picker.selected, [])
            selected_key = form.feedback_options[1][0]
            picker.select(selected_key)
            question = "请解释保留期限的含义与出处。\n不要修改文件。"
            form.query_one("#note", TextArea).text = question
            await pilot.pause()
            self.assertTrue(form.query_one("#dialog").content_region.contains_region(form.query_one("#save-only", Button).region),
                            (form.query_one("#dialog").content_region, form.query_one("#save-only", Button).region))
            await pilot.click("#save-only")
            await wait_ui(pilot, lambda: len(app.screen_stack) == 1)
            await pilot.press("b")
            await wait_ui(pilot, lambda: isinstance(app.screen, DispatchNote))
            restored = app.screen
            assert isinstance(restored, DispatchNote)
            self.assertEqual(restored.query_one("#note", TextArea).text, question)
            self.assertEqual(restored.query_one("#prior-feedback", SelectionList).selected, [selected_key])
            self.assertIn("草稿", restored.reference_hint)
            self.assertIn("未发送", restored.reference_hint)
            self.assertNotIn("decisions 用 key=", restored.query_one("#note", TextArea).text)
            self.assertEqual(len(app.board.runs), 2)
            self.assertEqual({key: asdict(run) for key, run in app.board.runs.items()}, before)
            self.assertEqual(self.commands(), [])

    async def test_saved_full_proposal_restores_only_user_text_and_exact_saved_history(self):
        self.saved_feedback_successor()
        before = {key: asdict(run) for key, run in self.board.runs.items()}
        app = Waggle(store=self.store, runtime=self.runtime)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("b")
            await wait_ui(pilot, lambda: isinstance(app.screen, Choice))
            await self.select_purpose(app, pilot, "advice")
            form = app.screen
            assert isinstance(form, DispatchNote)
            picker = form.query_one("#prior-feedback", SelectionList)
            self.assertEqual(picker.selected, [])
            selected_key = form.feedback_options[0][0]
            picker.select(selected_key)
            user_text = "请交可审材料清单；逐项列已有来源与缺项负责人。"
            form.query_one("#note", TextArea).text = user_text
            await pilot.click("#save-only")
            await wait_ui(pilot, lambda: len(app.screen_stack) == 1)
            self.assertIsNotNone(proposal_question(app.board.tasks[self.task.id].remaining))
            await pilot.press("b")
            await wait_ui(pilot, lambda: isinstance(app.screen, DispatchNote))
            restored = app.screen
            assert isinstance(restored, DispatchNote)
            self.assertEqual(restored.query_one("#note", TextArea).text, user_text)
            self.assertEqual(restored.query_one("#prior-feedback", SelectionList).selected, [selected_key])
            self.assertIn("未发送", restored.reference_hint)
            self.assertNotIn("decisions 用 key=", restored.query_one("#note", TextArea).text)
            self.assertEqual({key: asdict(run) for key, run in app.board.runs.items()}, before)
            self.assertEqual(self.commands(), [])

    async def test_proposal_editor_shows_three_lines_and_preserves_enter_at_all_sizes(self):
        async def unexpected_submit(text):
            self.fail("Editing or resizing must not submit a request")

        before = encode(self.store.load())
        app = Waggle(store=self.store, runtime=self.runtime)
        async with app.run_test(size=(80, 24)) as pilot:
            form = DispatchNote(
                "追问已有资料 · 不授权新工作", "用自己的话写问题", scope="仅核对现有资料；不修改、不试验。",
                initial="第一行问题\n第二行条件\n第三行说明\n第四行补充", save=self.fail, submit=unexpected_submit,
                feedback_options=(("batch", "批次标识是什么意思？ · 样例旧问题，仅供参考"),
                                  ("retention", "旧编号保留期限是什么意思？ · 样例旧问题，仅供参考")),
            )
            app.push_screen(form)
            await wait_ui(pilot, lambda: app.screen is form and form.is_mounted)
            editor = form.query_one("#note", TextArea)
            original = editor.text
            for width, height in ((80, 24), (40, 18), (120, 40)):
                with self.subTest(size=(width, height)):
                    await pilot.resize_terminal(width, height)
                    await pilot.pause()
                    self.assertGreaterEqual(editor.content_region.height, 3, editor.region)
                    dialog = form.query_one("#dialog")
                    self.assertTrue(dialog.content_region.contains_region(editor.region))
                    self.assertTrue(app.screen.region.contains_region(editor.region))
                    for button in form.query(".dialog-buttons Button").results(Button):
                        self.assertTrue(dialog.content_region.contains_region(button.region), button.id)
                        self.assertTrue(app.screen.region.contains_region(button.region), button.id)
                    self.assertEqual(form.query_one("#prior-feedback", SelectionList).selected, [])
                    self.assertEqual(editor.text, original)
                    self.assertFalse(self.commands())
            editor.focus()
            await pilot.press("enter")
            self.assertEqual(editor.text.count("\n"), original.count("\n") + 1)
            self.assertFalse(self.commands())
            self.assertEqual(encode(self.store.load()), before)

    async def test_next_round_editor_shows_prior_report_as_readonly_reference_and_actions_at_40_and_80(self):
        agent, old = self.ended()
        metadata = self.runtime.root / agent.id / "agent.json"
        metadata.unlink()
        publish(metadata, {"version": 1, **asdict(replace(agent, name="基于之前的 KORUUSDT 进度继续推进"))})
        before = encode(self.store.load())
        for size in ((80, 24), (40, 20)):
            with self.subTest(size=size):
                app = Waggle(store=self.store, runtime=self.runtime)
                async with app.run_test(size=size) as pilot:
                    if size[0] < 80:
                        await pilot.press("right")
                    form = await self.open_note(app, pilot)
                    scope = str(form.query_one("#note-scope-text", Static).content)
                    self.assertIn(old.remaining, scope)
                    self.assertIn("本轮结果", scope)
                    self.assertIn("仅供参考", scope)
                    self.assertEqual(form.query_one(TextArea).text, "")
                    await pilot.pause()
                    frame = chr(10).join(strip.text for strip in form._compositor.render_strips())
                    self.assertIn("精确目录", frame)
                    self.assertIn("执行者", frame, "选择提交前仍须能看到执行目标，不可被历史反馈挤走")
                    dialog = form.query_one("#dialog")
                    for button in form.query(".dialog-buttons Button").results(Button):
                        self.assertTrue(dialog.content_region.contains_region(button.region),
                                        f"{size}: {button.id} must stay visible while reference scrolls")
                    await self.discard_note(app, pilot)
                    self.assertEqual(encode(self.store.load()), before)
                    self.assertFalse(list(self.runtime.root.glob("*/commands/*.json")))

    async def test_next_round_shows_confirmed_old_feedback_as_readonly_not_as_new_authority(self):
        spec = DecisionSpec.from_record(spec_record(missing=["原始证据缺失"]))
        agent, old = self.ended(specs=(spec,))
        feedback = "请先列出原始回执清单，我再核对"
        answers = feedback_answers((spec,), (Answer(comment=feedback),))
        prepare_decision_intent(self.board, preview_decisions(self.board, self.task.id,
            answers, Target("saved")))
        self.store.save(self.board)
        prepared = prepare_decision_intent(self.board, preview_decisions(self.board, self.task.id,
            answers, Target("new_run", agent.id, agent.session_id, agent.cwd)))
        self.store.save(self.board)
        assert prepared.run is not None
        incoming = deepcopy(prepared.run)
        incoming.state, incoming.delivery, incoming.seq = "finished", "partial", 1
        incoming.summary, incoming.checks, incoming.remaining = "只核了现有材料", ["原始回执仍未拿到"], "其余旧 ref 还需只读核对"
        incoming.events = [encoded_event("partial", "旧报告后的新轮", source="executor", run_id=incoming.id, seq=1,
            report=Report(incoming.summary, incoming.checks, incoming.remaining,
                          Brief(["核对现有材料"], "阶段结果尚未验收", "未运行新实验")))]
        self.board.ingest(incoming)
        self.store.save(self.board)
        self.write_session(incoming)
        self.assertEqual(self.board.runs[old.id], old)
        before = encode(self.store.load())
        app = Waggle(store=self.store, runtime=self.runtime)
        async with app.run_test(size=(40, 20)) as pilot:
            await pilot.press("right")
            form = await self.open_note(app, pilot)
            scope = str(form.query_one("#note-scope-text", Static).content)
            for phrase in (feedback, incoming.remaining, "旧报告", "本轮结果", "仅供参考"):
                self.assertIn(phrase, scope)
            self.assertIn(feedback, str(form.query_one("#note-reference", Static).content))
            self.assertEqual(form.query_one(TextArea).text, "")
            await self.discard_note(app, pilot)
        self.assertEqual(encode(self.store.load()), before)
        self.assertFalse(self.commands())

    async def test_selected_old_feedback_only_becomes_a_non_authorizing_proposal_question(self):
        specs = tuple(DecisionSpec.from_record(spec_record(key=key, missing=["缺原始证据"])) for key in ("source", "usage"))
        agent, old = self.ended(specs=specs)
        answers = feedback_answers(specs, (Answer(comment="先列可核对清单，我再决定"),
                                           Answer(comment="给出已用时间空间和原件，我判断")))
        prepare_decision_intent(self.board, preview_decisions(self.board, self.task.id, answers, Target("saved")))
        self.store.save(self.board)
        dispatched = prepare_decision_intent(self.board, preview_decisions(self.board, self.task.id, answers,
            Target("new_run", agent.id, agent.session_id, agent.cwd)))
        self.store.save(self.board)
        assert dispatched.run is not None
        incoming = deepcopy(dispatched.run)
        incoming.state, incoming.seq, incoming.delivery = "finished", 1, "partial"
        incoming.summary, incoming.checks, incoming.remaining = "旧材料已经核验", ["仍缺原账本"], "需要可审方案"
        incoming.events = [encoded_event("partial", "新报告", source="executor", run_id=incoming.id, seq=1,
            report=Report(incoming.summary, incoming.checks, incoming.remaining))]
        self.board.ingest(incoming)
        self.store.save(self.board)
        self.write_session(incoming)
        self.assertEqual(self.board.runs[old.id], old)
        keys = prior_feedback_candidates(self.board, self.task)
        self.assertEqual(len(keys), 2)
        wide = Waggle(store=self.store, runtime=self.runtime)
        async with wide.run_test(size=(80, 24)) as pilot:
            await pilot.press("b")
            await wait_ui(pilot, lambda: isinstance(wide.screen, Choice))
            await self.select_purpose(wide, pilot, "advice")
            await wait_ui(pilot, lambda: isinstance(wide.screen, Note))
            form80 = wide.screen
            assert isinstance(form80, Note)
            self.assertEqual(form80.query_one("#prior-feedback", SelectionList).selected, [])
            dialog80 = form80.query_one("#dialog")
            for button in form80.query(".dialog-buttons Button").results(Button):
                self.assertTrue(dialog80.content_region.contains_region(button.region), ("80列", button.id, button.region))
            await pilot.press("escape")
            self.assertFalse(self.commands())
        app = Waggle(store=self.store, runtime=self.runtime)
        before = encode(self.store.load())
        async with app.run_test(size=(40, 20)) as pilot:
            await pilot.press("right", "b")
            await wait_ui(pilot, lambda: isinstance(app.screen, Choice))
            await self.select_purpose(app, pilot, "advice")
            await wait_ui(pilot, lambda: isinstance(app.screen, Note))
            form = app.screen
            assert isinstance(form, Note)
            choices = form.query_one("#prior-feedback", SelectionList)
            self.assertEqual(choices.selected, [])
            self.assertNotIn(answers[0].comment, form.query_one(TextArea).text)
            self.assertNotIn(answers[1].comment, form.query_one(TextArea).text)
            self.assertEqual(form.query_one(TextArea).text, "")
            form.query_one(TextArea).text = "请只根据已有原件交可审提案。"
            await pilot.click("#submit")
            await wait_ui(pilot, lambda: isinstance(app.screen, Confirm))
            empty = app.screen
            assert isinstance(empty, Confirm)
            self.assertNotIn(answers[0].comment, empty.body)
            self.assertNotIn(answers[1].comment, empty.body)
            await pilot.press("enter")
            await wait_ui(pilot, lambda: app.screen is form)
            choices.select(keys[0][0])
            await pilot.pause()
            dialog = form.query_one("#dialog")
            button = form.query_one("#submit", Button)
            self.assertTrue(dialog.content_region.contains_region(button.region), (dialog.content_region, button.region))
            self.assertTrue(await pilot.click("#submit"), (dialog.content_region, button.region))
            await pilot.pause(0.35)
            self.assertIsInstance(app.screen, Confirm, str(form.query_one("#form-error", Static).content))
            final = app.screen
            assert isinstance(final, Confirm)
            self.assertIn(answers[0].comment, final.body)
            self.assertNotIn(answers[1].comment, final.body)
            self.assertIn("不是许可", final.body)
            self.assertEqual(encode(self.store.load()), before)
            await pilot.click("#submit")
            await wait_ui(pilot, lambda: len(app.board.runs) == 3 and len(self.commands()) == 1)
            sent = app.board.runs[app.board.tasks[1].run_id]
            self.assertTrue(sent.requirements.startswith(EVIDENCE_ONLY_PREFIX))
            self.assertIn(answers[0].comment, sent.requirements)
            self.assertNotIn(answers[1].comment, sent.requirements)
            self.assertEqual(proposal_selected_keys(sent.requirements), (feedback_question_key(keys[0][0]),))

    async def test_next_round_intent_question_stays_evidence_only_through_final_confirmation(self):
        agent, old = self.ended()
        before = encode(self.store.load())
        app = Waggle(store=self.store, runtime=self.runtime)
        async with app.run_test(size=(40, 20)) as pilot:
            await pilot.press("right", "b")
            await wait_ui(pilot, lambda: isinstance(app.screen, Choice))
            choices = app.screen
            assert isinstance(choices, Choice)
            self.assertEqual(set(choices.options), {"question", "advice", "review", "approval"})
            self.assertEqual(encode(self.store.load()), before)
            await pilot.press("enter")  # Explicitly choose evidence-backed advice, not formal approval.
            await wait_ui(pilot, lambda: isinstance(app.screen, Note))
            form = app.screen
            assert isinstance(form, Note)
            self.assertEqual(form.query_one(TextArea).text, "")
            self.assertIn("模型额度", str(form.query_one("#note-scope-text", Static).content))
            form.query_one(TextArea).text = "只据现有回执给建议；不足就说明缺哪位负责人"
            await pilot.click("#submit")
            await wait_ui(pilot, lambda: isinstance(app.screen, Confirm))
            confirm = app.screen
            assert isinstance(confirm, Confirm)
            self.assertIn(agent.cwd, confirm.body)
            self.assertIn("只据现有回执给建议", confirm.body)
            self.assertIn("仅补证", confirm.body)
            self.assertEqual(encode(self.store.load()), before)
            await pilot.press("enter")  # Default is cancel; draft text survives.
            await wait_ui(pilot, lambda: app.screen is form)
            self.assertEqual(form.query_one(TextArea).text, "只据现有回执给建议；不足就说明缺哪位负责人")
            self.assertFalse(self.commands())
            await pilot.click("#submit")
            await wait_ui(pilot, lambda: isinstance(app.screen, Confirm))
            await pilot.click("#submit")
            await wait_ui(pilot, lambda: len(app.board.runs) == 2 and len(app.screen_stack) == 1)
            sent = app.board.runs[app.board.tasks[self.task.id].run_id]
            self.assertTrue(sent.requirements.startswith(EVIDENCE_ONLY_PREFIX))
            self.assertEqual(sent.task_id, self.task.id)
            self.assertEqual(app.board.runs[old.id], old)
            self.assertEqual(len(self.commands()), 1)
            self.assertFalse(app.board.tasks[self.task.id].can_accept)

    async def test_proposal_request_is_default_editable_evidence_only_and_never_implicit_send(self):
        _, old = self.ended()
        app = Waggle(store=self.store, runtime=self.runtime)
        before = encode(self.store.load())
        async with app.run_test(size=(40, 20)) as pilot:
            await pilot.press("right", "b")
            await wait_ui(pilot, lambda: isinstance(app.screen, Choice))
            menu = app.screen
            assert isinstance(menu, Choice)
            self.assertIn("可审提案", menu.options["advice"])
            await self.select_purpose(app, pilot, "advice")
            await wait_ui(pilot, lambda: isinstance(app.screen, Note))
            form = app.screen
            assert isinstance(form, Note)
            self.assertEqual(form.query_one(TextArea).text, "")
            self.assertIn("清单", str(form.query_one("#note-scope-text", Static).content))
            form.query_one(TextArea).text = "请交已有材料的可审提案，缺原件写明负责人。"
            self.assertEqual(encode(self.store.load()), before)
            self.assertEqual(self.commands(), [])
            await pilot.click("#submit")
            await wait_ui(pilot, lambda: isinstance(app.screen, Confirm))
            await pilot.press("enter")
            await wait_ui(pilot, lambda: app.screen is form)
            self.assertEqual(encode(self.store.load()), before)
            await pilot.click("#submit")
            await wait_ui(pilot, lambda: isinstance(app.screen, Confirm))
            await pilot.click("#submit")
            await wait_ui(pilot, lambda: len(app.board.runs) == 2 and len(self.commands()) == 1)
            sent = app.board.runs[app.board.tasks[1].run_id]
            self.assertTrue(sent.requirements.startswith(EVIDENCE_ONLY_PREFIX))
            self.assertIsNotNone(proposal_question(sent.requirements))
            self.assertEqual(app.board.runs[old.id], old)
            self.assertFalse(app.board.tasks[1].can_accept)

    async def test_proposal_round_without_structured_handoff_is_readonly_not_a_new_approval(self):
        agent, old = self.ended()
        self.board.save_supplement(self.task, proposal_requirements("只列出有据清单"), "next")
        self.board.assign(self.task, agent.id)
        run = deepcopy(self.board.queue_real(self.task, str(uuid4()), agent.session_id, agent.cwd))
        run.state, run.seq, run.delivery = "finished", 1, "partial"
        run.summary, run.checks, run.remaining = "只核对了旧材料", ["原件仍缺"], "尚无可审提案"
        run.events = [encoded_event("partial", "本轮未交提案", source="executor", run_id=run.id, seq=1,
            report=Report(run.summary, run.checks, run.remaining))]
        self.board.ingest(run)
        self.store.save(self.board)
        self.write_session(run)
        app = Waggle(store=self.store, runtime=self.runtime)
        before = encode(self.store.load())
        async with app.run_test(size=(40, 20)) as pilot:
            await pilot.press("right")
            self.assertEqual(app.primary_action().label, "提案未交 · 查看报告")
            self.assertIn("现在不需要你批准", str(app.query_one("#next-action", Static).content))
            self.assertFalse(app.board.tasks[1].can_accept)
            self.assertFalse(self.commands())
            await pilot.pause()
            self.assertIn("提案未交", "\n".join(strip.text for strip in app.screen._compositor.render_strips()))
        self.assertEqual(encode(self.store.load()), before)
        self.assertEqual(app.board.runs[old.id], old)

    async def test_advice_only_save_preserves_fence_without_creating_or_sending_run(self):
        self.ended()
        app = Waggle(store=self.store, runtime=self.runtime)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("b")
            await wait_ui(pilot, lambda: isinstance(app.screen, Choice))
            await pilot.press("enter")  # Advice is a selection, not an approval.
            await wait_ui(pilot, lambda: isinstance(app.screen, Note))
            form = app.screen
            form.query_one(TextArea).text = "请列举可核对证据以及缺项的负责人"
            await pilot.click("#save-only")
            await wait_ui(pilot, lambda: len(app.screen_stack) == 1)
            saved = app.board.tasks[1]
            self.assertEqual((saved.status, saved.run), ("ready", "none"))
            self.assertEqual(str(app.query_one("#accept-button", Button).label), "核对已存追问 s")
            self.assertIn("尚未发送", str(app.query_one("#next-action", Static).content))
            self.assertTrue(saved.remaining.startswith(EVIDENCE_ONLY_PREFIX))
            self.assertEqual(len(app.board.runs), 1)
            self.assertEqual(self.commands(), [])
            await pilot.press("b")
            await wait_ui(pilot, lambda: isinstance(app.screen, Note))
            editor = app.screen
            assert isinstance(editor, Note)
            self.assertIn("追问已有材料", editor.title_text)
            self.assertEqual(editor.query_one(TextArea).text, "请列举可核对证据以及缺项的负责人")
            await pilot.press("escape")
            self.assertTrue(self.store.load().tasks[1].remaining.startswith(EVIDENCE_ONLY_PREFIX))
            self.assertEqual(self.commands(), [])

    async def test_saved_advice_can_be_edited_and_dispatched_once_without_nested_marker(self):
        _, old = self.ended()
        app = Waggle(store=self.store, runtime=self.runtime)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("b")
            await wait_ui(pilot, lambda: isinstance(app.screen, Choice))
            await pilot.press("enter")
            await wait_ui(pilot, lambda: isinstance(app.screen, Note))
            app.screen.query_one(TextArea).text = "旧问题仅本地保存"
            await pilot.click("#save-only")
            await wait_ui(pilot, lambda: len(app.screen_stack) == 1)
            await pilot.press("b")
            await wait_ui(pilot, lambda: isinstance(app.screen, Note))
            editor = app.screen
            assert isinstance(editor, Note)
            self.assertEqual(editor.query_one(TextArea).text, "旧问题仅本地保存")
            editor.query_one(TextArea).text = "请核对新证据的出处及缺项负责人"
            await pilot.click("#submit")
            await wait_ui(pilot, lambda: isinstance(app.screen, Confirm))
            self.assertEqual(len(app.board.runs), 1)
            self.assertEqual(self.commands(), [])
            await pilot.click("#submit")
            await wait_ui(pilot, lambda: len(app.board.runs) == 2 and len(app.screen_stack) == 1)
            sent = app.board.runs[app.board.tasks[1].run_id]
            self.assertEqual(sent.requirements.count(EVIDENCE_ONLY_PREFIX), 1)
            self.assertIn("请核对新证据", sent.requirements)
            self.assertNotIn("旧问题仅本地保存", sent.requirements)
            self.assertEqual(app.board.runs[old.id], old)
            self.assertEqual(len(self.commands()), 1)

    async def test_saved_advice_primary_requires_confirm_and_preserves_evidence_fence(self):
        _, old = self.ended()
        app = Waggle(store=self.store, runtime=self.runtime)
        async with app.run_test(size=(40, 20)) as pilot:
            await pilot.press("right", "b")
            await wait_ui(pilot, lambda: isinstance(app.screen, Choice))
            await pilot.press("enter")
            await wait_ui(pilot, lambda: isinstance(app.screen, Note))
            app.screen.query_one(TextArea).text = "请说明旧回执的证据缺口"
            await pilot.click("#save-only")
            await wait_ui(pilot, lambda: len(app.screen_stack) == 1)
            self.assertEqual(str(app.query_one("#accept-button", Button).label), "核对已存追问 s")
            await pilot.click("#accept-button")
            await wait_ui(pilot, lambda: isinstance(app.screen, Confirm))
            confirmation = app.screen
            assert isinstance(confirmation, RequestReview)
            self.assertEqual(str(confirmation.query_one("#request-user-text", Static).content), "请说明旧回执的证据缺口")
            self.assertEqual(confirmation.query_one("#request-raw", TextArea).text, app.board.compiled_requirements(app.board.tasks[1]))
            await pilot.press("ctrl+enter")
            self.assertIs(app.screen, confirmation)
            self.assertEqual(self.commands(), [])
            for text in (EVIDENCE_ONLY_PREFIX, "工具层没有强制只读", "模型次数/费用未知", "请说明旧回执"):
                self.assertIn(text, confirmation.body)
            self.assertEqual(confirmation.body.count(EVIDENCE_ONLY_PREFIX), 1)
            self.assertIn(app.board.tasks[1].remaining, confirmation.body)
            await pilot.press("enter")  # Default return never publishes.
            self.assertEqual(len(app.board.runs), 1)
            self.assertEqual(self.commands(), [])
            await pilot.click("#accept-button")
            await wait_ui(pilot, lambda: isinstance(app.screen, Confirm))
            await pilot.click("#submit")
            await wait_ui(pilot, lambda: len(app.board.runs) == 2 and len(self.commands()) == 1)
            sent = app.board.runs[app.board.tasks[1].run_id]
            self.assertTrue(sent.requirements.startswith(EVIDENCE_ONLY_PREFIX))
            self.assertEqual(app.board.runs[old.id], old)
            self.assertFalse(app.board.tasks[1].can_accept)

    async def test_request_new_authorization_only_lists_proposal_materials_and_does_not_dispatch(self):
        self.ended()
        before = encode(self.store.load())
        app = Waggle(store=self.store, runtime=self.runtime)
        async with app.run_test(size=(40, 20)) as pilot:
            await pilot.press("right", "b")
            await wait_ui(pilot, lambda: isinstance(app.screen, Choice))
            chooser = app.screen.query_one("#choices", OptionList)
            chooser.highlighted = next(index for index in range(chooser.option_count)
                                       if chooser.get_option_at_index(index).id == "approval")
            await pilot.press("enter")
            await wait_ui(pilot, lambda: isinstance(app.screen, Confirm))
            proposal = app.screen
            assert isinstance(proposal, Confirm)
            self.assertIn("先准备提案", proposal.title_text)
            self.assertIn("预算上限", proposal.body)
            self.assertIn("止损", proposal.body)
            self.assertIn("独立审批", proposal.body)
            self.assertEqual(encode(self.store.load()), before)
            await pilot.press("escape")
            self.assertEqual(len(app.screen_stack), 1)
            self.assertEqual(self.commands(), [])

    async def test_40_column_intent_menu_displays_all_four_choices_without_scrolling(self):
        self.ended()
        app = Waggle(store=self.store, runtime=self.runtime)
        async with app.run_test(size=(40, 20)) as pilot:
            await pilot.press("right", "b")
            await wait_ui(pilot, lambda: isinstance(app.screen, Choice))
            menu = app.screen
            assert isinstance(menu, Choice)
            await pilot.pause()
            frame = "\n".join(strip.text for strip in menu._compositor.render_strips())
            for label in ("追问已有材料", "索取可审提案", "安排核查", "申请新授权"):
                self.assertIn(label, frame, "40 列必须同时看到四种本轮目的，而不是只能用滚动猜")
            self.assertGreaterEqual(menu.query_one("#choices", OptionList).region.height, 4)
            await self.select_purpose(app, pilot, "review")
            await wait_ui(pilot, lambda: isinstance(app.screen, Note))
            form = app.screen
            assert isinstance(form, Note)
            self.assertIn("下一轮", form.title_text)
            self.assertEqual(self.commands(), [])

    async def test_change_during_final_confirmation_cannot_retarget_or_commit(self):
        self.ended()
        before = encode(self.store.load())
        app = Waggle(store=self.store, runtime=self.runtime)
        async with app.run_test(size=(80, 24)) as pilot:
            form = await self.open_note(app, pilot)
            form.query_one(TextArea).text = "只检查指定回执"
            await pilot.press("ctrl+enter")
            await wait_ui(pilot, lambda: isinstance(app.screen, Confirm))
            confirmation = app.screen
            assert isinstance(confirmation, Confirm)
            self.assertIn("模型次数/费用未知", confirmation.body)
            app.board.tasks[1].description = "确认期间被修改"
            await pilot.click("#submit")
            await wait_ui(pilot, lambda: app.screen is form and not form._saving)
            self.assertIn("已变化", str(form.query_one("#form-error", Static).content))
            self.assertEqual(form.query_one(TextArea).text, "只检查指定回执")
            self.assertEqual(encode(self.store.load()), before)
            self.assertEqual(self.commands(), [])

    async def test_known_agent_primary_commits_requirements_and_run_before_one_dispatch(self):
        agent = self.assign()
        app = Waggle(store=self.store, runtime=self.runtime)
        original = self.runtime.dispatch
        def committed(run):
            with Store(self.store.path) as observer:
                board = observer.load()
            self.assertEqual(board.tasks[1].remaining, review_requirements(
                "只完成这一项", "仅当前隔离测试项目的已存材料", "列出可核对的原件与验收结果"))
            self.assertEqual(board.runs[run.id], run)
            self.assertEqual(run.requirements, review_requirements(
                "只完成这一项", "仅当前隔离测试项目的已存材料", "列出可核对的原件与验收结果"))
            original(run)
        with patch.object(self.runtime, "dispatch", side_effect=committed) as dispatch:
            async with app.run_test(size=(80, 24)) as pilot:
                form = await self.open_note(app, pilot)
                scope = str(form.query_one("#note-scope-text", Static).content)
                for value in (self.task.title, self.task.description, agent.cwd, agent.id, agent.session_id):
                    self.assertIn(value, scope)
                for button in form.query(".dialog-buttons Button"):
                    self.assertTrue(form.query_one("#dialog").content_region.contains_region(button.region))
                form.query_one(TextArea).text = "只完成这一项"
                await pilot.press("ctrl+enter")
                await wait_ui(pilot, lambda: isinstance(app.screen, Confirm))
                self.assertEqual(app.board.runs, {})
                await pilot.click("#submit")
                await wait_ui(pilot, lambda: len(app.screen_stack) == 1 and len(app.board.runs) == 1)
                self.assertNotIsInstance(app.screen, Confirm)
                self.assertEqual(app.board.tasks[1].run, "queued")
                self.assertEqual(len(self.commands()), 1)
                dispatch.assert_called_once()

    async def test_next_real_review_requires_scope_and_verification_before_even_only_save(self):
        _, old = self.ended()
        app = Waggle(store=self.store, runtime=self.runtime)
        before = encode(self.store.load())
        async with app.run_test(size=(40, 20)) as pilot:
            await pilot.press("right", "b")
            await wait_ui(pilot, lambda: isinstance(app.screen, Choice))
            chooser = app.screen.query_one("#choices", OptionList)
            chooser.highlighted = next(index for index in range(chooser.option_count)
                                       if chooser.get_option_at_index(index).id == "review")
            await pilot.press("enter")
            await wait_ui(pilot, lambda: isinstance(app.screen, Note))
            form = app.screen
            assert isinstance(form, Note)
            form.query_one(TextArea).text = "交出可核对结果"
            await pilot.click("#save-only")
            self.assertIs(app.screen, form)
            self.assertEqual(encode(self.store.load()), before)
            form.query_one("#review-scope", Input).value = "只核旧项目中已存材料"
            form.query_one("#review-verification", Input).value = "列出原始回执和可重复检查结果"
            self.assertEqual((form.query_one(TextArea).text, form.query_one("#review-scope", Input).value,
                              form.query_one("#review-verification", Input).value),
                             ("交出可核对结果", "只核旧项目中已存材料", "列出原始回执和可重复检查结果"))
            self.assertFalse(form._saving)
            self.assertFalse(form.query_one("#save-only", Button).disabled)
            await pilot.pause()  # Reflow after the error text and both newly filled Inputs.
            for button in form.query(".dialog-buttons Button").results(Button):
                self.assertTrue(form.query_one("#dialog").content_region.contains_region(button.region))
            self.assertEqual(form.query_one("#save-only", Button).active_effect_duration, 0)
            self.assertTrue(await pilot.click("#save-only"))
            await pilot.pause(0.2)
            if len(app.screen_stack) != 1:
                self.fail(str(form.query_one("#form-error", Static).content) + "\n" +
                          "\n".join(strip.text for strip in form._compositor.render_strips()))
            saved = app.board.tasks[1]
            self.assertEqual(review_fields(saved.remaining), ("交出可核对结果", "只核旧项目中已存材料", "列出原始回执和可重复检查结果"))
            self.assertEqual(len(app.board.runs), 1)
            self.assertEqual(self.commands(), [])
            await pilot.press("s")
            await wait_ui(pilot, lambda: isinstance(app.screen, Confirm))
            confirmation = app.screen
            assert isinstance(confirmation, Confirm)
            self.assertIn("验收依据", confirmation.body)
            await pilot.press("enter")
            self.assertEqual(len(app.board.runs), 1)
            self.assertEqual(app.board.runs[old.id], old)

    async def test_ended_round_starts_new_identity_and_preserves_history(self):
        agent, old = self.ended()
        app = Waggle(store=self.store, runtime=self.runtime)
        async with app.run_test(size=(120, 30)) as pilot:
            form = await self.open_note(app, pilot)
            form.query_one(TextArea).text = "仅核对缺失证据"
            await pilot.click("#submit")
            await wait_ui(pilot, lambda: isinstance(app.screen, Confirm))
            await pilot.click("#submit")
            await wait_ui(pilot, lambda: len(app.screen_stack) == 1)
            new = app.board.runs[app.board.tasks[1].run_id]
            self.assertNotEqual(new.id, old.id)
            self.assertEqual((new.agent_id, new.session_id), (agent.id, agent.session_id))
            fields = review_fields(new.requirements)
            assert fields is not None
            self.assertEqual(fields[0], "仅核对缺失证据")
            self.assertEqual(app.board.runs[old.id], old)
            self.assertEqual(len(self.commands()), 1)

    async def test_mouse_cancel_dispatch_note_is_handled_once_without_save_or_execution(self):
        self.ended()
        app = Waggle(store=self.store, runtime=self.runtime)
        before = encode(self.store.load())
        with patch.object(self.runtime, "dispatch") as dispatch, patch.object(self.store, "save", wraps=self.store.save) as save:
            async with app.run_test(size=(100, 32)) as pilot:
                form = await self.open_note(app, pilot)
                form.query_one(TextArea).text = "未提交的草稿，取消不能保存"
                with patch.object(form, "action_cancel", wraps=form.action_cancel) as cancel:
                    self.assertTrue(await pilot.click("#cancel"))
                    await wait_ui(pilot, lambda: isinstance(app.screen, DiscardChanges))
                    await pilot.click("#discard-edit")
                    await wait_ui(pilot, lambda: len(app.screen_stack) == 1)
                    cancel.assert_called_once()
                self.assertTrue(app.is_running)
                self.assertEqual(encode(app.board), before)
                save.assert_not_called()
                dispatch.assert_not_called()
                self.assertEqual(self.commands(), [])
        self.assertEqual(encode(self.store.load()), before)

    async def test_duplicate_and_late_cancel_cannot_pop_another_screen(self):
        self.ended()
        app = Waggle(store=self.store, runtime=self.runtime)
        before = encode(self.store.load())
        with patch.object(self.runtime, "dispatch") as dispatch:
            async with app.run_test(size=(100, 32)) as pilot:
                form = await self.open_note(app, pilot)
                button = form.query_one("#cancel", Button)
                form.post_message(Button.Pressed(button))
                form.post_message(Button.Pressed(button))
                await wait_ui(pilot, lambda: isinstance(app.screen, DiscardChanges))
                await pilot.click("#discard-edit")
                await wait_ui(pilot, lambda: len(app.screen_stack) == 1)
                other = Confirm("另一个窗口", "旧窗口不得关闭此窗口", "确认")
                app.push_screen(other)
                await pilot.pause()
                form.on_button_pressed(Button.Pressed(button))
                form.action_cancel()
                self.assertIs(app.screen, other)
                self.assertEqual(len(app.screen_stack), 2)
                self.assertTrue(app.is_running)
                await pilot.press("escape")
                self.assertEqual(len(app.screen_stack), 1)
                self.assertEqual(encode(app.board), before)
                dispatch.assert_not_called()
        self.assertEqual(encode(self.store.load()), before)

    async def test_regular_note_puts_submit_before_cancel_without_auto_save(self):
        app = Waggle(store=self.store, runtime=self.runtime)
        saved = []
        before = encode(self.store.load())
        async with app.run_test(size=(80, 24)) as pilot:
            form = Note("只保存", "内容", save=saved.append)
            app.push_screen(form)
            await wait_ui(pilot, lambda: form.is_running and bool(form.query("#submit")))
            self.assertEqual([b.id for b in form.query(".dialog-buttons Button").results(Button)], ["submit", "cancel"])
            self.assertIs(form.focused, form.query_one(TextArea))
            self.assertEqual(saved, [])
            await pilot.press("escape")
            self.assertEqual(saved, [])
        self.assertEqual(encode(self.store.load()), before)

    async def test_submit_and_process_leads_the_form_button_row(self):
        self.ended()
        before = encode(self.store.load())
        app = Waggle(store=self.store, runtime=self.runtime)
        async with app.run_test(size=(80, 24)) as pilot:
            form = await self.open_note(app, pilot)
            buttons = list(form.query(".dialog-buttons Button").results(Button))
            self.assertEqual([button.id for button in buttons], ["submit", "save-only", "cancel"])
            self.assertEqual(len({button.region.y for button in buttons}), 1)
            self.assertEqual([button.region.x for button in buttons], sorted(button.region.x for button in buttons))
            self.assertIs(form.focused, form.query_one(TextArea))
            self.assertEqual(encode(app.board), before)
            await self.discard_note(app, pilot)
        self.assertEqual(encode(self.store.load()), before)
        self.assertEqual(self.commands(), [])

    async def test_only_save_and_cancel_never_dispatch(self):
        self.ended()
        app = Waggle(store=self.store, runtime=self.runtime)
        before = encode(self.store.load())
        with patch.object(self.runtime, "dispatch") as dispatch:
            async with app.run_test(size=(80, 24)) as pilot:
                form = await self.open_note(app, pilot)
                form.query_one(TextArea).text = "取消不保存"
                await self.discard_note(app, pilot)
                self.assertEqual(encode(self.store.load()), before)
                form = await self.open_note(app, pilot)
                form.query_one(TextArea).text = "先保存，下次再做"
                await pilot.click("#save-only")
                await wait_ui(pilot, lambda: len(app.screen_stack) == 1)
                fields = review_fields(app.board.tasks[1].remaining)
                assert fields is not None
                self.assertEqual(fields[0], "先保存，下次再做")
                self.assertEqual(app.board.tasks[1].run, "none")
                self.assertEqual(len(app.board.runs), 1)
                self.assertEqual(self.commands(), [])
                dispatch.assert_not_called()

    async def test_intent_save_failure_keeps_text_and_rolls_back_requirements(self):
        self.ended()
        app = Waggle(store=self.store, runtime=self.runtime)
        before = encode(self.store.load())
        with patch.object(self.runtime, "dispatch") as dispatch:
            async with app.run_test(size=(80, 24)) as pilot:
                form = await self.open_note(app, pilot)
                form.query_one(TextArea).text = "保存失败必须保留"
                with patch.object(self.store, "save", side_effect=StorageError("fixture read-only")):
                    await pilot.press("ctrl+enter")
                    await wait_ui(pilot, lambda: isinstance(app.screen, Confirm))
                    await pilot.click("#submit")
                    await wait_ui(pilot, lambda: not form._saving)
                    self.assertIs(app.screen, form)
                    self.assertIn("未保存", str(form.query_one("#form-error", Static).content))
                self.assertEqual(form.query_one(TextArea).text, "保存失败必须保留")
                self.assertEqual(encode(self.store.load()), before)
                dispatch.assert_not_called()
                self.assertEqual(self.commands(), [])

    async def test_uncertain_publish_keeps_one_intent_and_does_not_retry(self):
        self.assign()
        app = Waggle(store=self.store, runtime=self.runtime)
        original = self.runtime.dispatch
        def uncertain(run):
            original(run)
            raise OSError("lost dispatch receipt")
        with patch.object(self.runtime, "dispatch", side_effect=uncertain) as dispatch:
            async with app.run_test(size=(80, 24)) as pilot:
                form = await self.open_note(app, pilot)
                form.query_one(TextArea).text = "不要重放"
                await pilot.press("ctrl+enter")
                await wait_ui(pilot, lambda: isinstance(app.screen, Confirm))
                await pilot.click("#submit")
                await wait_ui(pilot, lambda: len(app.screen_stack) == 1)
                self.assertEqual(len(app.board.runs), 1)
                self.assertEqual(app.board.tasks[1].run, "queued")
                self.assertEqual(len(self.commands()), 1)
                await pilot.press("b")
                self.assertNotIsInstance(app.screen, Note)
                dispatch.assert_called_once()

    async def test_missing_agent_continues_selection_then_confirmation_without_s(self):
        agent = self.agent()
        app = Waggle(store=self.store, runtime=self.runtime)
        async with app.run_test(size=(80, 24)) as pilot:
            form = await self.open_note(app, pilot)
            form.query_one(TextArea).text = "选好执行者后处理"
            await pilot.press("ctrl+enter")
            await wait_ui(pilot, lambda: isinstance(app.screen, Choice))
            self.assertEqual(app.board.tasks[1].remaining, review_requirements(
                "选好执行者后处理", "仅当前隔离测试项目的已存材料", "列出可核对的原件与验收结果"))
            self.assertEqual(self.commands(), [])
            await pilot.press("enter")
            await wait_ui(pilot, lambda: isinstance(app.screen, Confirm))
            confirmation = app.screen
            assert isinstance(confirmation, Confirm)
            self.assertIn(agent.session_id, confirmation.body)
            self.assertIn("选好执行者后处理", confirmation.body)
            await pilot.click("#submit")
            await wait_ui(pilot, lambda: len(app.board.runs) == 1 and not app._sending)
            self.assertEqual(app.board.tasks[1].agent, agent.id)
            self.assertEqual(len(self.commands()), 1)

    async def test_missing_agent_selection_cancel_preserves_only_saved_requirements(self):
        self.agent()
        app = Waggle(store=self.store, runtime=self.runtime)
        async with app.run_test(size=(80, 24)) as pilot:
            form = await self.open_note(app, pilot)
            form.query_one(TextArea).text = "取消选择，保留说明"
            await pilot.press("ctrl+enter")
            await wait_ui(pilot, lambda: isinstance(app.screen, Choice))
            await pilot.press("escape")
            self.assertEqual(app.board.tasks[1].remaining, review_requirements(
                "取消选择，保留说明", "仅当前隔离测试项目的已存材料", "列出可核对的原件与验收结果"))
            self.assertEqual(app.board.runs, {})
            self.assertEqual(self.commands(), [])

    async def test_closed_form_ignores_late_primary_submission(self):
        self.assign()
        app = Waggle(store=self.store, runtime=self.runtime)
        before = encode(self.store.load())
        async with app.run_test(size=(80, 24)) as pilot:
            form = await self.open_note(app, pilot)
            form.query_one(TextArea).text = "取消的指令不能晚到派发"
            await self.discard_note(app, pilot)
            form.action_submit()
            await pilot.pause()
            self.assertEqual(encode(self.store.load()), before)
            self.assertEqual(self.commands(), [])

    async def test_changed_task_cannot_continue_to_new_agent(self):
        app = Waggle(store=self.store, runtime=self.runtime)
        async with app.run_test(size=(80, 24)) as pilot:
            form = await self.open_note(app, pilot)
            form.query_one(TextArea).text = "原范围"
            await pilot.press("ctrl+enter")
            await wait_ui(pilot, lambda: isinstance(app.screen, Choice))
            app.board.tasks[1].description = "选择期间发生变化"
            with patch.object(app, "new_agent_for") as create:
                await pilot.press("enter")
                create.assert_not_called()
            self.assertEqual(app.board.runs, {})
            self.assertEqual(self.commands(), [])

    async def test_prior_pi_pending_input_blocks_both_submit_modes(self):
        agent, old = self.ended()
        app = Waggle(store=self.store, runtime=self.runtime)
        before = encode(self.store.load())
        async with app.run_test(size=(80, 24)) as pilot:
            form = await self.open_note(app, pilot)
            form.query_one(TextArea).text = "不能越过原 Pi 的新输入"
            session_path = self.runtime.root / agent.id / "session.json"
            session_path.write_text(json.dumps(session_record(old, seq=2, input_pending=True)), encoding="utf-8")
            session_path.chmod(0o600)
            await pilot.press("ctrl+enter")
            await wait_ui(pilot, lambda: not form._saving)
            self.assertIs(app.screen, form)
            self.assertIn("新输入", str(form.query_one("#form-error", Static).content))
            await pilot.click("#save-only")
            self.assertIs(app.screen, form)
            self.assertEqual(encode(self.store.load()), before)
            self.assertEqual(self.commands(), [])

    async def test_readonly_round_can_only_save_not_dispatch(self):
        _, old = self.ended()
        self.runtime.observe(old, self.runtime.root)
        app = Waggle(store=self.store, runtime=self.runtime)
        before = encode(self.store.load())
        async with app.run_test(size=(80, 24)) as pilot:
            form = await self.open_note(app, pilot)
            form.query_one(TextArea).text = "保留只读边界"
            await pilot.press("ctrl+enter")
            await wait_ui(pilot, lambda: not form._saving)
            self.assertIs(app.screen, form)
            self.assertIn("只读", str(form.query_one("#form-error", Static).content))
            self.assertEqual(encode(self.store.load()), before)
            self.assertEqual(self.commands(), [])
            await pilot.click("#save-only")
            fields = review_fields(app.board.tasks[1].remaining)
            assert fields is not None
            self.assertEqual(fields[0], "保留只读边界")
            self.assertEqual(len(app.board.runs), 1)

    async def test_selection_can_create_and_start_without_returning_to_s(self):
        def create(runtime, directory, name, model, identity):
            launch = prepare_launch(runtime, directory, name, model, identity)
            self.agent(identity=launch.id, session=launch.session_id, name=name)
            launch.state, launch.tab_id, launch.pane_id = "starting", "fixture-tab", "fixture-pane"
            return launch
        app = Waggle(store=self.store, runtime=self.runtime)
        with patch("app.create_pi", side_effect=create) as creation:
            async with app.run_test(size=(80, 24)) as pilot:
                form = await self.open_note(app, pilot)
                form.query_one(TextArea).text = "新建后仍是这份要求"
                await pilot.press("ctrl+enter")
                await wait_ui(pilot, lambda: isinstance(app.screen, Choice))
                await pilot.press("enter")
                await wait_ui(pilot, lambda: isinstance(app.screen, NewAgentForm))
                await pilot.click("#create-start")
                self.assertIsInstance(app.screen, Confirm)
                await pilot.press("ctrl+enter")
                await wait_ui(pilot, lambda: len(app.board.runs) == 1 and len(app.screen_stack) == 1)
                self.assertEqual(next(iter(app.board.runs.values())).requirements, review_requirements(
                    "新建后仍是这份要求", "仅当前隔离测试项目的已存材料", "列出可核对的原件与验收结果"))
                creation.assert_called_once()
                self.assertEqual(len(self.commands()), 1)

    async def test_task_or_directory_change_rejects_submit_without_persistence(self):
        self.assign()
        before = encode(self.store.load())
        for change in ("task", "directory"):
            with self.subTest(change=change):
                app = Waggle(store=self.store, runtime=self.runtime)
                async with app.run_test(size=(80, 24)) as pilot:
                    form = await self.open_note(app, pilot)
                    form.query_one(TextArea).text = "只接受已展示的范围"
                    if change == "task":
                        app.board.tasks[1].description = "修改后的范围"
                    else:
                        app.board.projects[self.task.project] = str(self.root)
                    await pilot.press("ctrl+enter")
                    await wait_ui(pilot, lambda form=form: not form._saving)
                    self.assertIs(app.screen, form)
                    self.assertIn("已变化", str(form.query_one("#form-error", Static).content))
                    self.assertEqual(form.query_one(TextArea).text, "只接受已展示的范围")
                    self.assertEqual(encode(self.store.load()), before)
                    self.assertEqual(self.commands(), [])

    async def test_long_save_error_keeps_all_controls_and_text_accessible(self):
        self.assign()
        app = Waggle(store=self.store, runtime=self.runtime)
        before = encode(self.store.load())
        async with app.run_test(size=(80, 24)) as pilot:
            form = await self.open_note(app, pilot)
            form.query_one(TextArea).text = "失败后仍能选择仅保存或复制"
            with patch.object(self.store, "save", side_effect=StorageError("权限问题和完整路径说明。" * 40)):
                await pilot.press("ctrl+enter")
                await wait_ui(pilot, lambda: isinstance(app.screen, Confirm))
                await pilot.click("#submit")
                await wait_ui(pilot, lambda: not form._saving)
            self.assertIs(app.screen, form)
            self.assertEqual(form.query_one(TextArea).text, "失败后仍能选择仅保存或复制")
            self.assertFalse(form.query_one(TextArea).read_only)
            for button in form.query(".dialog-buttons Button"):
                self.assertTrue(form.query_one("#dialog").content_region.contains_region(button.region))
            self.assertEqual(encode(self.store.load()), before)
            self.assertEqual(self.commands(), [])

    async def test_agent_session_change_preserves_input_and_does_not_write(self):
        agent = self.assign()
        app = Waggle(store=self.store, runtime=self.runtime)
        before = encode(self.store.load())
        async with app.run_test(size=(80, 24)) as pilot:
            form = await self.open_note(app, pilot)
            form.query_one(TextArea).text = "只允许已展示的会话"
            path = self.runtime.root / agent.id / "agent.json"
            path.unlink()
            publish(path, {"version": 1, **asdict(replace(agent, session_id=str(uuid4())))})
            await pilot.press("ctrl+enter")
            await wait_ui(pilot, lambda: not form._saving)
            self.assertIs(app.screen, form)
            self.assertIn("会话", str(form.query_one("#form-error", Static).content))
            self.assertEqual(form.query_one(TextArea).text, "只允许已展示的会话")
            self.assertEqual(encode(self.store.load()), before)
            self.assertEqual(self.commands(), [])
