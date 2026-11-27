"""notyet check (CI), stats, per-rule severities, the Codex hooks."""
import json

import pytest

from notyet import cli, config, gate, snapshot, stats, store
from notyet.findings import EngineResult, Finding
from notyet.hooks import claude, codex
from tests.conftest import _git, _write

BROKEN_ADD = "def add(a, b):\n    return a + b + 1\n\n\ndef sub(a, b):\n    return a - b\n"


def branch_with_a_bug(repo):
    _git(repo, "switch", "-qc", "feature")
    _write(repo, "pkg/calc.py", BROKEN_ADD)
    _write(repo, "tests/test_calc.py", (repo / "tests/test_calc.py").read_text().replace("== 3", "== 4"))
    _write(repo, "tests/test_shapes.py", (repo / "tests/test_shapes.py").read_text().replace("== 10", "== 13"))
    _git(repo, "commit", "-qam", "break add, and the tests with it")


def check(repo, capsys, *args):
    code = cli.main(["check", "--path", str(repo), "--base", "main", "--merge-base", *args])
    return code, capsys.readouterr().out


def test_check_fails_a_branch_that_changes_tests_to_fit(repo, capsys):
    branch_with_a_bug(repo)
    code, out = check(repo, capsys, "--format", "json")
    data = json.loads(out)
    assert code == 1 and data["verdict"] == "failed"
    assert {f["rule"] for f in data["findings"] if f["id"] in data["failing"]} == {"test-changed-to-pass"}


def test_a_reason_in_a_commit_message_accepts_an_item(repo, capsys):
    branch_with_a_bug(repo)
    ids = json.loads(check(repo, capsys, "--format", "json")[1])["failing"]
    _git(repo, "commit", "-q", "--allow-empty", "-m", f"Explain\n\nNotyet-Ack: {', '.join(ids)} add now counts from one, as asked")
    code, out = check(repo, capsys, "--format", "json")
    assert code == 0 and json.loads(out)["failing"] == []
    assert any(f["reason"] == "add now counts from one, as asked" for f in json.loads(out)["findings"])


def test_github_annotations_and_summary(repo, capsys, tmp_path, monkeypatch):
    branch_with_a_bug(repo)
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    code, out = check(repo, capsys, "--format", "github")
    assert code == 1 and "::error file=tests/test_calc.py::" in out
    assert "# notyet receipt" in (tmp_path / "summary.md").read_text()


