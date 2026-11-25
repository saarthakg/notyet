"""Running pytest and reading what actually happened.

Results come from pytest's junit XML, never from the exit code alone: an
agent (or a background shell) reporting "exit 0" is exactly the signal that
can't be trusted. Every run gets a hard timeout.
"""
import json
import os
import re
import shlex
import signal
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TestResult:
    nodeid: str
    outcome: str                  # passed | failed | error | skipped
    message: str = ""
    seconds: float = 0.0

    @property
    def bad(self) -> bool:
        return self.outcome in ("failed", "error")


@dataclass
class Run:
    results: dict[str, TestResult] = field(default_factory=dict)   # nodeid → result
    seconds: float = 0.0
    timed_out: bool = False
    crashed: Optional[str] = None     # pytest itself failed to run (usage error, internal error)
    aborted: bool = False             # it stopped before the end (pytest.exit(), an internal error): what's missing is unknown

    @property
    def failures(self) -> list[TestResult]:
        return [r for r in self.results.values() if r.bad]


def _nodeid(case: ET.Element, cwd: Optional[str] = None) -> str:
    """Rebuild pytest's node id from a junit (xunit1) testcase: file
    "tests/test_x.py", classname "tests.test_x.TestC", name "test_y[p]" ->
    "tests/test_x.py::TestC::test_y[p]"."""
    classname, name, path = case.get("classname", ""), case.get("name", ""), case.get("file")
    if not path:
        return f"{classname}::{name}"
    module = path[:-3].replace("/", ".") if path.endswith(".py") else path
    if not classname and name == module:
        return path       # the file itself failed to import or collect
    if classname and path.endswith(".py") and classname != module and not classname.startswith(module + "."):
        path = _path_from_classname(path, classname, cwd) or path
        module = path[:-3].replace("/", ".")
    inner = classname[len(module) + 1:] if classname.startswith(module + ".") else ""
    return "::".join([path, *(inner.split(".") if inner else []), name])


def _path_from_classname(path: str, classname: str, cwd: Optional[str] = None) -> Optional[str]:
    """The test file's path when junit's `file` names another one. pytest takes `file` from the code object, so
      - a test inherited from a base class or mixin names the file the base class is in;
      - a __pycache__ that moved with the repo still passes Python's freshness check, and names the
        old location ("../../old/tests/test_x.py").
    The classname comes from the node id, so it's right: take the longest start of it that is a file here,
    or else the longest tail of the path it starts with."""
    parts, cparts = path[:-3].split("/"), classname.split(".")
    if cwd is not None:
        for k in range(len(cparts), 0, -1):
            if os.path.isfile(os.path.join(cwd, *cparts[:k]) + ".py"):
                return "/".join(cparts[:k]) + ".py"
    for k in range(min(len(parts), len(cparts)), 0, -1):
        if parts[-k:] == cparts[:k]:
            return "/".join(cparts[:k]) + ".py"
    return None


def parse_junit(path: str, cwd: Optional[str] = None) -> dict[str, TestResult]:
    results: dict[str, TestResult] = {}
    try:
        tree = ET.parse(path)
    except (ET.ParseError, OSError):
        return results
    for case in tree.iter("testcase"):
        outcome, message = "passed", ""
        for tag in ("failure", "error", "skipped"):
            el = case.find(tag)
            if el is not None:
                outcome = {"failure": "failed", "error": "error", "skipped": "skipped"}[tag]
                message = (el.get("message") or el.text or "").strip()
                break
        nid = _nodeid(case, cwd)
        if nid in results and results[nid].bad:
            continue   # a teardown error recorded after a failure: keep the first
        try:
            seconds = float(case.get("time") or 0)
        except ValueError:
            seconds = 0.0
        results[nid] = TestResult(nid, outcome, message[:2000], seconds)
    return results


def anchored(command: str, root: str) -> str:
    """`command` with a repo-relative interpreter or tool (".venv/bin/python -m
    pytest") made absolute, so it also runs from a checkout elsewhere."""
    try:
        argv = shlex.split(command)
    except ValueError:
        return command
    if argv and "/" in argv[0] and not os.path.isabs(argv[0]) and os.path.exists(os.path.join(root, argv[0])):
        argv[0] = os.path.abspath(os.path.join(root, argv[0]))
        return shlex.join(argv)
    return command


