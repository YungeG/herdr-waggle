"""N6 responsive forms and actual private Runtime dispatch gates; no live state."""
import json
import os
import time
import unittest
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from textual.widgets import Button, Input, Select, SelectionList, Static, TextArea

from app import Choice, Confirm, Waggle
from dependencies import dependency_record, now, receipt_record, release_record
from dependency_view import DependencyForm, DependencyPanel
from evidence_view import EvidenceReview
from runtime import (
    Agent,
    Runtime,
    SessionObservation,
    private_dir,
)
from store import StorageError, Store, encode
from task_context import DEPENDENCY_MARKER, WorkContract
from test_capabilities import capability_record
from test_live import wait_ui
from test_n6_dependencies import attestation, expires
from test_task_context import _make_accepted_parent


class DependencyUI(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory(prefix='waggle-n6-ui-')))
        self.project = self.root / '隔离项目'
        self.project.mkdir()
        self.runtime = Runtime(self.root / 'bridges')
        self.store = self.enterContext(Store(self.root / 'state.db'))
        self.board = self.store.load()
        self.board.add_project('数据实验室', str(self.project))
        self.board.next_task_id = 9
        self.source = _make_accepted_parent(self.board)
        self.owner = self.board.create('明确接续文件', self.source.project, '只实施明确文件；盘点不需要写者交接')
        self.board.set_work_contract(self.owner, WorkContract('implementation', '明确文件改动', 'src/shared.py', '隔离回归', '不改其他文件、不新增预算'))
        run = self.board.runs[self.source.run_id]
        self.source_agent = Agent(run.agent_id, run.session_id, str(self.project), '隔离原写者', 'offline/fixture', os.getpid(),
                                  int(time.time() * 1000), True, True, '')
        self.receiver = Agent(str(uuid4()), str(uuid4()), str(self.project), '隔离接续者', 'offline/fixture', os.getpid(),
                              int(time.time() * 1000), True, True, '')
        self.write_source()
        self.write_receiver()
        self.store.save(self.board)

    def endpoint(self, agent):
        endpoint = self.runtime.root / agent.id
        for path in (self.runtime.root, endpoint, endpoint / 'commands', endpoint / 'runs'):
            private_dir(path, create=True)
        return endpoint

    def write_snapshot(self, path, value):
        # Heartbeat/session snapshots are replaceable, unlike immutable command publication.
        path.write_text(json.dumps(value), encoding='utf-8')
        path.chmod(0o600)

    def write_source(self, *, state='idle', bound=True, stale=False, new_session=False):
        heartbeat = int(time.time() * 1000)
        agent = replace(self.source_agent, heartbeat=heartbeat, idle=state == 'idle')
        if new_session:
            agent = replace(agent, session_id=str(uuid4()))
        session = SessionObservation(agent.id, agent.session_id, agent.cwd, 1, heartbeat - (20000 if stale else 0),
                                     state, '', self.source.run_id if bound else '', True, False)
        endpoint = self.endpoint(agent)
        self.write_snapshot(endpoint / 'agent.json', {'version': 1, **asdict(agent)})
        self.write_snapshot(endpoint / 'session.json', {'version': 1, **asdict(session)})
        self.write_snapshot(endpoint / 'report-capabilities.json', capability_record(agent))

    def write_receiver(self, *, idle=True):
        agent = replace(self.receiver, heartbeat=int(time.time() * 1000), idle=idle)
        endpoint = self.endpoint(agent)
        self.write_snapshot(endpoint / 'agent.json', {'version': 1, **asdict(agent)})
        self.write_snapshot(endpoint / 'report-capabilities.json', capability_record(agent))

    def app(self):
        app = Waggle(store=self.store, runtime=self.runtime)
        self.enterContext(patch.object(app, 'schedule_poll'))
        return app

    def select(self, app):
        app.selected_id = self.owner.id
        app.refresh_tasks()

    def seed_dependency(self, board):
        owner, source = board.tasks[self.owner.id], board.tasks[self.source.id]
        dep = dependency_record(board, owner, source, kind='write-conflict', operations=['implementation'],
                                files=['src/shared.py'], condition='明确原写者释放且接续者单独接收')
        board.add_dependency(owner, dep)
        self.store.save(board)
        return dep

    def seed_handover(self, app):
        dep = self.seed_dependency(app.board)
        owner = app.board.tasks[self.owner.id]
        release = release_record(app.board, dep.id, self.receiver.id, list(dep.files), expires_at=expires(), **attestation())
        app.board.release_ownership(owner, release)
        self.store.save(app.board)
        receipt = receipt_record(app.board, release.id, self.receiver.id, list(dep.files), verifier='独立接收人', method='核对同一精确范围', checked_at=now())
        app.board.receive_ownership(owner, receipt)
        self.store.save(app.board)
        return dep, release, receipt

    async def open_form(self, app, pilot, mode):
        self.select(app)
        app.run_worker(app.open_dependency_form(self.owner.id, mode))
        await wait_ui(pilot, lambda: isinstance(app.screen, DependencyForm))
        return app.screen

    async def fill_add(self, form, pilot):
        form.query_one('#dependency-selected', Select).value = str(self.source.id)
        form.query_one('#dependency-kind', Select).value = 'write-conflict'
        await pilot.pause()
        form.query_one('#dependency-proof', Select).value = 'release_receipt'
        form.query_one('#dependency-operations', SelectionList).select('implementation')
        form.query_one('#dependency-condition', TextArea).text = '原写者明确释放\n接续者独立核对具体文件'
        form.query_one('#dependency-files', TextArea).text = 'src/shared.py'

    async def confirm_form(self, app, pilot, form):
        await pilot.press('ctrl+enter')
        await wait_ui(pilot, lambda: isinstance(app.screen, EvidenceReview))
        assert app.screen.focused is not None
        self.assertEqual(app.screen.focused.id, 'cancel')
        await pilot.click('#submit')
        await wait_ui(pilot, lambda: len(app.screen_stack) == 1)

    async def width_probe(self, size):
        app = self.app()
        before_source = deepcopy(self.source), deepcopy(self.board.runs[self.source.run_id])
        async with app.run_test(size=size) as pilot:
            self.select(app)
            await pilot.press('ctrl+k')
            await wait_ui(pilot, lambda: isinstance(app.screen, Choice))
            assert isinstance(app.screen, Choice)
            self.assertIn('dependencies', app.screen.options)
            await pilot.press('escape')
            app.action_dependencies()
            await wait_ui(pilot, lambda: isinstance(app.screen, DependencyPanel))
            assert app.screen.focused is not None
            self.assertEqual(app.screen.focused.id, 'cancel')
            await pilot.click('#dependency-add')
            await wait_ui(pilot, lambda: isinstance(app.screen, DependencyForm))
            form = app.screen
            for key in ('selected', 'kind', 'proof'):
                self.assertIs(form.query_one('#dependency-' + key, Select).value, Select.NULL)
            self.assertEqual(form.query_one('#dependency-operations', SelectionList).selected, [])
            await pilot.press('ctrl+enter')
            self.assertIs(app.screen, form)
            self.assertIn('明确选择', str(form.query_one('#form-error', Static).render()))
            await self.fill_add(form, pilot)
            await pilot.press('ctrl+enter')
            await wait_ui(pilot, lambda: isinstance(app.screen, EvidenceReview))
            assert app.screen.focused is not None
            self.assertEqual(app.screen.focused.id, 'cancel')
            self.assertLessEqual(app.screen.query_one('#submit', Button).region.bottom, size[1])
            await pilot.press('escape')
            await wait_ui(pilot, lambda: app.screen is form)
            self.assertIn('\n', form.query_one('#dependency-condition', TextArea).text)
            await self.confirm_form(app, pilot, form)
            saved = self.store.load()
            self.assertEqual(len(saved.task_dependencies), 1)
            self.assertFalse(saved.ownership_releases)
            self.assertFalse(saved.ownership_receipts)
            self.assertEqual(len(saved.runs), 1)
            self.assertEqual((saved.tasks[self.source.id], saved.runs[self.source.run_id]), before_source)
            self.assertFalse(list(self.root.glob('bridges/*/commands/*.json')))
            self.assertEqual(app.primary_action().action, 'dependencies')

    async def test_40x18_no_defaults_final_cancel_multiline_mouse_keyboard_and_zero_execution(self):
        await self.width_probe((40, 18))

    async def test_80x24_no_defaults_and_final_cancel(self):
        await self.width_probe((80, 24))

    async def test_wide_no_defaults_and_final_cancel(self):
        await self.width_probe((140, 40))

    async def test_disk_failure_and_source_drift_preserve_input_without_publish(self):
        app = self.app()
        async with app.run_test(size=(40, 18)) as pilot:
            form = await self.open_form(app, pilot, 'add')
            await self.fill_add(form, pilot)
            await pilot.press('ctrl+enter')
            await wait_ui(pilot, lambda: isinstance(app.screen, EvidenceReview))
            with patch.object(self.store, 'save', side_effect=StorageError('fixture disk failure')):
                await pilot.click('#submit')
                await wait_ui(pilot, lambda: app.screen is form)
            self.assertIn('disk failure', str(form.query_one('#form-error', Static).render()))
            self.assertIn('\n', form.query_one('#dependency-condition', TextArea).text)
            self.assertFalse(app.board.task_dependencies)
            await pilot.press('ctrl+enter')
            await wait_ui(pilot, lambda: isinstance(app.screen, EvidenceReview))
            run = app.board.runs[self.source.run_id]
            run.events[0] = run.events[0].replace('样例方案', '漂移方案')
            await pilot.click('#submit')
            await wait_ui(pilot, lambda: app.screen is form)
            self.assertIn('来源', str(form.query_one('#form-error', Static).render()))
            self.assertEqual(form.query_one('#dependency-files', TextArea).text, 'src/shared.py')
            self.assertFalse(self.store.load().task_dependencies)
            self.assertFalse(list(self.root.glob('bridges/*/commands/*.json')))

    async def test_release_and_receipt_forms_separate_persisted_manual_fields_zero_assignment(self):
        app = self.app()
        dep = self.seed_dependency(app.board)
        async with app.run_test(size=(40, 18)) as pilot:
            release_form = await self.open_form(app, pilot, 'release')
            self.assertIs(release_form.query_one('#dependency-receiver', Select).value, Select.NULL)
            release_form.query_one('#dependency-selected', Select).value = dep.id
            await pilot.pause()
            self.assertEqual(release_form.query_one('#dependency-files', TextArea).text, '')
            release_form.query_one('#dependency-receiver', Select).value = self.receiver.id
            release_form.query_one('#dependency-files', TextArea).text = 'src/shared.py'
            for key, value in attestation().items():
                if key == 'checked_at':
                    release_form.query_one('#dependency-checked-at', Input).value = value
                else:
                    release_form.query_one('#dependency-' + key, TextArea).text = value
            release_form.query_one('#dependency-expires-at', Input).value = expires()
            await self.confirm_form(app, pilot, release_form)
            loaded = self.store.load()
            self.assertEqual(len(loaded.ownership_releases), 1)
            self.assertFalse(loaded.ownership_receipts)
            self.assertFalse(loaded.tasks[self.owner.id].agent)
            app.action_dependencies()
            await wait_ui(pilot, lambda: isinstance(app.screen, DependencyPanel))
            assert isinstance(app.screen, DependencyPanel)
            self.assertIn('尚未交接', app.screen.body_text)
            await pilot.press('escape')
            receipt_form = await self.open_form(app, pilot, 'receipt')
            release = next(iter(loaded.ownership_releases.values()))
            receipt_form.query_one('#dependency-selected', Select).value = release.id
            self.assertIs(receipt_form.query_one('#dependency-receiver', Select).value, Select.NULL)
            self.assertEqual(receipt_form.query_one('#dependency-files', TextArea).text, '')
            receipt_form.query_one('#dependency-receiver', Select).value = self.receiver.id
            receipt_form.query_one('#dependency-files', TextArea).text = 'src/shared.py'
            receipt_form.query_one('#dependency-verifier', TextArea).text = '独立接收人'
            receipt_form.query_one('#dependency-method', TextArea).text = '独立核对文件、原写者和接续者范围'
            receipt_form.query_one('#dependency-checked-at', Input).value = now()
            await self.confirm_form(app, pilot, receipt_form)
            loaded = self.store.load()
            self.assertEqual(len(loaded.ownership_receipts), 1)
            self.assertFalse(loaded.tasks[self.owner.id].agent)
            self.assertEqual(len(loaded.runs), 1)
            self.assertFalse(list(self.root.glob('bridges/*/commands/*.json')))

    async def test_original_pi_busy_offline_unbound_unknown_stale_or_new_session_never_assigns_or_preflights(self):
        app = self.app()
        self.seed_handover(app)
        async with app.run_test(size=(80, 24)):
            owner = app.board.tasks[self.owner.id]
            app.apply_change(lambda board: board.assign(board.tasks[owner.id], self.receiver.id))
            for state, bound, stale, new_session in [('busy', True, False, False), ('offline', True, False, False),
                                                     ('idle', False, False, False), ('idle', True, True, False),
                                                     ('idle', True, False, True)]:
                with self.subTest(state=state, bound=bound, stale=stale, new_session=new_session):
                    self.write_source(state=state, bound=bound, stale=stale, new_session=new_session)
                    before = encode(app.board)
                    with self.assertRaises(ValueError):
                        app.check_dependency_execution(app.board, app.board.tasks[owner.id], agent_id=self.receiver.id)
                    with patch.object(self.runtime, 'check_dispatch', wraps=self.runtime.check_dispatch) as gate:
                        self.assertFalse(await app.dispatch_task(deepcopy(app.board.tasks[owner.id]), self.receiver))
                        self.assertEqual(gate.call_count, 0)
                    self.assertEqual(encode(app.board), before)
            (self.endpoint(self.source_agent) / 'session.json').unlink()
            with patch.object(self.runtime, 'check_dispatch', wraps=self.runtime.check_dispatch) as gate:
                self.assertFalse(await app.dispatch_task(deepcopy(app.board.tasks[owner.id]), self.receiver))
                self.assertEqual(gate.call_count, 0)
            self.assertFalse(list(self.root.glob('bridges/*/commands/*.json')))
            self.assertEqual(len(self.store.load().runs), 1)

    async def test_source_busy_after_preflight_recheck_prevents_store_and_command(self):
        app = self.app()
        self.seed_handover(app)
        owner = app.board.tasks[self.owner.id]
        app.apply_change(lambda board: board.assign(board.tasks[owner.id], self.receiver.id))
        original_gate = self.runtime.check_dispatch
        def changed_after_gate(agent, requirements, **kwargs):
            result = original_gate(agent, requirements, **kwargs)
            self.write_source(state='busy')
            return result
        async with app.run_test(size=(80, 24)):
            before = encode(app.board)
            with patch.object(self.runtime, 'check_dispatch', side_effect=changed_after_gate) as gate:
                self.assertFalse(await app.dispatch_task(deepcopy(app.board.tasks[owner.id]), self.receiver))
                self.assertEqual(gate.call_count, 1)
            self.assertEqual(encode(app.board), before)
            self.assertEqual(len(self.store.load().runs), 1)
            self.assertFalse(list(self.root.glob('bridges/*/commands/*.json')))

    async def test_actual_confirm_preflight_store_run_and_private_mailbox_exact_quote_one_intent(self):
        app = self.app()
        dep, release, receipt = self.seed_handover(app)
        owner = app.board.tasks[self.owner.id]
        app.apply_change(lambda board: board.assign(board.tasks[owner.id], self.receiver.id))
        source_before = deepcopy(app.board.tasks[self.source.id]), deepcopy(app.board.runs[self.source.run_id])
        async with app.run_test(size=(80, 24)) as pilot:
            self.select(app)
            compiled = app.board.compiled_requirements(app.board.tasks[owner.id])
            with patch.object(self.runtime, 'check_dispatch', wraps=self.runtime.check_dispatch) as gate:
                app.run_worker(app.confirm_dispatch(owner.id))
                await wait_ui(pilot, lambda: isinstance(app.screen, Confirm))
                assert isinstance(app.screen, Confirm)
                self.assertIn(compiled, app.screen.body)
                assert app.screen.focused is not None
                self.assertEqual(app.screen.focused.id, 'cancel')
                self.assertEqual(len(self.store.load().runs), 1)
                await pilot.press('ctrl+enter')
                await wait_ui(pilot, lambda: len(app.board.runs) == 2)
            run = app.board.runs[app.board.tasks[owner.id].run_id]
            self.assertEqual(run.requirements, compiled)
            self.assertEqual(compiled.count(DEPENDENCY_MARKER), 1)
            for record in (dep, release, receipt):
                self.assertIn(record.id, compiled)
            self.assertTrue(all(call.args[1] == compiled for call in gate.call_args_list))
            commands = list((self.endpoint(self.receiver) / 'commands').glob('*.json'))
            self.assertEqual(len(commands), 1)
            self.assertEqual(json.loads(commands[0].read_text())['run']['requirements'], compiled)
            self.assertEqual(self.store.load().runs[run.id].requirements, compiled)
            self.assertEqual((app.board.tasks[self.source.id], app.board.runs[self.source.run_id]), source_before)

    async def test_inventory_dispatch_unaffected_by_write_only_dependency_and_unpersisted_panel_truthful(self):
        board = deepcopy(self.board)
        owner = board.tasks[self.owner.id]
        board.set_work_contract(owner, WorkContract('inventory', '明确材料清单', '只读已有材料', '来源和未知', '不实施不新增预算'))
        self.store.save(board)
        self.seed_dependency(board)
        app = self.app()
        async with app.run_test(size=(80, 24)):
            self.write_source(state='offline')
            app.apply_change(lambda board: board.assign(board.tasks[owner.id], self.receiver.id))
            self.assertTrue(await app.dispatch_task(deepcopy(app.board.tasks[owner.id]), self.receiver))
            self.assertEqual(len(self.store.load().runs), 2)
        unpersisted = Waggle(board=deepcopy(self.board), runtime=self.runtime)
        self.enterContext(patch.object(unpersisted, 'schedule_poll'))
        async with unpersisted.run_test(size=(40, 18)) as pilot:
            self.select(unpersisted)
            unpersisted.action_dependencies()
            await wait_ui(pilot, lambda: isinstance(unpersisted.screen, DependencyPanel))
            assert isinstance(unpersisted.screen, DependencyPanel)
            self.assertIn('尚未交接', unpersisted.screen.body_text)
            self.assertIn('未持久化', unpersisted.screen.body_text)
