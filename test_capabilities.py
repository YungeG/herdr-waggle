"""Capability / plain handoff sentinels; disposable database and mailboxes only."""
import asyncio
import json
import os
import shutil
import subprocess
import time
import unittest
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from textual.widgets import Static

from app import Confirm, Waggle
from decisions import proposal_requirements
from delivery import detail_text, overview_text
from handoff import Handoff
from journal import PREFIX, Brief, Report, encoded_event, parse_event
from model import Run, Task
from runtime import (
    Agent,
    BridgeError,
    Runtime,
    SessionObservation,
    private_dir,
    publish,
    read_json,
)
from store import Store, encode
from task_actions import project_primary_action
from task_context import WorkContract


def capability_record(agent, *, features=None):
    """Synthetic positive fixture, never used for a real connection."""
    observed = int(time.time() * 1000)
    return {"version": 2, "agent_id": agent.id, "session_id": agent.session_id, "cwd": agent.cwd,
            "observed_at": observed, "tool": "waggle_report", "active": True,
            "callable": True, "transport": "native", "ref": "waggle_report", "verified_at": observed,
            "boundary": "native-report-guard",
            "schema_sha256": "a" * 64, "features": features if features is not None else ["report-v1", "handoff-v1", "decisions-v3"]}


class CapabilityGateSentinels(unittest.IsolatedAsyncioTestCase):
    async def test_old_connection_is_observed_but_preflight_leaves_db_and_mailbox_untouched(self):
        with TemporaryDirectory(prefix="waggle-capability-sentinel-") as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            runtime = Runtime(root / "bridges")
            agent = Agent(str(uuid4()), str(uuid4()), str(project), "old fixture", "offline/fixture",
                          os.getpid(), int(time.time() * 1000), True, True, "")
            endpoint = runtime.root / agent.id
            for directory in (runtime.root, endpoint, endpoint / "commands", endpoint / "runs"):
                private_dir(directory, create=True)
            publish(endpoint / "agent.json", {"version": 1, **asdict(agent)})
            self.assertTrue(runtime.agent(agent.id).available)
            with Store(root / "tasks.sqlite3") as store:
                board = store.load()
                board.add_project("fixture", str(project))
                task = board.create("handoff", "fixture")
                board.assign(task, agent.id)
                task.remaining = proposal_requirements("只核对临时材料，交可审提案")
                store.save(board)
                before = encode(store.load())
                app = Waggle(store=store, runtime=runtime)
                async with app.run_test(size=(80, 24)):
                    sent = await app.dispatch_task(deepcopy(app.board.tasks[task.id]), agent)
                    self.assertFalse(sent, "unknown effective schema must stop BEFORE committing a new run")
                    self.assertEqual(encode(store.load()), before)
                    self.assertFalse(list(endpoint.glob("commands/*.json")))
                    self.assertFalse(app.board.runs)

    async def test_incomplete_proposal_is_executor_pending_not_user_approval_or_same_retry(self):
        run = Run(str(uuid4()), 2, str(uuid4()), str(uuid4()), "/virtual/isolated", "提案", "",
                  proposal_requirements("请答勾选的问题"), state="failed", seq=1, delivery="blocked",
                  summary="technical_summary_schema", checks=["only self-reported"],
                  remaining="decisions.routing.evidence_needs unavailable; native cancellation 60s")
        run.events = [encoded_event("blocked", "report", source="executor", run_id=run.id, seq=1,
                      report=Report(run.summary, run.checks, run.remaining,
                                    Brief(["只核对了旧文件"], "仅核证，没有可审提案", "原件未提供；未运行新试验")))]
        task = Task(2, "提案", "fixture", status="blocked", run="finished", execution="pi", run_id=run.id,
                    blocker=run.remaining, summary=run.summary, checks=run.checks, remaining=run.remaining)
        action = project_primary_action(task, run, has_directory=True)
        self.assertNotIn("按 b 可追问", action.reason)
        self.assertIn("执行者", action.reason)
        plain = overview_text(task, run).plain
        self.assertIn("下一步", plain)
        self.assertIn("执行者", plain)
        self.assertIn("现在不需要你批准", plain)
        self.assertNotIn("decisions.routing", plain, "technical source stays in details, not the homepage")
        self.assertNotIn("需要你\n", plain)


