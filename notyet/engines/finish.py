"""Unfinished work in what the session added: what a reviewer sends back before reading any further.

Leftovers, in lines the session added (not moved: a line counts only if the file has more copies of it than before):
- debugger calls: `breakpoint()`, `pdb.set_trace()` and the like, anywhere;
- debug prints in source: a print in a module that logs, or one that prints a variable dump (`f"{x=}"`, `repr(...)`,
  `vars(...)`, "DEBUG ...");
- new TODO / FIXME / XXX / HACK comments: the agent saying, in the code, that it isn't done;
- placeholders: a new function whose body is only `pass`, `...` or `raise NotImplementedError` (abstract methods,
  protocols and overloads aside), and the comments models put where they left code out ("# ... rest of the code",
  "# implement this");
- commented-out code: two or more added comment lines in a row that parse as Python.

Code nothing calls: a new function, class or private method in source (not tests) whose name appears nowhere else in
the repo, or, for a private name, only in tests. A public method isn't reported for having no caller here: code
outside the repo may call it by protocol (http.client calls a socket's `makefile`). A new public name only tests use is shown to the user: in a library
that's the API, in an application it's a feature that was built and never connected (claude-code #60451).
Names are matched as words in every tracked text file, so any mention (a string, a config file, an entry point)
counts as a use: this errs towards silence.
"""
import ast
import io
import re
import textwrap
import tokenize
from collections import Counter
from typing import Optional

from notyet import snapshot
from notyet.findings import Context, EngineResult, Finding, digests
from notyet.source import parse

DEBUGGER = re.compile(r"(?<![\w.])(breakpoint\(\)|(i?pdb|pudb|ipdb|pdbpp|pytest|rpdb|web_pdb|debugpy)\.set_trace\(|"
                      r"import (i?pdb|pudb)\b|from (i?pdb|pudb) import|debugpy\.wait_for_client\()")
DUMP = re.compile(r"""print\(\s*(f?["'](debug|DEBUG|>>>|---|\*\*\*|XXX|here|HERE)|f["'][^"']*=\}|(repr|vars|dir|type|locals)\()""")
MARKER = re.compile(r"#.*?\b(TODO|FIXME|XXX|HACK)\b")
# the comments models put where they left code out; the whole comment, so prose that mentions "existing code" isn't one
ELISION = re.compile(r"#\s*(\.\.\.|…)?\s*\(?\s*("
                     r"(the )?rest of (the )?(code|file|implementation|method|function|class|logic|tests?)( (goes )?here| unchanged| remains( the same)?)?|"
                     r"(existing|remaining|unchanged|original|other) (code|logic|implementation|methods|functions|tests|fields|cases)"
                     r"( (goes )?here| unchanged| remains( the same)?| as before)?|"
                     r"(implement|add|insert|write|put) (this|me|it|your (code|logic|implementation)|(the )?(actual|real) "
                     r"(code|logic|implementation))( here| later)?|"
                     r"(your|actual|real) (code|logic|implementation) (goes )?here|"
                     r"(placeholder|stub)( (code|implementation|logic|for now|value))?"
                     r")\s*\)?\s*(\.\.\.|…)?\s*[.:]?\s*$", re.IGNORECASE)
REGISTERING_OK = {"staticmethod", "classmethod", "property", "cached_property", "cache", "lru_cache", "wraps",
                  "functools.cache", "functools.lru_cache", "functools.cached_property", "functools.wraps",
                  "override", "typing.override", "typing_extensions.override"}
DISPATCHED = re.compile(r"^(visit_|do_|handle_|on_|pytest_|test|setUp|tearDown)")
MAX_TEXT_BYTES = 2 * 1024 * 1024
MAX_EACH = 6


