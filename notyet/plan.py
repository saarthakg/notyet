"""The agent's own plan: steps it wrote down and didn't finish.

The most reported way an agent hands over unfinished work is stopping partway through its own to-do list and
summarizing as if it were done (claude-code #6159, #1632, #599). The list is in the session transcript the Stop
hook is given, so no guessing is needed: a step still pending or in progress when the agent stops is unfinished,
by the agent's own account.

Read from the transcript (JSON lines), whatever wrote it:
- Claude Code's task tools: TaskCreate (the id is in the tool result), TaskUpdate (status, subject);
- Claude Code's older TodoWrite: the whole list each time;
- Codex's update_plan: the whole plan each time, as a JSON string of arguments.

A step the agent deleted unfinished is not counted as unfinished, but it is shown to the user.
"""
import json
import re
from dataclasses import dataclass
from typing import Optional

from notyet.findings import Finding, digests

OPEN = ("pending", "in_progress")
MAX_BYTES = 64 * 1024 * 1024
_TOOLS = ("TaskCreate", "TaskUpdate", "TodoWrite", "update_plan")


@dataclass
class Step:
    id: str
    subject: str
    status: str = "pending"


def read(transcript_path: Optional[str]) -> list[Step]:
    """The plan as it stands at the end of the transcript, in the order the steps were made."""
    if not transcript_path:
        return []
    try:
        with open(transcript_path, "rb") as fh:
            data = fh.read(MAX_BYTES)
    except OSError:
        return []
    steps: dict[str, Step] = {}
    pending_creates: dict[str, dict] = {}     # tool_use id -> TaskCreate input, until its result names the task id
    seen: set[tuple[str, str]] = set()
    for raw in data.decode("utf-8", "replace").splitlines():
        if '"task"' not in raw and not any(t in raw for t in _TOOLS):     # cheap: most lines aren't about the plan
            continue
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        for kind, use_id, value in _events(entry):
            if use_id and (kind, use_id) in seen:
                continue        # the same message is sometimes written twice
            if use_id:
                seen.add((kind, use_id))
            _apply(steps, pending_creates, kind, use_id, value)
    return list(steps.values())


def _events(entry: dict):
    """(kind, tool use id, value) for each plan event in one transcript line."""
    message = entry.get("message")
    message = message if isinstance(message, dict) else {}
    content = message.get("content")
    content = content if isinstance(content, list) else []
    result = entry.get("toolUseResult")
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_use" and block.get("name") in _TOOLS:
            yield f"use:{block['name']}", block.get("id"), block.get("input") or {}
        elif block.get("type") == "tool_result" and isinstance(result, dict) and isinstance(result.get("task"), dict):
            yield "created", block.get("tool_use_id"), result["task"]
    # Codex: {"type": "response_item", "payload": {"type": "function_call", "name": "update_plan", "arguments": "..."}}
    payload = entry.get("payload")
    payload = payload if isinstance(payload, dict) else entry
    if payload.get("type") == "function_call" and payload.get("name") == "update_plan":
        try:
            args = json.loads(payload.get("arguments") or "{}")
        except ValueError:
            args = {}
        yield "use:update_plan", payload.get("call_id"), args


def _apply(steps: dict[str, Step], creates: dict[str, dict], kind: str, use_id: Optional[str], value: dict) -> None:
    if kind == "use:TaskCreate":
        creates[use_id or ""] = value
    elif kind == "created":
        task_id = str(value.get("id", ""))
        made = creates.pop(use_id or "", {})
        if task_id:
            steps[task_id] = Step(task_id, str(value.get("subject") or made.get("subject") or ""))
    elif kind == "use:TaskUpdate":
        step = steps.get(str(value.get("taskId", "")))
        if step is not None:
            step.status = str(value.get("status") or step.status)
            step.subject = str(value.get("subject") or step.subject)
    elif kind == "use:TodoWrite" and isinstance(value.get("todos"), list):
        _replace(steps, [(t.get("content", ""), t.get("status", "pending")) for t in value["todos"] if isinstance(t, dict)])
    elif kind == "use:update_plan" and isinstance(value.get("plan"), list):
        _replace(steps, [(t.get("step", ""), t.get("status", "pending")) for t in value["plan"] if isinstance(t, dict)])


