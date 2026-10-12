"""Optional activity is bounded, private observation, never task authority."""
import json
import os
import subprocess
import unittest
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from activity import Activity
from model import Run
from runtime import Runtime, private_dir, publish


def activity_record(run: Run, now: int) -> dict:
    return {"version": 1, "run_id": run.id, "agent_id": run.agent_id, "session_id": run.session_id,
            "cwd": run.cwd, "seq": 1, "observed_at": now, "bound": True,
            "tools": [{"id": "call-1", "name": "read", "started_at": now - 1000}],
            "last_tool": None, "children": [], "children_observed_at": None, "overflow": False}


class ActivityTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(prefix="waggle-activity-test-")
        self.addCleanup(temp.cleanup)
        self.runtime = Runtime(Path(temp.name) / "bridges")
        self.attempt = Run(str(uuid4()), 1, str(uuid4()), str(uuid4()), temp.name, "task", "scope", "")
        self.endpoint = self.runtime.root / self.attempt.agent_id
        for directory in (self.runtime.root, self.endpoint):
            private_dir(directory, create=True)
        self.file = self.endpoint / "activity.json"
        self.now = 1_800_000_000_000
        self.record = activity_record(self.attempt, self.now)

    def test_optional_file_roundtrip_and_no_state_mutation(self):
        before = asdict(self.attempt)
        self.assertIsNone(self.runtime.read_activity(self.attempt))
        publish(self.file, self.record)
        original = self.file.read_bytes()
        result = self.runtime.read_activity(self.attempt)
        assert result
        self.assertEqual(result.tools[0].name, "read")
        self.assertEqual(result.tools[0].started_at, self.now - 1000)
        self.assertEqual(asdict(self.attempt), before)
        self.assertEqual(self.file.read_bytes(), original)

    def test_strict_identity_schema_types_bounds_and_event_times(self):
        cases = [{"version": 2}, {"seq": True}, {"seq": 0}, {"bound": 1}, {"observed_at": float("nan")},
                 {"run_id": str(uuid4())}, {"agent_id": str(uuid4())}, {"session_id": str(uuid4())},
                 {"cwd": "/different"}, {"args": "must not be exported"},
                 {"tools": self.record["tools"] * 9}, {"tools": self.record["tools"] * 2},
                 {"children": [{"id": "child", "state": "guessed"}]},
                 {"children_observed_at": self.now + 3000},
                 {"tools": [{"id": "call", "name": "secret\ncontrol", "started_at": self.now}]},
                 {"tools": [{"id": "call", "name": "read", "started_at": self.now + 3000}]},
                 {"last_tool": {"id": "old", "name": "bash", "started_at": self.now, "ended_at": self.now - 1, "is_error": False}}]
        for change in cases:
            with self.subTest(change=change):
                with self.assertRaises(ValueError):
                    Activity.from_record({**self.record, **change}, self.attempt)

    def test_replay_or_equal_revision_rewrite_does_not_replace_observation(self):
        first = Activity.from_record(self.record, self.attempt)
        later = Activity.from_record({**self.record, "seq": 2, "observed_at": self.now + 1000}, self.attempt)
        later.check_after(first)
        first.check_after(first)
        with self.assertRaises(ValueError):
            first.check_after(later)
        rewrite = Activity.from_record({**self.record, "tools": []}, self.attempt)
        with self.assertRaises(ValueError):
            rewrite.check_after(first)

    def test_stale_unbound_or_read_failure_never_claims_current_tool(self):
        snapshot = Activity.from_record(self.record, self.attempt)
        self.assertIn("工具 read", snapshot.describe(self.now))
        self.assertIn("开始", snapshot.describe(self.now))
        for now, issue in ((self.now + 11000, ""), (self.now - 3000, ""), (self.now, "invalid")):
            text = snapshot.describe(now, issue=issue)
            self.assertIn("当前未知", text)
            self.assertIn("上次观测", text)
            self.assertNotIn("工具 read", text)
        invalidated = Activity.from_record({**self.record, "bound": False}, self.attempt)
        self.assertIn("归属未确认", invalidated.describe(self.now))

    def test_child_unknown_is_not_zero_or_completion_and_tool_error_is_not_run_failure(self):
        record = deepcopy(self.record)
        record.update(tools=[], last_tool={"id": "done", "name": "bash", "started_at": self.now - 500,
                      "ended_at": self.now, "is_error": True}, children=[{"id": "owned", "state": "unknown"}])
        snapshot = Activity.from_record(record, self.attempt)
        text = snapshot.describe(self.now)
        self.assertIn("工具 bash 返回失败", text)
        self.assertIn("后台执行", text)
        self.assertIn("未知", text)
        self.assertNotIn("执行失败", text)
        self.assertNotIn("已完成", text)

    def test_producer_caps_privacy_and_late_callback_after_ownership_loss(self):
        module = Path(__file__).with_name("activity.ts").as_uri()
        script = "import { observeActivity } from " + json.dumps(module) + ";\n" + """
import fs from 'node:fs';
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
let owner = input.run, latest, maxBytes = 0, peakTools = 0, peakChildren = 0;
const original = JSON.stringify(owner), hooks = new Map(), listeners = new Map(), requests = [];
const pi = { on: (name, handler) => hooks.set(name, handler), events: {
  on: (name, handler) => { listeners.set(name, handler); return () => listeners.delete(name); },
  emit: (name, request) => requests.push({name, request})
}};
const observer = observeActivity(pi, () => owner, snapshot => {
  const raw = JSON.stringify(snapshot);
  maxBytes = Math.max(maxBytes, Buffer.byteLength(raw)); latest = JSON.parse(raw);
  peakTools = Math.max(peakTools, snapshot.tools.length);
  peakChildren = Math.max(peakChildren, snapshot.children.length);
});
for (let i = 0; i < input.count; i++) hooks.get('tool_execution_start')({
  toolCallId: `tool-${i}`, toolName: 'read', args: {secret: 'PRIVATE-ARG'}
});
for (let i = 0; i < input.count; i++) hooks.get('tool_execution_end')({
  toolCallId: `tool-${i}`, toolName: 'read', result: {text: 'PRIVATE-OUTPUT'}, isError: false
});
for (let i = 0; i < input.count; i++) {
  hooks.get('tool_execution_start')({ toolCallId: `create-${i}`, toolName: 'subagent', args: {agent: 'fake', task: 'PRIVATE-TASK'} });
  hooks.get('tool_execution_end')({ toolCallId: `create-${i}`, toolName: 'subagent', isError: false,
    result: {details: {runId: `child-${i}`, asyncId: `child-${i}`}} });
}
const before = latest, pending = requests[0].request;
const late = listeners.get(`subagents:rpc:v1:reply:${pending.requestId}`);
const unchanged = original === JSON.stringify(owner);
owner = undefined; observer.invalidate();
const invalidated = latest;
late({ version: 1, requestId: pending.requestId, success: true, data: {asyncSnapshot: {
  kind: 'pi-subagents.async-status-snapshot', version: 1, generatedAt: Date.now(),
  runs: [{id: 'child-0', state: 'complete'}]
}} });
console.log(JSON.stringify({before, invalidated, maxBytes, peakTools, peakChildren, unchanged,
  lateChanged: JSON.stringify(invalidated) !== JSON.stringify(latest), requests}));
"""
        for count, escaped_path in ((2, False), (12, False), (2, True)):
            with self.subTest(count=count, escaped_path=escaped_path):
                owner = asdict(self.attempt)
                if escaped_path:
                    owner["cwd"] = "/" + ("\x01" * 220 + "/") * 14
                process = subprocess.run(["node", "--input-type=module", "-e", script],
                    input=json.dumps({"run": owner, "count": count}), capture_output=True, text=True, timeout=10, check=True)
                self.assertNotIn("PRIVATE", process.stdout)
                result = json.loads(process.stdout)
                if escaped_path:
                    self.assertNotIn("before", result)
                    self.assertEqual(result["maxBytes"], 0)
                    self.assertTrue(result["unchanged"])
                    continue  # An oversized optional snapshot is not published, never truncated.
                snapshot = Activity.from_record(result["before"], self.attempt)
                self.assertEqual(snapshot.overflow, count > 8)
                self.assertLessEqual(result["maxBytes"], 16 * 1024)
                self.assertEqual((result["peakTools"], result["peakChildren"]), (min(8, count), min(8, count)))
                self.assertFalse(result["lateChanged"])
                self.assertFalse(result["invalidated"]["bound"])
                self.assertTrue(result["unchanged"])
                self.assertEqual([row["request"]["method"] for row in result["requests"]], ["status"])

    def test_optional_file_rejects_symlink_fifo_permissions_and_oversize(self):
        target = self.endpoint / "target"
        publish(target, self.record)
        self.file.symlink_to(target)
        with self.assertRaises((ValueError, OSError)):
            self.runtime.read_activity(self.attempt)
        self.file.unlink()
        os.mkfifo(self.file, 0o600)
        with self.assertRaises(ValueError):
            self.runtime.read_activity(self.attempt)
        self.file.unlink()
        self.file.write_bytes(b" " * (16 * 1024 + 1))
        self.file.chmod(0o600)
        with self.assertRaises(ValueError):
            self.runtime.read_activity(self.attempt)
        self.file.unlink()
        publish(self.file, self.record)
        self.file.chmod(0o644)
        with self.assertRaises(ValueError):
            self.runtime.read_activity(self.attempt)
        self.assertEqual(asdict(self.attempt)["seq"], 0)
