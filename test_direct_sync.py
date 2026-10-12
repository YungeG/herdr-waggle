"""Pi-side work remains observable without replaying input or rewriting old attempts."""

import unittest
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from textual.widgets import Static

from app import Waggle
from delivery import detail_text
from journal import encoded_event
from report_reader import ReportReader
from runtime import BridgeError, Runtime, publish
from store import StorageError, Store, decode, encode
from test_continuations import child_run, publish_child, session_record
from test_observation import files, mailbox, replace_record
from test_runtime import prepared_board


class RecoveryGuard(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory(prefix="waggle-recovery-guard-"))
        self.board, self.task, queued = prepared_board(directory)
        self.attempt = replace(queued, state="working", seq=1)
        self.board.ingest(self.attempt)
        self.runtime = Runtime(Path(directory) / "bridges")
        self.endpoint = mailbox(self.runtime.root, self.attempt)
        self.stale = session_record(self.attempt, observed_at=1, state="working", run_id=self.attempt.id)
        publish(self.endpoint / "session.json", self.stale)

    def test_persisted_human_abandonment_releases_old_observation_not_command_authority(self):
        self.runtime.observe(self.attempt, self.runtime.root)
        self.board.abandon(self.task, "已核实旧 Pi 与子进程停止")
        board = decode(encode(self.board))
        task, run = board.tasks[self.task.id], board.runs[self.attempt.id]
        app = Waggle(board, runtime=self.runtime)
        before = encode(board)
        path = self.endpoint / "session.json"
        for row in (self.stale, {"version": 99}, {**self.stale, "session_id": str(uuid4())}, None):
            with self.subTest(row=row), patch.object(self.runtime, "check_current", wraps=self.runtime.check_current) as check:
                path.unlink(missing_ok=True)
                if row is not None:
                    publish(path, row)
                app.check_terminal_task(board, task)
                check.assert_not_called()
                self.assertEqual(encode(board), before)
                with self.assertRaises(ValueError):
                    board.accept(task)
                with self.assertRaisesRegex(BridgeError, "只读"):
                    self.runtime.require_control(run)

    def test_abandoned_flag_needs_matching_local_human_record(self):
        self.attempt.state = "abandoned"
        self.task.status, self.task.run = "blocked", "finished"
        for events in ([], ["人工确认旧 Pi 已停止 · 无轮次身份"],
                       [encoded_event("abandoned", "other run", source="human", run_id=str(uuid4()), seq=1)],
                       [encoded_event("abandoned", "simulation", source="simulation", run_id=self.attempt.id, seq=1)],
                       [encoded_event("abandoned", "old revision", source="human", run_id=self.attempt.id, seq=0)]):
            with self.subTest(events=events):
                self.task.events = events
                app = Waggle(self.board, runtime=self.runtime)
                with self.assertRaisesRegex(BridgeError, "过期"):
                    app.check_terminal_task(self.board, self.task)

    def test_normal_terminal_states_still_recheck_stale_sessions(self):
        self.board.abandon(self.task, "fixture human record")
        for state in ("finished", "failed", "stopped", "interrupted"):
            with self.subTest(state=state):
                self.attempt.state = state
                app = Waggle(self.board, runtime=self.runtime)
                with self.assertRaisesRegex(BridgeError, "过期"):
                    app.check_terminal_task(self.board, self.task)


class DirectSyncUI(unittest.IsolatedAsyncioTestCase):
    async def test_failed_round_keeps_observing_executor_without_changing_history(self):
        with TemporaryDirectory(prefix="waggle-direct-sync-") as directory:
            root = Path(directory)
            board, task, run = prepared_board(directory)
            failed = replace(run, seq=1, state="failed", summary="Pi 响应失败：fixture transport error")
            board.ingest(failed)
            runtime, source = Runtime(root / "native"), root / "source"
            mailbox(source, failed, idle=True)
            source_before = files(source)
            with Store(root / "tasks.sqlite3") as store:
                store.load()
                store.save(board)
                app = Waggle(store=store, runtime=runtime, observations={run.id: source})
                before = deepcopy((app.board.tasks, app.board.runs))
                revision = store.revision
                with patch.object(app, "schedule_poll"), patch.object(runtime, "read_agent", wraps=runtime.read_agent) as read:
                    async with app.run_test(size=(120, 36)):
                        await app.poll_runtime()
                        read.assert_called_once_with(failed)
                        text = str(app.query_one("#execution-status", Static).content)
                        self.assertIn("执行失败", text)
                        self.assertIn("空闲", text)
                        self.assertEqual(app.board.tasks[task.id].status, "blocked")
                        self.assertEqual((app.board.tasks, app.board.runs), before)
                        self.assertEqual(store.revision, revision)
            self.assertEqual(files(source), source_before)
            self.assertFalse(runtime.root.exists())


