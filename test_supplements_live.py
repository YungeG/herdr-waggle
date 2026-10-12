"""Installed Pi + offline fixture; supplement tests never access real sessions/data."""
import asyncio
import json
import shutil
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from model import Board
from runtime import publish, supplement_request
from test_bridge import PiFixture


@unittest.skipUnless(shutil.which("pi"), "installed Pi required")
class SupplementLive(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = TemporaryDirectory(prefix="waggle-supplement-live-")
        self.addCleanup(temp.cleanup)
        self.fixture = PiFixture(Path(temp.name))
        self.addAsyncCleanup(self.fixture.close)
        await self.fixture.start(extra_extensions=(Path(__file__).with_name("fixture-compaction.ts"),))
        self.assertTrue((await self.fixture.request("prompt", message="/fixture-compaction normal"))["success"])
        self.agent = await self.fixture.connect()
        assert self.agent
        self.board = Board(live=True)
        self.board.add_project("fixture", self.agent.cwd)

    async def start_run(self, title="SUPPLEMENT"):
        assert self.agent
        task = self.board.create(title, "fixture", "original dispatch scope")
        self.board.assign(task, self.agent.id)
        run = self.board.queue_real(task, str(uuid4()), self.agent.session_id, self.agent.cwd)
        self.fixture.runtime.dispatch(run)
        await self.fixture.event(lambda event: event.get("type") == "tool_execution_start" and event.get("toolName") == "waggle_fixture_effect")
        incoming = await self.fixture.result(run, lambda result: result.state == "working")
        self.board.ingest(incoming)
        return task, self.board.runs[run.id]

    async def receipt(self, run, note, status):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            value = self.fixture.runtime.read_supplement(run, note)
            if value and value.status == status:
                return value
            await asyncio.sleep(0.04)
        self.fail(f"No {status} receipt: {self.fixture.runtime.read_supplement(run, note)}\n{self.fixture.stderr}\n{self.fixture.events[-8:]}")

    def release(self):
        (self.fixture.project / "fixture-supplement-release").write_text("release")

    async def test_real_steer_receipt_report_coverage_and_no_replay(self):
        task, run = await self.start_run()
        identity = run.identity
        note = self.board.prepare_supplement(task, str(uuid4()), "SUPPLEMENT-CONTENT：补充必须进入模型", int(time.time() * 1000) + 30000)
        self.fixture.runtime.send_supplement(run, note)
        queued = await self.receipt(run, note, "queued")
        self.assertFalse(task.can_accept)
        self.release()
        result = await self.fixture.result(run)
        self.assertEqual((result.state, result.delivery, result.identity), ("finished", "full", identity))
        covered = await self.receipt(run, note, "covered")
        self.assertGreater(covered.seq, queued.seq)
        visible = json.loads((self.fixture.project / "fixture-supplement-visible.json").read_text())
        self.assertEqual(len(visible), 1)
        self.assertIn(note.text, visible[0])
        self.board.ingest(result)
        self.assertFalse(task.can_accept)
        self.board.apply_supplement(covered)
        self.assertTrue(task.can_accept)
        self.assertEqual(result.description, "original dispatch scope")
        self.assertEqual(json.loads((self.fixture.project / "fixture-result.json").read_text())["counter"], 1)
        target = self.fixture.runtime.endpoint(run.agent_id) / "supplements/requests" / f"{note.id}.json"
        publish(target, supplement_request(run, note))
        deadline = time.monotonic() + 3
        while target.exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        self.assertFalse(target.exists())
        self.assertEqual(self.fixture.runtime.read_supplement(run, note), covered)
        self.assertEqual(json.loads((self.fixture.project / "fixture-result.json").read_text())["counter"], 1)

    async def test_multiple_received_notes_require_latest_token_and_cover_all(self):
        task, run = await self.start_run("SUPPLEMENT_MULTI")
        first = self.board.prepare_supplement(task, str(uuid4()), "first", int(time.time() * 1000) + 30000)
        self.fixture.runtime.send_supplement(run, first)
        await self.receipt(run, first, "queued"); self.release()
        await self.fixture.event(lambda event: event.get("type") == "tool_execution_start" and event.get("toolCallId") == f"second-{run.id}")
        received = await self.receipt(run, first, "received")
        updated = self.fixture.runtime.read_run(run); assert updated
        self.board.ingest(updated); self.board.apply_supplement(received)
        second = self.board.prepare_supplement(task, str(uuid4()), "second", int(time.time() * 1000) + 30000)
        self.fixture.runtime.send_supplement(updated, second)
        await self.receipt(run, second, "queued"); self.release()
        result = await self.fixture.result(run)
        self.board.ingest(result)
        self.board.apply_supplement(await self.receipt(run, first, "covered"))
        self.assertFalse(task.can_accept)
        self.board.apply_supplement(await self.receipt(run, second, "covered"))
        self.assertTrue(task.can_accept)
        self.assertEqual(len(json.loads((self.fixture.project / "fixture-supplement-visible.json").read_text())), 2)

    async def test_accepted_supplement_and_required_report_token_survive_compaction(self):
        for title, expected in (("SUPPLEMENT_COMPACT", "covered"), ("SUPPLEMENT_COMPACT_STALE", "received")):
            with self.subTest(title=title):
                task, run = await self.start_run(title)
                note = self.board.prepare_supplement(task, str(uuid4()), "COMPACT-NOTE：续接也必须覆盖", int(time.time() * 1000) + 30000)
                self.fixture.runtime.send_supplement(run, note)
                await self.receipt(run, note, "queued"); self.release()
                result = await self.fixture.result(run)
                receipt = await self.receipt(run, note, expected)
                self.assertEqual(result.identity, run.identity)
                summary = (self.fixture.project / "fixture-supplement-summary.txt").read_text()
                self.assertIn(note.text, summary)
                self.assertIn(note.id, summary)
                self.board.ingest(result); self.board.apply_supplement(receipt)
                self.assertEqual(task.can_accept, expected == "covered")
                self.assertEqual(result.state, "finished" if expected == "covered" else "failed")

    async def test_reservation_write_failure_cannot_inject_a_message(self):
        task, run = await self.start_run()
        note = self.board.prepare_supplement(task, str(uuid4()), "NOT-INJECTED", int(time.time() * 1000) + 30000)
        receipts = self.fixture.runtime.endpoint(run.agent_id) / "supplements/receipts"
        receipts.chmod(0o500)
        self.addCleanup(receipts.chmod, 0o700)
        self.fixture.runtime.send_supplement(run, note)
        deadline = time.monotonic() + 5
        while self.fixture.runtime.agent(run.agent_id).online and time.monotonic() < deadline: await asyncio.sleep(0.05)
        self.assertFalse(self.fixture.runtime.agent(run.agent_id).online)
        self.assertFalse(any(event.get("message", {}).get("customType") == "waggle-supplement" for event in self.fixture.events if isinstance(event.get("message"), dict)))
        self.assertIsNone(self.fixture.runtime.read_supplement(run, note))
        self.assertFalse(task.can_accept)

    async def test_stale_report_without_supplement_token_is_not_full_delivery(self):
        task, run = await self.start_run("SUPPLEMENT_STALE")
        note = self.board.prepare_supplement(task, str(uuid4()), "must cover", int(time.time() * 1000) + 30000)
        self.fixture.runtime.send_supplement(run, note)
        await self.receipt(run, note, "queued"); self.release()
        result = await self.fixture.result(run)
        self.assertEqual(result.state, "failed")
        self.assertNotEqual(result.delivery, "full")
        received = await self.receipt(run, note, "received")
        self.board.ingest(result); self.board.apply_supplement(received)
        self.assertFalse(task.can_accept)

    async def test_extra_input_revokes_queued_note_before_it_can_enter_model_context(self):
        task, run = await self.start_run()
        note = self.board.prepare_supplement(task, str(uuid4()), "MUST-NOT-ENTER-OTHER-WORK", int(time.time() * 1000) + 30000)
        self.fixture.runtime.send_supplement(run, note)
        await self.receipt(run, note, "queued")
        await self.fixture.native_input("Unrelated human input", "独立输入（解除任务关联）", streaming="steer")
        await self.receipt(run, note, "rejected")
        self.release()
        result = await self.fixture.result(run)
        self.assertEqual((result.state, result.delivery), ("interrupted", ""))
        self.assertEqual(json.loads((self.fixture.project / "fixture-supplement-visible.json").read_text()), [])

    async def test_wrong_scope_and_expired_requests_never_reach_the_model(self):
        task, run = await self.start_run()
        note = self.board.prepare_supplement(task, str(uuid4()), "WRONG-SCOPE", int(time.time() * 1000) + 30000)
        request = supplement_request(run, note); request["run"]["description"] = "another scope"
        endpoint = self.fixture.runtime.endpoint(run.agent_id)
        target = endpoint / "supplements/requests" / f"{note.id}.json"
        publish(target, request)
        deadline = time.monotonic() + 4
        while target.exists() and time.monotonic() < deadline: await asyncio.sleep(0.05)
        raw = json.loads((endpoint / "supplements/receipts" / f"{note.id}.json").read_text())
        self.assertEqual(raw["status"], "rejected")
        with self.assertRaises(ValueError): self.fixture.runtime.read_supplement(run, note)
        expired = supplement_request(run, note); expired["id"] = str(uuid4()); expired["expires"] = 1
        expired_file = endpoint / "supplements/requests" / f"{expired['id']}.json"
        publish(expired_file, expired)
        deadline = time.monotonic() + 4
        while expired_file.exists() and time.monotonic() < deadline: await asyncio.sleep(0.05)
        self.assertEqual(json.loads((endpoint / "supplements/receipts" / f"{expired['id']}.json").read_text())["status"], "rejected")
        self.release(); result = await self.fixture.result(run)
        self.assertEqual(json.loads((self.fixture.project / "fixture-supplement-visible.json").read_text()), [])
        self.board.ingest(result)
        self.assertFalse(task.can_accept)  # A mismatched receipt cannot clear the saved intent.
