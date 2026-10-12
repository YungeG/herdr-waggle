"""N4 source-bound selected inventory; disposable state, never scans user files."""
import json
import unittest
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from decisions import (
    Answer,
    BusinessAnswer,
    DecisionSpec,
    Target,
    historical_business_fact,
    prepare_decision_intent,
    preview_decisions,
    validate_preview,
)
from evidence import (
    EVIDENCE_MARKER,
    EvidenceItem,
    EvidenceVerification,
    fact_reference,
    inventory_candidates,
    inventory_text,
    verification_record,
)
from journal import Report, current_report, encoded_event, parse_event
from store import SCHEMA_VERSION, StorageError, Store, decode, encode
from task_context import CONTEXT_MARKER, WorkContract, create_followup_context
from test_decisions import decision_board, spec_record
from test_r1_integration import old_database
from test_task_context import _make_accepted_parent, _make_live_board


def inventory_board():
    board = _make_live_board()
    board.next_task_id = 9
    parent = _make_accepted_parent(board)
    owner = board.create('独立成果盘点', parent.project, '只核对指定旧材料；不是重复完成原目标')
    board.set_work_contract(owner, WorkContract('inventory', '选定成果目录', '仅已选材料', '逐项标明证据等级', '不访问未知路径，不实施、不新增预算'))
    report = current_report(parent, board.runs[parent.run_id])
    acceptance = next(event for raw in parent.events if (event := parse_event(raw)) and event.kind == 'accepted')
    assert report is not None
    return board, parent, owner, report, acceptance


def add_report(board, parent, owner, report, acceptance):
    from evidence import report_reference
    item = report_reference(board, owner, parent, report.id, acceptance.id, ['summary'],
                            name='D009 旧样例交付', locator='/unknown/original.csv',
                            limits='旧样例范围，不是新成果或访问许可', gaps='原件及路径未核实', next_owner='未知，待明确核验者')
    board.add_evidence(owner, item)
    return item


class InventorySentinels(unittest.TestCase):
    def test_d009_selected_source_in_real_new_run_not_chat_search_or_new_grant(self):
        board, parent, owner, report, acceptance = inventory_board()
        before = deepcopy(parent), deepcopy(board.runs[parent.run_id])
        item = add_report(board, parent, owner, report, acceptance)
        board.assign(owner, str(uuid4()))
        preview = board.compiled_requirements(owner)
        run = board.queue_real(owner, str(uuid4()), str(uuid4()), board.projects[owner.project])
        self.assertEqual(run.requirements, preview)
        self.assertIn(report.id, preview)
        self.assertIn(acceptance.id, preview)
        self.assertIn(item.source_digest, preview)
        self.assertIn('执行者自报', preview)
        self.assertNotIn('中文及空输入边界通过', preview)  # checks were not selected
        self.assertIn('不是新许可', preview)
        self.assertNotIn('全文搜', preview)
        self.assertEqual((parent, board.runs[parent.run_id]), before)
        self.assertEqual(decode(encode(board)).evidence_items[item.id], item)

    def test_unchecked_path_stays_unknown_not_missing_or_independently_verified(self):
        from evidence import inventory_text
        board, parent, owner, report, acceptance = inventory_board()
        item = add_report(board, parent, owner, report, acceptance)
        text = inventory_text(board, owner)
        self.assertIn('未核实', text)
        self.assertIn(item.locator, text)
        self.assertNotIn('路径不存在', text)
        self.assertNotIn('原件已独立核验', text)
        self.assertEqual(board.evidence_verifications, {})

    def test_arbitrary_quote_tail_and_same_id_raw_rewrite_reject_without_run(self):
        from evidence import EVIDENCE_MARKER
        board, parent, owner, report, acceptance = inventory_board()
        add_report(board, parent, owner, report, acceptance)
        with self.assertRaises(ValueError):
            board.compiled_requirements(owner, owner.remaining + EVIDENCE_MARKER + '{}')
        raw = board.runs[parent.run_id].events[0]
        board.runs[parent.run_id].events[0] = raw.replace('样例方案', '篡改方案')
        board.assign(owner, str(uuid4()))
        before = encode(board)
        with self.assertRaises(ValueError):
            board.queue_real(owner, str(uuid4()), str(uuid4()), board.projects[owner.project])
        self.assertEqual(encode(board), before)
        with self.assertRaises(StorageError):
            decode(before)

    def test_human_verification_appends_audit_only_and_binds_exact_item(self):
        from evidence import inventory_text, verification_record
        board, parent, owner, report, acceptance = inventory_board()
        item = add_report(board, parent, owner, report, acceptance)
        before = deepcopy(board.runs), deepcopy(parent)
        checked = verification_record(board, item.id, verifier='隔离夹具人工核验者', method='人工比对隔离原件摘要，不是平台扫描',
                                      result='unverified', limits='未接触真实原件', gaps='真实原件未知',
                                      next_owner='未知，先明确材料保管人', checked_at='2026-01-01T00:00:00.000Z')
        board.verify_evidence(owner, checked)
        self.assertIn('人工核验记录', inventory_text(board, owner))
        self.assertEqual((board.runs, parent), before)
        self.assertEqual(board.supplements, {})
        self.assertEqual(board.decisions, {})
        self.assertEqual(decode(encode(board)).evidence_verifications[checked.id], checked)
        with self.assertRaises(ValueError):
            board.verify_evidence(owner, replace(checked, id=str(uuid4()), item_digest='0' * 64))


