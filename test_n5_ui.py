"""Responsive independent N5 forms with private Store and zero runtime calls."""
import unittest
from copy import deepcopy
from unittest.mock import patch

from textual.widgets import Button, Input, Select, Static, TextArea

import test_n5_ledger as ledger_fixture
from app import Choice, Waggle
from budget_ledger import ledger_view, now
from budget_view import BudgetForm, BudgetPanel
from evidence_view import EvidenceReview
from runtime import Runtime
from store import StorageError, encode
from test_n5_ledger import ORIGIN


class BudgetUI(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = ledger_fixture.LedgerAdmission()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store, self.root = self.fixture.store, self.fixture.root
        self.runtime = Runtime(self.root / 'private-bridges')
        self.preflight = self.enterContext(patch.object(self.runtime, 'check_dispatch'))
        self.dispatch = self.enterContext(patch.object(self.runtime, 'dispatch'))
        self.stop = self.enterContext(patch.object(self.runtime, 'stop'))

    def app(self):
        app = Waggle(store=self.store, runtime=self.runtime)
        self.enterContext(patch.object(app, 'schedule_poll'))
        return app

    def fill(self, form, values):
        for key, value in values.items():
            widget = form.query_one('#budget-' + key.replace('_', '-'))
            if isinstance(widget, TextArea):
                widget.load_text(value)
            elif isinstance(widget, Input):
                widget.value = value
            elif isinstance(widget, Select):
                widget.value = value

    def confirm(self, app):
        screen = app.screen
        assert isinstance(screen, EvidenceReview)
        screen.action_submit()

    def scope_fields(self, key):
        return dict(resource_key=key, meter_id='fixture-ui-meter', definition='All isolated cumulative counters since origin, not per Run',
                    author='isolated UI author', started_at=ORIGIN)

    def untouched_execution(self, app, before):
        self.assertEqual((app.board.tasks, app.board.runs, app.board.supplements, app.board.decisions), before)
        self.preflight.assert_not_called()
        self.dispatch.assert_not_called()
        self.stop.assert_not_called()
        self.assertFalse(list((self.root / 'private-bridges').rglob('*.json')))

    async def test_scope_cancel_error_then_save_is_zero_execution_at_both_widths(self):
        for width in (56, 120):
            app = self.app()
            async with app.run_test(size=(width, 24)) as pilot:
                before = tuple(deepcopy(getattr(app.board, name)) for name in ('tasks', 'runs', 'supplements', 'decisions'))
                saved = encode(self.store.load())
                app.open_budget_form('scope')
                await pilot.pause()
                form = app.screen
                assert isinstance(form, BudgetForm)
                self.assertIsInstance(form, BudgetForm)
                self.assertTrue(all(not widget.value for widget in form.query(Input)))
                self.assertTrue(all(not widget.text for widget in form.query(TextArea)))
                self.assertEqual(getattr(app.focused, 'id', None), 'cancel')
                form.action_submit()
                await pilot.pause()
                self.assertIs(app.screen, form)
                self.assertIn('未保存', str(form.query_one('#form-error', Static).render()))
                self.fill(form, self.scope_fields(f'fixture/ui-{width}'))
                form.action_submit()
                await pilot.pause()
                self.assertIsInstance(app.screen, EvidenceReview)
                self.assertEqual(getattr(app.focused, 'id', None), 'cancel')
                self.assertEqual(encode(self.store.load()), saved)
                await pilot.press('escape')
                await pilot.pause()
                self.assertIs(app.screen, form)
                self.assertEqual(form.query_one('#budget-resource-key', Input).value, f'fixture/ui-{width}')
                form.action_submit()
                await pilot.pause()
                with patch.object(self.store, 'save', side_effect=StorageError('fixture disk denied')):
                    self.confirm(app)
                    await pilot.pause()
                self.assertIs(app.screen, form)
                self.assertIn('fixture disk denied', str(form.query_one('#form-error', Static).render()))
                self.assertEqual(encode(self.store.load()), saved)
                self.assertEqual(form.query_one('#budget-resource-key', Input).value, f'fixture/ui-{width}')
                form.action_submit()
                await pilot.pause()
                self.confirm(app)
                await pilot.pause()
                self.assertEqual(len(app.screen_stack), 1)
                self.assertIn(f'fixture/ui-{width}', [scope.resource_key for scope in self.store.load().budget_scopes.values()])
                self.untouched_execution(app, before)

    async def test_verified_saved_ledger_does_not_enable_formal_approval(self):
        observation = self.fixture.observe()
        self.fixture.verify(observation)
        self.fixture.grant()
        app = self.app()
        async with app.run_test(size=(56, 24)) as pilot:
            saved = encode(self.store.load())
            app.action_budget()
            await pilot.pause()
            self.assertIsInstance(app.screen, BudgetPanel)
            self.assertTrue(app.screen.query_one('#budget-approve', Button).disabled)
            body = str(app.screen.query_one('#budget-body', Static).render())
            self.assertIn('正式预算批准入口关闭', body)
            self.assertIn('3 次 / 20 分钟 / 80 MiB', body)
            self.assertIn('不是派发许可', body)
            self.assertEqual(getattr(app.focused, 'id', None), 'cancel')
            await pilot.press('enter')
            await pilot.pause()
            self.assertEqual(len(app.screen_stack), 1)
            self.assertEqual(encode(self.store.load()), saved)
            self.preflight.assert_not_called()
            self.dispatch.assert_not_called()
            self.stop.assert_not_called()

    async def test_receipt_then_explicit_complete_verification_are_separate_zero_execution_saves(self):
        app = self.app()
        async with app.run_test(size=(120, 30)) as pilot:
            before = tuple(deepcopy(getattr(app.board, name)) for name in ('tasks', 'runs', 'supplements', 'decisions'))
            app.open_budget_form('observe')
            await pilot.pause()
            form = app.screen
            assert isinstance(form, BudgetForm)
            self.assertTrue(all(widget.value is Select.NULL for widget in form.query(Select)))
            self.fill(form, dict(scope=self.fixture.scope.id, evidence_item=self.fixture.item.id, source_kind='executor_self_report',
                                 receipt_id='ui-receipt', meter_id=self.fixture.scope.meter_id, revision='1',
                                 attempts='2', minutes='10', storage_mib='20', observed_at=now()))
            form.action_submit()
            await pilot.pause()
            self.assertIsInstance(app.screen, EvidenceReview)
            self.assertFalse(self.store.load().usage_observations)
            self.confirm(app)
            await pilot.pause()
            observation = list(self.store.load().usage_observations.values())[-1]
            self.assertIsNone(ledger_view(app.board, self.fixture.scope.id).used)
            app.open_budget_form('verify')
            await pilot.pause()
            form = app.screen
            assert isinstance(form, BudgetForm)
            self.assertTrue(all(widget.value is Select.NULL for widget in form.query(Select)))
            self.fill(form, dict(scope=self.fixture.scope.id))
            await pilot.pause()
            self.fill(form, dict(observation=observation.id, result='verified', verifier='Explicit private verifier',
                                 method='All original receipts plus no-use interval checked', limits='Private fixture only', gaps='',
                                 next_owner='fixture verifier', checked_at=now()))
            form.action_submit()
            await pilot.pause()
            self.assertIs(app.screen, form)
            self.assertIn('完整覆盖', str(form.query_one('#form-error', Static).render()))
            self.fill(form, dict(complete='yes'))
            form.action_submit()
            await pilot.pause()
            self.assertIsInstance(app.screen, EvidenceReview)
            self.assertFalse(self.store.load().ledger_verifications)
            self.confirm(app)
            await pilot.pause()
            self.assertEqual(ledger_view(app.board, self.fixture.scope.id).used, (2, 10, 20))
            self.assertIsNone(ledger_view(app.board, self.fixture.scope.id).remaining)
            self.assertFalse(app.board.budget_grants)
            self.untouched_execution(app, before)

    async def test_stale_last_confirmation_preserves_input_and_does_not_save(self):
        app = self.app()
        async with app.run_test(size=(56, 24)) as pilot:
            app.open_budget_form('scope')
            await pilot.pause()
            form = app.screen
            assert isinstance(form, BudgetForm)
            self.fill(form, self.scope_fields('fixture/stale-form'))
            form.action_submit()
            await pilot.pause()
            app.apply_change(lambda board: board.create('Unrelated saved source change', '收集箱'))
            saved = encode(self.store.load())
            self.confirm(app)
            await pilot.pause()
            self.assertIs(app.screen, form)
            self.assertIn('变化', str(form.query_one('#form-error', Static).render()))
            self.assertEqual(form.query_one('#budget-resource-key', Input).value, 'fixture/stale-form')
            self.assertEqual(encode(self.store.load()), saved)
            self.dispatch.assert_not_called()

    async def test_revocation_is_explicit_saved_only_and_never_stops_executor(self):
        observation = self.fixture.observe()
        self.fixture.verify(observation)
        grant = self.fixture.grant()
        app = self.app()
        async with app.run_test(size=(56, 24)) as pilot:
            before = tuple(deepcopy(getattr(app.board, name)) for name in ('tasks', 'runs', 'supplements', 'decisions'))
            app.open_budget_form('revoke')
            await pilot.pause()
            form = app.screen
            assert isinstance(form, BudgetForm)
            self.assertIs(form.query_one('#budget-scope', Select).value, Select.NULL)
            self.fill(form, dict(scope=self.fixture.scope.id, author='Explicit fixture author', reason='Cumulative permission revoked locally'))
            form.action_submit()
            await pilot.pause()
            self.assertEqual(ledger_view(self.store.load(), self.fixture.scope.id).grant_id, grant.id)
            self.assertEqual(getattr(app.focused, 'id', None), 'cancel')
            self.confirm(app)
            await pilot.pause()
            audit = list(self.store.load().budget_grants.values())[-1]
            self.assertEqual((audit.action, audit.previous_id), ('revoke', grant.id))
            self.assertIsNone(ledger_view(app.board, self.fixture.scope.id).remaining)
            self.untouched_execution(app, before)

    async def test_budget_palette_action_exists_without_any_task(self):
        from model import Board
        app = Waggle(board=Board(live=True), runtime=self.runtime)
        self.enterContext(patch.object(app, 'schedule_poll'))
        async with app.run_test(size=(56, 24)) as pilot:
            app.action_budget()
            await pilot.pause()
            self.assertIn('unknown', str(app.screen.query_one('#budget-body', Static).render()))
            self.assertTrue(app.screen.query_one('#budget-approve', Button).disabled)
            await pilot.press('escape')
            app.action_palette()
            await pilot.pause()
            palette = app.screen
            assert isinstance(palette, Choice)
            self.assertIn('budget', palette.options)
            self.dispatch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
