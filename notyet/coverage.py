"""Changed lines that no selected test executed.

The execution engine runs the selected tests with notyet's line plugin
(notyet/lineplugin.py) watching the changed source files; this module sets
that up and turns the result into findings. Only executable lines count
(not comments, blank lines or docstrings), and lines excluded the usual ways
aren't expected to run: `# pragma: no cover`, `if TYPE_CHECKING:` blocks,
`if __name__ == "__main__":` blocks, `raise NotImplementedError`, and
`...`/`pass` bodies.
"""
import ast
import hashlib
import json
import os
import shutil
import time
from collections import Counter
from pathlib import Path

from notyet.findings import Finding, digests
from notyet.source import parse

PLUGIN = "notyet_lines"          # what pytest loads with -p: it saves when the run ends
RECORDER = "notyet_lines_core"    # lineplugin.py, imported at start-up. A module of its own, because pytest
                                  # warns about a plugin that was already imported (an error under -W error)
PLUGIN_SOURCE = f"""\
import os
import subprocess
import time
import {RECORDER}

_here = os.path.dirname(os.path.abspath(__file__))
_start = subprocess.Popen.__init__


def _traced(self, *args, **kwargs):
    # a test that gives its child process an environment (often with a PYTHONPATH) means it for the program
    # it starts; the recorder comes along in a copy
    env = kwargs.get("env")
    if env is not None and isinstance(env, dict) and _here not in str(env.get("PYTHONPATH", "")):
        path = os.pathsep.join(p for p in (_here, str(env.get("PYTHONPATH", ""))) if p)
        kwargs["env"] = {{**env, "PYTHONPATH": path, **{{k: v for k, v in os.environ.items() if k.startswith("NOTYET_TRACE_")}}}}
    _start(self, *args, **kwargs)


subprocess.Popen.__init__ = _traced


def pytest_runtest_logstart(nodeid, location):      # which test is running, for when one never finishes
    try:
        with open(os.environ["NOTYET_TRACE_OUT"] + ".now", "w") as f:
            f.write(f"{{time.time()}} {{nodeid}}")
    except (KeyError, OSError):
        pass


def pytest_unconfigure(config):
    {RECORDER}._save()
"""


# Watches the changed files in every Python the tests start (a CLI run as a subprocess, an xdist worker),
# then hands over to the sitecustomize this one stands in front of, if there is one.
SITECUSTOMIZE = f"""\
import importlib.machinery, importlib.util, os, sys
_here = os.path.dirname(os.path.abspath(__file__))
try:
    import {RECORDER}
except Exception:
    pass
_spec = importlib.machinery.PathFinder.find_spec("sitecustomize", [p for p in sys.path if os.path.abspath(p or ".") != _here])
if _spec is not None and _spec.loader is not None:
    _spec.loader.exec_module(importlib.util.module_from_spec(_spec))
"""


def setup(tmp: str, root: str, files: list[str]) -> tuple[dict, str]:
    """(env for the test run, path of the JSON it writes)."""
    shutil.copy(Path(__file__).with_name("lineplugin.py"), os.path.join(tmp, f"{RECORDER}.py"))
    with open(os.path.join(tmp, f"{PLUGIN}.py"), "w") as f:
        f.write(PLUGIN_SOURCE)
    with open(os.path.join(tmp, "sitecustomize.py"), "w") as f:     # child processes the tests start
        f.write(SITECUSTOMIZE)
    out = os.path.join(tmp, "lines.json")
    existing = os.environ.get("PYTHONPATH")
    env = {
        "PYTHONPATH": tmp + (os.pathsep + existing if existing else ""),
        "NOTYET_TRACE_FILES": os.pathsep.join(os.path.join(root, f) for f in files),
        "NOTYET_TRACE_OUT": out,
    }
    return env, out


def running(out: str) -> tuple[str, float]:
    """(the test that was running last, seconds since it started); ("", 0) if none started."""
    try:
        started, nodeid = Path(out + ".now").read_text().split(" ", 1)
        return nodeid, time.time() - float(started)
    except (OSError, ValueError):
        return "", 0.0


