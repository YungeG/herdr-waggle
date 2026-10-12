"""Frozen attempt links, bounded read-only catch-up and independent acceptance."""
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

from app import Waggle, main
from journal import append_event, encoded_event
from model import Run, carried_requirements
from runtime import RUN_IDENTITY_FIELDS, BridgeError, Runtime, publish
from store import StorageError, Store, decode, encode
from test_observation import files, mailbox, replace_record
from test_runtime import prepared_board


def child_run(parent, *, state="working", requirements=None):
    key = str(uuid4())
    return replace(parent, id=key, state=state, seq=3, delivery="full" if state == "finished" else "",
        requirements=carried_requirements(parent.requirements, parent.remaining) if requirements is None else requirements,
        summary="fixture child report" if state == "finished" else "", checks=["fixture proof"] if state == "finished" else [],
        remaining="", events=[encoded_event("continued", parent.id, source="system", run_id=key, seq=1),
            encoded_event("origin", "/tmp/fixture-session.jsonl", source="system", run_id=key, seq=2),
            encoded_event("acknowledged", "Pi 接收直接输入", source="system", run_id=key, seq=3)])


def link_record(parent, child):
    initial = replace(child, seq=2, state="queued", delivery="", summary="", checks=[], remaining="", events=child.events[:2])
    return {"version": 1, "parent": {key: getattr(parent, key) for key in RUN_IDENTITY_FIELDS},
            "parent_seq": parent.seq, "child": asdict(initial)}


def publish_child(endpoint, parent, child):
    publish(endpoint / "runs" / f"{child.id}.json", asdict(child))
    publish(endpoint / "runs" / f"{parent.id}.next.json", link_record(parent, child))


def session_record(run, **changes):
    return {"version": 1, "agent_id": run.agent_id, "session_id": run.session_id, "cwd": run.cwd,
            "seq": 1, "observed_at": int(time.time() * 1000), "state": "idle", "run_id": "",
            "linked_run_id": run.id, "following": True, "input_pending": False, **changes}


class ContinuationModel(unittest.TestCase):
    def setUp(self):
        self.board, self.task, queued = prepared_board()
        self.parent = replace(queued, state="failed", seq=1, summary="fixture failure", remaining="remaining scope")
        self.board.ingest(self.parent)

    def test_adopts_new_identity_freezes_parent_and_roundtrips_without_schema_migration(self):
        original = deepcopy(self.parent)
        child = child_run(self.parent, state="finished")
        self.assertTrue(self.board.continue_from(self.parent.id, child))
        self.assertEqual(self.parent, original)
        self.assertEqual(self.task.run_id, child.id)
        self.assertTrue(self.task.can_accept)
        self.assertEqual(self.task.status, "review")
        self.assertFalse(self.board.continue_from(self.parent.id, child))
        restored = decode(encode(self.board))
        self.assertEqual(restored.runs, self.board.runs)
        self.assertEqual(restored.tasks, self.board.tasks)

    def test_requires_current_active_unaccepted_task_and_exact_frozen_inputs(self):
        child = child_run(self.parent)
        for field, value in (("task_id", 90), ("agent_id", str(uuid4())), ("session_id", str(uuid4())),
                             ("cwd", "/other"), ("title", "different"), ("description", "different"), ("requirements", "lost scope")):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.board.continue_from(self.parent.id, replace(child, **{field: value}))
        for field, value in (("status", "done"), ("run", "none"), ("retention", "archived"), ("agent", str(uuid4())), ("title", "edited")):
            board = deepcopy(self.board)
            setattr(board.tasks[self.task.id], field, value)
            before = encode(board)
            with self.subTest(field=field), self.assertRaises(ValueError):
                board.continue_from(self.parent.id, child)
            self.assertEqual(encode(board), before)
        self.assertEqual(len(self.board.runs), 1)

    def test_historical_unpublished_supplement_is_not_bypassed_by_new_run_id(self):
        board, task, queued = prepared_board()
        working = replace(queued, state="working", seq=1)
        board.ingest(working)
        note = board.prepare_supplement(task, str(uuid4()), "must verify the missing branch", int(time.time() * 1000) + 30000)
        parent = replace(working, state="failed", seq=2)
        board.ingest(parent)
        for carried in (False, True):
            with self.subTest(carried=carried):
                candidate = deepcopy(board)
                child = child_run(parent, state="finished",
                    requirements=carried_requirements(parent.requirements, parent.remaining, note.text if carried else ""))
                candidate.continue_from(parent.id, child)
                self.assertEqual(candidate.tasks[task.id].can_accept, carried)
                if not carried:
                    with self.assertRaises(ValueError):
                        candidate.accept(candidate.tasks[task.id])
                    self.assertIn(note.text, candidate.supplement_prefill(candidate.tasks[task.id], "next"))
                self.assertEqual(candidate.supplements[note.id].status, "pending", "An old receipt must never be rewritten")
                self.assertEqual(decode(encode(candidate)).tasks, candidate.tasks)

    def test_parent_marker_is_validated_preserved_and_cannot_be_removed(self):
        child = child_run(self.parent)
        for body in (child.id, "../not-a-uuid", ""):
            values = [encoded_event("continued", body, source="system", run_id=child.id, seq=1)]
            with self.subTest(body=body), self.assertRaises(ValueError):
                Run.from_record(asdict(replace(child, events=values)))
        with self.assertRaises(ValueError):
            Run.from_record(asdict(replace(child, events=child.events + [encoded_event("continued", self.parent.id, source="system", run_id=child.id, seq=3)])))
        events = list(child.events)
        for seq in range(4, 150):
            events = append_event(events, encoded_event("warning", "bounded fixture warning", source="system", run_id=child.id, seq=seq))
        kept = replace(child, events=events, seq=149)
        self.assertEqual(kept.continuation_parent, self.parent.id)
        self.board.continue_from(self.parent.id, child)
        with self.assertRaises(ValueError):
            self.board.ingest(replace(child, seq=4, events=child.events[1:]))

    def test_store_rejects_orphans_forks_and_cycles(self):
        child = child_run(self.parent, state="finished")
        self.board.continue_from(self.parent.id, child)
        orphan = deepcopy(self.board)
        orphan.runs.pop(self.parent.id)
        with self.assertRaises(StorageError):
            decode(encode(orphan))
        sibling = child_run(self.parent, state="finished")
        fork = deepcopy(self.board)
        fork.runs[sibling.id] = sibling
        with self.assertRaises(StorageError):
            decode(encode(fork))
        cycle = deepcopy(self.board)
        cycle.runs[self.parent.id].events = [encoded_event("continued", child.id, source="system", run_id=self.parent.id, seq=1)]
        # Equal frozen requirements make this a genuine cycle rather than only a scope mismatch.
        cycle.runs[self.parent.id].requirements = child.requirements
        cycle.runs[self.parent.id].remaining = ""
        with self.assertRaises(StorageError):
            decode(encode(cycle))