class CapabilityRecords(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory(prefix="waggle-cap-record-")))
        self.project = self.root / "project"
        self.project.mkdir()
        self.runtime = Runtime(self.root / "bridges")
        self.agent = Agent(str(uuid4()), str(uuid4()), str(self.project), "fixture", "offline",
                           os.getpid(), int(time.time() * 1000), True, True, "")
        self.endpoint = self.runtime.root / self.agent.id
        for folder in (self.runtime.root, self.endpoint, self.endpoint / "commands", self.endpoint / "runs"):
            private_dir(folder, create=True)
        publish(self.endpoint / "agent.json", {"version": 1, **asdict(self.agent)})
        self.cap_file = self.endpoint / "report-capabilities.json"
        self.attempt = Run(str(uuid4()), 1, self.agent.id, self.agent.session_id, self.agent.cwd, "fixture", "", "")

    def proof(self, record):
        self.cap_file.unlink(missing_ok=True)
        publish(self.cap_file, record)

    def test_base_and_structured_features_are_independently_required(self):
        self.proof(capability_record(self.agent, features=["report-v1", "handoff-v1"]))
        self.runtime.check_dispatch(self.agent, "plain")
        with self.assertRaises(BridgeError):
            self.runtime.check_dispatch(self.agent, proposal_requirements("proposal"))
        with self.assertRaises(BridgeError):
            self.runtime.check_dispatch(self.agent, "feedback", structured=True)
        self.proof(capability_record(self.agent))
        self.runtime.dispatch(self.attempt)
        self.assertEqual(len(list(self.endpoint.glob("commands/*.json"))), 1)

    def test_identity_freshness_type_digest_and_effective_presence_fail_closed(self):
        for changes in ({"version": True}, {"version": 1}, {"session_id": str(uuid4())},
                        {"agent_id": str(uuid4())}, {"cwd": str(self.root)}, {"tool": "other"},
                        {"observed_at": 1}, {"observed_at": True},
                        {"observed_at": int(time.time() * 1000) + 10000}, {"active": False},
                        {"active": 1}, {"schema_sha256": ""}, {"schema_sha256": "not-a-digest"},
                        {"features": None}, {"features": [{}]}, {"features": ["report-v1"]},
                        {"features": ["report-v1", "report-v1"]}, {"unexpected": 1},
                        {"callable": "yes"}, {"callable": False}, {"transport": "other"}, {"transport": []},
                        {"ref": "extensions.waggle_report"}, {"boundary": "fabric-report-guard"},
                        {"verified_at": True}, {"verified_at": 0},
                        {"verified_at": int(time.time() * 1000) - 11000},
                        {"verified_at": int(time.time() * 1000) + 10000}):
            with self.subTest(changes=changes):
                self.proof(capability_record(self.agent) | changes)
                with self.assertRaises(BridgeError):
                    self.runtime.dispatch(self.attempt)
                self.assertFalse(list(self.endpoint.glob("commands/*.json")))
        self.proof(capability_record(self.agent))
        self.cap_file.chmod(0o644)
        with self.assertRaises(BridgeError):
            self.runtime.check_dispatch(self.agent, "plain")
        self.cap_file.chmod(0o600)
        target = self.root / "foreign-proof"
        self.cap_file.rename(target)
        self.cap_file.symlink_to(target)
        with self.assertRaisesRegex(BridgeError, "不可读"):
            self.runtime.check_dispatch(self.agent, "plain")
        self.assertFalse(list(self.endpoint.glob("commands/*.json")))

    def test_old_registration_and_refreshed_heartbeat_do_not_authorize(self):
        old = capability_record(self.agent)
        for key in ("callable", "transport", "ref", "verified_at", "boundary"):
            old.pop(key)
        old["version"] = 1
        stale = capability_record(self.agent)
        stale["verified_at"] -= 11000
        stale["observed_at"] = int(time.time() * 1000)
        for record in (old, stale):
            self.proof(record)
            with self.assertRaises(BridgeError):
                self.runtime.check_dispatch(self.agent, "plain")
        self.assertFalse(list(self.endpoint.glob("commands/*.json")))

    def test_typescript_reads_effective_schema_instead_of_source_version(self):
        script = '''
import { reportCapabilities, reportSchema, requiredReportFeatures } from './capabilities.ts';
const schema = reportSchema;
let parameters = structuredClone(schema), active = ['waggle_report'];
const pi = {getAllTools:()=>[{name:'waggle_report',parameters}],getActiveTools:()=>active};
const binding = {agent_id:'fixture',session_id:'session',cwd:'/scratch'};
const good = reportCapabilities(pi,binding,123);
delete parameters.properties.decisions;
const old = reportCapabilities(pi,binding,124);
parameters = structuredClone(schema);
parameters.properties.decisions.items.properties.routing.properties.evidence_needs.items.properties.source.type = 'number';
const wrongNested = reportCapabilities(pi,binding,125);
const incompatible = [];
for (const change of [s => {s.required.push('extra');s.properties.extra={type:'string'}},
 s => {s.properties.checks.maxItems=0}, s => {s.properties.run_id.pattern='^not-a-run-id$'}]) {
 parameters = structuredClone(schema); change(parameters); incompatible.push(reportCapabilities(pi,binding,125));
}
active = [];
const hidden = reportCapabilities(pi,binding,126);
const missing = reportCapabilities({getAllTools:()=>[],getActiveTools:()=>[]},binding,127);
console.log(JSON.stringify({good,old,wrongNested,hidden,missing,incompatible,required:requiredReportFeatures('结构化人工决定 · 草稿不是批准')}));
'''
        result = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True, check=True, timeout=15)
        values = json.loads(result.stdout)
        self.assertEqual(values["good"]["features"], ["report-v1", "handoff-v1", "decisions-v3"])
        for key in ("old", "wrongNested"):
            self.assertNotIn("decisions-v3", values[key]["features"])
            self.assertNotEqual(values["good"]["schema_sha256"], values[key]["schema_sha256"])
        self.assertFalse(values["hidden"]["active"])
        self.assertEqual(values["missing"]["features"], [])
        self.assertIn("decisions-v3", values["required"])
        for proof in values["incompatible"]:
            self.assertNotIn("report-v1", proof["features"])
            self.proof(capability_record(self.agent) | {key: proof[key] for key in ("active", "schema_sha256", "features")})
            with self.assertRaises(BridgeError):
                self.runtime.check_dispatch(self.agent, proposal_requirements("proposal"))
            with self.assertRaises(BridgeError):
                self.runtime.dispatch(self.attempt)
            self.assertFalse(list(self.endpoint.glob("commands/*.json")))


