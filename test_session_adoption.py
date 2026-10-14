"""Session attachment probes use only disposable SQLite and private mailboxes."""
import asyncio
import json
import os
import shutil
import time
import unittest
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from model import Board
from runtime import Agent, BridgeError, Runtime, private_dir
from store import Store, encode
from task_context import WorkContract
from test_bridge import PiFixture
from test_capabilities import capability_record


class SessionAdoptionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory(prefix="waggle-adoption-")))
        self.project = self.root / "project"
        self.project.mkdir()
        self.runtime = Runtime(self.root / "bridges")
        self.board = Board(live=True)
        self.board.add_project("fixture", str(self.project))
        self.task = self.board.create("Existing task", "fixture", "Keep the independent goal")
        self.old = Agent(str(uuid4()), str(uuid4()), str(self.project), "original", "offline/fixture",
                         os.getpid(), int(time.time() * 1000), False, False, "")
        self.current = replace(self.old, id=str(uuid4()), name="reconnected", online=True, idle=True)
        self.board.assign(self.task, self.old.id)
        self.task.remaining = WorkContract("evidence", "Inspect existing reports", str(self.project),
                                           "Cite actual evidence", "No experiments or budget reuse").compile()
        self.source = self.board.queue_real(self.task, str(uuid4()), self.old.session_id, self.old.cwd)
        self.source.state, self.source.seq = "finished", 1
        self.source.delivery, self.source.summary, self.source.remaining = "partial", "Existing evidence", "Still incomplete"
        self.board.runs[self.source.id] = self.source
        self.task.run, self.task.status, self.task.summary = "finished", "review", self.source.summary
        self.task.remaining = self.source.remaining
        for agent in (self.old, self.current):
            endpoint = self.runtime.root / agent.id
            for directory in (self.runtime.root, endpoint, endpoint / "runs", endpoint / "commands",
                              endpoint / "adoptions", endpoint / "adoptions" / "requests",
                              endpoint / "adoptions" / "history", endpoint / "adoptions" / "receipts"):
                private_dir(directory, create=True)
            self.write(endpoint / "agent.json", {"version": 1, **asdict(agent)})
            self.write(endpoint / "session.json", {
                "version": 1, "agent_id": agent.id, "session_id": agent.session_id, "cwd": agent.cwd,
                "seq": 1, "observed_at": int(time.time() * 1000), "state": "idle" if agent.online else "offline",
                "run_id": "", "linked_run_id": "", "following": False, "input_pending": False,
            })
            self.write(endpoint / "report-capabilities.json", capability_record(agent))
        self.write(self.runtime.root / self.old.id / "runs" / f"{self.source.id}.json", asdict(self.source))
        self.write_capability()

    @staticmethod
    def write(path, row):
        path.write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")
        path.chmod(0o600)

    def write_capability(self, association_id=""):
        self.write(self.runtime.root / self.current.id / "adoption-capabilities.json", {
            "version": 1, "agent_id": self.current.id, "session_id": self.current.session_id,
            "cwd": self.current.cwd, "pid": self.current.pid, "observed_at": int(time.time() * 1000),
            "association_id": association_id,
        })

    def acknowledge(self, request, state="attached", *, run_id=""):
        now = int(time.time() * 1000)
        row = {"version": 1, "id": request.intent.id, "request_hash": request.intent.request_hash,
               "agent_id": self.current.id, "session_id": self.current.session_id, "cwd": self.current.cwd,
               "task_id": self.task.id, "seq": 1, "state": state, "at": now,
               "accepted_at": now if state in {"attached", "consumed"} else 0,
               "branch_anchor": "fixture-leaf", "run_id": run_id, "detail": "No execution was started"}
        self.write(self.runtime.root / self.current.id / "adoptions" / "receipts" / f"{request.intent.id}.json", row)
        self.write_capability(request.intent.id if state == "attached" else "")
        session = self.runtime.root / self.current.id / "session.json"
        data = json.loads(session.read_text())
        data["linked_run_id"] = self.source.id if state == "attached" else run_id
        self.write(session, data)

    def test_same_native_session_reconnect_requires_explicit_attachment_and_does_not_run(self):
        old = deepcopy(asdict(self.source))
        with self.assertRaises(BridgeError):
            self.runtime.check_new_round(self.source)
        request = self.runtime.prepare_adoption(self.board, self.task, self.current,
                                                "Continue from existing evidence only", previous_checked=True)
        self.board.request_adoption(self.task, request.intent)
        self.runtime.publish_adoption(request)
        self.acknowledge(request)
        batch = self.runtime.read_adoptions(self.board)
        self.board.apply_adoption(request.intent, batch.receipts[self.task.id])
        self.runtime.check_new_round(self.source, adoption=request.intent)
        self.assertEqual(asdict(self.source), old)
        self.assertEqual(len(self.board.runs), 1)
        self.assertEqual(self.task.agent, self.current.id)
        self.assertFalse(list((self.runtime.root / self.current.id / "commands").iterdir()))
        with Store(self.root / "tasks.sqlite3") as store:
            store.load()
            store.save(self.board)
            self.assertEqual(encode(store.load()), encode(self.board))
            self.assertEqual(store.connection.execute("PRAGMA user_version").fetchone()[0], 12)

    def test_previous_execution_must_be_explicitly_checked(self):
        before = encode(self.board)
        with self.assertRaises(BridgeError):
            self.runtime.prepare_adoption(self.board, self.task, self.current, "Checkpoint")
        self.assertEqual(encode(self.board), before)

    def test_cross_session_never_becomes_a_same_session_attachment(self):
        current = replace(self.current, session_id=str(uuid4()))
        self.current = current
        endpoint = self.runtime.root / current.id
        self.write(endpoint / "agent.json", {"version": 1, **asdict(current)})
        self.write_capability()
        session = json.loads((endpoint / "session.json").read_text())
        session["session_id"] = current.session_id
        self.write(endpoint / "session.json", session)
        with self.assertRaisesRegex(BridgeError, "同一"):
            self.runtime.prepare_adoption(self.board, self.task, current, "Checkpoint", previous_checked=True)
        self.assertFalse(list((endpoint / "adoptions" / "requests").iterdir()))

    def test_busy_unbound_session_waits_without_assigning_or_running(self):
        endpoint = self.runtime.root / self.current.id
        self.current = replace(self.current, idle=False)
        self.write(endpoint / "agent.json", {"version": 1, **asdict(self.current)})
        row = json.loads((endpoint / "session.json").read_text())
        row["state"] = "working"
        self.write(endpoint / "session.json", row)
        request = self.runtime.prepare_adoption(self.board, self.task, self.current, "Checkpoint", previous_checked=True)
        self.board.request_adoption(self.task, request.intent)
        self.runtime.publish_adoption(request)
        self.acknowledge(request, "waiting")
        batch = self.runtime.read_adoptions(self.board)
        self.assertFalse(self.board.apply_adoption(request.intent, batch.receipts[self.task.id]))
        self.assertEqual(self.task.agent, self.old.id)
        self.assertEqual(len(self.board.runs), 1)

    def test_request_or_receipt_change_fails_before_assignment(self):
        request = self.runtime.prepare_adoption(self.board, self.task, self.current, "Checkpoint", previous_checked=True)
        self.board.request_adoption(self.task, request.intent)
        self.runtime.publish_adoption(request)
        self.acknowledge(request)
        file = self.runtime.root / self.current.id / "adoptions" / "receipts" / f"{request.intent.id}.json"
        row = json.loads(file.read_text())
        row["request_hash"] = "0" * 64
        self.write(file, row)
        batch = self.runtime.read_adoptions(self.board)
        self.assertIn(self.task.id, batch.errors)
        self.assertFalse(batch.receipts)
        self.assertEqual(self.task.agent, self.old.id)

    def test_read_only_observer_cannot_consume_a_persisted_control_attachment(self):
        request = self.runtime.prepare_adoption(self.board, self.task, self.current, "Checkpoint", previous_checked=True)
        self.board.request_adoption(self.task, request.intent)
        self.runtime.publish_adoption(request)
        self.acknowledge(request)
        observer = Runtime(self.runtime.root)
        observer.observe(self.source, self.runtime.root)
        batch = observer.read_adoptions(self.board)
        self.assertIn(self.task.id, batch.errors)
        self.assertFalse(batch.receipts)
        self.assertFalse(batch.runs)
        self.assertEqual(observer.observation_identity(self.source), (self.source.agent_id, self.source.session_id, self.source.cwd))

    def test_missing_new_bridge_capability_never_publishes(self):
        endpoint = self.runtime.root / self.current.id
        (endpoint / "adoption-capabilities.json").unlink()
        with self.assertRaises(FileNotFoundError):
            self.runtime.prepare_adoption(self.board, self.task, self.current, "Checkpoint", previous_checked=True)
        self.assertFalse(list((endpoint / "adoptions" / "requests").iterdir()))

    def test_unmanaged_session_can_attach_to_an_independent_new_task(self):
        self.task = self.board.create("New task", "fixture", "Only future explicit instructions")
        request = self.runtime.prepare_adoption(self.board, self.task, self.current, "User-chosen checkpoint")
        self.assertIsNone(request.intent.source)
        self.board.request_adoption(self.task, request.intent)
        self.runtime.publish_adoption(request)
        self.acknowledge(request)
        batch = self.runtime.read_adoptions(self.board)
        self.board.apply_adoption(request.intent, batch.receipts[self.task.id])
        self.assertEqual((self.task.agent, self.task.run_id, self.task.run), (self.current.id, "", "none"))
        self.assertEqual(len(self.board.runs), 1)
        self.assertFalse(list((self.runtime.root / self.current.id / "commands").iterdir()))


