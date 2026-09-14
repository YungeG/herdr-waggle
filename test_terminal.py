"""Launch the CLI in disposable PTYs, never a Herdr pane or the user's database."""
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from store import Store


@unittest.skipUnless(os.name == "posix", "PTY smoke requires a Unix terminal")
class TerminalTests(unittest.TestCase):
    def run_terminal(self, arguments, interactions, environment):
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
                env={**os.environ, "TERM": "xterm-256color", "COLUMNS": "80", "LINES": "24", **environment},
            )
            os.close(slave)
            slave = -1
            selector.register(master, selectors.EVENT_READ)

            def read_until(text):
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
