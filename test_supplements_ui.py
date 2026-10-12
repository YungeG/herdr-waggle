"""The same b entry saves locally or sends explicitly, never guesses the mode."""
import asyncio
import json
import time
import unittest
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import patch
from uuid import uuid4

from textual.widgets import Button, Input, Static, TextArea, Tree

from app import Confirm, Note, Waggle
from decisions import review_requirements
from journal import Report, encoded_event
from runtime import Runtime, private_dir, publish
from store import Store
from test_live import wait_ui


class SupplementsUI(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = TemporaryDirectory(prefix="waggle-note-ui-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = Store(self.root / "tasks.sqlite3")
        self.addCleanup(self.store.close)
        self.board = self.store.load()
        self.board.add_project("fixture", str(self.root))
        self.task = self.board.create("追加说明", "fixture")
        self.runtime = Runtime(self.root / "bridges")

    def live(self):
        self.task.remaining = review_requirements("核对当前隔离任务", str(self.root), "原件与补充回执可核对")
        self.board.assign(self.task, str(uuid4()))
        run = self.board.queue_real(self.task, str(uuid4()), str(uuid4()), str(self.root))
        incoming = deepcopy(run)
        incoming.seq = 1
        incoming.state = "working"
        self.board.ingest(incoming)
        endpoint = self.runtime.root / run.agent_id
        for directory in (self.runtime.root, endpoint, endpoint / "runs", endpoint / "supplements", endpoint / "supplements/requests", endpoint / "supplements/receipts"):
            private_dir(directory, create=True)
        publish(endpoint / "runs" / f"{run.id}.json", asdict(incoming))
        publish(endpoint / "agent.json", {"version": 1, "id": run.agent_id, "session_id": run.session_id, "cwd": run.cwd, "name": "offline", "model": "fixture", "pid": 1,
                "heartbeat": int(time.time() * 1000), "online": True, "idle": False, "active_run": run.id})
        publish(endpoint / "capabilities.json", {"version": 1, "agent_id": run.agent_id, "session_id": run.session_id, "cwd": run.cwd, "supplement": 1})
        return endpoint

    async def test_pending_b_saves_requirements_without_execution_at_two_widths(self):
        for size in ((80, 24), (120, 30)):
            self.store.save(self.board)
            app = Waggle(store=self.store, runtime=self.runtime)
            async with app.run_test(size=size) as pilot:
                self.assertEqual(app.query_one("#accept-button", Button).label, "选择执行者 a")
                self.assertTrue(app.query_one("#return-button", Button).disabled)
                await pilot.press("b")
                self.assertIsInstance(app.screen, Note)
                app.screen.query_one(TextArea).text = "未派发的补充要求"
                app.screen.query_one("#review-scope", Input).value = str(self.root)
                app.screen.query_one("#review-verification", Input).value = "核对保存后的完整要求"
                await pilot.pause()
                await pilot.click("#save-only")
                self.assertEqual(app.board.tasks[self.task.id].remaining,
                                 review_requirements("未派发的补充要求", str(self.root), "核对保存后的完整要求"))
                self.assertEqual(app.board.tasks[self.task.id].run, "none")
                self.assertEqual(app.board.runs, {})
                self.assertFalse(self.runtime.root.exists())
            self.board = self.store.load()

    async def test_live_confirmation_cancel_and_uncertain_publish_keep_one_durable_intent(self):
        endpoint = self.live()
        self.store.save(self.board)
        app = Waggle(store=self.store, runtime=self.runtime)
        async with app.run_test(size=(80, 24)) as pilot:
            await app.poll_runtime()
            await pilot.press("b")
            await pilot.pause()
            self.assertIsInstance(app.screen, Note)
            form = app.screen
            form.query_one(TextArea).text = "本轮补充，不是下一轮"
            await pilot.press("ctrl+enter")
            self.assertIsInstance(app.screen, Confirm)
            assert app.focused
            self.assertEqual(app.focused.id, "cancel")
            await pilot.press("enter")
            self.assertIs(app.screen, form)
            self.assertEqual(app.board.supplements, {})
            with patch.object(self.runtime, "send_supplement", side_effect=OSError("uncertain write")):
                await pilot.press("ctrl+enter", "ctrl+enter")
                await pilot.pause()
            self.assertEqual(len(app.board.supplements), 1)
            self.assertEqual(self.store.load().supplements, app.board.supplements)
            self.assertEqual(len(app.screen_stack), 1)
            self.assertEqual(app.board.tasks[self.task.id].run, "working")
            await pilot.press("b")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, Note)
            self.assertIn("补充", str(app.query_one("#execution-status", Static).content))
            self.assertEqual(list((endpoint / "supplements/requests").iterdir()), [])

    async def test_coverage_receipt_arriving_after_terminal_run_unlocks_only_full_review(self):
        endpoint = self.live()
        self.store.save(self.board)
        app = Waggle(store=self.store, runtime=self.runtime)
        async with app.run_test(size=(120, 30)) as pilot:
            await app.poll_runtime()
            await pilot.press("b")
            await pilot.pause()
            assert isinstance(app.screen, Note)
            app.screen.query_one(TextArea).text = "本轮需要覆盖"
            await pilot.press("ctrl+enter", "ctrl+enter")
            await pilot.pause()
            note = next(iter(app.board.supplements.values()))
            request = json.loads((endpoint / "supplements/requests" / f"{note.id}.json").read_text())
            run = deepcopy(app.board.runs[note.run_id])
            run.seq, run.state, run.delivery = 10, "finished", "full"
            run.summary, run.checks, run.remaining = "自报完成并覆盖补充", ["自报依据"], ""
            run.events.extend([
                encoded_event("full", "完整报告", source="executor", run_id=run.id, seq=9,
                              report=Report(run.summary, run.checks, run.remaining)),
                encoded_event("settled", "本轮已结束", source="system", run_id=run.id, seq=10),
            ])
            file = endpoint / "runs" / f"{run.id}.json"
            file.unlink()
            publish(file, asdict(run))
            await app.poll_runtime()
            self.assertEqual(asdict(app.board.runs[run.id]), asdict(run))
            self.assertNotEqual(app.query_one("#accept-button", Button).label, "核对交付 r")
            self.assertIn("没有可核验的完整交付", str(app.query_one("#detail-text", Static).content))
            self.assertIn("执行者自报报告已到达", str(app.query_one("#submission-status", Static).content))
            self.assertIn("尚未人工验收", str(app.query_one("#submission-status", Static).content))
            self.assertNotIn("阶段交付", str(app.query_one("#detail-text", Static).content))
            publish(endpoint / "supplements/receipts" / f"{note.id}.json", {"version": 1, "request": request, "status": "covered", "seq": 3, "run_seq": 9, "detail": "报告覆盖"})
            await app.poll_runtime()
            self.assertEqual(app.query_one("#accept-button", Button).label, "核对交付 r")
            self.assertEqual(app.board.tasks[self.task.id].status, "review")
            self.assertEqual(self.store.load().supplements[note.id].status, "covered")
            app.board.tasks[self.task.id].events = []  # Simulate pruning; the durable instruction must remain inspectable.
            app.action_toggle_detail()
            await pilot.pause()
            self.assertIn(note.text, str(app.query_one("#detail-text", Static).content))
            app.action_activity()
            await pilot.pause()
            tree = app.screen.query_one("#records-tree", Tree)
            tree.select_node(tree.root.children[0])
            await pilot.pause()
            self.assertIn(note.text, str(app.screen.query_one("#record-body", Static).content))
            await pilot.press("escape")
            app.action_review()
            await pilot.pause()
            self.assertIsInstance(app.screen, Confirm)
            assert isinstance(app.screen, Confirm)
            self.assertIn(note.text, app.screen.body)
            await pilot.press("escape")

    async def test_queued_old_bridge_and_readonly_do_not_save_or_send(self):
        endpoint = self.live()
        self.store.save(self.board)
        (endpoint / "capabilities.json").unlink()
        app = Waggle(store=self.store, runtime=self.runtime)
        async with app.run_test(size=(120, 30)) as pilot:
            await app.poll_runtime()
            await pilot.press("b")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, Note)
            self.assertEqual(app.board.supplements, {})
            run = app.board.runs[self.task.run_id]
            run.state = "queued"
            app.action_return_work()
            await pilot.pause()
            self.assertNotIsInstance(app.screen, Note)
            run.state = "working"
            self.runtime.observe(run, self.runtime.root)
            app.action_return_work()
            await pilot.pause()
            self.assertNotIsInstance(app.screen, Note)
            self.assertEqual(app.board.supplements, {})

    async def test_completed_then_purged_task_during_preflight_is_not_adopted_or_crashed(self):
        self.live()
        self.store.save(self.board)
        app = Waggle(store=self.store, runtime=self.runtime)
        entered, release = Event(), Event()
        check = self.runtime.check_supplement
        def delayed_check(run):
            check(run)
            entered.set()
            if not release.wait(5):
                raise TimeoutError("fixture preflight not released")
        async with app.run_test(size=(80, 24)) as pilot:
            await app.poll_runtime()
            app._polling = True
            with patch.object(self.runtime, "check_supplement", side_effect=delayed_check):
                pending = asyncio.create_task(app.open_live_supplement(self.task.id))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                    ended = deepcopy(app.board.runs[self.task.run_id])
                    ended.seq, ended.state, ended.summary = 2, "failed", "fixture ended"
                    app.apply_change(lambda board: board.ingest(ended))
                    app.apply_change(lambda board: board.retire(board.tasks[self.task.id], "trashed"))
                    app.apply_change(lambda board: board.purge(board.tasks[self.task.id]))
                    app.refresh_tasks()
                finally:
                    release.set()
                    await pending
            await pilot.pause()
            self.assertNotIsInstance(app.screen, Note)
            self.assertEqual(app.board.supplements, {})
            self.assertNotIn(self.task.id, self.store.load().tasks)

    async def test_local_failure_keeps_note_text(self):
        self.store.save(self.board)
        app = Waggle(store=self.store, runtime=self.runtime)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("b")
            self.assertIsInstance(app.screen, Note)
            app.screen.query_one(TextArea).text = "不能丢"
            app.screen.query_one("#review-scope", Input).value = str(self.root)
            app.screen.query_one("#review-verification", Input).value = "核对保存后的完整要求"
            await pilot.pause()
            self.store.connection.execute("PRAGMA query_only=ON")
            await pilot.click("#save-only")
            self.assertIsInstance(app.screen, Note)
            self.assertEqual(app.screen.query_one(TextArea).text, "不能丢")
            self.assertIn("未保存", str(app.screen.query_one("#form-error", Static).content))
            self.assertFalse(app.screen.query_one("#note-stage", Static).display)
            self.store.connection.execute("PRAGMA query_only=OFF")
            await wait_ui(pilot, lambda: not app.screen.query_one("#save-only", Button).has_class("-active"))
            await pilot.click("#save-only")
            self.assertEqual(app.board.tasks[self.task.id].remaining,
                             review_requirements("不能丢", str(self.root), "核对保存后的完整要求"))
