"""Live UI contracts; all Pi, projects, mailboxes and databases are disposable."""
import asyncio
from copy import deepcopy
from dataclasses import asdict
import json
import os
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from textual import events
from textual.containers import VerticalScroll
from textual.widgets import Button, Input, OptionList, Static, TextArea, Tree

from app import Choice, Confirm, Note, ProjectForm, TaskForm, TaskRoom
from model import Board
from records import TaskRecords
from runtime import Agent, Runtime, private_dir, publish
from store import Store
from test_bridge import PiFixture


async def wait_ui(pilot, predicate, timeout=10):
    async with asyncio.timeout(timeout):
        while not predicate():
            await pilot.pause(0.05)
    await pilot.pause()


@unittest.skipUnless(shutil.which("pi"), "installed Pi required for real-runtime UI test")
class LivePiUI(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = TemporaryDirectory(prefix="taskroom-live-ui-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.fixture = PiFixture(self.root)
        self.addAsyncCleanup(self.fixture.close)
        await self.fixture.start()
        agent = await self.fixture.connect()
        assert agent is not None
        self.agent = agent
        self.path = self.root / "tasks.sqlite3"

    async def test_keyboard_exact_project_dispatch_reopen_and_human_accept(self):
        with Store(self.path) as store:
            app = TaskRoom(store=store, runtime=self.fixture.runtime)
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.press("p", *"添加", "enter")
                self.assertIsInstance(app.screen, ProjectForm)
                await pilot.press(*"精确中文项目", "tab")
                app.post_message(events.Paste(str(self.fixture.project)))
                await pilot.pause()
                await pilot.press("ctrl+enter")
                await wait_ui(pilot, lambda: not isinstance(app.screen, ProjectForm))
                self.assertEqual(app.board.projects["精确中文项目"], str(self.fixture.project))
                self.assertEqual(app.project_name, "精确中文项目")
                await pilot.press("n", *"FULL", "enter", "a")
                await wait_ui(pilot, lambda: isinstance(app.screen, Choice))
                assert isinstance(app.screen, Choice)
                self.assertIn(self.agent.id, app.screen.options)
                await pilot.press("enter")
                self.assertEqual((app.board.tasks[1].run, app.board.tasks[1].agent), ("none", self.agent.id))
                self.assertFalse((self.fixture.project / "fixture-result.json").exists())
                await pilot.press("s")
                await wait_ui(pilot, lambda: isinstance(app.screen, Confirm))
                assert isinstance(app.screen, Confirm)
                self.assertIn(str(self.fixture.project), app.screen.body)
                self.assertIn(self.agent.session_id, app.screen.body)
                await pilot.press("escape")
                self.assertEqual(app.board.runs, {})
                await pilot.press("s")
                await wait_ui(pilot, lambda: isinstance(app.screen, Confirm))
                await pilot.press("ctrl+enter")
                await wait_ui(pilot, lambda: app.board.tasks[1].status == "review")
                current = app.board.runs[app.board.tasks[1].run_id]
                self.assertEqual((current.state, current.delivery), ("finished", "full"))
                self.assertTrue(app.board.tasks[1].can_accept)
                self.assertIn("自报", str(app.query_one("#detail-text", Static).content))
                self.assertIn("写入隔离测试文件", str(app.query_one("#detail-text", Static).content))
                await pilot.press("right")
                await pilot.click("#activity-button")
                assert isinstance(app.screen, TaskRecords)
                records = app.screen
                tree = records.query_one("#records-tree", Tree)
                report = next(node for node in tree.root.children[0].children if node.data and node.data[1] and node.data[1].kind == "full")
                tree.select_node(report)
                await pilot.pause()
                self.assertIn("Offline fixture report", str(records.query_one("#record-body", Static).content))
                await pilot.click("#locate-session")
                await wait_ui(pilot, lambda: isinstance(app.screen, Confirm))
                assert isinstance(app.screen, Confirm)
                self.assertIn("已核对原会话", app.screen.body)
                self.assertIn(current.session_id, app.screen.body)
                self.assertIn(current.id, app.screen.body)
                await pilot.press("escape", "escape")
                self.assertEqual(app.selected_id, 1)
                self.assertEqual(len(app.board.runs), 1)
        with Store(self.path) as store:
            app = TaskRoom(store=store, runtime=self.fixture.runtime)
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause()
                self.assertEqual(app.board.tasks[1].status, "review")
                await pilot.press("g", "r", "enter")  # Default cancel is safe, not acceptance.
                self.assertEqual(app.board.tasks[1].status, "review")
                await pilot.press("r", "ctrl+enter")
                self.assertEqual(app.board.tasks[1].status, "done")
        with Store(self.path) as store:
            app = TaskRoom(store=store, runtime=self.fixture.runtime)
            async with app.run_test(size=(180, 40)) as pilot:
                await pilot.press("v")
                self.assertEqual(app.board.tasks[1].status, "done")
                self.assertEqual(len(app.board.runs), 1)
                self.assertEqual(json.loads((self.fixture.project / "fixture-result.json").read_text())["counter"], 1)

    async def test_partial_delivery_rework_requires_separate_dispatch(self):
        with Store(self.path) as store:
            board = store.load()
            board.add_project("项目", str(self.fixture.project))
            task = board.create("PARTIAL", "项目")
            board.assign(task, self.agent.id)
            store.save(board)
            app = TaskRoom(store=store, runtime=self.fixture.runtime)
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.press("s")
                await wait_ui(pilot, lambda: isinstance(app.screen, Confirm))
                await pilot.press("ctrl+enter")
                await wait_ui(pilot, lambda: app.board.tasks[1].status == "review")
                previous = app.board.tasks[1].run_id
                self.assertFalse(app.board.tasks[1].can_accept)
                await pilot.press("r")
                self.assertNotIsInstance(app.screen, Confirm)
                await pilot.press("b", *"完成剩余工作", "ctrl+enter")
                self.assertEqual(app.board.tasks[1].run, "none")
                self.assertEqual(len(app.board.runs), 1)
                await pilot.press("e", "home", "shift+end", *"FULL", "enter", "s")
                await wait_ui(pilot, lambda: isinstance(app.screen, Confirm))
                assert isinstance(app.screen, Confirm)
                self.assertIn("完成剩余工作", app.screen.body)
                await pilot.press("ctrl+enter")
                await wait_ui(pilot, lambda: app.board.tasks[1].can_accept)
                current = app.board.runs[app.board.tasks[1].run_id]
                self.assertNotEqual(current.id, previous)
                self.assertEqual(current.requirements, "完成剩余工作")
                self.assertEqual(app.board.runs[previous].delivery, "partial")
                self.assertEqual(app.board.tasks[1].status, "review")
                self.assertEqual(json.loads((self.fixture.project / "fixture-result.json").read_text())["counter"], 2)

    async def test_keyboard_stop_waits_for_pi_and_rework_does_not_restart(self):
        with Store(self.path) as store:
            board = store.load()
            board.add_project("项目", str(self.fixture.project))
            task = board.create("SLOW", "项目")
            board.assign(task, self.agent.id)
            store.save(board)
            app = TaskRoom(store=store, runtime=self.fixture.runtime)
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.press("s")
                await wait_ui(pilot, lambda: isinstance(app.screen, Confirm))
                await pilot.press("ctrl+enter")
                await wait_ui(pilot, lambda: app.board.tasks[1].run == "working")
                await pilot.press("x", "ctrl+enter")
                await wait_ui(pilot, lambda: app.board.runs[app.board.tasks[1].run_id].terminal)
                self.assertEqual(app.board.runs[app.board.tasks[1].run_id].state, "stopped")
                self.assertFalse(app.board.tasks[1].can_accept)
                await pilot.press("b", *"稍后继续", "ctrl+enter")
                self.assertEqual(app.board.tasks[1].run, "none")
                self.assertEqual(len(app.board.runs), 1)
                self.assertFalse((self.fixture.project / "fixture-result.json").exists())


class LiveFailureUI(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="taskroom-live-failure-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / "non git 子目录"
        self.project.mkdir()
        self.path = self.root / "tasks.sqlite3"
        self.runtime = Runtime(self.root / "bridges")
        self.agent = Agent(str(uuid4()), str(uuid4()), str(self.project), "临时 Pi", "fixture", os.getpid(), int(time.time() * 1000), True, True, "")
        endpoint = self.runtime.root / self.agent.id
        for directory in (self.runtime.root, endpoint, endpoint / "commands", endpoint / "runs"):
            private_dir(directory, create=True)
        self.endpoint = endpoint
        publish(endpoint / "agent.json", {"version": 1, **asdict(self.agent)})
        self.store = self.enterContext(Store(self.path))
        board = self.store.load()
        board.add_project("真实项目", str(self.project))
        task = board.create("持久化意图", "真实项目")
        board.assign(task, self.agent.id)
        board.create("不抢焦点", "真实项目")
        self.store.save(board)

    def app(self, queued=False):
        if queued:
            board = self.store.load()
            board.queue_real(board.tasks[1], str(uuid4()), self.agent.session_id, self.agent.cwd)
            self.store.save(board)
        return TaskRoom(store=self.store, runtime=self.runtime)

    def result(self, run, state="working", seq=3):
        incoming = deepcopy(run)
        incoming.state, incoming.seq = state, seq
        incoming.summary = "Pi 自报进展\n" + "一段较长的交付说明。\n" * 40
        incoming.events = ["执行者报告 · progress · 旧版进展摘录。"]
        if state == "finished":
            incoming.delivery, incoming.checks = "full", ["Agent 自报检查；未独立核验"]
        file = self.endpoint / "runs" / f"{run.id}.json"
        file.unlink(missing_ok=True)
        publish(file, asdict(incoming))
        return incoming

    async def test_project_failure_keeps_typed_path_and_exact_subdirectory(self):
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("ctrl+k", *"添加真实", "enter", *"新增项目", "tab")
            form = app.screen
            assert isinstance(form, ProjectForm)
            directory = form.query_one("#project-path", Input)
            directory.value = str(self.root / "missing")
            await pilot.press("ctrl+enter")
            await wait_ui(pilot, lambda: not form.saving)
            self.assertIsInstance(app.screen, ProjectForm)
            self.assertIn("missing", directory.value)
            self.assertIn("未保存", str(app.screen.query_one("#form-error", Static).content))
            directory.value = str(self.root)
            self.store.connection.execute("PRAGMA query_only=ON")
            original = app.board
            await pilot.press("ctrl+enter")
            await wait_ui(pilot, lambda: not form.saving)
            self.assertIs(app.board, original)
            self.assertEqual(directory.value, str(self.root))
            self.assertEqual(app.screen.query_one("#project-name", Input).value, "新增项目")
            self.store.connection.execute("PRAGMA query_only=OFF")
            await pilot.press("ctrl+enter")
            await wait_ui(pilot, lambda: not isinstance(app.screen, ProjectForm))
            self.assertEqual(app.board.projects["新增项目"], str(self.root))

    async def test_failed_intent_never_publishes_and_lost_publish_never_retries(self):
        app = self.app()
        async with app.run_test(size=(120, 30)) as pilot:
            self.store.connection.execute("PRAGMA query_only=ON")
            with patch.object(self.runtime, "dispatch", wraps=self.runtime.dispatch) as send:
                await app.dispatch_task(deepcopy(app.board.tasks[1]), self.agent)
                send.assert_not_called()
            self.assertEqual(app.board.runs, {})
            self.store.connection.execute("PRAGMA query_only=OFF")
            original_send = self.runtime.dispatch
            def uncertain(run):
                self.assertNotEqual(threading.get_ident(), threading.main_thread().ident)
                with Store(self.path) as observer:
                    self.assertEqual(observer.load().runs[run.id], run)
                original_send(run)
                raise OSError("simulated lost publication result")
            with patch.object(self.runtime, "dispatch", side_effect=uncertain) as send:
                await app.dispatch_task(deepcopy(app.board.tasks[1]), self.agent)
                await pilot.press("s", "g")
                await wait_ui(pilot, lambda: not app._polling)
                self.assertEqual(send.call_count, 1)
            self.assertEqual(len(app.board.runs), 1)
            self.assertEqual(app.board.tasks[1].run, "queued")
            self.assertTrue(any("可能已发布" in event for event in app.board.tasks[1].events))
            self.assertEqual(len(list((self.endpoint / "commands").glob("*.json"))), 1)
        with Store(self.path) as reopened, patch.object(self.runtime, "dispatch") as send:
            again = TaskRoom(store=reopened, runtime=self.runtime)
            async with again.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                send.assert_not_called()
                self.assertEqual(again.board.tasks[1].run, "queued")
                self.assertEqual(len(list((self.endpoint / "commands").glob("*.json"))), 1)

    async def test_result_save_failure_retries_observation_without_disturbing_form(self):
        app = self.app(queued=True)
        run = app.board.runs[app.board.tasks[1].run_id]
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.press("down", "e", "home", "shift+end", *"保留输入 nvasfg")
            form, focused = app.screen, app.focused
            self.store.connection.execute("PRAGMA query_only=ON")
            self.result(run, "finished")
            app.schedule_poll()
            await wait_ui(pilot, lambda: not app._polling)
            self.assertEqual(app.board.runs[run.id].seq, 0)
            self.assertTrue(app._poll_save_error)
            self.assertIs(app.screen, form)
            self.assertIs(app.focused, focused)
            self.assertEqual(app.selected_id, 2)
            self.assertEqual(form.query_one("#task-title", Input).value, "保留输入 nvasfg")
            self.store.connection.execute("PRAGMA query_only=OFF")
            app.schedule_poll()
            await wait_ui(pilot, lambda: app.board.tasks[1].status == "review")
            self.assertIs(app.focused, focused)
            self.assertEqual(app.board.runs[run.id].seq, 3)
            self.assertFalse(list((self.endpoint / "commands").iterdir()))
            await pilot.press("escape")
            app.schedule_poll()
            await wait_ui(pilot, lambda: not app._visual_dirty)
            self.assertEqual(app.selected_id, 2)
            with Store(self.path) as observer:
                self.assertEqual(observer.load().tasks, app.board.tasks)

    async def test_progress_keeps_search_selection_scroll_and_responsive_input(self):
        app = self.app(queued=True)
        run = app.board.runs[app.board.tasks[1].run_id]
        self.result(run)
        async with app.run_test(size=(120, 30)) as pilot:
            await wait_ui(pilot, lambda: app.board.runs[run.id].seq == 3)
            await pilot.press("enter")
            await pilot.click("#detail-toggle")
            await pilot.press("pagedown")
            await pilot.pause()
            scroll = app.query_one("#detail-scroll", VerticalScroll)
            self.assertGreater(scroll.scroll_y, 0)
            old_scroll = scroll.scroll_y
            self.result(run, seq=4)
            app.schedule_poll()
            await wait_ui(pilot, lambda: app.board.runs[run.id].seq == 4)
            self.assertEqual(app.selected_id, 1)
            self.assertEqual(scroll.scroll_y, old_scroll)
            await pilot.press("escape", "slash", *"持久化")
            focused = app.focused
            release = threading.Event()
            started = threading.Event()
            original_read = self.runtime.read_run
            def slow_read(attempt):
                self.assertNotEqual(threading.get_ident(), threading.main_thread().ident)
                started.set()
                if not release.wait(5):
                    raise TimeoutError("test gate was not released")
                return original_read(attempt)
            with patch.object(self.runtime, "read_run", side_effect=slow_read):
                try:
                    app.schedule_poll()
                    await wait_ui(pilot, started.is_set)
                    await pilot.press(*"意图")
                    self.assertEqual(app.query_one("#search", Input).value, "持久化意图")
                    self.assertIs(app.focused, focused)
                finally:
                    release.set()
                await wait_ui(pilot, lambda: not app._polling)
            self.assertEqual(app.selected_id, 1)

    async def test_stale_run_requires_explicit_human_resolution_and_note(self):
        app = self.app(queued=True)
        run = app.board.runs[app.board.tasks[1].run_id]
        metadata = self.endpoint / "agent.json"
        metadata.unlink()
        publish(metadata, {"version": 1, **asdict(self.agent), "heartbeat": 1})
        async with app.run_test(size=(80, 24)) as pilot:
            await wait_ui(pilot, lambda: run.id in app.runtime_errors)
            self.assertFalse(app.board.runs[run.id].terminal)
            await pilot.press("ctrl+k", *"失联", "enter", "escape")
            self.assertFalse(app.board.runs[run.id].terminal)
            await pilot.press("ctrl+k", *"失联", "enter", "ctrl+enter")
            self.assertIsInstance(app.screen, Note)
            await pilot.press("ctrl+enter")
            self.assertFalse(app.board.runs[run.id].terminal)
            await pilot.press(*"已在隔离测试核实旧 Pi 不存在", "ctrl+enter")
            await wait_ui(pilot, lambda: app.board.runs[run.id].terminal)
            self.assertEqual(app.board.runs[run.id].state, "abandoned")
            await pilot.press("b", *"之后重新安排", "ctrl+enter")
            self.assertEqual(app.board.tasks[1].run, "none")
            self.assertEqual(len(app.board.runs), 1)
            self.assertFalse(list((self.endpoint / "commands").iterdir()))

    async def test_stop_must_commit_before_publish_and_wrong_directory_is_not_offered(self):
        app = self.app(queued=True)
        run = app.board.runs[app.board.tasks[1].run_id]
        async with app.run_test(size=(120, 30)) as pilot:
            self.store.connection.execute("PRAGMA query_only=ON")
            with patch.object(self.runtime, "stop") as stop:
                await app.stop_run(run)
                stop.assert_not_called()
            self.assertFalse(app.board.runs[run.id].terminal)
            self.store.connection.execute("PRAGMA query_only=OFF")
            metadata = self.endpoint / "agent.json"
            metadata.unlink()
            publish(metadata, {"version": 1, **asdict(self.agent), "cwd": str(self.root)})
            await pilot.press("down", "a")
            await wait_ui(pilot, lambda: isinstance(app.screen, Choice))
            assert isinstance(app.screen, Choice)
            self.assertNotIn(self.agent.id, app.screen.options)
            self.assertIn("__new_pi__", app.screen.options)
            await pilot.press(*"帮助", "enter")
            assert isinstance(app.screen, Confirm)
            self.assertEqual(app.screen.title_text, "明确接入 Pi")
            self.assertEqual(app.board.tasks[2].agent, "")


if __name__ == "__main__":
    unittest.main()
