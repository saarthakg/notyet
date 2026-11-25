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
from pathlib import Path

from notyet.findings import Finding

PLUGIN = "notyet_lines"          # what pytest loads with -p: it saves when the run ends
RECORDER = "notyet_lines_core"    # lineplugin.py, imported at start-up. A module of its own, because pytest
                                  # warns about a plugin that was already imported (an error under -W error)
PLUGIN_SOURCE = f"""\
import {RECORDER}


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


def read(out: str, root: str) -> dict[str, set[int]] | None:
    """What every process recorded, or None if nothing could be (another tracer took over)."""
    hits: dict[str, set[int]] = {}
    files = list(Path(out).parent.glob(Path(out).name + ".*"))
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
    try:
        tree = ast.parse(source)
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
            for s in node.body:  # docstrings aren't code a test should hit
                if isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant) and isinstance(s.value.value, str):
                    out |= set(range(s.lineno, (s.end_lineno or s.lineno) + 1))
    return out


def findings(sources: dict[str, str], added: dict[str, set[int]], hits: dict[str, set[int]]) -> list[Finding]:
    """One finding per file with changed executable lines that never ran."""
    out = []
    for path in sorted(sources):
        lines = sorted((added.get(path, set()) & executable_lines(sources[path], path)) - hits.get(path, set()))
        if not lines:
            continue
        text = sources[path].splitlines()
        evidence = [f"{n}: {text[n - 1].strip()[:120]}" for n in lines[:3] if n - 1 < len(text)]
        content = [text[n - 1].strip() for n in lines if n - 1 < len(text)]
        digest = hashlib.sha1("\n".join(content).encode()).hexdigest()[:10]
        out.append(Finding(
            rule="untested-change", severity="resolve", location=f"{path}:{lines[0]}",
            title=f"{len(lines)} changed line(s) in {path} never ran in the selected tests ({_ranges(lines)})",
            evidence=evidence, key=f"{path}|{digest}", covers=_line_digests(content),
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


def _line_digests(content: list[str]) -> list[str]:
    """One digest per line, by content rather than line number (so edits elsewhere don't move it);
    the k-th copy of a repeated line (`return out`) is its own item."""
    seen: dict[str, int] = {}
    out = []
    for line in content:
        seen[line] = seen.get(line, 0) + 1
        out.append(hashlib.sha1(f"{line}#{seen[line]}".encode()).hexdigest()[:10])
    return out
