"""Run the current gate on the trees of recorded dogfooding sessions (no agent, free): what would it say now?

  python eval/notyet_recheck.py WORKDIR [WORKDIR ...]      # session copies left by notyet_dogfood.py

Each recorded check is repeated in a copy: same starting point, same turns so far, same tree, the
acknowledgments that existed then, and what the agent had just told the user (from Claude Code's transcript).
--show prints what the user would see.
"""
import argparse
import dataclasses
import datetime
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from notyet import gate, snapshot, store
from notyet.findings import Finding


def closing_message(session_id: str, at: float) -> str:
    """The last thing the agent said before `at`."""
    said = ""
    for path in (Path.home() / ".claude/projects").glob(f"*/{session_id}.jsonl"):
        for line in path.open():
            e = json.loads(line)
            if e.get("type") != "assistant":
                continue
            when = datetime.datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00")).timestamp()
            text = " ".join(c.get("text", "") for c in e["message"].get("content", []) if c.get("type") == "text").strip()
            if text and when <= at:
                said = text
    return said


def recheck(work: Path) -> list[tuple]:
    rows = []
    for session in store.all_sessions(str(work)):
        with tempfile.TemporaryDirectory(prefix="notyet-recheck-") as tmp:
            copy = Path(tmp).resolve() / work.name
            shutil.copytree(work, copy, symlinks=True)
            for n, run in enumerate(session.runs):
                snapshot.git(str(copy), "read-tree", run.tree)
                snapshot.git(str(copy), "checkout-index", "--all", "--force")
                subprocess.run(["git", "-C", str(copy), "clean", "-qfd", "-e", ".venv", "-e", ".claude"], check=True)
                turns = [t for t in session.turns if t["at"] <= run.at]
                acks = {k: a for k, a in session.acks.items() if a["at"] <= run.at}
                then = dataclasses.replace(session, session_id=f"recheck-{n}", runs=session.runs[:n], turns=turns, acks=acks)
                then.runs = [dataclasses.replace(r) for r in then.runs]
                then.consecutive_blocks = 0
                decision = gate.check(str(copy), then, message=closing_message(session.session_id, run.at - run.seconds))
                old = [Finding(**f) for f in run.result["findings"] if f["severity"] != "note"]
                new = [f for f in decision.findings if f.severity != "note"]
                rows.append((work.name, n + 1, run.verdict, decision.verdict, old, new, decision.summary))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("work", nargs="+")
    ap.add_argument("--show", action="store_true")
    args = ap.parse_args()
    for work in args.work:
        for name, n, before, after, old, new, summary in recheck(Path(work).resolve()):
            if args.show:
                print(f"\n== {name} check {n}: {before} -> {after}\n{summary}")
                continue
            print(f"== {name} check {n}: {before} -> {after}; {len(old)} -> {len(new)} item(s)")
            kept = {(f.rule, f.severity, f.title) for f in new}
            for f in old:
                if (f.rule, f.severity, f.title) not in kept:
                    print(f"   - {f.severity:7} {f.rule}: {f.title[:130]}")
            was = {(f.rule, f.severity, f.title) for f in old}
            for f in new:
                print(f"   {'  ' if (f.rule, f.severity, f.title) in was else '+ '}{f.severity:7} {f.rule}: {f.title[:130]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
