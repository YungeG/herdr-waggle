"""Live UI contracts; all Pi, projects, mailboxes and databases are disposable."""

import asyncio
import json
import os
import shutil
import threading
import time
import unittest
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from textual import events
from textual.containers import VerticalScroll
from textual.widgets import Input, OptionList, Select, Static, TextArea, Tree

from app import (
    Choice,
    Confirm,
    Note,
    ProjectForm,
    RecoveryForm,
    Waggle,
    WorkContractForm,
)
from decisions import review_fields, review_requirements
from records import TaskRecords
from runtime import Agent, Runtime, private_dir, publish
from store import Store, encode
from task_context import WorkContract
from test_bridge import PiFixture
from test_capabilities import capability_record
from test_continuations import session_record

PARTIAL_COMPLETION_GOAL = "完成剩余工作 · FIXTURE_COMPLETE_PARTIAL"


def fixture_contract(directory):
    return WorkContract("implementation", "完成本次隔离离线任务", str(directory),
                        "fixture-result.json 与完整离线报告一致",
                        "不访问真实任务库，不扩大目录，不批准新的预算或试验")


def fill_recovery(form, directory, checkpoint, remaining):
    assert isinstance(form, RecoveryForm)
    assert form.query_one("#contract-purpose", Select).value == Select.NULL
    contract = fixture_contract(directory)
    form.query_one("#contract-purpose", Select).value = contract.purpose
    for key in ("deliverable", "allowed_scope", "verification", "exclusions"):
        field = form.query_one("#contract-" + key, TextArea)
        assert field.text == ""
        field.text = getattr(contract, key)
    for key, value in (("checkpoint", checkpoint), ("remaining", remaining)):
        field = form.query_one("#recovery-" + key, TextArea)
        assert field.text == ""
        field.text = value


async def wait_ui(pilot, predicate, timeout=10):
    async with asyncio.timeout(timeout):
        while not predicate():
            await pilot.pause(0.05)
    await pilot.pause()


