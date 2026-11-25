"""Which files are tests, read from the repo's own pytest configuration."""
from notyet import layout, snapshot
from tests.conftest import _git, _write


def read(repo):
    return layout.Layout.read(str(repo), [snapshot.snapshot(str(repo))])


def test_python_files_and_testpaths_come_from_the_config(repo):
    _write(repo, "pyproject.toml", '[tool.pytest.ini_options]\npython_files = ["check_*.py"]\ntestpaths = ["tests"]\n')
    _write(repo, "tests/check_x.py", "")
    lay = read(repo)
    assert lay.is_test_module("tests/check_x.py") and not lay.is_test_module("tests/test_calc.py")
    assert lay.tests() == {"tests/check_x.py"}
    assert not lay.in_testpaths("pkg/check_y.py")


def test_a_config_added_this_session_cannot_hide_tests(repo):
    before = snapshot.snapshot(str(repo))
    _write(repo, "pytest.ini", "[pytest]\ntestpaths = nowhere\npython_files = nothing_*.py\n")
    lay = layout.Layout.read(str(repo), [before, snapshot.snapshot(str(repo))])
    assert lay.in_testpaths("tests/test_calc.py")


def test_a_nested_config_only_speaks_for_its_directory(repo):
    _write(repo, "backend/pytest.ini", "[pytest]\npython_files = spec_*.py\n")
    lay = read(repo)
    assert lay.is_test_module("backend/spec_a.py") and not lay.is_test_module("spec_a.py")
    assert lay.is_test_module("tests/test_calc.py")


def test_test_side(repo):
    _write(repo, "pkg/testing/__init__.py", "")
    lay = read(repo)
    assert lay.is_test_side("tests/data/golden.json") and lay.is_test_side("conftest.py")
    assert not lay.is_test_side("pkg/testing/helpers.py")      # a package that is about testing is source
    assert not lay.is_test_side("pkg/calc.py")


def test_only_the_pytest_section_counts_as_test_config():
    old = {"pyproject.toml": "[project]\nname = 'a'\n"}
    new = {"pyproject.toml": "[project]\nname = 'b'\n"}
    assert not layout.config_changed(old.get, new.get, "pyproject.toml")
    new = {"pyproject.toml": "[project]\nname = 'a'\n[tool.pytest.ini_options]\naddopts = '-k x'\n"}
    assert layout.config_changed(old.get, new.get, "pyproject.toml")
    assert layout.config_changed({}.get, {"pytest.ini": ""}.get, "pytest.ini")     # even empty, it takes over


def test_layouts_are_reread_when_the_tree_changes(repo):
    one = layout.read(str(repo), (snapshot.snapshot(str(repo)),))
    _write(repo, "tests/test_more.py", "")
    two = layout.read(str(repo), (snapshot.snapshot(str(repo)),))
    assert "tests/test_more.py" in two.paths - one.paths
    _git(repo, "status")