@unittest.skipUnless(shutil.which("pi"), "installed offline Pi required")
class SessionAdoptionOffline(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory(prefix="waggle-adoption-pi-")))
        self.fixture = PiFixture(self.root)
        self.addAsyncCleanup(self.fixture.close)
        await self.fixture.start()
        self.board = Board(live=True)
        self.board.add_project("fixture", str(self.fixture.project))

    async def receipt(self, request, state):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            batch = self.fixture.runtime.read_adoptions(self.board)
            result = batch.receipts.get(request.intent.task_id)
            if result and result.state == state:
                return batch, result
            await asyncio.sleep(0.02)
        self.fail(f"No {state} adoption receipt: {batch.errors}; {self.fixture.stderr}")

    async def test_unmanaged_native_session_attachment_creates_no_turn_then_tracks_only_new_input(self):
        agent = await self.fixture.connect()
        assert agent is not None
        task = self.board.create("PARTIAL", "fixture", "Only the subsequent explicit fixture input")
        request = self.fixture.runtime.prepare_adoption(self.board, task, agent, "No earlier delivery is imported")
        self.board.request_adoption(task, request.intent)
        before = sum(event.get("type") == "agent_start" for event in self.fixture.events)
        self.fixture.runtime.publish_adoption(request)
        _, receipt = await self.receipt(request, "attached")
        self.board.apply_adoption(request.intent, receipt)
        self.assertFalse(self.board.runs)
        self.assertEqual(sum(event.get("type") == "agent_start" for event in self.fixture.events), before)
        self.assertFalse((self.fixture.project / "fixture-result.json").exists())
        response = await self.fixture.request("prompt", message="Now perform the bounded offline fixture and report")
        self.assertTrue(response["success"], response)
        batch, _ = await self.receipt(request, "consumed")
        incoming = batch.runs[0][1]
        finished = await self.fixture.result(incoming)
        self.board.ingest_adopted(request, finished)
        self.assertEqual((finished.state, finished.delivery), ("finished", "partial"))
        self.assertEqual(len(self.board.runs), 1)
        self.assertEqual(finished.adoption_id, request.intent.id)
        self.assertFalse(finished.adoption_parent)
        self.assertFalse(any(event.get("type") == "extension_ui_request" and event.get("method") == "select"
                             for event in self.fixture.events))
        self.assertFalse(list((self.fixture.runtime.root / agent.id / "commands").iterdir()))

    async def test_reconnected_original_session_preserves_old_run_and_uses_new_id_after_input(self):
        agent = await self.fixture.connect()
        assert agent is not None
        task = self.board.create("PARTIAL", "fixture", "Only a bounded fixture effect")
        self.board.assign(task, agent.id)
        self.board.set_work_contract(task, WorkContract("implementation", "Fixture report", agent.cwd,
                                                        "Actual offline report", "No network"))
        source = self.board.queue_real(task, str(uuid4()), agent.session_id, agent.cwd)
        self.fixture.runtime.dispatch(source)
        source = await self.fixture.result(source)
        self.board.ingest(source)
        before_run = deepcopy(asdict(source))
        source_file = self.fixture.runtime.root / agent.id / "runs" / f"{source.id}.json"
        before_bytes = source_file.read_bytes()
        response = await self.fixture.request("prompt", message="/waggle disconnect")
        self.assertTrue(response["success"])
        current = await self.fixture.connect()
        assert current is not None
        self.assertEqual(current.session_id, source.session_id)
        self.assertNotEqual(current.id, source.agent_id)
        request = self.fixture.runtime.prepare_adoption(self.board, task, current,
                                                        "Preserve the completed evidence", previous_checked=True)
        self.board.request_adoption(task, request.intent)
        turns = sum(event.get("type") == "agent_start" for event in self.fixture.events)
        self.fixture.runtime.publish_adoption(request)
        _, receipt = await self.receipt(request, "attached")
        self.board.apply_adoption(request.intent, receipt)
        self.fixture.runtime.check_new_round(source, adoption=request.intent)
        self.assertEqual(len(self.board.runs), 1)
        self.assertEqual(sum(event.get("type") == "agent_start" for event in self.fixture.events), turns)
        self.assertEqual(source_file.read_bytes(), before_bytes)
        response = await self.fixture.request("prompt", message="Continue only the newly instructed offline fixture")
        self.assertTrue(response["success"], response)
        batch, _ = await self.receipt(request, "consumed")
        incoming = await self.fixture.result(batch.runs[0][1])
        self.board.ingest_adopted(request, incoming)
        self.assertNotEqual(incoming.id, source.id)
        self.assertEqual(incoming.adoption_parent, source.id)
        self.assertEqual(self.board.lineage(incoming), [incoming, source])
        self.assertEqual(asdict(self.board.runs[source.id]), before_run)
        self.assertEqual(source_file.read_bytes(), before_bytes)
        with Store(self.root / "tasks.sqlite3") as store:
            store.load()
            store.save(self.board)
            self.assertEqual(encode(store.load()), encode(self.board))

    async def test_native_detach_revokes_attachment_without_starting_or_stopping_work(self):
        agent = await self.fixture.connect()
        assert agent is not None
        task = self.board.create("PARTIAL", "fixture", "Only future explicit input")
        request = self.fixture.runtime.prepare_adoption(self.board, task, agent, "No historical import")
        self.board.request_adoption(task, request.intent)
        self.fixture.runtime.publish_adoption(request)
        _, receipt = await self.receipt(request, "attached")
        self.board.apply_adoption(request.intent, receipt)
        response = await self.fixture.request("prompt", message="/waggle detach")
        self.assertTrue(response["success"])
        _, detached = await self.receipt(request, "detached")
        self.board.apply_adoption(request.intent, detached)
        self.assertEqual(detached.accepted_at, receipt.accepted_at)
        self.assertFalse(self.board.runs)
        self.assertFalse((self.fixture.project / "fixture-result.json").exists())

    async def test_busy_native_round_is_not_interrupted_or_retroactively_owned(self):
        await self.fixture.close()
        root = self.root / "busy"
        root.mkdir()
        gate = root / "release"
        ready = root / "ready"
        hold = root / "hold.ts"
        hold.write_text('import * as fs from "node:fs";\nexport default function(pi) { '
                        'pi.on("agent_before_settle", async () => { '
                        f'fs.writeFileSync({json.dumps(str(ready))}, "ready"); '
                        'const end = Date.now() + 5000; '
                        f'while (!fs.existsSync({json.dumps(str(gate))}) && Date.now() < end) '
                        'await new Promise(resolve => setTimeout(resolve, 10)); }); }\n')
        self.fixture = PiFixture(root)
        self.addAsyncCleanup(self.fixture.close)
        await self.fixture.start(extra_extensions=(hold,))
        self.board = Board(live=True)
        self.board.add_project("fixture", str(self.fixture.project))
        agent = await self.fixture.connect()
        assert agent is not None
        start = len(self.fixture.events)
        running = asyncio.create_task(self.fixture.request("prompt", message="Unmanaged existing native round"))
        try:
            end = time.monotonic() + 3
            while not ready.exists() and time.monotonic() < end:
                await asyncio.sleep(0.01)
            self.assertTrue(ready.exists())
            busy = self.fixture.runtime.agent(agent.id)
            self.assertFalse(busy.available)
            task = self.board.create("Future task", "fixture", "Do not claim the existing native round")
            request = self.fixture.runtime.prepare_adoption(self.board, task, busy, "Only after current round settles")
            self.board.request_adoption(task, request.intent)
            turns = sum(event.get("type") == "agent_start" for event in self.fixture.events)
            self.fixture.runtime.publish_adoption(request)
            _, waiting = await self.receipt(request, "waiting")
            self.assertEqual(waiting.accepted_at, 0)
            self.assertFalse(self.board.runs)
            self.assertFalse(any(event.get("type") == "agent_settled" for event in self.fixture.events[start:]))
            self.assertEqual(sum(event.get("type") == "agent_start" for event in self.fixture.events), turns)
            gate.touch()
            response = await asyncio.wait_for(running, 4)
            self.assertTrue(response["success"])
            _, attached = await self.receipt(request, "attached")
            self.board.apply_adoption(request.intent, attached)
            self.assertFalse(self.board.runs)
            self.assertEqual(sum(event.get("type") == "agent_start" for event in self.fixture.events), turns)
        finally:
            gate.touch(exist_ok=True)
            if not running.done():
                await asyncio.wait_for(running, 5)


if __name__ == "__main__":
    unittest.main()
