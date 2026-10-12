"""N3 checks with disposable SQLite, report sources and private mailbox."""
import json
import os
import time
import unittest
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from textual.containers import VerticalScroll
from textual.widgets import Button, Select, SelectionList, Static, TextArea

from app import Confirm, DispatchNote, Waggle
from decision_view import DecisionReview, DecisionSheet, DiscardChanges
from decisions import (
    Answer,
    BusinessAnswer,
    DecisionSpec,
    Target,
    feedback_answers,
    prepare_decision_intent,
    preview_decisions,
)
from journal import Report, encoded_event
from runtime import Agent, Runtime, SessionObservation, private_dir
from store import StorageError, Store, decode, encode
from test_capabilities import capability_record
from test_decisions import decision_board, spec_record
from test_live import wait_ui


def fact_spec(**changes):
    return DecisionSpec.from_record(spec_record(human_input={
        'kind': 'choice', 'question': 'Fixture order: must reverse before settlement?',
        'options': [{'key': 'yes', 'label': '需要'}, {'key': 'no', 'label': '不需要'},
                    {'key': 'conditional', 'label': '有条件'}, {'key': 'unknown', 'label': '不确定'}]}, **changes))


class IntentContracts(unittest.TestCase):
    def test_feedback_dispatch_omits_unselected_questions_but_keeps_whole_sheet_binding(self):
        specs = (DecisionSpec.from_record(spec_record(missing=['receipt'])),
                 DecisionSpec.from_record(spec_record(key='other', question='UNSELECTED QUESTION', missing=['owner'])))
        board, task, run = decision_board(specs)
        answers = feedback_answers(specs, (Answer(comment='Only explain existing evidence'), Answer()))
        preview = preview_decisions(board, task.id, answers, Target('new_run', run.agent_id, run.session_id, run.cwd))
        self.assertNotIn('UNSELECTED QUESTION', preview.compiled_text)
        self.assertEqual(preview.specs, specs)
        self.assertEqual(preview.answers[1], Answer())
        prepared = prepare_decision_intent(board, preview)
        assert prepared.run is not None
        self.assertEqual(prepared.run.requirements, preview.compiled_text)
        self.assertEqual(decode(encode(board)).decisions[prepared.record.id], prepared.record)

    def test_unselected_formal_history_not_copied_into_confirmed_feedback(self):
        specs = (DecisionSpec.from_record(spec_record()), DecisionSpec.from_record(spec_record(key='other')))
        board, task, _ = decision_board(specs)
        old = prepare_decision_intent(board, preview_decisions(board, task.id,
            (Answer('choice', 'a'), Answer('choice', 'a')), Target('saved'))).record
        later = preview_decisions(board, task.id, (Answer(), Answer('request_info', comment='Explain only')), Target('saved'))
        self.assertEqual(later.prior, ('', ''))
        self.assertNotIn(old.id, later.compiled_text)

    def test_unselected_historical_formal_answer_cannot_enter_other_formal_confirmation(self):
        specs = (DecisionSpec.from_record(spec_record()), DecisionSpec.from_record(spec_record(key='other')))
        board, task, run = decision_board(specs)
        prepare_decision_intent(board, preview_decisions(board, task.id,
            (Answer('choice', 'a'), Answer('choice', 'b')), Target('saved')))
        before = encode(board)
        for target in (Target('saved'), Target('new_run', run.agent_id, run.session_id, run.cwd)):
            with self.subTest(target=target.kind), self.assertRaisesRegex(ValueError, '整单|未选'):
                preview_decisions(board, task.id, (Answer(), Answer('choice', 'a')), target)
        self.assertEqual(encode(board), before)

    def test_mixed_formal_and_feedback_requires_separate_confirmation(self):
        specs = (DecisionSpec.from_record(spec_record()), DecisionSpec.from_record(spec_record(key='other')))
        board, task, run = decision_board(specs)
        before = encode(board)
        for target in (Target('saved'), Target('new_run', run.agent_id, run.session_id, run.cwd)):
            with self.subTest(target=target.kind), self.assertRaisesRegex(ValueError, '分别|拆分'):
                preview_decisions(board, task.id, (Answer('choice', 'a'), Answer('request_info')), target)
        self.assertEqual(encode(board), before)


