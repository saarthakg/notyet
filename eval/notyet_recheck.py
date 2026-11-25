"""Run the current gate on the trees of recorded dogfooding sessions (no agent, free): what would it say now?

  python eval/notyet_recheck.py WORKDIR [WORKDIR ...]      # session copies left by notyet_dogfood.py

Each recorded check is repeated in a copy: same starting point, same turns so far, same tree, the
acknowledgments that existed then.
"""
import argparse
import dataclasses
import shutil
import subprocess
import tempfile
from pathlib import Path

from notyet import gate, snapshot, store
from notyet.findings import Finding


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
                decision = gate.check(str(copy), then)
                old = [Finding(**f) for f in run.result["findings"] if f["severity"] != "note"]
                new = [f for f in decision.findings if f.severity != "note"]
                rows.append((work.name, n + 1, run.verdict, decision.verdict, old, new))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("work", nargs="+")
    for work in ap.parse_args().work:
        for name, n, before, after, old, new in recheck(Path(work).resolve()):
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
