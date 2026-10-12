"""Explicit native smoke: one owned outer pane + disposable named Herdr, OFFLINE Pi only.

Run manually from a Herdr pane with: python3 check_native_launch.py
Not part of unittest discovery: this check briefly changes terminal topology.
"""
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import time
from unittest.mock import patch
from uuid import uuid4

from launch import connected_agent, create_pi
from model import Run
from runtime import Runtime


CLEAR = ("HERDR_SOCKET_PATH", "HERDR_CLIENT_SOCKET_PATH", "HERDR_SESSION", "HERDR_WORKSPACE_ID", "HERDR_TAB_ID", "HERDR_PANE_ID")


def command(args: list[str], env: dict[str, str]) -> dict:
    result = subprocess.run(["herdr", *args], env=env, text=True, capture_output=True, timeout=20)
    if result.returncode:
        raise RuntimeError(result.stderr or result.stdout)
    if args[:2] == ["pane", "run"] and not result.stdout.strip():
        return {}
    if args[:2] == ["pane", "read"]:
        return {"text": result.stdout}
    response = json.loads(result.stdout)
    if not isinstance(response, dict):
        raise RuntimeError("Expected a Herdr response object")
    return response


def until(check, seconds=20):
    deadline = time.monotonic() + seconds
    last = None
    while time.monotonic() < deadline:
        try:
            result = check()
            if result:
                return result
        except (OSError, ValueError, RuntimeError) as error:
            last = error
        time.sleep(0.1)
    raise AssertionError(f"Native test condition not reached: {last}")


def main():
    parent = dict(os.environ)
    if parent.get("HERDR_ENV") != "1" or not parent.get("HERDR_PANE_ID"):
        raise RuntimeError("Run this explicit topology smoke from inside Herdr.")
    clean = {key: value for key, value in parent.items() if key not in CLEAR}
    session = f"tr-launch-{str(uuid4())[:10]}"
    named = {**clean, "HERDR_SESSION": session}
    outer = None
    session_requested = False
    with tempfile.TemporaryDirectory(prefix="waggle-native-", dir="/var/tmp") as temporary:
        root = Path(temporary)
        # Enable nesting ONLY for this isolated test config, never the user's config.
        herdr_config = root / "herdr-test.toml"
        herdr_config.write_text("[experimental]\nallow_nested = true\n")
        clean["HERDR_CONFIG_PATH"] = str(herdr_config)
        named["HERDR_CONFIG_PATH"] = str(herdr_config)
        project = root / "真实 project" / "精确子目录"
        project.mkdir(parents=True)
        config = root / "pi-config"
        (config / "extensions").mkdir(parents=True)
        fixture = Path(__file__).with_name("fixture-provider.ts").resolve()
        (config / "extensions/fixture.ts").write_text(
            f"import fixture from {json.dumps(str(fixture))};\n"
            f"process.env.WAGGLE_TEST_PROJECT = {json.dumps(str(project))};\nexport default fixture;\n")
        try:
            split = command(["pane", "split", "--current", "--direction", "down", "--cwd", str(root), "--no-focus"], parent)
            outer = split["result"]["pane"]["pane_id"]
            start = ["env", *[part for key in CLEAR for part in ("-u", key)],
                     f"HERDR_CONFIG_PATH={herdr_config}", "herdr", "--session", session]
            session_requested = True
            command(["pane", "run", outer, shlex.join(start)], parent)
            try:
                panes = until(lambda: command(["pane", "list"], named)["result"]["panes"])
            except AssertionError:
                print(command(["pane", "read", outer, "--source", "recent-unwrapped", "--lines", "50"], parent))
                raise
            caller = panes[0]
            session_record = next(item for item in command(["session", "list", "--json"], named)["sessions"] if item["name"] == session)
            inner = {**named, "HERDR_SOCKET_PATH": session_record["socket_path"],
                     "HERDR_ENV": "1", "HERDR_PANE_ID": caller["pane_id"],
                     "HERDR_TAB_ID": caller["tab_id"], "HERDR_WORKSPACE_ID": caller["workspace_id"],
                     "PI_CODING_AGENT_DIR": str(config), "XDG_STATE_HOME": str(root / "state"),
                     "XDG_CONFIG_HOME": str(root / "config"), "PI_OFFLINE": "1", "PI_TELEMETRY": "0",
                     "PI_SKIP_VERSION_CHECK": "1"}
            runtime = Runtime(root / "bridges")
            with patch.dict(os.environ, inner, clear=True):
                launch = create_pi(runtime, str(project), "离线新建回归", "waggle-test/fixture", str(uuid4()))
            assert launch.state == "starting", launch
            try:
                agent = until(lambda: connected_agent(runtime, launch))
            except AssertionError:
                # Only this freshly-created test pane is inspected; no user terminal contents.
                print(command(["pane", "read", launch.pane_id, "--source", "recent-unwrapped", "--lines", "50"], named))
                raise
            assert agent.available and agent.session_id == launch.session_id and agent.cwd == str(project), agent
            assert not (project / "fixture-result.json").exists(), "Creation must not execute a task."
            tabs = command(["tab", "list", "--workspace", caller["workspace_id"]], named)["result"]["tabs"]
            assert next(tab for tab in tabs if tab["tab_id"] == caller["tab_id"])["focused"], tabs
            assert not next(tab for tab in tabs if tab["tab_id"] == launch.tab_id)["focused"], tabs
            run = Run(str(uuid4()), 1, agent.id, agent.session_id, agent.cwd, "FULL", "", "")
            runtime.dispatch(run)
            def finished():
                result = runtime.read_run(run)
                return result if result and result.terminal else None
            result = until(finished)
            assert (result.state, result.delivery) == ("finished", "full"), result
            assert json.loads((project / "fixture-result.json").read_text())["counter"] == 1
            print(json.dumps({"native_launch": "PASS", "session": session, "tab": launch.tab_id,
                              "pane": launch.pane_id, "exact_directory": True, "creation_effects": 0,
                              "explicit_dispatch_effects": 1, "focus_preserved": True}, ensure_ascii=False))
        finally:
            # Names/IDs come only from this run. Never stop/delete the default server.
            try:
                if session_requested:
                    sessions = command(["session", "list", "--json"], clean)["sessions"]
                    record = next((item for item in sessions if item["name"] == session), None)
                    if record:
                        if record["running"]:
                            command(["session", "stop", session, "--json"], clean)
                        command(["session", "delete", session, "--json"], clean)
            finally:
                if outer:
                    command(["pane", "close", outer], parent)
            print("Owned named session and outer pane cleaned up.")


if __name__ == "__main__":
    main()
