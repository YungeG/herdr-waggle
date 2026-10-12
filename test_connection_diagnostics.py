"""N7 bounded diagnostics consume existing strict v2 proof, never probe or mutate."""
import json
import os
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from tempfile import TemporaryDirectory

from runtime import BridgeError, Runtime, private_dir
from test_capabilities import capability_record
from test_failure_view import failed_board


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory(prefix='waggle-n7-')))
        self.board, self.task, self.source_run, self.agent, self.session = failed_board(str(self.root))
        self.agent = replace(self.agent, pid=os.getpid())
        self.runtime = Runtime(self.root / 'bridges')
        self.endpoint = self.runtime.root / self.agent.id
        for path in (self.runtime.root, self.endpoint, self.endpoint / 'runs'):
            private_dir(path, create=True)
        self.put('agent.json', {'version': 1, **asdict(self.agent)})
        self.put('session.json', {'version': 1, **asdict(self.session)})

    def put(self, name, row):
        path = self.endpoint / name
        path.write_text(json.dumps(row), encoding='utf-8')
        path.chmod(0o600)

    def test_existing_v2_reasons_are_readonly_and_bounded(self):
        self.assertEqual(self.runtime.report_capability_diagnostic(self.agent).code, 'missing')
        initial = capability_record(self.agent)
        for changes, expected in [({}, 'ready'), ({'active': False}, 'inactive'),
                                  ({'schema_sha256': ''}, 'schema_unconfirmed'),
                                  ({'features': ['report-v1']}, 'features_missing'),
                                  ({'observed_at': 1, 'verified_at': 1}, 'stale_proof'),
                                  ({'verified_at': 1}, 'stale_probe'),
                                  ({'session_id': 'other-session'}, 'identity_mismatch'),
                                  ({'extra_wire_field': 'never accepted'}, 'invalid_proof'),
                                  ({'callable': False, 'transport': 'unknown', 'ref': '', 'verified_at': 0, 'boundary': ''}, 'callability_unconfirmed')]:
            with self.subTest(expected=expected):
                self.put('report-capabilities.json', initial | changes)
                before = {str(path): path.read_bytes() for path in self.endpoint.rglob('*') if path.is_file()}
                result = self.runtime.report_capability_diagnostic(self.agent)
                self.assertEqual(result.code, expected)
                self.assertLess(len(result.describe()), 2200)
                after = {str(path): path.read_bytes() for path in self.endpoint.rglob('*') if path.is_file()}
                self.assertEqual(before, after)
                if expected == 'ready':
                    self.runtime.check_report_capabilities(self.agent)
                else:
                    with self.assertRaises(BridgeError):
                        self.runtime.check_report_capabilities(self.agent)
                self.assertFalse(list(self.endpoint.glob('commands/*')))
        self.assertIn('现有 v2 不携带细分原因', result.uncertainty)

    def test_prior_round_gate_requires_exact_fresh_idle_bound_and_control(self):
        self.runtime.check_new_round(self.source_run)
        for changes in [{'state': 'working', 'linked_run_id': '', 'following': False},
                        {'state': 'offline'}, {'input_pending': True},
                        {'linked_run_id': '', 'following': False}, {'observed_at': 1}]:
            self.put('session.json', {'version': 1, **asdict(replace(self.session, **changes))})
            with self.assertRaises(BridgeError):
                self.runtime.check_new_round(self.source_run)
        (self.endpoint / 'session.json').unlink()
        with self.assertRaises(BridgeError):
            self.runtime.check_new_round(self.source_run)
        self.put('session.json', {'version': 1, **asdict(self.session)})
        self.runtime.observe(self.source_run, self.runtime.root)
        with self.assertRaisesRegex(BridgeError, '只读观测'):
            self.runtime.check_new_round(self.source_run)


if __name__ == '__main__':
    unittest.main()