def run(ctx: Context) -> EngineResult:
    result = EngineResult()
    py = [d for d in ctx.deltas if d.status != "D" and d.path.endswith(".py")]
    if not py:
        return result
    added = snapshot.added_lines(ctx.root, ctx.baseline_tree, ctx.current_tree, py)
    sources: dict[str, str] = {}
    for d in py:
        text = snapshot.show(ctx.root, ctx.current_tree, d.path)
        if text is not None and d.path in added:
            sources[d.path] = text
    before = {d.path: snapshot.show(ctx.root, ctx.baseline_tree, d.old_path or d.path) or "" for d in py}

    leftovers: dict[str, list[tuple[str, int, str]]] = {}      # kind -> (path, line, what)
    for path, text in sources.items():
        test_side = ctx.layout.is_test_side(path)
        lines = text.splitlines()
        gained = Counter(x.strip() for x in lines) - Counter(x.strip() for x in before[path].splitlines())
        comments = _comments(text)
        logs = bool(re.search(r"\blogging\.getLogger\(|\blogger\s*=|\blog\s*=\s*logging", text))
        for n in sorted(added[path]):
            if n > len(lines) or gained[lines[n - 1].strip()] <= 0:
                continue
            col, comment = comments.get(n, (len(lines[n - 1]), None))
            kind = _leftover(lines[n - 1][:col], comment, test_side, logs)
            if kind:
                gained[lines[n - 1].strip()] -= 1
                leftovers.setdefault(kind, []).append((path, n, lines[n - 1].strip()[:140]))
        if not test_side:
            leftovers.setdefault("placeholder", []).extend((path, n, what) for n, what in _placeholders(text, added[path], before[path]))
        leftovers.setdefault("commented-out-code", []).extend(
            (path, n, what) for n, what in _commented_code(lines, comments, added[path]))

    for kind, items in leftovers.items():
        result.findings += _leftover_findings(kind, items)

    result.findings += _unwired(ctx, sources, before, added)
    result.checks.append("leftovers and code nothing calls, in the lines this session added")
    return result


# ── Leftovers ────────────────────────────────────────────────────────────────

def _leftover(code: str, comment: Optional[str], test_side: bool, logs: bool) -> Optional[str]:
    if DEBUGGER.search(code):
        return "debugger-left"
    if comment and MARKER.search(comment):
        return "todo-added"
    if comment and ELISION.match(comment.strip()):
        return "placeholder"
    if not test_side and code.lstrip().startswith("print(") and (DUMP.search(code) or logs):
        return "debug-print"
    return None


def _comments(text: str) -> dict[int, tuple[int, str]]:
    """Line number -> (column, the comment on it): from the tokenizer, so a `#` in a string isn't one."""
    out: dict[int, tuple[int, str]] = {}
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT:
                out[tok.start[0]] = (tok.start[1], tok.string)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    return out


def _placeholders(text: str, added: set[int], old_text: str) -> list[tuple[int, str]]:
    """New functions with nothing in them."""
    tree = parse(text)
    if tree is None:
        return []
    lines = text.splitlines()
    old = parse(old_text)
    had = {_qual(n, p) for p, n in _defs(old)} if old is not None else set()
    out = []
    for parent, node in _defs(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.lineno not in added:
            continue
        if _qual(node, parent) in had or _decorated(node, {"abstractmethod", "abc.abstractmethod", "overload",
                                                           "typing.overload"}):
            continue
        if isinstance(parent, ast.ClassDef) and any(_name(b.value if isinstance(b, ast.Subscript) else b).rsplit(".", 1)[-1]
                                                    in ("Protocol", "ABC") for b in parent.bases):
            continue
        body = node.body[1:] if node.body and _is_docstring(node.body[0]) else node.body
        if not body or not all(_empty(s) for s in body):
            continue
        if len(body) == 1 and isinstance(body[0], ast.Expr) and lines[body[0].lineno - 1][:body[0].col_offset].strip():
            continue        # `def f(...) -> T: ...` on one line declares a signature (for type checkers), it isn't a stub
        # a method that raises NotImplementedError or does nothing is often a base class's hook to override;
        # one that promises a value and returns none isn't
        promises = node.returns is not None and not (isinstance(node.returns, ast.Constant) and node.returns.value is None)
        if parent is None or (promises and not isinstance(body[0], ast.Raise)):
            out.append((node.lineno, f"def {node.name}(...) has no body: {ast.unparse(body[0])[:60]}"))
    return out


def _empty(stmt: ast.stmt) -> bool:
    if isinstance(stmt, ast.Pass) or (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant)
                                      and stmt.value.value is Ellipsis):
        return True
    if isinstance(stmt, ast.Raise) and stmt.exc is not None:
        exc = stmt.exc.func if isinstance(stmt.exc, ast.Call) else stmt.exc
        return _name(exc) == "NotImplementedError"
    return False


