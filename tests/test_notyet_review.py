"""Bugs found by review (2026-11-25): each was a wrong verdict on a real setup."""
import os
import signal
import subprocess
import sys
import time

import pytest

from notyet import config, gate, snapshot, store, testrun
from notyet.findings import EngineResult
from tests.conftest import PYTEST, _git, _write

BROKEN_ADD = "def add(a, b):\n    return a + b + 1\n\n\ndef sub(a, b):\n    return a - b\n"


def commit(repo):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "setup")


def play(repo, change):
    """A whole check of `change`, from a session that starts now."""
    session = store.start_session(str(repo), f"s{time.time_ns()}", "session-start")
    change()
    decision = gate.check(str(repo), session)
    return decision, {(f.rule, f.severity) for f in decision.findings}, session


@pytest.mark.parametrize("addopts", ["-q", "-v", "-ra -q", "-vv"])
def test_verbosity_in_addopts_does_not_hide_collected_tests(repo, addopts):
    _write(repo, "pytest.ini", f"[pytest]\naddopts = {addopts}\n")
    commit(repo)
    got = testrun.collect(PYTEST, str(repo), ["tests/test_calc.py"], timeout=60)
    assert got == {"tests/test_calc.py::test_add", "tests/test_calc.py::test_sub"}


def test_collection_errors_are_named(repo):
    _write(repo, "tests/test_bad.py", "import nothing_like_this\n")
    got = testrun.collect(PYTEST, str(repo), [], timeout=60)
    assert got is not None and got.errors == {"tests/test_bad.py"} and "tests/test_calc.py::test_add" in got
    assert testrun.collect("python3 -c pass", str(repo), [], timeout=60) is None


@pytest.mark.parametrize("name", ["tests/test_calc.py.disabled", "tests/calc_old.py"])
def test_renaming_a_test_file_out_of_collection_blocks(repo, name):
    _write(repo, "tests/test_other.py", "from pkg.calc import add\n\n\ndef test_runs():\n    add(1, 2)\n")
    (repo / "tests/test_shapes.py").unlink()
    commit(repo)

    def change():
        _write(repo, "pkg/calc.py", BROKEN_ADD)
        (repo / "tests/test_calc.py").rename(repo / name)
    decision, found, _ = play(repo, change)
    assert ("test-removed", "block") in found and decision.verdict == "blocked"


def test_stopping_at_the_first_failure_hides_nothing(repo):
    _write(repo, "pytest.ini", "[pytest]\naddopts = -x\n")
    _write(repo, "tests/test_aaa.py", "def test_already_broken():\n    assert False\n")
    commit(repo)
    _, found, _ = play(repo, lambda: _write(repo, "pkg/calc.py", BROKEN_ADD))
    assert ("test-regression", "block") in found


def test_a_test_file_in_latin_1(repo):
    path = repo / "tests/test_calc.py"
    path.write_bytes(b"# -*- coding: latin-1 -*-\n# caf\xe9\n" + path.read_bytes())
    commit(repo)

    def change():
        _write(repo, "pkg/calc.py", BROKEN_ADD)
        path.write_bytes(path.read_bytes().replace(b"== 3", b"== 4  # \xe9"))
    decision, found, _ = play(repo, change)
    assert ("test-changed-to-pass", "block") in found
    assert decision.receipt_path and not any("failed" in g for g in found)


def test_an_engine_that_fails_is_never_a_pass(repo):
    def boom(ctx):
        raise RuntimeError("boom")
    session = store.start_session(str(repo), "b", "session-start")
    _write(repo, "pkg/calc.py", BROKEN_ADD)
    decision = gate.check(str(repo), session, engines=[boom, lambda ctx: EngineResult(checks=["lint"])])
    assert decision.verdict == "incomplete" and "check failed: boom" in decision.summary


def test_a_broken_config_written_mid_session_does_not_switch_the_gate_off(repo):
    def change():
        _write(repo, "pkg/calc.py", BROKEN_ADD)
        _write(repo, ".notyet.toml", '[test]\nbudget_seconds = "fast"\ncommand = 3\n[static]\nruff = []\n')
    decision, found, _ = play(repo, change)
    assert decision.verdict == "blocked" and ("test-regression", "block") in found
    cfg = config.parse('test = 3\n[rules]\nx = "note"\n[gate]\nmode = "enforce"\n', "x")
    assert cfg.enforce and cfg.rules == {"x": "note"} and cfg.problems


def test_ignored_data_is_copied_so_tests_cannot_touch_the_users(repo):
    _write(repo, ".gitignore", "data/\n")
    _write(repo, "tests/test_data.py", "import shutil\nfrom pathlib import Path\n\nfrom pkg.calc import add\n\nD = Path('data')\n\n\n"
           "def test_data():\n    shutil.copy(D / 'seed', D / 'used')\n    assert add(1, 1) == 2\n")
    commit(repo)
    _write(repo, "data/seed", "x")
    with snapshot.checkout(str(repo), snapshot.snapshot(str(repo)), "t") as tmp:
        (os.path.islink(os.path.join(tmp, "data"))) and pytest.fail("linked to the user's directory")
        os.remove(os.path.join(tmp, "data/seed"))
    assert (repo / "data/seed").exists()
    _, found, _ = play(repo, lambda: _write(repo, "pkg/calc.py", BROKEN_ADD))
    assert ("test-regression", "block") in found


