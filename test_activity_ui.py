"""Selection may change while the bounded external observation read is in flight."""
import asyncio
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import patch
from uuid import uuid4

from textual.widgets import Static

from activity import Activity
from app import Waggle
from model import Board
from runtime import Runtime
from task_context import WorkContract
from test_activity import activity_record


class ActivitySelection(unittest.IsolatedAsyncioTestCase):
    async def test_observation_returning_after_window_shutdown_does_not_refresh_or_mutate(self):
        with TemporaryDirectory(prefix="waggle-activity-shutdown-") as directory:
            runtime = Runtime(Path(directory) / "bridges")
            board = Board(live=True)
            board.add_project("fixture", directory)
            task = board.create("closing", "fixture")
            board.assign(task, str(uuid4()))
            contract = WorkContract(
                purpose="implementation",
                deliverable="完成整任务交付",
                allowed_scope="项目目录",
                verification="回归测试通过",
                exclusions="不修改其他模块",
            )
            task.remaining = contract.compile()
            run = board.queue_real(task, str(uuid4()), str(uuid4()), directory)
            app = Waggle(board, runtime=runtime)
            app._polling = True
            entered, release = Event(), Event()
            pending = None

            def slow_read(expected):
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("fixture read was not released")
                return Activity.from_record(activity_record(expected, int(time.time() * 1000)), expected)

            try:
                async with app.run_test(size=(80, 24)):
                    await app.poll_runtime()  # Seed identical observation errors: exercise the status-only path.
                    app._polling = True
                    with patch.object(runtime, "read_activity", side_effect=slow_read):
                        pending = asyncio.create_task(app.poll_runtime())
                        self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                self.assertFalse(app.is_running)
            finally:
                release.set()
                if pending:
                    await pending
            self.assertIsNone(app.activity)
            self.assertEqual((board.runs[run.id].seq, board.runs[run.id].state), (0, "queued"))
            self.assertFalse(runtime.root.exists())

    async def test_late_observation_is_discarded_after_selecting_another_task(self):
        with TemporaryDirectory(prefix="waggle-activity-selection-") as directory:
            runtime = Runtime(Path(directory) / "bridges")
            board = Board(live=True)
            board.add_project("fixture", directory)
            first = board.create("first", "fixture")
            board.assign(first, str(uuid4()))
            contract = WorkContract(
                purpose="implementation",
                deliverable="完成整任务交付",
                allowed_scope="项目目录",
                verification="回归测试通过",
                exclusions="不修改其他模块",
            )
            first.remaining = contract.compile()
            board.queue_real(first, str(uuid4()), str(uuid4()), directory)
            second = board.create("second", "fixture")
            board.assign(second, str(uuid4()))
            entered, release = Event(), Event()

            def slow_read(run):
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("fixture read was not released")
                return Activity.from_record(activity_record(run, int(time.time() * 1000)), run)

            app = Waggle(board, runtime=runtime)
            app._polling = True  # Start the one controlled observation below, not an interval race.
            async with app.run_test(size=(80, 24)) as pilot:
                with patch.object(runtime, "read_activity", side_effect=slow_read):
                    pending = asyncio.create_task(app.poll_runtime())
                    try:
                        self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                        app.selected_id = second.id
                        app.refresh_tasks()
                    finally:
                        release.set()
                        await pending
                    await pilot.pause()
                self.assertEqual(app.selected_id, second.id)
                self.assertIsNone(app.activity)
                self.assertNotEqual(app.activity_run_id, first.run_id)
                self.assertIn("尚未派发", str(app.query_one("#execution-status", Static).content))
                self.assertNotIn("工具 read", str(app.query_one("#execution-status", Static).content))
                self.assertEqual(board.runs[first.run_id].seq, 0)
            self.assertFalse(runtime.root.exists())
