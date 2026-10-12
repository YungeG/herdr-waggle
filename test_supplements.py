"""Run-bound supplements: no scope rewrite, replay, implicit execution or acceptance."""

import json
import os
import sqlite3
import time
import unittest
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from model import Board, Supplement
from runtime import Runtime, private_dir, publish, supplement_request
from store import StorageError, Store, decode, encode
from task_context import WorkContract


def working():
    board = Board(live=True)
    board.add_project("fixture", "/virtual/project")
    task = board.create("task", "fixture", "immutable scope")
    board.assign(task, str(uuid4()))
    contract = WorkContract(
        purpose="implementation",
        deliverable="完成整任务交付",
        allowed_scope="项目目录",
        verification="回归测试通过",
        exclusions="不修改其他模块",
    )
    task.remaining = contract.compile()
    run = board.queue_real(task, str(uuid4()), str(uuid4()), "/virtual/project")
    incoming = deepcopy(run)
    incoming.state, incoming.seq = "working", 1
    board.ingest(incoming)
    return board, task, board.runs[run.id]


class SupplementModelTests(unittest.TestCase):
    def test_modes_pending_live_next_do_not_overwrite_dispatched_identity(self):
        board = Board(live=True)
        task = board.create("draft", "收集箱")
        self.assertEqual(board.supplement_mode(task), "pending")
        board.save_supplement(task, "追加要求", "pending")
        self.assertEqual(
            (task.remaining, task.run, task.status), ("追加要求", "none", "draft")
        )
        board, task, run = working()
        before = run.identity
        self.assertEqual(board.supplement_mode(task), "live")
        note = board.prepare_supplement(
            task, str(uuid4()), "本轮纠正", int(time.time() * 1000) + 30000
        )
        self.assertEqual(
            (note.status, note.run_id, run.identity), ("pending", run.id, before)
        )
        self.assertFalse(task.full_delivery)
        with self.assertRaises(ValueError):
            board.prepare_supplement(task, str(uuid4()), "勿重复", note.expires)
        run.state, task.run, task.status = "failed", "finished", "blocked"
        self.assertEqual(board.supplement_mode(task), "next")
        self.assertIn("本轮纠正", board.supplement_prefill(task, "next"))
        board.save_supplement(task, "人工核对后下一轮要求", "next")
        self.assertEqual((task.status, task.run), ("ready", "none"))
        self.assertEqual(run.identity, before)

    def test_unsafe_states_never_fall_back_to_post_run_mode(self):
        for state in ("queued", "waiting"):
            board, task, run = working()
            run.state = state
            with self.assertRaises(ValueError):
                board.supplement_mode(task)
        for kind, text in (
            ("stop_requested", "human stop"),
            ("invalidated", "Pi 收到额外输入；不能接管"),
        ):
            board, task, run = working()
            board.note(task, kind, text, run_id=run.id)
            with self.assertRaises(ValueError):
                board.supplement_mode(task)
        board, task, run = working()
        task.retention = "archived"
        with self.assertRaises(ValueError):
            board.supplement_mode(task)
        task.retention = "active"
        del board.runs[run.id]
        with self.assertRaises(ValueError):
            board.supplement_mode(task)

    def test_receipt_sequence_identity_and_report_gate_survive_terminal_run(self):
        board, task, run = working()
        note = board.prepare_supplement(
            task, str(uuid4()), "需要覆盖的补充", 1800000000000
        )
        finished = deepcopy(run)
        finished.seq, finished.state, finished.delivery = 5, "finished", "full"
        finished.summary, finished.checks, finished.remaining = (
            "old full claim",
            ["evidence"],
            "",
        )
        board.ingest(finished)
        self.assertFalse(task.can_accept)
        with self.assertRaises(ValueError):
            board.accept(task)
        covered = replace(note, status="covered", seq=3, run_seq=4, detail="新报告覆盖")
        self.assertTrue(board.apply_supplement(covered))
        self.assertTrue(task.can_accept)
        self.assertFalse(board.apply_supplement(covered))
        for bad in (
            replace(covered, seq=2),
            replace(covered, text="changed"),
            replace(covered, status="rejected", seq=4),
            replace(covered, detail="same seq rewrite"),
        ):
            with self.assertRaises(ValueError):
                board.apply_supplement(bad)
        board.accept(task)
        self.assertEqual(task.status, "done")

    def test_receipt_waits_for_run_revision_and_unresolved_notes_cannot_be_forged_away(
        self,
    ):
        board, task, run = working()
        note = board.prepare_supplement(task, str(uuid4()), "note", 1800000000000)
        received = replace(note, status="received", seq=2, run_seq=3)
        self.assertFalse(board.apply_supplement(received))
        self.assertEqual(board.supplements[note.id].status, "pending")
        run.seq = 3
        self.assertTrue(board.apply_supplement(received))
        raw = json.loads(encode(board))
        raw["tasks"][0]["full_delivery"] = True
        with self.assertRaises(StorageError):
            decode(json.dumps(raw))
        self.assertEqual(decode(encode(board)).supplements, board.supplements)

    def test_supplement_schema_is_strict_and_purge_cleans_only_owned_notes(self):
        board, task, run = working()
        note = board.prepare_supplement(task, str(uuid4()), "note", 1800000000000)
        for fields in (
            {"seq": True},
            {"status": "unknown"},
            {"text": "x" * 4001},
            {"detail": "x" * 601},
            {"run_seq": -1},
            {"extra": "bad"},
        ):
            with self.assertRaises(ValueError):
                Supplement.from_record({**asdict(note), **fields})
        run.state = "failed"
        task.run = "finished"
        task.status = "blocked"
        board.retire(task, "trashed")
        board.purge(task)
        self.assertEqual(board.supplements, {})
        self.assertEqual(board.next_task_id, 2)


class SupplementMailboxTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="waggle-note-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.board, self.task, self.attempt = working()
        self.attempt.cwd = str(self.root)
        self.runtime = Runtime(self.root / "bridges")
        self.endpoint = self.runtime.root / self.attempt.agent_id
        for directory in (
            self.runtime.root,
            self.endpoint,
            self.endpoint / "supplements",
            self.endpoint / "supplements/requests",
            self.endpoint / "supplements/receipts",
        ):
            private_dir(directory, create=True)
        self.agent = {
            "version": 1,
            "id": self.attempt.agent_id,
            "session_id": self.attempt.session_id,
            "cwd": str(self.root),
            "name": "fixture",
            "model": "offline",
            "pid": 1,
            "heartbeat": int(time.time() * 1000),
            "online": True,
            "idle": False,
            "active_run": self.attempt.id,
        }
        publish(self.endpoint / "agent.json", self.agent)
        self.capability = {
            "version": 1,
            "agent_id": self.attempt.agent_id,
            "session_id": self.attempt.session_id,
            "cwd": str(self.root),
            "supplement": 1,
        }

    def test_old_bridge_and_read_only_are_rejected_before_any_command(self):
        with self.assertRaises(ValueError):
            self.runtime.check_supplement(self.attempt)
        publish(self.endpoint / "capabilities.json", self.capability)
        self.runtime.check_supplement(self.attempt)
        self.runtime.observe(self.attempt, self.runtime.root)
        with self.assertRaises(ValueError):
            self.runtime.check_supplement(self.attempt)
        self.assertEqual(list((self.endpoint / "supplements/requests").iterdir()), [])

    def test_typed_request_and_receipt_match_full_immutable_input(self):
        publish(self.endpoint / "capabilities.json", self.capability)
        note = self.board.prepare_supplement(
            self.task, str(uuid4()), "精确补充", int(time.time() * 1000) + 30000
        )
        self.runtime.send_supplement(self.attempt, note)
        file = self.endpoint / "supplements/requests" / f"{note.id}.json"
        request = json.loads(file.read_text())
        self.assertEqual(
            set(request["run"]),
            {
                "id",
                "task_id",
                "agent_id",
                "session_id",
                "cwd",
                "title",
                "description",
                "requirements",
            },
        )
        self.assertEqual(request["text"], note.text)
        with self.assertRaises(FileExistsError):
            self.runtime.send_supplement(self.attempt, note)
        receipt = {
            "version": 1,
            "request": request,
            "status": "received",
            "seq": 2,
            "run_seq": 3,
            "detail": "Pi已接收",
        }
        target = self.endpoint / "supplements/receipts" / f"{note.id}.json"
        publish(target, receipt)
        parsed = self.runtime.read_supplement(self.attempt, note)
        assert parsed
        self.assertEqual((parsed.status, parsed.text), ("received", note.text))
        target.unlink()
        receipt["request"]["run"]["description"] = "wrong scope"
        publish(target, receipt)
        with self.assertRaises(ValueError):
            self.runtime.read_supplement(self.attempt, note)
        self.assertFalse((self.endpoint / "commands").exists())

    def test_capability_receipt_private_files_and_primitive_types_fail_closed(self):
        publish(self.endpoint / "capabilities.json", self.capability)
        note = self.board.prepare_supplement(
            self.task, str(uuid4()), "note", int(time.time() * 1000) + 30000
        )
        target = self.endpoint / "supplements/receipts" / f"{note.id}.json"
        request = supplement_request(self.attempt, note)
        row = {
            "version": 1,
            "request": request,
            "status": "received",
            "seq": 2,
            "run_seq": 3,
            "detail": "received",
        }
        row["request"]["run"]["task_id"] = True
        publish(target, row)
        with self.assertRaises(ValueError):
            self.runtime.read_supplement(self.attempt, note)
        target.unlink()
        target.symlink_to(self.endpoint / "agent.json")
        with self.assertRaises((ValueError, OSError)):
            self.runtime.read_supplement(self.attempt, note)
        target.unlink()
        os.mkfifo(target, 0o600)
        with self.assertRaises(ValueError):
            self.runtime.read_supplement(self.attempt, note)
        target.unlink()
        target.write_bytes(b" " * (256 * 1024 + 1))
        target.chmod(0o600)
        with self.assertRaises(ValueError):
            self.runtime.read_supplement(self.attempt, note)
        capability = self.endpoint / "capabilities.json"
        capability.unlink()
        publish(capability, {**self.capability, "supplement": 2})
        with self.assertRaises(ValueError):
            self.runtime.check_supplement(self.attempt)

    def test_saved_intents_cannot_be_erased_to_restore_acceptance(self):
        with Store(self.root / "notes.sqlite3") as store:
            store.load()
            note = self.board.prepare_supplement(
                self.task, str(uuid4()), "must remain", 1800000000000
            )
            store.save(self.board)
            candidate = deepcopy(self.board)
            candidate.supplements.clear()
            with self.assertRaises(StorageError):
                store.save(candidate)
            self.assertIn(note.id, store.load().supplements)

    def test_schema4_upgrade_backs_up_unchanged_source(self):
        file = self.root / "state.sqlite3"
        raw = json.loads(encode(self.board))
        raw.pop("supplements", None)
        raw.pop("decisions")
        raw.pop("task_contexts")
        raw.pop("evidence_items", None)
        raw.pop("evidence_verifications", None)
        for field in ("task_dependencies", "dependency_checks", "ownership_releases", "ownership_receipts",
                      "budget_scopes", "usage_observations", "ledger_verifications", "budget_grants"):
            raw.pop(field)
        for task in raw["tasks"]:
            task.pop("steps", None)
        original = json.dumps(raw)
        from store import APPLICATION_ID, SCHEMA_VERSION

        connection = sqlite3.connect(file)
        with connection:
            connection.execute(
                "CREATE TABLE state(singleton INTEGER PRIMARY KEY, revision INTEGER NOT NULL, data TEXT NOT NULL)"
            )
            connection.execute("INSERT INTO state VALUES(1, 4, ?)", (original,))
            connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
            connection.execute("PRAGMA user_version=4")
        connection.close()
        with Store(file) as store:
            restored = store.load()
            self.assertEqual(restored.supplements, {})
            self.assertEqual(restored.next_task_id, self.board.next_task_id)
            self.assertEqual(
                store.connection.execute("SELECT data FROM legacy_v4").fetchone()[0],
                original,
            )
            self.assertEqual(
                store.connection.execute("PRAGMA user_version").fetchone()[0],
                SCHEMA_VERSION,
            )