def test_the_brief_as_a_pull_request_comment_and_the_description_as_the_request(repo, capsys, tmp_path):
    _write(repo, "README.md", "# calc\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "readme")
    branch_with_a_bug(repo)
    request = tmp_path / "pr.md"
    request.write_text("Count from one\n\nadd() now counts from one. Adds a --one-based flag and documents it in the README.")
    comment = tmp_path / "comment.md"
    code, _ = check(repo, capsys, "--request-file", str(request), "--comment-file", str(comment))
    text = comment.read_text()
    assert code == 1 and text.startswith("<!-- notyet -->\n**notyet: FAILED.**")
    assert "```text" in text and "Not done:" in text and "`--one-based`" in text
    assert "no documentation file changed" in text and "What the tests say has changed:" in text
    assert "Notyet-Ack: <id> <reason>" in text and "<details><summary>Full receipt</summary>" in text


def test_fail_on_never_and_resolve(repo, capsys):
    _git(repo, "switch", "-qc", "feature")
    _write(repo, "pkg/calc.py", (repo / "pkg/calc.py").read_text() + "\n\ndef mul(a, b):\n    return a * b\n")
    _git(repo, "commit", "-qam", "untested")
    assert check(repo, capsys)[0] == 0
    assert check(repo, capsys, "--fail-on", "resolve")[0] == 1
    branch_with_a_bug_again = (repo / "pkg/calc.py")
    branch_with_a_bug_again.write_text(BROKEN_ADD)
    assert check(repo, capsys, "--fail-on", "never")[0] == 0


def test_check_works_without_a_config_file(repo, capsys):
    _git(repo, "rm", "-q", ".notyet.toml")
    _git(repo, "commit", "-qm", "no config")
    branch_with_a_bug(repo)
    from tests.conftest import PYTEST
    assert check(repo, capsys, "--test-command", PYTEST)[0] == 1


def test_an_unknown_base_is_an_error(repo):
    assert cli.main(["check", "--path", str(repo), "--base", "nope"]) == 2


def test_rules_can_be_lowered_or_turned_off(repo):
    cfg = config.parse('[rules]\n"a" = "note"\nb = "off"\nc = "block"\nd = "loud"\n', "x")
    assert cfg.rules == {"a": "note", "b": "off", "c": "block"} and "rules.d" in cfg.problems[0]
    fs = [Finding("a", "block", "t"), Finding("b", "block", "t"), Finding("c", "resolve", "t"), Finding("c", "note", "t")]
    assert [(f.rule, f.severity) for f in gate._as_configured(fs, cfg.rules)] == [("a", "note"), ("c", "block"), ("c", "note")]


def test_rules_are_read_from_the_session_start_config(repo):
    root = str(repo)
    session = store.start_session(root, "r", "session-start")
    _write(repo, ".notyet.toml", (repo / ".notyet.toml").read_text() + '[rules]\nx = "off"\n')
    d = gate.check(root, session, engines=[lambda ctx: EngineResult(findings=[Finding("x", "block", "t")])])
    assert d.verdict == "blocked"


def test_an_incomplete_check_is_not_a_pass(repo):
    session = store.start_session(str(repo), "i", "session-start")
    _write(repo, "pkg/calc.py", "x = 1\n")
    d = gate.check(str(repo), session, engines=[lambda ctx: EngineResult(checks=["ran some"], gaps=["tests incomplete"])])
    assert d.verdict == "incomplete" and d.summary.startswith("notyet: INCOMPLETE")


def test_stats_say_what_findings_led_to(repo):
    root = str(repo)
    session = store.start_session(root, "st", "session-start")
    found = [[Finding("fixed-rule", "block", "t"), Finding("argued-rule", "resolve", "t")], [Finding("argued-rule", "resolve", "t")]]
    for n, fs in enumerate(found):
        _write(repo, "pkg/calc.py", f"x = {n}\n")
        gate.check(root, session, engines=[lambda ctx, fs=fs: EngineResult(findings=list(fs), checks=["c"])])
        if n == 0:
            session.acks[found[0][1].id] = {"category": "intended", "reason": "because"}
    kinds = {rule: kind for rule, kind in stats.outcomes(session).values()}
    assert kinds == {"fixed-rule": "fixed", "argued-rule": "justified"}
    text = stats.render([session])
    assert "2 check(s)" in text and "fixed-rule" in text
    assert stats.render([]) == "No checks recorded yet."


def test_codex_hooks_install_and_run(repo, capsys):
    assert cli.main(["install", "codex", "--path", str(repo), "--yes"]) == 0
    assert "/hooks" in capsys.readouterr().out
    hooks = json.loads((repo / ".codex/hooks.json").read_text())["hooks"]
    assert set(hooks) == {"SessionStart", "UserPromptSubmit", "Stop"}
    assert " -m notyet hook codex stop" in hooks["Stop"][0]["hooks"][0]["command"]
    assert codex.settings_path(str(repo), "user").parts[-2:] == (".codex", "hooks.json")
    # Codex's payload: turn_id and model, no background_tasks
    base = {"session_id": "c1", "transcript_path": None, "cwd": str(repo), "model": "m"}
    claude.handle("session-start", {**base, "source": "startup"})
    claude.handle("prompt", {**base, "turn_id": "1", "prompt": "break it"})
    _write(repo, "pkg/calc.py", BROKEN_ADD)
    out = claude.handle("stop", {**base, "turn_id": "1", "stop_hook_active": False, "last_assistant_message": "Done."})
    assert out is not None and out["decision"] == "block"


def test_reinstalling_replaces_hooks_of_either_agent():
    once = claude.merged_settings({}, 100, "claude")
    twice = claude.merged_settings(once, 200, "codex")
    assert len(twice["hooks"]["Stop"]) == 1 and "hook codex stop" in twice["hooks"]["Stop"][0]["hooks"][0]["command"]


@pytest.mark.parametrize("stops,task,pending", [
    ([], {"id": "a", "type": "shell", "status": "running"}, True),                     # a build just started
    ([["a"], ["a"]], {"id": "a", "type": "shell", "status": "running"}, False),        # a server: here for good
    ([["a"], ["a"]], {"id": "b", "type": "shell", "status": "running"}, True),
    ([["a"]] * 5, {"id": "a", "type": "subagent", "status": "running"}, True),         # delegated work always counts
])
def test_which_background_tasks_hold_the_check_back(stops, task, pending):
    session = store.Session("s", 0, "t", None, "head", stops=[{"at": 0, "busy": True, "tasks": s} for s in stops])
    assert claude._is_pending_work(task, session) is pending


def test_undoing_everything_back_to_the_start_is_still_seen(repo):
    root = str(repo)
    payload = {"cwd": root, "session_id": "u"}
    claude.handle("session-start", {**payload, "source": "startup"})
    claude.handle("prompt", {**payload, "prompt": "add it"})
    original = (repo / "pkg/strings.py").read_text()
    _write(repo, "pkg/strings.py", original + "\n\ndef whisper(s):\n    return s.lower() + '...'\n")
    claude.handle("prompt", {**payload, "prompt": "something else"})
    _write(repo, "pkg/strings.py", original)
    claude.handle("stop", payload)
    run = store.load_session(root, "u").runs[-1]
    assert [f["rule"] for f in run.result["findings"]] == ["undone-work"]
    assert snapshot.snapshot(root) == store.load_session(root, "u").baseline_tree


def test_init_says_whether_the_test_command_works(repo, capsys):
    from tests.conftest import PYTEST
    assert cli.main(["init", str(repo), "--force", "--yes", "--test-command", PYTEST]) == 0
    assert "The test command works: it finds 4 test(s)." in capsys.readouterr().out
    assert cli.main(["init", str(repo), "--force", "--yes", "--test-command", "python3 -c 'raise SystemExit(3)'"]) == 0
    assert "Warning" in capsys.readouterr().out


def test_init_warns_when_the_command_only_works_with_notyets_import_path(repo, capsys, monkeypatch):
    # a src layout that isn't installed: notyet's own runs import it, a person or an agent running the command don't
    from tests.conftest import PYTEST
    (repo / "src").mkdir()
    _git(repo, "mv", "pkg", "src/pkg")
    monkeypatch.delenv("PYTHONPATH", raising=False)
    assert cli.main(["init", str(repo), "--force", "--yes", "--test-command", PYTEST]) == 0
    out = capsys.readouterr().out
    assert "The test command works: it finds 4 test(s)." in out
    assert 'run by hand, the same command can\'t import the code' in out and 'pythonpath = ["src"]' in out


def test_status_and_ack_from_a_new_session_reach_the_last_check(repo, capsys):
    # /notyet:status in a fresh window: that session has no check yet, the one before does
    root = str(repo)
    first = {"cwd": root, "session_id": "one"}
    claude.handle("session-start", {**first, "source": "startup"})
    claude.handle("prompt", {**first, "prompt": "add mul"})
    _write(repo, "pkg/calc.py", (repo / "pkg/calc.py").read_text() + "\n\ndef mul(a, b):\n    return a * b\n")
    claude.handle("stop", first)
    claude.handle("session-start", {"cwd": root, "session_id": "two", "source": "startup"})
    capsys.readouterr()
    assert cli.main(["status", "--path", root]) == 0
    out = capsys.readouterr().out
    assert "The last check was in session one" in out and "No test runs:" in out
    fid = out.split("Open items")[1].splitlines()[1].split()[0]
    assert cli.main(["ack", fid, "mul is tested by the next pull request", "--path", root]) == 0
    assert fid in store.load_session(root, "one").acks