class HandoffCodec(unittest.TestCase):
    def test_python_typescript_codec_parity_and_legacy_absence(self):
        base = {"result": "仅核对了已有材料", "blocker": "缺具体交接方案", "next_actor": "executor",
                "next_action": "先补齐方案和原始依据", "user_need": "none"}
        cases = [(base, True), (base | {"blocker": ""}, True),
                 (base | {"next_actor": "user", "user_need": "input"}, True)]
        cases += [(base | change, False) for change in ({"result": ""}, {"result": "x" * 161},
                  {"result": "\ufeff"}, {"result": "\ud800"}, {"result": "a\nb"}, {"blocker": " "},
                  {"next_actor": ["user"]}, {"user_need": {}}, {"next_actor": "owner"},
                  {"user_need": "decision"}, {"next_actor": "user"}, {"extra": "not allowed"})]
        raw = encoded_event("partial", "fixture", source="executor", run_id=str(uuid4()), seq=1,
                            report=Report("完整原文", ["自报证据"], "缺材料"))
        original = json.loads(raw.removeprefix(PREFIX))
        wires = [(raw, True)]
        for value, expected in cases + [(None, False)]:
            row = deepcopy(original)
            row["report"]["handoff"] = value
            wires.append((PREFIX + json.dumps(row, ensure_ascii=True), expected))
        for wire, expected in wires:
            try:
                parse_event(wire)
                accepted = True
            except ValueError:
                accepted = False
            self.assertEqual(accepted, expected)
        script = '''import fs from 'node:fs'; import {parseEvent} from './journal.ts';
const values=JSON.parse(fs.readFileSync(0,'utf8'));
console.log(JSON.stringify(values.map(raw=>{try{parseEvent(raw);return true}catch{return false}})));'''
        out = subprocess.run(["node", "--input-type=module", "-e", script], input=json.dumps([wire for wire, _ in wires]),
                             capture_output=True, text=True, check=True, timeout=15)
        self.assertEqual(json.loads(out.stdout), [expected for _, expected in wires])
        self.assertNotIn("handoff", original["report"])

    def test_legacy_technical_brief_is_not_mistaken_for_a_plain_handoff(self):
        run = Run(str(uuid4()), 1, str(uuid4()), str(uuid4()), "/virtual", "fixture", "",
                  proposal_requirements("proposal"), state="failed", seq=1, delivery="blocked",
                  summary="technical source", checks=["self report"], remaining="missing proposal")
        brief = Brief(["checked old material"], "RAW_CODE decisions.routing: 60s budget not approved", "Not verified")
        run.events = [encoded_event("blocked", "fixture", source="executor", run_id=run.id, seq=1,
                      report=Report(run.summary, run.checks, run.remaining, brief))]
        task = Task(1, "fixture", "fixture", status="blocked", run="finished", run_id=run.id, execution="pi",
                    summary=run.summary, checks=run.checks, remaining=run.remaining)
        text = overview_text(task, run, capability_issue="legacy connection unknown").plain
        self.assertIn("现在需要你处理连接，不是批准任务方案", text)
        original = deepcopy((asdict(task), asdict(run)))
        for technical in ("RAW_CODE", "decisions.routing", "60s"):
            self.assertNotIn(technical, text)
            self.assertIn(technical, detail_text(task, run).plain)
        self.assertIn("自报，不能验收或正式授权", detail_text(task, run).plain)
        self.assertFalse(task.can_accept)
        self.assertEqual((asdict(task), asdict(run)), original)

    def test_short_claim_cannot_turn_missing_proposal_into_human_approval(self):
        run = Run(str(uuid4()), 1, str(uuid4()), str(uuid4()), "/virtual", "fixture", "",
                  proposal_requirements("proposal"), state="finished", seq=1, delivery="partial",
                  summary="unstructured", checks=["self report"], remaining="missing proposal")
        claim = Handoff("只核证了材料", "", "user", "请批准试验", "decision")
        run.events = [encoded_event("partial", "fixture", source="executor", run_id=run.id, seq=1,
                      report=Report(run.summary, run.checks, run.remaining, handoff=claim))]
        task = Task(1, "fixture", "fixture", status="review", run="finished", run_id=run.id, execution="pi")
        text = overview_text(task, run).plain
        self.assertIn("执行者", text)
        self.assertIn("现在不需要你批准", text)
        self.assertNotIn("请批准试验", text)


