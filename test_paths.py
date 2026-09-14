"""Bounded path lookup and real Textual selection; all paths/databases disposable."""
from contextlib import contextmanager
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

from textual import events
from textual.containers import VerticalScroll
from textual.widgets import Button, Input, OptionList, Static

from app import ProjectForm, TaskRoom
from paths import DirectorySuggestions, directory_suggestions
from runtime import Runtime
from store import Store
from test_live import wait_ui


class DirectoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(TemporaryDirectory(prefix="taskroom-path-test-"))
        self.root = Path(self.temp)
        for name in ("中文 alpha", "中文 beta", ".hidden"):
            (self.root / name).mkdir()
        (self.root / "中文.txt").write_text("Not a selectable project directory")
        (self.root / "中文 alpha/deeper").mkdir()

    def test_prefix_unicode_spaces_exact_directory_and_single_level(self):
        result = directory_suggestions(str(self.root / "中文"))
        self.assertEqual(result.paths, tuple(str(self.root / name) + "/" for name in ("中文 alpha", "中文 beta")))
        self.assertEqual(directory_suggestions(str(self.root / "中文 alpha")).paths, (str(self.root / "中文 alpha") + "/",))
        children = directory_suggestions(str(self.root) + "/")
        self.assertNotIn(str(self.root / ".hidden") + "/", children.paths)
        self.assertFalse(any("deeper" in path for path in children.paths))
        self.assertEqual(directory_suggestions(str(self.root) + "/.").paths, (str(self.root / ".hidden") + "/",))
        self.assertEqual(directory_suggestions(str(self.root / "中文.txt")).paths, ())

    def test_blank_relative_controls_do_not_scan_and_tilde_is_explicit(self):
        with patch("paths.os.scandir") as scan, patch("paths.Path.is_dir") as check:
            for value in ("", "relative/path", "bad\x00path", "x" * 5000):
                self.assertFalse(directory_suggestions(value).paths)
            scan.assert_not_called()
            check.assert_not_called()
        with patch.dict(os.environ, {"HOME": str(self.root)}):
            self.assertEqual(directory_suggestions("~").paths, (str(self.root) + "/",))
            self.assertTrue(directory_suggestions("~/中文").paths)

    def test_symlink_spelling_is_preserved_until_save_and_errors_are_explicit(self):
        link = self.root / "shortcut"
        link.symlink_to(self.root / "中文 alpha", target_is_directory=True)
        self.assertEqual(directory_suggestions(str(link)).paths, (str(link) + "/",))
        missing = directory_suggestions(str(self.root / "missing") + "/")
        self.assertFalse(missing.paths)
        self.assertIn("无法读取", missing.message)
        with patch("paths.os.scandir", side_effect=PermissionError(13, "permission denied")):
            denied = directory_suggestions(str(self.root) + "/")
        self.assertFalse(denied.paths)
        self.assertIn("permission denied", denied.message)

    def test_scanned_entries_and_results_are_bounded_and_sorted(self):
        visited = []
        class Entry:
            def __init__(self, name):
                self.name = name
            def is_dir(self):
                return True
        def entries():
            for number in range(20, 0, -1):
                visited.append(number)
                yield Entry(f"pre-{number:02}")
        @contextmanager
        def scan(_):
            yield entries()
        with patch("paths.os.scandir", side_effect=scan):
            result = directory_suggestions(str(self.root / "pre-"), limit=2, scan_limit=5)
        self.assertEqual(visited, [20, 19, 18, 17, 16])
        self.assertEqual(result.paths, (str(self.root / "pre-16") + "/", str(self.root / "pre-17") + "/"))
        self.assertIn("最多显示 2 项、检查 5 项", result.message)