def test_failures_where_ignored_data_could_not_be_copied_prove_nothing(repo, monkeypatch):
    monkeypatch.setattr(snapshot, "MAX_IGNORED_BYTES", 3)
    _write(repo, ".gitignore", "data/\n")
    _write(repo, "tests/test_data.py", "from pathlib import Path\n\nfrom pkg.calc import add\n\n\n"
           "def test_data():\n    assert Path('data/seed').read_text() and add(1, 1) == 2\n")
    commit(repo)
    _write(repo, "data/seed", "too large")
    _, found, session = play(repo, lambda: _write(repo, "pkg/calc.py", BROKEN_ADD))
    assert ("test-failing", "resolve") in found and ("test-failing-before", "note") not in found
    assert any("too large to copy" in n for n in session.runs[-1].result["not_checked"])


def test_a_session_start_run_that_times_out_is_not_evidence(repo, monkeypatch):
    from notyet.engines import execution
    real = testrun.run_pytest

    def slow_baseline(command, cwd, targets, timeout, env=None, extra=None):
        run = real(command, cwd, targets, timeout, env, extra)
        if "notyet-baseline" in cwd and "canary" not in targets[0]:
            return testrun.Run(timed_out=True, seconds=timeout)
        return run
    monkeypatch.setattr(execution.testrun, "run_pytest", slow_baseline)
    _write(repo, "tests/test_strings.py", "from pkg.strings import shout\n\n\ndef test_shout():\n    assert shout('a') == 'nope'\n")
    commit(repo)
    _, found, _ = play(repo, lambda: _write(repo, "pkg/strings.py", "def shout(s):\n    return s.upper()  # touched\n"))
    assert ("new-test-failing", "block") not in found and ("test-failing", "resolve") in found


def test_one_old_conftest_that_cannot_run_does_not_hide_the_other_files(repo):
    _write(repo, "tests/sub/conftest.py", "from pkg.strings import shout\n")
    _write(repo, "tests/sub/test_s.py", "def test_s():\n    pass\n")
    commit(repo)

    def change():
        _write(repo, "pkg/calc.py", BROKEN_ADD)
        _write(repo, "tests/test_calc.py", (repo / "tests/test_calc.py").read_text().replace("== 3", "== 4"))
        _write(repo, "tests/test_shapes.py", (repo / "tests/test_shapes.py").read_text().replace("== 10", "== 13"))
        _write(repo, "pkg/strings.py", "def loud(s):\n    return s.upper()\n")
        _write(repo, "tests/test_strings.py", "from pkg.strings import loud\n\n\ndef test_shout():\n    assert loud('a') == 'A'\n")
        _write(repo, "tests/sub/conftest.py", "from pkg.strings import loud\n")
        _write(repo, "tests/sub/test_s.py", "def test_s():\n    assert True\n")
    _, found, session = play(repo, change)
    assert ("test-changed-to-pass", "block") in found
    assert any("can't run on the new code" in f["title"] for f in session.runs[-1].result["findings"])


def test_a_conftest_edit_next_to_a_renamed_test_file(repo):
    _write(repo, "tests/conftest.py", "X = 1\n")
    commit(repo)

    def change():
        (repo / "tests/test_calc.py").rename(repo / "tests/test_math.py")
        _write(repo, "tests/conftest.py", "X = 2\n")
    _, found, _ = play(repo, change)
    assert not any(rule == "test-removed" for rule, _ in found)


def test_fixture_packages_do_not_shadow_the_standard_library(repo):
    _write(repo, "tests/data/proj/string/__init__.py", "raise ImportError('the fixture, not the stdlib')\n")
    commit(repo)
    _, found, _ = play(repo, lambda: _write(repo, "pkg/calc.py", BROKEN_ADD))
    assert ("test-regression", "block") in found


def test_a_break_in_a_package_init_selects_the_tests_of_its_modules(repo):
    from notyet import pyimports
    graph = pyimports.ImportGraph(str(repo), snapshot.snapshot(str(repo)))
    assert "tests/test_calc.py" in graph.importers(["pkg/__init__.py"])


def test_killing_the_hook_takes_its_test_run_along(repo):
    marker = repo / "started"
    _write(repo, "tests/test_calc.py", f"import os, pathlib, time\nfrom pkg.calc import add\n\n\ndef test_add():\n"
           f"    pathlib.Path({str(marker)!r}).write_text(str(os.getpid()))\n    time.sleep(60)\n")
    _write(repo, ".gitignore", "started\n")
    commit(repo)
    store.start_session(str(repo), "k", "session-start")
    _write(repo, "pkg/calc.py", BROKEN_ADD)
    root = os.path.dirname(os.path.dirname(os.path.abspath(gate.__file__)))
    hook = subprocess.Popen([sys.executable, "-m", "notyet", "hook", "claude", "stop"], stdin=subprocess.PIPE, text=True,
                            cwd=root, stdout=subprocess.DEVNULL)
    assert hook.stdin is not None
    hook.stdin.write(f'{{"session_id": "k", "cwd": "{repo}"}}')
    hook.stdin.close()
    for _ in range(200):
        if marker.exists() and marker.read_text():
            break
        time.sleep(0.1)
    pid = int(marker.read_text())
    hook.send_signal(signal.SIGTERM)
    hook.wait(timeout=20)
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    pytest.fail("the test run outlived the hook")