class ContinuationRuntime(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(prefix="waggle-links-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.board, self.task, run = prepared_board(str(self.root))
        self.parent = replace(run, seq=1, state="failed")
        self.board.ingest(self.parent)
        self.source = self.root / "source"
        self.endpoint = mailbox(self.source, self.parent, idle=True)
        self.runtime = Runtime(self.root / "native")
        self.child = child_run(self.parent, state="finished")
        publish_child(self.endpoint, self.parent, self.child)

    def test_exact_run_grant_never_reads_successors_or_gains_command_rights(self):
        self.runtime.observe(self.parent, self.source)
        with patch("runtime.read_json") as read:
            self.assertEqual(self.runtime.read_continuations(self.parent), [])
            read.assert_not_called()
        with self.assertRaises(BridgeError):
            self.runtime.inherit_observation(self.parent, self.child)
        self.assertFalse(self.runtime.observation_only(self.child))

    def test_task_grant_catches_up_read_only_and_reopens_from_historical_anchor(self):
        self.runtime.observe(self.parent, self.source, follow=True)
        before = files(self.source)
        self.assertEqual(self.runtime.read_continuations(self.parent), [self.child])
        self.assertFalse(self.runtime.observation_only(self.child), "Read alone cannot register a descendant")
        self.board.continue_from(self.parent.id, self.child)
        self.runtime.inherit_observation(self.parent, self.child)
        with patch("runtime.publish") as write:
            for method in (self.runtime.dispatch, self.runtime.stop, self.runtime.check_supplement):
                with self.assertRaisesRegex(BridgeError, "只读"):
                    method(self.child)
            write.assert_not_called()
        reopened = Runtime(self.root / "other-native")
        Waggle(decode(encode(self.board)), runtime=reopened, task_observations={self.parent.id: self.source})
        self.assertTrue(reopened.observation_only(self.child))
        self.assertTrue(reopened.follows_task(self.child))
        self.assertEqual(reopened.read_run(self.child), self.child)
        self.assertEqual(files(self.source), before)
        self.assertFalse(self.runtime.root.exists())
        self.assertFalse(reopened.root.exists())

    def test_task_cli_accepts_verified_historical_anchor_without_persisting_authority(self):
        self.board.continue_from(self.parent.id, self.child)
        path = self.root / "tasks.sqlite3"
        with Store(path) as store:
            store.load()
            store.save(self.board)
        before = path.read_bytes()
        args = ["--db", str(path), "--observe-task", self.parent.id, str(self.source)]
        def inspect(app):
            assert app.runtime is not None
            self.assertTrue(app.runtime.observation_only(self.child))
            self.assertTrue(app.runtime.follows_task(self.child))
        with patch.dict(os.environ, {"WAGGLE_BRIDGE_DIR": str(self.runtime.root)}):
            with patch.object(Waggle, "run", autospec=True, side_effect=inspect):
                self.assertEqual(main(args), 0)
            with Store(path) as store:
                plain = Waggle(store=store)
                assert plain.runtime is not None
                self.assertFalse(plain.runtime.observation_only(self.child))
            for extra in (["--demo", "--observe-task", self.parent.id, str(self.source)],
                          args + ["--observe-run", self.parent.id, str(self.source)]):
                with patch("app.Store") as opened, redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    main(extra)
                opened.assert_not_called()
            with patch.object(Waggle, "run") as launch, redirect_stderr(io.StringIO()):
                self.assertEqual(main(["--db", str(path), "--observe-task", str(uuid4()), str(self.source)]), 1)
                launch.assert_not_called()
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(self.runtime.root.exists())

    def test_link_validates_parent_revision_identity_initial_snapshot_and_latest_history(self):
        self.runtime.observe(self.parent, self.source, follow=True)
        link = self.endpoint / "runs" / f"{self.parent.id}.next.json"
        original = link_record(self.parent, self.child)
        bad = [{**original, "version": True}, {**original, "parent_seq": 0},
               {**original, "parent": {**original["parent"], "task_id": 999}},
               {**original, "child": {**original["child"], "agent_id": str(uuid4())}},
               {**original, "child": {**original["child"], "state": "working"}}]
        for row in bad:
            replace_record(link, row)
            with self.subTest(row=row.keys()), self.assertRaises(ValueError):
                self.runtime.read_continuations(self.parent)
        replace_record(link, original)
        for changed in (replace(self.child, seq=1), replace(self.child, events=self.child.events[1:]),
                        replace(self.child, description="changed")):
            replace_record(self.endpoint / "runs" / f"{self.child.id}.json", asdict(changed))
            with self.assertRaises(ValueError):
                self.runtime.read_continuations(self.parent)

    def test_bounded_catch_up_and_missing_or_unsafe_files_never_discover_other_mail(self):
        self.runtime.observe(self.parent, self.source, follow=True)
        grandchild = child_run(self.child, state="finished")
        publish_child(self.endpoint, self.child, grandchild)
        self.assertEqual(self.runtime.read_continuations(self.parent, limit=1), [self.child])
        self.assertEqual(self.runtime.read_continuations(self.parent), [self.child, grandchild])
        with self.assertRaises(BridgeError):
            self.runtime.read_continuations(self.parent, limit=9)
        child_file = self.endpoint / "runs" / f"{self.child.id}.json"
        child_file.unlink()
        with self.assertRaises(FileNotFoundError):
            self.runtime.read_continuations(self.parent)
        child_file.symlink_to(self.endpoint / "runs" / f"{self.parent.id}.json")
        with self.assertRaises(OSError):
            self.runtime.read_continuations(self.parent)

    def test_current_check_catches_not_yet_ingested_child(self):
        self.runtime.observe(self.parent, self.source, follow=True)
        with self.assertRaisesRegex(BridgeError, "续接"):
            self.runtime.check_current(self.parent)
        self.assertEqual(self.task.run_id, self.parent.id)

    def test_missing_explicit_source_cannot_bypass_confirmation_as_a_legacy_import(self):
        missing = self.root / "missing-observed-source"
        for follow in (False, True):
            runtime = Runtime(self.root / "unused-native")
            runtime.observe(self.parent, missing, follow=follow)
            with self.subTest(follow=follow), self.assertRaisesRegex(BridgeError, "来源不可用"):
                runtime.check_current(self.parent)
        Runtime(self.root / "legacy-import-without-source").check_current(self.parent)
        self.assertFalse(missing.exists())
        self.assertFalse((self.root / "unused-native").exists())
        self.assertFalse((self.root / "legacy-import-without-source").exists())

    def test_optional_session_schema_revisions_and_uncertain_input(self):
        self.runtime.observe(self.parent, self.source, follow=True)
        self.assertIsNone(self.runtime.read_session(self.parent))
        path = self.endpoint / "session.json"
        row = session_record(self.parent)
        publish(path, row)
        first = self.runtime.read_session(self.parent)
        assert first is not None
        first.check_after(None)
        for changes in ({"agent_id": str(uuid4())}, {"seq": True}, {"run_id": "bad"}, {"state": "completed"}, {"following": True, "linked_run_id": ""}):
            replace_record(path, {**row, **changes})
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.runtime.read_session(self.parent)
        for newer in (replace(first, seq=0), replace(first, state="working")):
            with self.assertRaises(BridgeError):
                newer.check_after(first)
        (self.endpoint / "runs" / f"{self.parent.id}.next.json").unlink()
        for changes in ({"input_pending": True}, {"observed_at": 1}, {"state": "working", "run_id": self.parent.id}, {"linked_run_id": self.child.id}):
            replace_record(path, {**row, **changes})
            with self.subTest(changes=changes), self.assertRaises(BridgeError):
                self.runtime.check_current(self.parent)
        replace_record(path, row)
        self.runtime.check_current(self.parent)
        path.unlink()
        with self.assertRaises(BridgeError):
            self.runtime.check_current(self.parent, known_session=True)


if __name__ == "__main__":
    unittest.main()
