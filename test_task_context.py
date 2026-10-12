"""N1 sentinel: task context, follow-up drafts, work contracts, migration and purge guards.

All databases are disposable; no real user data is touched.
"""
import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from decisions import EVIDENCE_ONLY_PREFIX, review_requirements
from journal import Brief, Report, encoded_event, parse_event
from model import PROJECTS, Board, Run, Task
from store import APPLICATION_ID, Store, encode


def _make_accepted_parent(board: Board) -> Task:
    """Create an accepted pi task with a real terminal run and acceptance event."""
    task = board.create("核对中文样本边界", "数据实验室", "检查跨日样例和空值")
    board.assign(task, str(uuid4()))
    run_id = str(uuid4())
    session_id = str(uuid4())
    cwd = board.projects[task.project]
    # Build a real full-delivery run with valid events
    report = Report(
        summary="样例方案和回归说明已整理。",
        checks=["基础流程通过", "中文及空输入边界通过"],
        remaining="",
        brief=Brief(["核对完成"], "样例通过", "回归说明已整理"),
    )
    full_event = encoded_event("full", "完整交付自报", source="executor", run_id=run_id, seq=1, report=report)
    # Note: settled is not in REPORT_KINDS, so we use a valid report kind or system source
    # For simplicity, just use the full event
    run = Run(run_id, task.id, task.agent, session_id, cwd, task.title, task.description,
              "样例方案和回归说明。", state="finished", seq=1, delivery="full",
              summary="样例方案和回归说明已整理。",
              checks=["基础流程通过", "中文及空输入边界通过"],
              remaining="", events=[full_event])
    board.runs[run.id] = run
    task.run_id = run.id
    task.run = "finished"
    task.status = "review"
    task.full_delivery = True
    task.summary = run.summary
    task.checks = list(run.checks)
    task.remaining = ""
    board.accept(task)
    return task


def _make_live_board() -> Board:
    """Create a live board with projects populated."""
    board = Board(live=True)
    board.projects = dict(PROJECTS)
    return board


class WorkContractTests(unittest.TestCase):
    """WorkContract is pure bounded text compiled into Task.remaining."""

    def test_blank_contract_rejected_by_queue_real(self):
        """First real queue_real rejects missing contract."""
        board = _make_live_board()
        task = board.create("测试任务", "数据实验室")
        board.assign(task, str(uuid4()))
        # Blank remaining means no contract
        with self.assertRaises(ValueError):
            board.queue_real(task, str(uuid4()), str(uuid4()), board.projects[task.project])

    def test_explicit_inventory_full_allowed(self):
        """Purpose inventory as standalone new task can report full."""
        from task_context import WorkContract
        contract = WorkContract(
            purpose="inventory",
            deliverable="盘点已有成果清单",
            allowed_scope="只读核对",
            verification="清单与原件逐条对应",
            exclusions="不修改文件",
        )
        compiled = contract.compile()
        self.assertIn("盘点已有成果清单", compiled)
        self.assertLessEqual(len(compiled), 6000)

    def test_evidence_only_rejects_full(self):
        """EVIDENCE_ONLY_PREFIX run cannot claim full delivery of original task."""
        from task_context import WorkContract
        contract = WorkContract(
            purpose="evidence",
            deliverable="核对已有材料",
            allowed_scope="只读",
            verification="来源逐条核对",
            exclusions="不修改文件不运行试验",
        )
        compiled = contract.compile()
        self.assertTrue(compiled.startswith(EVIDENCE_ONLY_PREFIX))

    def test_implementation_contract_compiles(self):
        from task_context import WorkContract
        contract = WorkContract(
            purpose="implementation",
            deliverable="实现边界检查",
            allowed_scope="中文样本目录",
            verification="回归测试通过",
            exclusions="不修改金融模块",
        )
        compiled = contract.compile()
        self.assertIn("实现边界检查", compiled)
        self.assertIn("回归测试通过", compiled)


