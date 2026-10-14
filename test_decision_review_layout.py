"""Presentation regressions using production CSS; no HUMAN or dispatch evidence."""
import unittest
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path

from textual.app import App
from textual.containers import VerticalScroll
from textual.geometry import Region
from textual.widgets import Button, Collapsible, Select, Static

from budget_ledger import FORMAL_APPROVAL_ENABLED
from decision_presentation import capture_report_view
from decision_view import DecisionReview, DecisionSheet
from decisions import Answer, DecisionSpec, Target, preview_decisions
from journal import parse_event
from store import encode
from test_decisions import decision_board, routing_payload, spec_record


class ReviewLayoutApp(App):
    CSS_PATH = str(Path(__file__).with_name("style.tcss"))

    def __init__(self, review):
        super().__init__(watch_css=False)
        self.review = review

    def on_mount(self):
        self.push_screen(self.review)


class DecisionReviewLayoutTests(unittest.IsolatedAsyncioTestCase):
    def make_review(self, target_kind="new_run", *, feedback=False, routed=False, insufficient=False):
        directory = "/fixture/" + "long-review-scope-" * 6 + "/project"
        scope = directory + "（仅三条虚构资料；不涉及真实目录）"
        spec = DecisionSpec.from_record(spec_record(
            question="这三条虚构资料采用哪种展示方案？", scope=scope,
            options=[dict(option, scope=scope, label=f"方案 {option['key'].upper()} · 编号清单")
                     for option in spec_record()["options"]]))
        if routed:
            route = routing_payload("owner_evidence" if insufficient else "user_decision",
                gap="尚未核实原始结果；UNKNOWN，不能当作已通过" if insufficient else "",
                status="insufficient" if insufficient else "supported")
            route["advice"]["owner_signed"] = False
            route["advice"]["uncertainty"] = "UNKNOWN：尚未核实环境与原始结果；不能将自报成功解释为验收。" * 3
            if not insufficient:
                route["advice"]["conditions"] = "只有原件逐条核对且旧单已撤销才适用；不得重做实验、访问其他目录或续用旧预算。" * 3
            options = [dict(option, scope=scope if option["key"] == "a" else "所选方案仅覆盖虚构编号 B；不扩大整体目录。",
                            risk="所选方案风险：可能无法复原旧编号；不等于整体风险已消除。",
                            cost="所选方案成本：两次合成核对，不继承任何旧预算。") for option in spec_record()["options"]]
            spec = DecisionSpec.from_record(spec_record(question="核对有据方案，哪些限制仍然生效？", scope=scope,
                risk="整体风险：环境与原结果仍须人工核验。", cost="整体成本：仅一次隔离展示，不授权真实消耗。",
                exclusions=["不得访问真实目录或真实数据库", "不得执行新研究或继承旧预算"],
                missing=[need["gap"] for need in route["evidence_needs"]], options=options, routing=route))
            scope = spec.options[1].scope
        board, task, run = decision_board([spec], live=target_kind == "live_supplement")
        if routed:
            run.requirements += "\n继承要求全文：仅核对虚构编号；未核原件不得继续，否定条件不能省略。" * 3
        board.projects[task.project] = directory
        run.cwd = directory
        target = (Target(target_kind, run.agent_id, run.session_id, directory)
                  if target_kind in {"new_run", "live_supplement"} else Target(target_kind))
        answer = Answer("request_info", comment="请解释已有资料") if feedback else Answer("choice", "b")
        preview = preview_decisions(board, task.id, (answer,), target)
        commits = []

        async def deny_commit(value):
            commits.append(value)
            raise AssertionError("Layout regression must never confirm or dispatch")

        label = ("反馈／请求解释" if feedback else "开始新一轮") + (
            "\n接收方：合成回归接收箱（无执行者）\n目录：" + directory
            + "\n仅测试展示，不发送、不启动执行者、不批准预算")
        raw = next(raw for raw in run.events if (event := parse_event(raw)) is not None and event.id == preview.source.event_id)
        review = DecisionReview(preview, label, deny_commit, report_view=capture_report_view(preview.source, raw))
        return review, board, run, commits, scope

    async def collect_compositor_rows(self, pilot, review, targets):
        body = review.query_one(".decision-body", VerticalScroll)
        body.focus()
        body.scroll_home(animate=False)
        await pilot.pause()
        collected = {target.id: {} for target in targets}
        stride = max(1, body.content_region.height // 2)
        for _ in range(int(body.max_scroll_y) + 2):
            frame = review._compositor.render_strips()
            for target in targets:
                visible = target.region.intersection(body.content_region).intersection(review.region)
                for y in range(max(0, visible.y), min(len(frame), visible.bottom)):
                    collected[target.id][y - target.region.y] = frame[y].crop(target.region.x, target.region.right).text
            if isinstance(review, DecisionReview):
                self.assertEqual(self.rendered_button(review, '#decision-confirm'), review.confirm_label())
                self.assertEqual(self.rendered_button(review, '#decision-back'), '返回修改')
            else:
                self.assertEqual(self.rendered_button(review, '#decision-close'), '返回')
                self.assertEqual(self.rendered_button(review, '#decision-draft'), '保存草稿')
                self.assertEqual(self.rendered_button(review, '#decision-saved'), '确认不发送')
                active = sum(answer != Answer() for answer in review.answers())
                self.assertEqual(self.rendered_button(review, '#decision-continue'), f'核对本单{active}项')
            if body.scroll_y >= body.max_scroll_y:
                break
            await pilot.press(*(["down"] * stride))
            await pilot.pause()
        result = {}
        for target in targets:
            self.assertGreater(target.region.height, 0)
            rows = collected[target.id]
            self.assertEqual(set(rows), set(range(target.region.height)),
                             f"Every actual compositor row must be reachable: {target.id}")
            result[target.id] = "".join("".join(rows[y].split()) for y in sorted(rows))
        return result

    async def check_critical_visibility(self, size, target_kind="new_run", *, insufficient=False):
        review, board, run, commits, _ = self.make_review(target_kind, feedback=insufficient, routed=True, insufficient=insufficient)
        before_preview, before_run = deepcopy(asdict(review.preview)), deepcopy(asdict(run))
        spec = review.preview.specs[0]
        async with ReviewLayoutApp(review).run_test(size=size) as pilot:
            await pilot.pause()
            original = review.query_one("#decision-review-source", Collapsible)
            authority = review.query_one("#decision-submit-text", Collapsible)
            self.assertTrue(original.collapsed)
            self.assertFalse(authority.collapsed)
            summary = review.query_one("#decision-review-summary", Static)
            compiled = review.query_one("#decision-preview-text", Static)
            self.assertEqual(str(compiled.content), review.preview.compiled_text)
            self.assertIn('未选 0 项；未选项不发送', str(summary.content))
            self.assertNotIn('未选 0 项，本次不发送', str(summary.content))
            target = review.query_one("#decision-target", Static)
            visible = await self.collect_compositor_rows(pilot, review, [summary, compiled, target])
            self.assertTrue(original.collapsed, "Critical visibility must not depend on opening the original report")
            compact = lambda value: "".join(value.split())
            assert spec.routing is not None
            required = [spec.scope, spec.risk, spec.cost, spec.routing.advice.uncertainty,
                        *spec.exclusions, "负责人签收：否", "不是独立核验或本次批准"]
            if insufficient:
                required.extend([*spec.missing, "暂不能推荐具体设置值"])
                self.assertNotIn("建议（非批准）：", str(summary.content))
            else:
                required.extend([spec.routing.advice.conditions, "建议（非批准）：30 秒",
                                 spec.options[1].scope, spec.options[1].risk, spec.options[1].cost])
            for value in required:
                self.assertIn(compact(value), visible[summary.id])
            self.assertEqual(visible[compiled.id], compact(review.preview.compiled_text),
                             "Default-expanded authoritative send text must be entirely painted, not merely stored")
            self.assertIn(compact(review.preview.target.cwd), visible[target.id])
            self.assertIn(compact(review.target_label.splitlines()[-1]), visible[target.id])
            self.assertEqual(asdict(review.preview), before_preview)
            self.assertEqual(asdict(run), before_run)
            self.assertFalse(board.decisions)
            self.assertFalse(commits)
            await pilot.press("escape")
        self.assertFalse(commits)

    async def check_sheet_critical_visibility(self, size, *, legacy=False, insufficient=False):
        review, board, run, commits, _ = self.make_review(feedback=insufficient, routed=not legacy, insufficient=insufficient)
        before_board, before_run = encode(board), deepcopy(asdict(run))
        calls = []

        async def deny_prepare(*args):
            calls.append(args)
            raise AssertionError('Layout inspection must not prepare or submit a request')

        sheet = DecisionSheet(board, review.preview.task_id, deny_prepare)
        spec = sheet.specs[0]
        async with ReviewLayoutApp(sheet).run_test(size=size) as pilot:
            await pilot.pause()
            original = sheet.query_one('#decision-report-source', Collapsible)
            reply = sheet.query_one('#decision-other-reply-0', Collapsible)
            self.assertTrue(original.collapsed)
            self.assertTrue(reply.collapsed)
            if insufficient:
                self.assertTrue(sheet.query_one('#decision-details-0', Collapsible).collapsed)
            sheet.query_one('#intent-0', Select).value = 'formal' if not insufficient else 'feedback'
            sheet.change_intent(0)
            sheet.query_one('#answer-0', Select).value = 'choice:b' if not insufficient else 'preference:b'
            await pilot.pause()
            self.assertTrue(reply.collapsed, 'Selecting fixture values does not force a valid collapsed reply open')
            answers, values, snapshot = sheet.answers(), sheet.edit_values(), sheet.report_view
            boundary = sheet.query_one('#decision-boundary-0', Static)
            advice = sheet.query_one('#decision-recommendation-0', Static)
            selected = sheet.query_one('#decision-selected-0', Static)
            status = sheet.query_one('#decision-reply-status-0', Static)
            targets = [boundary, advice, selected, status]
            if insufficient:
                targets.append(sheet.query_one('#decision-gap-0-1', Static))
            visible = await self.collect_compositor_rows(pilot, sheet, targets)
            compact = lambda value: ''.join(value.split())
            for value in (spec.scope, spec.risk, spec.cost, *spec.exclusions):
                self.assertIn(compact(value), visible[boundary.id])
            for value in (spec.options[1].scope, spec.options[1].risk, spec.options[1].cost):
                self.assertIn(compact(value), visible[selected.id])
            if spec.routing:
                for value in (spec.routing.advice.uncertainty, '负责人签收：否', '不是独立核验或本次批准'):
                    self.assertIn(compact(value), visible[advice.id])
                if insufficient:
                    self.assertIn('暂不能推荐具体设置值', visible[advice.id])
                    self.assertIn(compact(spec.missing[0]), visible['decision-gap-0-1'])
                    self.assertTrue(sheet.query_one('#decision-details-0', Collapsible).collapsed)
                else:
                    self.assertIn(compact(spec.routing.advice.conditions), visible[advice.id])
                    self.assertIn('建议（非批准）：30秒', visible[advice.id])
            else:
                self.assertIn('未提供结构化不确定性', visible[advice.id])
                self.assertIn('签收状态未提供', visible[advice.id])
            self.assertIn('将列入最后核对，尚未发送', visible[status.id])
            self.assertTrue(original.collapsed)
            self.assertTrue(reply.collapsed)
            self.assertEqual(sheet.answers(), answers)
            self.assertEqual(sheet.edit_values(), values)
            self.assertEqual(sheet.report_view, snapshot)
            self.assertEqual(encode(board), before_board)
            self.assertEqual(asdict(run), before_run)
            self.assertFalse(calls)
            self.assertFalse(commits)

    async def test_sheet_critical_fields_and_collapsed_selected_answer_smoke_80(self):
        await self.check_sheet_critical_visibility((80, 24))

    async def test_sheet_critical_fields_for_supported_insufficient_and_legacy_at_all_sizes(self):
        for size in ((40, 18), (80, 24), (130, 50)):
            for flags in ({}, {'insufficient': True}, {'legacy': True}):
                with self.subTest(size=size, **flags):
                    await self.check_sheet_critical_visibility(size, **flags)

    async def test_critical_and_authoritative_text_smoke_80(self):
        await self.check_critical_visibility((80, 24))

    async def test_critical_and_authoritative_text_40_and_wide_for_both_send_kinds(self):
        for size in ((40, 18), (130, 50)):
            for kind in ("new_run", "live_supplement"):
                with self.subTest(size=size, kind=kind):
                    await self.check_critical_visibility(size, kind)

    async def test_insufficient_advice_negation_unknown_and_unsigned_are_visible(self):
        for size in ((40, 18), (80, 24), (130, 50)):
            with self.subTest(size=size):
                await self.check_critical_visibility(size, insufficient=True)

    def test_review_requires_snapshot_and_rejects_wrong_source_specs_or_envelope(self):
        review, _, _, commits, _ = self.make_review()
        preview = review.preview
        with self.assertRaises(TypeError):
            DecisionReview(preview, review.target_label, review.commit)  # pyright: ignore[reportCallIssue] -- required-argument rejection
        with self.assertRaisesRegex(ValueError, "不一致"):
            DecisionReview(replace(preview, source=replace(preview.source, seq=2)), review.target_label,
                           review.commit, report_view=review.report_view)
        with self.assertRaisesRegex(ValueError, "不一致"):
            DecisionReview(replace(preview, specs=(DecisionSpec.from_record(spec_record(cost="不同成本")),)),
                           review.target_label, review.commit, report_view=review.report_view)
        with self.assertRaisesRegex(ValueError, "快照"):
            DecisionReview(preview, review.target_label, review.commit,
                           report_view=replace(review.report_view, source_bytes="not an envelope"))
        self.assertFalse(commits)

    def rendered_button(self, review, button_id):
        button = review.query_one(button_id, Button)
        container = '#decision-review' if isinstance(review, DecisionReview) else '#decision-sheet'
        self.assertTrue(review.query_one(container).content_region.contains_region(button.region))
        self.assertTrue(review.region.contains_region(button.region))
        # Crop the actual compositor output, not Button.label or an unoccluded widget render.
        rows = review._compositor.render_strips()
        visible = [row.crop(button.region.x, button.region.right).text.strip()
                   for row in rows[button.region.y:button.region.bottom]]
        return "".join(visible)

    def assert_scope_rendering(self, review, scope):
        summary = review.query_one("#decision-review-summary", Static)
        self.assertEqual(str(summary.content), review.summary())
        rendered = [strip.text for strip in summary.render_lines(
            Region(0, 0, summary.size.width, summary.size.height))]
        start = next((i for i, line in enumerate(rendered) if line.lstrip().startswith("范围：")), None)
        self.assertIsNotNone(start, "Selected scope must be a separate answer field")
        assert start is not None
        end = next(i for i in range(start + 1, len(rendered))
                   if rendered[i].lstrip().startswith("意图："))
        scope_lines = rendered[start:end]
        self.assertGreater(len(scope_lines), 1, "Fixture must actually exercise wrapping")
        self.assertTrue(all(line.startswith("     ") for line in scope_lines if line.strip()),
                        "Wrapped value lines must not jump back to the left margin")
        self.assertEqual("".join(line.strip() for line in scope_lines), "范围：" + scope)

    async def check_confirmation(self, size, target_kind="new_run", *, feedback=False):
        review, board, run, commits, _ = self.make_review(target_kind, feedback=feedback)
        before_preview = deepcopy(asdict(review.preview))
        before_run = deepcopy(asdict(run))
        expected = ("确认并发送反馈·仅补证" if feedback else {
            "draft": "保存草稿", "saved": "确认决定，仅保存",
            "new_run": "确认并开始下一轮", "live_supplement": "确认并发送本轮",
        }[target_kind])
        self.assertIs(FORMAL_APPROVAL_ENABLED, False)
        async with ReviewLayoutApp(review).run_test(size=size) as pilot:
            await pilot.pause()
            self.assertEqual(tuple(review.size), size)
            self.assertEqual(self.rendered_button(review, "#decision-confirm"), expected,
                             "Full confirmation consequence must be visible on the actual screen")
            self.assertEqual(self.rendered_button(review, "#decision-back"), "返回修改")
            self.assertTrue(review.query_one("#decision-back", Button).has_focus)
            self.assertEqual(asdict(review.preview), before_preview)
            self.assertFalse(board.decisions)
            self.assertEqual(asdict(run), before_run)
            self.assertFalse(commits)
            await pilot.press("escape")
            await pilot.pause()
            self.assertIsNot(pilot.app.screen, review)
        self.assertFalse(commits)
        self.assertFalse(board.decisions)
        self.assertEqual(asdict(run), before_run)
        self.assertIs(FORMAL_APPROVAL_ENABLED, False)

    async def test_new_run_confirmation_text_at_observed_37x15(self):
        await self.check_confirmation((37, 15))

    async def test_complete_confirmation_consequences_at_exact_sizes_and_wide(self):
        for size in ((40, 18), (80, 24), (130, 63)):
            for target in ("draft", "saved", "new_run", "live_supplement"):
                with self.subTest(size=size, target=target):
                    await self.check_confirmation(size, target)
            with self.subTest(size=size, target="non_authorizing_feedback"):
                await self.check_confirmation(size, feedback=True)

    async def test_resize_preserves_full_consequences_scope_and_unconfirmed_data(self):
        review, board, run, commits, scope = self.make_review()
        before = deepcopy(asdict(review.preview))
        before_run = deepcopy(asdict(run))
        async with ReviewLayoutApp(review).run_test(size=(80, 24)) as pilot:
            for size in ((40, 18), (130, 63), (37, 15), (80, 24)):
                with self.subTest(size=size):
                    await pilot.resize_terminal(*size)
                    await pilot.pause()
                    self.assertEqual(self.rendered_button(review, "#decision-confirm"), "确认并开始下一轮")
                    self.assertEqual(self.rendered_button(review, "#decision-back"), "返回修改")
                    self.assert_scope_rendering(review, scope)
                    self.assertEqual(asdict(review.preview), before)
                    self.assertFalse(commits)
                    self.assertFalse(board.decisions)
                    self.assertEqual(asdict(run), before_run)
            self.assertTrue(await pilot.click("#decision-back"))
            await pilot.pause()
            self.assertIsNot(pilot.app.screen, review)
        self.assertFalse(commits)
        self.assertFalse(board.decisions)
        self.assertEqual(asdict(run), before_run)
        self.assertIs(FORMAL_APPROVAL_ENABLED, False)

    async def test_target_directory_and_consequences_are_readable_with_keyboard_scroll(self):
        for size in ((40, 18), (80, 24), (130, 63)):
            with self.subTest(size=size):
                review, board, run, commits, _ = self.make_review()
                before = deepcopy(asdict(review.preview))
                before_run = deepcopy(asdict(run))
                async with ReviewLayoutApp(review).run_test(size=size) as pilot:
                    await pilot.pause()
                    target = review.query_one("#decision-target", Static)
                    text = str(target.content)
                    self.assertIn("提交目标：\n  接收方：", text)
                    self.assertIn("\n  目录：" + review.preview.target.cwd, text)
                    consequence = review.target_label.splitlines()[-1]
                    self.assertIn("\n确认后果：\n  " + consequence, text)
                    body = review.query_one(".decision-body", VerticalScroll)
                    body.focus()
                    await pilot.pause()
                    collected = {}
                    for _ in range(int(body.max_scroll_y) + 2):
                        frame = review._compositor.render_strips()
                        visible = target.region.intersection(body.content_region)
                        for y in range(max(0, visible.y), min(len(frame), visible.bottom)):
                            collected[y - target.region.y] = frame[y].crop(target.region.x, target.region.right).text
                        self.assertEqual(self.rendered_button(review, "#decision-confirm"), "确认并开始下一轮")
                        self.assertEqual(self.rendered_button(review, "#decision-back"), "返回修改")
                        if body.scroll_y >= body.max_scroll_y:
                            break
                        await pilot.press("down")
                        await pilot.pause()
                    self.assertEqual(set(collected), set(range(target.region.height)),
                                     "Keyboard scrolling must expose every actual target/consequence row")
                    compact = "".join("".join(collected[y].split()) for y in sorted(collected))
                    self.assertIn("目录：" + "".join(review.preview.target.cwd.split()), compact)
                    self.assertIn("确认后果：" + "".join(consequence.split()), compact)
                    self.assertEqual(asdict(review.preview), before)
                    await pilot.press("escape")
                    await pilot.pause()
                self.assertFalse(commits)
                self.assertFalse(board.decisions)
                self.assertEqual(asdict(run), before_run)
                self.assertIs(FORMAL_APPROVAL_ENABLED, False)

    def test_answer_summary_groups_business_solution_and_selected_scope(self):
        review, _, _, commits, scope = self.make_review()
        before = deepcopy(asdict(review.preview))
        summary = review.summary()
        self.assertIn("\n   本次答复：\n     方案：正式选择：", summary)
        self.assertNotIn('本项无独立业务问题', summary)
        self.assertNotIn('业务：', summary)
        self.assertIn("\n     方案：正式选择：方案 B · 编号清单\n", summary)
        self.assertIn("\n     范围：" + scope + "\n", summary)
        self.assertNotIn("编号清单；范围：", summary)
        self.assertEqual(asdict(review.preview), before)
        self.assertFalse(commits)

    async def test_wrapped_summary_scope_retains_group_indent_and_plain_content(self):
        review, board, run, commits, scope = self.make_review()
        before = deepcopy(asdict(review.preview))
        before_run = deepcopy(asdict(run))
        async with ReviewLayoutApp(review).run_test(size=(40, 18)) as pilot:
            await pilot.pause()
            self.assert_scope_rendering(review, scope)
            self.assertEqual(asdict(review.preview), before)
            await pilot.press("escape")
            await pilot.pause()
        self.assertFalse(commits)
        self.assertFalse(board.decisions)
        self.assertEqual(asdict(run), before_run)
        self.assertIs(FORMAL_APPROVAL_ENABLED, False)
