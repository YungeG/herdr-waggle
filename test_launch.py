"""Creation tests use private temporary paths and the real OFFLINE Pi fixture only."""
import asyncio
from dataclasses import asdict
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from launch import _herdr, connected_agent, create_pi, pi_command, prepare_launch
from model import Run
from runtime import BridgeError, Runtime, install_bridge, read_json
from test_bridge import PiFixture


class LaunchTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory(prefix="taskroom-launch-test-"))
        self.project = Path(self.temp) / "中文 project"
        self.project.mkdir()
        self.runtime = Runtime(Path(self.temp) / "bridges")
        self.enterContext(patch.dict(os.environ, {"HERDR_ENV": "1", "HERDR_PANE_ID": "caller", "PI_CODING_AGENT_DIR": self.temp}))
        self.enterContext(patch("launch.shutil.which", return_value="/bin/fake"))

    def replies(self):
        return [{"pane": {"workspace_id": "workspace-actual"}},
                {"tab": {"tab_id": "new-tab", "workspace_id": "workspace-actual"},
                 "root_pane": {"pane_id": "new-pane", "workspace_id": "workspace-actual"}},
                {"type": "pane_run"}]

    def test_create_only_new_pane_preserves_focus_and_records_before_start(self):
        identity = str(uuid4())
        replies = self.replies()
        def respond(*args):
            if args[:2] == ("pane", "run"):
                record = read_json(self.runtime.root / "launches" / identity / "launch.json")
                self.assertEqual((record["state"], record["pane_id"]), ("tab_created", "new-pane"))
                self.assertEqual(args[2], "new-pane")
                command = shlex.split(args[3])
                self.assertNotIn("--approve", command)
                self.assertNotIn("--continue", command)
                self.assertIn("name with ' quote", command)
            return replies.pop(0)
        with patch("launch._herdr", side_effect=respond) as cli:
            launch = create_pi(self.runtime, str(self.project), "name with ' quote", "provider/model", identity)
        self.assertEqual(launch.state, "starting")
        self.assertEqual(cli.call_count, 3)
        self.assertIn("--no-focus", cli.call_args_list[1].args)
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

    def test_ambiguous_create_or_run_preserves_identity_and_never_retries(self):
        for stage in ("create", "run"):
            with self.subTest(stage=stage):
                responses = self.replies()[:1 if stage == "create" else 2]
                responses.append(subprocess.TimeoutExpired("herdr", 15))
                identity = str(uuid4())
                with patch("launch._herdr", side_effect=responses) as cli:
                    launch = create_pi(self.runtime, str(self.project), "test", "", identity)
                self.assertEqual(launch.state, "unknown")
                self.assertIn("不会重试", launch.message)
                self.assertEqual(cli.call_count, 2 if stage == "create" else 3)
                self.assertEqual(read_json(launch.directory(self.runtime) / "launch.json"), asdict(launch))

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

    def test_installed_bridge_is_reused_not_registered_twice(self):
        target = install_bridge(str(self.project))
        launch = prepare_launch(self.runtime, str(self.project), "test", "", str(uuid4()))
        self.assertEqual(pi_command(self.runtime, launch)[2], str(target))
        target.write_text("// unrelated existing extension")
        with self.assertRaises(BridgeError):
            prepare_launch(self.runtime, str(self.project), "test", "", str(uuid4()))


@unittest.skipUnless(shutil.which("pi"), "installed Pi required")
class OwnedPiTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_use_grant_connects_fresh_pi_without_prompt_or_execution(self):
        with tempfile.TemporaryDirectory(prefix="taskroom-owned-pi-") as directory:
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
                await fixture.request("prompt", message="/taskroom disconnect")
                await fixture.event(lambda event: event.get("method") == "notify" and "已断开" in event.get("message", ""))
                response = await fixture.request("new_session")
                self.assertTrue(response["success"], response)
                self.assertEqual(fixture.runtime.discover(), [])
                self.assertFalse((launch.directory(fixture.runtime) / "request.json").exists())
            finally:
                await fixture.close()

    async def test_expired_mismatched_unsafe_and_replayed_grants_never_connect(self):
        for invalid in ("expired", "session", "cwd", "permissions", "replayed"):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory(prefix="taskroom-denied-pi-") as directory:
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
