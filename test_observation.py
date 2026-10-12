"""Explicit per-run observation grants no command authority; fixtures only."""
import io
import os
import time
import unittest
from contextlib import redirect_stderr
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from textual.widgets import Static

from app import Waggle, main
from runtime import BridgeError, Runtime, private_dir, publish, read_json
from store import StorageError, Store
from task_context import WorkContract
from test_activity import activity_record
from test_runtime import prepared_board


def mailbox(root, run, *, idle=False):
    """An existing private source, deliberately without a commands directory."""
    private_dir(root, create=True)
    endpoint = root / run.agent_id
    private_dir(endpoint, create=True)
    private_dir(endpoint / "runs", create=True)
    publish(endpoint / "agent.json", {
        "version": 1, "id": run.agent_id, "session_id": run.session_id,
        "cwd": run.cwd, "name": "Fixture observer", "model": "offline", "pid": 1,
        "heartbeat": int(time.time() * 1000), "online": True, "idle": idle,
        "active_run": "" if idle else run.id,
    })
    publish(endpoint / "runs" / f"{run.id}.json", asdict(run))
    return endpoint


def files(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def replace_record(path, row):
    path.unlink(missing_ok=True)
    publish(path, row)


class ObservationTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="waggle-observation-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.native = self.root / "native"
        self.source = self.root / "original"
        self.board, self.task, self.attempt = prepared_board(str(self.root))
        self.latest = replace(self.attempt, seq=7, state="working", delivery="progress", summary="阶段核验 30/60", remaining="继续下一节点")
        self.endpoint = mailbox(self.source, self.latest)
        self.runtime = Runtime(self.native)

    def test_explicit_bound_source_restores_progress_without_fallback_or_writes(self):
        before = files(self.source)
        with self.assertRaises(FileNotFoundError):
            self.runtime.read_run(self.attempt)
        self.assertEqual(self.runtime.discover(), [])
        self.runtime.observe(self.attempt, self.source)
        self.assertTrue(self.runtime.observation_only(self.attempt))
        self.assertEqual(self.runtime.read_run(self.attempt), self.latest)
        agent = self.runtime.read_agent(self.attempt)
        self.assertEqual((agent.session_id, agent.cwd, agent.active_run), (self.attempt.session_id, self.attempt.cwd, self.attempt.id))
        self.assertTrue(agent.fresh)
        self.assertIsNone(self.runtime.read_activity(self.attempt))
        self.assertEqual(self.runtime.discover(), [])
        self.assertFalse(self.native.exists())
        self.assertEqual(files(self.source), before)
        self.assertFalse((self.endpoint / "commands").exists())

    def test_observation_rejects_every_command_before_any_io(self):
        self.runtime.observe(self.attempt, self.source)
        with patch("runtime.read_json") as read, patch("runtime.publish") as write:
            for action in (self.runtime.dispatch, self.runtime.stop,
                           lambda run: self.runtime._command(run, "stop")):
                with self.subTest(action=action), self.assertRaisesRegex(BridgeError, "只读"):
                    action(self.attempt)
            read.assert_not_called()
            write.assert_not_called()
        self.assertFalse(self.native.exists())
        self.assertFalse((self.endpoint / "commands").exists())

    def test_binding_pins_every_immutable_input_and_rejects_rebinding(self):
        for root in (Path("relative"), Path("")):
            with self.assertRaisesRegex(BridgeError, "绝对"):
                self.runtime.observe(self.attempt, root)
        with self.assertRaises(ValueError):
            self.runtime.observe(replace(self.attempt, id="../bad"), self.source)
        self.runtime.observe(self.attempt, self.source)
        with self.assertRaisesRegex(BridgeError, "重复"):
            self.runtime.observe(self.attempt, self.native)
        for field, value in (("task_id", 99), ("agent_id", str(uuid4())), ("session_id", str(uuid4())),
                             ("cwd", "/another"), ("title", "other"), ("description", "other"), ("requirements", "other")):
            changed = replace(self.attempt, **{field: value})
            with self.subTest(field=field), patch("runtime.read_json") as read:
                for action in (self.runtime.read_run, self.runtime.read_agent, self.runtime.read_activity):
                    with self.assertRaisesRegex(BridgeError, "身份已改变"):
                        action(changed)
                read.assert_not_called()

    def test_missing_source_never_falls_back_and_other_runs_keep_native_routing(self):
        native_endpoint = mailbox(self.native, replace(self.latest, seq=99, summary="wrong source"), idle=True)
        publish(native_endpoint / "activity.json", activity_record(self.attempt, int(time.time() * 1000)))
        self.runtime.observe(self.attempt, self.source)
        self.assertEqual(self.runtime.read_run(self.attempt), self.latest)
        self.assertIsNone(self.runtime.read_activity(self.attempt), "Optional activity must not fall back to the native mailbox.")
        (self.endpoint / "runs" / f"{self.attempt.id}.json").unlink()
        (self.endpoint / "agent.json").unlink()
        self.assertIsNone(self.runtime.read_run(self.attempt))
        with self.assertRaises(FileNotFoundError):
            self.runtime.read_agent(self.attempt)
        other = replace(self.attempt, id=str(uuid4()), task_id=99)
        publish(native_endpoint / "runs" / f"{other.id}.json", asdict(other))
        self.assertFalse(self.runtime.observation_only(other))
        self.assertEqual(self.runtime.read_run(other), other)
        self.assertTrue(self.runtime.read_agent(other).available)
        self.assertEqual(self.runtime.discover(), [self.runtime.agent(other.agent_id)])
        private_dir(native_endpoint / "commands", create=True)
        self.runtime.dispatch(other)
        self.assertEqual(len(list((native_endpoint / "commands").iterdir())), 1)
        self.assertFalse((self.endpoint / "commands").exists())

    def test_source_agent_and_report_must_match_the_original_identity(self):
        self.runtime.observe(self.attempt, self.source)
        metadata_file = self.endpoint / "agent.json"
        metadata = read_json(metadata_file)
        for key, value in (("id", str(uuid4())), ("session_id", str(uuid4())), ("cwd", "/another")):
            with self.subTest(agent_field=key):
                replace_record(metadata_file, {**metadata, key: value})
                with self.assertRaisesRegex(BridgeError, "身份不一致"):
                    self.runtime.read_agent(self.attempt)
        replace_record(metadata_file, metadata)
        result_file = self.endpoint / "runs" / f"{self.attempt.id}.json"
        for key, value in (("id", str(uuid4())), ("task_id", 99), ("agent_id", str(uuid4())),
                           ("session_id", str(uuid4())), ("cwd", "/another"), ("title", "other"),
                           ("description", "other"), ("requirements", "other")):
            with self.subTest(report_field=key):
                replace_record(result_file, {**asdict(self.latest), key: value})
                with self.assertRaisesRegex(BridgeError, "身份不匹配"):
                    self.runtime.read_run(self.attempt)

    def test_read_only_routing_preserves_private_directory_and_file_guards(self):
        alias = self.root / "alias"
        alias.symlink_to(self.source, target_is_directory=True)
        self.runtime.observe(self.attempt, alias)
        for action in (self.runtime.read_run, self.runtime.read_agent, self.runtime.read_activity):
            with self.assertRaises(BridgeError):
                action(self.attempt)
        runtime = Runtime(self.native)
        runtime.observe(self.attempt, self.source)
        self.source.chmod(0o755)
        with self.assertRaises(BridgeError):
            runtime.read_run(self.attempt)
        self.source.chmod(0o700)
        (self.endpoint / "agent.json").chmod(0o644)
        with self.assertRaises(BridgeError):
            runtime.read_agent(self.attempt)
        self.assertFalse(self.native.exists())

    def test_optional_activity_uses_only_the_pinned_source_and_validates_binding(self):
        self.runtime.observe(self.attempt, self.source)
        path = self.endpoint / "activity.json"
        record = activity_record(self.attempt, int(time.time() * 1000))
        publish(path, record)
        snapshot = self.runtime.read_activity(self.attempt)
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.run_id if snapshot else None, self.attempt.id)
        replace_record(path, {**record, "run_id": str(uuid4())})
        with self.assertRaises(ValueError):
            self.runtime.read_activity(self.attempt)

    def test_cli_is_explicit_ephemeral_and_rejects_demo_duplicates_and_unknown_runs(self):
        db = self.root / "tasks.sqlite3"
        with Store(db) as store:
            store.load()
            store.save(self.board)
        before = db.read_bytes()
        argv = ["--db", str(db), "--observe-run", self.attempt.id, str(self.source)]

        def inspect(app):
            self.assertIsNotNone(app.runtime)
            if app.runtime:
                self.assertTrue(app.runtime.observation_only(app.board.runs[self.attempt.id]))
                self.assertEqual(app.runtime.read_run(app.board.runs[self.attempt.id]), self.latest)
                self.assertEqual(app.runtime.root, self.native)

        with patch.dict(os.environ, {"WAGGLE_BRIDGE_DIR": str(self.native)}):
            with patch.object(Waggle, "run", autospec=True, side_effect=inspect) as launch:
                self.assertEqual(main(argv), 0)
                launch.assert_called_once()
            with Store(db) as store:
                reopened = Waggle(store=store)
                self.assertFalse(reopened.runtime.observation_only(self.attempt) if reopened.runtime else True)
            for args in (["--demo", "--observe-run", self.attempt.id, str(self.source)],
                         argv + ["--observe-run", self.attempt.id, str(self.native)]):
                with self.subTest(args=args), patch("app.Store") as opened, redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as error:
                        main(args)
                    self.assertEqual(error.exception.code, 2)
                    opened.assert_not_called()
            for run_id, directory in ((str(uuid4()), str(self.source)), (self.attempt.id, "relative")):
                with patch.object(Waggle, "run") as launch, redirect_stderr(io.StringIO()):
                    self.assertEqual(main(["--db", str(db), "--observe-run", run_id, directory]), 1)
                    launch.assert_not_called()
        self.assertEqual(db.read_bytes(), before)
        self.assertFalse(self.native.exists())

    def test_only_the_current_database_round_can_be_registered(self):
        self.board.ingest(replace(self.attempt, seq=1, state="interrupted"))
        self.board.return_work(self.task, "已核实停止，稍后继续")
        with self.assertRaisesRegex(BridgeError, "当前 Run"):
            Waggle(self.board, runtime=self.runtime, observations={self.attempt.id: self.source})
        contract = WorkContract(
            purpose="implementation",
            deliverable="完成整任务交付",
            allowed_scope="项目目录",
            verification="回归测试通过",
            exclusions="不修改其他模块",
        )
        self.task.remaining = contract.compile()
        self.board.queue_real(self.task, str(uuid4()), self.attempt.session_id, self.attempt.cwd)
        with self.assertRaisesRegex(BridgeError, "当前 Run"):
            Waggle(self.board, runtime=self.runtime, observations={self.attempt.id: self.source})
        self.assertFalse(self.runtime.observation_only(self.attempt))


