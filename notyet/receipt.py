"""What the gate says: the receipt for the human, a short summary shown to
the user, and the message the agent gets when it's blocked.

The agent-facing message stays small (Claude Code caps hook text at 10,000
characters, and long messages get skimmed): the top findings, what to do,
and how to acknowledge.
"""
import os
import re
import shlex
import sys
import time
from typing import Sequence

from notyet import store
from notyet.findings import Context, EngineResult, Finding, find_ack, uncovered

MAX_AGENT_FINDINGS = 8
MAX_AGENT_CHARS = 3500

VERDICT_TEXT = {
    "passed": "PASSED",
    "incomplete": "INCOMPLETE: nothing wrong in what ran",
    "needs-review": "NEEDS YOUR REVIEW",
    "blocked": "NOT DONE YET",
    "unresolved": "UNRESOLVED",
    "reported": "REPORTED (report mode: not enforced)",
    "not-checked": "NOT CHECKED",
}


def invocation() -> str:
    """How the agent should call notyet (the same interpreter the hook runs in)."""
    entry = os.environ.get("NOTYET_ENTRY")             # run from a checkout (the plugin), not installed
    target = shlex.quote(entry) if entry else "-m notyet"
    return f"{shlex.quote(sys.executable)} {target}"   # paths with spaces must survive a copy into a shell


def _where(f: Finding) -> str:
    """ " (location)", unless the title already names it."""
    return f" ({f.location})" if f.location and f.location not in f.title else ""


def write(root: str, session: store.Session, ctx: Context, result: EngineResult,
          standing: list[Finding], verdict: str) -> str:
    n = len(session.runs) + 1
    path = store.receipts_dir(root) / f"{_safe(session.session_id)}-{n:03d}.md"
    path.write_text(render(session, ctx, result, standing, verdict))
    (store.receipts_dir(root) / "latest.md").write_text(path.read_text())
    return str(path)


def render(session: store.Session, ctx: Context, result: EngineResult,
           standing: list[Finding], verdict: str) -> str:
    standing_ids = {f.id for f in standing}
    lines = [
        "# notyet receipt",
        "",
        f"**Verdict: {VERDICT_TEXT.get(verdict, verdict)}**" + (" **(incomplete)**" if result.gaps and verdict != "incomplete" else ""),
        "",
        *[f"- **Not checked:** {g}" for g in result.gaps],
        *([""] if result.gaps else []),
        f"Session `{session.session_id}` · checked {time.strftime('%Y-%m-%d %H:%M:%S')} · "
        f"compared with: {_baseline_label(session)}",
        "",
    ]
    if short := brief(session, result, standing):
        lines += ["## In short", "", "```", *short, "```", ""]
    needs_human = [f for f in result.findings if (find_ack(f, session.acks) or {}).get("category") == "needs-human"]
    if verdict == "unresolved" or needs_human:
        lines += ["## Needs your attention", ""]
        if verdict == "unresolved":
            lines += [f"- **unresolved** `{f.id}` {f.title}" + _where(f)
                      for f in standing]
        lines += [f"- **needs human** `{f.id}` {f.title}: {(find_ack(f, session.acks) or {}).get('reason', '')}"
                  for f in needs_human]
        lines.append("")

    lines += [f"## Change since {'HEAD' if session.baseline_source == 'head' else 'the base' if session.baseline_source == 'ref' else 'session start'}: "
              f"{len(ctx.deltas)} file(s)", ""]
    lines += [f"- {d.status} {d.path}" + (f" (from {d.old_path})" if d.old_path else "") for d in ctx.deltas[:50]]
    if len(ctx.deltas) > 50:
        lines.append(f"- … and {len(ctx.deltas) - 50} more")
    lines.append("")

    if result.findings:
        lines += ["## Findings", ""]
        for f in result.findings:
            state = "open" if f.id in standing_ids else _ack_state(session, f)
            lines.append(f"- [{f.severity}] `{f.id}` {f.title}" + _where(f)
                         + f": {state}")
            lines += [f"  - {e}" for e in f.evidence[:6]]
        lines.append("")

    acked = [(fid, a) for fid, a in session.acks.items() if a.get("category") != "needs-human"]
    if acked:
        lines += ["## Acknowledged by the agent", ""]
        lines += [f"- `{fid}` ({a.get('category', 'acknowledged')}): {a.get('reason', '')}" for fid, a in acked]
        lines.append("")

    lines += ["## Checks run", ""] + ([f"- {c}" for c in result.checks] or ["- none"]) + [""]
    slow = sorted(((s, part) for part, s in result.timings.items() if s >= 1), reverse=True)
    if slow:
        lines += ["Time: " + ", ".join(f"{part} {s:.0f}s" for s, part in slow), ""]
    lines += ["## Not checked", ""] + ([f"- {c}" for c in result.not_checked] or ["- nothing left out"]) + [""]
    for m in session.moves:
        what = f"{m['commits']} commit(s) that arrived during the session (up to {m['head'][:8]})" if m["commits"] else \
            f"The branch that was checked out during the session ({m['head'][:8]})"
        lines += [f"Note: {what} "
                  + ("are part of the starting point, not of the change." if m["ok"] else
                     f"couldn't be separated from the change ({m['why']}), so they count as part of it."), ""]
    if session.baseline_source not in ("session-start", "ref"):
        lines += [f"Note: {_baseline_note(session)}", ""]
    if session.baseline_source != "ref":
        lines.append("Edits you made yourself during this session count as part of the change.")
    if result.advice:
        lines += ["", "## Advice (not enforced)", ""] + [f"- {a}" for a in result.advice]
    return "\n".join(lines) + "\n"


