"""What the request asked for that nothing in the change answers."""
from notyet import config, gate, snapshot, store
from notyet.engines import asked
from notyet.findings import Context
from tests.conftest import _git, _write


def start(repo, *prompts):
    root = str(repo)
    return store.Session(session_id="a", started=0, baseline_tree=snapshot.snapshot(root),
                         baseline_head=snapshot.head_tree(root), baseline_source="session-start", prompts=list(prompts))


def check(repo, session):
    root = str(repo)
    current = snapshot.snapshot(root)
    ctx = Context(root=root, session=session, config=config.load(root, session.baseline_tree),
                  baseline_tree=session.baseline_tree, current_tree=current,
                  deltas=snapshot.diff_trees(root, session.baseline_tree, current))
    return {f.rule: f for f in asked.run(ctx).findings}


def test_what_a_request_asks_for():
    assert asked.requested("Add a --dry-run flag: fetch everything but don't commit. Tests and README.") == (["--dry-run"], True)
    assert asked.requested("Add a start_return tool like lookup_order that writes to logs/returns.jsonl.") == (
        ["returns.jsonl", "start_return"], False)
    # named as something to read or imitate, not to make
    assert asked.requested("Add `halve`. Follow the conventions in PRINCIPLES.md and STYLE.md.") == (["halve"], False)
    # negated, pasted, or docs only mentioned
    assert asked.requested("Don't add a --force flag. The docs say retries cap at 3; fix the code.") == ([], False)
    assert asked.requested("Here:\n```\nfoo_bar --verbose failed\n```\nfix it") == ([], False)
    assert asked.requested("Use `retryPolicy()` here, and document it.") == (["retryPolicy"], True)


def test_a_new_name_the_request_asked_for_that_never_appeared(repo):
    _write(repo, "README.md", "# calc\n")
    _git(repo, "add", "-A")
    session = start(repo, "Add a mean function `mean_of` to calc and a --precision flag to the CLI, same style as add. "
                          "Update the README.")
    _write(repo, "pkg/calc.py", (repo / "pkg/calc.py").read_text() + "\n\ndef mean_of(xs):\n    return sum(xs) / len(xs)\n")
    found = check(repo, session)
    assert found["asked-missing"].title == "you asked for `--precision`; it appears nowhere in the repo"
    assert "docs-missing" in found
    # both done: nothing to say
    _write(repo, "pkg/cli.py", "FLAGS = ['--precision']\n")
    _write(repo, "README.md", "# calc\n\n`--precision N` rounds the mean.\n")
    assert check(repo, session) == {}


def test_names_the_repo_already_had_are_context_not_requests(repo):
    session = start(repo, "Make shout() in pkg/strings.py handle None, the same way add handles it in calc.py.")
    _write(repo, "pkg/strings.py", "def shout(s):\n    return (s or '').upper()\n")
    assert check(repo, session) == {}


def test_a_repo_without_docs_is_not_asked_to_update_them(repo):
    session = start(repo, "Add `halve` to calc and document it.")
    _write(repo, "pkg/calc.py", (repo / "pkg/calc.py").read_text() + "\n\ndef halve(x):\n    return x / 2\n")
    assert check(repo, session) == {}


def test_the_gate_hands_it_over_when_the_closing_message_says_what_was_left(repo, monkeypatch):
    from notyet.engines import asked as asked_engine
    _write(repo, "README.md", "# calc\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "readme")
    monkeypatch.setattr(gate, "default_engines", lambda: [asked_engine.run])
    root = str(repo)
    session = store.start_session(root, "s", "session-start")
    session.prompts = ["Add `halve` to calc and update the README."]
    _write(repo, "pkg/calc.py", (repo / "pkg/calc.py").read_text() + "\n\ndef halve(x):\n    return x / 2\n")
    d = gate.check(root, session, message="Added halve.")
    assert d.verdict == "blocked" and "no documentation file changed" in (d.block_reason or "")
    d = gate.check(root, session, message="Added halve.\n\nI didn't update the README: it doesn't list functions.")
    assert d.verdict == "needs-review" and "Not done:" in d.summary
