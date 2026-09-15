"""Launch the CLI in disposable PTYs, never a Herdr pane or the user's database."""
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from store import Store


@unittest.skipUnless(os.name == "posix", "PTY smoke requires a Unix terminal")
class TerminalTests(unittest.TestCase):
    def run_terminal(self, arguments, interactions, environment, *, negotiate_resize=False):
        import errno
        import fcntl
        import pty
        import selectors
        import signal
        import struct
        import subprocess
        import termios
        import time

        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
        output = bytearray()
        selector = selectors.DefaultSelector()
        process = None
        try:
            process = subprocess.Popen(
                ["just", *arguments], cwd=Path(__file__).parent,
                stdin=slave, stdout=slave, stderr=slave,
                start_new_session=True,
                env={key: value for key, value in {
                    **os.environ, "TERM": "xterm-256color", "COLUMNS": "80", "LINES": "24", **environment,
                }.items() if value is not None},
            )
            os.close(slave)
            slave = -1
            selector.register(master, selectors.EVENT_READ)
            reported_support = reported_size = False

            def read_until(text):
                nonlocal reported_support, reported_size
                needle = text.encode()
                deadline = time.monotonic() + 10
                while needle not in output:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        self.fail(f"PTY did not render {text!r}; tail={bytes(output[-400:])!r}")
                    if selector.select(remaining):
                        try:
                            chunk = os.read(master, 65536)
                        except OSError as error:
                            if error.errno == errno.EIO:
                                chunk = b""
                            else:
                                raise
                        if not chunk:
                            self.fail(f"PTY closed before {text!r}; tail={bytes(output[-400:])!r}")
                        output.extend(chunk)
                        self.assertLess(len(output), 2_000_000, "unexpected output flood")
                        if negotiate_resize:
                            if not reported_support and b"\x1b[?2048$p" in output:
                                os.write(master, b"\x1b[?2048;2$y")
                                reported_support = True
                            if not reported_size and b"\x1b[?2048h" in output:
                                # Report 3x9-pixel cells only after in-band resize is enabled.
                                os.write(master, b"\x1b[48;24;80;216;240t")
                                reported_size = True

            for expected, keys in interactions:
                read_until(expected)
                if keys:
                    os.write(master, keys)
            read_until("\x1b[?1049l")
            self.assertEqual(process.wait(timeout=5), 0)
            self.assertNotIn(b"Traceback", output)
            return bytes(output)
        finally:
            if process is not None and process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=3)
            selector.close()
            os.close(master)
            if slave >= 0:
                os.close(slave)

    def test_herdr_defaults_to_cell_mouse_after_terminal_negotiation(self):
        with TemporaryDirectory(prefix="taskroom-pty-herdr-mouse-") as directory:
            output = self.run_terminal(["demo"], [
                ("DEMO", b"\x1b[<0;30;15M\x1b[<0;30;15m"),  # D002, not the navigation button.
                ("g 模拟确认", b"\x1b[B\r"),
                ("需要你确认样例口径", b"\x11"),
            ], {"XDG_STATE_HOME": directory, "HERDR_ENV": "1", "TEXTUAL_SMOOTH_SCROLL": None,
                "TERM_PROGRAM": None, "LC_TERMINAL": None}, negotiate_resize=True)
            self.assertIn(b"\x1b[?2048$p", output)
            self.assertNotIn(b"\x1b[?2048h", output)
            self.assertNotIn(b"\x1b[?1016h", output)
            self.assertNotIn("收起导航".encode(), output)

    def test_mouse_mode_preserves_external_terminal_defaults_and_explicit_settings(self):
        for herdr, smooth, pixels in ((None, None, True), ("0", None, True),
                                      ("1", "1", True), (None, "0", False), ("1", "0", False)):
            with self.subTest(herdr=herdr, smooth=smooth), TemporaryDirectory(prefix="taskroom-pty-mode-") as directory:
                # Same D002 cell: use 3x9-pixel coordinates only when negotiated.
                x, y = (88, 127) if pixels else (30, 15)
                output = self.run_terminal(["demo"], [
                    ("DEMO", b""),
                    ("\x1b[?1016h" if pixels else "DEMO", f"\x1b[<0;{x};{y}M\x1b[<0;{x};{y}m".encode()),
                    ("g 模拟确认", b"\x11"),
                ], {"XDG_STATE_HOME": directory, "HERDR_ENV": herdr, "TEXTUAL_SMOOTH_SCROLL": smooth,
                    "TERM_PROGRAM": None, "LC_TERMINAL": None}, negotiate_resize=True)
                self.assertIn(b"\x1b[?2048$p", output)
                self.assertEqual(b"\x1b[?2048h" in output, pixels)
                self.assertEqual(b"\x1b[?1016h" in output, pixels)
                self.assertNotIn("收起导航".encode(), output)

    def test_native_arrows_open_return_and_advance_tasks(self):
        with TemporaryDirectory(prefix="taskroom-pty-arrows-") as directory:
            self.run_terminal(["demo"], [
                ("DEMO", b"\x1b[B\x1b[B\x1b[C"),  # D003: Right opens detail.
                ("需要你确认样例口径", b"\x1b[D\x1b[B\x1b[C"),  # Left, Down, Right: D004.
                ("D004  ·  执行中", b"\t\t\r"),  # Disabled accept/rework are skipped; Tab via Records to Expand.
                ("本轮交付正文", b"\x1b[6~"),  # Scroll only after expanded content is painted.
                ("80×24 使用列表/详情切换", b"\x1b[D\x1b[B\x1b[C"),  # D005.
                ("D005  ·  待安排", b"\t\t\r"),
                ("先记下来，不需要立刻交给 Agent", b"\x11"),
            ], {"XDG_STATE_HOME": directory, "HERDR_ENV": "1", "TEXTUAL_SMOOTH_SCROLL": None,
                "TERM_PROGRAM": None, "LC_TERMINAL": None}, negotiate_resize=True)

    def test_native_mouse_card_selection_keeps_arrow_navigation(self):
        with TemporaryDirectory(prefix="taskroom-pty-mouse-") as directory:
            self.run_terminal(["demo"], [
                ("DEMO", b"\x1b[<0;6;15M\x1b[<0;6;15m"),  # Click D002 at 80x24.
                ("g 模拟确认", b"\x1b[B\r"),  # Down selects D003; Enter opens its detail.
                ("需要你确认样例口径", b"\x11"),
            ], {"XDG_STATE_HOME": directory})

    def test_native_mouse_opens_narrow_navigation_and_status_picker(self):
        with TemporaryDirectory(prefix="taskroom-pty-nav-") as directory:
            self.run_terminal(["demo"], [
                ("DEMO", b"\x1b[<0;7;2M\x1b[<0;7;2m"),  # Navigation button.
                ("工作队列", b"\x1b[<0;63;6M\x1b[<0;63;6m"),  # Middle toolbar row.
                ("筛选任务状态", b"\x11"),
            ], {"XDG_STATE_HOME": directory})

    def test_real_entry_create_draft_and_clean_exit(self):
        with TemporaryDirectory(prefix="taskroom-pty-demo-") as directory:
            path = Path(directory) / "taskroom/tasks.sqlite3"
            with Store(path) as store:
                board = store.load()
                board.create("已有记录不能混入演示", "收集箱")
                store.save(board)
            before = path.read_bytes()
            output = self.run_terminal(["demo"], [
                ("DEMO", b"n"),
                ("捕捉一个任务", "PTY，冒烟。\r".encode()),
                ("草稿已创建", b""),
                ("PTY，冒烟。", b"\x11"),
            ], {"XDG_STATE_HOME": directory})
            self.assertNotIn("已有记录不能混入演示".encode(), output)
            self.assertEqual(path.read_bytes(), before)

    def test_persistent_entry_with_spaces_reopens_exact_task(self):
        with TemporaryDirectory(prefix="taskroom-pty-local-") as directory:
            path = Path(directory) / "folder with spaces" / "任务.sqlite3"
            arguments = ["run", "--db", str(path)]
            environment = {"XDG_STATE_HOME": str(Path(directory) / "unused-default")}
            self.run_terminal(arguments, [
                ("LOCAL", b"n"),
                ("捕捉一个任务", "重启，仍在。\r".encode()),
                ("保存到本地", b"\x11"),
            ], environment)
            with Store(path) as store:
                board = store.load()
                self.assertEqual(list(board.tasks), [1])
                self.assertEqual(board.tasks[1].title, "重启，仍在。")
                self.assertEqual(board.tasks[1].run, "none")
            before = path.read_bytes()
            self.run_terminal(arguments, [("LOCAL", b""), ("重启，仍在。", b"\x11")], environment)
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse(Path(environment["XDG_STATE_HOME"]).exists())


if __name__ == "__main__":
    unittest.main()
