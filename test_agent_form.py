"""User-facing creation contracts. No real Herdr/user session is touched."""

import os
import threading
import time
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from textual import events
from textual.widgets import Button, Input, Static

from app import Choice, Confirm, NewAgentForm, Waggle
from launch import Launch, prepare_launch
from runtime import Agent, Runtime, private_dir, publish
from store import StorageError, Store
from task_context import WorkContract
from test_capabilities import capability_record
from test_live import wait_ui


class NewAgentUI(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = self.enterContext(TemporaryDirectory(prefix="waggle-agent-ui-"))
        self.project = Path(self.temp) / "中文 project"
        self.project.mkdir()
        self.runtime = Runtime(Path(self.temp) / "bridges")
        self.store = self.enterContext(Store(Path(self.temp) / "tasks.sqlite3"))
        board = self.store.load()
        board.add_project("测试项目", str(self.project))
        task = board.create("Only explicit confirmation may execute", "测试项目")
        contract = WorkContract(
            purpose="implementation",
            deliverable="完成整任务交付",
            allowed_scope="项目目录",
            verification="回归测试通过",
            exclusions="不修改其他模块",
        )
        task.remaining = contract.compile()
        self.store.save(board)
        self.app = Waggle(store=self.store, runtime=self.runtime)

    def create(self, runtime, directory, name, model, identity):
        launch = prepare_launch(runtime, directory, name, model, identity)
        endpoint = runtime.root / launch.id
        for path in (endpoint, endpoint / "commands", endpoint / "runs"):
            private_dir(path, create=True)
        agent = Agent(launch.id, launch.session_id, directory, name, model or "fixture/default", os.getpid(), int(time.time() * 1000), True, True, "")
        publish(endpoint / "agent.json", {"version": 1, **asdict(agent)})
        publish(endpoint / "report-capabilities.json", capability_record(agent))
        launch.state, launch.tab_id, launch.pane_id = "starting", "test-tab", "test-pane"
        return launch

    async def test_inherited_mouse_cancel_and_connect_submit_each_run_once(self):
        with patch("app.create_pi", side_effect=self.create) as creation:
            async with self.app.run_test(size=(100, 32)) as pilot:
                self.app.action_new_agent()
                await wait_ui(pilot, lambda: isinstance(self.app.screen, NewAgentForm) and bool(self.app.screen.query("#cancel")))
                form = self.app.screen
                assert isinstance(form, NewAgentForm)
                with patch.object(form, "action_cancel", wraps=form.action_cancel) as cancel:
                    self.assertTrue(await pilot.click("#cancel"))
                    await wait_ui(pilot, lambda: len(self.app.screen_stack) == 1)
                    cancel.assert_called_once()
                    creation.assert_not_called()
                self.app.action_new_agent()
                await wait_ui(pilot, lambda: isinstance(self.app.screen, NewAgentForm) and bool(self.app.screen.query("#submit")))
                form = self.app.screen
                assert isinstance(form, NewAgentForm)
                with patch.object(form, "action_submit", wraps=form.action_submit) as submit:
                    self.assertTrue(await pilot.click("#submit"))
                    await wait_ui(pilot, lambda: form.connected)
                    submit.assert_called_once()
                    creation.assert_called_once()
                self.assertFalse(self.app.board.runs)
                self.assertFalse(list(self.runtime.root.glob("*/commands/*.json")))
                await pilot.press("escape")
                self.assertEqual(len(self.app.screen_stack), 1)

    async def test_create_and_start_leads_the_agent_form_buttons(self):
        async with self.app.run_test(size=(80, 24)) as pilot:
            self.app.action_new_agent()
            await wait_ui(pilot, lambda: isinstance(self.app.screen, NewAgentForm)
                          and bool(self.app.screen.query("#create-start")))
            form = self.app.screen
            assert isinstance(form, NewAgentForm)
            controls = list(form.query(".dialog-buttons Button").results(Button))
            self.assertEqual([button.id for button in controls], ["create-start", "submit", "cancel"])
            self.assertEqual(len({button.region.y for button in controls}), 1)
            self.assertEqual([button.region.x for button in controls], sorted(button.region.x for button in controls))
            await pilot.press("escape")
            self.assertFalse(list(self.runtime.root.glob("*/commands/*.json")))
            self.assertFalse(self.app.board.runs)

    async def test_connection_only_still_puts_its_available_action_first(self):
        async with self.app.run_test(size=(80, 24)) as pilot:
            self.app.project_name = "测试项目"
            self.app.new_agent_for(None)
            await wait_ui(pilot, lambda: isinstance(self.app.screen, NewAgentForm)
                          and bool(self.app.screen.query("#submit")))
            form = self.app.screen
            assert isinstance(form, NewAgentForm)
            self.assertEqual([b.id for b in form.query(".dialog-buttons Button").results(Button)], ["submit", "cancel"])
            self.assertFalse(form.query("#create-start"))
            await pilot.press("escape")
            self.assertFalse(self.app.board.runs)
            self.assertFalse(list(self.runtime.root.glob("*/commands/*.json")))

    async def test_create_cancel_then_connect_assign_without_dispatch_at_80_columns(self):
        with patch("app.create_pi", side_effect=self.create) as create:
            async with self.app.run_test(size=(80, 24)) as pilot:
                await pilot.press("a")
                await wait_ui(pilot, lambda: isinstance(self.app.screen, Choice))
                await pilot.press("enter")
                self.assertIsInstance(self.app.screen, NewAgentForm)
                self.assertEqual(self.app.screen.query_one("#agent-name", Input).value,
                                 self.app.board.tasks[1].title)
                await pilot.press("escape")
                create.assert_not_called()
                await pilot.press("ctrl+k", *"新建 Pi", "enter")
                self.assertIsInstance(self.app.screen, NewAgentForm)
                self.assertEqual(self.app.screen.query_one("#agent-name", Input).value,
                                 self.app.board.tasks[1].title)
                await pilot.press("home", "shift+end")
                self.app.post_message(events.Paste("我的 Pi，独立会话"))
                await pilot.pause()
                await pilot.press("tab", *"fixture/model", "ctrl+enter")
                form = self.app.screen
                assert isinstance(form, NewAgentForm)
                await wait_ui(pilot, lambda: form.connected)
                self.assertEqual(self.app.board.tasks[1].agent, form.launch_id)
                self.assertEqual(self.app.board.tasks[1].run, "none")
                self.assertEqual(self.app.board.runs, {})
                self.assertIn("按 s", str(form.query_one("#launch-result", Static).content))
                self.assertTrue(form.query_one("#submit", Button).region.height)
                await pilot.press("ctrl+enter")
                self.assertNotIsInstance(self.app.screen, NewAgentForm)
                create.assert_called_once()
                self.assertEqual(create.call_args.args[2:4], ("我的 Pi，独立会话", "fixture/model"))
                self.assertFalse(list(self.runtime.root.glob("*/commands/*.json")))
        restored = self.store.load()
        self.assertEqual(restored.tasks[1].run, "none")
        self.assertEqual(restored.runs, {})

    async def approve_create_and_start(self, pilot):
        self.app.action_new_agent()
        await pilot.pause()
        form = self.app.screen
        assert isinstance(form, NewAgentForm)
        self.assertTrue(form.query("#create-start"), "新建窗口应提供无需返回再按 s 的明确启动入口")
        self.assertTrue(await pilot.click("#create-start"))
        self.assertIsInstance(self.app.screen, Confirm)
        await pilot.press("ctrl+enter")
        return form

    async def test_create_and_start_confirms_scope_then_dispatches_once_after_commit(self):
        self.app.board.tasks[1].description = "只实现指定功能，不扩大范围。"
        contract = WorkContract(
            purpose="implementation",
            deliverable="完成整任务交付",
            allowed_scope="项目目录",
            verification="回归测试通过",
            exclusions="不修改其他模块",
        )
        self.app.board.tasks[1].remaining = contract.compile()
        self.store.save(self.app.board)
        original_dispatch = self.runtime.dispatch

        def committed_dispatch(run):
            with Store(self.store.path) as observer:
                self.assertEqual(observer.load().runs[run.id], run)
            original_dispatch(run)

        with patch("app.create_pi", side_effect=self.create) as create, patch.object(self.runtime, "dispatch", side_effect=committed_dispatch) as dispatch:
            async with self.app.run_test(size=(80, 24)) as pilot:
                self.app.action_new_agent()
                await pilot.pause()
                form = self.app.screen
                assert isinstance(form, NewAgentForm)
                self.assertTrue(form.query("#create-start"))
                button = form.query_one("#create-start", Button)
                self.assertLessEqual(button.region.bottom, 24)
                self.assertLessEqual(button.region.right, 80)
                await pilot.click("#create-start")
                confirm = self.app.screen
                assert isinstance(confirm, Confirm)
                for value in ("D001", str(self.project), self.app.board.tasks[1].description, self.app.board.tasks[1].remaining):
                    self.assertIn(value, confirm.body)
                await pilot.press("enter")  # The default still cancels execution.
                create.assert_not_called()
                dispatch.assert_not_called()
                await pilot.pause(0.3)
                await pilot.click("#create-start")
                await pilot.press("ctrl+enter", "ctrl+enter")
                await wait_ui(pilot, lambda: len(self.app.screen_stack) == 1 and len(self.app.board.runs) == 1)
                self.assertFalse(self.app._sending)
                task = self.app.board.tasks[1]
                run = self.app.board.runs[task.run_id]
                self.assertEqual((task.run, run.state, run.seq), ("queued", "queued", 0))
                self.assertEqual(run.agent_id, form.launch_id)
                self.assertEqual(run.requirements, contract.compile())
                form.poll_connection()
                await pilot.pause()
                create.assert_called_once()
                dispatch.assert_called_once()
                self.assertEqual(len(list(self.runtime.root.glob("*/commands/*.json"))), 1, task.events)

    async def test_cancel_waiting_connection_prevents_late_dispatch(self):
        connection = {}

        def delayed_connection(*args):
            launch = self.create(*args)
            path = self.runtime.root / launch.id / "agent.json"
            connection.update(path=path, row=self.runtime.agent(launch.id))
            path.unlink()
            return launch

        with patch("app.create_pi", side_effect=delayed_connection) as create, patch.object(self.runtime, "dispatch") as dispatch:
            async with self.app.run_test(size=(80, 24)) as pilot:
                form = await self.approve_create_and_start(pilot)
                await wait_ui(pilot, lambda: form.launch is not None and not form.creating)
                self.assertFalse(form.connected)
                await pilot.press("escape")
                self.assertEqual(len(self.app.screen_stack), 1)
                publish(connection["path"], {"version": 1, **asdict(connection["row"])})
                await form.read_connection()
                dispatch.assert_not_called()
                self.assertEqual(self.store.load().runs, {})
                self.assertEqual(self.app.board.tasks[1].agent, "")
                create.assert_called_once()

    async def test_assignment_save_failure_does_not_dispatch_or_recreate(self):
        with patch("app.create_pi", side_effect=self.create) as create, patch.object(self.runtime, "dispatch") as dispatch:
            async with self.app.run_test(size=(80, 24)) as pilot:
                with patch.object(self.store, "save", side_effect=StorageError("fixture assignment save failure")):
                    form = await self.approve_create_and_start(pilot)
                    await wait_ui(pilot, lambda: form.connected)
                self.assertIn("分配未保存", str(form.query_one("#form-error", Static).content))
                self.assertEqual(self.app.board.tasks[1].agent, "")
                self.assertEqual(self.store.load().runs, {})
                form.poll_connection()
                await pilot.pause()
                dispatch.assert_not_called()
                create.assert_called_once()

    async def test_lost_dispatch_receipt_keeps_one_intent_and_never_retries(self):
        original_dispatch = self.runtime.dispatch

        def uncertain(run):
            original_dispatch(run)
            raise OSError("fixture lost receipt after publish")

        with patch("app.create_pi", side_effect=self.create) as create, patch.object(self.runtime, "dispatch", side_effect=uncertain) as dispatch:
            async with self.app.run_test(size=(80, 24)) as pilot:
                form = await self.approve_create_and_start(pilot)
                await wait_ui(pilot, lambda: len(self.app.board.runs) == 1 and not self.app._sending)
                self.assertIs(self.app.screen, form)
                self.assertIn("派发未确认", str(form.query_one("#form-error", Static).content))
                self.assertTrue(form.query_one("#create-start", Button).disabled)
                form.poll_connection()
                await pilot.pause()
                dispatch.assert_called_once()
                create.assert_called_once()
                self.assertEqual(len(self.store.load().runs), 1)
                self.assertEqual(len(list(self.runtime.root.glob("*/commands/*.json"))), 1)

    async def test_connected_agent_can_start_here_without_returning_or_pressing_s(self):
        with patch("app.create_pi", side_effect=self.create) as create:
            async with self.app.run_test(size=(80, 24)) as pilot:
                self.app.action_new_agent()
                await pilot.pause()
                form = self.app.screen
                assert isinstance(form, NewAgentForm)
                await pilot.press("ctrl+enter")
                await wait_ui(pilot, lambda: form.connected)
                self.assertEqual(self.app.board.runs, {})
                self.assertEqual(str(form.query_one("#create-start", Button).label), "启动本任务")
                await pilot.click("#create-start")
                confirm = self.app.screen
                assert isinstance(confirm, Confirm)
                self.assertIn(form.launch_id, confirm.body)
                await pilot.press("ctrl+enter")
                await wait_ui(pilot, lambda: len(self.app.screen_stack) == 1 and not self.app._sending)
                self.assertEqual(len(self.store.load().runs), 1)
                create.assert_called_once()
                self.assertEqual(len(list(self.runtime.root.glob("*/commands/*.json"))), 1)

    async def test_task_changed_during_creation_cannot_receive_approved_old_scope(self):
        def changed(*args):
            launch = self.create(*args)
            self.app.board.tasks[1].description = "different scope"
            return launch

        with patch("app.create_pi", side_effect=changed) as create, patch.object(self.runtime, "dispatch") as dispatch:
            async with self.app.run_test(size=(120, 30)) as pilot:
                form = await self.approve_create_and_start(pilot)
                await wait_ui(pilot, lambda: form.connected)
                self.assertIn("原任务", str(form.query_one("#form-error", Static).content))
                self.assertEqual(self.app.board.tasks[1].agent, "")
                self.assertTrue(form.query_one("#create-start", Button).disabled)
                dispatch.assert_not_called()
                create.assert_called_once()
                self.assertEqual(self.store.load().runs, {})

    async def test_changed_session_at_dispatch_preflight_never_publishes(self):
        original_agent = self.runtime.agent
        reads = 0

        def changed(agent_id):
            nonlocal reads
            agent = original_agent(agent_id)
            reads += 1
            return replace(agent, session_id=str(uuid4())) if reads >= 2 else agent

        with patch("app.create_pi", side_effect=self.create), patch.object(self.runtime, "agent", side_effect=changed), patch.object(self.runtime, "dispatch") as dispatch:
            async with self.app.run_test(size=(80, 24)) as pilot:
                form = await self.approve_create_and_start(pilot)
                await wait_ui(pilot, lambda: form.start_attempted and not form.starting)
                self.assertIn("会话身份已变化", str(form.query_one("#form-error", Static).content))
                self.assertEqual(self.store.load().runs, {})
                dispatch.assert_not_called()

    async def test_task_removed_during_preflight_does_not_publish(self):
        original_agent = self.runtime.agent
        reads = 0

        def removed(agent_id):
            nonlocal reads
            agent = original_agent(agent_id)
            reads += 1
            if reads >= 2:
                self.app.board.tasks.pop(1, None)
            return agent

        with patch("app.create_pi", side_effect=self.create), patch.object(self.runtime, "agent", side_effect=removed), patch.object(self.runtime, "dispatch") as dispatch:
            async with self.app.run_test(size=(80, 24)) as pilot:
                form = await self.approve_create_and_start(pilot)
                await wait_ui(pilot, lambda: form.start_attempted and not form.starting)
                self.assertIn("派发未确认", str(form.query_one("#form-error", Static).content))
                self.assertEqual(self.store.load().runs, {})
                dispatch.assert_not_called()

    async def test_intent_save_failure_prevents_publish_after_assignment(self):
        original_save = self.store.save

        def failed_intent(board):
            if board.runs:
                raise StorageError("fixture intent save failure")
            original_save(board)

        with patch("app.create_pi", side_effect=self.create), patch.object(self.store, "save", side_effect=failed_intent), patch.object(self.runtime, "dispatch") as dispatch:
            async with self.app.run_test(size=(80, 24)) as pilot:
                form = await self.approve_create_and_start(pilot)
                await wait_ui(pilot, lambda: form.start_attempted and not form.starting)
                self.assertIn("派发未确认", str(form.query_one("#form-error", Static).content))
                self.assertEqual(self.store.load().runs, {})
                self.assertEqual(self.app.board.tasks[1].run, "none")
                self.assertEqual(self.app.board.tasks[1].agent, form.launch_id)
                dispatch.assert_not_called()
                form.poll_connection()
                await pilot.pause()
                dispatch.assert_not_called()

    async def test_unresolved_task_only_allows_an_independent_connection(self):
        agent = str(uuid4())
        self.app.apply_change(lambda board: board.assign(board.tasks[1], agent))
        self.app.apply_change(lambda board: board.queue_real(board.tasks[1], str(uuid4()), str(uuid4()), str(self.project)))
        with patch("app.create_pi", side_effect=self.create):
            async with self.app.run_test(size=(80, 24)) as pilot:
                self.app.action_new_agent()
                await pilot.pause()
                form = self.app.screen
                assert isinstance(form, NewAgentForm)
                self.assertFalse(form.query("#create-start"))
                await pilot.press("ctrl+enter")
                await wait_ui(pilot, lambda: form.connected)
                self.assertEqual(self.app.board.tasks[1].agent, agent)
                self.assertEqual(len(self.store.load().runs), 1)
                self.assertFalse(list(self.runtime.root.glob("*/commands/*.json")))

    async def test_long_task_name_is_bounded_without_changing_title_or_reusing_identity(self):
        title = "检查中文任务🧪" * 35
        self.app.apply_change(lambda board: board.edit(board.tasks[1], title, "测试项目", ""))
        with patch("app.create_pi", side_effect=self.create) as create:
            async with self.app.run_test(size=(80, 24)) as pilot:
                self.app.action_new_agent()
                await pilot.pause()
                form = self.app.screen
                assert isinstance(form, NewAgentForm)
                self.assertEqual(form.query_one("#agent-name", Input).value, title[:80])
                first_identity = form.launch_id
                await pilot.press("escape")
                create.assert_not_called()
                self.app.action_new_agent()
                await pilot.pause()
                form = self.app.screen
                assert isinstance(form, NewAgentForm)
                self.assertNotEqual(form.launch_id, first_identity)
                await pilot.press("ctrl+enter")
                await wait_ui(pilot, lambda: form.connected)
                create.assert_called_once()
                self.assertEqual(create.call_args.args[2], title[:80])
                self.assertEqual(self.runtime.agent(form.launch_id).name, title[:80])
        self.assertEqual(self.store.load().tasks[1].title, title)

    async def test_project_only_creation_keeps_directory_name_without_assigning_task(self):
        with patch("app.create_pi", side_effect=self.create) as create:
            async with self.app.run_test(size=(80, 24)) as pilot:
                self.app.set_project("测试项目")
                self.app.new_agent_for(None)
                await pilot.pause()
                form = self.app.screen
                assert isinstance(form, NewAgentForm)
                name = f"Pi · {self.project.name}"
                self.assertEqual(form.query_one("#agent-name", Input).value, name)
                self.assertFalse(form.query("#create-start"))
                await pilot.press("ctrl+enter")
                await wait_ui(pilot, lambda: form.connected)
                create.assert_called_once()
                self.assertEqual(create.call_args.args[2], name)
                self.assertEqual(self.app.board.tasks[1].agent, "")
                self.assertEqual(self.app.board.runs, {})

    async def test_slow_creation_is_single_and_does_not_assign_changed_task(self):
        entered, release = threading.Event(), threading.Event()
        def slow(*args):
            entered.set()
            if not release.wait(5):
                raise AssertionError("test did not release its worker")
            return self.create(*args)
        with patch("app.create_pi", side_effect=slow) as create:
            async with self.app.run_test(size=(120, 30)) as pilot:
                await pilot.press("ctrl+k", *"新建 Pi", "enter", "ctrl+enter")
                try:
                    await wait_ui(pilot, entered.is_set)
                    await pilot.press("ctrl+enter", "escape")
                    self.assertIsInstance(self.app.screen, NewAgentForm)
                    self.app.apply_change(lambda board: board.edit(board.tasks[1], "Changed", "测试项目", ""))
                finally:
                    release.set()
                form = self.app.screen
                assert isinstance(form, NewAgentForm)
                await wait_ui(pilot, lambda: form.connected)
                create.assert_called_once()
                self.assertEqual(self.app.board.tasks[1].agent, "")
                self.assertIn("原任务", str(form.query_one("#form-error", Static).content))

    async def test_unknown_creation_refresh_never_restarts_and_preflight_keeps_input(self):
        def unknown(runtime, directory, name, model, identity):
            return Launch(identity, identity, directory, name, model, "unknown", message="API timeout; 不会重试")
        with patch("app.create_pi", side_effect=[ValueError("outside Herdr")]) as create:
            async with self.app.run_test(size=(80, 24)) as pilot:
                self.app.action_new_agent()
                await pilot.pause()
                await pilot.press("ctrl+enter")
                form = self.app.screen
                assert isinstance(form, NewAgentForm)
                await wait_ui(pilot, lambda: not form.creating)
                self.assertIn("outside Herdr", str(form.query_one("#form-error", Static).content))
                self.assertTrue(form.query_one("#agent-name", Input).value)
                create.side_effect = unknown
                await pilot.press("ctrl+enter")
                await wait_ui(pilot, lambda: form.launch is not None)
                await pilot.press("ctrl+enter", "ctrl+enter")
                self.assertEqual(create.call_count, 2)
                self.assertFalse(self.app.board.tasks[1].agent)
                await pilot.press("escape")
                self.assertFalse(self.runtime.root.exists())


if __name__ == "__main__":
    unittest.main()