BROKEN = {"test-regression", "new-test-failing", "test-run-broken", "test-hangs", "test-failing", "tests-failing-unverified"}
EXPECTATIONS = {"test-changed-to-pass", "test-removed", "test-disabled", "failing-test-changed", "assertions-removed"}
ALSO = {"suppression-added": "suppression comment", "lint-new": "lint", "type-error-new": "type error", "undone-work": "undone earlier work",
        "test-vacuous": "no new test fails without the change", "test-not-collected": "new test never collected",
        "source-detects-tests": "source checks whether it's tested", "always-equal": "an __eq__ that's always true"}
MAX_BRIEF = 5


def evidence(result: EngineResult) -> str:
    """The checks in one line: what a reader needs to know how far to trust the verdict."""
    f = result.facts
    if not f.get("ran"):
        return "; ".join(result.checks[:2])
    parts = [f"{f['passed']} of {f['ran']} tests pass" if f["passed"] != f["ran"] else f"{f['ran']} tests pass"]
    parts[0] += " (the whole suite)" if f.get("whole") else " (the ones this change reaches)"
    if f.get("earlier"):
        parts.append(f"{f['as_written']} of {f['earlier']} earlier tests in edited files pass as they were")
    if f.get("new"):
        parts.append(f"{f['new_tell']} of {f['new']} new or edited tests fail without the change")
    if f.get("lines_checked") and not any(x.rule == "untested-change" for x in result.findings):
        parts.append("every changed line ran")
    return "; ".join(parts)


def brief(session: store.Session, result: EngineResult, standing: list[Finding]) -> list[str]:
    """What the user should look at, in a few lines: what broke, what tests no longer say, what never ran."""
    from notyet.engines.crosscheck import balanced
    from notyet.gate import HANDED_OVER, handed_over_now
    open_ids = {f.id for f in standing}

    def shown(f: Finding) -> bool:
        ack = find_ack(f, session.acks)
        return f.severity != "note" and (f.id in open_ids or handed_over_now(session, ack))

    live = [f for f in result.findings if shown(f)]
    out: list[str] = []
    if broken := [f for f in live if f.rule in BROKEN]:
        out.append("Broken:")
        out += [f"  {f.title}" for f in broken[:MAX_BRIEF]] + _more(broken)
    # a test that checks less is the same news as one whose expectations changed
    changed = {t for f in live if f.rule in EXPECTATIONS - {"assertions-removed"} for t in f.tests or [f.location]}
    tests = [f for f in live if f.rule in EXPECTATIONS and (f.severity == "block" or f.rule != "test-changed-to-pass")
             and not (f.rule == "assertions-removed" and f.location in changed)]
    if tests:
        out.append("What the tests say has changed:")
        said = ""
        for f in tests[:MAX_BRIEF]:
            ack = find_ack(f, session.acks) or {}
            out.append(f"  {_headline(f)}")
            out += [f"    {line}" for line in (f.diff if any(x.startswith("~ test") or x.startswith("~ Test") for x in f.diff)
                                               else balanced(f.diff, 2))[:9]]
            reason = re.sub(r"[*`_]{2,}|^\W+", "", ack.get("reason", "")).strip()
            if reason and reason != said:
                out.append(f"    agent: {reason[:200]}")
                said = reason
        out += _more(tests)
    if untested := [f for f in live if f.rule == "untested-change"]:
        out.append("No test runs:")
        out += [f"  {f.title.split(' never ran')[0]}: {(f.evidence or [''])[0]}" for f in untested[:MAX_BRIEF]] + _more(untested)
    rest = [f for f in live if f not in broken and f not in tests and f not in untested
            and not (f.rule == "assertions-removed" and f.location in changed)]
    counts: dict[str, int] = {}
    for f in rest:
        label = ALSO.get(f.rule, "edited test(s) that can't be rerun as they were (names or signatures moved)"
                         if f.rule == "test-changed-to-pass" else f.rule)
        counts[label] = counts.get(label, 0) + 1
    earlier = sum(1 for f in result.findings if f.severity != "note" and (a := find_ack(f, session.acks))
                  and a.get("category") in HANDED_OVER and not handed_over_now(session, a))
    also = [f"{label}" + (f" ×{n}" if n > 1 else "") for label, n in counts.items()]
    also += [f"{earlier} item(s) shown to you in an earlier turn still stand"] if earlier else []
    if also:
        out.append("Also: " + "; ".join(also))
    return out


