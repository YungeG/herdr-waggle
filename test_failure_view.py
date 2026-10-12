"""N2 safety checks use only explicit fixtures and disposable local records."""
import json
import time
import unittest
from copy import deepcopy
from dataclasses import asdict, replace
from uuid import uuid4

from failure_view import (
    RECOVERY_END,
    RECOVERY_MARKER,
    project_failure,
    project_ownership,
)
from journal import Report, current_report, encoded_event, last_progress_report
from model import Board
from runtime import Agent, SessionObservation
from store import decode, encode
from task_context import WorkContract


def failed_board(cwd='/tmp'):
    board = Board(live=True)
    board.add_project('fixture', cwd)
    task = board.create('recover explicit fixture', 'fixture', 'Only check fixture outputs')
    agent = Agent(str(uuid4()), str(uuid4()), cwd, 'fixture', 'offline/fixture', 1,
                  int(time.time() * 1000), True, True, '')
    board.set_work_contract(task, WorkContract('implementation', 'Check fixture', cwd, 'Fixture assertions', 'No real data'))
    board.assign(task, agent.id)
    run = board.queue_real(task, str(uuid4()), agent.session_id, cwd)
    progress = encoded_event('progress', 'work done before fault', source='executor', run_id=run.id, seq=1,
                             report=Report('Saved intermediate fixture', ['self-reported file'], 'Check pending output'))
    failure = encoded_event('failed', 'auth_unavailable: route failed: server_is_overloaded', source='system', run_id=run.id, seq=2)
    board.ingest(replace(run, state='failed', seq=2, delivery='progress', summary='auth_unavailable: server_is_overloaded',
                            remaining='Check pending output', events=[progress, failure]))
    run = board.runs[run.id]
    session = SessionObservation(agent.id, agent.session_id, cwd, 1, agent.heartbeat, 'idle', '', run.id, True, False)
    return board, task, run, agent, session


class FailureSentinel(unittest.TestCase):
    def test_progress_is_readable_but_failure_cannot_supply_acceptance_or_decision_source(self):
        board, task, run, _, _ = failed_board()
        before = encode(board)
        event = last_progress_report(run)
        assert event is not None and event.report is not None
        self.assertEqual(event.report.summary, 'Saved intermediate fixture')
        self.assertIsNone(current_report(task, run))
        self.assertFalse(task.can_accept)
        failure = project_failure(run)
        self.assertEqual(failure.category, 'provider_overload')
        self.assertIn('路由不可用', failure.uncertainty)
        self.assertNotIn('密钥', failure.recommended_next_step)
        self.assertEqual(failure.evidence, 'auth_unavailable: route failed: server_is_overloaded')
        self.assertEqual(encode(board), before)

    def test_busy_unbound_session_has_no_dispatch_action(self):
        _, task, run, agent, session = failed_board()
        view = project_ownership(task, run, replace(agent, idle=False), replace(session, state='working', linked_run_id='', following=False))
        self.assertEqual(view.ownership, 'unbound')
        self.assertIn('prepare_recovery', view.allowed_actions)
        self.assertNotIn('send_new_round', view.allowed_actions)
        self.assertIn('核实原 Pi 当前活动', view.recommended_next_step)


