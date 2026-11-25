"""What notyet's own Stop hook got wrong, or made hard to read, on its first long session (2026-11-25)."""
import json
import os
import subprocess
import sys
from pathlib import Path

from notyet import gate, store, testrun
from notyet.engines import static
from notyet.hooks import claude
from tests.conftest import PYTEST, _git, _write

ROOT = Path(__file__).parent.parent


def play(repo, change):
    session = store.start_session(str(repo), f"d{os.urandom(4).hex()}", "session-start")
    change()
    gate.check(str(repo), session)
    return session, session.runs[-1].result


def test_the_plugin_goes_by_the_events_directory_not_the_hosts(repo, tmp_path):
    """The host's CLAUDE_PROJECT_DIR named another, configured repo."""
    (repo / ".notyet.toml").unlink()
    _write(tmp_path / "other", ".notyet.toml", "")
    out = subprocess.run([sys.executable, str(ROOT / "scripts/notyet"), "hook", "claude", "stop"], text=True, capture_output=True,
                         cwd=tmp_path / "other", env={**os.environ, "CLAUDE_PROJECT_DIR": str(tmp_path / "other")},
                         input=json.dumps({"session_id": "x", "cwd": str(repo)}))
    assert out.returncode == 0 and out.stdout == "" and not (repo / ".git/notyet").exists()


def test_test_runs_do_not_see_the_hosts_hook_variables(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/somewhere")
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_API_SETTING", "the project's own")
    _write(tmp_path, "test_env.py", "import os\n\n\ndef test_env():\n    assert 'CLAUDE_PROJECT_DIR' not in os.environ\n"
                                    "    assert 'CLAUDECODE' not in os.environ\n    assert os.environ['CLAUDE_API_SETTING']\n")
    assert not testrun.run_pytest(PYTEST, str(tmp_path), ["test_env.py"], timeout=60).failures


def test_suppressions_are_comments_not_text_in_strings():
    source = 'X = "import a  # noqa: F401"\nimport b  # noqa: F401\n\n\ndef f():\n    """>>> f()  # doctest: +SKIP\n\n    # type: ignore\n    """\n'
    assert [(n, kind) for n, kind, _ in static._suppression_comments(source)] == [(2, "noqa"), (6, "doctest: +skip")]


def test_many_suppressions_are_one_item_per_file_and_ids_are_unique(repo):
    _, result = play(repo, lambda: _write(repo, "pkg/extra.py", "".join(f"import m{i}  # noqa\n" for i in range(8))))
    found = [f for f in result["findings"] if f["rule"] == "suppression-added"]
    assert len(found) == 1 and "8 suppression comment(s) in pkg/extra.py" in found[0]["title"]
    _, result = play(repo, lambda: _write(repo, "pkg/twice.py", "x = 1  # noqa\nx = 1  # noqa\n"))
    ids = [f["key"] for f in result["findings"] if f["rule"] == "suppression-added"]
    assert len(ids) == 2 == len(set(ids))


def test_many_diagnostics_are_one_item_per_file():
    new = [static.Diagnostic("a.py", n, "E1" if n < 5 else "E2", f"m{n}") for n in range(7)] + [static.Diagnostic("b.py", 1, "E1", "m")]
    found = static._findings("ruff", new)
    assert [f.title for f in found] == ["ruff: 7 new diagnostic(s) in a.py (E1 ×5, E2 ×2)", "ruff: 1 new diagnostic(s) in b.py (E1)"]
    assert len(found[0].covers) == 7      # an acknowledgment carries over while it stays about these


def test_many_edited_tests_are_one_item_per_file(repo):
    tests = "from pkg.strings import shout\n\n\n" + "".join(f"def test_{i}():\n    assert shout('{c}') == '{c.upper()}'\n\n\n"
                                                           for i, c in enumerate("abcdefgh"))
    _write(repo, "tests/test_strings.py", tests)
    _git(repo, "commit", "-qam", "many")

    def change():
        _write(repo, "pkg/strings.py", "def shout(s):\n    return s.upper() + '!'\n")
        text = tests
        for c in "ABCDEFGH":
            text = text.replace(f"'{c}'", f"'{c}!'")
        _write(repo, "tests/test_strings.py", text)
    _, result = play(repo, change)
    found = [f for f in result["findings"] if f["rule"] == "test-changed-to-pass"]
    assert [f["severity"] for f in found] == ["block"] and found[0]["title"].startswith("8 edited tests in tests/test_strings.py")


def turns(repo, *texts):
    payload = {"cwd": str(repo), "session_id": "t"}
    claude.handle("session-start", {**payload, "source": "startup"})
    for text in texts:
        claude.handle("prompt", {**payload, "prompt": "next"})
        _write(repo, "pkg/strings.py", text)
        claude.handle("stop", payload)
    return [f["rule"] for f in store.load_session(str(repo), "t").runs[-1].result["findings"] if f["rule"] == "undone-work"]


START = "def shout(s):\n    return s.upper()\n"
FIXED = "def shout(s):\n    return s.strip().upper()\n"


def test_working_on_an_earlier_line_is_not_undoing_it(repo):
    assert turns(repo, FIXED, "def shout(s):\n    return s.strip().replace('a', 'b').upper()\n") == []


def test_putting_the_old_line_back_is(repo):
    assert turns(repo, FIXED, START + "\n\ndef other():\n    return 'something else'\n") == ["undone-work"]


def test_the_receipt_says_where_the_time_went(repo):
    session, result = play(repo, lambda: _write(repo, "pkg/calc.py", "def add(a, b):\n    return a + b + 1\n"))
    assert result["timings"]["tests"] > 0 and "failures vs session start" in result["timings"]
    assert "Time: " in Path(session.runs[-1].receipt).read_text()


def test_the_whole_check_is_bounded_by_about_twice_the_budget():
    from notyet.config import Config
    assert Config(budget_seconds=150).check_seconds == 420


def test_a_file_without_an_extension_does_not_select_every_test(repo):
    _write(repo, "scripts/pkg", "#!/bin/sh\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "script")
    _write(repo, ".notyet.toml", (repo / ".notyet.toml").read_text().replace("budget_seconds", "widen_seconds = 0\nbudget_seconds"))
    _git(repo, "commit", "-qam", "no widening")
    _, result = play(repo, lambda: _write(repo, "scripts/pkg", "#!/bin/sh\necho\n"))
    assert not any("selected because" in c for c in result["checks"])
