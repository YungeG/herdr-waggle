"""Disposable UI/SQLite/mailbox recovery probes; no live task control."""
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

from textual.widgets import Button, Select, Static, TextArea

from app import Confirm, RecoveryForm, Waggle
from delivery import detail_text
from runtime import BridgeError, Runtime, private_dir
from store import StorageError, Store, encode
from task_context import WorkContract
from test_bridge import PiFixture
from test_capabilities import capability_record
from test_failure_view import failed_board
from test_live import wait_ui


class RecoveryUI(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory(prefix='waggle-n2-ui-')))
        self.project = self.root / 'project'
        self.project.mkdir()
        self.board, self.task, self.source_run, self.agent, self.session = failed_board(str(self.project))
        self.agent = replace(self.agent, pid=os.getpid())
        self.runtime = Runtime(self.root / 'bridges')
        self.endpoint = self.runtime.root / self.agent.id
        for path in (self.runtime.root, self.endpoint, self.endpoint / 'runs', self.endpoint / 'commands'):
            private_dir(path, create=True)
        self.store = self.enterContext(Store(self.root / 'tasks.sqlite3'))
        self.store.load()  # Establish the real optimistic revision before the first save.
        self.store.save(self.board)
        self.write_live(self.agent, self.session)

    def write_live(self, agent, session):
        now = int(time.time() * 1000)
        agent = replace(agent, heartbeat=now)
        session = replace(session, observed_at=now)
        for name, row in [('agent.json', {'version': 1, **asdict(agent)}),
                          ('session.json', {'version': 1, **asdict(session)}),
                          ('report-capabilities.json', capability_record(agent))]:
            path = self.endpoint / name
            path.write_text(json.dumps(row), encoding='utf-8')
            path.chmod(0o600)

    def fill(self, form):
        form.query_one('#contract-purpose', Select).value = 'evidence'
        for key, text in [('deliverable', 'Inspect existing fixture'), ('allowed_scope', str(self.project)),
                          ('verification', 'Existing file assertions'), ('exclusions', 'No repeat writes or experiments')]:
            form.query_one('#contract-' + key, TextArea).text = text
        form.query_one('#recovery-checkpoint', TextArea).text = 'Inspect existing file only'
        form.query_one('#recovery-remaining', TextArea).text = 'Check incomplete output'

    def prepare(self):
        self.board.prepare_recovery(self.task, deepcopy(self.source_run), 'Inspect existing file only', 'Check incomplete output',
                                    WorkContract('evidence', 'Inspect fixture', str(self.project), 'Assertions', 'No repeat writes'))
        self.store.save(self.board)

    async def save_width(self, size):
        self.write_live(replace(self.agent, idle=False), replace(self.session, state='working', linked_run_id='', following=False))
        old_run = deepcopy(asdict(self.source_run))
        app = Waggle(store=self.store, runtime=self.runtime)
        async with app.run_test(size=size) as pilot:
            app.selected_id = self.task.id
            app.refresh_tasks()
            await pilot.press('ctrl+k', *'准备恢复', 'enter')
            await wait_ui(pilot, lambda: isinstance(app.screen, RecoveryForm))
            form = app.screen
            self.assertEqual(form.query_one('#contract-purpose', Select).value, Select.NULL)
            self.fill(form)
            for button in form.query('.dialog-buttons Button').results(Button):
                self.assertTrue(form.query_one('#dialog').content_region.contains_region(button.region))
            await pilot.press('ctrl+enter')
            await wait_ui(pilot, lambda: len(app.screen_stack) == 1)
            saved = app.board.tasks[self.task.id]
            self.assertEqual(saved.run, 'none')
            self.assertEqual(len(app.board.runs), 1)
            self.assertEqual(asdict(app.board.runs[self.source_run.id]), old_run)
            body = detail_text(saved, app.board.runs[self.source_run.id]).plain
            self.assertIn('Saved intermediate fixture', body)
            self.assertIn('auth_unavailable', body)
            before = encode(app.board)
            self.assertFalse(await app.dispatch_task(deepcopy(saved), self.agent))
            self.assertEqual(encode(app.board), before)
            self.assertFalse(list(self.endpoint.glob('commands/*.json')))

    async def test_busy_unbound_save_only_at_40_columns(self):
        await self.save_width((40, 18))

    async def test_busy_unbound_save_only_at_80_columns(self):
        await self.save_width((80, 24))

    async def test_busy_unbound_save_only_at_wide_columns(self):
        await self.save_width((140, 40))

    async def test_cancel_save_error_and_source_change_preserve_input_and_zero_intents(self):
        app = Waggle(store=self.store, runtime=self.runtime)
        async with app.run_test(size=(80, 24)) as pilot:
            app.selected_id = self.task.id
            app.refresh_tasks()
            app.action_prepare_recovery()
            await wait_ui(pilot, lambda: isinstance(app.screen, RecoveryForm))
            form = app.screen
            self.fill(form)
            before = encode(self.store.load())
            with patch.object(self.store, 'save', side_effect=StorageError('fixture disk failure')):
                await pilot.press('ctrl+enter')
            self.assertIs(app.screen, form)
            self.assertEqual(form.query_one('#recovery-checkpoint', TextArea).text, 'Inspect existing file only')
            self.assertIn('fixture disk failure', str(form.query_one('#form-error', Static).content))
            self.assertEqual(encode(self.store.load()), before)
            app.board.tasks[self.task.id].description = 'source changed while typing'
            await pilot.press('ctrl+enter')
            self.assertIs(app.screen, form)
            self.assertIn('变化', str(form.query_one('#form-error', Static).content))
            await pilot.press('escape')
            self.assertEqual(len(app.board.runs), 1)
            self.assertEqual(encode(self.store.load()), before)
            self.assertFalse(list(self.endpoint.glob('commands/*.json')))

    async def test_final_confirmation_cancels_or_rechecks_current_session(self):
        self.prepare()
        app = Waggle(store=self.store, runtime=self.runtime)
        async with app.run_test(size=(80, 24)) as pilot:
            app.selected_id = self.task.id
            app.refresh_tasks()
            preview = app.board.compiled_requirements(app.board.tasks[self.task.id])
            await app.confirm_dispatch(self.task.id)
            await wait_ui(pilot, lambda: isinstance(app.screen, Confirm))
            assert isinstance(app.screen, Confirm)
            self.assertIn(preview, app.screen.body)
            await pilot.click('#cancel')
            self.assertEqual(len(app.board.runs), 1)
            await app.confirm_dispatch(self.task.id)
            await wait_ui(pilot, lambda: isinstance(app.screen, Confirm))
            self.write_live(replace(self.agent, idle=False), replace(self.session, state='working', linked_run_id='', following=False))
            await pilot.click('#submit')
            await wait_ui(pilot, lambda: not app._sending and len(app.screen_stack) == 1)
            self.assertEqual(len(app.board.runs), 1)
            self.assertFalse(list(self.endpoint.glob('commands/*.json')))

    async def test_fresh_bound_idle_commit_uses_preview_and_new_id_once(self):
        self.prepare()
        app = Waggle(store=self.store, runtime=self.runtime)
        async with app.run_test(size=(80, 24)):
            app.selected_id = self.task.id
            app.refresh_tasks()
            expected = deepcopy(app.board.tasks[self.task.id])
            preview = app.board.compiled_requirements(app.board.tasks[self.task.id])
            self.assertTrue(await app.dispatch_task(expected, self.agent, expected_requirements=preview))
            self.assertEqual(len(app.board.runs), 2)
            new_run = app.board.runs[app.board.tasks[self.task.id].run_id]
            self.assertNotEqual(new_run.id, self.source_run.id)
            self.assertEqual(new_run.requirements, preview)
            self.assertEqual(asdict(app.board.runs[self.source_run.id]), asdict(self.source_run))
            self.assertEqual(len(list(self.endpoint.glob('commands/*.json'))), 1)
            self.assertFalse(await app.dispatch_task(expected, self.agent, expected_requirements=preview))
            self.assertEqual(len(list(self.endpoint.glob('commands/*.json'))), 1)
            persisted = self.store.load()
            self.assertEqual(persisted.runs[new_run.id].requirements, preview)

    async def test_human_abandonment_does_not_bypass_current_activity_or_observation(self):
        failed_id, failed_snapshot = self.source_run.id, deepcopy(asdict(self.source_run))
        self.prepare()
        self.board.assign(self.task, self.agent.id)
        source = self.board.queue_real(self.task, str(uuid4()), self.agent.session_id, str(self.project))
        self.store.save(self.board)
        # Abandon a saved nonterminal round through the real transition, never relabel a failure.
        self.board.abandon(self.task, 'fixture human verified prior stop')
        self.store.save(self.board)
        self.source_run = source
        self.session = replace(self.session, run_id=source.id, linked_run_id='', following=False)
        self.prepare()
        self.write_live(replace(self.agent, idle=False), replace(self.session, state='working', linked_run_id='', following=False))
        app = Waggle(store=self.store, runtime=self.runtime)
        async with app.run_test(size=(80, 24)):
            before = encode(app.board)
            self.assertFalse(await app.dispatch_task(deepcopy(app.board.tasks[self.task.id]), self.agent))
            self.assertEqual(encode(app.board), before)
            self.runtime.observe(source, self.runtime.root)
            self.write_live(self.agent, self.session)
            self.assertFalse(await app.dispatch_task(deepcopy(app.board.tasks[self.task.id]), self.agent))
            self.assertEqual(encode(app.board), before)
            self.assertEqual(asdict(self.store.load().runs[failed_id]), failed_snapshot)
            self.assertFalse(list(self.endpoint.glob('commands/*.json')))

    async def test_unknown_publication_retains_unique_run_without_retry(self):
        self.prepare()
        app = Waggle(store=self.store, runtime=self.runtime)
        async with app.run_test(size=(80, 24)):
            expected = deepcopy(app.board.tasks[self.task.id])
            with patch.object(self.runtime, 'dispatch', side_effect=BridgeError('fixture publish unknown')) as publish_mock:
                self.assertFalse(await app.dispatch_task(expected, self.agent))
                self.assertEqual(publish_mock.call_count, 1)
                self.assertEqual(len(app.board.runs), 2)
                self.assertFalse(await app.dispatch_task(deepcopy(app.board.tasks[self.task.id]), self.agent))
                self.assertEqual(publish_mock.call_count, 1)
            self.assertEqual(len(self.store.load().runs), 2)


