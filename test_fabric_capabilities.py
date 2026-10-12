"""Real offline Pi + installed Fabric; never opens a real task database/session."""
import asyncio
import json
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from app import Waggle
from decisions import proposal_requirements
from journal import parse_event
from model import Board
from runtime import BridgeError, read_json
from store import Store, encode
from task_context import WorkContract
from test_bridge import PiFixture


class ActualFabricOffline(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory(prefix="waggle-fabric-cap-")))
        self.fixture = PiFixture(self.root)
        self.addAsyncCleanup(self.fixture.close)

    async def test_native_registry_positive_filtered_fabric_route_rejects_before_intent(self):
        fixture = self.fixture
        await fixture.start(fabric=True, fabric_allowlist=())
        agent = await fixture.connect()
        assert agent
        endpoint = fixture.runtime.endpoint(agent.id)
        proof = read_json(endpoint / "report-capabilities.json")
        self.assertTrue(proof["active"], "native active registration must NOT stand in for Fabric invocation")
        self.assertIn("decisions-v3", proof["features"])
        start = len(fixture.events)
        await fixture.request("prompt", message="FIXTURE_FABRIC_ROUTE_PROBE")
        await fixture.event(lambda event: event.get("type") == "agent_settled", after=start, timeout=25)
        receipt = next(event["message"] for event in fixture.events[start:] if event.get("type") == "message_end"
                       and event.get("message", {}).get("role") == "toolResult" and event["message"].get("toolName") == "fabric_exec")
        result = json.loads("\n".join(part["text"] for part in receipt["content"] if part["type"] == "text"))
        self.assertFalse(result["listed"])
        self.assertIn("Unknown Fabric action: extensions.waggle_report", result["error"])
        with Store(self.root / "tasks.sqlite3") as store:
            board = store.load()
            board.add_project("fixture", str(fixture.project))
            task = board.create("HANDOFF", "fixture")
            board.assign(task, agent.id)
            task.remaining = proposal_requirements("只核对临时四项报告，不运行新工作")
            store.save(board)
            before = encode(store.load())
            app = Waggle(store=store, runtime=fixture.runtime)
            async with app.run_test(size=(80, 24)):
                sent = await app.dispatch_task(deepcopy(app.board.tasks[task.id]), fixture.runtime.agent(agent.id))
                self.assertFalse(sent, "missing actual Fabric report route must stop BEFORE a new intent")
                self.assertEqual(encode(store.load()), before)
                self.assertFalse(app.board.runs)
                self.assertFalse(list(endpoint.glob("commands/*.json")))
        self.assertFalse((fixture.project / "fixture-result.json").exists())

    async def test_disabled_capture_cannot_pass_actual_channel_preflight(self):
        fixture = self.fixture
        await fixture.start(fabric=True, fabric_capture=False)
        agent = await fixture.connect()
        assert agent
        with self.assertRaises(BridgeError):
            fixture.runtime.check_dispatch(agent, "plain isolated request")
        self.assertFalse(list(fixture.runtime.endpoint(agent.id).glob("commands/*.json")))

    async def test_orchestration_keeps_native_report_path(self):
        f = self.fixture
        await f.start(fabric=True, fabric_full_code=False)
        agent = await f.connect()
        assert agent
        proof = read_json(f.runtime.endpoint(agent.id) / "report-capabilities.json")
        self.assertTrue(proof["callable"], (proof, f.stderr))
        self.assertEqual((proof["transport"], proof["ref"]), ("native", "waggle_report"))
        board = Board(live=True)
        board.add_project("fixture", str(f.project))
        task = board.create("HANDOFF", "fixture")
        board.assign(task, agent.id)
        contract = WorkContract(
            purpose="implementation",
            deliverable="完成整任务交付",
            allowed_scope="项目目录",
            verification="回归测试通过",
            exclusions="不修改其他模块",
        )
        task.remaining = contract.compile()
        run = board.queue_real(task, str(uuid4()), agent.session_id, agent.cwd)
        f.runtime.dispatch(run)
        finished = await f.result(run)
        self.assertEqual((finished.state, finished.delivery), ("finished", "partial"))
        self.assertEqual(json.loads((f.project / "fixture-result.json").read_text())["counter"], 1)

    async def test_permission_denial_has_no_fallback_or_new_turn(self):
        f = self.fixture
        await f.start(fabric=True, fabric_approvals={"read": "allow", "execute": "deny"})
        agent = await f.connect()
        assert agent
        endpoint = f.runtime.endpoint(agent.id)
        proof = read_json(endpoint / "report-capabilities.json")
        self.assertTrue(proof["active"])
        self.assertFalse(proof["callable"])
        with self.assertRaises(BridgeError):
            f.runtime.check_dispatch(agent, "plain")
        self.assertFalse(list(endpoint.glob("commands/*.json")))
        self.assertFalse(list(endpoint.glob("runs/*.json")))
        self.assertFalse(any(event.get("type") == "agent_start" for event in f.events))

    async def test_idle_freshness_does_not_create_turns_or_reports(self):
        f = self.fixture
        await f.start(fabric=True)
        agent = await f.connect()
        assert agent
        endpoint = f.runtime.endpoint(agent.id)
        first = read_json(endpoint / "report-capabilities.json")
        self.assertEqual(first["boundary"], "fabric-host-guard")
        self.assertTrue(first["callable"])
        await asyncio.sleep(12)
        latest = read_json(endpoint / "report-capabilities.json")
        self.assertTrue(latest["callable"], (latest, f.stderr))
        self.assertGreater(latest["verified_at"], first["verified_at"] + 8000)
        f.runtime.check_dispatch(f.runtime.agent(agent.id), "plain")
        self.assertFalse(list(endpoint.glob("runs/*.json")))
        self.assertFalse(any(event.get("type") == "agent_start" for event in f.events))

    async def test_consumption_rechecks_withdrawn_fabric_and_never_reinjects(self):
        f = self.fixture
        await f.start(fabric=True)
        agent = await f.connect()
        assert agent
        endpoint = f.runtime.endpoint(agent.id)
        f.runtime.check_dispatch(agent, "plain")
        board = Board(live=True)
        board.add_project("fixture", str(f.project))
        task = board.create("HANDOFF", "fixture")
        board.assign(task, agent.id)
        contract = WorkContract(
            purpose="implementation",
            deliverable="完成整任务交付",
            allowed_scope="项目目录",
            verification="回归测试通过",
            exclusions="不修改其他模块",
        )
        task.remaining = contract.compile()
        run = board.queue_real(task, str(uuid4()), agent.session_id, agent.cwd)
        config_file = f.root / "pi-config/fabric.json"
        config = json.loads(config_file.read_text())
        config["capture"]["enabled"] = False
        config_file.write_text(json.dumps(config))
        response = await f.request("prompt", message="/fabric reload")
        self.assertTrue(response["success"], response)
        f.runtime._command(run, "dispatch")  # Downgrade after the earlier publication precheck.
        failed = await f.result(run)
        self.assertEqual((failed.state, failed.delivery), ("failed", ""))
        self.assertTrue(read_json(endpoint / "report-capabilities.json")["active"])
        self.assertFalse((f.project / "fixture-result.json").exists())
        before = (endpoint / "runs" / f"{run.id}.json").read_bytes()
        f.runtime._command(run, "dispatch")
        async with asyncio.timeout(8):
            while list(endpoint.glob("commands/*.json")):
                await asyncio.sleep(0.05)
        self.assertEqual((endpoint / "runs" / f"{run.id}.json").read_bytes(), before)
        self.assertFalse(any(event.get("type") == "agent_start" for event in f.events))

    async def test_probe_cannot_save_report_when_middleware_rewrites_the_run_id(self):
        f = self.fixture
        await f.start(fabric=True, extra_extensions=(Path(__file__).with_name("fixture-capabilities.ts"),))
        agent = await f.connect()
        assert agent
        await f.request("prompt", message="/capability-fixture rewrite-probes")
        board = Board(live=True)
        board.add_project("fixture", str(f.project))
        task = board.create("SLOW", "fixture")
        board.assign(task, agent.id)
        contract = WorkContract(
            purpose="implementation",
            deliverable="完成整任务交付",
            allowed_scope="项目目录",
            verification="回归测试通过",
            exclusions="不修改其他模块",
        )
        task.remaining = contract.compile()
        run = board.queue_real(task, str(uuid4()), agent.session_id, agent.cwd)
        f.runtime.dispatch(run)
        rewritten = f.project / "fixture-probe-rewrite.json"
        async with asyncio.timeout(10):
            while not rewritten.exists():
                await asyncio.sleep(0.05)
        self.assertEqual(json.loads(rewritten.read_text())["currentRun"], run.id)
        await f.request("get_state")
        incoming = f.runtime.read_run(run)
        assert incoming
        self.assertFalse([raw for raw in incoming.events if (event := parse_event(raw)) and event.report],
                         "a fixed diagnostic must never write delivery, even after supported middleware transforms input")
        self.assertEqual(incoming.delivery, "")
        self.assertFalse((f.project / "fixture-result.json").exists())

    async def test_exclusive_full_code_four_decisions_and_handoff_roundtrip(self):
        fixture = self.fixture
        await fixture.start(fabric=True)
        agent = await fixture.connect()
        assert agent
        proof = read_json(fixture.runtime.endpoint(agent.id) / "report-capabilities.json")
        self.assertTrue(proof.get("callable"), ("invocation-bound proof required", proof, fixture.stderr[-3000:]))
        self.assertEqual((proof["transport"], proof["ref"]), ("fabric", "extensions.waggle_report"))
        board = Board(live=True)
        board.add_project("fixture", str(fixture.project))
        task = board.create("DECISIONS", "fixture")
        board.assign(task, agent.id)
        task.remaining = proposal_requirements("提交四项隔离问题，仅测试报告通道")
        run = board.queue_real(task, str(uuid4()), agent.session_id, agent.cwd)
        fixture.runtime.dispatch(run)
        finished = await fixture.result(run, timeout=25)
        self.assertEqual((finished.state, finished.delivery), ("finished", "partial"))
        report = next(event.report for raw in finished.events if (event := parse_event(raw)) and event.report)
        assert report.decisions and report.handoff
        self.assertEqual(len(report.decisions), 4)
        self.assertEqual(len([raw for raw in finished.events if (event := parse_event(raw)) and event.report]), 1)
        self.assertFalse(any((event := parse_event(raw)) and event.kind in {"warning", "history_gap"} for raw in finished.events))
        self.assertEqual([spec.key for spec in report.decisions], [f"fixture_{index}" for index in range(1, 5)])
        self.assertEqual(report.handoff.result, "已提交四项隔离问题")
        visible = json.loads((fixture.project / "fixture-fabric-loadout.json").read_text())
        self.assertEqual(visible, ["fabric_exec"], "the successful report must traverse the same exclusive full-code route")
        self.assertFalse((fixture.project / "fixture-result.json").exists())
        board.ingest(finished)
        self.assertFalse(task.can_accept)


if __name__ == "__main__":
    unittest.main()