class DirectoryUI(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = self.enterContext(TemporaryDirectory(prefix="taskroom-path-ui-"))
        self.root = Path(self.temp)
        self.project = self.root / "中文 project"
        self.project.mkdir()
        self.runtime = Runtime(self.root / "bridges")
        self.store = self.enterContext(Store(self.root / "tasks.sqlite3"))
        self.app = TaskRoom(store=self.store, runtime=self.runtime)

    async def open_form(self, pilot):
        await pilot.press("p", *"添加", "enter", *"我的项目", "tab")
        form = self.app.screen
        assert isinstance(form, ProjectForm)
        return form

    async def test_keyboard_completion_fills_without_saving_then_saves_exact_path_at_80_columns(self):
        async with self.app.run_test(size=(80, 24)) as pilot:
            form = await self.open_form(pilot)
            field = form.query_one("#project-path", Input)
            choices = form.query_one("#path-options", OptionList)
            self.app.post_message(events.Paste(str(self.root / "中文")))
            await wait_ui(pilot, lambda: choices.option_count == 1)
            self.assertIs(form.focused, field)
            await pilot.pause()
            body = form.query_one(".form-body", VerticalScroll)
            self.assertGreaterEqual(body.region.intersection(choices.region).height, 2, f"候选须可见：{body.region=}, {choices.region=}")
            self.assertGreaterEqual(body.region.intersection(field.region).height, 3, "候选出现后输入框仍须完整可见")
            await pilot.press("down", "enter")
            self.assertEqual(field.value, str(self.project) + "/")
            self.assertEqual(field.cursor_position, len(field.value))
            self.assertIs(form.focused, field)
            self.assertNotIn("我的项目", self.app.board.projects)
            submit = form.query_one("#submit", Button)
            self.assertLessEqual(submit.region.bottom, 24)
            self.assertGreater(submit.region.height, 0)
            await pilot.press("ctrl+enter")
            await wait_ui(pilot, lambda: not isinstance(self.app.screen, ProjectForm))
            self.assertEqual(self.app.board.projects["我的项目"], str(self.project.resolve()))
            self.assertFalse(self.runtime.root.exists())
        self.assertEqual(self.store.load().projects["我的项目"], str(self.project.resolve()))

    async def test_mouse_selection_cancel_and_native_tab(self):
        async with self.app.run_test(size=(120, 30)) as pilot:
            form = await self.open_form(pilot)
            field = form.query_one("#project-path", Input)
            field.value = str(self.project)
            choices = form.query_one("#path-options", OptionList)
            await wait_ui(pilot, lambda: choices.option_count == 1)
            await pilot.press("tab")
            self.assertIs(form.focused, choices)
            self.assertTrue(await pilot.click("#path-options", offset=(2, 0)))
            self.assertEqual(field.value, str(self.project) + "/")
            self.assertIs(form.focused, field)
            await pilot.press("escape")
            self.assertEqual(self.app.board.projects, {"收集箱": ""})
            self.assertFalse(self.runtime.root.exists())

    async def test_single_inflight_lookup_discards_stale_result_and_keeps_typing(self):
        entered, release = threading.Event(), threading.Event()
        queries = []
        def lookup(value):
            self.assertNotEqual(threading.get_ident(), threading.main_thread().ident)
            queries.append(value)
            if value.endswith("old"):
                entered.set()
                if not release.wait(5):
                    raise AssertionError("test failed to release lookup")
                return DirectorySuggestions((str(self.root / "WRONG") + "/",), "old result")
            return DirectorySuggestions((str(self.project) + "/",), "fresh result")
        with patch("app.directory_suggestions", side_effect=lookup):
            async with self.app.run_test(size=(80, 24)) as pilot:
                form = await self.open_form(pilot)
                field = form.query_one("#project-path", Input)
                try:
                    field.value = str(self.root / "old")
                    await wait_ui(pilot, entered.is_set)
                    field.value = str(self.root / "new")
                    field.cursor_position = len(field.value)
                    await pilot.press("x")
                    self.assertTrue(field.value.endswith("newx"))
                    await pilot.pause(0.3)
                    self.assertEqual(len(queries), 1)
                finally:
                    release.set()
                await wait_ui(pilot, lambda: form._suggestions_query == field.value)
                self.assertEqual(form._path_matches, (str(self.project) + "/",))
                self.assertEqual(queries, [str(self.root / "old"), str(self.root / "newx")])
                self.assertIs(form.focused, field)
                self.assertEqual(field.value, str(self.root / "newx"))
                self.assertEqual(self.app.board.projects, {"收集箱": ""})

    async def test_lookup_error_and_closed_form_never_overwrite_input_or_save(self):
        with patch("app.directory_suggestions", side_effect=PermissionError("test lookup denied")):
            async with self.app.run_test(size=(80, 24)) as pilot:
                form = await self.open_form(pilot)
                field = form.query_one("#project-path", Input)
                field.value = str(self.project)
                await wait_ui(pilot, lambda: "test lookup denied" in str(form.query_one("#path-hint", Static).content))
                self.assertEqual(field.value, str(self.project))
                self.assertIs(form.focused, field)
                self.assertFalse(form.query_one("#path-options", OptionList).display)
                await pilot.press("tab")
                self.assertIs(form.focused, form.query_one("#cancel", Button))
                await pilot.press("escape")
                self.assertEqual(self.app.board.projects, {"收集箱": ""})
        # A closed Textual App cannot be mounted again; reopen a fresh instance.
        self.app = TaskRoom(store=self.store, runtime=self.runtime)
        # A slow read finishing after Escape must not update the closed screen.
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        def slow(_):
            entered.set()
            release.wait(5)
            finished.set()
            return DirectorySuggestions((str(self.project) + "/",), "late")
        with patch("app.directory_suggestions", side_effect=slow):
            async with self.app.run_test(size=(80, 24)) as pilot:
                form = await self.open_form(pilot)
                form.query_one("#project-path", Input).value = str(self.root) + "/"
                try:
                    await wait_ui(pilot, entered.is_set)
                    await pilot.press("escape")
                    self.assertNotIsInstance(self.app.screen, ProjectForm)
                finally:
                    release.set()
                await wait_ui(pilot, finished.is_set)
                self.assertEqual(self.app.board.projects, {"收集箱": ""})


if __name__ == "__main__":
    unittest.main()
