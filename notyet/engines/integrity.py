"""Test integrity, read from the source: assertions removed from a test, and (when the session-start
tests couldn't be run on the new code, see crosscheck.py) tests switched off.

Also the reading of test modules the other engines share. A test function counts as edited when its
body, its decorators (a `parametrize` expectation is part of the test) or its class's setup changed,
or a module-level value, helper or fixture it uses did.
"""
import ast
from dataclasses import dataclass

from notyet import snapshot
from notyet.findings import Context, EngineResult, Finding

DISABLING_DECORATORS = ("mark.skip", "mark.xfail", "unittest.skip", "skipIf", "skipUnless", "expectedFailure")
AUTOUSE = {"autouse fixture"}
DISABLING_CALLS = ("pytest.skip", "pytest.xfail", "self.skipTest", "pytest.importorskip")


@dataclass
class TestFunc:
    name: str                 # "test_x" or "TestC::test_x"
    node: ast.AST
    disablers: frozenset[str]
    assertions: int
    body: str                 # normalized source of the body, to tell edits apart
    setup: str = ""           # normalized source of its class, without the test methods
    collected: bool = True    # by pytest's default naming


def run(ctx: Context) -> EngineResult:
    result = EngineResult()
    for d in ctx.deltas:
        if d.status not in ("M", "R") or not ctx.layout.is_test_module(d.path):
            continue
        before = _functions(snapshot.show(ctx.root, ctx.baseline_tree, d.old_path or d.path) or "")
        after = _functions(snapshot.show(ctx.root, ctx.current_tree, d.path) or "")
        for name in sorted(before.keys() & after.keys()):
            b, a = before[name], after[name]
            nid = f"{d.path}::{name}"
            added = sorted(a.disablers - b.disablers)
            if added and not ctx.shared.get("crosscheck"):
                result.findings.append(Finding(
                    rule="test-disabled", severity="block", location=nid,
                    title=f"{nid} was switched off this session ({', '.join(added)})",
                    evidence=added[:3],
                    action="Remove the skip/xfail and fix the code. If the test should really be off, "
                           "hand it to the user: `ack <id> --needs-human \"why\"`."))
            elif not added and a.assertions < b.assertions:
                result.findings.append(Finding(
                    rule="assertions-removed", severity="resolve", location=nid,
                    title=f"{nid} checks less than it did: {b.assertions} assertion(s) at session start, "
                          f"{a.assertions} now",
                    action="Restore the assertions, or acknowledge why they no longer apply."))
    return result


# ── reading test functions ─────────────────────────────────────────────────────

def changed_functions(before_source: str, after_source: str) -> dict[str, str]:
    """{test name: "new" | "edited"} between two versions of a test module."""
    before, after = _functions(before_source), _functions(after_source)
    changed_names = _changed_module_names(before_source, after_source)
    out = {}
    for name, func in after.items():
        old = before.get(name)
        if old is None:
            out[name] = "new"
        elif _signature(old) != _signature(func) or changed_names & (_names_used(func.node) | AUTOUSE):
            out[name] = "edited"
    return out


def test_bodies(source: str) -> dict[str, str]:
    """{test name: its body and decorators, normalized}, to recognize a test that moved."""
    return {name: "\n".join(_signature(f)[:2]) for name, f in _functions(source).items()}


def test_names(source: str) -> set[str]:
    """Test functions pytest's default naming collects from a module ("test_x", "TestC::test_x")."""
    return {name for name, f in _functions(source).items() if f.collected}


def _signature(func: TestFunc) -> tuple[str, str, str]:
    return func.body, "\n".join(ast.unparse(d) for d in getattr(func.node, "decorator_list", [])), func.setup


def _names_used(node: ast.AST) -> set[str]:
    """Names a test refers to, including the fixtures it asks for."""
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)} | {a.arg for a in ast.walk(node) if isinstance(a, ast.arg)}


def _changed_module_names(before_source: str, after_source: str) -> set[str]:
    """Module-level values and helpers (not tests, not imports) whose
    definition changed, appeared or disappeared."""
    def definitions(source: str) -> dict[str, str]:
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return {}
        defs: dict[str, str] = {}
        for node in tree.body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    for n in ast.walk(t):
                        if isinstance(n, ast.Name):
                            defs[n.id] = ast.unparse(node)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                    and not node.name.startswith(("test", "Test")):
                text = ast.unparse(node)
                defs[node.name] = text
                if "autouse=True" in text:      # applies to every test without being named
                    defs["autouse fixture"] = defs.get("autouse fixture", "") + text
        return defs
    before, after = definitions(before_source), definitions(after_source)
    return {n for n in before.keys() | after.keys() if before.get(n) != after.get(n)}


def _functions(source: str) -> dict[str, TestFunc]:
    """Every function that looks like a test, at any class depth, whatever the class is called."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    out: dict[str, TestFunc] = {}

    def visit(body: list[ast.stmt], prefix: str, marks: frozenset[str], setup: str, collected: bool) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
                out[prefix + node.name] = _read(prefix + node.name, node, marks, setup, collected)
            elif isinstance(node, ast.ClassDef):
                rest = [n for n in node.body if not isinstance(n, ast.ClassDef)
                        and not (isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test"))]
                bases = [ast.unparse(b) for b in node.bases]
                visit(node.body, f"{prefix}{node.name}::", marks | _decorator_disablers(node) | _mark_disablers(node.body),
                      "\n".join([setup, *bases, *(ast.unparse(n) for n in rest)]),
                      collected and (node.name.startswith("Test") or any("TestCase" in b for b in bases)))

    visit(tree.body, "", _mark_disablers(tree.body), "", True)
    return out


def _read(name: str, node, inherited: frozenset[str], setup: str, collected: bool) -> TestFunc:
    disablers = set(inherited) | _decorator_disablers(node)
    assertions = 0
    for sub in ast.walk(node):
        if isinstance(sub, ast.Assert):
            assertions += 1
        elif isinstance(sub, ast.Call):
            func = ast.unparse(sub.func)
            if func.split(".")[-1].lower().startswith("assert") or func in ("pytest.raises", "pytest.warns") \
                    or func.startswith("_assert") or func.split(".")[-1].startswith("_assert"):
                assertions += 1
            if func in DISABLING_CALLS:
                disablers.add(f"{func}()")
    body = "\n".join(ast.unparse(stmt) for stmt in node.body)
    return TestFunc(name, node, frozenset(disablers), assertions, body, setup, collected)


def _decorator_disablers(node) -> frozenset[str]:
    found = set()
    for dec in getattr(node, "decorator_list", []):
        text = ast.unparse(dec)
        if any(k in text for k in DISABLING_DECORATORS):
            found.add(f"@{text}")
    return frozenset(found)


def _mark_disablers(body: list[ast.stmt]) -> frozenset[str]:
    """`pytestmark = pytest.mark.skip(...)` in a module or class body."""
    found = set()
    for node in body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets):
            text = ast.unparse(node.value)
            if any(k in text for k in DISABLING_DECORATORS):
                found.add(f"pytestmark = {text}")
    return frozenset(found)
