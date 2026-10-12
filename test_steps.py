"""Explicit business plans, no inferred progress, and owned SQLite migration checks."""

import json
import sqlite3
import unittest
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from journal import encoded_event, readable_event
from model import Board, Run
from steps import Step
from store import APPLICATION_ID, SCHEMA_VERSION, StorageError, Store, decode, encode
from task_context import WorkContract


def update(board, task, index=0, /, **changes):
    values = asdict(task.steps[index])
    for key in ("id", "updated_at", "source"):
        values.pop(key)
    board.update_step(task, task.steps[index].id, **(values | changes))


def planned_board():
    board = Board(live=True)
    board.add_project("fixture", "/virtual/project")
    task = board.create("明确任务", "fixture", "不自动生成步骤")
    board.add_steps(task, ["需求", "实现", "验证"])
    return board, task


class StepModelTests(unittest.TestCase):
    def test_plan_starts_pending_and_done_never_accepts_or_promotes(self):
        board, task = planned_board()
        self.assertEqual([s.status for s in task.steps], ["pending"] * 3)
        ids = [s.id for s in task.steps]
        update(board, task, status="doing", goal="检查范围")
        update(
            board,
            task,
            status="done",
            output="已核对范围",
            evidence="人工记录：范围说明",
        )
        self.assertEqual([s.status for s in task.steps], ["done", "pending", "pending"])
        self.assertEqual(
            (task.status, task.run, task.can_accept, board.runs),
            ("draft", "none", False, {}),
        )
        self.assertEqual(ids, [s.id for s in task.steps])
        self.assertTrue(
            all(s.source == "human" and s.updated_at.endswith("Z") for s in task.steps)
        )
        events = [readable_event(raw) for raw in task.events]
        self.assertTrue(
            all(
                event and event.source == "human" and not event.run_id
                for event in events
            )
        )

    def test_invalid_progress_and_second_current_step_leave_plan_unchanged(self):
        board, task = planned_board()
        for changes in [
            {"status": "done"},
            {"status": "blocked", "blocker": "缺少授权"},
            {"title": "\n"},
            {"title": "a" * 81},
            {"goal": "a" * 2001},
            {"title": "bad\x1b[1m"},
            {"status": "unknown"},
        ]:
            before = deepcopy(vars(board))
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                update(board, task, **changes)
            self.assertEqual(vars(board), before)
        update(
            board,
            task,
            status="blocked",
            blocker="缺少授权",
            next_action="用户核对数据来源",
        )
        with self.assertRaises(ValueError):
            update(board, task, 1, status="doing")
        self.assertEqual(task.steps[1].status, "pending")
        update(board, task, status="pending")
        update(board, task, 1, status="doing")
        self.assertEqual(task.steps[1].status, "doing")

    def test_append_keeps_ids_and_rejects_oversized_or_ambiguous_plans(self):
        board, task = planned_board()
        original = deepcopy(task.steps)
        board.add_steps(task, [f"明确步骤 {i}" for i in range(17)])
        self.assertEqual(task.steps[:3], original)
        for titles in [[], [""], ["再次追加"], "不是列表", [None]]:
            before = deepcopy(task)
            with self.subTest(titles=titles), self.assertRaises(ValueError):
                board.add_steps(task, titles)
            self.assertEqual(task, before)

    def test_run_links_are_many_to_many_but_never_foreign(self):
        board, task = planned_board()
        run1 = Run(
            str(uuid4()),
            task.id,
            str(uuid4()),
            str(uuid4()),
            "/virtual/project",
            task.title,
            "",
            "",
            state="finished",
        )
        run2 = replace(run1, id=str(uuid4()))
        board.runs = {run1.id: run1, run2.id: run2}
        for index in (0, 1):
            update(board, task, index, run_ids=[run1.id, run2.id])
        other = board.create("无关任务", "fixture")
        foreign = replace(run1, id=str(uuid4()), task_id=other.id)
        board.runs[foreign.id] = foreign
        for links in [[foreign.id], [str(uuid4())], [run1.id, run1.id]]:
            with self.subTest(links=links), self.assertRaises(ValueError):
                update(board, task, run_ids=links)
        self.assertEqual(task.steps[0].run_ids, [run1.id, run2.id])

    def test_runs_do_not_infer_or_update_steps_and_old_tasks_have_no_plan(self):
        board = Board(live=True)
        board.add_project("fixture", "/virtual/project")
        task = board.create("没有计划", "fixture")
        self.assertEqual(task.steps, [])
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
        board.add_steps(task, ["单独记录的目标"])
        update(board, task, status="doing")
        before = deepcopy(task.steps)
        result = replace(
            run,
            state="finished",
            seq=1,
            delivery="full",
            summary="自报完成",
            checks=["自报依据"],
        )
        board.ingest(result)
        self.assertEqual(task.steps, before)
        self.assertTrue(task.can_accept)
        board.accept(task)
        self.assertEqual(task.steps, before)
        with self.assertRaises(ValueError):
            update(board, task, status="pending")

    def test_retained_or_foreign_tasks_are_not_editable(self):
        board, task = planned_board()
        board.retire(task, "trashed")
        for operation in [
            lambda: update(board, task),
            lambda: board.add_steps(task, ["额外"]),
            lambda: board.add_steps(deepcopy(task), ["额外"]),
        ]:
            with self.assertRaises(ValueError):
                operation()
        board.restore(task)
        board.add_steps(task, ["重新恢复后添加"])
        self.assertEqual(len(task.steps), 4)

    def test_step_payload_and_event_provenance_are_strict(self):
        board, task = planned_board()
        row = asdict(task.steps[0])
        for bad in [
            row | {"source": "system"},
            row | {"updated_at": "tomorrow"},
            row | {"run_ids": True},
            row | {"run_ids": [None]},
            row | {"extra": "field"},
            row | {"id": "not-id"},
            row | {"goal": "\ud800"},
            row | {"status": True},
        ]:
            with self.subTest(bad=repr(bad)), self.assertRaises(ValueError):
                Step.from_record(bad)
        with self.assertRaises(ValueError):
            encoded_event("step_updated", "not human", source="system")


class StepStorageTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory(prefix="waggle-steps-")))
        self.path = self.root / "tasks.sqlite3"

    def old_database(self):
        board, task = planned_board()
        raw = json.loads(encode(board))
        raw.pop("decisions")  # v5 predates the separate human decision ledger.
        raw.pop("task_contexts")  # v5 predates task contexts.
        raw.pop("evidence_items", None)
        raw.pop("evidence_verifications", None)
        for field in ("task_dependencies", "dependency_checks", "ownership_releases", "ownership_receipts",
                      "budget_scopes", "usage_observations", "ledger_verifications", "budget_grants"):
            raw.pop(field)
        for row in raw["tasks"]:
            row.pop("steps")
        # A genuine v5 payload cannot have new feature events either.
        raw["tasks"][0]["events"] = raw["tasks"][0]["events"][:1]
        original = json.dumps(raw, ensure_ascii=False)
        with sqlite3.connect(self.path) as conn:
            conn.execute(f"PRAGMA application_id={APPLICATION_ID}")
            conn.execute("PRAGMA user_version=5")
            conn.execute(
                "CREATE TABLE state (singleton INTEGER PRIMARY KEY, revision INTEGER, data TEXT)"
            )
            conn.execute("INSERT INTO state VALUES (1, 8, ?)", (original,))
        return original

    def test_v5_migrates_empty_plan_with_exact_backup_and_roundtrips(self):
        original = self.old_database()
        with Store(self.path) as store:
            board = store.load()
            task = board.tasks[1]
            self.assertEqual(task.steps, [])
            self.assertEqual(
                store.connection.execute(
                    "SELECT revision,data FROM legacy_v5"
                ).fetchone(),
                (8, original),
            )
            self.assertEqual(
                store.connection.execute("PRAGMA user_version").fetchone()[0],
                SCHEMA_VERSION,
            )
            board.add_steps(task, ["人工定义"])
            update(
                board,
                task,
                status="blocked",
                blocker="等待核对",
                next_action="请用户确认",
            )
            store.save(board)
            saved = deepcopy(task)
        with Store(self.path) as store:
            self.assertEqual(store.load().tasks[1], saved)
            self.assertEqual(
                store.connection.execute("SELECT data FROM legacy_v5").fetchone()[0],
                original,
            )

    def test_migration_failure_rolls_back_version_backup_and_payload(self):
        original = self.old_database()
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "CREATE TRIGGER deny BEFORE UPDATE ON state BEGIN SELECT RAISE(ABORT, 'denied'); END"
            )
        with self.assertRaises(StorageError):
            Store(self.path)
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 5)
            self.assertEqual(
                conn.execute("SELECT data FROM state").fetchone()[0], original
            )
            self.assertFalse(
                conn.execute(
                    "SELECT name FROM sqlite_master WHERE name='legacy_v5'"
                ).fetchall()
            )

    def test_failed_save_and_stale_writer_do_not_erase_plan(self):
        with Store(self.path) as store, Store(self.path) as other:
            board = store.load()
            stale = other.load()
            task = board.create("计划", "收集箱")
            board.add_steps(task, ["第一步"])
            store.save(board)
            candidate = deepcopy(board)
            update(candidate, candidate.tasks[1], status="doing")
            store.connection.execute("PRAGMA query_only=ON")
            with self.assertRaises(StorageError):
                store.save(candidate)
            self.assertEqual(store.load().tasks[1].steps[0].status, "pending")
            with self.assertRaises(StorageError):
                other.save(stale)
            store.connection.execute("PRAGMA query_only=OFF")
            store.save(candidate)
            self.assertEqual(store.load().tasks[1].steps[0].status, "doing")

    def test_step_erasure_replacement_and_reordering_are_rejected_but_purge_works(self):
        with Store(self.path) as store:
            store.load()
            board, task = planned_board()
            store.save(board)
            for mutation in [
                lambda t: t.steps.clear(),
                lambda t: t.steps.reverse(),
                lambda t: t.steps.__setitem__(0, replace(t.steps[0], id=str(uuid4()))),
            ]:
                candidate = deepcopy(board)
                mutation(candidate.tasks[task.id])
                with self.assertRaises(StorageError):
                    store.save(candidate)
            board.retire(task, "trashed")
            store.save(board)
            candidate = deepcopy(board)
            candidate.tasks[task.id].steps[0] = replace(
                task.steps[0], title="隐藏时篡改"
            )
            with self.assertRaises(StorageError):
                store.save(candidate)
            board.purge(task)
            store.save(board)
            self.assertEqual(store.load().tasks, {})

    def test_schema6_requires_strict_plan_and_own_run_links(self):
        board, _ = planned_board()
        raw = json.loads(encode(board))
        for value in [
            None,
            {},
            [asdict(board.tasks[1].steps[0])] * 2,
            [asdict(board.tasks[1].steps[0]) | {"run_ids": [str(uuid4())]}],
        ]:
            candidate = deepcopy(raw)
            candidate["tasks"][0]["steps"] = value
            with self.subTest(value=value), self.assertRaises(StorageError):
                decode(json.dumps(candidate))
        raw["tasks"][0].pop("steps")
        with self.assertRaises(StorageError):
            decode(json.dumps(raw))
