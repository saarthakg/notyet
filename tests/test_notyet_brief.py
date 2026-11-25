"""What the user sees, and what the agent is sent back for."""
import pytest

from notyet import gate, store
from notyet.disclosure import disclosed
from tests.conftest import _git, _write

REVERSED = "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return b - a\n"
T = ["tests/test_calc.py::test_sub"]


@pytest.mark.parametrize("message,told", [
    ("Done. I changed `test_sub` to expect -2, since you asked for the reversed order.", True),
    ("I reversed sub.\n\nTests updated:\n\n- tests/test_calc.py: `sub(3, 1)` is now -2\n", True),
    ("**Changed tests:**\n\n| test | why |\n| `test_sub` | reversed on request |", True),
    ("Done. All 12 tests in tests/test_calc.py pass.", False),                # named, but nothing about changing it
    ("I changed sub and ran the tests.", False),                               # changed, but no test named
    ("I updated test_subtotal.", False),                                       # another test's name
    ("", False),
])
def test_disclosure(message, told):
    assert bool(disclosed(message, T)) is told


def change_and_stop(repo, message, mode="enforce"):
    session = store.start_session(str(repo), f"b{hash(message)}", "session-start")
    _write(repo, "pkg/calc.py", REVERSED)
    _write(repo, "tests/test_calc.py", (repo / "tests/test_calc.py").read_text().replace("sub(3, 1) == 2", "sub(3, 1) == -2"))
    return gate.check(str(repo), session, message=message), session


def test_a_test_change_the_agent_told_the_user_about_goes_to_the_user_not_back_to_the_agent(repo):
    decision, session = change_and_stop(repo, "Reversed `sub` as asked, and updated test_sub to match.")
    assert decision.verdict == "needs-review" and decision.block_reason is None
    assert "tests/test_calc.py::test_sub" in decision.summary
    assert "- assert sub(3, 1) == 2" in decision.summary and "+ assert sub(3, 1) == -2" in decision.summary
    assert "agent: Reversed `sub` as asked, and updated test_sub to match." in decision.summary


def test_one_it_did_not_mention_is_sent_back_and_telling_the_user_is_enough(repo):
    decision, session = change_and_stop(repo, "Reversed sub. All tests pass.")
    assert decision.verdict == "blocked" and "closing message" in (decision.block_reason or "")
    again = gate.check(str(repo), session, message="Reversed sub. I also changed test_sub in tests/test_calc.py: it now expects -2.")
    assert again.verdict == "needs-review"


def test_a_regression_is_sent_back_whatever_the_agent_says(repo):
    session = store.start_session(str(repo), "reg", "session-start")
    _write(repo, "pkg/calc.py", REVERSED)
    decision = gate.check(str(repo), session, message="I changed test_sub and tests/test_calc.py and everything else too.")
    assert decision.verdict == "blocked" and "passed at session start and fails now" in (decision.block_reason or "")
    assert "Broken:" in decision.summary


def test_what_moved_or_lint_would_say_does_not_interrupt(repo):
    """Renaming what the tests import: one line for the user, nothing for the agent to do."""
    session = store.start_session(str(repo), "mv", "session-start")
    _write(repo, "pkg/strings.py", "def loud(s):\n    return s.upper()\n")
    _write(repo, "tests/test_strings.py", "from pkg.strings import loud\n\n\ndef test_shout():\n    assert loud('a') == 'A'\n")
    decision = gate.check(str(repo), session, message="Renamed shout to loud.")
    assert decision.verdict == "passed" and "can't be rerun as they were" in decision.summary


def test_a_quiet_pass_says_what_it_rests_on(repo):
    session = store.start_session(str(repo), "q", "session-start")
    _write(repo, "pkg/strings.py", "def shout(s):\n    out = s.upper()\n    return out\n")
    decision = gate.check(str(repo), session)
    first, *rest = decision.summary.splitlines()
    assert first == "notyet: PASSED. 4 tests pass (the whole suite); every changed line ran"
    assert len(rest) == 1 and "Receipt:" in rest[0]


def test_untested_lines_are_quoted(repo):
    session = store.start_session(str(repo), "u", "session-start")
    _write(repo, "pkg/strings.py", "def shout(s):\n    if not s:\n        raise ValueError('empty')\n    return s.upper()\n")
    decision = gate.check(str(repo), session)
    assert decision.verdict == "blocked" and "No test runs:" in decision.summary and "raise ValueError('empty')" in decision.summary


def test_an_undisclosed_edit_of_a_failing_test_is_sent_back(repo):
    _write(repo, "pkg/calc.py", REVERSED)
    _git(repo, "commit", "-qam", "already broken")
    session = store.start_session(str(repo), "f", "session-start")
    _write(repo, "tests/test_calc.py", (repo / "tests/test_calc.py").read_text().replace("sub(3, 1) == 2", "sub(3, 1) == -2"))
    assert gate.check(str(repo), session, message="The suite is green.").verdict == "blocked"
    told = gate.check(str(repo), session, message="test_sub expected the wrong value; I changed it.")
    assert told.verdict == "needs-review"


def test_what_was_said_about_tests_holds_however_they_are_grouped(repo):
    from notyet.findings import Finding, find_ack
    acks = {"x": {"category": "disclosed", "rule": "test-changed-to-pass", "tests": ["a::1", "a::2"], "at": 1},
            "y": {"category": "disclosed", "rule": "test-changed-to-pass", "tests": ["a::3"], "at": 2}}
    grouped = Finding("test-changed-to-pass", "block", "3 edited", "a", tests=["a::1", "a::2", "a::3"])
    assert find_ack(grouped, acks) == acks["y"]
    assert find_ack(Finding("test-changed-to-pass", "block", "4", "a", tests=["a::1", "a::4"]), acks) is None
    assert find_ack(Finding("test-removed", "block", "1", "a::1", tests=["a::1"]), acks) is None


def test_a_removed_test_shows_what_it_checked_or_what_replaced_it(repo):
    session = store.start_session(str(repo), "rm", "session-start")
    text = (repo / "tests/test_calc.py").read_text()
    _write(repo, "pkg/calc.py", REVERSED)
    _write(repo, "tests/test_calc.py", text.replace("def test_sub():\n    assert sub(3, 1) == 2", "def test_sub_reversed():\n    assert sub(3, 1) == -2"))
    summary = gate.check(str(repo), session, message="I replaced test_sub in tests/test_calc.py.").summary
    assert "test_sub is gone" in summary and "~ now test_sub_reversed" in summary and "+ assert sub(3, 1) == -2" in summary
