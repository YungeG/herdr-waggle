"""Focused red tests for N1 blockers identified by Main.

These tests verify the actual security and correctness issues:
1. Prefix-only spoof rejection
2. Source tamper detection
3. Digest/snapshot tamper detection
4. Wrong child/parent link detection
5. User title/goal preservation
6. Read-only context in dispatched requirements
"""
import unittest
from dataclasses import replace
from uuid import uuid4

from decisions import EVIDENCE_ONLY_PREFIX
from journal import Brief, Report, encoded_event, parse_event
from model import PROJECTS, Board, Run
from store import decode, encode
from task_context import (
    CONTRACT_PREFIX,
    TaskContext,
    WorkContract,
    create_followup_context,
    has_valid_contract,
    parse_compiled_contract,
    validate_source,
)


def _make_live_board() -> Board:
    board = Board(live=True)
    board.projects = dict(PROJECTS)
    return board


def _make_accepted_parent(board: Board):
    """Create an accepted pi task with a real terminal run."""
    task = board.create("核对中文样本边界", "数据实验室", "检查跨日样例和空值")
    board.assign(task, str(uuid4()))
    run_id = str(uuid4())
    session_id = str(uuid4())
    cwd = board.projects[task.project]
    report = Report(
        summary="样例方案和回归说明已整理。",
        checks=["基础流程通过", "中文及空输入边界通过"],
        remaining="",
        brief=Brief(["核对完成"], "样例通过", "回归说明已整理"),
    )
    full_event = encoded_event(
        "full", "完整交付自报", source="executor", run_id=run_id, seq=1, report=report
    )
    run = Run(
        run_id, task.id, task.agent, session_id, cwd, task.title, task.description,
        "样例方案和回归说明。", state="finished", seq=1, delivery="full",
        summary="样例方案和回归说明已整理。",
        checks=["基础流程通过", "中文及空输入边界通过"],
        remaining="", events=[full_event],
    )
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


def _find_events(parent):
    """Find report and acceptance events from an accepted parent."""
    report_event = None
    acceptance_event = None
    parent_run = _get_parent_run(parent)
    for raw in parent_run.events:
        event = parse_event(raw)
        if event is not None and event.kind == "full":
            report_event = event
    for raw in parent.events:
        event = parse_event(raw)
        if event is not None and event.kind == "accepted":
            acceptance_event = event
    assert report_event is not None, "full report event not found"
    assert acceptance_event is not None, "accepted event not found"
    return report_event, acceptance_event


_last_board: Board | None = None


def _get_parent_run(parent):
    """Get the parent run from the board via the test fixture.

    Since parent doesn't carry a board ref, we use a module-level cache.
    """
    assert _last_board is not None, "_last_board not set"
    return _last_board.runs[parent.run_id]


class PrefixSpoofTests(unittest.TestCase):
    """Prefix-only text MUST be rejected."""

    def test_contract_prefix_only_garbage_rejected(self):
        """'本轮核查合同：garbage' MUST return False."""
        garbage = CONTRACT_PREFIX + "garbage"
        self.assertFalse(has_valid_contract(garbage))

    def test_contract_prefix_only_empty_rejected(self):
        """'本轮核查合同：' alone MUST return False."""
        self.assertFalse(has_valid_contract(CONTRACT_PREFIX))

    def test_contract_prefix_only_whitespace_rejected(self):
        """'本轮核查合同：   ' MUST return False."""
        self.assertFalse(has_valid_contract(CONTRACT_PREFIX + "   "))

    def test_evidence_prefix_only_garbage_rejected(self):
        """'仅补证请求：garbage' with <5 chars MUST return False."""
        self.assertFalse(has_valid_contract(EVIDENCE_ONLY_PREFIX + "短"))

    def test_parse_compiled_contract_rejects_garbage(self):
        """parse_compiled_contract MUST return None for garbage."""
        self.assertIsNone(parse_compiled_contract(CONTRACT_PREFIX + "garbage"))
        self.assertIsNone(parse_compiled_contract(CONTRACT_PREFIX))

    def test_prefix_spoof_not_accepted_by_queue_real(self):
        """Prefix-only spoof MUST be rejected by queue_real."""
        board = _make_live_board()
        task = board.create("测试", "数据实验室")
        board.assign(task, str(uuid4()))
        task.remaining = CONTRACT_PREFIX + "garbage"
        with self.assertRaises(ValueError):
            board.queue_real(task, str(uuid4()), str(uuid4()), board.projects[task.project])


