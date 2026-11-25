"""What the gate's findings led to, over every session in this repo.

A finding's outcome, per session, from the checks that followed:
  fixed         open at one check, gone at a later one, with no reason recorded
  justified     the agent gave a reason
  handed over   the agent handed it to the user
  open          still open at the session's last check
"""
import statistics
from collections import Counter, defaultdict

from notyet import store
from notyet.findings import Finding, find_ack

OUTCOMES = ("fixed", "justified", "handed over", "open")


def outcomes(session: store.Session) -> dict[str, tuple[str, str]]:
    """{finding id: (rule, outcome)} for the findings that were ever open."""
    seen: dict[str, Finding] = {}
    for run in session.runs:
        for data in run.result.get("findings", []):
            f = Finding(**data)
            if f.severity != "note":
                seen[f.id] = f
    last = set(session.runs[-1].finding_ids) if session.runs else set()
    present = {Finding(**d).id for d in session.runs[-1].result.get("findings", [])} if session.runs else set()
    out = {}
    for fid, f in seen.items():
        ack = find_ack(f, session.acks)
        if ack:
            kind = "handed over" if ack.get("category") == "needs-human" else "justified"
        else:
            kind = "open" if fid in last or fid in present else "fixed"
        out[fid] = (f.rule, kind)
    return out


def render(sessions: list[store.Session]) -> str:
    sessions = [s for s in sessions if s.baseline_source != "ref"]
    runs = [r for s in sessions for r in s.runs]
    if not runs:
        return "No checks recorded yet."
    stops = [st for s in sessions for st in s.stops]
    verdicts = Counter(r.verdict for r in runs)
    lines = [f"{len(sessions)} session(s), {len(runs)} check(s): " + ", ".join(f"{n} {v}" for v, n in verdicts.most_common())]
    if stops:
        lines.append(f"{len(stops)} stop(s); {sum(1 for st in stops if st.get('busy'))} not checked because work was still running")
    timed = sorted(r.seconds for r in runs if r.seconds)
    if timed:
        lines.append(f"check time: median {statistics.median(timed):.0f}s, longest {timed[-1]:.0f}s")
    table: dict[str, Counter] = defaultdict(Counter)
    for s in sessions:
        for rule, kind in outcomes(s).values():
            table[rule][kind] += 1
    if table:
        lines += ["", f"{'rule':<26}" + "".join(f"{o:>13}" for o in OUTCOMES)]
        for rule, counts in sorted(table.items(), key=lambda kv: -sum(kv[1].values())):
            lines.append(f"{rule:<26}" + "".join(f"{counts[o] or '-':>13}" for o in OUTCOMES))
        lines += ["", "A rule that is nearly always justified is noise here: set it to \"note\" or \"off\" under [rules] in .notyet.toml."]
    return "\n".join(lines)
