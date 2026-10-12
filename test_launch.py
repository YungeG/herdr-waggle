"""Creation tests use private temporary paths and the real OFFLINE Pi fixture only."""
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from launch import _herdr, connected_agent, create_pi, pi_command, prepare_launch
from model import Run
from runtime import (
    BridgeError,
    Runtime,
    bridge_entry,
    bridge_loader_content,
    install_bridge,
    read_json,
)
from test_bridge import PiFixture


class LaunchTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory(prefix="waggle-launch-test-"))
        self.project = Path(self.temp) / "中文 project"
        self.project.mkdir()
        self.runtime = Runtime(Path(self.temp) / "bridges")
        self.enterContext(patch.dict(os.environ, {"HERDR_ENV": "1", "HERDR_PANE_ID": "caller", "PI_CODING_AGENT_DIR": self.temp}))
        self.enterContext(patch("launch.shutil.which", return_value="/bin/fake"))

    def replies(self, name="test"):
        return [{"pane": {"workspace_id": "workspace-actual"}},
                {"tab": {"tab_id": "new-tab", "workspace_id": "workspace-actual"},
                 "root_pane": {"pane_id": "new-pane", "workspace_id": "workspace-actual"}},
                {"pane": {"pane_id": "new-pane", "tab_id": "new-tab",
                          "workspace_id": "workspace-actual", "label": name}},
                {"type": "pane_run"}]

    def test_create_only_new_pane_preserves_focus_and_records_before_start(self):
        identity = str(uuid4())
        replies = self.replies("name with ' quote")
        def respond(*args):
            if args[:2] in (("pane", "rename"), ("pane", "run")):
                record = read_json(self.runtime.root / "launches" / identity / "launch.json")
                self.assertEqual((record["state"], record["pane_id"]), ("tab_created", "new-pane"))
                self.assertEqual(args[2], "new-pane")
            if args[:2] == ("pane", "run"):
                command = shlex.split(args[3])
                self.assertNotIn("--approve", command)
                self.assertNotIn("--continue", command)
                self.assertEqual(command[command.index("--name") + 1], "name with ' quote")
            return replies.pop(0)
        with patch("launch._herdr", side_effect=respond) as cli:
            launch = create_pi(self.runtime, str(self.project), "name with ' quote", "provider/model", identity)
        self.assertEqual(launch.state, "starting")
        self.assertEqual(cli.call_count, 4)
        self.assertEqual(cli.call_args_list[2].args, ("pane", "rename", "new-pane", " name with ' quote"))
        tab_args = cli.call_args_list[1].args
        self.assertIn("--no-focus", tab_args)
        self.assertEqual(tab_args[tab_args.index("--label") + 1], f"Pi · name with ' quote · {identity[:8]}")
        self.assertEqual(cli.call_args_list[0].args, ("pane", "current", "--current"))
        self.assertIsNone(connected_agent(self.runtime, launch))
        self.assertFalse((self.project / ".pi").exists())
        grant = read_json(launch.directory(self.runtime) / "request.json")
        self.assertEqual((grant["session_id"], grant["cwd"]), (launch.session_id, str(self.project)))
        self.assertEqual((launch.directory(self.runtime) / "request.json").stat().st_mode & 0o777, 0o600)
        # Same creation identity cannot make another tab, even after an API acknowledgement was lost.
        with patch("launch._herdr", return_value=self.replies()[0]) as cli:
            with self.assertRaises(FileExistsError):
                create_pi(self.runtime, str(self.project), "again", "", identity)
        self.assertEqual(cli.call_count, 1)

    def test_ambiguous_create_rename_or_run_preserves_identity_and_never_retries(self):
        for index, stage in enumerate(("create", "rename", "run"), start=1):
            with self.subTest(stage=stage):
                responses = self.replies()[:index]
                responses.append(subprocess.TimeoutExpired("herdr", 15))
                identity = str(uuid4())
                with patch("launch._herdr", side_effect=responses) as cli:
                    launch = create_pi(self.runtime, str(self.project), "test", "", identity)
                self.assertEqual(launch.state, "unknown")
                self.assertIn("不会重试", launch.message)
                self.assertEqual(cli.call_count, index + 1)
                self.assertEqual(read_json(launch.directory(self.runtime) / "launch.json"), asdict(launch))
                if stage != "create":
                    self.assertEqual((launch.tab_id, launch.pane_id), ("new-tab", "new-pane"))

    def test_unconfirmed_pane_name_or_identity_never_starts_pi(self):
        pane = self.replies()[2]["pane"]
        invalid = [BridgeError("rename denied"), {}, {"pane": None}, {"pane": "invalid"},
                   *({"pane": {**pane, key: "different"}} for key in pane)]
        for response in invalid:
            with self.subTest(response=response):
                with patch("launch._herdr", side_effect=[*self.replies()[:2], response]) as cli:
                    launch = create_pi(self.runtime, str(self.project), "test", "", str(uuid4()))
                self.assertEqual(launch.state, "unknown")
                self.assertEqual(cli.call_count, 3)
                self.assertEqual(cli.call_args.args[:3], ("pane", "rename", "new-pane"))
                self.assertEqual((launch.tab_id, launch.pane_id), ("new-tab", "new-pane"))
                self.assertEqual(read_json(launch.directory(self.runtime) / "launch.json"), asdict(launch))

    def test_pane_names_are_values_not_cli_options(self):
        for name in ("--clear", "--help", "-h", "中文任务 ' 引号 🧪"):
            with self.subTest(name=name):
                with patch("launch._herdr", side_effect=self.replies(name)) as cli:
                    launch = create_pi(self.runtime, str(self.project), name, "", str(uuid4()))
                self.assertEqual(launch.state, "starting")
                self.assertEqual(cli.call_args_list[2].args, ("pane", "rename", "new-pane", f" {name}"))
                command = shlex.split(cli.call_args_list[3].args[3])
                self.assertEqual(command[command.index("--name") + 1], name)

    def test_validation_and_write_failure_cannot_start_anything(self):
        with patch.dict(os.environ, {"HERDR_ENV": "0"}), patch("launch._herdr") as cli:
            with self.assertRaises(BridgeError):
                create_pi(self.runtime, str(self.project), "test", "", str(uuid4()))
            cli.assert_not_called()
        with patch("launch._herdr", return_value=self.replies()[0]) as cli, patch("launch.publish", side_effect=OSError("disk denied")):
            with self.assertRaises(OSError):
                create_pi(self.runtime, str(self.project), "test", "", str(uuid4()))
            self.assertEqual(cli.call_count, 1)
        with self.assertRaises(BridgeError):
            prepare_launch(self.runtime, str(self.project), "bad\nname", "", str(uuid4()))

    def test_native_pane_run_empty_success_is_acknowledged_but_other_bad_json_is_not(self):
        result = subprocess.CompletedProcess(["herdr"], 0, "", "")
        with patch("launch.subprocess.run", return_value=result):
            self.assertEqual(_herdr("pane", "run", "new-pane", "pi")["type"], "pane_run")
            with self.assertRaises(BridgeError):
                _herdr("tab", "create")
            with self.assertRaises(BridgeError):
                _herdr("pane", "rename", "new-pane", " test")

    def test_legacy_entry_is_refused_before_new_tab_grant_or_installation(self):
        extensions = self.project / ".pi/extensions"
        extensions.mkdir(parents=True)
        legacy = extensions / "taskroom.ts"
        content = '// Taskroom: old project loader\n' + bridge_loader_content().splitlines(keepends=True)[1]
        legacy.write_text(content)
        unrelated = extensions / "confirmation.ts"
        unrelated.write_text("export default function () {}\n")
        for modern in (False, True):
            with self.subTest(modern=modern):
                if modern:
                    (extensions / "waggle.ts").write_text(bridge_loader_content())
                with patch("launch._herdr", return_value=self.replies()[0]) as cli:
                    with self.assertRaisesRegex(BridgeError, "taskroom.ts"):
                        create_pi(self.runtime, str(self.project), "test", "", str(uuid4()))
                    self.assertEqual([call.args for call in cli.call_args_list], [("pane", "current", "--current")])
                with self.assertRaisesRegex(BridgeError, "taskroom.ts"):
                    bridge_entry(str(self.project))
                with self.assertRaisesRegex(BridgeError, "taskroom.ts"):
                    install_bridge(str(self.project))
                self.assertFalse(self.runtime.root.exists())
                self.assertEqual(legacy.read_text(), content)
                self.assertEqual(unrelated.read_text(), "export default function () {}\n")
                self.assertEqual((extensions / "waggle.ts").exists(), modern)
        # Explicitly moving the legacy loader out is a user's action, not automatic migration.
        backup = self.project / ".pi/loader-backups"
        backup.mkdir()
        legacy.rename(backup / legacy.name)
        self.assertEqual(bridge_entry(str(self.project)), extensions / "waggle.ts")
        self.assertEqual((backup / "taskroom.ts").read_text(), content)

    def test_dangling_legacy_link_is_not_followed_or_silently_ignored(self):
        extensions = self.project / ".pi/extensions"
        extensions.mkdir(parents=True)
        legacy = extensions / "taskroom.ts"
        legacy.symlink_to(self.project / "missing.ts")
        with self.assertRaisesRegex(BridgeError, "taskroom.ts"):
            prepare_launch(self.runtime, str(self.project), "test", "", str(uuid4()))
        self.assertTrue(legacy.is_symlink())
        self.assertFalse(self.runtime.root.exists())

    def test_installed_bridge_is_reused_not_registered_twice(self):
        target = install_bridge(str(self.project))
        launch = prepare_launch(self.runtime, str(self.project), "test", "", str(uuid4()))
        self.assertEqual(pi_command(self.runtime, launch)[2], str(target))
        target.write_text("// unrelated existing extension")
        with self.assertRaises(BridgeError):
            prepare_launch(self.runtime, str(self.project), "test", "", str(uuid4()))


