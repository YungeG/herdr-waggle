"""Direct native Pi input, real RPC/runtime, offline provider and private disposable state."""
import json
import shutil
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from model import Board
from test_bridge import PiFixture


@unittest.skipUnless(shutil.which("pi"), "installed Pi required")
class DirectSyncLive(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = TemporaryDirectory(prefix="waggle-direct-pi-")
        self.addCleanup(temp.cleanup)
        self.fixture = PiFixture(Path(temp.name))
        self.addAsyncCleanup(self.fixture.close)
        await self.fixture.start()
        agent = await self.fixture.connect()
        assert agent is not None
        self.board = Board(live=True)
        self.board.add_project("fixture", str(self.fixture.project))
        self.task = self.board.create("FULL", "fixture", "Native input must never replay the original effect.")
        self.board.assign(self.task, agent.id)
        self.attempt = self.board.queue_real(self.task, str(uuid4()), agent.session_id, agent.cwd)

    async def choose(self, message, option, *, streaming=None):
        reply = await self.fixture.native_input(message, option, streaming=streaming)
        self.assertTrue(reply["success"], reply)

    async def test_terminal_native_input_creates_one_child_without_replaying_parent(self):
        runtime = self.fixture.runtime
        runtime.dispatch(self.attempt)
        parent = await self.fixture.result(self.attempt)
        self.board.ingest(parent)
        parent_file = runtime.endpoint(parent.agent_id) / "runs" / f"{parent.id}.json"
        original = parent_file.read_bytes()
        await self.choose("Continue the explicitly associated task once.", "继续当前任务")
        children = runtime.read_continuations(parent)
        self.assertEqual(len(children), 1)
        child = await self.fixture.result(children[0])
        self.assertEqual((child.state, child.delivery, child.continuation_parent), ("finished", "full", parent.id))
        self.assertNotEqual(child.id, parent.id)
        self.board.continue_from(parent.id, child)
        self.assertEqual(self.task.run_id, child.id)
        self.assertEqual(self.task.status, "review")
        self.assertEqual(parent_file.read_bytes(), original)
        self.assertEqual(json.loads((self.fixture.project / "fixture-result.json").read_text())["counter"], 2)
        self.assertFalse(self.board.continue_from(parent.id, child))
        self.assertEqual(len(self.board.runs), 2)
        self.assertEqual(self.task.status, "review", "No automatic human acceptance")
        start = len(self.fixture.events)
        self.assertTrue((await self.fixture.request("prompt", message="Continue the same associated task again."))["success"])
        third = await self.fixture.result(runtime.read_continuations(child)[0])
        self.assertEqual(third.continuation_parent, child.id)
        self.assertEqual(json.loads((self.fixture.project / "fixture-result.json").read_text())["counter"], 3)
        self.assertFalse(any(e.get("method") == "select" for e in self.fixture.events[start:]), "Confirmed binding persists without asking for every input")

    async def test_provider_failure_can_continue_as_a_new_attempt(self):
        self.task.title = self.attempt.title = "DIRECT_ERROR"
        runtime = self.fixture.runtime
        runtime.dispatch(self.attempt)
        failed = await self.fixture.result(self.attempt)
        self.assertEqual(failed.state, "failed")
        original = (runtime.endpoint(failed.agent_id) / "runs" / f"{failed.id}.json").read_bytes()
        self.assertFalse((self.fixture.project / "fixture-result.json").exists())
        await self.choose("Retry this task, without replaying work that already happened.", "继续当前任务")
        child = await self.fixture.result(runtime.read_continuations(failed)[0])
        self.assertEqual((child.state, child.delivery), ("finished", "full"))
        self.assertEqual(json.loads((self.fixture.project / "fixture-result.json").read_text())["counter"], 1)
        self.assertEqual((runtime.endpoint(failed.agent_id) / "runs" / f"{failed.id}.json").read_bytes(), original)

    async def test_cancel_and_independent_input_do_not_modify_a_terminal_attempt(self):
        runtime = self.fixture.runtime
        runtime.dispatch(self.attempt)
        parent = await self.fixture.result(self.attempt)
        file = runtime.endpoint(parent.agent_id) / "runs" / f"{parent.id}.json"
        before = file.read_bytes()
        start = len(self.fixture.events)
        await self.choose("Do not execute this cancelled input.", "取消输入")
        self.assertFalse(any(e.get("type") == "agent_start" for e in self.fixture.events[start:]))
        self.assertEqual(runtime.read_continuations(parent), [])
        start = len(self.fixture.events)
        await self.choose("Independent input; do not run another fixture effect.", "独立输入（解除任务关联）")
        await self.fixture.event(lambda e: e.get("type") == "agent_end", after=start)
        await self.fixture.request("get_state")
        self.assertEqual(file.read_bytes(), before)
        self.assertEqual(runtime.read_continuations(parent), [])
        snapshot = runtime.read_session(parent)
        assert snapshot is not None
        self.assertEqual((snapshot.following, snapshot.linked_run_id, snapshot.run_id), (False, "", ""))
        self.assertEqual(json.loads((self.fixture.project / "fixture-result.json").read_text())["counter"], 1)

    async def test_same_task_steering_preserves_activity_and_uses_the_existing_run(self):
        self.task.title = self.attempt.title = "DIRECT_LATCH"
        runtime = self.fixture.runtime
        runtime.dispatch(self.attempt)
        await self.fixture.event(lambda e: e.get("type") == "tool_execution_start" and e.get("toolName") == "waggle_fixture_effect")
        await self.choose("Add the same-task verification before final delivery.", "继续当前任务", streaming="steer")
        pending = runtime.read_session(self.attempt)
        assert pending is not None
        self.assertTrue(pending.input_pending)
        activity = runtime.read_activity(self.attempt)
        assert activity is not None
        self.assertTrue(activity.bound, "A confirmed same-task input must not poison all future activity")
        (self.fixture.project / "fixture-supplement-release").touch()
        finished = await self.fixture.result(self.attempt)
        self.assertEqual((finished.state, finished.delivery), ("finished", "full"))
        self.assertEqual(runtime.read_continuations(finished), [])
        self.assertTrue(any("input_received" in raw for raw in finished.events))
        self.assertEqual(json.loads((self.fixture.project / "fixture-result.json").read_text())["counter"], 1)

    async def test_followup_rejects_the_old_report_before_native_input_is_received(self):
        self.task.title = self.attempt.title = "DIRECT_QUEUE"
        runtime = self.fixture.runtime
        runtime.dispatch(self.attempt)
        await self.fixture.event(lambda e: e.get("type") == "tool_execution_start" and e.get("toolName") == "waggle_fixture_effect")
        await self.choose("Follow up on this same task before delivery.", "继续当前任务", streaming="followUp")
        (self.fixture.project / "fixture-supplement-release").touch()
        finished = await self.fixture.result(self.attempt)
        reports = [e for e in self.fixture.events if e.get("type") == "tool_execution_end" and e.get("toolName") == "waggle_report"]
        self.assertGreaterEqual(len(reports), 2)
        self.assertTrue(reports[0]["isError"], "A report computed before the queued input must be refused")
        self.assertFalse(reports[-1]["isError"])
        self.assertEqual((finished.state, finished.delivery), ("finished", "full"))
        self.assertEqual(json.loads((self.fixture.project / "fixture-result.json").read_text())["counter"], 1)

    async def test_cleared_native_queue_stays_uncertain_until_explicit_detach(self):
        self.task.title = self.attempt.title = "DIRECT_QUEUE"
        runtime = self.fixture.runtime
        runtime.dispatch(self.attempt)
        await self.fixture.event(lambda e: e.get("type") == "tool_execution_start" and e.get("toolName") == "waggle_fixture_effect")
        await self.choose("Queue an input which the test owner will clear.", "继续当前任务", streaming="followUp")
        self.assertTrue((await self.fixture.request("clear_queue"))["success"])
        start = len(self.fixture.events)
        (self.fixture.project / "fixture-supplement-release").touch()
        await self.fixture.event(lambda e: e.get("type") == "agent_end", after=start)
        await self.fixture.request("get_state")
        current = runtime.read_run(self.attempt)
        snapshot = runtime.read_session(self.attempt)
        assert current is not None and snapshot is not None
        self.assertFalse(current.terminal)
        self.assertTrue(snapshot.input_pending)
        self.assertTrue((await self.fixture.request("prompt", message="/waggle detach"))["success"])
        ended = await self.fixture.result(self.attempt)
        self.assertEqual((ended.state, ended.delivery), ("interrupted", ""))

    async def test_cancel_while_original_agent_settles_preserves_its_valid_delivery(self):
        self.task.title = self.attempt.title = "DIRECT_LATCH"
        runtime = self.fixture.runtime
        runtime.dispatch(self.attempt)
        await self.fixture.event(lambda e: e.get("type") == "tool_execution_start" and e.get("toolName") == "waggle_fixture_effect")
        start, request_id = len(self.fixture.events), str(uuid4())
        await self.fixture.send({"type": "prompt", "id": request_id, "message": "Cancel this proposed input.", "streamingBehavior": "steer"})
        choice = await self.fixture.event(lambda e: e.get("method") == "select", after=start)
        (self.fixture.project / "fixture-supplement-release").touch()
        await self.fixture.event(lambda e: e.get("type") == "agent_end", after=start)
        pending = runtime.read_run(self.attempt)
        assert pending is not None
        self.assertEqual(pending.delivery, "full")
        self.assertFalse(pending.terminal, "Settlement must wait for attribution, not revoke an otherwise valid report")
        await self.fixture.send({"type": "extension_ui_response", "id": choice["id"], "cancelled": True})
        await self.fixture.event(lambda e: e.get("type") == "response" and e.get("id") == request_id, after=start)
        final = await self.fixture.result(self.attempt)
        self.assertEqual((final.state, final.delivery), ("finished", "full"))
        self.assertEqual(runtime.read_continuations(final), [])
        self.assertEqual(json.loads((self.fixture.project / "fixture-result.json").read_text())["counter"], 1)

    async def test_uncovered_received_supplement_is_carried_without_rewriting_its_receipt(self):
        self.task.title = self.attempt.title = "SUPPLEMENT_STALE"
        runtime = self.fixture.runtime
        runtime.dispatch(self.attempt)
        await self.fixture.event(lambda e: e.get("type") == "tool_execution_start" and e.get("toolName") == "waggle_fixture_effect")
        working = await self.fixture.result(self.attempt, lambda run: run.state == "working")
        self.board.ingest(working)
        note = self.board.prepare_supplement(self.task, str(uuid4()), "INHERIT-THE-UNVERIFIED-REQUIREMENT", int(time.time() * 1000) + 30000)
        runtime.send_supplement(working, note)
        await self.fixture.result(self.attempt, lambda run: any("supplement_queued" in raw for raw in run.events))
        (self.fixture.project / "fixture-supplement-release").touch()
        failed = await self.fixture.result(self.attempt)
        self.assertEqual(failed.state, "failed")
        receipt = runtime.read_supplement(failed, note)
        assert receipt is not None
        self.assertEqual(receipt.status, "received")
        self.board.ingest(failed)
        self.board.apply_supplement(receipt)
        receipt_file = runtime.endpoint(failed.agent_id) / "supplements/receipts" / f"{note.id}.json"
        original = receipt_file.read_bytes()
        start = len(self.fixture.events)
        await self.choose("Continue, including the unresolved requirement.", "继续当前任务")
        child = runtime.read_continuations(failed)[0]
        self.assertIn(note.text, child.requirements)
        await self.fixture.event(lambda e: e.get("type") == "tool_execution_start" and e.get("toolName") == "waggle_fixture_effect", after=start)
        (self.fixture.project / "fixture-supplement-release").touch()
        final = await self.fixture.result(child)
        self.assertEqual((final.state, final.delivery), ("finished", "full"))
        self.board.continue_from(failed.id, final)
        self.assertTrue(self.task.can_accept)
        self.assertEqual(receipt_file.read_bytes(), original)
        self.assertEqual(json.loads((self.fixture.project / "fixture-result.json").read_text())["counter"], 2)

    async def test_optional_session_telemetry_failure_cannot_invent_or_revoke_a_report(self):
        runtime = self.fixture.runtime
        path = runtime.endpoint(self.attempt.agent_id) / "session.json"
        path.unlink()
        path.mkdir(mode=0o700)
        runtime.dispatch(self.attempt)
        result = await self.fixture.result(self.attempt)
        self.assertEqual((result.state, result.delivery), ("finished", "full"))
        self.assertTrue(runtime.agent(result.agent_id).online)
        with self.assertRaises((ValueError, OSError)):
            runtime.read_session(result)
        with self.assertRaises((ValueError, OSError)):
            runtime.check_current(result)

    async def test_uncertain_link_publication_never_executes_or_retries_native_input(self):
        runtime = self.fixture.runtime
        runtime.dispatch(self.attempt)
        parent = await self.fixture.result(self.attempt)
        endpoint = runtime.endpoint(parent.agent_id)
        link = endpoint / "runs" / f"{parent.id}.next.json"
        link.mkdir(mode=0o700)  # Child reservation succeeds, exclusive link publication fails.
        start = len(self.fixture.events)
        await self.choose("This input must not run after uncertain publication.", "继续当前任务")
        await self.fixture.event(lambda e: e.get("type") == "extension_ui_request" and "桥接已暂停" in e.get("message", ""), after=start)
        self.assertFalse(any(e.get("type") == "agent_start" for e in self.fixture.events[start:]))
        self.assertEqual(json.loads((self.fixture.project / "fixture-result.json").read_text())["counter"], 1)
        self.assertFalse(runtime.agent(parent.agent_id).online)
        link.rmdir()
        await self.fixture.request("get_state")
        self.assertFalse(link.exists())
        self.assertEqual(runtime.read_continuations(parent), [])


if __name__ == "__main__":
    unittest.main()
