"""The Claude Code plugin: notyet run from a checkout, quiet where it isn't configured."""
import json
import subprocess
import sys
from pathlib import Path

from tests.conftest import _write

ROOT = Path(__file__).parent.parent


def call(cwd, event, **payload):
    out = subprocess.run([sys.executable, str(ROOT / "scripts/notyet"), "hook", "claude", event], cwd=cwd, text=True,
                         input=json.dumps({"session_id": "p", "cwd": str(cwd), **payload}), capture_output=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout) if out.stdout.strip() else None


def test_the_hooks_gate_a_configured_repo(repo):
    call(repo, "session-start", source="startup")
    _write(repo, "pkg/strings.py", "def shout(s):\n    return s\n")
    out = call(repo, "stop")
    assert out["decision"] == "block" and f"{ROOT}/scripts/notyet ack" in out["reason"]


def test_they_do_nothing_in_a_repo_without_config(repo):
    (repo / ".notyet.toml").unlink()
    call(repo, "session-start", source="startup")
    _write(repo, "pkg/strings.py", "def shout(s):\n    return s\n")
    assert call(repo, "stop") is None and not (repo / ".git/notyet").exists()


def test_every_hook_command_points_at_the_script():
    hooks = json.loads((ROOT / "hooks/hooks.json").read_text())["hooks"]
    commands = [h["command"] for groups in hooks.values() for g in groups for h in g["hooks"]]
    assert len(commands) == 3 and all('"${CLAUDE_PLUGIN_ROOT}/scripts/notyet" hook claude ' in c for c in commands)
    from notyet import __version__
    assert json.loads((ROOT / ".claude-plugin/plugin.json").read_text())["version"] == __version__
