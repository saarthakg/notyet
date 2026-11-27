"""Test runs as they happen in real repos: child processes, xdist, inherited tests, hangs, configs."""
import os

from notyet import coverage, pyimports, snapshot, testrun
from notyet.engines import execution, gaming
from tests.conftest import PYTEST, _git, _write
from tests.test_notyet_execution import check, rules


def test_lines_run_in_a_child_process_count(repo):
    _write(repo, "pkg/__main__.py", "from pkg.calc import add\n\nprint(add(1, 2))\n")
    _write(repo, "tests/test_cli.py", "import subprocess\nimport sys\n\n\ndef test_cli():\n"
           "    out = subprocess.run([sys.executable, '-m', 'pkg'], capture_output=True, text=True).stdout\n    assert out == '3\\n'\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "cli")
    _write(repo, "pkg/__main__.py", "from pkg.calc import add\n\nvalue = add(1, 2)\nprint(value)\n")
    result = check(repo)
    assert [f for f in result.findings if f.rule == "untested-change"] == []


def test_lines_run_under_xdist_count(repo):
    _write(repo, "pytest.ini", "[pytest]\naddopts = -n 2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "xdist")
    _write(repo, "pkg/calc.py", "def add(a, b):\n    total = a + b\n    return total\n\n\ndef sub(a, b):\n    out = a - b\n    return out\n")
    result = check(repo)
    assert result.findings == [] and any(c.startswith("coverage: recorded") for c in result.checks)


def test_the_recorder_survives_warnings_as_errors_and_chains_sitecustomize(repo, tmp_path, monkeypatch):
    _write(tmp_path, "site/sitecustomize.py", "import os\nos.environ['SITE_RAN'] = '1'\n")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "site"))
    _write(repo, "pytest.ini", "[pytest]\nfilterwarnings = error\n")
    _write(repo, "tests/test_site.py", "import os\n\nimport pkg.calc\n\n\ndef test_site():\n    assert os.environ['SITE_RAN'] == '1'\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "strict")
    _write(repo, "pkg/calc.py", (repo / "pkg/calc.py").read_text() + "\n# touched\n")
    result = check(repo)
    assert result.findings == [] and "failed" in result.checks[0] and "0 failed" in result.checks[0]


def test_a_lost_tracer_is_not_reported_as_untested(tmp_path):
    (tmp_path / "lines.json.123").write_text('{"lost": true, "/r/a.py": [1]}')
    assert coverage.read(str(tmp_path / "lines.json"), "/r") is None
    (tmp_path / "lines.json.123").write_text('{"/r/a.py": [1]}')
    (tmp_path / "lines.json.124").write_text('{"/r/a.py": [2]}')
    assert coverage.read(str(tmp_path / "lines.json"), "/r") == {"a.py": {1, 2}}


def test_an_inherited_test_keeps_the_id_of_the_file_it_is_collected_in(tmp_path):
    _write(tmp_path, "tests/contract.py", "class Contract:\n    def test_it(self):\n        assert self.value == 1\n")
    _write(tmp_path, "tests/test_a.py", "from contract import Contract\n\n\nclass TestA(Contract):\n    value = 1\n")
    run = testrun.run_pytest(PYTEST, str(tmp_path), ["tests/test_a.py"], timeout=60)
    assert set(run.results) == {"tests/test_a.py::TestA::test_it"}


def test_ending_the_process_is_not_a_pass(tmp_path):
    _write(tmp_path, "conftest.py", "import os\nos._exit(0)\n")
    _write(tmp_path, "test_a.py", "def test_a():\n    pass\n")
    assert testrun.run_pytest(PYTEST, str(tmp_path), ["test_a.py"], timeout=60).crashed


def test_a_test_that_stopped_finishing_blocks(repo, monkeypatch):
    monkeypatch.setattr(execution, "HANG_SECONDS", 3)
    monkeypatch.setattr(testrun, "INTERRUPT_GRACE", 3)
    _write(repo, ".notyet.toml", f"[test]\ncommand = {PYTEST!r}\nbudget_seconds = 12\n")
    _git(repo, "commit", "-qam", "short budget")
    _write(repo, "pkg/strings.py", "def shout(s):\n    while True:\n        pass\n")
    result = check(repo)
    assert ("test-hangs", "block", "tests/test_strings.py::test_shout") in rules(result)


def test_a_slow_test_is_not_a_hang(repo, monkeypatch):
    monkeypatch.setattr(execution, "HANG_SECONDS", 2)
    _write(repo, "tests/test_strings.py", "import time\nfrom pkg.strings import shout\n\n\ndef test_shout():\n    time.sleep(30)\n")
    _write(repo, ".notyet.toml", f"[test]\ncommand = {PYTEST!r}\nbudget_seconds = 5\n")
    _git(repo, "commit", "-qam", "it was always slow")
    _write(repo, "pkg/strings.py", "def shout(s):\n    return s.upper()  # touched\n")
    assert not any(f.rule == "test-hangs" for f in check(repo).findings)


def test_one_broken_function_does_not_produce_a_wall_of_findings(repo):
    _write(repo, "tests/test_many.py", "import pytest\nfrom pkg.calc import add\n\n\n@pytest.mark.parametrize('n', range(30))\n"
                                       "def test_add_n(n):\n    assert add(n, 0) == n\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "many")
    _write(repo, "pkg/calc.py", "def add(a, b):\n    return a + b + 1\n\n\ndef sub(a, b):\n    return a - b\n")
    found = check(repo).findings
    assert len(found) == 3 and found[0].title.startswith("30 tests in tests/test_many.py passed at session start")


def test_the_rest_of_the_suite_runs_while_it_is_quick(repo):
    times = execution._Durations(str(repo))
    times.seconds = {"a.py": 1.0, "b.py": 500.0, "c.py": 2.0}
    assert times.affordable(["a.py", "b.py", "c.py"], 20) == [["a.py", "c.py"]]
    run = testrun.Run(results={"a.py::t": testrun.TestResult("a.py::t", "passed", "", 4.0)}, seconds=10, timed_out=True)
    times.record(["a.py", "d.py"], run)
    assert times.seconds["a.py"] == 4.0 and times.seconds["d.py"] == 20.0      # it needs more than it got
    times.save()
    assert execution._Durations(str(repo)).seconds == times.seconds


def test_conftest_fixtures_dotted_names_and_programs_are_uses(repo):
    _write(repo, "tests/conftest.py", "import pytest\nfrom pkg.shapes import perimeter\n\n\n@pytest.fixture\ndef p():\n    return perimeter\n")
    _write(repo, "tests/test_fixture.py", "def test_p(p):\n    assert p(1, 1) == 4\n")
    _write(repo, "tests/test_named.py", "from unittest import mock\n\n\ndef test_m():\n    with mock.patch('pkg.strings.shout'):\n        pass\n")
    _write(repo, "pkg/__main__.py", "")
    _write(repo, "tests/test_program.py", "import subprocess, sys\n\n\ndef test_x():\n    subprocess.run([sys.executable, '-m', 'pkg'])\n")
    _write(repo, "scripts/tool.py", "X = 1\n")
    _write(repo, "tests/test_tool.py", "import sys\nsys.path.insert(0, 'scripts')\nimport tool\n\n\ndef test_x():\n    assert tool.X\n")
    graph = pyimports.ImportGraph(str(repo), snapshot.snapshot(str(repo)))
    assert "tests/test_fixture.py" in graph.importers(["pkg/calc.py"])        # through conftest and shapes
    assert "tests/test_named.py" in graph.importers(["pkg/strings.py"])
    assert "tests/test_program.py" in graph.importers(["pkg/__main__.py"])
    assert "tests/test_tool.py" in graph.importers(["scripts/tool.py"])


def test_a_program_run_with_dash_m_is_its_main_not_every_module_in_its_package(repo):
    # found on a real pull request: git's `-m notyet` (a commit message) made a new, unimported module look used everywhere
    _write(repo, "pkg/__main__.py", "from pkg.calc import add\n")
    _write(repo, "pkg/unused.py", "X = 1\n")
    _write(repo, "tests/test_program.py", "import subprocess, sys\n\n\ndef test_x():\n    subprocess.run([sys.executable, '-m', 'pkg'])\n")
    _write(repo, "tests/test_git.py", "import subprocess\n\n\ndef test_x():\n    subprocess.run(['git', 'commit', '-m', 'pkg'])\n")
    graph = pyimports.ImportGraph(str(repo), snapshot.snapshot(str(repo)))
    assert "tests/test_program.py" in graph.importers(["pkg/calc.py"])          # through __main__
    assert "tests/test_program.py" not in graph.importers(["pkg/unused.py"])
    assert "tests/test_git.py" not in graph.importers(["pkg/calc.py"])


def test_test_detecting_source_is_fix_or_justify():
    signs = gaming._signs("import os, sys\n\n\ndef f():\n    if 'PYTEST_CURRENT_TEST' in os.environ:\n        return 1\n"
                          "    if 'pytest' in sys.modules:\n        return 2\n    return sys.modules.get('pytest')\n\n\n"
                          "class A:\n    def __eq__(self, other):\n        return True\n")
    assert [(line, rule) for line, rule, *_ in signs] == [
        (5, "source-detects-tests"), (7, "source-detects-tests"), (9, "source-detects-tests"), (13, "always-equal")]
    assert gaming._signs("def f(x):\n    '''Run pytest to check.'''\n    return 'pytest' in x\n") == []


def test_checkouts_get_export_ignored_and_needed_ignored_files(repo, tmp_path):
    _write(repo, ".gitattributes", "tests export-ignore\n")
    _write(repo, ".gitignore", "pkg/_version.py\n.venv/\nnode_modules/\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "attributes")
    _write(repo, "pkg/_version.py", "v = 1\n")
    _write(repo, ".venv/x.py", "")
    _write(repo, "node_modules/m/index.js", "")
    with snapshot.checkout(str(repo), snapshot.snapshot(str(repo)), "t") as tmp:
        assert os.path.exists(os.path.join(tmp, "tests/test_calc.py"))
        assert os.path.exists(os.path.join(tmp, "pkg/_version.py"))
        assert os.path.exists(os.path.join(tmp, "node_modules/m/index.js"))
        assert not os.path.exists(os.path.join(tmp, ".venv"))


def test_user_diff_settings_and_hidden_files_change_nothing(repo):
    root = str(repo)
    for key, value in (("diff.noprefix", "true"), ("diff.mnemonicPrefix", "true"), ("color.ui", "always"), ("diff.external", "false")):
        _git(repo, "config", key, value)
    base = snapshot.snapshot(root)
    _git(repo, "update-index", "--assume-unchanged", "pkg/calc.py")
    _write(repo, "pkg/calc.py", (repo / "pkg/calc.py").read_text() + "X = 1\n")
    now = snapshot.snapshot(root)
    deltas = snapshot.diff_trees(root, base, now)
    assert [d.path for d in deltas] == ["pkg/calc.py"]
    assert snapshot.added_lines(root, base, now, deltas) == {"pkg/calc.py": {7}}




def test_docstrings_are_not_lines_a_test_should_run():
    # found on a real pull request: a new module's docstring was the example of untested code
    source = '"""A module."""\n\n\nclass A:\n    """A class."""\n\n    def f(self):\n        """A method."""\n        return 1\n'
    assert coverage.executable_lines(source) == {4, 7, 9}