class PlainHandoffUI(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory(prefix="waggle-handoff-ui-")))
        self.project = self.root / "project"
        self.project.mkdir()
        self.runtime = Runtime(self.root / "bridges")
        self.store = self.enterContext(Store(self.root / "tasks.sqlite3"))
        board = self.store.load()
        board.add_project("fixture", str(self.project))
        self.agent = Agent(str(uuid4()), str(uuid4()), str(self.project), "fixture", "offline",
                           os.getpid(), int(time.time() * 1000), True, True, "")
        self.endpoint = self.runtime.root / self.agent.id
        for folder in (self.runtime.root, self.endpoint, self.endpoint / "commands", self.endpoint / "runs"):
            private_dir(folder, create=True)
        publish(self.endpoint / "agent.json", {"version": 1, **asdict(self.agent)})
        self.task = board.create("隔离提案", "fixture")
        board.assign(self.task, self.agent.id)
        self.task.remaining = proposal_requirements("只核对已有材料")
        run = deepcopy(board.queue_real(self.task, str(uuid4()), self.agent.session_id, self.agent.cwd))
        run.state, run.seq, run.delivery = "failed", 1, "blocked"
        run.summary, run.checks, run.remaining = "RAW_SCHEMA_BACKGROUND", ["fixture source"], "decisions.routing RAW_OWNER_MISSING"
        run.events = [encoded_event("blocked", "fixture", source="executor", run_id=run.id, seq=1,
                      report=Report(run.summary, run.checks, run.remaining, Brief(["核对旧文件"], "只有核证，未交提案", "未验证")))]
        board.ingest(run)
        self.store.save(board)
        self.attempt = run
        self.session = SessionObservation(self.agent.id, self.agent.session_id, self.agent.cwd, 1,
                                          self.agent.heartbeat, "idle", "", run.id, True, False)
        publish(self.endpoint / "session.json", {"version": 1, **asdict(self.session)})

    def app(self):
        app = Waggle(store=self.store, runtime=self.runtime)
        self.enterContext(patch.object(app, "schedule_poll"))
        self.enterContext(patch.object(self.runtime, "check_current"))
        app.agents[self.agent.id] = self.agent
        app.sessions[self.attempt.id] = self.session
        return app

    async def test_old_connection_help_is_visible_and_readonly_at_40_and_80(self):
        before = encode(self.store.load())
        for size in ((40, 20), (80, 24)):
            app = self.app()
            async with app.run_test(size=size) as pilot:
                await pilot.press("right")
                await pilot.pause()
                self.assertEqual(app.primary_action().action, "capability_help")
                body = str(app.query_one("#detail-text", Static).content)
                self.assertIn("现在需要你处理连接", body)
                self.assertNotIn("RAW_SCHEMA_BACKGROUND", body)
                self.assertNotIn("RAW_OWNER_MISSING", body)
                frame = chr(10).join(strip.text for strip in app.screen._compositor.render_strips())
                self.assertIn("检查连接", frame)
                self.assertIn("不是批准", frame)
                app.action_primary()
                await pilot.pause()
                self.assertIsInstance(app.screen, Confirm)
                assert isinstance(app.screen, Confirm)
                self.assertIn("/reload", app.screen.body)
                self.assertIn("加载路径未知", app.screen.body)
                self.assertIn(str(self.project), app.screen.body)
                await pilot.press("escape")
                self.assertEqual(encode(self.store.load()), before)
                self.assertFalse(list(self.endpoint.glob("commands/*.json")))
                self.assertIn("RAW_OWNER_MISSING", detail_text(app.board.tasks[self.task.id], self.attempt).plain)

    async def test_final_confirmation_schema_downgrade_keeps_editor_and_zero_new_intents(self):
        from textual.widgets import TextArea

        from app import Note, RequestReview
        from test_live import wait_ui
        publish(self.endpoint / "report-capabilities.json", capability_record(self.agent))
        before = encode(self.store.load())
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            app.run_worker(app.open_dispatch_supplement(self.task.id, intent="proposal"))
            await wait_ui(pilot, lambda: isinstance(app.screen, Note))
            form = app.screen
            assert isinstance(form, Note)
            form.query_one(TextArea).text = "请补齐原方案，不授权执行"
            await pilot.click("#submit")
            await wait_ui(pilot, lambda: isinstance(app.screen, RequestReview))
            review = app.screen
            (self.endpoint / "report-capabilities.json").unlink()
            await pilot.press("ctrl+enter")
            self.assertIs(app.screen, review)
            self.assertIn("尚未发送", str(review.query_one("#request-shortcut-hint", Static).content))
            self.assertEqual(encode(self.store.load()), before)
            self.assertFalse(list(self.endpoint.glob("commands/*.json")))
            self.assertTrue(await pilot.click("#submit"))
            await wait_ui(pilot, lambda: app.screen is form and not form._saving)
            self.assertEqual(form.query_one(TextArea).text, "请补齐原方案，不授权执行")
            self.assertEqual(encode(self.store.load()), before)
            self.assertFalse(list(self.endpoint.glob("commands/*.json")))

    async def test_publish_time_downgrade_keeps_single_uncertain_identity_and_never_retries(self):
        publish(self.endpoint / "report-capabilities.json", capability_record(self.agent))
        app = self.app()
        dispatch = self.runtime.dispatch
        def changed(run):
            (self.endpoint / "report-capabilities.json").unlink()
            return dispatch(run)
        async with app.run_test(size=(80, 24)):
            with patch.object(self.runtime, "dispatch", side_effect=changed) as publisher:
                sent = await app.dispatch_task(deepcopy(app.board.tasks[self.task.id]), self.agent,
                                               supplement=("next", proposal_requirements("check existing source")))
            self.assertFalse(sent)
            publisher.assert_called_once()
            self.assertEqual(len(app.board.runs), 2)
            new = app.board.runs[app.board.tasks[self.task.id].run_id]
            self.assertEqual(new.state, "queued")
            self.assertIn("可能已发布", app.runtime_errors[new.id])
            self.assertFalse(list(self.endpoint.glob("commands/*.json")))
            self.assertEqual(self.store.load().tasks[self.task.id].run_id, new.id)