def read(out: str, root: str) -> dict[str, set[int]] | None:
    """What every process recorded, or None if nothing could be (another tracer took over)."""
    hits: dict[str, set[int]] = {}
    files = [p for p in Path(out).parent.glob(Path(out).name + ".*") if p.suffix[1:].isdigit()]
    for path in files:
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if data.pop("lost", None):
            return None
        for k, v in data.items():
            hits.setdefault(os.path.relpath(k, root), set()).update(v)
    return hits if files else None


def executable_lines(source: str, filename: str = "<changed>") -> set[int]:
    tree = parse(source)
    if tree is None:
        return set()
    try:
        code = compile(tree, filename, "exec")
    except (SyntaxError, ValueError):
        return set()
    lines: set[int] = set()
    stack = [code]
    while stack:
        co = stack.pop()
        lines |= {line for _, _, line in co.co_lines() if line}
        stack += [c for c in co.co_consts if hasattr(c, "co_lines")]
    return lines - _excluded(tree, source)


def _excluded(tree: ast.AST, source: str) -> set[int]:
    pragma = {i for i, text in enumerate(source.splitlines(), 1) if "pragma: no cover" in text}
    out = set(pragma)
    for node in ast.walk(tree):
        start = getattr(node, "lineno", None)
        if getattr(node, "body", None) and start in pragma:  # the whole block, as coverage.py does
            out |= set(range(start, (getattr(node, "end_lineno", None) or start) + 1))
        if isinstance(node, ast.If):
            test = ast.unparse(node.test)
            if test in ("TYPE_CHECKING", "typing.TYPE_CHECKING") or test.replace("'", '"') == '__name__ == "__main__"':
                out |= set(range(node.lineno, (node.end_lineno or node.lineno) + 1))
        elif isinstance(node, ast.Raise) and node.exc is not None and "NotImplementedError" in ast.unparse(node.exc):
            out.add(node.lineno)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            body = [s for s in node.body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]
            if all(isinstance(s, ast.Pass) for s in body):
                out |= set(range(node.lineno, (node.end_lineno or node.lineno) + 1))
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            for s in node.body:    # docstrings (and bare strings) aren't code a test should hit
                if isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant) and isinstance(s.value.value, str):
                    out |= set(range(s.lineno, (s.end_lineno or s.lineno) + 1))
    return out


def findings(sources: dict[str, str], added: dict[str, set[int]], hits: dict[str, set[int]],
             removed: Counter[str] | None = None) -> list[Finding]:
    """One finding per file with changed executable lines that never ran. `removed`: the lines the session took
    out, anywhere; one that reappears unchanged was moved, and was as untested before."""
    out = []
    moved: Counter[str] = Counter(removed) if removed else Counter()
    for path in sorted(sources):
        lines = sorted((added.get(path, set()) & executable_lines(sources[path], path)) - hits.get(path, set()))
        if not lines:
            continue
        text = sources[path].splitlines()
        fresh = []
        for n in lines:
            line = text[n - 1].strip() if n - 1 < len(text) else ""
            if moved[line] > 0:
                moved[line] -= 1
            else:
                fresh.append(n)
        lines = fresh
        if not lines:
            continue
        evidence = [f"{n}: {text[n - 1].strip()[:120]}" for n in lines[:3] if n - 1 < len(text)]
        content = [text[n - 1].strip() for n in lines if n - 1 < len(text)]
        digest = hashlib.sha1("\n".join(content).encode()).hexdigest()[:10]
        out.append(Finding(
            rule="untested-change", severity="resolve", location=f"{path}:{lines[0]}",
            title=f"{len(lines)} changed line(s) in {path} never ran in the selected tests ({_ranges(lines)})",
            evidence=evidence, key=f"{path}|{digest}", covers=digests(content),
            action="Add or extend a test that exercises them, or acknowledge why they can't be tested."))
    return out


def _ranges(lines: list[int]) -> str:
    parts, start, prev = [], lines[0], lines[0]
    for n in lines[1:] + [None]:
        if n is not None and n == prev + 1:
            prev = n
            continue
        parts.append(f"{start}" if start == prev else f"{start}-{prev}")
        if n is not None:
            start = prev = n
    return ", ".join(parts[:6]) + (" …" if len(parts) > 6 else "")