def _replace(steps: dict[str, Step], items: list[tuple[str, str]]) -> None:
    """A whole-list update: steps left out of it were dropped."""
    for step in steps.values():
        if step.status in OPEN:
            step.status = "deleted"
    for subject, status in items:
        if str(subject).strip():
            steps[f"list:{subject}"] = Step(f"list:{subject}", str(subject), str(status))


def findings(steps: list[Step]) -> tuple[list[Finding], dict[str, int]]:
    """What the plan says isn't done, and counts for the evidence line."""
    real = [s for s in steps if s.subject.strip()]
    open_ = [s for s in real if s.status in OPEN]
    dropped = [s for s in real if s.status == "deleted"]
    facts = {"plan": len([s for s in real if s.status != "deleted"]),
             "plan_done": len([s for s in real if s.status == "completed"])} if real else {}
    out = []
    if open_:
        subjects = [s.subject.strip() for s in open_]
        out.append(Finding(
            rule="plan-unfinished", severity="resolve", location="the agent's plan",
            title=f"{len(open_)} of {facts['plan']} step(s) in your own plan aren't done",
            evidence=[f"{'in progress' if s.status == 'in_progress' else 'not started'}: {s.subject.strip()[:150]}"
                      for s in open_[:6]],
            key="plan|" + "|".join(sorted(subjects)), covers=digests(subjects),
            action="Finish them, or mark the ones that no longer apply as completed or deleted. If you're stopping "
                   "on purpose, say which steps are left and why in your closing message: the user will see it."))
    if dropped:
        out.append(Finding(
            rule="plan-dropped", severity="note", location="the agent's plan",
            title=f"{len(dropped)} step(s) were taken off the plan unfinished",
            evidence=[s.subject.strip()[:150] for s in dropped[:6]],
            key="plan-dropped|" + "|".join(sorted(s.subject for s in dropped))))
    return out, facts


_WORD = re.compile(r"[a-z0-9_]{3,}")
_STOP = {"add", "the", "and", "for", "all", "new", "use", "with", "that", "this", "from", "into", "when", "then", "them", "have", "make", "sure", "each", "they", "will",
         "should", "would", "also", "than", "more", "only", "file", "files", "code", "test", "tests", "update"}
_LEFT = re.compile(r"\b(not (yet )?(done|finished|complete|implemented|started)|left (to do|for|undone|open)|remain|"
                   r"still (need|to do|todo|pending|open)|next steps?|didn'?t (get|finish|do|update|add|write|change|touch)|haven'?t|not (updated|changed|documented|added)|not addressed|"
                   r"follow.?up|out of scope|skipp|deferr|postpon|todo|pending|incomplete|unfinished|dropp|no longer|not needed|won'?t)", re.IGNORECASE)


def mentioned(message: str, subjects: list[str]) -> Optional[str]:
    """The passage of `message` that tells the user these steps are left, or None. Each step has to be recognizable
    in it (most of its words), next to a word for being left undone: a summary that names the steps as done doesn't
    count."""
    if not message or not subjects:
        return None
    words: set[str] = set()
    said = ""
    for block in re.split(r"\n\s*\n", message):
        if _LEFT.search(block):         # a passage about what's left, and the list under it
            words = set(_WORD.findall(block.lower()))
            said = next((x for x in block.splitlines() if _LEFT.search(x)), block)
        elif said and re.match(r"\s*([-*|]|\d+[.)])", block):
            words |= set(_WORD.findall(block.lower()))
        else:
            words, said = set(), ""
        if said and all(_recognized(s, words, subjects) for s in subjects):
            return re.sub(r"\s+", " ", said).strip(" -*|#")[:240]
    return None


_CODE = re.compile(r"`([^`]+)`|([A-Za-z_][\w.]*)\(|\b([a-z]\w*_\w+|[A-Za-z]\w*\.[A-Za-z]\w*)\b")


def _recognized(subject: str, words: set[str], subjects: list[str]) -> bool:
    """Is `subject` named among `words`? By a name from its code (`mul(`, `retry_policy`, `config.py`) that no other
    step has, or by most of its words."""
    others = " ".join(s for s in subjects if s != subject).lower()
    for m in _CODE.finditer(subject):
        name = next(g for g in m.groups() if g).lower()
        parts = _WORD.findall(name)
        if parts and name not in others and all(p in words for p in parts):
            return True
    key = [w for w in _WORD.findall(subject.lower()) if w not in _STOP]
    if not key:
        return False
    return sum(w in words for w in key) >= max(1, (len(key) * 3 + 4) // 5)      # 60%, rounded up