def run_pytest(command: str, cwd: str, targets: list[str], timeout: float,
               env: Optional[dict] = None, extra: Optional[list[str]] = None) -> Run:
    """Run `command` (a pytest invocation) on `targets` (files or node ids)."""
    with tempfile.TemporaryDirectory(prefix="notyet-junit-") as tmp:
        junit = os.path.join(tmp, "junit.xml")
        argv = shlex.split(command) + [
            *targets, "-q", "-o", f"cache_dir={os.path.join(tmp, 'cache')}",    # not the repo's; --lf & co. stay valid
            "--maxfail=0",            # over an -x in addopts: every test has to have its result
            "--continue-on-collection-errors", f"--junitxml={junit}",
            f"--rootdir={cwd}",   # node ids relative to the repo, even with a nested tests/pytest.ini
            "-o", "junit_family=xunit1", *(extra or []),
        ]
        start = time.monotonic()
        try:
            proc = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                    env={**clean_environ(), **(env or {}), "PYTHONDONTWRITEBYTECODE": "1"},
                                    start_new_session=True)
        except OSError as e:
            return Run(crashed=f"couldn't start the test command: {e}")
        _running.add(proc)
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _interrupt(proc)
            return Run(results=_complete_files(parse_junit(junit, cwd)), seconds=time.monotonic() - start, timed_out=True)
        finally:
            _running.discard(proc)
        run = Run(results=parse_junit(junit, cwd), seconds=time.monotonic() - start, aborted=proc.returncode in (2, 3))
        # pytest exit codes: 0 ok, 1 test failures, 5 no tests; 2-4 are usage/internal errors.
        # No results with any other code means pytest never ran (e.g. "No module named pytest").
        # A clean exit that left no report at all isn't "no tests" either: something ended the process
        # (os._exit in a conftest), or the command isn't pytest.
        if not run.results and (proc.returncode not in (0, 5) or not os.path.exists(junit)):
            tail = (stdout + stderr).strip().splitlines()[-6:]
            run.crashed = f"the test command exited {proc.returncode} without results: " + " | ".join(tail)
        return run


# What the host sets for its hooks. A test that sees them can behave differently here than in anyone's shell.
_HOST = re.compile(r"CLAUDECODE|CLAUDE_(PROJECT_DIR|ENV_FILE|PLUGIN_\w+|CODE_\w+)|CODEX_(THREAD_ID|SANDBOX\w*)|AI_AGENT|NOTYET_ENTRY")


def clean_environ() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not _HOST.fullmatch(k)}


INTERRUPT_GRACE = 10   # seconds pytest gets, after Ctrl-C, to write the junit file


_running: set[subprocess.Popen] = set()


def kill_all() -> None:
    """End every test run in progress: they have process groups of their own, so whatever ends notyet
    (the host's hook timeout) wouldn't end them."""
    for proc in list(_running):
        _signal(proc, signal.SIGKILL)


def _signal(proc: subprocess.Popen, sig: int) -> None:
    try:
        os.killpg(proc.pid, sig)
    except OSError:          # gone already, or (macOS) only a process we may not signal is left in the group
        pass


def _interrupt(proc: subprocess.Popen) -> None:
    """Ctrl-C the run's process group, as a person would: pytest stops after the current test and
    still writes its junit file. Killed outright if it doesn't exit within the grace period."""
    _signal(proc, signal.SIGINT)
    for last in (False, True):
        try:
            proc.communicate(timeout=3 if last else INTERRUPT_GRACE)
            return
        except subprocess.TimeoutExpired:
            _signal(proc, signal.SIGKILL)
        except (OSError, ValueError):
            return
    for pipe in (proc.stdout, proc.stderr):       # a detached child still holds them open
        if pipe is not None:
            pipe.close()


def _complete_files(results: dict[str, "TestResult"]) -> dict[str, "TestResult"]:
    """An interrupted run's results without the file it was in the middle of (the last one
    reported): pytest runs files in order, so every earlier file finished. The interrupted test
    itself is an empty <testcase/> (node id "::"), which is dropped too."""
    results = {n: r for n, r in results.items() if n.split("::")[0]}
    if not results:
        return results
    last = next(reversed(results)).split("::")[0]
    return {n: r for n, r in results.items() if n.split("::")[0] != last}


