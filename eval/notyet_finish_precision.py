"""How often do the unfinished-work checks fire on commits humans wrote and maintainers merged?

Each first-parent, non-merge commit that changed Python source is treated as a session: baseline = its parent's tree,
working tree = the commit's. Only the unfinished-work engine runs (engines/finish.py), straight on the git trees: no
checkout, no tests, so hundreds of commits take seconds. Merged work was reviewed, so most hits here are false
alarms; a TODO a maintainer chose to merge is real but deliberate. --show prints each hit.

  python eval/notyet_finish_precision.py OUT.json REPO [REPO ...] [--commits N] [--show]

Read-only: the repos aren't touched.
"""
import argparse
import collections
import json
import subprocess
import sys
from pathlib import Path

from notyet import config, snapshot, store
from notyet.engines import finish
from notyet.findings import Context


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout


def commits(root: Path, limit: int) -> list[str]:
    out = git(root, "log", "--first-parent", "--no-merges", "--format=@%H", "--name-only", "-n", str(limit * 6))
    picked, sha, files = [], None, []
    for line in out.splitlines() + ["@"]:
        if line.startswith("@"):
            if sha and any(f.endswith(".py") and not snapshot_is_test(f) for f in files) and len(files) <= 30:
                picked.append(sha)
            sha, files = line[1:], []
        elif line.strip():
            files.append(line.strip())
    return picked[:limit]


def snapshot_is_test(path: str) -> bool:
    from notyet.layout import is_test_side
    return is_test_side(path)


def run(root: Path, sha: str) -> list[dict]:
    old = git(root, "rev-parse", f"{sha}^^{{tree}}").strip()
    new = git(root, "rev-parse", f"{sha}^{{tree}}").strip()
    session = store.Session(session_id="precision", started=0, baseline_tree=old, baseline_head=old,
                            baseline_source="session-start")
    ctx = Context(root=str(root), session=session, config=config.load(str(root), old), baseline_tree=old,
                  current_tree=new, deltas=snapshot.diff_trees(str(root), old, new))
    return [{"rule": f.rule, "title": f.title, "evidence": f.evidence[:4]} for f in finish.run(ctx).findings]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("repos", nargs="+")
    ap.add_argument("--commits", type=int, default=100)
    ap.add_argument("--show", action="store_true")
    args = ap.parse_args()
    rows = []
    for repo in map(Path, args.repos):
        for sha in commits(repo, args.commits):
            hits = run(repo, sha)
            subject = git(repo, "log", "-1", "--format=%s", sha).strip()[:90]
            rows.append({"repo": repo.name, "sha": sha[:10], "subject": subject, "findings": hits})
            if args.show and hits:
                print(f"== {repo.name} {sha[:10]} {subject}")
                for h in hits:
                    print(f"  [{h['rule']}] {h['title']}")
                    for e in h["evidence"]:
                        print(f"      {e}")
    Path(args.out).write_text(json.dumps(rows, indent=1))
    by_rule = collections.Counter(h["rule"] for r in rows for h in r["findings"])
    with_any = sum(1 for r in rows if r["findings"])
    print(f"\n{len(rows)} commits, {with_any} with a finding")
    for rule, n in by_rule.most_common():
        print(f"  {rule}: {n} finding(s) in {sum(1 for r in rows if any(h['rule'] == rule for h in r['findings']))} commit(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
