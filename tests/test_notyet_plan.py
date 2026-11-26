"""The agent's own plan: steps left pending when it stops."""
import json

from notyet import gate, plan
from notyet.findings import EngineResult
from tests.test_notyet_gate import _enforce, _write, hook, repo  # noqa: F401  (fixture)


def _line(**entry):
    return json.dumps(entry)


def _use(use_id, name, **inp):
    return _line(type="assistant", message={"role": "assistant", "content": [
        {"type": "tool_use", "id": use_id, "name": name, "input": inp}]})


def _created(use_id, task_id, subject):
    return _line(type="user", message={"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": use_id, "content": f"Task #{task_id} created successfully: {subject}"}]},
        toolUseResult={"task": {"id": task_id, "subject": subject}})


def task_transcript(tmp_path, done=1):
    """Claude Code's task tools: three steps made, `done` of them finished. Lines written twice, as happens."""
    steps = ["Add the retry setting", "Use it in the client", "Document the setting in the README"]
    lines = []
    for i, s in enumerate(steps, 1):
        lines += [_use(f"u{i}", "TaskCreate", subject=s, description=s), _use(f"u{i}", "TaskCreate", subject=s),
                  _created(f"u{i}", str(i), s)]
    for i in range(1, done + 1):
        lines += [_use(f"p{i}", "TaskUpdate", taskId=str(i), status="in_progress"),
                  _use(f"c{i}", "TaskUpdate", taskId=str(i), status="completed")]
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(lines) + "\n")
    return str(path)


def test_reads_task_tools_todowrite_and_codex_plans(tmp_path):
    steps = plan.read(task_transcript(tmp_path, done=1))
    assert [(s.subject, s.status) for s in steps] == [("Add the retry setting", "completed"),
                                                     ("Use it in the client", "pending"),
                                                     ("Document the setting in the README", "pending")]

    todo = tmp_path / "todo.jsonl"
    todo.write_text("\n".join([
        _use("a", "TodoWrite", todos=[{"content": "one", "status": "completed"}, {"content": "two", "status": "pending"},
                                      {"content": "three", "status": "pending"}]),
        _use("b", "TodoWrite", todos=[{"content": "one", "status": "completed"}, {"content": "two", "status": "in_progress"}]),
    ]))
    assert [(s.subject, s.status) for s in plan.read(str(todo))] == [
        ("one", "completed"), ("two", "in_progress"), ("three", "deleted")]

    codex = tmp_path / "codex.jsonl"
    codex.write_text(_line(type="response_item", payload={"type": "function_call", "name": "update_plan", "call_id": "c1",
                                                          "arguments": json.dumps({"plan": [
                                                              {"step": "parse", "status": "completed"},
                                                              {"step": "wire", "status": "pending"}]})}))
    assert [(s.subject, s.status) for s in plan.read(str(codex))] == [("parse", "completed"), ("wire", "pending")]
    assert plan.read(None) == [] and plan.read(str(tmp_path / "missing.jsonl")) == []


def test_findings_name_what_is_left_and_count_the_plan(tmp_path):
    found, facts = plan.findings(plan.read(task_transcript(tmp_path, done=1)))
    assert facts == {"plan": 3, "plan_done": 1}
    assert [f.rule for f in found] == ["plan-unfinished"]
    assert found[0].title == "2 of 3 step(s) in your own plan aren't done"
    assert found[0].evidence == ["not started: Use it in the client", "not started: Document the setting in the README"]
    assert plan.findings(plan.read(task_transcript(tmp_path, done=3)))[0] == []


def test_a_closing_message_that_says_what_is_left_hands_it_over():
    steps = ["Use it in the client", "Document the setting in the README"]
    assert plan.mentioned("Added the setting.\n\nNot done yet: using it in the client and documenting the setting "
                          "in the README; I ran out of context.", steps)
    assert plan.mentioned("Done with the setting.\n\nStill to do:\n\n- use it in the client\n\n- README docs for the setting",
                          steps)
    # a summary that names the steps as finished doesn't
    assert plan.mentioned("All done: I used it in the client and documented the setting in the README.", steps) is None
    # nor one that names only some of them
    assert plan.mentioned("Remaining: using it in the client.", steps) is None


def test_the_gate_sends_the_agent_back_for_its_own_unfinished_plan(repo, monkeypatch, tmp_path):  # noqa: F811
    _enforce(repo)
    monkeypatch.setattr(gate, "default_engines", lambda: [lambda ctx: EngineResult(checks=["fake"])])
    hook(repo, "session-start", source="startup")
    _write(repo, "app.py", "def f():\n    return 2\n")
    transcript = task_transcript(tmp_path, done=1)

    out = hook(repo, "stop", transcript_path=transcript, last_assistant_message="All done!")
    assert out["decision"] == "block" and "2 of 3 step(s) in your own plan" in out["reason"]
    assert "not started: Use it in the client" in out["reason"]

    # same tree, the agent says what's left: handed to the user, who has to look
    out = hook(repo, "stop", transcript_path=transcript, last_assistant_message=(
        "I added the setting.\n\nNot done: using it in the client, and documenting the setting in the README."))
    assert "decision" not in out and out["systemMessage"].startswith("notyet: NEEDS YOUR REVIEW")
    assert "all 3 steps" not in out["systemMessage"] and "1 of 3 steps done" in out["systemMessage"]
    assert "Not done:" in out["systemMessage"] and "agent: Not done: using it in the client" in out["systemMessage"]


def test_an_unfinished_plan_is_checked_even_when_no_file_changed(repo, monkeypatch, tmp_path):  # noqa: F811
    _enforce(repo)
    monkeypatch.setattr(gate, "default_engines", lambda: [lambda ctx: EngineResult(checks=["fake"])])
    hook(repo, "session-start", source="startup")
    assert hook(repo, "stop", transcript_path=task_transcript(tmp_path, done=3)) is None     # nothing to say
    out = hook(repo, "stop", transcript_path=task_transcript(tmp_path, done=0))
    assert out["decision"] == "block" and "3 of 3 step(s)" in out["reason"]
    # the agent finishes them all: nothing changed in the repo, nothing left to say
    assert hook(repo, "stop", transcript_path=task_transcript(tmp_path, done=3)) is None
