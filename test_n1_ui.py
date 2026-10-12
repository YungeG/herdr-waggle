"""Follow-up/first-dispatch UI integration with disposable databases and mailboxes."""
import os
import shutil
import time
import unittest
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from textual.widgets import Button, Input, Select, SelectionList, Static, TextArea

from app import Confirm, FollowupForm, Waggle, WorkContractForm
from journal import current_report, parse_event
from runtime import Agent, Runtime, private_dir, publish
from store import StorageError, Store
from task_context import WorkContract, create_followup_context, parse_compiled_contract
from test_bridge import PiFixture
from test_capabilities import capability_record
from test_live import wait_ui
from test_task_context import _make_accepted_parent


class N1UI(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory(prefix='waggle-n1-ui-')))
        self.project = self.root / '项目'
        self.project.mkdir()
        self.runtime = Runtime(self.root / 'bridges')
        self.store = self.enterContext(Store(self.root / 'tasks.sqlite3'))
        self.board = self.store.load()
        self.board.add_project('数据实验室', str(self.project))
        self.parent = _make_accepted_parent(self.board)
        self.store.save(self.board)

    def child(self):
        report = current_report(self.parent, self.board.runs[self.parent.run_id])
        assert report is not None
        acceptance = next(e for raw in self.parent.events if (e := parse_event(raw)) and e.kind == 'accepted')
        ctx = create_followup_context(self.board, self.parent, report.id, acceptance.id,
                                      ['summary', 'checks'], '明确后续', '独立目标')
        child = self.board.create_followup(self.parent.id, ctx, '明确后续', '独立目标')
        self.store.save(self.board)
        return child

    def agent(self, child):
        agent = Agent(str(uuid4()), str(uuid4()), str(self.project), '隔离 Pi', 'offline/fixture',
                      os.getpid(), int(time.time() * 1000), True, True, '')
        endpoint = self.runtime.root / agent.id
        for path in (self.runtime.root, endpoint, endpoint / 'commands', endpoint / 'runs'):
            private_dir(path, create=True)
        publish(endpoint / 'agent.json', {'version': 1, **asdict(agent)})
        publish(endpoint / 'report-capabilities.json', capability_record(agent))
        self.board.assign(child, agent.id)
        self.store.save(self.board)
        return agent

    def fill_contract(self, form, purpose='inventory'):
        form.query_one('#contract-purpose', Select).value = purpose
        for key, value in [('deliverable', '只盘点已有成果'), ('allowed_scope', '仅当前项目已存材料'),
                           ('verification', '清单逐项标明来源'), ('exclusions', '不修改、不下载、不运行试验')]:
            form.query_one('#contract-' + key, TextArea).text = value

    async def test_followup_palette_selects_explicit_data_and_saves_only_at_40_80_wide(self):
        parent_bytes = deepcopy(self.board.tasks[self.parent.id])
        old_run = deepcopy(self.board.runs[self.parent.run_id])
        for size in ((40, 18), (80, 24), (140, 40)):
            app = Waggle(store=self.store, runtime=self.runtime)
            async with app.run_test(size=size) as pilot:
                app.selected_id = self.parent.id
                app.refresh_tasks()
                await pilot.press('ctrl+k', *'后续', 'enter')
                await wait_ui(pilot, lambda: isinstance(app.screen, FollowupForm))
                form = app.screen
                self.assertEqual(form.query_one('#followup-refs', SelectionList).selected, [])
                form.query_one('#followup-title', Input).value = '用户自己的标题'
                form.query_one('#followup-goal', TextArea).text = '用户自己的独立目标'
                await pilot.press('ctrl+enter')
                self.assertIs(app.screen, form)
                self.assertIn('明确选择', str(form.query_one('#form-error', Static).content))
                form.query_one('#followup-refs', SelectionList).select('summary')
                for button in form.query('.dialog-buttons Button').results(Button):
                    self.assertTrue(form.query_one('#dialog').content_region.contains_region(button.region))
                await pilot.click('#submit')
                await wait_ui(pilot, lambda: len(app.screen_stack) == 1)
                child = app.selected_task
                assert child is not None
                self.assertEqual((child.title, child.description, child.status, child.run),
                                 ('用户自己的标题', '用户自己的独立目标', 'draft', 'none'))
                self.assertEqual(app.board.tasks[self.parent.id], parent_bytes)
                self.assertEqual(app.board.runs[self.parent.run_id], old_run)
                self.assertEqual(len(app.board.runs), 1)
                context = next(ctx for ctx in app.board.task_contexts.values() if ctx.child_task_id == child.id)
                self.assertEqual(context.selected_refs, ('summary',))
                self.assertFalse(list(self.runtime.root.glob('*/commands/*.json')))

    async def test_followup_source_drift_or_save_failure_keeps_input_and_no_new_task(self):
        for failure in ('source', 'store'):
            app = Waggle(store=self.store, runtime=self.runtime)
            async with app.run_test(size=(80, 24)) as pilot:
                app.action_followup()
                await pilot.pause()
                form = app.screen
                form.query_one('#followup-title', Input).value = '保留标题'
                form.query_one('#followup-goal', TextArea).text = '保留目标'
                form.query_one('#followup-refs', SelectionList).select('summary')
                if failure == 'source':
                    app.board.runs[self.parent.run_id].requirements += '\n源已改变'
                    await pilot.press('ctrl+enter')
                else:
                    with patch.object(self.store, 'save', side_effect=StorageError('磁盘失败')):
                        await pilot.press('ctrl+enter')
                self.assertIs(app.screen, form)
                self.assertEqual(form.query_one('#followup-goal', TextArea).text, '保留目标')
                self.assertEqual(len(self.store.load().tasks), 1)
                self.assertEqual(len(app.board.tasks), 1)
                await pilot.press('escape')

    async def test_missing_first_contract_save_only_cancel_then_exact_preflight_commit(self):
        child = self.child()
        self.agent(child)
        app = Waggle(store=self.store, runtime=self.runtime)
        with patch.object(self.runtime, 'dispatch') as dispatch, patch.object(self.runtime, 'check_dispatch', wraps=self.runtime.check_dispatch) as gate:
            async with app.run_test(size=(40, 18)) as pilot:
                app.selected_id = child.id
                app.refresh_tasks()
                app.action_start()
                await wait_ui(pilot, lambda: isinstance(app.screen, WorkContractForm))
                form = app.screen
                gate.assert_not_called()
                self.assertEqual(form.query_one('#contract-purpose', Select).value, Select.NULL)
                await pilot.press('ctrl+enter')
                self.assertIs(app.screen, form)
                self.fill_contract(form)
                await pilot.pause()
                for button in form.query('.dialog-buttons Button').results(Button):
                    self.assertTrue(form.query_one('#dialog').content_region.contains_region(button.region))
                await pilot.click('#contract-save')
                await wait_ui(pilot, lambda: len(app.screen_stack) == 1)
                self.assertEqual(len(self.store.load().runs), 1)
                parsed_contract = parse_compiled_contract(app.board.tasks[child.id].remaining)
                assert parsed_contract is not None
                self.assertEqual(parsed_contract.purpose, 'inventory')
                dispatch.assert_not_called()
                app.action_start()
                await wait_ui(pilot, lambda: isinstance(app.screen, Confirm))
                compiled = app.board.compiled_requirements(app.board.tasks[child.id])
                assert isinstance(app.screen, Confirm)
                self.assertIn(compiled, app.screen.body)
                await pilot.press('escape')
                dispatch.assert_not_called()
                app.action_start()
                await wait_ui(pilot, lambda: isinstance(app.screen, Confirm))
                await pilot.press('ctrl+enter')
                await wait_ui(pilot, lambda: len(app.board.runs) == 2)
                run = app.board.runs[app.board.tasks[child.id].run_id]
                self.assertEqual(run.requirements, compiled)
                self.assertTrue(all(call.args[1] == compiled for call in gate.call_args_list))
                self.assertEqual(self.store.load().runs[run.id].requirements, compiled)
                dispatch.assert_called_once()
                self.assertNotEqual(app.board.tasks[child.id].status, 'done')

    async def test_followup_supplement_final_preview_preflight_and_commit_all_include_source(self):
        from app import Note, RequestReview
        from decisions import task_evidence_request
        child = self.child()
        self.agent(child)
        app = Waggle(store=self.store, runtime=self.runtime)
        with patch.object(self.runtime, 'dispatch') as dispatch, patch.object(self.runtime, 'check_dispatch', wraps=self.runtime.check_dispatch) as gate:
            async with app.run_test(size=(80, 24)) as pilot:
                app.selected_id = child.id
                app.refresh_tasks()
                app.run_worker(app.open_dispatch_supplement(child.id, intent='advice'))
                await wait_ui(pilot, lambda: isinstance(app.screen, Note))
                form = app.screen
                form.query_one(TextArea).text = '请核对已有原件的来源'
                await pilot.click('#submit')
                await wait_ui(pilot, lambda: isinstance(app.screen, RequestReview))
                expected = app.board.compiled_requirements(app.board.tasks[child.id], task_evidence_request('请核对已有原件的来源'))
                review = app.screen
                assert isinstance(review, RequestReview)
                self.assertIn(expected, review.body)
                self.assertEqual(getattr(review.focused, 'id', None), 'cancel')
                await pilot.press('ctrl+enter')
                self.assertIs(app.screen, review)
                self.assertIn('尚未发送', str(review.query_one('#request-shortcut-hint', Static).content))
                self.assertEqual(len(app.board.runs), 1)
                self.assertEqual(len(self.store.load().runs), 1)
                self.assertFalse(list(self.runtime.root.glob('*/commands/*.json')))
                dispatch.assert_not_called()
                await pilot.click('#submit')
                await wait_ui(pilot, lambda: len(app.board.runs) == 2)
                run = app.board.runs[app.board.tasks[child.id].run_id]
                self.assertEqual(run.requirements, expected)
                self.assertTrue(all(call.args[1] == expected for call in gate.call_args_list))
                self.assertEqual(self.store.load().runs[run.id].requirements, expected)
                dispatch.assert_called_once()

    async def test_first_pending_review_collects_scope_and_verification_before_confirmation(self):
        from app import Note
        child = self.child()
        self.agent(child)
        app = Waggle(store=self.store, runtime=self.runtime)
        with patch.object(self.runtime, 'dispatch') as dispatch:
            async with app.run_test(size=(80, 24)) as pilot:
                app.selected_id = child.id
                app.refresh_tasks()
                app.run_worker(app.open_dispatch_supplement(child.id, intent='review'))
                await wait_ui(pilot, lambda: isinstance(app.screen, Note))
                form = app.screen
                form.query_one(TextArea).text = '核对隔离成果'
                await pilot.click('#submit')
                self.assertIs(app.screen, form)
                dispatch.assert_not_called()
                self.assertEqual(len(app.board.runs), 1)
                form.query_one('#review-scope', Input).value = '仅当前项目，不修改原件'
                form.query_one('#review-verification', Input).value = '清单与报告逐项对应'
                await pilot.click('#submit')
                await wait_ui(pilot, lambda: isinstance(app.screen, Confirm))
                self.assertEqual(len(self.store.load().runs), 1)
                await pilot.press('escape')
                await wait_ui(pilot, lambda: app.screen is form)
                self.assertEqual(form.query_one(TextArea).text, '核对隔离成果')
                dispatch.assert_not_called()

    async def test_source_changed_after_final_preview_zero_commit_or_publish(self):
        child = self.child()
        self.agent(child)
        self.board.set_work_contract(child, WorkContract('evidence', '补证', '当前目录', '列出原件', '不试验'))
        self.store.save(self.board)
        app = Waggle(store=self.store, runtime=self.runtime)
        with patch.object(self.runtime, 'dispatch') as dispatch:
            async with app.run_test(size=(80, 24)) as pilot:
                app.selected_id = child.id
                app.refresh_tasks()
                app.action_start()
                await wait_ui(pilot, lambda: isinstance(app.screen, Confirm))
                app.board.runs[self.parent.run_id].requirements += '\n源漂移'
                await pilot.press('ctrl+enter')
                await pilot.pause()
                dispatch.assert_not_called()
                self.assertEqual(len(self.store.load().runs), 1)
                self.assertEqual(len(app.board.runs), 1)