class SourceTamperTests(unittest.TestCase):
    """Source tamper detection."""

    def test_same_event_id_report_text_tamper_detected(self):
        """Tampered report text in same event ID MUST be detected by validate_source."""
        global _last_board
        board = _make_live_board()
        _last_board = board
        parent = _make_accepted_parent(board)
        report_event, acceptance_event = _find_events(parent)

        ctx = create_followup_context(
            board, parent, report_event.id, acceptance_event.id,
            selected_refs=["summary"], new_title="后续", new_goal="补齐",
        )
        board.task_contexts[ctx.id] = ctx
        # Tamper with the report event text (replace with different content)
        tampered_report = Report(
            summary="被篡改的摘要",
            checks=["被篡改的检查"],
            remaining="",
            brief=Brief(["篡改"], "篡改", "篡改"),
        )
        tampered_event = encoded_event(
            "full", "篡改", source="executor",
            run_id=parent.run_id, seq=1, report=tampered_report,
        )
        board.runs[parent.run_id].events = [tampered_event]
        # validate_source MUST detect the tamper via digest mismatch
        with self.assertRaises(ValueError):
            validate_source(board, ctx)

    def test_accepted_source_forged_wrong_run_rejected(self):
        """Acceptance event from wrong Run MUST be rejected."""
        global _last_board
        board = _make_live_board()
        _last_board = board
        parent = _make_accepted_parent(board)
        report_event, _ = _find_events(parent)

        # Create a fake acceptance event with wrong run_id
        fake_acceptance = encoded_event(
            "accepted", "伪造验收", source="human", run_id=str(uuid4()), seq=0,
        )
        parent.events.append(fake_acceptance)
        fake_event = parse_event(fake_acceptance)
        assert fake_event is not None
        # create_followup_context MUST reject this
        with self.assertRaises(ValueError) as cm:
            create_followup_context(
                board, parent, report_event.id, fake_event.id,
                selected_refs=["summary"], new_title="后续", new_goal="补齐",
            )
        self.assertIn("验收锚点", str(cm.exception))


class DigestTamperTests(unittest.TestCase):
    """Digest/snapshot tamper detection."""

    def test_tampered_persisted_digest_detected(self):
        """Tampered persisted digest MUST be detected."""
        global _last_board
        board = _make_live_board()
        _last_board = board
        parent = _make_accepted_parent(board)
        report_event, acceptance_event = _find_events(parent)

        ctx = create_followup_context(
            board, parent, report_event.id, acceptance_event.id,
            selected_refs=["summary"], new_title="后续", new_goal="补齐",
        )
        board.task_contexts[ctx.id] = ctx
        tampered_ctx = replace(ctx, source_digest="0" * 64)
        board.task_contexts[ctx.id] = tampered_ctx
        with self.assertRaises(ValueError) as cm:
            validate_source(board, tampered_ctx)
        self.assertIn("前序来源", str(cm.exception))

    def test_tampered_snapshot_detected(self):
        """Tampered source_snapshot MUST be detected."""
        global _last_board
        board = _make_live_board()
        _last_board = board
        parent = _make_accepted_parent(board)
        report_event, acceptance_event = _find_events(parent)

        ctx = create_followup_context(
            board, parent, report_event.id, acceptance_event.id,
            selected_refs=["summary"], new_title="后续", new_goal="补齐",
        )
        board.task_contexts[ctx.id] = ctx
        tampered_ctx = replace(ctx, source_snapshot="被篡改的快照")
        board.task_contexts[ctx.id] = tampered_ctx
        with self.assertRaises(ValueError) as cm:
            validate_source(board, tampered_ctx)
        self.assertIn("前序来源", str(cm.exception))


class WrongLinkTests(unittest.TestCase):
    """Wrong child/parent link detection."""

    def test_wrong_child_task_id_rejected_by_store(self):
        """TaskContext with wrong child_task_id MUST be rejected by Store decode."""
        global _last_board
        board = _make_live_board()
        _last_board = board
        parent = _make_accepted_parent(board)
        report_event, acceptance_event = _find_events(parent)

        ctx = create_followup_context(
            board, parent, report_event.id, acceptance_event.id,
            selected_refs=["summary"], new_title="后续", new_goal="补齐",
        )
        board.create_followup(parent.id, ctx, "后续任务", "补齐边界用例")
        # Tamper with child_task_id to point to nonexistent task
        tampered_ctx = replace(ctx, child_task_id=99999)
        board.task_contexts[ctx.id] = tampered_ctx
        # Store decode MUST reject this
        data = encode(board)
        from store import StorageError
        with self.assertRaises(StorageError):
            decode(data)


class UserTitleGoalTests(unittest.TestCase):
    """User title/goal preservation."""

    def test_user_title_preserved_in_followup(self):
        """User-provided title and goal MUST be preserved in the draft."""
        global _last_board
        board = _make_live_board()
        _last_board = board
        parent = _make_accepted_parent(board)
        report_event, acceptance_event = _find_events(parent)

        user_title = "用户自定义标题：补齐边界用例"
        ctx = create_followup_context(
            board, parent, report_event.id, acceptance_event.id,
            selected_refs=["summary"], new_title=user_title, new_goal="补齐",
        )
        self.assertIsInstance(ctx, TaskContext)
        # The context should be creatable without error
        child = board.create_followup(parent.id, ctx, "后续任务", "补齐边界用例")
        self.assertEqual(child.status, "draft")
        self.assertIn(child.id, board.tasks)