def run_tests(command: str, cwd: str, node_ids: list[str], timeout: float, env: dict | None = None) -> Run:
    """`run_pytest` on node ids. When one targeted file can't be imported,
    pytest drops the results of every other targeted file too; then the files
    are run whole, where it carries on past the error."""
    run = run_pytest(command, cwd, node_ids, timeout=timeout, env=env)
    collection_error = any("::" not in n.strip(":") and r.bad for n, r in run.results.items())
    if collection_error and any(outcome_of(n, run.results) == "missing" for n in node_ids):
        files = list(dict.fromkeys(n.split("::")[0] for n in node_ids))
        run = run_pytest(command, cwd, files, timeout=timeout, env=env)
    return run


def function_of(nodeid: str) -> str:
    """The test function a node id belongs to: its parameter set left off."""
    return nodeid.split("[", 1)[0]


def outcome_of(nodeid: str, results: dict[str, TestResult]) -> str:
    """passed | failed | skipped | missing for a test function, across its
    parameter sets. A file that fails to import fails every test in it."""
    file = nodeid.split("::")[0]
    if file in results and results[file].bad:
        return "failed"
    mine = [r for n, r in results.items() if n == nodeid or n.startswith(nodeid + "[")]
    if not mine:
        # a module-level skip is reported under the file alone
        return "skipped" if file in results and results[file].outcome == "skipped" else "missing"
    if any(r.bad for r in mine):
        return "failed"
    if all(r.outcome == "passed" for r in mine):
        return "passed"
    return "skipped"


def failure_of(nodeid: str, results: dict[str, TestResult]) -> str:
    """The message of the first failure behind outcome_of(...) == "failed"."""
    file = nodeid.split("::")[0]
    if file in results and results[file].bad:
        return results[file].message
    return next((r.message for n, r in results.items() if (n == nodeid or n.startswith(nodeid + "[")) and r.bad), "")


class Collected(set):
    """Collected node ids, plus the files that failed to import or collect."""
    def __init__(self, ids=(), errors=()):
        super().__init__(ids)
        self.errors: set[str] = set(errors)


# Node ids come from a plugin, not from what --collect-only prints: that depends on -q/-v in addopts.
_COLLECTOR = """\
import json
import os

errors = []


def pytest_collectreport(report):
    if report.failed and report.nodeid:
        errors.append(report.nodeid.split("::")[0])


def pytest_collection_finish(session):
    with open(os.environ["NOTYET_COLLECT_OUT"], "w") as f:
        json.dump({"ids": [i.nodeid for i in session.items], "errors": errors}, f)
"""


def collect(command: str, cwd: str, targets: list[str], timeout: float,
            env: Optional[dict] = None) -> Optional[Collected]:
    """Node ids pytest collects from `targets` (and files it couldn't), or
    None if pytest itself failed."""
    with tempfile.TemporaryDirectory(prefix="notyet-collect-") as tmp:
        out = os.path.join(tmp, "collected.json")
        with open(os.path.join(tmp, "notyet_collect.py"), "w") as f:
            f.write(_COLLECTOR)
        env = dict(env or {})
        env["PYTHONPATH"] = os.pathsep.join([p for p in (env.get("PYTHONPATH", os.environ.get("PYTHONPATH")), tmp) if p])
        argv = shlex.split(command) + [
            *targets, "--collect-only", "-q", "-o", f"cache_dir={os.path.join(tmp, 'cache')}", "-p", "notyet_collect",
            "--continue-on-collection-errors", f"--rootdir={cwd}"]
        try:
            subprocess.run(argv, cwd=cwd, capture_output=True, timeout=timeout, check=False, stdin=subprocess.DEVNULL,
                           env={**clean_environ(), **env, "NOTYET_COLLECT_OUT": out, "PYTHONDONTWRITEBYTECODE": "1"})
            with open(out) as f:
                data = json.load(f)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            return None
        return Collected(data["ids"], data["errors"])
