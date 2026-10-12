"""Local source releases never package task state or execute project code."""
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from release import ReleaseError, build_package, source_files


@unittest.skipUnless(shutil.which("git"), "Git is required to select release sources")
class ReleaseTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(prefix="waggle-release-test-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "源码 with spaces"
        self.root.mkdir()
        self.output = Path(temp.name) / "output"
        self.git("init", "-q")
        self.put(".gitignore", "private/\ndist/\n")
        self.put("release-files.txt", "*.py\n*.ts\n*.tcss\nREADME.md\nDEPLOY.md\nrun.sh\npi.sh\nrelease-files.txt\n.gitignore\n")
        for name in ("runtime.py", "release.py"):
            self.put(name, "pass\n")
        for name in ("pi-bridge.ts", "style.tcss", "README.md", "DEPLOY.md"):
            self.put(name, "fixture\n")
        for name in ("run.sh", "pi.sh"):
            self.put(name, "#!/bin/sh\nexit 0\n")
        self.put("app.py", self.app("1.0"))

    def app(self, version):
        return f'# /// script\n# requires-python = ">=3.11"\n# dependencies = ["fixture-package=={version}"]\n# ///\nprint("not executed by packaging")\n'

    def put(self, name, content):
        file = self.root / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content)
        return file

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, text=True, check=True)

    def payload(self, archive):
        with tarfile.open(archive) as bundle:
            result = {}
            for member in bundle.getmembers():
                self.assertTrue(member.isfile())
                self.assertEqual((member.uid, member.gid), (0, 0))
                stream = bundle.extractfile(member)
                assert stream is not None
                with stream:
                    result[member.name.removeprefix("waggle/")] = stream.read()
                if member.name.endswith(("/run.sh", "/pi.sh")):
                    self.assertEqual(member.mode, 0o755)
            return result

    def test_current_modified_untracked_and_nested_sources_not_git_head(self):
        self.put("deleted.py", "pass\n")
        self.git("add", "app.py", "deleted.py")
        self.put("app.py", self.app("2.0"))
        (self.root / "deleted.py").unlink()
        self.put("ui/new module.py", "new = True\n")
        archive = build_package(self.root, self.output)
        data = self.payload(archive)
        self.assertEqual(data["app.py"], self.app("2.0").encode())
        self.assertIn("ui/new module.py", data)
        self.assertNotIn("deleted.py", data)
        self.assertEqual(data["requirements.txt"], b"fixture-package==2.0\n")
        manifest = json.loads(data["SOURCE-MANIFEST.json"])
        self.assertTrue(manifest["working_tree_snapshot"])
        self.assertEqual(manifest["tests"], "not_run")
        for name, checksum in manifest["files"].items():
            self.assertEqual(hashlib.sha256(data[name]).hexdigest(), checksum)
        for line in data["SHA256SUMS"].decode().splitlines():
            checksum, name = line.split("  ", 1)
            self.assertEqual(hashlib.sha256(data[name]).hexdigest(), checksum)
        checksum, filename = (archive.parent / "SHA256SUMS").read_text().strip().split("  ", 1)
        self.assertEqual(filename, archive.name)
        self.assertEqual(checksum, hashlib.sha256(archive.read_bytes()).hexdigest())

    def test_private_ignored_and_non_allowlisted_files_are_not_read_or_packaged(self):
        for name in (".pi/secret.py", ".local/secret.py", ".env", "sessions/secret.py", "bridges/secret.py", "previews/secret.py", "tasks.sqlite3", "auth.json", "private/secret.py"):
            self.put(name, "PRIVATE-NOT-FOR-RELEASE")
        self.git("add", "-f", ".pi/secret.py", "sessions/secret.py", "tasks.sqlite3", "private/secret.py")
        for name in (".pi/secret.py", "sessions/secret.py", "private/secret.py"):
            (self.root / name).chmod(0)  # The selector must exclude these before opening content.
        data = self.payload(build_package(self.root, self.output))
        self.assertFalse(any(b"PRIVATE-NOT-FOR-RELEASE" in content for content in data.values()))
        self.assertFalse(any("secret" in name or name.endswith(".sqlite3") for name in data))

    def test_link_pipe_and_linked_parent_fail_closed(self):
        outside = self.root.parent / "outside.py"
        outside.write_text("private = True\n")
        link = self.root / "linked.py"
        link.symlink_to(outside)
        with self.assertRaises(ReleaseError):
            build_package(self.root, self.output)
        link.unlink()
        # Git does not enumerate FIFOs; a previously tracked source replaced by one must still fail.
        self.put("linked.py", "pass\n")
        self.git("add", "linked.py")
        link.unlink()
        os.mkfifo(link, 0o600)
        with self.assertRaises(ReleaseError):
            build_package(self.root, self.output)
        link.unlink()
        self.git("rm", "--cached", "-f", "linked.py")
        nested = self.put("nested/module.py", "pass\n")
        self.git("add", "nested/module.py")
        nested.unlink()
        nested.parent.rmdir()
        nested.parent.symlink_to(self.root.parent, target_is_directory=True)
        (self.root.parent / "module.py").write_text("private = True\n")
        with self.assertRaises(ReleaseError):
            build_package(self.root, self.output)

    def test_missing_entries_invalid_metadata_or_syntax_do_not_publish(self):
        (self.root / "style.tcss").unlink()
        with self.assertRaises(ReleaseError):
            build_package(self.root, self.output)
        self.put("style.tcss", "fixture\n")
        self.put("app.py", "print('no script metadata')\n")
        with self.assertRaises(ReleaseError):
            build_package(self.root, self.output)
        self.put("app.py", self.app("1.0"))
        self.put("broken.py", "def broken(\n")
        with self.assertRaises(ReleaseError):
            build_package(self.root, self.output)
        self.assertFalse(self.output.exists())

    def test_repeated_release_preserves_previous_archive(self):
        first = build_package(self.root, self.output)
        before = first.read_bytes()
        self.put("new.py", "added = True\n")
        second = build_package(self.root, self.output)
        self.assertNotEqual(first, second)
        self.assertEqual(first.read_bytes(), before)
        self.assertIn("new.py", self.payload(second))

    def test_existing_release_directory_is_not_overwritten_on_name_collision(self):
        with patch("release.datetime") as clock, patch("release.uuid4") as nonce:
            clock.now.return_value = datetime(2030, 1, 1, tzinfo=timezone.utc)
            nonce.return_value.hex = "a" * 32
            archive = build_package(self.root, self.output)
            before = archive.read_bytes()
            with self.assertRaises(ReleaseError):
                build_package(self.root, self.output)
            self.assertEqual(archive.read_bytes(), before)

    def test_source_changed_during_capture_fails_without_publication(self):
        from release import read_source
        reads = 0

        def changing_read(source, name):
            nonlocal reads
            result = read_source(source, name)
            if name == "app.py":
                reads += 1
                if reads == 1:
                    self.put("app.py", self.app("2.0"))
            return result

        with patch("release.read_source", side_effect=changing_read):
            with self.assertRaisesRegex(ReleaseError, "源码发生变化"):
                build_package(self.root, self.output)
        self.assertFalse(self.output.exists())

    def test_crlf_metadata_is_supported_and_dependency_directives_are_rejected(self):
        self.put("app.py", self.app("1.0").replace("\n", "\r\n"))
        self.assertEqual(self.payload(build_package(self.root, self.output))["requirements.txt"], b"fixture-package==1.0\n")
        self.put("app.py", self.app("1.0").replace("fixture-package==1.0", "--extra-index-url https://example.invalid"))
        with self.assertRaises(ReleaseError):
            build_package(self.root, self.output)

    def test_archive_write_failure_leaves_no_release_or_temporary_directory(self):
        with patch("release.tarfile.open", side_effect=OSError("fixture write failure")):
            with self.assertRaises(OSError):
                build_package(self.root, self.output)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_manifest_cannot_select_private_files_and_inventory_is_read_only(self):
        before = (self.root / "app.py").read_bytes()
        self.put("release-files.txt", "../outside.py\n")
        with self.assertRaises(ReleaseError):
            source_files(self.root)
        self.put("release-files.txt", ".pi/*.py\n*.py\n")
        self.put(".pi/private.py", "private = True\n")
        self.assertNotIn(".pi/private.py", source_files(self.root))
        self.assertEqual((self.root / "app.py").read_bytes(), before)
        self.assertFalse(self.output.exists())
