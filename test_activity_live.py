"""Real Pi events + fake optional backend; no real child/model or user state."""
import asyncio
import json
import shutil
import time
import unittest
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from model import Board
from test_bridge import PiFixture


@unittest.skipUnless(shutil.which("pi"), "installed Pi required for offline event integration")
class ActivityLive(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = TemporaryDirectory(prefix="waggle-activity-live-")
        self.addCleanup(temp.cleanup)
        self.fixture = PiFixture(Path(temp.name))
        self.addAsyncCleanup(self.fixture.close)
        await self.fixture.start(extra_extensions=(Path(__file__).with_name("fixture-activity.ts"),))
        agent = await self.fixture.connect()
        assert agent
        self.board = Board(live=True)
        self.board.add_project("fixture", agent.cwd)
        self.agent = agent

    def prepare(self, title):
        task = self.board.create(title, "fixture")
        self.board.assign(task, self.agent.id)
        return self.board.queue_real(task, str(uuid4()), self.agent.session_id, self.agent.cwd)

    async def observe(self, run, predicate, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                result = self.fixture.runtime.read_activity(run)
            except ValueError:
                result = None  # The previous round's single sidecar is not this round's evidence.
            if result is not None and predicate(result):
                return result
            await asyncio.sleep(0.05)
        self.fail(f"No expected activity: {self.fixture.runtime.read_activity(run)}\n{self.fixture.stderr}")

    async def stop(self, run):
        self.fixture.runtime.stop(run)
        result = await self.fixture.result(run)
        self.assertEqual(result.state, "stopped")
        self.board.ingest(result)
        return result

    async def test_owned_backend_ids_only_status_rpc_and_original_tool_time(self):
        run = self.prepare("ACTIVITY_CHILD")
        before = asdict(run)
        self.fixture.runtime.dispatch(run)
        first = await self.observe(run, lambda value: value.children and value.children[0].state == "running" and value.tools)
        self.assertEqual([child.id for child in first.children], ["fixture-owned-backend-run"])
        self.assertEqual(first.tools[0].name, "waggle_fixture_effect")
        next_observation = await self.observe(run, lambda value: value.observed_at >= first.observed_at + 1500)
        self.assertEqual(first.tools[0].started_at, next_observation.tools[0].started_at)
        self.assertGreater(next_observation.seq, first.seq)
        completed = await self.observe(run, lambda value: value.children[0].state == "complete")
        assert completed.children_observed_at is not None and first.children_observed_at is not None
        self.assertGreater(completed.children_observed_at, first.children_observed_at)
        working = await self.fixture.result(run, lambda value: value.state == "working")
        self.assertEqual(working.delivery, "")  # Backend completion cannot settle or accept the task.
        self.assertEqual(asdict(run), before)
        raw = (self.fixture.runtime.endpoint(run.agent_id) / "activity.json").read_text()
        for private in ("PRIVATE", "another-tasks-run", "slow", "asyncDir", "task", "label"):
            self.assertNotIn(private, raw)
        methods = (self.fixture.project / "fixture-rpc-methods.txt").read_text().splitlines()
        self.assertTrue(methods)
        self.assertEqual(set(methods), {"status"})
        self.assertLessEqual(len(methods), 2)
        await self.stop(run)

    async def test_missing_bad_version_and_wrong_reply_remain_unknown(self):
        for mode in ("NO_PROVIDER", "BAD_VERSION", "WRONG_REPLY"):
            with self.subTest(mode=mode):
                run = self.prepare("ACTIVITY_" + mode)
                self.fixture.runtime.dispatch(run)
                first = await self.observe(run, lambda value: value.children and value.tools)
                later = await self.observe(run, lambda value: value.observed_at >= first.observed_at + 3200)
                self.assertEqual(later.children[0].state, "unknown")
                self.assertIsNone(later.children_observed_at)
                working = await self.fixture.result(run, lambda value: value.state == "working")
                self.assertFalse(working.terminal)
                await self.stop(run)

    async def test_management_result_does_not_adopt_another_tasks_backend_id(self):
        run = self.prepare("ACTIVITY_MANAGEMENT")
        self.fixture.runtime.dispatch(run)
        snapshot = await self.observe(run, lambda value: value.tools and value.tools[0].name == "waggle_fixture_effect")
        self.assertEqual(snapshot.children, ())
        self.assertFalse((self.fixture.project / "fixture-rpc-methods.txt").exists())
        await self.stop(run)

    async def test_optional_write_failure_does_not_pause_bridge_or_invalidate_full_delivery(self):
        run = self.prepare("FULL")
        file = self.fixture.runtime.endpoint(run.agent_id) / "activity.json"
        file.mkdir(mode=0o700)  # Deliberately unwritable as a snapshot, not a global disk failure.
        self.fixture.runtime.dispatch(run)
        result = await self.fixture.result(run)
        self.assertEqual((result.state, result.delivery), ("finished", "full"))
        self.board.ingest(result)
        self.assertTrue(self.board.tasks[run.task_id].can_accept)
        self.assertEqual(json.loads((self.fixture.project / "fixture-result.json").read_text())["counter"], 1)
        self.assertTrue(self.fixture.runtime.agent(run.agent_id).online)
        self.assertTrue(file.is_dir())

    async def test_input_invalidates_snapshot_without_claiming_old_tool_stopped(self):
        run = self.prepare("SLOW")
        self.fixture.runtime.dispatch(run)
        first = await self.observe(run, lambda value: bool(value.tools))
        await self.fixture.native_input("Unrelated fixture input; never adopt this as the task", "独立输入（解除任务关联）", streaming="steer")
        unbound = await self.observe(run, lambda value: not value.bound)
        self.assertEqual(unbound.run_id, first.run_id)
        self.assertIn("归属未确认", unbound.describe(int(time.time() * 1000)))
        self.assertNotIn("工具 waggle_fixture_effect", unbound.describe(int(time.time() * 1000)))
