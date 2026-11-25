"""HEAD moving during a session: what came from elsewhere isn't the session's change."""
from notyet import moves, snapshot, store
from tests.conftest import _git, _write


def start(repo):
    return store.start_session(str(repo), "s", "session-start")


def changed(repo, session):
    return sorted(d.path for d in snapshot.diff_trees(str(repo), session.baseline_tree, snapshot.snapshot(str(repo))))


def upstream(repo, rel="pkg/strings.py", text="def shout(s):\n    return s.upper() + '!'\n"):
    _git(repo, "switch", "-qc", "upstream")
    _write(repo, rel, text)
    _git(repo, "commit", "-qam", "upstream")
    _git(repo, "switch", "-q", "main")


def test_a_fast_forward_moves_the_starting_point(repo):
    upstream(repo)
    session = start(repo)
    _write(repo, "pkg/calc.py", (repo / "pkg/calc.py").read_text() + "\n# mine\n")
    _git(repo, "merge", "-q", "--ff-only", "upstream")
    moves.follow(str(repo), session)
    assert changed(repo, session) == ["pkg/calc.py"]
    assert session.moves == [{"commits": 1, "head": moves.head_commit(str(repo)), "ok": True}]


def test_a_merge_keeps_the_sessions_own_commits_as_its_change(repo):
    upstream(repo)
    session = start(repo)
    _write(repo, "pkg/calc.py", (repo / "pkg/calc.py").read_text() + "\n# mine\n")
    _git(repo, "commit", "-qam", "mine")
    _git(repo, "merge", "-q", "--no-edit", "upstream")
    moves.follow(str(repo), session)
    assert changed(repo, session) == ["pkg/calc.py"]


def test_a_rebase_onto_upstream(repo):
    upstream(repo)
    session = start(repo)
    _write(repo, "pkg/calc.py", (repo / "pkg/calc.py").read_text() + "\n# mine\n")
    _git(repo, "commit", "-qam", "mine")
    _git(repo, "rebase", "-q", "upstream")
    moves.follow(str(repo), session)
    assert changed(repo, session) == ["pkg/calc.py"]


def test_the_users_uncommitted_edits_stay_out_of_the_change(repo):
    upstream(repo)
    _write(repo, "pkg/shapes.py", (repo / "pkg/shapes.py").read_text() + "\n# the user's\n")
    session = start(repo)
    _git(repo, "merge", "-q", "--ff-only", "upstream")
    moves.follow(str(repo), session)
    assert changed(repo, session) == []


def test_switching_branches(repo):
    _git(repo, "switch", "-qc", "other")
    (repo / "tests/test_strings.py").unlink()
    _git(repo, "commit", "-qam", "other")
    _git(repo, "switch", "-q", "main")
    session = start(repo)
    _git(repo, "switch", "-q", "other")
    moves.follow(str(repo), session)
    assert changed(repo, session) == []


def test_the_sessions_own_commits_do_not_move_it(repo):
    session = start(repo)
    base = session.baseline_tree
    (repo / "tests/test_strings.py").unlink()
    _git(repo, "commit", "-qam", "drop tests")
    _git(repo, "commit", "-q", "--amend", "-m", "drop tests, quietly")
    moves.follow(str(repo), session)
    assert session.baseline_tree == base and changed(repo, session) == ["tests/test_strings.py"]


def test_resetting_to_an_older_commit_is_seen(repo):
    _write(repo, "pkg/extra.py", "X = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "the user's earlier work")
    session = start(repo)
    _git(repo, "reset", "-q", "--hard", "HEAD~1")
    moves.follow(str(repo), session)
    assert changed(repo, session) == ["pkg/extra.py"]      # work that disappeared is part of the change


def test_a_conflict_keeps_the_old_starting_point_and_says_so(repo):
    upstream(repo, "pkg/calc.py", "def add(a, b):\n    return b + a\n\n\ndef sub(a, b):\n    return a - b\n")
    _write(repo, "pkg/calc.py", "def add(a, b):\n    return sum([a, b])\n\n\ndef sub(a, b):\n    return a - b\n")
    session = start(repo)
    base = session.baseline_tree
    _git(repo, "checkout", "-q", "--force", "upstream")
    moves.follow(str(repo), session)
    assert session.baseline_tree == base and session.moves[0]["ok"] is False