class ObservationUI(unittest.IsolatedAsyncioTestCase):
    async def test_poll_persists_only_progress_and_never_converts_observation_into_control(self):
        with TemporaryDirectory(prefix="waggle-observation-ui-") as directory:
            root = Path(directory)
            board, task, run = prepared_board(directory)
            latest = replace(run, seq=7, state="working", delivery="progress", summary="阶段核验 30/60", remaining="还有工作")
            source, runtime = root / "original", Runtime(root / "native")
            endpoint = mailbox(source, latest)
            source_before = files(source)
            with Store(root / "tasks.sqlite3") as store:
                store.load()
                store.save(board)
                app = Waggle(store=store, runtime=runtime, observations={run.id: source})
                app._polling = True
                with patch.object(app, "schedule_poll"), patch.object(runtime, "_command") as command:
                    async with app.run_test(size=(120, 36)):
                        await app.poll_runtime()
                        self.assertEqual(store.load().runs[run.id], latest)
                        self.assertEqual(app.board.tasks[task.id].summary, latest.summary)
                        self.assertFalse(app.board.tasks[task.id].can_accept)
                        status = str(app.query_one("#execution-status", Static).content)
                        self.assertIn("连接正常", status)
                        self.assertIn("本轮只读", status)
                        self.assertIn("当前步骤未上报", status)
                        before, revision = deepcopy(app.board.tasks), store.revision
                        with patch.object(app, "push_screen") as screen, patch.object(runtime, "stop") as stop:
                            app.action_stop()
                            await app.stop_run(app.board.runs[run.id])
                            screen.assert_not_called()
                            stop.assert_not_called()
                        self.assertEqual(app.board.tasks, before)
                        self.assertEqual(store.revision, revision)
                        with patch.object(app, "push_screen") as screen:
                            app.action_palette()
                            self.assertNotIn("stop", screen.call_args.args[0].options)
                        # Stale heartbeats cannot turn a last-known report into stopped/completed.
                        metadata = read_json(endpoint / "agent.json")
                        replace_record(endpoint / "agent.json", {**metadata, "heartbeat": 0})
                        await app.poll_runtime()
                        status = str(app.query_one("#execution-status", Static).content)
                        self.assertNotIn("连接正常", status)
                        self.assertIn("核实", status)
                        self.assertEqual(app.board.runs[run.id].state, "working")
                        self.assertEqual(store.revision, revision)
                        # Identity-matching final reports can arrive after liveness is lost.
                        final = replace(latest, seq=8, state="finished", delivery="full", remaining="", checks=["自报核验"])
                        replace_record(endpoint / "runs" / f"{run.id}.json", asdict(final))
                        await app.poll_runtime()
                        self.assertEqual(store.load().runs[run.id], final)
                        self.assertEqual(app.board.tasks[task.id].status, "review")
                        self.assertTrue(app.board.tasks[task.id].can_accept)
                        self.assertNotEqual(app.board.tasks[task.id].status, "done")
                        with patch.object(runtime, "read_run") as read:
                            await app.poll_runtime()
                            read.assert_not_called()
                        command.assert_not_called()
            self.assertFalse(runtime.root.exists())
            self.assertFalse((endpoint / "commands").exists())
            # Only the two deliberately replaced fixture files changed.
            after = files(source)
            self.assertEqual(set(after), set(source_before))
            for name in source_before:
                if name not in (f"{run.agent_id}/agent.json", f"{run.agent_id}/runs/{run.id}.json"):
                    self.assertEqual(after[name], source_before[name])

    async def test_failed_save_keeps_the_original_revision_and_retries_only_reads(self):
        with TemporaryDirectory(prefix="waggle-observation-save-") as directory:
            root = Path(directory)
            board, _, run = prepared_board(directory)
            latest = replace(run, seq=3, state="working", summary="新进展")
            source, runtime = root / "original", Runtime(root / "native")
            mailbox(source, latest)
            before = files(source)
            with Store(root / "tasks.sqlite3") as store:
                store.load()
                store.save(board)
                app = Waggle(store=store, runtime=runtime, observations={run.id: source})
                app._polling = True
                with patch.object(app, "schedule_poll"), patch.object(runtime, "_command") as command:
                    async with app.run_test(size=(80, 24)):
                        revision = store.revision
                        with patch.object(store, "save", side_effect=StorageError("fixture save failure")):
                            await app.poll_runtime()
                        self.assertEqual(app.board.runs[run.id], run)
                        self.assertEqual(store.load().runs[run.id], run)
                        self.assertEqual(store.revision, revision)
                        await app.poll_runtime()
                        self.assertEqual(store.load().runs[run.id], latest)
                        command.assert_not_called()
            self.assertEqual(files(source), before)
            self.assertFalse(runtime.root.exists())


if __name__ == "__main__":
    unittest.main()
