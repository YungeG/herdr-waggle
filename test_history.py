"""Key history contracts through Board/Store and disposable runtime records."""
from copy import deepcopy
from dataclasses import asdict
from datetime import UTC, datetime
import json
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from model import Board, Run
from store import StorageError, Store
from test_runtime import prepared_board


PREFIX = "taskroom:event:1 "


def event_record(run_id="", seq=0):
    return {"id": str(uuid4()), "at": "2026-06-01T12:34:56.789Z", "kind": "progress",
            "source": "executor", "run_id": run_id, "seq": seq, "text": "执行者报告 · progress · 第一份报告",
            "report": {"summary": "当时的完整正文\n不是最新正文。", "checks": ["当时的依据"], "remaining": "当时未完成的项目",
                       "brief": {"actions": ["核对已保存文档"], "outcome": "发现待补项", "verification": "未运行测试"}}}


def wire_event(record):
    return PREFIX + json.dumps(record, ensure_ascii=False, separators=(",", ":"))


class HistoryStorage(unittest.TestCase):
    def test_created_event_keeps_its_source_time_and_identity_across_reopens(self):
        before = datetime.now(UTC)
        board = Board(live=True)
        task = board.create("保留发生时间", "收集箱")
        after = datetime.now(UTC)
        original = task.events[-1]
        self.assertTrue(original.startswith("taskroom:event:1 "), "New human actions must persist their event time, not a timeless sentence.")
        record = json.loads(original.removeprefix("taskroom:event:1 "))
        self.assertEqual((record["kind"], record["source"], record["run_id"], record["seq"]), ("created", "human", "", 0))
        self.assertTrue(record["at"].endswith("Z"))
        recorded = datetime.fromisoformat(record["at"])
        # The public event format stores milliseconds, not sub-millisecond clock precision.
        self.assertLessEqual(before.replace(microsecond=before.microsecond // 1000 * 1000), recorded)
        self.assertLessEqual(recorded, after)
        with TemporaryDirectory(prefix="taskroom-history-test-") as temporary:
            path = Path(temporary) / "tasks.sqlite3"
            with Store(path) as store:
                store.load()
                store.save(board)
            for _ in range(2):
                with Store(path) as store:
                    self.assertEqual(store.load().tasks[task.id].events, [original])

    def test_malformed_or_unknown_reserved_events_reject_save_without_overwriting(self):
        with TemporaryDirectory(prefix="taskroom-history-test-") as temporary:
            path = Path(temporary) / "tasks.sqlite3"
            with Store(path) as store:
                board = store.load()
                task = board.create("不能破坏历史", "收集箱")
                store.save(board)
                original = path.read_bytes()
                created = json.loads(task.events[0].removeprefix(PREFIX))
                invalid = ["taskroom:event:2 {}", PREFIX + "not json", PREFIX + "{}"]
                for field, value in [("at", "2026-06-01T12:34:56"), ("at", "2026-02-30T00:00:00.000Z"),
                                     ("id", "not-a-uuid"), ("source", "model-clock"), ("seq", True),
                                     ("seq", -1), ("text", []), ("extra", "unknown")]:
                    invalid.append(wire_event(created | {field: value}))
                report = event_record()
                invalid.extend([wire_event(report | {"report": {"summary": "missing fields"}}),
                                wire_event(report | {"report": report["report"] | {"brief": {"actions": [], "outcome": "", "verification": ""}}}),
                                wire_event(report | {"report": report["report"] | {"checks": ["x"] * 33}}),
                                wire_event(report | {"report": report["report"] | {"summary": "x" * 6001}})])
                for value in invalid:
                    with self.subTest(value=value[:90]):
                        candidate = deepcopy(board)
                        candidate.tasks[task.id].events.append(value)
                        with self.assertRaises(StorageError):
                            store.save(candidate)
                        self.assertEqual(path.read_bytes(), original)
                        self.assertEqual(store.load().tasks[task.id].events, task.events)

    def test_report_snapshot_roundtrip_and_run_binding_are_strict(self):
        board = Board(live=True)
        board.add_project("内存项目", "/virtual/not-accessed")
        task = board.create("报告不是最后结果的别名", "内存项目")
        board.assign(task, str(uuid4()))
        queued = board.queue_real(task, str(uuid4()), str(uuid4()), "/virtual/not-accessed")
        result = deepcopy(queued)
        result.state, result.seq, result.delivery = "working", 2, "progress"
        old = event_record(result.id, 1)
        result.events = [wire_event(old)]
        result.summary, result.checks, result.remaining = "最新正文", ["最新依据"], "最新剩余项"
        self.assertTrue(board.ingest(result))
        with TemporaryDirectory(prefix="taskroom-history-test-") as temporary:
            path = Path(temporary) / "tasks.sqlite3"
            with Store(path) as store:
                store.load()
                store.save(board)
                restored = store.load()
                self.assertEqual(json.loads(restored.runs[result.id].events[0].removeprefix(PREFIX)), old)
                self.assertNotEqual(old["report"]["summary"], restored.runs[result.id].summary)
                for change in ({"run_id": str(uuid4())}, {"seq": 3}, {"source": "human"},
                               {"kind": "accepted", "source": "system", "report": None},
                               {"kind": "warning", "report": None},
                               {"report": old["report"] | {"checks": [" "]}},
                               {"kind": "partial", "report": old["report"] | {"remaining": ""}}):
                    with self.subTest(change=change):
                        row = asdict(result)
                        row["events"] = [wire_event(old | change)]
                        with self.assertRaises(ValueError):
                            Run.from_record(row)
                candidate = deepcopy(board)
                candidate.tasks[task.id].events.append(wire_event(event_record(str(uuid4()), 1)))
                before = path.read_bytes()
                with self.assertRaises(StorageError):
                    store.save(candidate)
                self.assertEqual(path.read_bytes(), before)

    def test_human_actions_bind_to_the_right_round_and_keep_revision_anchors(self):
        board, task, run = prepared_board()
        dispatched = json.loads(task.events[-1].removeprefix(PREFIX))
        self.assertEqual((dispatched["kind"], dispatched["run_id"], dispatched["seq"]), ("dispatched", run.id, 0))
        result = deepcopy(run)
        result.seq, result.state, result.delivery = 5, "finished", "full"
        result.summary, result.checks = "已完成", ["自报依据"]
        board.ingest(result)
        accepted = deepcopy(board)
        accepted.accept(accepted.tasks[task.id])
        acceptance = json.loads(accepted.tasks[task.id].events[-1].removeprefix(PREFIX))
        self.assertEqual((acceptance["kind"], acceptance["run_id"], acceptance["seq"]), ("accepted", run.id, 5))
        board.return_work(task, "请补录出处")
        returned = json.loads(task.events[-1].removeprefix(PREFIX))
        self.assertEqual((returned["kind"], returned["run_id"], returned["seq"]), ("returned", run.id, 5))
        board.edit(task, task.title, task.project, "下一轮说明")
        edited = json.loads(task.events[-1].removeprefix(PREFIX))
        self.assertEqual((edited["kind"], edited["run_id"], edited["seq"]), ("edited", "", 0))
        self.assertFalse(any("人工补充" in value for value in board.runs[run.id].events), "Human records must not rewrite bridge-owned history.")

    def test_task_history_retention_is_bounded_and_explicit(self):
        board = Board(live=True)
        task = board.create("长期任务", "收集箱")
        for number in range(145):
            board.edit(task, task.title, task.project, f"第 {number} 次补充")
        self.assertLessEqual(len(task.events), 120)
        self.assertLessEqual(len(json.dumps(task.events, ensure_ascii=False).encode()), 384 * 1024)
        records = [json.loads(value.removeprefix(PREFIX)) for value in task.events]
        self.assertEqual(sum(record["kind"] == "history_gap" for record in records), 1)
        self.assertEqual(records[-1]["kind"], "edited")
        with TemporaryDirectory(prefix="taskroom-history-test-") as temporary:
            with Store(Path(temporary) / "tasks.sqlite3") as store:
                store.load()
                store.save(board)
                self.assertEqual(store.load().tasks[task.id].events, task.events)

    def test_ingestion_cannot_replace_or_remove_the_original_session_record(self):
        board, _, run = prepared_board()
        incoming = deepcopy(run)
        incoming.seq, incoming.state = 1, "working"
        origin = event_record(run.id, 1) | {"kind": "origin", "source": "system", "text": "/virtual/original.jsonl", "report": None}
        incoming.events = [wire_event(origin)]
        board.ingest(incoming)
        before = deepcopy(board.runs[run.id])
        for events in ([], [wire_event(origin | {"text": "/virtual/wrong-session.jsonl"})]):
            later = deepcopy(incoming)
            later.seq, later.events = 2, events
            with self.subTest(events=events):
                with self.assertRaises(ValueError):
                    board.ingest(later)
                self.assertEqual(board.runs[run.id], before)

    @unittest.skipUnless(shutil.which("node"), "Node required to exercise the actual bridge journal producer")
    def test_bridge_producer_retains_origin_and_report_with_unicode_byte_bounds(self):
        producer = Path(__file__).with_name("journal.ts").as_uri()
        script = f"""
import {{ appendKeyEvent }} from {json.dumps(producer)};
import fs from 'node:fs';
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const run = input.run;
run.events = appendKeyEvent([], 'origin', '/virtual/original.jsonl', run.id, ++run.seq);
run.events = appendKeyEvent(run.events, 'full', '执行者报告 · full', run.id, ++run.seq, input.report);
const protectedRecords = [...run.events];
for (let index = 0; index < input.count; index++) {{
    run.events = appendKeyEvent(run.events, 'warning', input.body, run.id, ++run.seq);
}}
run.state = 'finished'; run.delivery = 'full';
run.summary = input.report.summary; run.checks = input.report.checks; run.remaining = '';
console.log(JSON.stringify({{run, protectedRecords}}));
"""
        # Representative small case first, then pathological escaping and pruning.
        for count, size in ((4, 10), (145, 1000)):
            with self.subTest(count=count):
                board, _, run = prepared_board()
                report = {"summary": "\0" * (size * 6 - 1) + "尾", "checks": ["\0" * (size - 1) + "据"] * 32,
                          "remaining": "", "brief": {"actions": ["检查隔离输入"], "outcome": "仅测试记录边界", "verification": "未运行实际项目测试"}}
                process = subprocess.run(["node", "--input-type=module", "-e", script],
                                         input=json.dumps({"run": asdict(run), "report": report, "body": "\0" * (size * 6), "count": count}),
                                         capture_output=True, text=True, timeout=15, check=True)
                output = json.loads(process.stdout)
                result = Run.from_record(output["run"])
                self.assertTrue(all(value in result.events for value in output["protectedRecords"]))
                self.assertLessEqual(len(result.events), 120)
                self.assertLessEqual(len(json.dumps(result.events, ensure_ascii=False).encode()), 384 * 1024)
                self.assertLessEqual(len(json.dumps(asdict(result), ensure_ascii=False).encode()), 1024 * 1024)
                self.assertEqual(any("history_gap" in value for value in result.events), count > 120)
                self.assertTrue(board.ingest(result))
                with TemporaryDirectory(prefix="taskroom-history-test-") as temporary:
                    with Store(Path(temporary) / "tasks.sqlite3") as store:
                        store.load()
                        store.save(board)
                        self.assertEqual(store.load().runs[run.id].events, result.events)


if __name__ == "__main__":
    unittest.main()
