"""User-facing creation contracts. No real Herdr/user session is touched."""
from dataclasses import asdict
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import patch

from textual import events
from textual.widgets import Button, Input, Static

from app import Choice, NewAgentForm, TaskRoom
from launch import Launch, prepare_launch
from runtime import Agent, Runtime, private_dir, publish
from store import Store
from test_live import wait_ui


class NewAgentUI(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = self.enterContext(TemporaryDirectory(prefix="taskroom-agent-ui-"))
        self.project = Path(self.temp) / "中文 project"
        self.project.mkdir()
        self.runtime = Runtime(Path(self.temp) / "bridges")
        self.store = self.enterContext(Store(Path(self.temp) / "tasks.sqlite3"))
        board = self.store.load()
        board.add_project("测试项目", str(self.project))
        board.create("Only an explicit s may execute", "测试项目")
        self.store.save(board)
        self.app = TaskRoom(store=self.store, runtime=self.runtime)

    def create(self, runtime, directory, name, model, identity):
        launch = prepare_launch(runtime, directory, name, model, identity)
        endpoint = runtime.root / launch.id
        for path in (endpoint, endpoint / "commands", endpoint / "runs"):
            private_dir(path, create=True)
        agent = Agent(launch.id, launch.session_id, directory, name, model or "fixture/default", os.getpid(), int(time.time() * 1000), True, True, "")
        publish(endpoint / "agent.json", {"version": 1, **asdict(agent)})
        launch.state, launch.tab_id, launch.pane_id = "starting", "test-tab", "test-pane"
        return launch

    async def test_create_cancel_then_connect_assign_without_dispatch_at_80_columns(self):
        with patch("app.create_pi", side_effect=self.create) as create:
            async with self.app.run_test(size=(80, 24)) as pilot:
                await pilot.press("a")
                await wait_ui(pilot, lambda: isinstance(self.app.screen, Choice))
                await pilot.press("enter")
                self.assertIsInstance(self.app.screen, NewAgentForm)
                await pilot.press("escape")
                create.assert_not_called()
                await pilot.press("ctrl+k", *"新建 Pi", "enter")
                self.assertIsInstance(self.app.screen, NewAgentForm)
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