def _more(items: list[Finding]) -> list[str]:
    return [f"  and {len(items) - MAX_BRIEF} more"] if len(items) > MAX_BRIEF else []


def _headline(f: Finding) -> str:
    what = {"test-removed": "is gone", "test-disabled": "is switched off", "assertions-removed": "checks less than it did",
            "failing-test-changed": "was failing; it was changed, and still fails as it was"}.get(f.rule)
    return f"{f.location} {what}" if what and len(f.tests) <= 1 else f.location if len(f.tests) <= 1 and not what else f.title


def summary(verdict: str, session: store.Session, result: EngineResult, standing: list[Finding], path: str) -> str:
    head = f"notyet: {VERDICT_TEXT.get(verdict, verdict)}"
    if result.gaps:     # never let "didn't check" read as "checked"
        head += f"{' (incomplete)' if verdict != 'incomplete' else ''}. {'; '.join(result.gaps)}"
    detail = evidence(result) if result.checks else "; ".join(result.not_checked[:2]) or "no checks ran"
    lines = brief(session, result, standing)
    return "\n".join([f"{head}. {detail}", *(f"  {line}" for line in lines), f"  Receipt: {path}"])


def agent_message(standing: list[Finding], last_chance: bool, advice: Sequence[str] = ()) -> str:
    cmd = invocation()
    lines = [f"notyet: not done yet. {len(standing)} item(s) to resolve before stopping.", ""]
    for i, f in enumerate(standing[:MAX_AGENT_FINDINGS], 1):
        lines.append(f"{i}. [{f.severity}] {f.title}" + _where(f) + f"  id={f.id}")
        lines += [f"   {e}" for e in f.evidence[:3]]
        if f.action:
            lines.append(f"   -> {f.action}")
    if len(standing) > MAX_AGENT_FINDINGS:
        lines.append(f"... and {len(standing) - MAX_AGENT_FINDINGS} more (see the receipt).")
    lines += [
        "",
        "Fix these, then finish. If you changed a test on purpose, there's nothing to restore and no need to keep old test "
        "setup working: name the test and say why in your closing message to the user. If another item is intended and you're sure, acknowledge it with a specific "
        f"reason, which the user will see: `{cmd} ack <id> \"<reason>\"`. Items marked [block] can only be "
        f"fixed or handed to the user: `{cmd} ack <id> --needs-human \"<reason>\"`. Several ids sharing one "
        f"reason can go in one call, comma-separated.",
    ]
    if advice:
        lines += ["", "Also worth a look (from history; not required):"] + [f"- {a}" for a in advice[:3]]
    if last_chance:
        lines.append("This is the last automatic check for these items: anything still open will be "
                     "reported to the user as unresolved.")
    text = "\n".join(lines)
    return text if len(text) <= MAX_AGENT_CHARS else text[:MAX_AGENT_CHARS] + "\n... (truncated; see the receipt)"


def _ack_state(session: store.Session, f: Finding) -> str:
    ack = find_ack(f, session.acks)
    if not ack:
        return "resolved"
    if f.id not in session.acks:
        changed = uncovered(f, ack)
        since = f", {changed} line(s) new or changed since" if changed else ""
        return f"acknowledged earlier{since} ({ack.get('category', 'acknowledged')}): {ack.get('reason', '')}"
    return f"{ack.get('category', 'acknowledged')}: {ack.get('reason', '')}"


def _baseline_label(session: store.Session) -> str:
    return {"session-start": "the tree at session start", "first-prompt": "the tree at the first prompt",
            "head": "HEAD (the session's start wasn't recorded)",
            "ref": "the base it was given"}.get(session.baseline_source, session.baseline_source)


def _baseline_note(session: store.Session) -> str:
    if session.baseline_source == "head":
        return ("notyet was installed mid-session, so the change is measured from HEAD and may include "
                "edits made before this session.")
    return "the change is measured from the first prompt notyet saw."


def _safe(s: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in s)[:60]