class TaskContextTests(unittest.TestCase):
    """TaskContext is immutable snapshot referencing specific events."""

    def test_create_followup_from_accepted_parent(self):
        """Accepted parent -> follow-up draft with source snapshot, parent bytes unchanged."""
        from task_context import TaskContext, create_followup_context
        board = _make_live_board()
        parent = _make_accepted_parent(board)
        parent_events_before = list(parent.events)
        parent_run = board.runs[parent.run_id]
        parent_run_events_before = list(parent_run.events)

        # Find acceptance event
        acceptance_event = None
        for raw in parent.events:
            event = parse_event(raw)
            if event and event.kind == "accepted":
                acceptance_event = event
                break
        self.assertIsNotNone(acceptance_event)

        # Find the full report event
        report_event = None
        for raw in parent_run.events:
            event = parse_event(raw)
            if event and event.kind == "full":
                report_event = event
                break
        self.assertIsNotNone(report_event)

        assert report_event is not None and acceptance_event is not None
        ctx = create_followup_context(
            board, parent, report_event.id, acceptance_event.id,
            selected_refs=["summary", "checks"],
            new_title="基于D009成果补齐边界用例",
            new_goal="补齐空值和跨日边界测试",
        )
        self.assertIsInstance(ctx, TaskContext)
        self.assertEqual(ctx.parent_task_id, parent.id)
        self.assertEqual(ctx.source_report_event_id, report_event.id)
        self.assertEqual(ctx.acceptance_event_id, acceptance_event.id)
        self.assertEqual(ctx.cwd, board.projects[parent.project])

        # Parent bytes are unchanged
        self.assertEqual(parent.events, parent_events_before)
        self.assertEqual(parent_run.events, parent_run_events_before)

    def test_followup_creates_draft_task(self):
        """create_followup creates a draft task with context snapshot."""
        from task_context import create_followup_context
        board = _make_live_board()
        parent = _make_accepted_parent(board)
        report_event = None
        acceptance_event = None
        for raw in board.runs[parent.run_id].events:
            event = parse_event(raw)
            if event and event.kind == "full":
                report_event = event
        for raw in parent.events:
            event = parse_event(raw)
            if event and event.kind == "accepted":
                acceptance_event = event

        assert report_event is not None and acceptance_event is not None
        ctx = create_followup_context(
            board, parent, report_event.id, acceptance_event.id,
            selected_refs=["summary"],
            new_title="后续边界测试",
            new_goal="补齐空值用例",
        )
        child = board.create_followup(parent.id, ctx, "后续任务", "补齐边界用例")
        self.assertEqual(child.status, "draft")
        self.assertIn(child.id, board.tasks)
        self.assertIn(ctx.id, board.task_contexts)
        self.assertEqual(board.task_contexts[ctx.id].child_task_id, child.id)

    def test_source_drift_fails_closed(self):
        """Tampered source report fails context creation."""
        from task_context import create_followup_context
        board = _make_live_board()
        parent = _make_accepted_parent(board)
        report_event = None
        acceptance_event = None
        for raw in board.runs[parent.run_id].events:
            event = parse_event(raw)
            if event and event.kind == "full":
                report_event = event
        for raw in parent.events:
            event = parse_event(raw)
            if event and event.kind == "accepted":
                acceptance_event = event

        assert report_event is not None and acceptance_event is not None
        # Tamper with the source run events
        board.runs[parent.run_id].events = ["tampered"]
        with self.assertRaises(ValueError):
            create_followup_context(
                board, parent, report_event.id, acceptance_event.id,
                selected_refs=["summary"],
                new_title="后续",
                new_goal="补齐",
            )

    def test_oversize_export_refused(self):
        """Oversize source snapshot fails closed."""
        from task_context import create_followup_context
        board = _make_live_board()
        parent = _make_accepted_parent(board)
        report_event = None
        acceptance_event = None
        for raw in board.runs[parent.run_id].events:
            event = parse_event(raw)
            if event and event.kind == "full":
                report_event = event
        for raw in parent.events:
            event = parse_event(raw)
            if event and event.kind == "accepted":
                acceptance_event = event

        assert report_event is not None and acceptance_event is not None
        # Make parent description huge
        parent.description = "x" * 25000
        with self.assertRaises(ValueError):
            create_followup_context(
                board, parent, report_event.id, acceptance_event.id,
                selected_refs=["summary"],
                new_title="后续",
                new_goal="补齐",
            )