def _is_docstring(stmt: ast.stmt) -> bool:
    return isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str)


def _commented_code(lines: list[str], comments: dict[int, tuple[int, str]], added: set[int]) -> list[tuple[int, str]]:
    """Runs of two or more added whole-line comments that parse as Python statements with code in them."""
    runs: list[list[int]] = []
    for n in sorted(added):
        whole = n <= len(lines) and n in comments and not lines[n - 1][:comments[n][0]].strip() \
            and not MARKER.search(comments[n][1])      # a TODO above the code is reported as a TODO
        if not whole:
            continue
        if runs and runs[-1][-1] == n - 1:
            runs[-1].append(n)
        else:
            runs.append([n])
    out = []
    for run in runs:
        body = [re.sub(r"^\s*#\s?", "", lines[i - 1]) for i in run]
        if len(run) >= 2 and _is_code(body):
            out.append((run[0], " / ".join(b.strip() for b in body[:3])))
    return out


def _is_code(body: list[str]) -> bool:
    text = textwrap.dedent("\n".join(body))
    if not re.search(r"[=(\[:]", text):
        return False
    tree = parse(text)
    if tree is None or not tree.body:
        return False
    # prose that happens to parse ("Note: x") is a bare name or an annotation; code calls, assigns or defines
    return any(isinstance(s, (ast.Assign, ast.AugAssign, ast.Return, ast.If, ast.For, ast.While, ast.With, ast.FunctionDef,
                              ast.ClassDef, ast.Import, ast.ImportFrom, ast.Raise))
               or (isinstance(s, ast.Expr) and isinstance(s.value, ast.Call)) for s in tree.body)


LEFTOVER_TEXT = {
    "debugger-left": ("debugger call(s) left in", "Remove them."),
    "debug-print": ("print(s) that look like debugging, in source", "Remove them, or use the module's logger."),
    "todo-added": ("new TODO/FIXME comment(s): work the code says isn't done",
                   "Do the work, or say in your closing message what's left and why: the user will see it."),
    "placeholder": ("placeholder(s): empty functions, or comments where code was left out",
                    "Write the code that belongs there."),
    "commented-out-code": ("block(s) of commented-out code", "Delete them, or restore them as code."),
}


def _leftover_findings(kind: str, items: list[tuple[str, int, str]]) -> list[Finding]:
    """One finding per file, so fixing one file leaves the others' ids alone."""
    by_file: dict[str, list[tuple[int, str]]] = {}
    for path, n, what in items:
        by_file.setdefault(path, []).append((n, what))
    what, action = LEFTOVER_TEXT[kind]
    out = []
    for path, these in by_file.items():
        these.sort()
        out.append(Finding(
            rule=kind, severity="resolve", location=f"{path}:{these[0][0]}",
            title=f"{len(these)} {what}: {path}", evidence=[f"line {n}: {text}" for n, text in these[:MAX_EACH]],
            action=action, key=f"{path}|{kind}", covers=digests(text for _, text in these)))
    return out


# ── Code nothing calls ───────────────────────────────────────────────────────