@unittest.skipUnless(shutil.which('pi'), 'installed Pi required')
class RecoveryOffline(unittest.IsolatedAsyncioTestCase):
    async def test_actual_provider_failure_new_round_is_explicit_and_never_revives_source(self):
        with TemporaryDirectory(prefix='waggle-n2-offline-') as directory:
            fixture = PiFixture(Path(directory))
            try:
                await fixture.start()
                agent = await fixture.connect()
                assert agent is not None
                with Store(Path(directory) / 'tasks.sqlite3') as store:
                    board = store.load()
                    board.add_project('fixture', str(fixture.project))
                    task = board.create('PROVIDER_ERROR', 'fixture', 'Only disposable offline provider failure')
                    contract = WorkContract('implementation', 'Check fixture failure', str(fixture.project), 'Explicit provider fault receipt', 'No real services or data')
                    board.set_work_contract(task, contract)
                    board.assign(task, agent.id)
                    store.save(board)
                    app = Waggle(store=store, runtime=fixture.runtime)
                    async with app.run_test(size=(80, 24)) as pilot:
                        self.assertTrue(await app.dispatch_task(deepcopy(app.board.tasks[task.id]), agent))
                        source = app.board.runs[app.board.tasks[task.id].run_id]
                        failed = await fixture.result(source)
                        self.assertEqual(failed.state, 'failed')
                        await wait_ui(pilot, lambda: app.board.tasks[task.id].status == 'blocked')
                        original = deepcopy(asdict(app.board.runs[source.id]))
                        commands_before = tuple(path.name for path in (fixture.runtime.root / agent.id / 'commands').glob('*.json'))
                        app.apply_change(lambda candidate: candidate.prepare_recovery(candidate.tasks[task.id], candidate.runs[source.id],
                                        'Inspect the provider error only', 'Check another isolated receipt', contract))
                        self.assertEqual(len(store.load().runs), 1)
                        self.assertEqual(tuple(path.name for path in (fixture.runtime.root / agent.id / 'commands').glob('*.json')), commands_before)
                        preview = app.board.compiled_requirements(app.board.tasks[task.id])
                        fresh = fixture.runtime.agent(agent.id)
                        self.assertTrue(await app.dispatch_task(deepcopy(app.board.tasks[task.id]), fresh, expected_requirements=preview))
                        new = app.board.runs[app.board.tasks[task.id].run_id]
                        second = await fixture.result(new)
                        self.assertEqual(second.state, 'failed')
                        self.assertNotEqual(new.id, source.id)
                        self.assertEqual(second.requirements, preview)
                        self.assertEqual(asdict(app.board.runs[source.id]), original)
                        self.assertEqual(len(store.load().runs), 2)
                        self.assertNotEqual(app.board.tasks[task.id].status, 'done')
                        self.assertFalse((fixture.project / 'fixture-result.json').exists())
            finally:
                await fixture.close()


if __name__ == '__main__':
    unittest.main()