class RecoveryTests(unittest.TestCase):
    def test_recovery_saves_source_and_contract_without_altering_old_run(self):
        board, task, run, agent, _ = failed_board()
        old = deepcopy(asdict(run))
        count = len(board.runs)
        contract = WorkContract('evidence', 'Check saved fixture', '/tmp', 'Inspect existing fixture only', 'No repeat tool calls')
        board.prepare_recovery(task, deepcopy(run), 'Only inspect saved file', 'Verify incomplete output', contract)
        self.assertEqual(asdict(board.runs[run.id]), old)
        self.assertEqual(len(board.runs), count)
        self.assertEqual(task.run, 'none')
        self.assertFalse(task.can_accept)
        preview = board.compiled_requirements(task)
        self.assertIn(run.id, preview)
        self.assertIn('Only inspect saved file', preview)
        roundtrip = decode(encode(board))
        self.assertEqual(roundtrip.compiled_requirements(roundtrip.tasks[task.id]), preview)
        new = board.queue_real(task, str(uuid4()), agent.session_id, agent.cwd)
        self.assertNotEqual(new.id, run.id)
        self.assertEqual(new.requirements, preview)
        self.assertEqual(asdict(board.runs[run.id]), old)
        with self.assertRaises(ValueError):
            board.queue_real(task, str(uuid4()), agent.session_id, agent.cwd)

    def test_source_drift_and_oversize_recovery_are_atomic(self):
        board, task, run, _, _ = failed_board()
        original = encode(board)
        changed = replace(run, summary='Changed source')
        contract = WorkContract('evidence', 'Inspect fixture', '/tmp', 'Assertions', 'No experiments')
        with self.assertRaises(ValueError):
            board.prepare_recovery(task, changed, 'Checkpoint', 'Remainder', contract)
        self.assertEqual(encode(board), original)
        with self.assertRaises(ValueError):
            board.prepare_recovery(task, run, 'x' * 1801, 'Remainder', contract)
        self.assertEqual(encode(board), original)
        board.prepare_recovery(task, deepcopy(run), 'Checkpoint', 'Remainder', contract)
        board.runs[run.id].events[-1] = encoded_event('failed', 'Tampered raw error', source='system', run_id=run.id, seq=2)
        before_queue = encode(board)
        with self.assertRaises(ValueError):
            board.queue_real(task, str(uuid4()), str(uuid4()), '/tmp')
        self.assertEqual(encode(board), before_queue)

    def test_inventory_cannot_turn_original_implementation_into_full_recovery(self):
        board, task, run, _, _ = failed_board()
        before = encode(board)
        with self.assertRaisesRegex(ValueError, '独立成果盘点'):
            board.prepare_recovery(task, run, 'Inspect', 'Check', WorkContract('inventory', 'Inventory', '/tmp', 'Listing', 'No implementation'))
        self.assertEqual(encode(board), before)
        board.prepare_recovery(task, run, 'Inspect', 'Check', WorkContract('evidence', 'Check', '/tmp', 'Listing', 'No implementation'))
        saved = encode(board)
        with self.assertRaisesRegex(ValueError, '盘点'):
            board.set_work_contract(task, WorkContract('inventory', 'Inventory', '/tmp', 'Listing', 'No implementation'))
        self.assertEqual(encode(board), saved)

    def test_malformed_source_types_fail_as_value_errors(self):
        board, task, run, _, _ = failed_board()
        board.prepare_recovery(task, run, 'Inspect', 'Check', WorkContract('evidence', 'Check', '/tmp', 'Assertions', 'No writes'))
        base, _, tail = task.remaining.partition(RECOVERY_MARKER)
        row = json.loads(tail.partition(RECOVERY_END)[0])
        for key, value in [('source_run_id', []), ('source_run_id', {}), ('source_run_id', True),
                           ('source_report_seq', True), ('source_seq', False), ('source_digest', []), ('cwd', 1)]:
            with self.subTest(key=key, value=value):
                candidate = base + RECOVERY_MARKER + json.dumps(row | {key: value}, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + RECOVERY_END
                with self.assertRaises(ValueError):
                    board.compiled_requirements(task, candidate)

    def test_ownership_identity_and_freshness_never_grant_permission(self):
        _, task, run, agent, session = failed_board()
        self.assertIn('send_new_round', project_ownership(task, run, agent, session).allowed_actions)
        for changed_agent, changed_session, code in (
            (replace(agent, session_id=str(uuid4())), session, 'mismatch'),
            (agent, replace(session, observed_at=1), 'unknown'),
            (agent, None, 'unknown'),
            (agent, replace(session, linked_run_id='', following=False), 'unbound'),
            (replace(agent, online=False), session, 'unknown'),
            (agent, replace(session, input_pending=True), 'bound'),
        ):
            view = project_ownership(task, run, changed_agent, changed_session)
            self.assertEqual(view.ownership, code)
            self.assertNotIn('send_new_round', view.allowed_actions)


if __name__ == '__main__':
    unittest.main()
