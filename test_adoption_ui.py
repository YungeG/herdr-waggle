"""Real Textual/SQLite attachment flow, with disposable declared mailboxes only."""
import json
import time
import unittest
from copy import deepcopy
from dataclasses import asdict, replace
from uuid import uuid4

from textual.widgets import Button, Checkbox, Input, Select, TextArea

from app import AdoptionForm, Confirm, Waggle
from decision_view import DecisionReview, DecisionSheet
from decisions import DecisionSpec
from journal import Report, encoded_event
from model import Run
from runtime import BridgeError
from session_adoption import latest_adoption
from store import Store, encode
from test_decisions import spec_record
from test_live import wait_ui
from test_session_adoption import SessionAdoptionTests


class AdoptionUI(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = SessionAdoptionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store = self.enterContext(Store(self.fixture.root / "tasks.sqlite3"))
        self.store.load()
        self.store.save(self.fixture.board)

    async def open(self, app, pilot):
        app.selected_id = self.fixture.task.id
        app.detail_open = True
        app.refresh_tasks()
        await app.adoption_form(self.fixture.current, self.fixture.task.project, str(self.fixture.project))
        await wait_ui(pilot, lambda: isinstance(app.screen, AdoptionForm))
        return app.screen

    async def flow(self, size):
        app = Waggle(store=self.store, runtime=self.fixture.runtime)
        async with app.run_test(size=size) as pilot:
            form = await self.open(app, pilot)
            self.assertEqual(form.query_one("#adopt-task", Select).value, str(self.fixture.task.id))
            self.assertTrue(form.query_one("#adopt-title", Input).disabled)
            self.assertTrue(form.query_one("#adopt-goal", TextArea).read_only)
            form.query_one("#adopt-checkpoint", TextArea).text = "Preserve reports; only follow subsequent input"
            form.query_one("#adopt-checked", Checkbox).value = True
            for button in form.query(".dialog-buttons Button").results(Button):
                self.assertTrue(form.query_one("#dialog").content_region.contains_region(button.region))
            before = deepcopy(asdict(app.board.runs[self.fixture.source.id]))
            await pilot.press("ctrl+enter")
            await wait_ui(pilot, lambda: isinstance(app.screen, Confirm))
            self.assertEqual(len(app.board.runs), 1)
            self.assertFalse(list((self.fixture.runtime.root / self.fixture.current.id / "adoptions" / "requests").iterdir()))
            await pilot.click("#submit")
            await wait_ui(pilot, lambda: len(app.screen_stack) == 1)
            task = app.board.tasks[self.fixture.task.id]
            intent = latest_adoption(task)
            assert intent is not None
            request = self.fixture.runtime._adoption_request(intent)
            self.fixture.acknowledge(request)
            await wait_ui(pilot, lambda: app.board.tasks[task.id].agent == self.fixture.current.id)
            self.assertEqual(asdict(app.board.runs[self.fixture.source.id]), before)
            self.assertEqual(len(app.board.runs), 1)
            self.assertIn("send_new_round", app.ownership_for(app.board.tasks[task.id], app.board.runs[self.fixture.source.id]).allowed_actions)
            self.assertEqual(app.primary_action().action, "return_work")
            self.assertFalse(list((self.fixture.runtime.root / self.fixture.current.id / "commands").iterdir()))
            self.assertEqual(encode(self.store.load()), encode(app.board))

    async def test_existing_task_same_session_attachment_at_40_columns(self):
        await self.flow((40, 24))

    async def test_existing_task_same_session_attachment_at_80_columns(self):
        await self.flow((80, 30))

    async def test_existing_task_same_session_attachment_at_wide_columns(self):
        await self.flow((140, 40))

    async def test_cancel_and_missing_confirmation_preserve_input_without_intent(self):
        app = Waggle(store=self.store, runtime=self.fixture.runtime)
        async with app.run_test(size=(80, 30)) as pilot:
            form = await self.open(app, pilot)
            form.query_one("#adopt-checkpoint", TextArea).text = "Keep this input"
            original = encode(app.board)
            await pilot.press("ctrl+enter")
            await pilot.pause()
            self.assertIs(app.screen, form)
            self.assertEqual(encode(app.board), original)
            self.assertEqual(form.query_one("#adopt-checkpoint", TextArea).text, "Keep this input")
            form.query_one("#adopt-checked", Checkbox).value = True
            await pilot.press("ctrl+enter")
            await wait_ui(pilot, lambda: isinstance(app.screen, Confirm))
            await pilot.press("escape")
            await wait_ui(pilot, lambda: app.screen is form)
            self.assertEqual(encode(app.board), original)
            self.assertEqual(form.query_one("#adopt-checkpoint", TextArea).text, "Keep this input")

    async def test_new_task_attachment_is_saved_but_not_dispatched(self):
        app = Waggle(store=self.store, runtime=self.fixture.runtime)
        async with app.run_test(size=(80, 30)) as pilot:
            form = await self.open(app, pilot)
            form.query_one("#adopt-task", Select).value = "new"
            await pilot.pause()
            form.query_one("#adopt-title", Input).value = "New independent goal"
            form.query_one("#adopt-goal", TextArea).text = "Only new explicit instructions, not past conversation"
            form.query_one("#adopt-checkpoint", TextArea).text = "Start here"
            await pilot.press("ctrl+enter")
            await wait_ui(pilot, lambda: isinstance(app.screen, Confirm))
            await pilot.click("#submit")
            await wait_ui(pilot, lambda: len(app.screen_stack) == 1)
            assert app.selected_id is not None
            new = app.board.tasks[app.selected_id]
            intent = latest_adoption(new)
            assert intent is not None
            self.assertIsNone(intent.source)
            self.assertFalse(intent.previous_checked)
            self.assertEqual((new.run_id, new.run, len(app.board.runs)), ("", "none", 1))
            self.assertFalse(list((self.fixture.runtime.root / self.fixture.current.id / "commands").iterdir()))

    def finished_successor(self, *, native=False, independent=False, persist=False):
        """Consume an attachment in a disposable mailbox, retaining its original source."""
        fixture, board = self.fixture, self.fixture.board
        if independent:
            fixture.task = board.create("New task", "fixture", "Only future explicit instructions")
        task = fixture.task
        task.remaining = fixture.source.requirements
        request = fixture.runtime.prepare_adoption(board, task, fixture.current, "Preserve prior evidence",
                                                    previous_checked=not independent)
        board.request_adoption(task, request.intent)
        fixture.runtime.publish_adoption(request)
        fixture.acknowledge(request)
        batch = fixture.runtime.read_adoptions(board)
        board.apply_adoption(request.intent, batch.receipts[task.id])
        if persist:
            self.store.save(board)
        events = []
        if native:
            run = Run(str(uuid4()), task.id, fixture.current.id, fixture.current.session_id,
                      fixture.current.cwd, task.title, task.description, request.requirements)
            events.append(encoded_event("adopted", json.dumps({
                "adoption_id": request.intent.id,
                "parent_run_id": request.intent.source.run_id if request.intent.source else "",
                "source_digest": request.intent.source.digest if request.intent.source else "",
            }), source="system", run_id=run.id, seq=1))
        else:
            board.save_supplement(task, request.requirements, "next")
            run = board.queue_real(task, str(uuid4()), fixture.current.session_id, fixture.current.cwd)
            if persist:
                self.store.save(board)  # Preserve real write-ahead ordering.
        spec = DecisionSpec.from_record(spec_record(missing=["负责人原始回执待核"]))
        report = Report("Only retained fixture evidence", ["No experiment"], "Owner evidence remains", decisions=(spec,))
        events.append(encoded_event("partial", "Bounded fixture report", source="executor", run_id=run.id,
                                    seq=len(events) + 1, report=report))
        events.append(encoded_event("settled", "Native session idle", source="system", run_id=run.id,
                                    seq=len(events) + 1))
        finished = replace(run, state="finished", seq=len(events), delivery="partial", summary=report.summary,
                           checks=report.checks, remaining=report.remaining, events=events)
        if native:
            board.ingest_adopted(request, finished)
        else:
            board.ingest(finished)
        endpoint = fixture.runtime.root / fixture.current.id
        fixture.write(endpoint / "runs" / f"{finished.id}.json", asdict(finished))
        receipt_file = endpoint / "adoptions" / "receipts" / f"{request.intent.id}.json"
        receipt = json.loads(receipt_file.read_text())
        receipt.update(seq=2, state="consumed", at=int(time.time() * 1000), run_id=finished.id)
        fixture.write(receipt_file, receipt)
        fixture.write_capability()
        session_file = endpoint / "session.json"
        session = json.loads(session_file.read_text())
        session.update(linked_run_id=finished.id, observed_at=int(time.time() * 1000))
        fixture.write(session_file, session)
        if persist:
            self.store.save(board)
        return board, task, finished

    async def test_consumed_attachment_allows_dispatched_successor_feedback_review_without_sending(self):
        board, task, run = self.finished_successor(persist=True)
        app = Waggle(store=self.store, runtime=self.fixture.runtime)
        before = encode(self.store.load())
        source_before = asdict(board.runs[self.fixture.source.id])
        async with app.run_test(size=(80, 30)) as pilot:
            app.selected_id = task.id
            app.detail_open = True
            app.refresh_tasks()
            app.check_new_round_task(app.board, app.board.tasks[task.id])
            app.action_decisions()
            await wait_ui(pilot, lambda: isinstance(app.screen, DecisionSheet))
            form = app.screen
            assert isinstance(form, DecisionSheet)
            form.query_one("#intent-0", Select).value = "feedback"
            form.change_intent(0)
            form.query_one("#comment-0", TextArea).text = "请负责人核对已有材料；不重复实验"
            await pilot.pause()
            form.query_one("#decision-continue", Button).scroll_visible(animate=False)
            await pilot.pause()
            self.assertTrue(await pilot.click("#decision-continue"))
            await wait_ui(pilot, lambda: isinstance(app.screen, DecisionReview))
            review = app.screen
            assert isinstance(review, DecisionReview)
            self.assertEqual(review.preview.source.run_id, run.id)
            self.assertEqual(review.preview.target.agent_id, self.fixture.current.id)
            self.assertIn("仅补证请求", review.preview.compiled_text)
            self.assertEqual(encode(self.store.load()), before)
            self.assertFalse(list(self.fixture.runtime.root.glob("*/commands/*.json")))
            await pilot.press("enter")  # Default is back, never submit.
            await wait_ui(pilot, lambda: app.screen is form)
            self.assertEqual(form.query_one("#comment-0", TextArea).text, "请负责人核对已有材料；不重复实验")
        self.assertEqual(encode(self.store.load()), before)
        self.assertEqual(asdict(board.runs[self.fixture.source.id]), source_before)

    async def test_consumed_attachment_allows_native_successor_of_existing_task(self):
        board, task, _ = self.finished_successor(native=True)
        app = Waggle(board, runtime=self.fixture.runtime)
        before = encode(board)
        app.check_new_round_task(board, task)
        self.assertEqual(encode(board), before)
        self.assertFalse(list(self.fixture.runtime.root.glob("*/commands/*.json")))

    async def test_consumed_attachment_allows_native_first_round_of_independent_task(self):
        board, task, _ = self.finished_successor(native=True, independent=True)
        app = Waggle(board, runtime=self.fixture.runtime)
        before = encode(board)
        app.check_new_round_task(board, task)
        self.assertEqual(encode(board), before)
        self.assertFalse(list(self.fixture.runtime.root.glob("*/commands/*.json")))

    async def test_consumed_attachment_keeps_native_activity_and_ownership_guards(self):
        board, task, run = self.finished_successor()
        app = Waggle(board, runtime=self.fixture.runtime)
        endpoint = self.fixture.runtime.root / self.fixture.current.id
        original_agent = json.loads((endpoint / "agent.json").read_text())
        original_session = json.loads((endpoint / "session.json").read_text())
        before = encode(board)
        for changes, pattern in (
            ({"state": "working"}, "当前活动|新输入"),
            ({"input_pending": True}, "新输入"),
            ({"linked_run_id": str(uuid4())}, "非空闲"),
            ({"session_id": str(uuid4())}, "身份"),
        ):
            with self.subTest(changes=changes):
                self.fixture.write(endpoint / "session.json", original_session | changes)
                with self.assertRaisesRegex(BridgeError, pattern):
                    app.check_new_round_task(board, task)
        self.fixture.write(endpoint / "session.json", original_session)
        for changes in ({"online": False}, {"idle": False}, {"active_run": str(uuid4())}):
            with self.subTest(changes=changes):
                self.fixture.write(endpoint / "agent.json", original_agent | changes)
                with self.assertRaisesRegex(BridgeError, "非空闲"):
                    app.check_new_round_task(board, task)
        self.fixture.write(endpoint / "agent.json", original_agent)
        self.fixture.runtime.observe(run, self.fixture.runtime.root)
        with self.assertRaisesRegex(BridgeError, "只读"):
            app.check_new_round_task(board, task)
        self.assertEqual(encode(board), before)
        self.assertFalse(list(self.fixture.runtime.root.glob("*/commands/*.json")))


if __name__ == "__main__":
    unittest.main()
