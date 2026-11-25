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


def test_a_constant_that_moved_and_its_import_are_not_undone(repo):
    payload = {"cwd": str(repo), "session_id": "m"}
    claude.handle("session-start", {**payload, "source": "startup"})
    claude.handle("prompt", {**payload, "prompt": "a limit"})
    _write(repo, "pkg/limits.py", "MAX_FAILED = 3\n")
    _write(repo, "pkg/strings.py", "from pkg.limits import MAX_FAILED\n" + START)
    claude.handle("prompt", {**payload, "prompt": "put it in config"})
    _write(repo, "pkg/limits.py", "")
    _write(repo, "pkg/config.py", "MAX_FAILED = 5\n")
    _write(repo, "pkg/strings.py", START)
    claude.handle("stop", payload)
    assert "undone-work" not in [f["rule"] for f in store.load_session(str(repo), "m").runs[-1].result["findings"]]


def test_lines_run_in_a_child_with_its_own_pythonpath_count(repo):
    """The usual CLI test: subprocess.run(..., env={**os.environ, "PYTHONPATH": SRC})."""
    _write(repo, "pkg/__main__.py", "print(1)\n")
    _write(repo, "tests/test_cli.py", "import os\nimport subprocess\nimport sys\n\n\ndef test_cli():\n"
           "    env = {**os.environ, 'PYTHONPATH': os.getcwd()}\n"
           "    p = subprocess.run([sys.executable, '-m', 'pkg'], capture_output=True, text=True, env=env)\n"
           "    assert p.returncode == 0 and 'NOTYET_TRACE_OUT' not in {}\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "cli")
    _, result = play(repo, lambda: _write(repo, "pkg/__main__.py", "value = 2\nprint(value)\n"))
    assert [f for f in result["findings"] if f["rule"] == "untested-change"] == []


def test_untested_lines_that_only_moved_are_not_raised(repo):
    body = "\n\ndef rare(x):\n    if x == 'never':\n        return 'this line was never run'\n    return x\n"
    _write(repo, "pkg/strings.py", START + body)
    _write(repo, "tests/test_rare.py", "from pkg.strings import rare\n\n\ndef test_rare():\n    assert rare(1) == 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "rare")

    def change():
        _write(repo, "pkg/strings.py", "from pkg.rare import rare  # moved\n\n\n" + START)
        _write(repo, "pkg/rare.py", body.lstrip())
    _, result = play(repo, change)
    assert [f for f in result["findings"] if f["rule"] == "untested-change"] == []


def test_suppressions_in_tests_are_not_raised(repo):
    _, result = play(repo, lambda: _write(repo, "tests/test_bad_input.py", "from pkg.calc import add\n\n\ndef test_x():\n"
                                                                         "    assert add('a', 'b') == 'ab'  # type: ignore\n"))
    assert "suppression-added" not in [f["rule"] for f in result["findings"]]


def test_a_test_setup_that_cannot_call_the_new_code_is_not_a_block(repo):
    """An internal constructor gained an argument; conftest builds one for every test."""
    _write(repo, "pkg/deps.py", "class Deps:\n    def __init__(self, a):\n        self.a = a\n")
    _write(repo, "tests/conftest.py", "import pytest\n\nfrom pkg.deps import Deps\n\n\n@pytest.fixture\ndef deps():\n    return Deps(1)\n")
    _write(repo, "tests/test_deps.py", "def test_deps(deps):\n    assert deps.a == 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "deps")

    def change():
        _write(repo, "pkg/deps.py", "class Deps:\n    def __init__(self, a, b):\n        self.a, self.b = a, b\n")
        _write(repo, "tests/conftest.py", (repo / "tests/conftest.py").read_text().replace("Deps(1)", "Deps(1, 2)"))
    _, result = play(repo, change)
    assert [f["severity"] for f in result["findings"] if f["rule"] == "test-changed-to-pass"] == ["resolve"]


def test_a_hand_over_is_this_turns_news_only_once(repo):
    root, payload = str(repo), {"cwd": str(repo), "session_id": "h"}
    claude.handle("session-start", {**payload, "source": "startup"})
    claude.handle("prompt", {**payload, "prompt": "reverse sub"})
    _write(repo, "pkg/calc.py", "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return b - a\n")
    _write(repo, "tests/test_calc.py", (repo / "tests/test_calc.py").read_text().replace("sub(3, 1) == 2", "sub(3, 1) == -2"))
    assert claude.handle("stop", payload)["decision"] == "block"
    session = store.load_session(root, "h")
    fid = session.runs[-1].finding_ids[0]
    session.acks[fid] = {"category": "needs-human", "reason": "asked for", "at": session.turns[-1]["at"] + 1}
    store.save_session(root, session)
    assert "NEEDS YOUR REVIEW" in claude.handle("stop", payload)["systemMessage"]
    claude.handle("prompt", {**payload, "prompt": "now something unrelated"})
    session = store.load_session(root, "h")
    session.turns[-1]["at"] += 5
    store.save_session(root, session)
    _write(repo, "pkg/strings.py", "def shout(s):\n    return s.upper()  # louder\n")
    message = claude.handle("stop", payload)["systemMessage"]
    assert message.startswith("notyet: PASSED") and "1 item(s) handed to you earlier" in message


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
