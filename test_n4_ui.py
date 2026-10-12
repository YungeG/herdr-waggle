"""N4 real Store / private mailbox / responsive form integration only."""
import json
import os
import time
import unittest
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from textual.widgets import Button, Input, Select, SelectionList, Static, TextArea

from app import Choice, Confirm, Waggle
from evidence import EVIDENCE_MARKER, now
from evidence_view import (
    EvidenceForm,
    EvidenceInventory,
    EvidenceReview,
    EvidenceVerificationForm,
)
from journal import current_report, parse_event
from runtime import Agent, Runtime, private_dir, publish
from store import StorageError, Store
from task_context import WorkContract
from test_capabilities import capability_record
from test_live import wait_ui
from test_task_context import _make_accepted_parent


class InventoryUI(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory(prefix='waggle-n4-ui-')))
        self.project = self.root / '隔离项目'
        self.project.mkdir()
        self.runtime = Runtime(self.root / 'bridges')
        self.store = self.enterContext(Store(self.root / 'tasks.sqlite3'))
        self.board = self.store.load()
        self.board.add_project('数据实验室', str(self.project))
        self.board.next_task_id = 9
        self.parent = _make_accepted_parent(self.board)
        self.owner = self.board.create('新独立盘点', self.parent.project, '新目标不得沿用旧收益、名单和测试结果')
        self.board.set_work_contract(self.owner, WorkContract('inventory', '有来源清单', '仅明确引用', '逐项来源和缺口', '不修改文件、不新增预算'))
        self.store.save(self.board)

    def app(self):
        app = Waggle(store=self.store, runtime=self.runtime)
        self.enterContext(patch.object(app, 'schedule_poll'))
        return app

    def select_task(self, app):
        app.selected_id = self.owner.id
        app.refresh_tasks()

    def fill_reference(self, form):
        key = next(key for key, source in form.candidates.items() if source.task_id == self.parent.id)
        form.query_one('#evidence-source', Select).value = key
        form.query_one('#evidence-refs', SelectionList).select('summary')
        for key, value in [('name', 'D009 旧样例（不是新成果）'), ('locator', '/未核实/original.csv'),
                           ('limits', '仅原样例边界'), ('gaps', '真实原件未核实'), ('next_owner', '未知，先明确材料保管人')]:
            form.query_one('#evidence-' + key, TextArea).text = value

    async def open_reference(self, app, pilot):
        self.select_task(app)
        app.action_evidence()
        await wait_ui(pilot, lambda: isinstance(app.screen, EvidenceInventory))
        await pilot.click('#inventory-add')
        await wait_ui(pilot, lambda: isinstance(app.screen, EvidenceForm))
        return app.screen

    async def save_reference(self, app, pilot):
        form = await self.open_reference(app, pilot)
        self.fill_reference(form)
        await pilot.press('ctrl+enter')
        await wait_ui(pilot, lambda: isinstance(app.screen, EvidenceReview))
        await pilot.press('ctrl+enter')
        await wait_ui(pilot, lambda: len(app.screen_stack) == 1)
        return next(iter(app.board.evidence_items.values()))

    async def width_probe(self, size):
        app = self.app()
        immutable_task, immutable_run = deepcopy(self.parent), deepcopy(self.board.runs[self.parent.run_id])
        async with app.run_test(size=size) as pilot:
            self.select_task(app)
            await pilot.press('ctrl+k')
            await wait_ui(pilot, lambda: isinstance(app.screen, Choice))
            assert isinstance(app.screen, Choice)
            self.assertIn('evidence', app.screen.options)
            await pilot.press('escape')
            await wait_ui(pilot, lambda: len(app.screen_stack) == 1)
            form = await self.open_reference(app, pilot)
            self.assertIs(form.query_one('#evidence-source', Select).value, Select.NULL)
            self.assertEqual(form.query_one('#evidence-refs', SelectionList).selected, [])
            await pilot.press('ctrl+enter')
            self.assertIs(app.screen, form)
            self.assertIn('显式选择', str(form.query_one('#form-error', Static).render()))
            self.fill_reference(form)
            await pilot.pause()
            source_detail = str(form.query_one('#evidence-source-detail', Static).render())
            self.assertIn(self.parent.run_id, source_detail)
            self.assertIn('人工验收', source_detail)
            await pilot.press('ctrl+enter')
            await wait_ui(pilot, lambda: isinstance(app.screen, EvidenceReview))
            review = app.screen
            assert isinstance(review, EvidenceReview) and review.focused is not None
            self.assertEqual(review.focused.id, 'cancel')
            self.assertIn('未核实', review.body_text)
            self.assertFalse(self.store.load().evidence_items)
            for selector in ('#cancel', '#submit'):
                button = review.query_one(selector, Button)
                self.assertGreaterEqual(button.region.y, 0)
                self.assertLess(button.region.bottom, size[1] + 1)
            await pilot.press('escape')
            await wait_ui(pilot, lambda: app.screen is form)
            self.assertEqual(form.query_one('#evidence-name', TextArea).text, 'D009 旧样例（不是新成果）')
            await pilot.press('ctrl+enter')
            await wait_ui(pilot, lambda: isinstance(app.screen, EvidenceReview))
            await pilot.click('#submit')
            await wait_ui(pilot, lambda: len(app.screen_stack) == 1)
            saved = self.store.load()
            self.assertEqual(len(saved.evidence_items), 1)
            self.assertFalse(saved.evidence_verifications)
            self.assertEqual(saved.tasks[self.parent.id], immutable_task)
            self.assertEqual(saved.runs[self.parent.run_id], immutable_run)
            self.assertEqual(len(saved.runs), 1)
            self.assertFalse(list(self.root.glob('bridges/*/commands/*.json')))
            app.action_evidence()
            await wait_ui(pilot, lambda: isinstance(app.screen, EvidenceInventory))
            assert isinstance(app.screen, EvidenceInventory)
            self.assertIn('原件及路径未核实', app.screen.body_text)

    async def test_inventory_keyboard_mouse_40(self):
        await self.width_probe((40, 18))

    async def test_inventory_keyboard_mouse_80(self):
        await self.width_probe((80, 24))

    async def test_inventory_keyboard_mouse_wide(self):
        await self.width_probe((140, 40))

    async def test_failed_save_and_source_drift_preserve_input_at_final_confirm(self):
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            form = await self.open_reference(app, pilot)
            self.fill_reference(form)
            form.query_one('#evidence-name', TextArea).text = '中文多行\n保留输入'
            await pilot.press('ctrl+enter')
            await wait_ui(pilot, lambda: isinstance(app.screen, EvidenceReview))
            with patch.object(self.store, 'save', side_effect=StorageError('fixture disk failure')):
                await pilot.click('#submit')
                await wait_ui(pilot, lambda: app.screen is form)
            self.assertEqual(form.query_one('#evidence-name', TextArea).text, '中文多行\n保留输入')
            self.assertIn('fixture disk failure', str(form.query_one('#form-error', Static).render()))
            self.assertFalse(app.board.evidence_items)
            self.assertFalse(self.store.load().evidence_items)
            await pilot.press('ctrl+enter')
            await wait_ui(pilot, lambda: isinstance(app.screen, EvidenceReview))
            app.board.runs[self.parent.run_id].events[0] = app.board.runs[self.parent.run_id].events[0].replace('样例方案', '原文已变')
            await pilot.click('#submit')
            await wait_ui(pilot, lambda: app.screen is form)
            self.assertEqual(form.query_one('#evidence-name', TextArea).text, '中文多行\n保留输入')
            self.assertIn('报告', str(form.query_one('#form-error', Static).render()))
            self.assertFalse(self.store.load().evidence_items)
            self.assertFalse(list(self.root.glob('bridges/*/commands/*.json')))

    async def test_independent_human_verification_blank_fields_and_append_only_zero_execution(self):
        app = self.app()
        async with app.run_test(size=(40, 18)) as pilot:
            item = await self.save_reference(app, pilot)
            app.action_evidence()
            await wait_ui(pilot, lambda: isinstance(app.screen, EvidenceInventory))
            await pilot.click('#inventory-verify')
            await wait_ui(pilot, lambda: isinstance(app.screen, EvidenceVerificationForm))
            form = app.screen
            self.assertIs(form.query_one('#verification-item', Select).value, Select.NULL)
            self.assertIs(form.query_one('#verification-result', Select).value, Select.NULL)
            await pilot.press('ctrl+enter')
            self.assertIs(app.screen, form)
            form.query_one('#verification-item', Select).value = item.id
            form.query_one('#verification-result', Select).value = 'unverified'
            for key, value in [('verifier', '夹具人工核验人'), ('method', '只比对隔离记录，不触碰真实原件'),
                               ('limits', '旧样例'), ('gaps', '真实原件和联系人未知'), ('next_owner', '未知')]:
                form.query_one('#verification-' + key, TextArea).text = value
            form.query_one('#verification-checked-at', Input).value = now()
            await pilot.press('ctrl+enter')
            await wait_ui(pilot, lambda: isinstance(app.screen, EvidenceReview))
            self.assertFalse(self.store.load().evidence_verifications)
            await pilot.click('#submit')
            await wait_ui(pilot, lambda: len(app.screen_stack) == 1)
            saved = self.store.load()
            self.assertEqual(len(saved.evidence_verifications), 1)
            self.assertEqual(len(saved.runs), 1)
            self.assertFalse(saved.decisions)
            self.assertFalse(saved.supplements)
            self.assertFalse(list(self.root.glob('bridges/*/commands/*.json')))
            app.action_evidence()
            await wait_ui(pilot, lambda: isinstance(app.screen, EvidenceInventory))
            assert isinstance(app.screen, EvidenceInventory)
            self.assertIn('人工核验记录', app.screen.body_text)
            self.assertIn('仍未核实', app.screen.body_text)

    async def test_real_confirm_preflight_run_and_mailbox_receive_exact_selected_inventory(self):
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            item = await self.save_reference(app, pilot)
            owner = app.board.tasks[self.owner.id]
            agent = Agent(str(uuid4()), str(uuid4()), str(self.project), '隔离 Pi', 'offline/fixture', os.getpid(),
                          int(time.time() * 1000), True, True, '')
            endpoint = self.runtime.root / agent.id
            for path in (self.runtime.root, endpoint, endpoint / 'commands', endpoint / 'runs'):
                private_dir(path, create=True)
            publish(endpoint / 'agent.json', {'version': 1, **asdict(agent)})
            publish(endpoint / 'report-capabilities.json', capability_record(agent))
            app.apply_change(lambda board: board.assign(board.tasks[owner.id], agent.id))
            app.agents[agent.id] = agent
            compiled = app.board.compiled_requirements(app.board.tasks[owner.id])
            report = current_report(self.parent, self.board.runs[self.parent.run_id])
            assert report is not None
            acceptance = next(e for raw in self.parent.events if (e := parse_event(raw)) and e.kind == 'accepted')
            with patch.object(self.runtime, 'check_dispatch', wraps=self.runtime.check_dispatch) as gate:
                app.run_worker(app.confirm_dispatch(owner.id))
                await wait_ui(pilot, lambda: isinstance(app.screen, Confirm))
                assert isinstance(app.screen, Confirm)
                self.assertIn(compiled, app.screen.body)
                self.assertEqual(len(self.store.load().runs), 1)
                await pilot.press('ctrl+enter')
                await wait_ui(pilot, lambda: len(app.board.runs) == 2)
            run = app.board.runs[app.board.tasks[owner.id].run_id]
            self.assertEqual(run.requirements, compiled)
            self.assertEqual(run.requirements.count(EVIDENCE_MARKER), 1)
            self.assertIn(item.source_digest, run.requirements)
            self.assertIn(report.id, run.requirements)
            self.assertIn(acceptance.id, run.requirements)
            self.assertTrue(all(call.args[1] == compiled for call in gate.call_args_list))
            self.assertEqual(self.store.load().runs[run.id].requirements, compiled)
            files = list((endpoint / 'commands').glob('*.json'))
            self.assertEqual(len(files), 1)
            packet = json.loads(files[0].read_text())
            self.assertEqual(packet['run']['requirements'], compiled)
            self.assertEqual(self.store.load().runs[self.parent.run_id], self.board.runs[self.parent.run_id])
