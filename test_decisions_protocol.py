"""Frozen malformed/Unicode fixtures through both real journal codecs, no Pi/network."""

import json
import shutil
import subprocess
import unittest
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from decisions import DecisionSpec
from journal import Report, encoded_event, parse_event
from test_decisions import routing_payload, spec_record

PREFIX = "waggle:event:1 "


class NewDecisionProducerContract(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node required for new-report producer validation")
    def test_new_reports_require_routing_while_legacy_journal_reader_stays_compatible(self):
        script = f"""
import {{ parseDecisions, parseNewDecisions, decisionSchema }} from {json.dumps(Path(__file__).with_name('decisions.ts').as_uri())};
import fs from 'node:fs';
const [old, current] = JSON.parse(fs.readFileSync(0, 'utf8'));
const accepts = (fn, value) => {{ try {{ fn(value); return true }} catch {{ return false }} }};
console.log(JSON.stringify({{ schemaRequired: decisionSchema.items.required.includes('routing'),
    oldReadable: accepts(parseDecisions, [old]), oldProduced: accepts(parseNewDecisions, [old]),
    newProduced: accepts(parseNewDecisions, [current]) }}));
"""
        old = spec_record()
        current = spec_record(missing=["原始回执缺失"], routing={
            "audience": "owner_evidence", "evidence_needs": [{"gap": "原始回执缺失", "owner": "回测负责人", "source": "原始回执"}],
            "advice": {"status": "insufficient", "value": "", "unit": "", "conditions": "", "evidence": [],
                       "uncertainty": "未拿到回执", "owner_signed": False}})
        result = subprocess.run(["node", "--input-type=module", "-e", script], input=json.dumps([old, current]),
                                capture_output=True, text=True, check=True, timeout=15)
        self.assertEqual(json.loads(result.stdout), {"schemaRequired": True, "oldReadable": True,
                                                      "oldProduced": False, "newProduced": True})


class DecisionProtocol(unittest.TestCase):
    @unittest.skipUnless(
        shutil.which("node"), "Node required for actual TypeScript codec parity"
    )
    def test_python_typescript_accept_reject_and_roundtrip_parity(self):
        run_id = str(uuid4())
        base = json.loads(
            encoded_event(
                "partial",
                "fixture",
                source="executor",
                run_id=run_id,
                seq=1,
                report=Report(
                    "中文报告",
                    ["隔离证据"],
                    "等待决定",
                    decisions=(DecisionSpec.from_record(spec_record()),),
                ),
            ).removeprefix(PREFIX)
        )
        cases = []

        def add(name, value, valid):
            cases.append((name, PREFIX + json.dumps(value, ensure_ascii=True), valid))

        for kind in ("choice", "approval", "budget", "preference"):
            row = deepcopy(base)
            row["report"]["decisions"] = [spec_record(kind)]
            add(kind, row, True)
        row = deepcopy(base)
        row["report"]["decisions"][0]["human_input"] = {
            "kind": "choice", "question": "未结清是否反转？", "options": [
                {"key": "yes", "label": "需要"}, {"key": "no", "label": "不需要"},
                {"key": "conditional", "label": "视条件而定"}, {"key": "unknown", "label": "暂不确定"}]}
        add("independent business question", row, True)
        row = deepcopy(base)
        row["report"]["decisions"] = [spec_record(missing=["原始回执缺失"], routing=routing_payload())]
        add("v3 owner evidence and insufficient advice", row, True)
        row = deepcopy(base)
        row["report"]["decisions"] = [spec_record(routing=routing_payload("user_decision", "", "supported"))]
        add("v3 user decision supported recommendation", row, True)
        for change in (lambda r: r["routing"].update(audience="unknown"),
                       lambda r: r["routing"]["evidence_needs"][0].update(owner=""),
                       lambda r: r["routing"]["evidence_needs"][0].update(source=""),
                       lambda r: r["routing"]["advice"].update(status="supported"),
                       lambda r: r.update(routing=None),
                       lambda r: r["routing"].update(audience="user_fact")):
            bad = deepcopy(base)
            bad["report"]["decisions"] = [spec_record(missing=["原始回执缺失"], routing=routing_payload())]
            change(bad["report"]["decisions"][0])
            add("invalid v3 routing " + str(len(cases)), bad, False)
        for invalid in (None, {"kind": "execute", "question": "问题", "options": []},
                        {"kind": "choice", "question": "问题", "options": [
                            {"key": "x", "label": "A"}, {"key": "y", "label": "B"}], "unknown": True},
                        {"kind": "text", "question": "路径", "options": [{"key": "a", "label": "A"}]},
                        {"kind": "choice", "question": "问题", "options": [{"key": "x", "label": "A"}]},
                        {"kind": "choice", "question": "问题", "options": [
                            {"key": "x", "label": "A"}, {"key": "x", "label": "B"}]}):
            bad = deepcopy(base)
            bad["report"]["decisions"][0]["human_input"] = invalid
            add("invalid human_input " + repr(invalid)[:25], bad, False)
        row = deepcopy(base)
        row["report"].pop("decisions")
        add("legacy absent", row, True)
        for value, valid in [
            ("😀" * 200, True),
            ("😀" * 201, False),
            ("\u001c", False),
            ("\u0085\u3000", False),
            ("\ufeff", True),
            ("\ud800", False),
        ]:
            row = deepcopy(base)
            row["report"]["decisions"][0]["question"] = value
            add("unicode " + repr(value[:3]), row, valid)
        for value in (None, [], [spec_record()] * 2):
            row = deepcopy(base)
            row["report"]["decisions"] = value
            add("absent is not null/empty/duplicate", row, False)
        for change in (
            {"missing": "not a list"},
            {"owner": ""},
            {"unknown": True},
            {"kind": "execute"},
            {"options": []},
            {"options": [base["report"]["decisions"][0]["options"][0]] * 2},
            {"key": "A"},
            {"key": "__proto__"},
        ):
            row = deepcopy(base)
            row["report"]["decisions"][0].update(change)
            add("invalid fields " + repr(change), row, False)
        for value, valid in [
            (0, True),
            (2147483647, True),
            (2147483648, False),
            (-1, False),
            (True, False),
            (1.5, False),
            ("1", False),
        ]:
            row = deepcopy(base)
            proposal = spec_record("budget")
            budget = proposal["budget"]
            assert isinstance(budget, dict)
            budget["attempts"] = value
            row["report"]["decisions"] = [proposal]
            add("budget " + repr(value), row, valid)
        row = deepcopy(base)
        row["report"]["decisions"] = [spec_record("budget")]
        raw = PREFIX + json.dumps(row)
        for number in ("1.0", "1e0"):
            cases.append(
                (
                    "integer spelling " + number,
                    raw.replace('"attempts": 1', '"attempts": ' + number),
                    False,
                )
            )
        row["report"]["decisions"][0]["budget"]["stop_conditions"] = []
        add("stop conditions required", row, False)
        row = deepcopy(base)
        row["kind"], row["report"]["remaining"] = "full", ""
        add("full cannot contain pending decisions", row, False)
        raw = PREFIX + json.dumps(base)
        cases.append(
            (
                "escaped duplicate key",
                raw.replace(
                    '"key": "direction"', r'"key": "direction", "\u006bey": "other"', 1
                ),
                False,
            )
        )
        cases.append(
            (
                "duplicate nested report",
                raw.replace('"summary":', '"summary": "first", "summary":', 1),
                False,
            )
        )
        cases.append(("trailing data", raw + "{}", False))

        producer = Path(__file__).with_name("journal.ts").as_uri()
        script = f"""
import {{ parseEvent, appendKeyEvent }} from {json.dumps(producer)};
import fs from 'node:fs';
const rows = JSON.parse(fs.readFileSync(0, 'utf8'));
console.log(JSON.stringify(rows.map(raw => {{
    try {{
        const event = parseEvent(raw);
        const produced = appendKeyEvent([], event.kind, event.text, event.run_id, event.seq, event.report).at(-1);
        return {{ valid: true, event, produced }};
    }} catch {{ return {{ valid: false }}; }}
}})));
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            input=json.dumps([raw for _, raw, _ in cases]),
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        actual = json.loads(result.stdout)
        self.assertEqual(len(actual), len(cases))
        for (name, raw, valid), node in zip(cases, actual, strict=True):
            with self.subTest(name=name):
                self.assertEqual(node["valid"], valid, "TypeScript verdict")
                if valid:
                    event = parse_event(raw)
                    produced = parse_event(node["produced"])
                    assert event and event.report and produced and produced.report
                    self.assertEqual(
                        node["event"], json.loads(raw.removeprefix(PREFIX))
                    )
                    self.assertEqual(asdict(event.report), asdict(produced.report))
                else:
                    with self.assertRaises(ValueError):
                        parse_event(raw)


if __name__ == "__main__":
    unittest.main()
