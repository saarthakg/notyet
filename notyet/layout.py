"""Which files are tests, and which are test support, in this repo.

Read from the repo's own pytest configuration (`python_files`, `testpaths`), in
the session-start tree and the current one together: a config the session
changed can add tests, but it can't make a test file stop counting as one.

The *test side* is everything that decides what the tests check rather than
what the code does: test modules, conftest.py, anything under a tests
directory (helpers, fixtures, golden files), and pytest's configuration.
"""
import configparser
import fnmatch
import functools
import tomllib
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Callable, Iterable, Optional

from notyet import snapshot

CONFIG_NAMES = ("pytest.ini", ".pytest.ini", "pytest.toml", ".pytest.toml", "pyproject.toml", "tox.ini", "setup.cfg")
DEFAULT_PYTHON_FILES = ("test_*.py", "*_test.py")
MAX_CONFIG_DEPTH = 3


def is_test_module(path: str) -> bool:
    """By pytest's default naming; Layout.is_test_module also knows the repo's own."""
    return any(fnmatch.fnmatch(PurePosixPath(path).name, p) for p in DEFAULT_PYTHON_FILES)


def in_test_dir(path: str) -> bool:
    return any(p in ("tests", "test", "testing") for p in PurePosixPath(path).parts[:-1])


def pytest_section(name: str, text: Optional[str]) -> Optional[dict]:
    """The pytest options in a config file: a plain dict of raw values, or None if it has no pytest section."""
    if text is None:
        return None
    if name.endswith(".toml"):
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            return None
        if name != "pyproject.toml":
            return data.get("pytest", data)
        tool = data.get("tool", {}).get("pytest")
        return None if tool is None else tool.get("ini_options", tool)
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(text)
    except configparser.Error:
        return None
    section = "tool:pytest" if name == "setup.cfg" else "pytest"
    if parser.has_section(section):
        return dict(parser.items(section))
    return {} if name in ("pytest.ini", ".pytest.ini") else None    # an empty pytest.ini still wins


def _words(value) -> list[str]:
    if isinstance(value, str):
        return value.split()
    return [str(v) for v in value or []]


@dataclass
class Layout:
    paths: set[str]
    python_files: dict[str, list[str]] = field(default_factory=dict)    # config directory -> patterns
    testpaths: list[list[str]] = field(default_factory=list)            # one list per tree read
    doctests: set[str] = field(default_factory=set)                     # modules --doctest-modules runs examples of

    @classmethod
    def read(cls, root: str, trees: Iterable[str]) -> "Layout":
        paths: set[str] = set()
        out = cls(paths)
        for tree in dict.fromkeys(trees):
            listed = [p for p in snapshot.git(root, "ls-tree", "-r", "-z", "--name-only", tree).split("\0") if p]
            paths |= set(listed)
            configs: dict[str, dict] = {}
            for path in listed:
                parts = PurePosixPath(path)
                folder = "" if str(parts.parent) == "." else str(parts.parent)
                if parts.name in CONFIG_NAMES and len(parts.parts) <= MAX_CONFIG_DEPTH:
                    section = pytest_section(parts.name, snapshot.show(root, tree, path))
                    if section is not None:
                        configs.setdefault(folder, {})[parts.name] = section
            for folder, found in configs.items():
                section = next(found[n] for n in CONFIG_NAMES if n in found)     # pytest's order of precedence
                if patterns := _words(section.get("python_files")):
                    known = out.python_files.setdefault(folder, [])
                    known += [p for p in patterns if p not in known]
                if folder == "":
                    out.testpaths.append(_words(section.get("testpaths")))
                    if "--doctest-modules" in _words(section.get("addopts")):
                        out.doctests |= _with_examples(root, tree)
            if "" not in configs:
                out.testpaths.append([])
        return out

    def is_test_module(self, path: str) -> bool:
        p = PurePosixPath(path)
        if p.suffix != ".py" or p.name == "conftest.py":
            return False
        patterns: Iterable[str] = DEFAULT_PYTHON_FILES
        for folder in [str(f) if str(f) != "." else "" for f in p.parents]:    # the nearest config that names any
            if folder in self.python_files:
                patterns = self.python_files[folder]
                break
        return any(fnmatch.fnmatch(path if "/" in pat else p.name, pat) for pat in patterns)

    def in_test_dir(self, path: str) -> bool:
        """Under a tests directory. `test`/`testing` inside a package (numpy/testing, django/test) is source."""
        parts = PurePosixPath(path).parts[:-1]
        for i, part in enumerate(parts):
            if part == "tests":
                return True
            if part in ("test", "testing") and f"{'/'.join(parts[:i])}/__init__.py" not in self.paths:
                return True
        return False

    def in_testpaths(self, path: str) -> bool:
        """Would a bare `pytest` look here? (Under the testpaths of either tree.)"""
        return any(not paths or any(_under(path, t) for t in paths) for paths in self.testpaths or [[]])

    def is_test_side(self, path: str) -> bool:
        return (PurePosixPath(path).name == "conftest.py" or self.is_test_module(path) or self.in_test_dir(path)
                or any(_under(path, t) for paths in self.testpaths for t in paths
                       if t.strip("./") and self._is_tests_only(t)))

    def _is_tests_only(self, testpath: str) -> bool:
        """testpaths may name the source package itself (doctests, tests next to code): then it isn't test side."""
        here = [p for p in self.paths if _under(p, testpath) and p.endswith(".py")]
        tests = sum(1 for p in here if self.is_test_module(p))
        return bool(tests) and tests * 2 >= len(here)

    def tests(self, among: Optional[Iterable[str]] = None) -> set[str]:
        return {p for p in (self.paths if among is None else among)
                if (self.is_test_module(p) or p in self.doctests) and self.in_testpaths(p)}

    def in_package(self, path: str) -> list[str]:
        """The modules of the nearest package around a file (its data, templates, ...), if it's in one."""
        folder = PurePosixPath(path).parent
        while str(folder) != ".":
            if f"{folder}/__init__.py" in self.paths:
                return [p for p in self.paths if p.startswith(f"{folder}/") and p.endswith(".py")]
            folder = folder.parent
        return []


def _with_examples(root: str, tree: str) -> set[str]:
    try:
        out = snapshot.git(root, "grep", "-l", "-z", "-F", ">>>", tree, "--", "*.py")
    except snapshot.GitError:       # nothing found
        return set()
    return {p.split(":", 1)[1] for p in out.split("\0") if ":" in p}


def _under(path: str, folder: str) -> bool:
    folder = folder.removeprefix("./").rstrip("/")
    if any(c in folder for c in "*?["):
        return any(fnmatch.fnmatch(str(parent), folder) for parent in [PurePosixPath(path), *PurePosixPath(path).parents])
    return folder in ("", ".") or path == folder or path.startswith(folder + "/")


def config_changed(show_old: Callable[[str], Optional[str]], show_new: Callable[[str], Optional[str]], path: str) -> bool:
    """Did the pytest section of this config file change? (pyproject.toml changes for many other reasons.)"""
    name = PurePosixPath(path).name
    return name in CONFIG_NAMES and pytest_section(name, show_old(path)) != pytest_section(name, show_new(path))


@functools.lru_cache(maxsize=16)
def read(root: str, trees: tuple[str, ...]) -> Layout:
    return Layout.read(root, trees)       # trees are content hashes, so this never goes stale


def is_test_side(path: str) -> bool:
    """By default naming only; Layout.is_test_side also knows the repo's own."""
    return is_test_module(path) or PurePosixPath(path).name == "conftest.py" or in_test_dir(path)