class InventoryIntegrity(unittest.TestCase):
    def test_strict_decoders_reject_bool_bad_ids_refs_and_recomputed_forged_snapshot(self):
        board, parent, owner, report, acceptance = inventory_board()
        item = add_report(board, parent, owner, report, acceptance)
        for field, value in [('owner_task_id', True), ('source_task_id', {}), ('source_report_seq', False),
                             ('source_run_id', []), ('acceptance_event_id', True), ('decision_record_id', {}),
                             ('selected_refs', [{}]), ('selected_refs', ['summary', 'summary']), ('cwd', False),
                             ('created_at', '2026-13-01T00:00:00.000Z'), ('source_digest', 'z' * 64)]:
            with self.subTest(field=field, value=value):
                row = asdict(item) | {field: value}
                with self.assertRaises(ValueError):
                    EvidenceItem.from_record(row)
                raw = json.loads(encode(board))
                raw['evidence_items'][0] = row
                with self.assertRaises(StorageError):
                    decode(json.dumps(raw))
        raw = json.loads(encode(board))
        raw['evidence_items'][0]['source_snapshot'] = '{"伪造结果":"新目标已完成"}'
        from evidence import digest
        raw['evidence_items'][0]['source_digest'] = digest(raw['evidence_items'][0])
        with self.assertRaises(StorageError):
            decode(json.dumps(raw))
        record = verification_record(board, item.id, verifier='测试核验人', method='只核对夹具', result='unverified',
                                     limits='不访问原件', gaps='未知', next_owner='未知', checked_at='2026-01-01T00:00:00.000Z')
        for field, value in [('version', True), ('item_id', []), ('verifier', False), ('result', {}),
                             ('checked_at', '2030-01-01T00:00:00.000Z'), ('item_digest', 'bad')]:
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    EvidenceVerification.from_record(asdict(record) | {field: value})

    def test_scope_change_and_partial_or_failed_claims_cannot_be_accepted_inventory(self):
        board, parent, owner, report, acceptance = inventory_board()
        item = add_report(board, parent, owner, report, acceptance)
        for changed in ('owner', 'source', 'directory'):
            candidate = deepcopy(board)
            if changed == 'owner':
                candidate.tasks[owner.id].description = '另一个目标'
            elif changed == 'source':
                candidate.tasks[parent.id].title = '另一个原任务'
            else:
                candidate.projects[parent.project] += '/expanded'
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                candidate.compiled_requirements(candidate.tasks[owner.id])
        for state, delivery in [('finished', 'partial'), ('failed', 'progress')]:
            candidate = deepcopy(board)
            source = candidate.runs[parent.run_id]
            source.state, source.delivery = state, delivery
            candidate.tasks[parent.id].full_delivery = False
            self.assertFalse(inventory_candidates(candidate))
            from evidence import report_reference
            with self.assertRaises(ValueError):
                report_reference(candidate, candidate.tasks[owner.id], candidate.tasks[parent.id], report.id,
                                 acceptance.id, ['summary'], name='声称', locator='未知', limits='未知', gaps='未知', next_owner='未知')
        self.assertEqual(item.source_kind, 'accepted_report')

    def test_purge_protects_source_but_owner_cleanup_removes_its_references_and_audit(self):
        board, parent, owner, report, acceptance = inventory_board()
        item = add_report(board, parent, owner, report, acceptance)
        record = verification_record(board, item.id, verifier='夹具', method='人工样例比对', result='unverified',
                                     limits='旧范围', gaps='原件未知', next_owner='未知', checked_at='2026-01-01T00:00:00.000Z')
        board.verify_evidence(owner, record)
        board.retire(parent, 'trashed')
        before = encode(board)
        with self.assertRaises(ValueError):
            board.purge(parent)
        self.assertEqual(encode(board), before)
        with self.assertRaises(ValueError):
            board.delete_project(parent.project)
        board.retire(owner, 'trashed')
        board.purge(owner)
        self.assertFalse(board.evidence_items)
        self.assertFalse(board.evidence_verifications)
        board.purge(parent)
        self.assertNotIn(parent.id, decode(encode(board)).tasks)

    def test_append_only_store_rejects_rewrite_delete_and_reorder_without_advancing_revision(self):
        board, parent, owner, report, acceptance = inventory_board()
        with TemporaryDirectory(prefix='waggle-n4-audit-') as temporary, Store(Path(temporary) / 'state.sqlite3') as store:
            store.load()
            store.save(board)
            item = add_report(board, parent, owner, report, acceptance)
            store.save(board)
            for mutation in ('rewrite', 'delete'):
                candidate = deepcopy(board)
                if mutation == 'rewrite':
                    candidate.evidence_items[item.id] = replace(item, locator='改写旧定位')
                else:
                    candidate.evidence_items.clear()
                revision = store.revision
                with self.assertRaises(StorageError):
                    store.save(candidate)
                self.assertEqual(store.revision, revision)
                self.assertEqual(store.load().evidence_items[item.id], item)
            for result in ('unverified', 'conflict'):
                record = verification_record(board, item.id, verifier='夹具核验人', method='人工核对摘要', result=result,
                                             limits='旧数据', gaps='原件未知', next_owner='未知', checked_at='2026-01-01T00:00:00.000Z')
                board.verify_evidence(owner, record)
                store.save(board)
            for mutation in ('rewrite', 'delete', 'reorder'):
                candidate = deepcopy(board)
                key, old = next(iter(candidate.evidence_verifications.items()))
                if mutation == 'rewrite':
                    candidate.evidence_verifications[key] = replace(old, result='verified')
                elif mutation == 'delete':
                    del candidate.evidence_verifications[key]
                else:
                    candidate.evidence_verifications = dict(reversed(list(candidate.evidence_verifications.items())))
                revision = store.revision
                with self.assertRaises(StorageError):
                    store.save(candidate)
                self.assertEqual(store.revision, revision)
                self.assertEqual(store.load().evidence_verifications, board.evidence_verifications)

    def test_combined_context_inventory_once_and_new_attestation_recompiled_for_formal_run(self):
        board, parent, _, report, acceptance = inventory_board()
        context = create_followup_context(board, parent, report.id, acceptance.id, ['summary'], '实施后续', '独立核对新目标')
        owner = board.create_followup(parent.id, context, '实施后续', '独立核对新目标')
        board.set_work_contract(owner, WorkContract('implementation', '核对新结果', '隔离样例', '直接验证', '不沿用旧收益或预算'))
        item = add_report(board, parent, owner, report, acceptance)
        board.assign(owner, str(uuid4()))
        first = board.queue_real(owner, str(uuid4()), str(uuid4()), board.projects[owner.project])
        immutable = deepcopy(first)
        ended = deepcopy(first)
        ended.state, ended.delivery, ended.seq = 'finished', 'partial', 2
        ended.summary, ended.checks, ended.remaining = '方案已整理', ['样例证据'], '需选择方案'
        ended.events = [encoded_event('partial', '正式方案', source='executor', run_id=first.id, seq=1,
                       report=Report(ended.summary, ended.checks, ended.remaining,
                                     decisions=(DecisionSpec.from_record(spec_record()),)))]
        board.ingest(ended)
        target = Target('new_run', first.agent_id, first.session_id, first.cwd)
        old_preview = preview_decisions(board, owner.id, (Answer('choice', 'a'),), target)
        record = verification_record(board, item.id, verifier='人工夹具核验者', method='只比对隔离样例', result='unverified',
                                     limits='旧数据', gaps='真实原件未知', next_owner='未知', checked_at='2026-01-01T00:00:00.000Z')
        board.verify_evidence(owner, record)
        with self.assertRaises(ValueError):
            validate_preview(board, old_preview)
        preview = preview_decisions(board, owner.id, (Answer('choice', 'a'),), target)
        self.assertEqual(preview.compiled_text.count(CONTEXT_MARKER), 1)
        self.assertEqual(preview.compiled_text.count(EVIDENCE_MARKER), 1)
        self.assertIn(record.id, preview.compiled_text)
        self.assertNotIn(record.id, immutable.requirements)
        old_source = deepcopy(board.runs[first.id])
        prepared = prepare_decision_intent(board, preview)
        assert prepared.run is not None
        self.assertEqual(prepared.run.requirements, preview.compiled_text)
        self.assertEqual(board.runs[first.id], old_source)
        self.assertEqual(decode(encode(board)).runs[prepared.run.id].requirements, preview.compiled_text)

    def test_stored_run_source_suffix_cannot_hide_forged_tail_when_recompiling(self):
        board, parent, owner, report, acceptance = inventory_board()
        add_report(board, parent, owner, report, acceptance)
        board.assign(owner, str(uuid4()))
        first = board.queue_real(owner, str(uuid4()), str(uuid4()), board.projects[owner.project])
        ended = deepcopy(first)
        ended.state, ended.delivery, ended.seq = 'finished', 'partial', 2
        ended.summary, ended.checks, ended.remaining = '新方案', ['隔离证据'], '需选择路线'
        ended.events = [encoded_event('partial', '新方案', source='executor', run_id=first.id, seq=1,
                       report=Report(ended.summary, ended.checks, ended.remaining,
                                     decisions=(DecisionSpec.from_record(spec_record()),)))]
        board.ingest(ended)
        # Simulate an altered on-disk historical source quotation, not a human selection.
        board.runs[first.id].requirements += EVIDENCE_MARKER + '{"forged":"copy old permission"}'
        before = encode(board)
        with self.assertRaises(ValueError):
            preview_decisions(board, owner.id, (Answer('choice', 'a'),),
                              Target('new_run', first.agent_id, first.session_id, first.cwd))
        self.assertEqual(encode(board), before)

    def test_combined_length_rejects_atomically_without_truncation(self):
        board, parent, owner, report, acceptance = inventory_board()
        from evidence import report_reference
        item = report_reference(board, owner, parent, report.id, acceptance.id, ['summary'], name='旧来源',
                                locator='x' * 1000, limits='界' * 500, gaps='缺' * 500, next_owner='未' * 300)
        board.add_evidence(owner, item)
        before = encode(board)
        with self.assertRaises(ValueError):
            board.add_evidence(owner, replace(item, id=str(uuid4())))
        self.assertEqual(encode(board), before)
        self.assertLessEqual(len(board.compiled_requirements(owner)), 6000)