@unittest.skipUnless(shutil.which("pi"), "installed Pi required for real-runtime UI test")
class LivePiUI(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = TemporaryDirectory(prefix="waggle-live-ui-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.fixture = PiFixture(self.root)
        self.addAsyncCleanup(self.fixture.close)
        await self.fixture.start()
        agent = await self.fixture.connect()
        assert agent is not None
        self.agent = agent
        self.path = self.root / "tasks.sqlite3"

    async def declare_contract(self, app, pilot):
        await pilot.press("ctrl+k", *"工作合同", "enter")
        await wait_ui(pilot, lambda: isinstance(app.screen, WorkContractForm))
        form = app.screen
        assert isinstance(form, WorkContractForm)
        self.assertEqual(form.query_one("#contract-purpose", Select).value, Select.NULL)
        contract = fixture_contract(self.fixture.project)
        form.query_one("#contract-purpose", Select).value = contract.purpose
        for key in ("deliverable", "allowed_scope", "verification", "exclusions"):
            field = form.query_one("#contract-" + key, TextArea)
            self.assertEqual(field.text, "")
            field.text = getattr(contract, key)
        await pilot.pause()
        self.assertTrue(await pilot.click("#contract-save"))
        await wait_ui(pilot, lambda: len(app.screen_stack) == 1)
        self.assertEqual(app.board.tasks[1].remaining, contract.compile())
        self.assertEqual(app.board.runs, {})
        self.assertFalse(list(self.fixture.runtime.root.glob("*/commands/*.json")))

    async def test_keyboard_exact_project_dispatch_reopen_and_human_accept(self):
        with Store(self.path) as store:
            app = Waggle(store=store, runtime=self.fixture.runtime)
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
                await self.declare_contract(app, pilot)
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
                app.action_activity()
                await pilot.pause()
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
            app = Waggle(store=store, runtime=self.fixture.runtime)
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause()
                self.assertEqual(app.board.tasks[1].status, "review")
                await pilot.press("g", "r", "enter")  # Default cancel is safe, not acceptance.
                self.assertEqual(app.board.tasks[1].status, "review")
                await pilot.press("r", "ctrl+enter")
                self.assertEqual(app.board.tasks[1].status, "done")
        with Store(self.path) as store:
            app = Waggle(store=store, runtime=self.fixture.runtime)
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
            board.set_work_contract(task, fixture_contract(self.fixture.project))
            board.assign(task, self.agent.id)
            store.save(board)
            app = Waggle(store=store, runtime=self.fixture.runtime)
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.press("s")
                await wait_ui(pilot, lambda: isinstance(app.screen, Confirm))
                await pilot.press("ctrl+enter")
                await wait_ui(pilot, lambda: app.board.tasks[1].status == "review")
                previous = app.board.tasks[1].run_id
                previous_run = deepcopy(app.board.runs[previous])
                self.assertFalse(app.board.tasks[1].can_accept)
                await pilot.press("r")
                self.assertNotIsInstance(app.screen, Confirm)
                await pilot.press("b")
                await wait_ui(pilot, lambda: isinstance(app.screen, Choice))
                choices = app.screen.query_one("#choices", OptionList)
                choices.highlighted = next(index for index in range(choices.option_count)
                                           if choices.get_option_at_index(index).id == "review")
                await pilot.press("enter")  # Choose review, then save-only: no implicit execution.
                await wait_ui(pilot, lambda: isinstance(app.screen, Note))
                app.screen.query_one(TextArea).text = PARTIAL_COMPLETION_GOAL
                app.screen.query_one("#review-scope", Input).value = "仅临时测试项目目录的离线计数器"
                app.screen.query_one("#review-verification", Input).value = "fixture-result.json 计数为2且真实回执完整"
                await pilot.pause()
                await pilot.click("#save-only")
                await wait_ui(pilot, lambda: len(app.screen_stack) == 1)
                self.assertEqual(app.board.tasks[1].run, "none")
                self.assertEqual(len(app.board.runs), 1)
                expected_request = review_requirements(
                    PARTIAL_COMPLETION_GOAL, "仅临时测试项目目录的离线计数器", "fixture-result.json 计数为2且真实回执完整")
                self.assertEqual(app.board.tasks[1].remaining, expected_request)
                before = encode(store.load())
                commands_before = sorted(path.name for path in self.fixture.runtime.root.glob("*/commands/*.json"))
                app.board.tasks[1].title = "FULL"  # Ephemeral drift must fail before preflight, not rewrite the saved source.
                with patch.object(self.fixture.runtime, "check_dispatch", wraps=self.fixture.runtime.check_dispatch) as preflight:
                    await app.confirm_dispatch(1)
                    preflight.assert_not_called()
                self.assertEqual(len(app.screen_stack), 1)
                self.assertEqual(encode(store.load()), before)
                self.assertEqual(len(app.board.runs), 1)
                self.assertEqual(sorted(path.name for path in self.fixture.runtime.root.glob("*/commands/*.json")), commands_before)
                app.board.tasks[1].title = "PARTIAL"
                app.refresh_tasks()
                await pilot.press("s")
                await wait_ui(pilot, lambda: isinstance(app.screen, Confirm))
                assert isinstance(app.screen, Confirm)
                self.assertIn("完成剩余工作", app.screen.body)
                await pilot.press("ctrl+enter")
                await wait_ui(pilot, lambda: app.board.tasks[1].can_accept)
                current = app.board.runs[app.board.tasks[1].run_id]
                self.assertNotEqual(current.id, previous)
                fields = review_fields(current.requirements)
                assert fields is not None
                self.assertEqual(fields, (PARTIAL_COMPLETION_GOAL, "仅临时测试项目目录的离线计数器",
                                          "fixture-result.json 计数为2且真实回执完整"))
                self.assertEqual(current.requirements, expected_request)
                self.assertEqual((current.title, current.description, current.cwd),
                                 (previous_run.title, previous_run.description, previous_run.cwd))
                self.assertEqual(app.board.tasks[1].title, "PARTIAL")
                self.assertEqual(app.board.runs[previous], previous_run)
                self.assertEqual(store.load().runs[previous], previous_run)
                self.assertEqual(app.board.tasks[1].status, "review")
                self.assertEqual(json.loads((self.fixture.project / "fixture-result.json").read_text())["counter"], 2)

    async def test_partial_completion_marker_in_reference_or_other_deliverable_stays_partial(self):
        quoted = json.dumps(review_requirements(PARTIAL_COMPLETION_GOAL, str(self.fixture.project),
                                               "旧引用不是本次完成依据"), ensure_ascii=False)
        literal_reference = ("旧模型上下文仅引用，不是当前请求：\nRework / remaining requirements:\n"
                             + review_requirements(PARTIAL_COMPLETION_GOAL, str(self.fixture.project), "旧引用不是本次完成依据")
                             + "\n\nWork only on this task\n旧上下文引用结束。")
        cases = (("只核对已有材料；历史引用不是本轮交付物：\n" + quoted, "历史文字仅供参考：" + quoted),
                 (PARTIAL_COMPLETION_GOAL + "（历史引用，不是本次完成交付）", "历史文字仅供参考：" + quoted),
                 ("只核对当前隔离材料，不请求完成", literal_reference))
        with Store(self.path) as store:
            for counter, (goal, description) in enumerate(cases, 1):
                with self.subTest(goal=goal, description=description):
                    board = store.load()
                    if "项目" not in board.projects:
                        board.add_project("项目", str(self.fixture.project))
                    task = board.create("PARTIAL", "项目", description)
                    request = review_requirements(goal, str(self.fixture.project), "隔离回执为 partial，不能整任务验收")
                    board.set_work_contract(task, fixture_contract(self.fixture.project))
                    task.remaining = request  # Explicit current bounded review, not the quoted completed deliverable.
                    board.assign(task, self.agent.id)
                    store.save(board)
                    app = Waggle(store=store, runtime=self.fixture.runtime)
                    app.selected_id = task.id
                    async with app.run_test(size=(80, 24)) as pilot:
                        await wait_ui(pilot, lambda: self.fixture.runtime.agent(self.agent.id).available)
                        await pilot.press("s")
                        await wait_ui(pilot, lambda: isinstance(app.screen, Confirm))
                        self.assertEqual(len(app.board.runs), counter - 1)
                        await pilot.press("ctrl+enter")
                        await wait_ui(pilot, lambda: app.board.tasks[task.id].status == "review")
                        run = app.board.runs[app.board.tasks[task.id].run_id]
                        self.assertEqual((run.state, run.delivery), ("finished", "partial"))
                        self.assertEqual(run.requirements, request)
                        self.assertFalse(app.board.tasks[task.id].can_accept)
                        self.assertEqual(json.loads((self.fixture.project / "fixture-result.json").read_text())["counter"], counter)

    async def test_keyboard_stop_waits_for_pi_and_rework_does_not_restart(self):
        with Store(self.path) as store:
            board = store.load()
            board.add_project("项目", str(self.fixture.project))
            task = board.create("SLOW", "项目")
            board.set_work_contract(task, fixture_contract(self.fixture.project))
            board.assign(task, self.agent.id)
            store.save(board)
            app = Waggle(store=store, runtime=self.fixture.runtime)
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.press("s")
                await wait_ui(pilot, lambda: isinstance(app.screen, Confirm))
                await pilot.press("ctrl+enter")
                await wait_ui(pilot, lambda: app.board.tasks[1].run == "working")
                await pilot.press("x", "ctrl+enter")
                await wait_ui(pilot, lambda: app.board.runs[app.board.tasks[1].run_id].terminal)
                self.assertEqual(app.board.runs[app.board.tasks[1].run_id].state, "stopped")
                self.assertFalse(app.board.tasks[1].can_accept)
                stopped = deepcopy(app.board.runs[app.board.tasks[1].run_id])
                commands_before = sorted(path.name for path in self.fixture.runtime.root.glob("*/commands/*.json"))
                await pilot.press("ctrl+k", *"准备恢复", "enter")
                await wait_ui(pilot, lambda: isinstance(app.screen, RecoveryForm))
                fill_recovery(app.screen, self.fixture.project, "稍后核对停止前的隔离成果", "仅续做明确未完成项")
                await pilot.press("ctrl+enter")
                await wait_ui(pilot, lambda: len(app.screen_stack) == 1)
                self.assertEqual(app.board.tasks[1].run, "none")
                self.assertEqual(len(app.board.runs), 1)
                self.assertEqual(app.board.runs[stopped.id], stopped)
                self.assertEqual(store.load().runs[stopped.id], stopped)
                self.assertEqual(sorted(path.name for path in self.fixture.runtime.root.glob("*/commands/*.json")), commands_before)
                self.assertFalse((self.fixture.project / "fixture-result.json").exists())


class LiveFailureUI(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="waggle-live-failure-")
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
        publish(endpoint / "report-capabilities.json", capability_record(self.agent))
        self.store = self.enterContext(Store(self.path))
        board = self.store.load()
        board.add_project("真实项目", str(self.project))
        task = board.create("持久化意图", "真实项目")
        board.set_work_contract(task, fixture_contract(self.project))
        board.assign(task, self.agent.id)
        board.create("不抢焦点", "真实项目")
        self.store.save(board)

    def app(self, queued=False):
        if queued:
            board = self.store.load()
            board.queue_real(board.tasks[1], str(uuid4()), self.agent.session_id, self.agent.cwd)
            self.store.save(board)
        return Waggle(store=self.store, runtime=self.runtime)

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
            again = Waggle(store=reopened, runtime=self.runtime)
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
            app.action_toggle_detail()
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

    async def test_disconnected_execution_updates_cards_header_and_filters_without_mutation(self):
        app = self.app(queued=True)
        run = app.board.runs[app.board.tasks[1].run_id]
        app.apply_change(lambda board: board.ingest(self.result(run)))
        original = self.store.load()
        metadata = self.endpoint / "agent.json"

        def connection(**changes):
            metadata.unlink(missing_ok=True)
            publish(metadata, {"version": 1, **asdict(self.agent), "idle": False,
                               "active_run": run.id, "heartbeat": int(time.time() * 1000), **changes})

        connection()
        app._polling = True
        async with app.run_test(size=(180, 32)) as pilot:
            await app.poll_runtime()
            app._polling = True
            self.assertIn("执行中", str(app.query_one("#task-list", OptionList).get_option("task-1").prompt))
            # Neither the run revision nor the process's online flag changes after a crash.
            for changes in ({"heartbeat": 1}, {"active_run": str(uuid4())}, {"session_id": str(uuid4())}, None):
                with self.subTest(changes=changes):
                    connection()
                    await app.poll_runtime()
                    app._polling = True
                    if changes is None:
                        metadata.unlink()
                    else:
                        connection(**changes)
                    await app.poll_runtime()
                    app._polling = True
                    for selector in ("#task-list", "#lane-running"):
                        card = str(app.query_one(selector, OptionList).get_option("task-1").prompt)
                        self.assertIn("执行待核实", card)
                        self.assertNotIn("执行中", card)
                    self.assertIn("执行待核实", str(app.query_one("#detail-state", Static).content))
                    nav = app.query_one("#nav-status", OptionList)
                    self.assertEqual(str(nav.get_option("running").prompt).split()[-1], "0")
                    self.assertEqual(str(nav.get_option("blocked").prompt).split()[-1], "1")
                    app.set_filter("running")
                    self.assertNotIn(1, [task.id for task in app.visible_tasks()])
                    app.set_filter("blocked")
                    self.assertEqual([task.id for task in app.visible_tasks()], [1])
                    app.set_filter("all")
                    app.selected_id = 1
                    app.detail_expanded = True
                    app.refresh_tasks()
                    tail = str(app.query_one("#detail-tail", Static).content)
                    arrangement, separator, timeline = tail.partition("轮次与关键时间\n")
                    self.assertTrue(separator)
                    self.assertIn("连接/观测异常 · 核实原 Pi", arrangement)
                    self.assertNotIn("执行中", arrangement)
                    self.assertIn("按保存的轮次与修订顺序", timeline)
                    self.assertIn("不推断执行状态", timeline)
                    self.assertIn("第 1 轮 · 本轮 · 执行中", timeline)
                    self.assertEqual(app.board.tasks, original.tasks)
                    self.assertEqual(app.board.runs, original.runs)
            await pilot.resize_terminal(80, 24)
            await pilot.press("f")
            await wait_ui(pilot, lambda: isinstance(app.screen, Choice))
            assert isinstance(app.screen, Choice)
            self.assertIn("阻塞", app.screen.options["blocked"])
            await pilot.press(*"阻塞", "enter")
            self.assertEqual(app.status_filter, "blocked")
            self.assertIn("执行待核实", str(app.query_one("#task-list", OptionList).get_option("task-1").prompt))
            connection()
            await app.poll_runtime()
            app._polling = True
            app.set_filter("running")
            self.assertEqual([task.id for task in app.visible_tasks()], [1])
            self.assertIn("执行中", app.card(app.board.tasks[1]).plain)
            self.assertEqual(self.store.load().tasks, original.tasks)
            self.assertEqual(self.store.load().runs, original.runs)
            self.assertFalse(list((self.endpoint / "commands").iterdir()))

    async def test_stale_run_requires_explicit_human_resolution_and_note(self):
        app = self.app(queued=True)
        run = app.board.runs[app.board.tasks[1].run_id]
        metadata = self.endpoint / "agent.json"
        metadata.unlink()
        publish(metadata, {"version": 1, **asdict(self.agent), "heartbeat": 1})
        publish(self.endpoint / "session.json", session_record(run, observed_at=1, state="working", run_id=run.id))
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
        # A saved abandonment is not new ownership or dispatch authority.
        app = self.app()
        abandoned = deepcopy(app.board.runs[run.id])
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("ctrl+k", *"准备恢复", "enter")
            await wait_ui(pilot, lambda: isinstance(app.screen, RecoveryForm))
            fill_recovery(app.screen, self.project, "先核对已有成果", "只续做明确未完成项")
            await pilot.press("ctrl+enter")
            await wait_ui(pilot, lambda: len(app.screen_stack) == 1)
            self.assertEqual(app.board.tasks[1].run, "none")
            self.assertEqual(len(app.board.runs), 1)
            self.assertFalse(list((self.endpoint / "commands").iterdir()))
            expected_request = app.board.tasks[1].remaining
            replacement = replace(self.agent, id=str(uuid4()), session_id=str(uuid4()), name="接续助手", heartbeat=int(time.time() * 1000))
            endpoint = self.runtime.root / replacement.id
            for directory in (endpoint, endpoint / "commands", endpoint / "runs"):
                private_dir(directory, create=True)
            publish(endpoint / "agent.json", {"version": 1, **asdict(replacement)})
            publish(endpoint / "report-capabilities.json", capability_record(replacement))
            before = encode(self.store.load())
            with patch.object(self.runtime, "check_dispatch", wraps=self.runtime.check_dispatch) as preflight:
                await app.confirm_dispatch(1)
                preflight.assert_not_called()
            self.assertEqual(len(app.screen_stack), 1)
            self.assertEqual(encode(self.store.load()), before)
            self.assertEqual(app.board.tasks[1].agent, abandoned.agent_id)
            self.assertEqual(len(app.board.runs), 1)
            self.assertFalse(list((endpoint / "commands").iterdir()))

            # Separate fresh exact observation of the ORIGINAL owner/session, not an abandonment bypass.
            source_agent = replace(self.agent, heartbeat=int(time.time() * 1000), idle=True, active_run="")
            metadata.write_text(json.dumps({"version": 1, **asdict(source_agent)}), encoding="utf-8")
            metadata.chmod(0o600)
            session_path = self.endpoint / "session.json"
            session_path.write_text(json.dumps(session_record(abandoned, seq=2)), encoding="utf-8")
            session_path.chmod(0o600)
            self.runtime.check_new_round(abandoned)
            await pilot.press("a")
            await wait_ui(pilot, lambda: isinstance(app.screen, Choice))
            await pilot.press(*replacement.name, "enter")
            await wait_ui(pilot, lambda: len(app.screen_stack) == 1)
            self.assertEqual(app.board.tasks[1].agent, replacement.id)
            self.assertEqual(app.board.tasks[1].run, "none")
            self.assertFalse(list((endpoint / "commands").iterdir()))
            await pilot.press("s")
            await wait_ui(pilot, lambda: isinstance(app.screen, Confirm))
            await pilot.press("enter")  # Default is back, never dispatch.
            self.assertEqual(len(app.board.runs), 1)
            self.assertFalse(list((endpoint / "commands").iterdir()))
            await pilot.press("s")
            await wait_ui(pilot, lambda: isinstance(app.screen, Confirm))
            await pilot.press("ctrl+enter")
            await wait_ui(pilot, lambda: not app._sending and len(app.board.runs) == 2)
            new_run = app.board.runs[app.board.tasks[1].run_id]
            self.assertNotEqual(new_run.id, run.id)
            self.assertEqual((new_run.task_id, new_run.agent_id, new_run.session_id), (1, replacement.id, replacement.session_id))
            self.assertEqual(new_run.requirements, expected_request)
            self.assertEqual(app.board.runs[run.id], abandoned)
            self.assertEqual(self.store.load().runs[run.id], abandoned)
            self.assertEqual(len(list((endpoint / "commands").iterdir())), 1)
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