@unittest.skipUnless(shutil.which("pi"), "installed Pi required")
class OwnedPiTests(unittest.IsolatedAsyncioTestCase):
    async def test_moved_legacy_entry_allows_one_bridge_without_disabling_other_plugins(self):
        with tempfile.TemporaryDirectory(prefix="waggle-loader-recovery-") as directory:
            fixture = PiFixture(Path(directory))
            extensions = fixture.project / ".pi/extensions"
            extensions.mkdir(parents=True)
            legacy = extensions / "taskroom.ts"
            original = '// Taskroom: old project loader\n' + bridge_loader_content().splitlines(keepends=True)[1]
            legacy.write_text(original)
            other = extensions / "confirmation.ts"
            other.write_text('export default function(pi) { pi.registerCommand("fixture-project-confirmation", { description: "Offline project plugin", handler: async () => {} }); }\n')
            with self.assertRaisesRegex(BridgeError, "taskroom.ts"):
                prepare_launch(fixture.runtime, str(fixture.project), "test", "", str(uuid4()))
            self.assertFalse(fixture.runtime.root.exists())
            backup = fixture.project / ".pi/loader-backups"
            backup.mkdir()
            legacy.rename(backup / legacy.name)  # Explicit test action, never performed by the preflight.
            try:
                await fixture.start(launcher="installed")
                commands = await fixture.request("get_commands")
                names = [item["name"] for item in commands["data"]["commands"]]
                self.assertEqual(names.count("waggle"), 1)
                self.assertIn("fixture-project-confirmation", names)
                agent = await fixture.connect()
                self.assertIsNotNone(agent)
                self.assertFalse((fixture.project / "fixture-result.json").exists())
                self.assertEqual((backup / legacy.name).read_text(), original)
                self.assertTrue(other.is_file())
            finally:
                await fixture.close()

    async def test_single_use_grant_connects_fresh_pi_without_prompt_or_execution(self):
        with tempfile.TemporaryDirectory(prefix="waggle-owned-pi-") as directory:
            fixture = PiFixture(Path(directory))
            launch = prepare_launch(fixture.runtime, str(fixture.project), "Owned offline Pi", "", str(uuid4()))
            try:
                await fixture.start(launch=launch)
                agent = connected_agent(fixture.runtime, launch)
                assert agent is not None
                self.assertTrue(agent.available)
                self.assertEqual(agent.id, launch.id)
                self.assertFalse(any(event.get("method") == "confirm" for event in fixture.events))
                self.assertFalse((fixture.project / "fixture-result.json").exists())
                self.assertFalse((launch.directory(fixture.runtime) / "request.json").exists())
                self.assertTrue((launch.directory(fixture.runtime) / "consumed.json").exists())
                # Creation isn't dispatch. Only the subsequent explicit task produces an effect.
                run = Run(str(uuid4()), 1, agent.id, agent.session_id, agent.cwd, "FULL", "", "")
                fixture.runtime.dispatch(run)
                result = await fixture.result(run)
                self.assertEqual((result.state, result.delivery), ("finished", "full"))
                self.assertEqual(json.loads((fixture.project / "fixture-result.json").read_text())["counter"], 1)
                await fixture.request("prompt", message="/waggle disconnect")
                await fixture.event(lambda event: event.get("method") == "notify" and "已断开" in event.get("message", ""))
                response = await fixture.request("new_session")
                self.assertTrue(response["success"], response)
                self.assertEqual(fixture.runtime.discover(), [])
                self.assertFalse((launch.directory(fixture.runtime) / "request.json").exists())
            finally:
                await fixture.close()

    async def test_expired_mismatched_unsafe_and_replayed_grants_never_connect(self):
        for invalid in ("expired", "session", "cwd", "permissions", "replayed"):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory(prefix="waggle-denied-pi-") as directory:
                fixture = PiFixture(Path(directory))
                launch = prepare_launch(fixture.runtime, str(fixture.project), "Denied test", "", str(uuid4()))
                path = launch.directory(fixture.runtime) / "request.json"
                grant = read_json(path)
                if invalid == "expired":
                    grant["expires"] = 1
                elif invalid == "session":
                    grant["session_id"] = str(uuid4())
                elif invalid == "cwd":
                    grant["cwd"] = str(fixture.project.parent)
                path.write_text(json.dumps(grant))
                if invalid == "permissions":
                    path.chmod(0o644)
                elif invalid == "replayed":
                    os.link(path, path.with_name("consumed.json"))
                try:
                    await fixture.start(launch=launch)
                    self.assertEqual(fixture.runtime.discover(), [])
                    self.assertFalse((fixture.project / "fixture-result.json").exists())
                    self.assertTrue(any(event.get("method") == "notify" and "桥接已暂停" in event.get("message", "") for event in fixture.events))
                    if invalid == "expired":
                        # Recovery is a NEW native confirmation, never replay of expired consent.
                        agent = await fixture.connect()
                        assert agent is not None
                        self.assertNotEqual(agent.id, launch.id)
                        run = Run(str(uuid4()), 1, agent.id, agent.session_id, agent.cwd, "FULL", "", "")
                        fixture.runtime.dispatch(run)
                        self.assertEqual((await fixture.result(run)).state, "finished")
                finally:
                    await fixture.close()


if __name__ == "__main__":
    unittest.main()
