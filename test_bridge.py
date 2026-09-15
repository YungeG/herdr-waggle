"""Actual installed Pi RPC with a deterministic offline provider; all state disposable."""
import asyncio
from dataclasses import asdict
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from uuid import uuid4

from journal import parse_event
from launch import Launch, pi_command
from model import Board, Run
from runtime import Runtime, publish
from store import Store

HERE = Path(__file__).resolve().parent


class PiFixture:
    def __init__(self, root: Path):
        self.root = root
        self.project = root / "真实子目录 中文" / "nested"
        self.project.mkdir(parents=True)
        self.runtime = Runtime(root / "bridges")
        self.events: list[dict] = []
        self.changed = asyncio.Event()
        self.stderr = ""
        self.process = None
        self.pumps = []

    async def start(self, *, launcher="direct", launch: Launch | None = None, extra_extensions: tuple[Path, ...] = ()):
        env = {**os.environ, "PI_CODING_AGENT_DIR": str(self.root / "pi-config"),
               "XDG_STATE_HOME": str(self.root / "state"), "XDG_CONFIG_HOME": str(self.root / "config"),
               "TASKROOM_BRIDGE_DIR": str(self.runtime.root), "TASKROOM_TEST_PROJECT": str(self.project)}
        arguments = ["--mode", "rpc", "-e", str(HERE / "fixture-provider.ts"), "--no-skills", "--no-prompt-templates",
                     "--no-context-files", "--offline", "--session-dir", str(self.root / "sessions"),
                     "--provider", "taskroom-test", "--model", "fixture"]
        for extension in extra_extensions:
            arguments.extend(["-e", str(extension)])
        if launch is not None:
            position = arguments.index("--session-dir")
            del arguments[position:position + 2]
            command, launch_cwd = [*pi_command(self.runtime, launch), "--no-approve", "--no-extensions", *arguments], self.project
        elif launcher == "installed":
            installed = await asyncio.to_thread(subprocess.run, ["just", "install-bridge", str(self.project)],
                    cwd=HERE, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, check=True, timeout=15)
            assert "已准备" in installed.stdout, installed
            # Pi's flag grants project-resource trust for this disposable directory only.
            # It does not persist trust or auto-answer tool/UI permission requests.
            command, launch_cwd = ["pi", "--approve", *arguments], self.project
        elif launcher == "recipe":
            command, launch_cwd = ["just", "pi", str(self.project), "--no-approve", "--no-extensions", *arguments], HERE
        else:
            command, launch_cwd = ["pi", "--no-approve", "--no-extensions", "-e", str(HERE / "pi-bridge.ts"), *arguments], self.project
        self.process = await asyncio.create_subprocess_exec(*command, cwd=launch_cwd, env=env,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
        self.pumps = [asyncio.create_task(self._stdout()), asyncio.create_task(self._stderr())]
        response = await self.request("get_commands")
        assert response["success"], response
        assert sum(item["name"] == "taskroom" for item in response["data"]["commands"]) == 1, (response, self.stderr)
        if launch is None:
            assert not self.runtime.root.exists(), "Loading the extension must not create a mailbox or consent."
        return self

    async def _stdout(self):
        assert self.process and self.process.stdout
        async for line in self.process.stdout:
            try:
                self.events.append(json.loads(line))
            except ValueError:
                self.events.append({"invalid_stdout": line.decode(errors="replace")})
            self.changed.set()
        self.changed.set()

    async def _stderr(self):
        assert self.process and self.process.stderr
        async for line in self.process.stderr:
            self.stderr = (self.stderr + line.decode(errors="replace"))[-16000:]

    async def send(self, message: dict):
        assert self.process and self.process.stdin
        self.process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode())
        await self.process.stdin.drain()

    async def event(self, predicate, *, after=0, timeout=15):
        async def wait():
            while True:
                self.changed.clear()
                for event in self.events[after:]:
                    if predicate(event):
                        return event
                if self.process and self.process.returncode is not None:
                    raise AssertionError(f"Pi exited {self.process.returncode}: {self.stderr}\n{self.events[-8:]}")
                await self.changed.wait()
        try:
            return await asyncio.wait_for(wait(), timeout)
        except TimeoutError as error:
            raise AssertionError(f"Pi event timeout: {self.stderr}\n{self.events[-12:]}") from error

    async def request(self, command: str, **fields):
        request_id = str(uuid4())
        await self.send({"id": request_id, "type": command, **fields})
        return await self.event(lambda item: item.get("type") == "response" and item.get("id") == request_id)

    async def connect(self, approved=True):
        start = len(self.events)
        await self.send({"id": str(uuid4()), "type": "prompt", "message": "/taskroom connect"})
        confirmation = await self.event(lambda item: item.get("type") == "extension_ui_request" and item.get("method") == "confirm", after=start)
        assert confirmation["title"] == "连接 Taskroom", confirmation
        await self.send({"type": "extension_ui_response", "id": confirmation["id"], "confirmed": approved})
        if not approved:
            await self.request("get_state")
            return None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            agents = self.runtime.discover()
            if agents:
                return agents[0]
            await asyncio.sleep(0.05)
        raise AssertionError(f"No joined Pi: {self.stderr}\n{self.events[-12:]}")

    async def result(self, run: Run, predicate=lambda run: run.terminal, timeout=12):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            incoming = self.runtime.read_run(run)
            if incoming is not None and predicate(incoming):
                return incoming
            await asyncio.sleep(0.05)
        raise AssertionError(f"Run did not reach expected state: {self.runtime.read_run(run)}\n{self.stderr}\n{self.events[-12:]}")

    async def close(self):
        if self.process:
            if self.process.returncode is None:
                os.killpg(self.process.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(self.process.wait(), 5)
                except TimeoutError:
                    os.killpg(self.process.pid, signal.SIGKILL)
                    await self.process.wait()
            if self.process.stdin:
                self.process.stdin.close()
                await self.process.stdin.wait_closed()
            await asyncio.gather(*self.pumps)


@unittest.skipUnless(shutil.which("pi"), "installed Pi required for the real-runtime offline integration test")
class BridgeSmoke(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="taskroom-pi-test-")
        self.addCleanup(self.temp.cleanup)
        self.fixture = PiFixture(Path(self.temp.name))
        self.addAsyncCleanup(self.fixture.close)
        await self.fixture.start()

    async def test_real_pi_full_report_settled_persistence_and_no_duplicate_effect(self):
        agent = await self.fixture.connect()
        assert agent is not None
        board = Board(live=True)
        board.add_project("精确子目录", str(self.fixture.project))
        task = board.create("FULL", "精确子目录", "Only the explicitly isolated fixture effect, then report.")
        board.assign(task, agent.id)
        run = board.queue_real(task, str(uuid4()), agent.session_id, agent.cwd)
        with Store(Path(self.temp.name) / "tasks.sqlite3") as store:
            store.load()
            store.save(board)  # Intent is durable BEFORE any mailbox publication.
            self.fixture.runtime.dispatch(run)
            result = await self.fixture.result(run)
            self.assertEqual((result.state, result.delivery), ("finished", "full"))
            self.assertTrue(any("已确认对应任务消息" in event for event in result.events))
            self.assertTrue(any("agent_settled" in event for event in result.events))
            self.assertEqual(json.loads((self.fixture.project / "fixture-result.json").read_text())["counter"], 1)
            board.ingest(result)
            self.assertTrue(task.can_accept)
            self.assertEqual(task.status, "review")
            store.save(board)
        with Store(Path(self.temp.name) / "tasks.sqlite3") as store:
            restored = store.load()
            self.assertEqual(asdict(restored.runs[run.id]), asdict(result))
            self.assertEqual(restored.tasks[task.id].status, "review")
            restored.accept(restored.tasks[task.id])
            store.save(restored)
        # Replay identical dispatch intent despite lost acknowledgement: durable run identity wins.
        command_id = str(uuid4())
        command_file = self.fixture.runtime.endpoint(agent.id) / "commands" / f"{command_id}.json"
        publish(command_file, {"version": 1, "id": command_id, "kind": "dispatch", "agent_id": agent.id,
                "session_id": agent.session_id, "expires": int(time.time() * 1000) + 30000, "run": asdict(run)})
        deadline = time.monotonic() + 3
        while command_file.exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        self.assertFalse(command_file.exists())
        self.assertEqual(json.loads((self.fixture.project / "fixture-result.json").read_text())["counter"], 1)
        replayed = self.fixture.runtime.read_run(run)
        assert replayed is not None
        self.assertEqual(asdict(replayed), asdict(result))

    async def test_key_journal_keeps_report_snapshots_and_original_session_not_tool_noise(self):
        board, task, run = await self.prepared_run("HISTORY")
        self.fixture.runtime.dispatch(run)
        result = await self.fixture.result(run)
        self.assertEqual((result.state, result.delivery), ("finished", "full"))
        events = [parse_event(value) for value in result.events]
        self.assertTrue(events and all(event is not None for event in events), "New bridge history must persist typed source-stamped records.")
        typed = [event for event in events if event is not None]
        origins = [event for event in typed if event.kind == "origin"]
        self.assertEqual(len(origins), 1)
        self.assertTrue(origins[0].text.startswith(self.temp.name))
        self.assertTrue(Path(origins[0].text).is_file())
        self.assertEqual({event.run_id for event in typed}, {run.id})
        self.assertTrue(all(0 < event.seq <= result.seq for event in typed))
        reports = [event for event in typed if event.report is not None]
        self.assertEqual([event.kind for event in reports], ["progress", "full"])
        first, final = reports[0].report, reports[-1].report
        assert first and final and final.brief
        self.assertEqual(first.remaining, "仍需最终核对")
        self.assertNotEqual(first.summary, final.summary)
        self.assertEqual((final.summary, final.checks, final.remaining), (result.summary, result.checks, result.remaining))
        self.assertEqual(final.brief.actions, ["写入隔离测试文件"])
        self.assertFalse(any(event.text.startswith(("工具开始", "工具返回成功", "Pi ·")) for event in typed))
        board.ingest(result)
        with Store(Path(self.temp.name) / "history.sqlite3") as store:
            store.load()
            store.save(board)
            restored = store.load()
            self.assertEqual(restored.runs[run.id].events, result.events)
        self.assertEqual(json.loads((self.fixture.project / "fixture-result.json").read_text())["counter"], 1)
        self.assertTrue(task.can_accept)

    async def test_report_without_optional_brief_remains_compatible(self):
        board, task, run = await self.prepared_run("FULL_LEGACY")
        self.fixture.runtime.dispatch(run)
        result = await self.fixture.result(run)
        self.assertEqual((result.state, result.delivery), ("finished", "full"))
        reports = [event.report for value in result.events if (event := parse_event(value)) is not None and event.report is not None]
        self.assertEqual(len(reports), 1)
        self.assertIsNone(reports[0].brief)
        self.assertEqual(reports[0].summary, result.summary)
        board.ingest(result)
        self.assertTrue(task.can_accept)

    async def prepared_run(self, title="FULL"):
        agent = await self.fixture.connect()
        assert agent is not None
        board = Board(live=True)
        board.add_project("临时项目", str(self.fixture.project))
        task = board.create(title, "临时项目")
        board.assign(task, agent.id)
        run = board.queue_real(task, str(uuid4()), agent.session_id, agent.cwd)
        return board, task, run

    def raw_command(self, run: Run, kind="dispatch", *, expires=None):
        command_id = str(uuid4())
        file = self.fixture.runtime.endpoint(run.agent_id) / "commands" / f"{command_id}.json"
        publish(file, {"version": 1, "id": command_id, "kind": kind, "agent_id": run.agent_id,
                      "session_id": run.session_id, "expires": int(time.time() * 1000) + 30000 if expires is None else expires,
                      "run": asdict(run)})
        return file

    async def test_declined_connect_has_no_mailbox_or_execution(self):
        self.assertIsNone(await self.fixture.connect(approved=False))
        self.assertFalse(self.fixture.runtime.root.exists())
        self.assertFalse((self.fixture.project / "fixture-result.json").exists())

    async def test_expired_request_and_stop_overtaking_dispatch_never_execute(self):
        board, task, run = await self.prepared_run()
        self.raw_command(run, expires=int(time.time() * 1000) - 1)
        result = await self.fixture.result(run)
        self.assertEqual(result.state, "failed")
        self.assertIn("过期", result.summary)
        board.ingest(result)
        board.return_work(task, "明确重派")
        next_run = board.queue_real(task, str(uuid4()), run.session_id, run.cwd)
        self.raw_command(next_run, kind="stop")
        stopped = await self.fixture.result(next_run)
        self.assertEqual(stopped.state, "stopped")
        file = self.raw_command(next_run)
        deadline = time.monotonic() + 3
        while file.exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        self.assertFalse(file.exists())
        self.assertFalse((self.fixture.project / "fixture-result.json").exists())

    async def test_partial_report_is_never_human_acceptance(self):
        board, task, run = await self.prepared_run("PARTIAL")
        self.fixture.runtime.dispatch(run)
        result = await self.fixture.result(run)
        self.assertEqual((result.state, result.delivery), ("finished", "partial"))
        board.ingest(result)
        self.assertEqual(task.status, "review")
        self.assertFalse(task.can_accept)
        with self.assertRaises(ValueError):
            board.accept(task)

    async def test_no_final_report_is_failed_not_delivery(self):
        board, task, run = await self.prepared_run("NO_REPORT")
        self.fixture.runtime.dispatch(run)
        result = await self.fixture.result(run)
        self.assertEqual(result.state, "failed")
        self.assertIn("未提交", result.summary)
        board.ingest(result)
        self.assertEqual(task.status, "blocked")
        self.assertFalse(task.can_accept)

    async def test_late_tool_failure_invalidates_full_report(self):
        board, task, run = await self.prepared_run("LATE_FAILURE")
        self.fixture.runtime.dispatch(run)
        result = await self.fixture.result(run)
        self.assertEqual((result.state, result.delivery), ("finished", "partial"))
        self.assertIn("工具", result.remaining)
        self.assertTrue(any((event := parse_event(value)) is not None and event.kind == "invalidated" and "完整报告之后" in event.text for value in result.events))
        board.ingest(result)
        self.assertFalse(task.can_accept)

    async def test_stop_only_owned_active_run_waits_for_settled(self):
        board, task, run = await self.prepared_run("SLOW")
        self.fixture.runtime.dispatch(run)
        await self.fixture.event(lambda event: event.get("type") == "tool_execution_start" and event.get("toolName") == "taskroom_fixture_effect")
        working = await self.fixture.result(run, lambda result: result.state == "working")
        self.assertEqual(working.state, "working")
        board.ingest(working)
        self.fixture.runtime.stop(working)
        result = await self.fixture.result(run)
        self.assertEqual(result.state, "stopped")
        self.assertTrue(any("agent_settled" in event for event in result.events))
        self.assertFalse(any(event.get("type") == "extension_ui_request" and
                any(text in event.get("message", "") for text in ("桥接已暂停", "拒绝请求")) for event in self.fixture.events))
        board.ingest(result)
        self.assertEqual(task.status, "blocked")
        self.assertFalse((self.fixture.project / "fixture-result.json").exists())

    async def test_busy_pi_rejects_second_attempt_and_crash_stays_unknown(self):
        _, _, run = await self.prepared_run("SLOW")
        self.fixture.runtime.dispatch(run)
        await self.fixture.event(lambda event: event.get("type") == "tool_execution_start" and event.get("toolName") == "taskroom_fixture_effect")
        working = await self.fixture.result(run, lambda result: result.state == "working")
        another = Run(str(uuid4()), 2, run.agent_id, run.session_id, run.cwd, "FULL", "", "")
        self.raw_command(another)
        rejected = await self.fixture.result(another)
        self.assertEqual(rejected.state, "failed")
        self.assertIn("正忙", rejected.summary)
        assert self.fixture.process is not None
        os.killpg(self.fixture.process.pid, signal.SIGKILL)
        await self.fixture.process.wait()
        orphan = self.fixture.runtime.read_run(run)
        assert orphan is not None
        self.assertFalse(orphan.terminal)
        self.assertGreaterEqual(orphan.seq, working.seq)
        self.assertFalse((self.fixture.project / "fixture-result.json").exists())

    @unittest.skipIf(os.getuid() == 0, "root bypasses the injected directory write denial")
    async def test_heartbeat_write_failure_after_reservation_pauses_without_injection_or_retry(self):
        _, _, run = await self.prepared_run()
        endpoint = self.fixture.runtime.endpoint(run.agent_id)
        start = len(self.fixture.events)
        endpoint.chmod(0o500)  # Run/command subdirectories stay writable; heartbeat temp creation fails.
        try:
            command = self.raw_command(run)
            await self.fixture.event(lambda event: event.get("type") == "extension_ui_request"
                    and "桥接已暂停" in event.get("message", ""), after=start, timeout=3)
        finally:
            endpoint.chmod(0o700)
        reserved = self.fixture.runtime.read_run(run)
        assert reserved is not None
        self.assertEqual((reserved.state, reserved.seq), ("queued", 1))
        self.assertTrue(command.exists(), "Ambiguous commands must remain for diagnosis, not be discarded.")
        # Restoring storage is not consent to retry injection or consume another command.
        another = Run(str(uuid4()), 2, run.agent_id, run.session_id, run.cwd, "FULL", "", "")
        next_command = self.raw_command(another)
        await asyncio.sleep(0.6)
        self.assertTrue(next_command.exists())
        self.assertIsNone(self.fixture.runtime.read_run(another))
        self.assertFalse((self.fixture.project / "fixture-result.json").exists())
        self.assertFalse(any(event.get("type") == "agent_start" for event in self.fixture.events[start:]))
        unchanged = self.fixture.runtime.read_run(run)
        assert unchanged is not None
        self.assertEqual(asdict(unchanged), asdict(reserved))

    async def test_native_pi_confirmation_waits_without_workbench_answer(self):
        board, task, run = await self.prepared_run("CONFIRM")
        self.fixture.runtime.dispatch(run)
        confirmation = await self.fixture.event(lambda event: event.get("type") == "extension_ui_request"
                and event.get("method") == "confirm" and event.get("title") == "Offline fixture permission")
        waiting = await self.fixture.result(run, lambda result: result.state == "waiting")
        board.ingest(waiting)
        self.assertEqual(task.status, "blocked")
        self.assertIn("不会自动回答", task.blocker)
        self.assertFalse(task.can_accept)
        await asyncio.sleep(0.3)
        still_waiting = self.fixture.runtime.read_run(run)
        assert still_waiting is not None
        self.assertEqual(still_waiting.state, "waiting")
        self.assertFalse((self.fixture.project / "fixture-result.json").exists())
        await self.fixture.send({"type": "extension_ui_response", "id": confirmation["id"], "confirmed": True})
        finished = await self.fixture.result(run)
        self.assertEqual((finished.state, finished.delivery), ("finished", "full"))
        self.assertTrue(any("人工确认已返回" in event for event in finished.events))
        self.assertEqual(json.loads((self.fixture.project / "fixture-result.json").read_text())["counter"], 1)

    async def test_extra_input_loses_ownership_and_bridge_refuses_to_stop(self):
        board, task, run = await self.prepared_run("SLOW")
        self.fixture.runtime.dispatch(run)
        await self.fixture.event(lambda event: event.get("type") == "tool_execution_start" and event.get("toolName") == "taskroom_fixture_effect")
        response = await self.fixture.request("prompt", message="Independent human input: do not run another fixture effect.", streamingBehavior="steer")
        self.assertTrue(response["success"], response)
        unowned = await self.fixture.result(run, lambda result: any("收到额外输入" in event for event in result.events))
        self.fixture.runtime.stop(unowned)
        refused = await self.fixture.result(run, lambda result: any("停止尚未执行" in event for event in result.events))
        self.assertFalse(refused.terminal)
        self.assertFalse(any("收到人工停止请求" in event for event in refused.events))
        self.assertFalse((self.fixture.project / "fixture-result.json").exists())
        # The test owner, not the workbench, now aborts its isolated Pi explicitly.
        self.assertTrue((await self.fixture.request("abort"))["success"])
        result = await self.fixture.result(run)
        self.assertEqual((result.state, result.delivery), ("interrupted", ""))
        board.ingest(result)
        self.assertFalse(task.can_accept)

    async def test_disconnect_reconnect_uses_new_endpoint_and_never_consumes_old_mail(self):
        _, _, run = await self.prepared_run()
        response = await self.fixture.request("prompt", message="/taskroom disconnect")
        self.assertTrue(response["success"])
        await self.fixture.event(lambda event: event.get("type") == "extension_ui_request" and "已断开" in event.get("message", ""))
        old = self.fixture.runtime.agent(run.agent_id)
        self.assertFalse(old.online)
        file = self.raw_command(run)
        joined = await self.fixture.connect()
        assert joined is not None
        self.assertNotEqual(joined.id, run.agent_id)
        self.assertEqual(joined.session_id, run.session_id)
        self.assertTrue(file.exists())
        self.assertIsNone(self.fixture.runtime.read_run(run))
        self.assertFalse((self.fixture.project / "fixture-result.json").exists())


@unittest.skipUnless(shutil.which("pi") and shutil.which("just"), "installed Pi and just required")
class BridgeEntrypoints(unittest.IsolatedAsyncioTestCase):
    async def test_project_loader_and_pi_recipe_use_exact_directory_and_require_consent(self):
        for launcher in ("installed", "recipe"):
            with self.subTest(launcher=launcher), tempfile.TemporaryDirectory(prefix="taskroom-entry-test-") as directory:
                fixture = PiFixture(Path(directory))
                try:
                    await fixture.start(launcher=launcher)
                    self.assertEqual((fixture.project / ".pi/extensions/taskroom.ts").exists(), launcher == "installed")
                    agent = await fixture.connect()
                    assert agent is not None
                    self.assertEqual(agent.cwd, str(fixture.project))
                    run = Run(str(uuid4()), 1, agent.id, agent.session_id, agent.cwd, "FULL", "", "")
                    fixture.runtime.dispatch(run)
                    result = await fixture.result(run)
                    self.assertEqual((result.state, result.delivery), ("finished", "full"))
                    self.assertEqual(json.loads((fixture.project / "fixture-result.json").read_text())["counter"], 1)
                finally:
                    await fixture.close()


if __name__ == "__main__":
    unittest.main()