@unittest.skipUnless(shutil.which('pi'), 'installed Pi required')
class N1Offline(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_contract_actual_dispatch_reports_without_auto_acceptance(self):
        with TemporaryDirectory(prefix='waggle-n1-offline-') as directory:
            fixture = PiFixture(Path(directory))
            try:
                await fixture.start()
                agent = await fixture.connect()
                assert agent is not None
                with Store(Path(directory) / 'tasks.sqlite3') as store:
                    board = store.load()
                    board.add_project('项目', str(fixture.project))
                    task = board.create('FULL', '项目', '只执行隔离离线计数器')
                    board.set_work_contract(task, WorkContract('implementation', '隔离计数一次',
                                            '仅临时项目', 'fixture-result counter=1，报告 full', '不联网、不访问真实文件'))
                    board.assign(task, agent.id)
                    store.save(board)
                    app = Waggle(store=store, runtime=fixture.runtime)
                    async with app.run_test(size=(80, 24)) as pilot:
                        app.action_start()
                        await wait_ui(pilot, lambda: isinstance(app.screen, Confirm))
                        compiled = app.board.compiled_requirements(app.board.tasks[task.id])
                        await pilot.press('ctrl+enter')
                        await wait_ui(pilot, lambda: bool(app.board.runs))
                        run = app.board.runs[app.board.tasks[task.id].run_id]
                        result = await fixture.result(run)
                        self.assertEqual((result.state, result.delivery, result.requirements), ('finished', 'full', compiled))
                        await wait_ui(pilot, lambda: app.board.tasks[task.id].can_accept)
                        self.assertNotEqual(app.board.tasks[task.id].status, 'done')
                        self.assertEqual(len(store.load().runs), 1)
            finally:
                await fixture.close()
