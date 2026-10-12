"""Launcher routing is tested without installing packages or starting a real Pi."""
import os
import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

HERE = Path(__file__).resolve().parent


class LauncherTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="waggle-launcher-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.package = self.root / "目录 with spaces"
        self.package.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name in ("run.sh", "pi.sh"):
            shutil.copyfile(HERE / name, self.package / name)
        dirname = shutil.which("dirname")
        assert dirname
        (self.bin / "dirname").symlink_to(dirname)

    def program(self, file, label, *, dependency=True):
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(f'#!/bin/sh\nif [ "$1" = "-c" ]; then exit {0 if dependency else 1}; fi\nprintf "%s\\n" "{label}" "$@"\n')
        file.chmod(0o755)

    def launch(self, script, *args):
        return subprocess.run(["/bin/sh", str(self.package / script), *args], cwd=self.root,
            env={**os.environ, "PATH": str(self.bin)}, capture_output=True, text=True, timeout=5)

    def test_uv_is_preferred_and_arguments_keep_spaces(self):
        self.program(self.bin / "uv", "uv")
        self.program(self.package / ".venv/bin/python", "venv")
        result = self.launch("run.sh", "--db", "/fake dir/tasks.sqlite3")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.splitlines(), ["uv", "run", "--no-project", "--python", "python3", str(self.package / "app.py"), "--db", "/fake dir/tasks.sqlite3"])
        result = self.launch("pi.sh", "/fake project", "--help")
        self.assertEqual(result.stdout.splitlines()[-4:], [str(self.package / "runtime.py"), "pi", "/fake project", "--help"])

    def test_without_uv_venv_and_then_python3_are_used_without_installing(self):
        self.program(self.package / ".venv/bin/python", "venv")
        self.program(self.bin / "python3", "system")
        for name, target, arguments in (("run.sh", "app.py", ("--demo",)), ("pi.sh", "runtime.py", ("/fake project",))):
            with self.subTest(name=name):
                result = self.launch(name, *arguments)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stdout.splitlines()[:2], ["venv", str(self.package / target)])
        (self.package / ".venv/bin/python").unlink()
        result = self.launch("run.sh", "--demo")
        self.assertEqual(result.stdout.splitlines(), ["system", str(self.package / "app.py"), "--demo"])
        self.assertFalse(any((self.package / path).exists() for path in ("app.py", "bridges", "tasks.sqlite3")))

    def test_missing_python_or_ui_dependency_has_setup_guidance(self):
        for name in ("run.sh", "pi.sh"):
            result = self.launch(name, "--help")
            self.assertEqual(result.returncode, 127)
            self.assertIn("DEPLOY.md", result.stderr)
        self.program(self.bin / "python3", "system", dependency=False)
        result = self.launch("run.sh")
        self.assertEqual(result.returncode, 1)
        self.assertIn("requirements.txt", result.stderr)
        result = self.launch("pi.sh", "--help")
        self.assertEqual(result.returncode, 0)  # The bridge CLI is stdlib-only; no Textual probe.