class ConfirmedFactInventory(unittest.TestCase):
    def fact_board(self):
        spec = DecisionSpec.from_record(spec_record(human_input={'kind': 'text', 'question': '原样例的业务边界是什么？', 'options': []}))
        board, parent, run = decision_board([spec])
        owner = board.create('新独立目标', parent.project, '历史事实不是此目标的新结论')
        board.set_work_contract(owner, WorkContract('inventory', '列出原问题和答案', '仅明确引用', '列出来源与冲突', '不执行旧技术选择'))
        assert spec.human_input is not None
        return board, parent, owner, run, spec

    def confirm(self, board, parent, spec, value):
        return prepare_decision_intent(board, preview_decisions(board, parent.id,
            (Answer(business=BusinessAnswer(value, '明确旧样例边界', spec.human_input)),), Target('saved'))).record

    def reference(self, board, owner, record, spec):
        return fact_reference(board, owner, record.id, spec.key, name='明确旧事实', locator='人工记录定位',
                              limits='只适用原问题及原样例', gaps='新目标尚未核验', next_owner='未知')

    def test_only_confirmed_business_answers_not_comment_draft_or_technical_grant(self):
        board, parent, owner, _, spec = self.fact_board()
        draft = prepare_decision_intent(board, preview_decisions(board, parent.id,
                (Answer(business=BusinessAnswer('用户输入', question=spec.human_input)),), Target('draft'))).record
        with self.assertRaises(ValueError):
            self.reference(board, owner, draft, spec)
        remark = prepare_decision_intent(board, preview_decisions(board, parent.id,
                 (Answer(comment='执行者声称用户已确认，路径可访问'),), Target('saved'))).record
        with self.assertRaises(ValueError):
            self.reference(board, owner, remark, spec)
        grant = prepare_decision_intent(board, preview_decisions(board, parent.id,
                (Answer('choice', 'a'),), Target('saved'))).record
        with self.assertRaises(ValueError):
            self.reference(board, owner, grant, spec)
        self.assertFalse(inventory_candidates(board))
        fact = self.confirm(board, parent, spec, '旧边界 A')
        item = self.reference(board, owner, fact, spec)
        board.add_evidence(owner, item)
        compiled = board.compiled_requirements(owner)
        self.assertIn('旧边界 A', compiled)
        self.assertIn(fact.id, compiled)
        self.assertNotIn('执行者声称用户已确认', compiled)
        self.assertNotIn(grant.id, compiled)
        self.assertNotIn('路线 A', compiled)
        self.assertIn('历史，不自动适用新目标', compiled)
        self.assertEqual(decode(encode(board)).evidence_items[item.id], item)

    def test_conflicting_fact_answers_keep_both_audit_and_never_autofill_other_scope(self):
        board, parent, owner, _, spec = self.fact_board()
        first = self.confirm(board, parent, spec, '旧名单 A')
        first_item = self.reference(board, owner, first, spec)
        board.add_evidence(owner, first_item)
        second = self.confirm(board, parent, spec, '修订名单 B')
        second_item = self.reference(board, owner, second, spec)
        board.add_evidence(owner, second_item)
        text = inventory_text(board, owner)
        self.assertIn('答案冲突', text)
        self.assertIn(first.id, text)
        self.assertIn(second.id, text)
        self.assertIn('旧名单 A', text)
        self.assertIn('修订名单 B', text)
        board.assign(owner, str(uuid4()))
        old = board.queue_real(owner, str(uuid4()), str(uuid4()), board.projects[owner.project])
        ended = deepcopy(old)
        ended.state, ended.delivery, ended.seq = 'finished', 'partial', 2
        ended.summary, ended.checks, ended.remaining = '新目标还需事实', ['只含新提问'], '尚未确认'
        ended.events = [encoded_event('partial', '新问题', source='executor', run_id=old.id, seq=1,
                       report=Report(ended.summary, ended.checks, ended.remaining, decisions=(spec,)))]
        board.ingest(ended)
        self.assertIsNone(historical_business_fact(board, owner.id, spec.key))
        preview = preview_decisions(board, owner.id, (Answer(),), Target('saved'))
        self.assertIsNone(preview.answers[0].business)
        self.assertEqual(preview.prior, ('',))
        candidate = deepcopy(board)
        candidate.tasks[parent.id].description += '范围扩大'
        with self.assertRaises(ValueError):
            candidate.compiled_requirements(candidate.tasks[owner.id])