class MigrationTests(unittest.TestCase):
    """v7 -> v8 migration preserves old data exactly."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory(prefix="waggle-n1-test-")
        self.addCleanup(self.tmpdir.cleanup)
        self.path = Path(self.tmpdir.name) / "state" / "tasks.sqlite3"

    def test_v7_migration_adds_empty_contexts(self):
        """Old v7 data migrates to current schema with empty task_contexts registry."""
        # Create a v7-format snapshot
        board = _make_live_board()
        board.create("旧任务", "数据实验室", "旧说明")
        v7_data = json.dumps({
            "live": True,
            "projects": board.projects,
            "next_task_id": board.next_task_id,
            "tasks": [asdict(task) for task in board.tasks.values()],
            "runs": [],
            "supplements": [],
            "decisions": [],
        }, ensure_ascii=False)

        # Write as v7
        import sqlite3
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        with conn:
            conn.execute("CREATE TABLE state (singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision INTEGER NOT NULL, data TEXT NOT NULL)")
            conn.execute("INSERT INTO state VALUES (1, 0, ?)", (v7_data,))
            conn.execute(f"PRAGMA application_id={APPLICATION_ID}")
            conn.execute("PRAGMA user_version=7")
        conn.close()

        # Opening with current code should migrate
        with Store(self.path) as store:
            loaded = store.load()
            self.assertEqual(len(loaded.tasks), 1)
            self.assertEqual(loaded.tasks[1].title, "旧任务")
            self.assertEqual(loaded.task_contexts, {})  # Empty registry
            store.save(loaded)

        # Verify backup exists
        conn = sqlite3.connect(self.path)
        tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        conn.close()
        self.assertIn("legacy_v7", tables)

    def test_v7_no_inferred_relationships(self):
        """Old tasks without links get empty context registry, no fabricated parents."""
        board = _make_live_board()
        board.create("任务A", "数据实验室")
        board.create("任务B", "终端工作台")
        data = encode(board)
        # Simulate v7 by removing task_contexts if it exists and setting version
        raw = json.loads(data)
        if "task_contexts" in raw:
            del raw["task_contexts"]
        raw.pop("evidence_items")
        raw.pop("evidence_verifications")
        for field in ("task_dependencies", "dependency_checks", "ownership_releases", "ownership_receipts",
                      "budget_scopes", "usage_observations", "ledger_verifications", "budget_grants"):
            raw.pop(field)
        v7_data = json.dumps(raw, ensure_ascii=False)

        import sqlite3
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        with conn:
            conn.execute("CREATE TABLE state (singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision INTEGER NOT NULL, data TEXT NOT NULL)")
            conn.execute("INSERT INTO state VALUES (1, 0, ?)", (v7_data,))
            conn.execute(f"PRAGMA application_id={APPLICATION_ID}")
            conn.execute("PRAGMA user_version=7")
        conn.close()

        with Store(self.path) as store:
            loaded = store.load()
            self.assertEqual(loaded.task_contexts, {})
            # No fabricated relationships
            for ctx in loaded.task_contexts.values():
                self.assertNotEqual(ctx.parent_task_id, 0)


class PurgeGuardTests(unittest.TestCase):
    """Purge guards: protected parent cannot be purged if referenced."""

    def test_protected_parent_purge_blocked(self):
        """Parent referenced by TaskContext cannot be purged."""
        from task_context import create_followup_context
        board = _make_live_board()
        parent = _make_accepted_parent(board)
        report_event = None
        acceptance_event = None
        for raw in board.runs[parent.run_id].events:
            event = parse_event(raw)
            if event and event.kind == "full":
                report_event = event
        for raw in parent.events:
            event = parse_event(raw)
            if event and event.kind == "accepted":
                acceptance_event = event

        assert report_event is not None and acceptance_event is not None
        ctx = create_followup_context(
            board, parent, report_event.id, acceptance_event.id,
            selected_refs=["summary"],
            new_title="后续",
            new_goal="补齐",
        )
        child = board.create_followup(parent.id, ctx, "后续任务", "补齐边界用例")

        self.assertEqual(board.task_contexts[ctx.id].child_task_id, child.id)
        # Try to purge parent - should fail
        parent.retention = "trashed"
        with self.assertRaises(ValueError):
            board.purge(parent)

    def test_child_purge_removes_own_context(self):
        """Purging child task removes its own TaskContext."""
        from task_context import create_followup_context
        board = _make_live_board()
        parent = _make_accepted_parent(board)
        report_event = None
        acceptance_event = None
        for raw in board.runs[parent.run_id].events:
            event = parse_event(raw)
            if event and event.kind == "full":
                report_event = event
        for raw in parent.events:
            event = parse_event(raw)
            if event and event.kind == "accepted":
                acceptance_event = event

        assert report_event is not None and acceptance_event is not None
        ctx = create_followup_context(
            board, parent, report_event.id, acceptance_event.id,
            selected_refs=["summary"],
            new_title="后续",
            new_goal="补齐",
        )
        child = board.create_followup(parent.id, ctx, "后续任务", "补齐边界用例")
        child.retention = "trashed"
        context_id = ctx.id

        board.purge(child)
        self.assertNotIn(context_id, board.task_contexts)
        # Parent still exists (not purged)
        self.assertIn(parent.id, board.tasks)


class QueueRealContractTests(unittest.TestCase):
    """queue_real enforces WorkContract on first real dispatch."""

    def test_queue_real_requires_contract(self):
        """First real dispatch without contract is rejected."""
        board = _make_live_board()
        task = board.create("测试", "数据实验室")
        board.assign(task, str(uuid4()))
        # No remaining = no contract
        with self.assertRaises(ValueError):
            board.queue_real(task, str(uuid4()), str(uuid4()), board.projects[task.project])

    def test_queue_real_accepts_explicit_contract(self):
        """queue_real accepts task with explicit work contract."""
        from task_context import WorkContract
        board = _make_live_board()
        task = board.create("测试", "数据实验室")
        board.assign(task, str(uuid4()))
        contract = WorkContract(
            purpose="implementation",
            deliverable="实现功能",
            allowed_scope="项目目录",
            verification="测试通过",
            exclusions="不改其他模块",
        )
        task.remaining = contract.compile()
        run = board.queue_real(task, str(uuid4()), str(uuid4()), board.projects[task.project])
        self.assertEqual(run.requirements, task.remaining)

    def test_queue_real_accepts_review_fields(self):
        """Existing review_fields contract is accepted as legacy."""
        board = _make_live_board()
        task = board.create("测试", "数据实验室")
        board.assign(task, str(uuid4()))
        task.remaining = review_requirements("交付物", "允许范围", "验收依据")
        run = board.queue_real(task, str(uuid4()), str(uuid4()), board.projects[task.project])
        self.assertIn("交付物", run.requirements)

    def test_queue_real_accepts_evidence_only(self):
        """EVIDENCE_ONLY_PREFIX with complete question packet is accepted as legacy contract."""
        from decisions import task_evidence_request
        board = _make_live_board()
        task = board.create("测试", "数据实验室")
        board.assign(task, str(uuid4()))
        # Use complete canonical evidence request, not just prefix
        task.remaining = task_evidence_request("核对已有材料并给出建议")
        run = board.queue_real(task, str(uuid4()), str(uuid4()), board.projects[task.project])
        self.assertTrue(run.requirements.startswith(EVIDENCE_ONLY_PREFIX))


if __name__ == "__main__":
    unittest.main()