class IntentUI(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory(prefix='waggle-n3-')))
        self.project = self.root / 'project'
        self.project.mkdir()
        self.board, self.task, self.source_run = decision_board([fact_spec()])
        self.board.projects[self.task.project] = str(self.project)
        self.source_run.cwd = str(self.project)
        self.store = self.enterContext(Store(self.root / 'tasks.sqlite3'))
        self.store.load()
        # Finish any customized initial report before the first persistence.
        self.runtime = Runtime(self.root / 'bridges')
        self.agent = Agent(self.source_run.agent_id, self.source_run.session_id, str(self.project), 'Fixture',
                           'offline/test', os.getpid(), int(time.time() * 1000), True, True, '')
        self.session = SessionObservation(self.agent.id, self.agent.session_id, self.agent.cwd, 1,
            int(time.time() * 1000), 'idle', self.source_run.id, '', False, False)
        self.endpoint = self.runtime.root / self.agent.id
        for path in (self.runtime.root, self.endpoint, self.endpoint / 'commands', self.endpoint / 'runs'):
            private_dir(path, create=True)
        self.write_observation()

    def write_observation(self):
        now = int(time.time() * 1000)
        self.agent = replace(self.agent, heartbeat=now)
        self.session = replace(self.session, observed_at=now)
        for name, row in [('agent.json', {'version': 1, **asdict(self.agent)}),
                          ('session.json', {'version': 1, **asdict(self.session)}),
                          ('report-capabilities.json', capability_record(self.agent))]:
            path = self.endpoint / name
            path.write_text(json.dumps(row), encoding='utf-8')
            path.chmod(0o600)

    def seed_fixture(self):
        if self.store.revision == 0:
            self.store.save(self.board)

    def app(self):
        self.seed_fixture()
        app = Waggle(store=self.store, runtime=self.runtime)
        app.agents[self.agent.id] = self.agent
        app.sessions[self.source_run.id] = self.session
        self.enterContext(patch.object(app, 'schedule_poll'))
        return app

    async def sheet(self, app, pilot):
        app.selected_id = self.task.id
        app.refresh_tasks()
        app.action_decisions()
        await wait_ui(pilot, lambda: isinstance(app.screen, DecisionSheet))
        return app.screen

    async def review(self, app, pilot, kind):
        form = app.screen
        form.request(kind)
        await wait_ui(pilot, lambda: isinstance(app.screen, DecisionReview))
        return app.screen

    async def confirm(self, app, pilot):
        await pilot.click('#decision-confirm')
        await wait_ui(pilot, lambda: len(app.screen_stack) == 1)

    async def test_sheet_actions_name_whole_sheet_and_review_names_receiver(self):
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            form = await self.sheet(app, pilot)
            form.query_one('#intent-0', Select).value = 'feedback'
            form.query_one('#comment-0', TextArea).text = '请解释已存材料的来源'
            await pilot.pause()
            self.assertIn('本单', str(form.query_one('#decision-feedback-0', Button).label))
            self.assertIn('本单', str(form.query_one('#decision-continue', Button).label))
            await self.review(app, pilot, 'feedback')
            target = str(app.screen.query_one('#decision-target', Static).content)
            self.assertIn(self.agent.name, target)
            self.assertIn(str(self.project), target)
            self.assertIn('新一轮', target)
            assert app.screen.focused is not None
            self.assertEqual(app.screen.focused.id, 'decision-back')
            self.assertEqual(list((self.endpoint / 'commands').glob('*.json')), [])

    async def test_closing_changed_decision_sheet_defaults_to_keep_editing(self):
        app = self.app()
        before = encode(self.store.load())
        async with app.run_test(size=(40, 18)) as pilot:
            form = await self.sheet(app, pilot)
            form.query_one('#intent-0', Select).value = 'feedback'
            text = '尚未确认的反馈\n请解释来源'
            form.query_one('#comment-0', TextArea).text = text
            await pilot.pause()
            await pilot.press('escape')
            self.assertEqual(type(app.screen).__name__, 'DiscardChanges')
            await pilot.press('enter')
            await wait_ui(pilot, lambda: app.screen is form)
            self.assertEqual(form.query_one('#comment-0', TextArea).text, text)
            await pilot.press('escape')
            await pilot.click('#discard-edit')
            await wait_ui(pilot, lambda: len(app.screen_stack) == 1)
            self.assertEqual(encode(self.store.load()), before)
            self.assertEqual(list((self.endpoint / 'commands').glob('*.json')), [])

    async def check_keep_editing_input_routes(self, size):
        app = self.app()
        before = encode(self.store.load())
        async with app.run_test(size=size) as pilot:
            form = await self.sheet(app, pilot)
            form.query_one('#intent-0', Select).value = 'feedback'
            text = '未保存的反馈\n保留原始选择与文字，不确认或发送'
            form.query_one('#comment-0', TextArea).text = text
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
                    self.assertEqual(form.query_one('#comment-0', TextArea).text, text)
                    self.assertEqual(form.query_one('#intent-0', Select).value, 'feedback')
                    self.assertIsNotNone(form.focused)
                    assert form.focused is not None
                    self.assertEqual(form.focused.id, 'decision-overview')
                    self.assertEqual(encode(self.store.load()), before)
                    self.assertEqual(list((self.endpoint / 'commands').glob('*.json')), [])
            # Reopening on every route exercises guard reset through the public UI.
            await pilot.press('escape')
            self.assertIsInstance(app.screen, DiscardChanges)
            self.assertTrue(await pilot.click('#discard-edit'))
            await wait_ui(pilot, lambda: len(app.screen_stack) == 1)
        self.assertEqual(encode(self.store.load()), before)

    async def test_keep_editing_input_routes_reset_guard_at_standard_size(self):
        await self.check_keep_editing_input_routes((80, 24))

    async def test_keep_editing_input_routes_at_compact_and_large_sizes(self):
        for size in ((40, 18), (120, 40)):
            await self.check_keep_editing_input_routes(size)

    async def test_mouse_return_keeps_unsaved_sheet_editable_and_reopens_prompt(self):
        app = self.app()
        before = encode(self.store.load())
        revision = self.store.revision
        async with app.run_test(size=(107, 63)) as pilot:
            form = await self.sheet(app, pilot)
            try:
                form.query_one('#intent-0', Select).value = 'fact'
                form.query_one('#business-choice-0', Select).value = 'yes'
                form.query_one('#comment-0', TextArea).text = '未保存的业务说明\n保留选择和原文，不确认或发送'
                await pilot.pause()
                scratch = form.edit_values()
                for _ in range(2):
                    # Mouse OPEN matters: its result callback belongs to the sheet,
                    # unlike the existing Escape-opened modal route checks.
                    self.assertTrue(await pilot.click('#decision-close'))
                    await wait_ui(pilot, lambda: isinstance(app.screen, DiscardChanges))
                    framework_closing = form._closing
                    prompt_open = getattr(form, '_discard_prompt_open', None)
                    self.assertTrue(await pilot.click('#keep-editing'))
                    await wait_ui(pilot, lambda: app.screen is form)
                    await wait_ui(pilot, lambda: form.focused is not None
                                  and form.focused.id == 'decision-overview', timeout=2)
                    self.assertEqual(form.edit_values(), scratch)
                    self.assertFalse(framework_closing)
                    self.assertTrue(prompt_open)
                    self.assertFalse(form._closing)
                    self.assertFalse(form._discard_prompt_open)
                    self.assertEqual(encode(self.store.load()), before)
                    self.assertEqual(self.store.revision, revision)
                    self.assertEqual(list(self.runtime.root.glob('*/commands/*.json')), [])
                    self.assertEqual(list(self.runtime.root.glob('*/supplements/requests/*.json')), [])
                self.assertTrue(await pilot.click('#decision-close'))
                await wait_ui(pilot, lambda: isinstance(app.screen, DiscardChanges))
                self.assertFalse(form._closing)
                self.assertTrue(form._discard_prompt_open)
                self.assertTrue(await pilot.click('#discard-edit'))
                await wait_ui(pilot, lambda: len(app.screen_stack) == 1)
            except Exception:
                # Teardown only after a failed verdict: the original guard collision
                # also blocks Textual shutdown. Never repair it before assertions.
                form._closing = False
                raise
        self.assertEqual(encode(self.store.load()), before)
        self.assertEqual(self.store.revision, revision)
        self.assertEqual(list(self.runtime.root.glob('*/commands/*.json')), [])
        self.assertEqual(list(self.runtime.root.glob('*/supplements/requests/*.json')), [])

    async def width_probe(self, size):
        app = self.app()
        before = encode(self.store.load())
        async with app.run_test(size=size) as pilot:
            form = await self.sheet(app, pilot)
            self.assertEqual(form.query_one('#intent-0', Select).value, '')
            self.assertEqual(form.intent_options(0)[0], ('暂未选择', ''))
            headings = [str(widget.content) for widget in form.query('.decision-section-heading').results(Static)]
            self.assertIn('本次答复类型', headings)
            explanation = '未选择的条目不会发送；选择类型不等于保存或发送。'
            guide = next(widget for widget in form.query('.decision-meta').results(Static)
                         if str(widget.content) == explanation)
            guide.scroll_visible(top=True, animate=False)
            await pilot.pause()
            rendered = ''.join(strip.text.strip() for strip in guide.render_lines(guide.region.size.region))
            self.assertIn(explanation, rendered)
            self.assertEqual(form.answers(), (Answer(),))
            self.assertTrue(form.query_one('#decision-saved', Button).disabled)
            self.assertFalse(form.query_one('#decision-fact-0').display)
            self.assertFalse(form.query_one('#decision-comment-0').display)
            self.assertTrue(form.query_one('#decision-continue', Button).disabled)
            for button in form.query('.decision-controls Button').results(Button):
                self.assertTrue(form.query_one('#decision-sheet').content_region.contains_region(button.region))
            form.query_one('#intent-0', Select).scroll_visible(top=True, animate=False)
            form.query_one('#intent-0', Select).focus()
            await pilot.press('enter', 'down', 'down', 'enter')  # Explicit feedback, not facts.
            await pilot.pause()
            self.assertEqual(form.query_one('#intent-0', Select).value, 'feedback')
            form.query_one('#comment-0', TextArea).text = '能\nPlease explain existing evidence only'
            await pilot.pause()
            answer = form.answers()[0]
            self.assertIsNone(answer.business)
            self.assertEqual(answer.kind, 'undecided')
            form.query_one('#decision-body', VerticalScroll).scroll_end(animate=False)
            await pilot.pause()
            self.assertTrue(await pilot.click('#decision-saved'))
            await wait_ui(pilot, lambda: isinstance(app.screen, DecisionReview))
            review = app.screen
            assert isinstance(review, DecisionReview)
            self.assertIn('有确认记录', review.summary())
            self.assertIn('反馈', review.summary())
            self.assertIsNone(review.preview.answers[0].business)
            await pilot.press('escape')
            await wait_ui(pilot, lambda: app.screen is form)
            self.assertEqual(form.query_one('#comment-0', TextArea).text, '能\nPlease explain existing evidence only')
            self.assertFalse(app.board.decisions)
        self.assertEqual(encode(self.store.load()), before)
        self.assertFalse(list(self.endpoint.glob('commands/*.json')))

    async def test_intent_keyboard_mouse_40(self):
        await self.width_probe((40, 18))

    async def test_intent_keyboard_mouse_80(self):
        await self.width_probe((80, 24))

    async def test_intent_keyboard_mouse_wide(self):
        await self.width_probe((140, 40))

    async def test_conditional_only_shows_and_checks_for_fact_intent(self):
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            form = await self.sheet(app, pilot)
            form.query_one('#intent-0', Select).value = 'fact'
            form.query_one('#business-choice-0', Select).value = 'conditional'
            await pilot.pause()
            self.assertTrue(form.query_one('#business-detail-0').display)
            form.request('continue')
            self.assertIs(app.screen, form)
            self.assertIn('条件', str(form.query_one('#decision-error', Static).content))
            form.query_one('#business-detail-0').value = 'Only if fixture settlement is confirmed'
            self.assertIsInstance(form.answers()[0].business, BusinessAnswer)
            form.query_one('#intent-0', Select).value = 'feedback'
            await pilot.pause()
            self.assertFalse(form.query_one('#decision-fact-0').display)
            self.assertIsNone(form.answers()[0].business)
            self.assertEqual(form.query_one('#business-detail-0').value, 'Only if fixture settlement is confirmed')
        self.assertFalse(list(self.endpoint.glob('commands/*.json')))

    async def test_saved_business_fact_is_not_labelled_formal_decision(self):
        app = self.app()
        original = asdict(self.source_run)
        async with app.run_test(size=(80, 24)) as pilot:
            form = await self.sheet(app, pilot)
            form.query_one('#intent-0', Select).value = 'fact'
            form.query_one('#business-choice-0', Select).value = 'yes'
            await pilot.pause()
            await self.review(app, pilot, 'saved')
            await self.confirm(app, pilot)
            form = await self.sheet(app, pilot)
            previous = str(form.query_one('#decision-previous-0', Static).content)
            self.assertIn('此前业务事实', previous)
            self.assertNotIn('此前正式决定', previous)
            self.assertIn('未发送', previous)
            self.assertEqual(asdict(app.board.runs[self.source_run.id]), original)
        self.assertFalse(list(self.endpoint.glob('commands/*.json')))

    async def test_formal_saved_has_confirmation_record_zero_dispatch_and_no_autofill(self):
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            form = await self.sheet(app, pilot)
            form.query_one('#intent-0', Select).value = 'formal'
            await pilot.pause()
            form.query_one('#answer-0', Select).value = 'choice:a'
            review = await self.review(app, pilot, 'saved')
            self.assertIn('有确认记录', review.summary())
            self.assertIn('正式', review.summary())
            await self.confirm(app, pilot)
            record = list(app.board.decisions.values())[-1]
            self.assertTrue(record.confirmed_at)
            self.assertEqual(record.preview.answers[0].kind, 'choice')
            self.assertEqual(len(app.board.runs), 1)
            form = await self.sheet(app, pilot)
            self.assertEqual(form.query_one('#intent-0', Select).value, '')
            self.assertEqual(form.answers(), (Answer(),))
            self.assertTrue(form.query_one('#decision-continue', Button).disabled)
        self.assertEqual(len(self.store.load().decisions), 1)
        self.assertFalse(list(self.endpoint.glob('commands/*.json')))

    async def test_length_save_error_and_source_drift_preserve_multiline_input(self):
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            form = await self.sheet(app, pilot)
            form.query_one('#intent-0', Select).value = 'feedback'
            area = form.query_one('#comment-0', TextArea)
            area.text = 'x' * 501
            form.request('saved')
            self.assertIs(app.screen, form)
            self.assertEqual(area.text, 'x' * 501)
            area.text = 'Keep this\nfixture question'
            review = await self.review(app, pilot, 'saved')
            with patch.object(self.store, 'save', side_effect=StorageError('fixture disk failure')):
                await pilot.click('#decision-confirm')
                await wait_ui(pilot, lambda: not review.busy)
            self.assertIs(app.screen, review)
            self.assertFalse(app.board.decisions)
            await pilot.press('escape')
            await wait_ui(pilot, lambda: app.screen is form)
            self.assertEqual(area.text, 'Keep this\nfixture question')
            source = app.board.runs[self.source_run.id]
            event = source.events[0]
            source.events[0] = event.replace('待决策报告', 'source changed')
            form.request('saved')
            await wait_ui(pilot, lambda: not form.busy)
            self.assertIs(app.screen, form)
            self.assertEqual(area.text, 'Keep this\nfixture question')
            self.assertFalse(app.board.decisions)
        self.assertFalse(list(self.endpoint.glob('commands/*.json')))

    async def test_historical_question_picker_has_full_source_defaults_empty_and_sends_only_selected(self):
        specs = (DecisionSpec.from_record(spec_record(question='Complete historical question: existing receipt interpretation?')),
                 DecisionSpec.from_record(spec_record(key='other', question='UNSELECTED historical question')))
        self.source_run.events = [encoded_event('partial', 'Fixture history', source='executor', run_id=self.source_run.id, seq=1,
            report=Report(self.source_run.summary, self.source_run.checks, self.source_run.remaining, decisions=specs))]
        self.store.save(self.board)  # Persist the actual source before appending a confirmation record.
        prepare_decision_intent(self.board, preview_decisions(self.board, self.task.id,
            (Answer(comment='Long selected reply: ' + 'evidence ' * 10), Answer(comment='UNSELECTED old reply')), Target('saved')))
        self.store.save(self.board)
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            app.selected_id = self.task.id
            app.refresh_tasks()
            app.run_worker(app.open_dispatch_supplement(self.task.id, intent='proposal'))
            await wait_ui(pilot, lambda: isinstance(app.screen, DispatchNote))
            form = app.screen
            assert isinstance(form, DispatchNote)
            choices = form.query_one('#prior-feedback', SelectionList)
            self.assertEqual(choices.selected, [])
            key, label = form.feedback_options[0]
            self.assertIn(specs[0].question, label)
            self.assertIn(('evidence ' * 10).strip(), label)
            self.assertIn('来源：', label)
            self.assertIn('状态：已确认仅保存', label)
            self.assertIn('默认不选', label)
            choices.select(key)
            form.query_one('#note', TextArea).text = 'Only explain the selected existing receipt'
            await pilot.press('ctrl+enter')
            await wait_ui(pilot, lambda: isinstance(app.screen, Confirm))
            review = app.screen
            assert isinstance(review, Confirm)
            self.assertIn(specs[0].question, review.body)
            self.assertNotIn('UNSELECTED historical question', review.body)
            self.assertNotIn('UNSELECTED old reply', review.body)
            await pilot.click('#submit')
            await wait_ui(pilot, lambda: len(app.screen_stack) == 1)
            run = list(app.board.runs.values())[-1]
            self.assertIn(specs[0].question, run.requirements)
            self.assertNotIn('UNSELECTED old reply', run.requirements)
            self.assertEqual(len(app.board.runs), 2)
            self.assertEqual(len(list(self.endpoint.glob('commands/*.json'))), 1)

    async def test_intent_switch_preserves_local_fields_without_carrying_hidden_grants(self):
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            form = await self.sheet(app, pilot)
            form.query_one('#intent-0', Select).value = 'formal'
            await pilot.pause()
            form.query_one('#answer-0', Select).value = 'choice:a'
            form.query_one('#intent-0', Select).value = 'feedback'
            await pilot.pause()
            form.query_one('#comment-0', TextArea).text = 'Only explain\nexisting evidence'
            self.assertEqual(form.answers()[0].kind, 'undecided')
            self.assertIsNone(form.answers()[0].business)
            form.query_one('#intent-0', Select).value = ''
            await pilot.pause()
            self.assertEqual(form.answers(), (Answer(),))
            form.query_one('#intent-0', Select).value = 'formal'
            await pilot.pause()
            self.assertEqual(form.query_one('#answer-0', Select).value, 'choice:a')
            self.assertEqual(form.query_one('#comment-0', TextArea).text, 'Only explain\nexisting evidence')
            self.assertFalse(app.board.decisions)
        self.assertFalse(list(self.endpoint.glob('commands/*.json')))

    async def test_only_feedback_one_new_round_exact_preview_and_old_run_immutable(self):
        app = self.app()
        old = deepcopy(asdict(self.source_run))
        async with app.run_test(size=(80, 24)) as pilot:
            form = await self.sheet(app, pilot)
            form.query_one('#intent-0', Select).value = 'feedback'
            form.query_one('#comment-0', TextArea).text = 'Explain existing fixture receipt only'
            review = await self.review(app, pilot, 'continue')
            compiled = review.preview.compiled_text
            self.assertTrue(compiled.startswith('仅补证请求：'))
            await self.confirm(app, pilot)
            runs = list(app.board.runs.values())
            self.assertEqual(len(runs), 2)
            self.assertEqual(asdict(app.board.runs[self.source_run.id]), old)
            self.assertEqual(runs[-1].requirements, compiled)
            self.assertNotEqual(runs[-1].id, self.source_run.id)
            self.assertEqual(len(list(self.endpoint.glob('commands/*.json'))), 1)


if __name__ == '__main__':
    unittest.main()
