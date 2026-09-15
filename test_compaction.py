"""Real Pi with offline effects and a controlled pi-context lifecycle, no live sessions."""
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from uuid import uuid4

from journal import parse_event
from model import Board
from runtime import BridgeError
from store import Store
from test_bridge import HERE, PiFixture

PI_CONTEXT = Path(os.environ.get("TASKROOM_TEST_PI_CONTEXT", str(Path.home() / ".pi/agent/npm/node_modules/pi-context/src/index.ts")))


@unittest.skipUnless(shutil.which("pi"), "installed Pi required")
class CompactionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="taskroom-compact-test-")
        self.addCleanup(self.temp.cleanup)
        self.fixture = PiFixture(Path(self.temp.name))
        self.addAsyncCleanup(self.fixture.close)
        await self.fixture.start(extra_extensions=(HERE / "fixture-compaction.ts",))

    async def prepared(self, title="COMPACT", mode="normal"):
        configured = await self.fixture.request("prompt", message=f"/fixture-compaction {mode}")
        self.assertTrue(configured["success"])
        agent = await self.fixture.connect()
        assert agent is not None
        board = Board(live=True)
        board.add_project("临时项目", str(self.fixture.project))
        task = board.create(title, "临时项目")
        board.assign(task, agent.id)
        run = board.queue_real(task, str(uuid4()), agent.session_id, agent.cwd)
        return board, task, run

    async def test_same_run_survives_repeated_compaction_and_delivers_once(self):
        board, task, run = await self.prepared("COMPACT_TWICE")
        self.fixture.runtime.dispatch(run)
        await self.fixture.event(lambda event: event.get("type") == "tool_execution_end"
                and event.get("toolName") == "taskroom_report")
        result = await self.fixture.result(run)
        self.assertEqual((result.state, result.delivery), ("finished", "full"))
        self.assertEqual(result.identity, run.identity)
        self.assertEqual(json.loads((self.fixture.project / "fixture-result.json").read_text())["counter"], 1)
        self.assertEqual(sum(event.get("type") == "tool_execution_end" and event.get("toolName") == "context_compact"
                for event in self.fixture.events), 2)
        board.ingest(result)
        self.assertEqual(task.status, "review")
        self.assertTrue(task.can_accept)
        with Store(Path(self.temp.name) / "tasks.sqlite3") as store:
            store.load()
            store.save(board)
            self.assertEqual(store.load().runs[run.id].identity, run.identity)

    async def assert_delivery(self, mode):
        _, _, run = await self.prepared(mode=mode)
        self.fixture.runtime.dispatch(run)
        await self.fixture.event(lambda event: event.get("type") == "tool_execution_end"
                and event.get("toolName") == "taskroom_report")
        result = await self.fixture.result(run)
        self.assertEqual((result.state, result.delivery), ("finished", "full"))
        self.assertEqual(result.identity, run.identity)
        self.assertEqual(json.loads((self.fixture.project / "fixture-result.json").read_text())["counter"], 1)
        return result

    async def test_failed_navigation_continues_only_on_original_branch(self):
        result = await self.assert_delivery("failure")
        self.assertTrue(any("取消/失败" in event for event in result.events))

    async def test_cancelled_compaction_continues_only_on_original_branch(self):
        result = await self.assert_delivery("cancel")
        self.assertTrue(any("取消/失败" in event for event in result.events))

    async def test_tool_error_does_not_arm_a_handoff(self):
        result = await self.assert_delivery("tool-error")
        self.assertFalse(any((event := parse_event(value)) is not None and event.kind == "invalidated" for value in result.events))

    async def test_successful_noop_does_not_arm_a_handoff(self):
        result = await self.assert_delivery("noop")
        self.assertFalse(any((event := parse_event(value)) is not None and event.kind == "invalidated" for value in result.events))

    async def test_stop_before_receipt_cannot_claim_to_cancel_deferred_continuation(self):
        _, _, run = await self.prepared(mode="pending-tool")
        self.fixture.runtime.dispatch(run)
        prompt = await self.fixture.event(lambda event: event.get("type") == "extension_ui_request"
                and event.get("title") == "Fixture compact receipt")
        working = self.fixture.runtime.read_run(run)
        assert working is not None
        self.fixture.runtime.stop(working)
        refused = await self.fixture.result(run, lambda result: any("无法取消外部扩展" in event for event in result.events))
        self.assertFalse(refused.terminal)
        self.assertFalse(any("收到人工停止请求" in event for event in refused.events))
        await self.fixture.send({"type": "extension_ui_response", "id": prompt["id"], "confirmed": True})
        result = await self.fixture.result(run)
        self.assertEqual((result.state, result.delivery), ("finished", "full"))
        self.assertEqual(json.loads((self.fixture.project / "fixture-result.json").read_text())["counter"], 1)

    async def test_unsolicited_pi_context_notice_cannot_be_adopted(self):
        _, _, run = await self.prepared("CONFIRM")
        self.fixture.runtime.dispatch(run)
        prompt = await self.fixture.event(lambda event: event.get("type") == "extension_ui_request"
                and event.get("title") == "Offline fixture permission")
        self.assertTrue((await self.fixture.request("prompt", message="/fixture-compaction notice"))["success"])
        await self.fixture.send({"type": "extension_ui_response", "id": prompt["id"], "confirmed": True})
        result = await self.fixture.result(run)
        self.assertEqual((result.state, result.delivery), ("interrupted", ""))
        self.assertTrue(any("未经本轮请求" in event for event in result.events))

    async def pause(self, mode="pause"):
        board, task, run = await self.prepared(mode=mode)
        self.fixture.runtime.dispatch(run)
        await self.fixture.event(lambda event: event.get("type") == "extension_ui_request"
                and event.get("message") == "fixture-compaction-paused")
        self.assertFalse(self.fixture.runtime.agent(run.agent_id).available)
        pending = self.fixture.runtime.read_run(run)
        assert pending is not None
        self.assertFalse(pending.terminal)
        self.assertEqual(pending.identity, run.identity)
        board.ingest(pending)
        self.assertEqual(task.status, "running")
        self.assertFalse(task.can_accept)
        return run, pending

    async def release(self):
        self.assertTrue((await self.fixture.request("prompt", message="/fixture-compaction release"))["success"])

    async def test_gap_keeps_binding_refuses_unsafe_stop_and_unrelated_navigation(self):
        run, pending = await self.pause()
        self.assertEqual(self.fixture.runtime.agent(run.agent_id).active_run, run.id)
        with self.assertRaises(BridgeError):
            self.fixture.runtime.dispatch(run)
        self.fixture.runtime.stop(pending)
        refused = await self.fixture.result(run, lambda result: any("无法取消外部扩展" in event for event in result.events))
        self.assertFalse(refused.terminal)
        self.assertFalse(any("收到人工停止请求" in event for event in refused.events))
        self.assertTrue((await self.fixture.request("prompt", message="/fixture-compaction navigate"))["success"])
        await self.fixture.event(lambda event: event.get("message") == "fixture-navigation-cancelled=true")
        await self.release()
        result = await self.fixture.result(run)
        self.assertEqual((result.state, result.delivery), ("finished", "full"))
        self.assertEqual(json.loads((self.fixture.project / "fixture-result.json").read_text())["counter"], 1)

    async def test_missing_continuation_stays_unknown_not_failed_or_replayed(self):
        run, _ = await self.pause("missing")
        await self.release()
        state = await self.fixture.request("get_state")
        self.assertFalse(state["data"]["isStreaming"])
        pending = self.fixture.runtime.read_run(run)
        assert pending is not None
        self.assertFalse(pending.terminal)
        self.assertEqual(self.fixture.runtime.agent(run.agent_id).active_run, run.id)
        self.assertEqual(json.loads((self.fixture.project / "fixture-result.json").read_text())["counter"], 1)

    async def test_extra_input_during_gap_is_not_adopted_by_continuation(self):
        run, _ = await self.pause()
        self.assertTrue((await self.fixture.request("prompt", message="Independent human input, not this task."))["success"])
        interrupted = await self.fixture.result(run)
        self.assertEqual((interrupted.state, interrupted.delivery), ("interrupted", ""))
        await self.release()
        await self.fixture.request("get_state")
        self.assertEqual(self.fixture.runtime.read_run(run), interrupted)
        self.assertEqual(json.loads((self.fixture.project / "fixture-result.json").read_text())["counter"], 1)

    async def test_mismatched_summary_cannot_restore_report_authority(self):
        _, _, run = await self.prepared(mode="mismatch")
        self.fixture.runtime.dispatch(run)
        await self.fixture.event(lambda event: event.get("type") == "extension_ui_request"
                and "Cannot verify the pi-context continuation" in event.get("message", ""))
        self.assertFalse(self.fixture.runtime.agent(run.agent_id).online)
        result = self.fixture.runtime.read_run(run)
        assert result is not None
        self.assertFalse(result.terminal)
        self.assertNotEqual(result.delivery, "full")
        self.assertEqual(json.loads((self.fixture.project / "fixture-result.json").read_text())["counter"], 1)

    @unittest.skipIf(os.getuid() == 0, "root bypasses directory write denial")
    async def test_resume_write_failure_pauses_bridge_without_accepting_report(self):
        run, pending = await self.pause()
        runs = self.fixture.runtime.endpoint(run.agent_id) / "runs"
        runs.chmod(0o500)
        try:
            await self.release()
            await self.fixture.event(lambda event: event.get("type") == "extension_ui_request"
                    and "桥接已暂停" in event.get("message", ""))
            self.assertFalse(self.fixture.runtime.agent(run.agent_id).online)
            self.assertEqual(self.fixture.runtime.read_run(run), pending)
        finally:
            runs.chmod(0o700)

    async def test_real_stop_after_verified_continuation_still_stops(self):
        _, _, run = await self.prepared("COMPACT_SLOW")
        self.fixture.runtime.dispatch(run)
        await self.fixture.event(lambda event: event.get("type") == "tool_execution_start"
                and event.get("toolCallId", "").startswith("continued-effect-"))
        working = self.fixture.runtime.read_run(run)
        assert working is not None
        self.fixture.runtime.stop(working)
        result = await self.fixture.result(run)
        self.assertEqual((result.state, result.delivery), ("stopped", ""))
        self.assertEqual(json.loads((self.fixture.project / "fixture-result.json").read_text())["counter"], 1)

    async def test_provider_error_after_continuation_is_not_ignored(self):
        _, _, run = await self.prepared("COMPACT_PROVIDER_ERROR")
        self.fixture.runtime.dispatch(run)
        result = await self.fixture.result(run)
        self.assertEqual((result.state, result.delivery), ("failed", ""))
        self.assertIn("Deliberate offline provider failure", result.summary)
        self.assertTrue(any(event.get("type") == "message_end" and event["message"].get("customType") == "pi-context" for event in self.fixture.events))
        self.assertTrue(any((event := parse_event(value)) is not None and event.kind == "failed" for value in result.events))

    async def test_ordinary_provider_error_is_still_failed(self):
        _, _, run = await self.prepared("PROVIDER_ERROR")
        self.fixture.runtime.dispatch(run)
        result = await self.fixture.result(run)
        self.assertEqual((result.state, result.delivery), ("failed", ""))
        self.assertIn("Deliberate offline provider failure", result.summary)