class ContinuationUI(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = TemporaryDirectory(prefix="waggle-follow-ui-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.board, self.task, queued = prepared_board(str(self.root))
        self.parent = replace(queued, seq=1, state="finished", delivery="full", summary="original report", checks=["original proof"])
        self.board.ingest(self.parent)
        self.child = child_run(self.parent)
        self.source = self.root / "source"
        self.endpoint = mailbox(self.source, self.parent, idle=True)
        publish_child(self.endpoint, self.parent, self.child)
        self.runtime = Runtime(self.root / "native")
        self.store = Store(self.root / "tasks.sqlite3")
        self.addCleanup(self.store.close)
        self.store.load()
        self.store.save(self.board)

    def app(self):
        return Waggle(store=self.store, runtime=self.runtime, task_observations={self.parent.id: self.source})

    async def test_save_failure_does_not_adopt_or_grant_routes_then_retry_only_reads(self):
        app = self.app()
        before = files(self.source)
        revision = self.store.revision
        with patch.object(app, "schedule_poll"), patch.object(self.runtime, "_command") as command:
            async with app.run_test(size=(120, 36)):
                with patch.object(self.store, "save", side_effect=StorageError("fixture save failed")):
                    await app.poll_runtime()
                self.assertEqual(app.board.tasks[self.task.id].run_id, self.parent.id)
                self.assertNotIn(self.child.id, self.store.load().runs)
                self.assertEqual(self.store.revision, revision)
                self.assertFalse(self.runtime.observation_only(self.child))
                await app.poll_runtime()
                self.assertEqual(app.board.tasks[self.task.id].run_id, self.child.id)
                self.assertTrue(self.runtime.observation_only(self.child))
                self.assertEqual(self.store.load().runs[self.parent.id], self.parent)
                saved_revision = self.store.revision
                await app.poll_runtime()
                self.assertEqual(self.store.revision, saved_revision)
                command.assert_not_called()
        self.assertEqual(files(self.source), before)
        self.assertFalse(self.runtime.root.exists())

    async def test_acceptance_rechecks_unseen_child_and_rejects_a_changed_modal_snapshot(self):
        app = self.app()
        with patch.object(app, "schedule_poll"):
            async with app.run_test(size=(120, 36)):
                with patch.object(app, "push_screen") as screen:
                    app.action_review()
                    confirm_old = screen.call_args.args[1]
                confirm_old(True)
                self.assertEqual(app.board.tasks[self.task.id].status, "review", "A child not yet ingested must block acceptance of the old report")
                await app.poll_runtime()
                final = replace(self.child, seq=4, state="finished", delivery="full", summary="new report", checks=["new proof"])
                replace_record(self.endpoint / "runs" / f"{self.child.id}.json", asdict(final))
                publish(self.endpoint / "session.json", session_record(final))
                await app.poll_runtime()
                self.assertTrue(app.board.tasks[self.task.id].can_accept)
                confirm_old(True)
                self.assertEqual(app.board.tasks[self.task.id].status, "review", "A modal must not accept a report the human never read")
                with patch.object(app, "push_screen") as screen:
                    app.action_review()
                    screen.call_args.args[1](True)
                self.assertEqual(app.board.tasks[self.task.id].status, "done")
                with patch.object(self.runtime, "read_updates") as read:
                    await app.poll_runtime()
                    read.assert_not_called()

    async def test_live_observation_failure_does_not_change_a_terminal_report(self):
        runtime = Runtime(self.root / "unavailable-mailbox")
        app = Waggle(store=self.store, runtime=runtime)
        before = self.store.path.read_bytes()
        with patch.object(app, "schedule_poll"):
            async with app.run_test(size=(80, 24)) as pilot:
                await app.poll_runtime()
                self.assertIn(self.parent.id, app.runtime_errors)
                state = str(app.query_one("#execution-status", Static).content)
                self.assertIn("Pi 当前", state)
                self.assertIn("连接未知", state)
                app.action_read_report()
                await pilot.pause()
                reader = app.screen
                assert isinstance(reader, ReportReader)
                self.assertEqual(reader.body, detail_text(app.board.tasks[self.task.id], self.parent).plain)
                self.assertEqual(app.board.runs[self.parent.id], self.parent)
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertFalse(runtime.root.exists())

    async def test_exact_run_observer_keeps_history_without_silently_following_new_work(self):
        app = Waggle(store=self.store, runtime=self.runtime, observations={self.parent.id: self.source})
        with patch.object(app, "schedule_poll"):
            async with app.run_test(size=(120, 36)):
                await app.poll_runtime()
                self.assertNotIn(self.child.id, app.board.runs)
                self.assertEqual(app.board.tasks[self.task.id].run_id, self.parent.id)
                self.assertIn("不跟随后续轮次", str(app.query_one("#execution-status", Static).content))
                self.assertFalse(self.runtime.observation_only(self.child))


if __name__ == "__main__":
    unittest.main()