def _unwired(ctx: Context, sources: dict[str, str], before: dict[str, str], added: dict[str, set[int]]) -> list[Finding]:
    old_names = set()
    for d in ctx.deltas:
        if d.status in ("M", "R", "D") and d.path.endswith(".py"):
            tree = parse(snapshot.show(ctx.root, ctx.baseline_tree, d.old_path or d.path) or "")
            old_names |= {n.name for _, n in _defs(tree)} if tree else set()
    new: list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef, Optional[ast.ClassDef]]] = []
    for path, text in sources.items():
        if ctx.layout.is_test_side(path) or path.endswith(("conftest.py", "__main__.py", "setup.py")):
            continue
        tree = parse(text)
        if tree is None:
            continue
        exported = _exported(tree)
        for parent, node in _defs(tree):
            name = node.name
            if node.lineno not in added.get(path, ()) or name in old_names or name in exported \
                    or (name.startswith("__") and name.endswith("__")) or DISPATCHED.match(name) \
                    or _decorated(node, None) or (parent is not None and parent.bases):
                continue        # a method of a subclass may be an override that the base class calls
            new.append((path, node, parent))
    if not new:
        return []
    uses = _uses(ctx, {node.name for _, node, _ in new})
    out = []
    for path, node, parent in new:
        name = node.name
        span = (node.lineno - len(node.decorator_list), node.end_lineno or node.lineno)
        elsewhere = [(p, n) for p, n in uses.get(name, []) if not (p == path and span[0] <= n <= span[1])]
        product = [(p, n) for p, n in elsewhere if not ctx.layout.is_test_side(p)]
        what = f"{'class' if isinstance(node, ast.ClassDef) else 'method' if parent else 'function'} " \
               f"{(parent.name + '.') if parent else ''}{name}"
        if not elsewhere and parent is not None and not name.startswith("_"):
            continue        # a public method can be called by code that isn't here: duck typing (a file-like `read`)
        if not elsewhere:
            out.append(Finding(
                rule="unwired-code", severity="resolve", location=f"{path}:{node.lineno}",
                title=f"new {what} ({path}:{node.lineno}) isn't used anywhere",
                action="Call it where it's needed, or remove it if it isn't.", key=f"{path}|{name}"))
        elif not product:
            tests = sorted({p for p, _ in elsewhere})
            private = name.startswith("_")
            out.append(Finding(
                rule="unwired-code" if private else "only-tests-use", severity="resolve",
                location=f"{path}:{node.lineno}",
                title=f"new {what} ({path}:{node.lineno}) is used only by tests",
                evidence=[f"used in {', '.join(tests[:3])}" + (f" and {len(tests) - 3} more" if len(tests) > 3 else "")],
                action="Connect it to the code that should use it, or say why tests are its only callers."
                if private else "If something in the product should call it, connect it; if it's API for others, say so.",
                key=f"{path}|{name}"))
    return out


def _uses(ctx: Context, names: set[str]) -> dict[str, list[tuple[str, int]]]:
    """Where each name appears as a word in the current tree's tracked text files."""
    out: dict[str, list[tuple[str, int]]] = {}
    if not names:
        return out
    pattern = []
    for name in sorted(names):
        pattern += ["-e", name]
    try:
        text = snapshot.git(ctx.root, "-c", "grep.column=false", "-c", "grep.fullName=false", "grep", "-I", "-n", "-o", "-w", "-F", "--no-color", *pattern, ctx.current_tree)
    except snapshot.GitError:       # no match at all exits 1
        return out
    prefix = ctx.current_tree + ":"
    for line in text.splitlines():
        if not line.startswith(prefix):
            continue
        path, n, word = line[len(prefix):].rsplit(":", 2)
        if word in names:
            out.setdefault(word, []).append((path, int(n)))
    return out


def _defs(tree: Optional[ast.AST]):
    """(class or None, def) for module-level functions and classes and the methods of module-level classes."""
    if tree is None:
        return
    for node in getattr(tree, "body", []):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield None, node
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    yield node, item


def _qual(node, parent) -> str:
    return f"{parent.name}.{node.name}" if parent is not None else node.name


def _exported(tree: ast.Module) -> set[str]:
    out = set()
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets):
            if isinstance(node.value, (ast.List, ast.Tuple)):
                out |= {e.value for e in node.value.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)}
    return out


def _decorated(node, only: Optional[set[str]]) -> bool:
    """Has a decorator in `only`; with `only` None, one that may register the function (a route, a command, a
    fixture), so not being called by name says nothing."""
    names = [_name(d.func if isinstance(d, ast.Call) else d) for d in getattr(node, "decorator_list", [])]
    if only is not None:
        return any(n in only or n.rsplit(".", 1)[-1] in only for n in names)
    return any(n not in REGISTERING_OK and n.rsplit(".", 1)[-1] not in {"setter", "getter", "deleter"} for n in names)


def _name(node) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""