@unittest.skipUnless(shutil.which("pi") and PI_CONTEXT.is_file(), "installed Pi and pi-context required; set TASKROOM_TEST_PI_CONTEXT")
class InstalledPiContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_installed_context_compact_preserves_run_and_single_effect(self):
        with tempfile.TemporaryDirectory(prefix="taskroom-real-compact-") as directory:
            fixture = PiFixture(Path(directory))
            try:
                await fixture.start(extra_extensions=(PI_CONTEXT,))
                self.assertTrue((await fixture.request("prompt", message="/acm"))["success"])
                agent = await fixture.connect()
                assert agent is not None
                board = Board(live=True)
                board.add_project("临时项目", str(fixture.project))
                task = board.create("COMPACT_TWICE", "临时项目")
                board.assign(task, agent.id)
                run = board.queue_real(task, str(uuid4()), agent.session_id, agent.cwd)
                fixture.runtime.dispatch(run)
                await fixture.event(lambda event: event.get("type") == "tool_execution_end"
                        and event.get("toolName") == "taskroom_report")
                result = await fixture.result(run)
                self.assertEqual((result.state, result.delivery), ("finished", "full"))
                self.assertEqual(result.identity, run.identity)
                self.assertEqual(json.loads((fixture.project / "fixture-result.json").read_text())["counter"], 1)
                self.assertEqual(sum(event.get("type") == "tool_execution_end" and event.get("toolName") == "context_compact" and not event.get("isError") for event in fixture.events), 2)
                self.assertFalse(any("续接已核验" in value or "工具开始" in value or "工具返回成功" in value for value in result.events))
            finally:
                await fixture.close()


if __name__ == "__main__":
    unittest.main()
