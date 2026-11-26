"""Leftovers and code nothing calls, in what the session added."""
from notyet import config, snapshot, store
from notyet.engines import finish
from notyet.findings import Context
from tests.conftest import _write


def start(repo):
    root = str(repo)
    return store.Session(session_id="f", started=0, baseline_tree=snapshot.snapshot(root),
                         baseline_head=snapshot.head_tree(root), baseline_source="session-start")


def findings(repo, session):
    root = str(repo)
    current = snapshot.snapshot(root)
    ctx = Context(root=root, session=session, config=config.load(root, session.baseline_tree),
                  baseline_tree=session.baseline_tree, current_tree=current,
                  deltas=snapshot.diff_trees(root, session.baseline_tree, current))
    return finish.run(ctx).findings


def check(repo, session):
    return {f.rule: f for f in findings(repo, session)}


CALC = "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n"


def test_leftovers_in_added_lines(repo):
    session = start(repo)
    _write(repo, "pkg/calc.py", CALC + (
        "\n\nimport logging\nlog = logging.getLogger(__name__)\n\n\n"
        "def mul(a, b):\n"
        "    breakpoint()\n"
        "    print(f'{a=} {b=}')\n"
        "    # TODO: handle overflow\n"
        "    # result = a * b\n"
        "    # return round(result)\n"
        "    return a * b or len('# TODO: in a string, not a comment')\n"
        "\n\ndef div(a, b) -> float:\n    \"\"\"Divide.\"\"\"\n    pass\n"
        "\n\ndef mod(a, b):\n    # ... rest of the implementation\n    return 0\n"))
    found = check(repo, session)
    assert found["debugger-left"].evidence == ["line 14: breakpoint()"]
    assert found["debug-print"].evidence == ["line 15: print(f'{a=} {b=}')"]
    assert found["todo-added"].evidence == ["line 16: # TODO: handle overflow"]
    assert found["commented-out-code"].evidence == ["line 17: result = a * b / return round(result)"]
    assert [e.split(":")[0] for e in found["placeholder"].evidence] == ["line 22", "line 28"]
    assert "def div(...) has no body" in found["placeholder"].evidence[0]


MENTIONS = '''
import logging
log = logging.getLogger(__name__)
DOC = """Leftovers: `breakpoint()`, `pdb.set_trace()`."""
SCRIPT = """
print(sku, level(sku))
breakpoint()
"""
# a comment that mentions TODO markers isn't one
X = 1  # see the FIXME convention in CONTRIBUTING
'''


def test_words_in_strings_and_comments_that_only_mention_a_marker_are_not_leftovers(repo):
    # found on notyet's own session: its docstrings, fixtures and comments talk about these things
    session = start(repo)
    _write(repo, "pkg/calc.py", CALC + MENTIONS)
    _write(repo, "tests/test_more.py", 'CASE = "    breakpoint()\\n"\n\n\ndef test_case():\n    assert "breakpoint()" in CASE\n')
    assert not {"debugger-left", "debug-print", "todo-added"} & set(check(repo, session))


def test_what_was_there_before_or_only_moved_is_not_a_leftover(repo):
    _write(repo, "pkg/calc.py", CALC + "\n\n# TODO: support complex numbers\ndef neg(a):\n    print(a)\n    return -a\n")
    _write(repo, "pkg/cli.py", "import sys\n\n\ndef main():\n    print('usage: calc A B')\n")
    import subprocess
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    session = start(repo)
    # the TODO moves to the top of the file; the CLI gets another print (no logger: printing is its job)
    _write(repo, "pkg/calc.py", "# TODO: support complex numbers\n" + CALC + "\n\ndef neg(a):\n    print(a)\n    return -a\n")
    _write(repo, "pkg/cli.py", "import sys\n\n\ndef main():\n    print('usage: calc A B')\n    print('  adds A and B')\n")
    found = check(repo, session)
    assert not {"todo-added", "debug-print"} & set(found)


def test_abstract_methods_and_base_class_hooks_are_not_placeholders(repo):
    session = start(repo)
    _write(repo, "pkg/base.py", (
        "import abc\nfrom typing import Protocol\n\n\nclass Shape(abc.ABC):\n    @abc.abstractmethod\n"
        "    def area(self) -> float:\n        ...\n\n    def describe(self):\n        raise NotImplementedError\n\n"
        "    def on_draw(self):\n        pass\n\n\nclass Drawable(Protocol):\n    def draw(self) -> None: ...\n"))
    _write(repo, "pkg/shapes.py", "from pkg.base import Shape, Drawable\n" + (repo / "pkg/shapes.py").read_text())
    assert "placeholder" not in check(repo, session)


def test_new_code_nothing_calls(repo):
    session = start(repo)
    _write(repo, "pkg/calc.py", CALC + (
        "\n\ndef mul(a, b):\n    return a * b\n"                  # used by shapes: fine
        "\n\ndef power(a, b):\n    return a ** b\n"               # only tests: public, shown to the user
        "\n\ndef _clamp(x):\n    return max(0, x)\n"              # only tests, private: not connected
        "\n\ndef retry_policy():\n    return 3\n"                 # nothing at all
        "\n\ndef fact(n):\n    return 1 if n < 2 else n * fact(n - 1)\n"   # only itself
        "\n\nclass Helper(dict):\n    def lookup(self, k):\n        return self[k]\n"   # subclass method: may be an override
        "\n\n@register\ndef plugin():\n    return 1\n"
        "\n\nclass FakeSocket:\n    def makefile(self, mode):\n        return None\n\n    def _unused(self):\n        return 1\n"))
    _write(repo, "pkg/shapes.py", "from pkg.calc import add, mul\n\n\ndef perimeter(w, h):\n    return mul(add(w, h), 2)\n")
    _write(repo, "tests/test_more.py", "from pkg.calc import power, _clamp, Helper, FakeSocket\n\n\ndef test_power():\n"
                                       "    assert power(2, 3) == 8 and _clamp(-1) == 0 and Helper and FakeSocket\n")
    rules = {f.key.split("|")[1]: f.rule for f in findings(repo, session) if f.rule in ("unwired-code", "only-tests-use")}
    assert rules == {"power": "only-tests-use", "_clamp": "unwired-code", "retry_policy": "unwired-code",
                     "fact": "unwired-code", "Helper": "only-tests-use",
                     "FakeSocket": "only-tests-use", "_unused": "unwired-code"}     # not makefile: a protocol's method


def test_names_listed_in_all_or_called_from_a_script_are_used(repo):
    session = start(repo)
    _write(repo, "pkg/calc.py", "__all__ = ['add', 'sub', 'mean']\n" + CALC + "\n\ndef mean(xs):\n    return sum(xs) / len(xs)\n")
    _write(repo, "scripts/report.py", "def main():\n    print('report')\n\n\nif __name__ == '__main__':\n    main()\n")
    assert not {"unwired-code", "only-tests-use"} & set(check(repo, session))