class ReadonlyContextTests(unittest.TestCase):
    """Read-only context in dispatched requirements."""

    def test_context_snapshot_in_child_description(self):
        """Context source_snapshot MUST be stored in registry, not child description."""
        global _last_board
        board = _make_live_board()
        _last_board = board
        parent = _make_accepted_parent(board)
        report_event, acceptance_event = _find_events(parent)

        ctx = create_followup_context(
            board, parent, report_event.id, acceptance_event.id,
            selected_refs=["summary", "checks"],
            new_title="后续", new_goal="补齐",
        )
        child = board.create_followup(parent.id, ctx, "后续任务", "补齐边界用例")
        # child.description is the user goal, not the context snapshot
        self.assertEqual(child.description, "补齐边界用例")
        # Context snapshot lives in registry
        stored_ctx = board.task_contexts[ctx.id]
        self.assertEqual(stored_ctx.source_snapshot, ctx.source_snapshot)
        # The snapshot should contain the selected refs
        self.assertIn("成果摘要", ctx.source_snapshot)
        self.assertIn("核验依据", ctx.source_snapshot)

    def test_context_refs_in_dispatched_requirements(self):
        """Selected refs from context MUST appear when compiled into requirements."""
        global _last_board
        board = _make_live_board()
        _last_board = board
        parent = _make_accepted_parent(board)
        report_event, acceptance_event = _find_events(parent)

        ctx = create_followup_context(
            board, parent, report_event.id, acceptance_event.id,
            selected_refs=["summary", "checks"],
            new_title="后续", new_goal="补齐",
        )
        child = board.create_followup(parent.id, ctx, "后续任务", "补齐边界用例")
        # Set a contract as remaining
        contract = WorkContract(
            purpose="implementation",
            deliverable="补齐边界用例",
            allowed_scope="中文样本目录",
            verification="回归测试通过",
            exclusions="不修改金融模块",
        )
        child.remaining = contract.compile()
        # The compiled requirements should contain the contract
        self.assertIn("补齐边界用例", child.remaining)
        # child.description is the user goal
        self.assertEqual(child.description, "补齐边界用例")
        # Context snapshot is in registry
        stored_ctx = board.task_contexts[ctx.id]
        self.assertIn("成果摘要", stored_ctx.source_snapshot)


class TaskContextFromRecordTests(unittest.TestCase):
    """TaskContext.from_record strict validation."""

    def test_from_record_rejects_extra_keys(self):
        """Extra keys MUST be rejected."""
        from task_context import TaskContext
        row = {
            "id": str(uuid4()),
            "child_task_id": 1,
            "parent_task_id": 2,
            "source_run_id": str(uuid4()),
            "source_report_event_id": str(uuid4()),
            "source_report_seq": 1,
            "acceptance_event_id": str(uuid4()),
            "cwd": "/test",
            "source_digest": "a" * 64,
            "selected_refs": [],
            "source_snapshot": "",
            "created_at": "2026-01-01T00:00:00.000Z",
            "extra_key": "bad",
        }
        with self.assertRaises(ValueError):
            TaskContext.from_record(row)

    def test_from_record_rejects_bool_as_int(self):
        """Boolean values MUST NOT be accepted as ints."""
        from task_context import TaskContext
        row = {
            "id": str(uuid4()),
            "child_task_id": True,  # bool, not int
            "parent_task_id": 2,
            "source_run_id": str(uuid4()),
            "source_report_event_id": str(uuid4()),
            "source_report_seq": 1,
            "acceptance_event_id": str(uuid4()),
            "cwd": "/test",
            "source_digest": "a" * 64,
            "selected_refs": [],
            "source_snapshot": "",
            "created_at": "2026-01-01T00:00:00.000Z",
        }
        with self.assertRaises(ValueError):
            TaskContext.from_record(row)

    def test_from_record_rejects_bad_digest(self):
        """Invalid digest format MUST be rejected."""
        from task_context import TaskContext
        row = {
            "id": str(uuid4()),
            "child_task_id": 1,
            "parent_task_id": 2,
            "source_run_id": str(uuid4()),
            "source_report_event_id": str(uuid4()),
            "source_report_seq": 1,
            "acceptance_event_id": str(uuid4()),
            "cwd": "/test",
            "source_digest": "not-a-valid-digest",
            "selected_refs": [],
            "source_snapshot": "",
            "created_at": "2026-01-01T00:00:00.000Z",
        }
        with self.assertRaises(ValueError):
            TaskContext.from_record(row)


if __name__ == "__main__":
    unittest.main()
