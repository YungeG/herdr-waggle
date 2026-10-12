"""Presentation regressions using production CSS; no HUMAN or dispatch evidence."""
import unittest
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path

from textual.app import App
from textual.containers import VerticalScroll
from textual.geometry import Region
from textual.widgets import Button, Static

from budget_ledger import FORMAL_APPROVAL_ENABLED
from decision_view import DecisionReview
from decisions import Answer, DecisionSpec, Target, preview_decisions
from test_decisions import decision_board, spec_record


class ReviewLayoutApp(App):
    CSS_PATH = str(Path(__file__).with_name("style.tcss"))

    def __init__(self, review):
        super().__init__(watch_css=False)
        self.review = review

    def on_mount(self):
        self.push_screen(self.review)


class DecisionReviewLayoutTests(unittest.IsolatedAsyncioTestCase):
    def make_review(self, target_kind="new_run", *, feedback=False):
        directory = "/fixture/" + "long-review-scope-" * 6 + "/project"
        scope = directory + "（仅三条虚构资料；不涉及真实目录）"
        spec = DecisionSpec.from_record(spec_record(
            question="这三条虚构资料采用哪种展示方案？", scope=scope,
            options=[dict(option, scope=scope, label=f"方案 {option['key'].upper()} · 编号清单")
                     for option in spec_record()["options"]]))
        board, task, run = decision_board([spec], live=target_kind == "live_supplement")
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
        review = DecisionReview(preview, label, deny_commit)
        return review, board, run, commits, scope

    def rendered_button(self, review, button_id):
        button = review.query_one(button_id, Button)
        self.assertTrue(review.query_one("#decision-review").content_region.contains_region(button.region))
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
        self.assertIn("\n   本次答复：\n     业务：本项无独立业务问题\n", summary)
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