@unittest.skipUnless(shutil.which("pi"), "installed offline Pi required")
class ActualSchemaOffline(unittest.IsolatedAsyncioTestCase):
    async def test_registered_schema_changes_cannot_rewrite_the_supported_contract(self):
        from test_bridge import PiFixture
        with TemporaryDirectory(prefix="waggle-contract-alias-") as temporary:
            fixture = PiFixture(Path(temporary))
            try:
                await fixture.start(extra_extensions=(Path(__file__).with_name("fixture-capabilities.ts"),))
                agent = await fixture.connect()
                assert agent
                endpoint = fixture.runtime.endpoint(agent.id)
                proof_file = endpoint / "report-capabilities.json"
                initial = read_json(proof_file)
                self.assertIn("report-v1", initial["features"])
                await fixture.request("prompt", message="/capability-fixture invalidate-schema")
                async with asyncio.timeout(8):
                    while read_json(proof_file)["observed_at"] <= initial["observed_at"]:
                        await asyncio.sleep(0.05)
                changed = read_json(proof_file)
                self.assertNotIn("report-v1", changed["features"], "actual schema and expected contract must NOT share mutable objects")
                with self.assertRaises(BridgeError):
                    fixture.runtime.check_dispatch(fixture.runtime.agent(agent.id), "plain request")
                self.assertFalse(list(endpoint.glob("commands/*.json")))
                self.assertFalse((fixture.project / "fixture-result.json").exists())
                await fixture.request("prompt", message="/capability-fixture restore-schema")
                async with asyncio.timeout(8):
                    while "report-v1" not in read_json(proof_file)["features"]:
                        await asyncio.sleep(0.05)
                fixture.runtime.check_dispatch(fixture.runtime.agent(agent.id), "plain request")
            finally:
                await fixture.close()

    async def test_active_schema_proof_legacy_continuation_and_short_report_roundtrip(self):
        from test_bridge import PiFixture
        with TemporaryDirectory(prefix="waggle-actual-cap-") as temporary:
            fixture = PiFixture(Path(temporary))
            try:
                await fixture.start(extra_extensions=(Path(__file__).with_name("fixture-capabilities.ts"),))
                agent = await fixture.connect()
                assert agent
                proof_file = fixture.runtime.endpoint(agent.id) / "report-capabilities.json"
                proof = read_json(proof_file)
                self.assertEqual(set(proof["features"]), {"report-v1", "handoff-v1", "decisions-v3"})
                fixture.runtime.check_dispatch(agent, proposal_requirements("fixture"))
                self.assertEqual(proof["session_id"], agent.session_id)
                self.assertEqual(len(proof["schema_sha256"]), 64)
                await fixture.request("prompt", message="/capability-fixture hide")
                await fixture.request("get_state")
                from model import Board
                board = Board(live=True)
                board.add_project("fixture", str(fixture.project))
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
                old = board.queue_real(task, str(uuid4()), agent.session_id, agent.cwd)
                fixture.runtime._command(old, "dispatch")  # Simulate a schema downgrade AFTER publication precheck.
                rejected = await fixture.result(old)
                self.assertEqual(rejected.state, "failed")
                self.assertFalse((fixture.project / "fixture-result.json").exists())
                async with asyncio.timeout(8):
                    while read_json(proof_file)["active"]:
                        await asyncio.sleep(0.05)
                with self.assertRaises(BridgeError):
                    fixture.runtime.check_dispatch(fixture.runtime.agent(agent.id), "plain")
                await fixture.request("prompt", message="/capability-fixture show")
                async with asyncio.timeout(8):
                    while not read_json(proof_file)["active"]:
                        await asyncio.sleep(0.05)
                run = replace(old, id=str(uuid4()))
                fixture.runtime.dispatch(run)
                finished = await fixture.result(run)
                self.assertEqual((finished.state, finished.delivery), ("finished", "partial"))
                reports = [event.report for raw in finished.events if (event := parse_event(raw)) and event.report]
                report = reports[0]
                assert report.handoff
                self.assertEqual(report.handoff.result, "仅核对了临时成果")
                self.assertEqual(json.loads((fixture.project / "fixture-result.json").read_text())["counter"], 1)
                before = len(fixture.events)
                await fixture.native_input("Continue this disposable task", "继续当前任务")
                await fixture.event(lambda event: event.get("type") == "agent_end", after=before)
                child = fixture.runtime.read_continuations(finished)[0]
                self.assertNotEqual(child.id, finished.id)
                child = await fixture.result(child)  # agent_end precedes the bridge settle receipt.
                self.assertEqual(child.state, "finished")
            finally:
                await fixture.close()


if __name__ == "__main__":
    unittest.main()