class InventoryMigration(unittest.TestCase):
    def test_v8_exact_context_report_acceptance_backup_and_empty_new_registries(self):
        board, parent, _, report, acceptance = inventory_board()
        context = create_followup_context(board, parent, report.id, acceptance.id, ['summary'], '旧后续', '旧明确目标')
        board.create_followup(parent.id, context, '旧后续', '旧明确目标')
        raw = json.loads(encode(board))
        raw.pop('evidence_items')
        raw.pop('evidence_verifications')
        for field in ("task_dependencies", "dependency_checks", "ownership_releases", "ownership_receipts",
                      "budget_scopes", "usage_observations", "ledger_verifications", "budget_grants"):
            raw.pop(field)
        original = json.dumps(raw, ensure_ascii=False, indent=2)
        with TemporaryDirectory(prefix='waggle-n4-v8-') as temporary:
            path = Path(temporary) / 'old.sqlite3'
            old_database(path, 8, original)
            with Store(path) as store:
                migrated = store.load()
                self.assertEqual(store.connection.execute('PRAGMA user_version').fetchone()[0], SCHEMA_VERSION)
                self.assertEqual(store.connection.execute('SELECT revision,data FROM legacy_v8').fetchall(), [(9, original)])
                self.assertEqual(migrated.tasks[parent.id].events, parent.events)
                self.assertEqual(migrated.runs[parent.run_id].events, board.runs[parent.run_id].events)
                self.assertEqual(migrated.task_contexts, board.task_contexts)
                self.assertFalse(migrated.evidence_items)
                self.assertFalse(migrated.evidence_verifications)
                store.save(migrated)
                self.assertEqual(store.connection.execute('SELECT data FROM legacy_v8').fetchone()[0], original)

    def test_v8_corruption_and_update_rejection_rollback_exact_physical_bytes(self):
        board, _, _, _, _ = inventory_board()
        payload = json.loads(encode(board))
        payload.pop('evidence_items')
        payload.pop('evidence_verifications')
        for field in ("task_dependencies", "dependency_checks", "ownership_releases", "ownership_receipts",
                      "budget_scopes", "usage_observations", "ledger_verifications", "budget_grants"):
            payload.pop(field)
        with TemporaryDirectory(prefix='waggle-n4-v8-fail-') as temporary:
            for denied in (False, True):
                original = json.dumps(payload, ensure_ascii=False) if denied else '{"bad":false}'
                path = Path(temporary) / f'{denied}.sqlite3'
                old_database(path, 8, original, deny=denied)
                before = path.read_bytes()
                with self.assertRaises(StorageError):
                    Store(path)
                self.assertEqual(path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
