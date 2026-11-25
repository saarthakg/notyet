"""Regression recall on real repos: the repo's own full suite is the ground truth.

Each mutant is one small, random source change (a flipped comparison, a negated
condition, a dropped call, an off-by-one, a swapped boolean, a nulled return).
For each one:
  truth   run the whole suite; the tests that newly fail are what it broke;
  gate    run notyet's check, as at the end of a session that made that change.

  caught        the suite breaks and the gate blocks with a regression
  missed        the suite breaks and the gate has no test finding
                  (why: not selected | out of time | other)
  false block   the suite is clean and the gate blocks
  agree-clean   the suite is clean and the gate doesn't block

  python eval/notyet_recall.py OUT.json REPO [REPO ...] [--mutants N] [--seed S]

Edits the repos in place and restores them: throwaway clones only.
"""
import argparse
import ast
import json
import random
import statistics
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from notyet_latency import source_modules  # noqa: E402

from notyet import gate, snapshot, store, testrun  # noqa: E402
from notyet.engines.execution import source_path_env  # noqa: E402

SWAP = {"==": "!=", "!=": "==", "<": ">=", ">": "<=", "<=": ">", ">=": "<", " is not ": " is ", " is ": " is not ",
        " not in ": " in ", " in ": " not in "}
OPS = {ast.Eq: "==", ast.NotEq: "!=", ast.Lt: "<", ast.Gt: ">", ast.LtE: "<=", ast.GtE: ">=", ast.IsNot: " is not ",
       ast.Is: " is ", ast.NotIn: " not in ", ast.In: " in "}


def mutants(text: str) -> list[tuple[str, int, str]]:
    """[(operator, line, mutated source)], one edit each."""
    tree = ast.parse(text)
    lines = text.splitlines(keepends=True)
    out = []

    def edit(node, new, op):
        if node.lineno != node.end_lineno:
            return
        raw = lines[node.lineno - 1].encode()
        changed = raw[:node.col_offset] + new.encode() + raw[node.end_col_offset:]
        mutated = "".join(lines[:node.lineno - 1] + [changed.decode()] + lines[node.lineno:])
        try:
            ast.parse(mutated)
        except SyntaxError:
            return
        if mutated != text:
            out.append((op, node.lineno, mutated))

    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(func):
            src = ast.get_source_segment(text, node) if hasattr(node, "lineno") else None
            if src is None:
                continue
            if isinstance(node, ast.Compare) and len(node.ops) == 1:
                a, b = ast.get_source_segment(text, node.left), ast.get_source_segment(text, node.comparators[0])
                sym = OPS.get(type(node.ops[0]))
                if a and b and sym:
                    edit(node, f"{a} {SWAP[sym].strip()} {b}", "compare")
            elif isinstance(node, ast.If):
                t = ast.get_source_segment(text, node.test)
                if t:
                    edit(node.test, f"not ({t})", "negate-if")
            elif isinstance(node, ast.Constant) and type(node.value) is int and node.value < 1000:
                edit(node, str(node.value + 1), "off-by-one")
            elif isinstance(node, ast.Constant) and isinstance(node.value, bool):
                edit(node, str(not node.value), "flip-bool")
            elif isinstance(node, ast.Return) and node.value is not None:
                edit(node, "return None", "return-none")
            elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                edit(node, "pass", "drop-call")
    return out


def full_suite(root: Path) -> dict:
    run = testrun.run_pytest(f"{root}/.venv/bin/python -m pytest", str(root), [], timeout=600, env=source_path_env(str(root)))
    return run.results


def one_repo(repo: str, n: int, seed: int, budget: int) -> dict:
    root = Path(repo).resolve()
    rng = random.Random(seed)
    (root / ".notyet.toml").write_text(f'[test]\ncommand = ".venv/bin/python -m pytest"\nbudget_seconds = {budget}\n'
                                       f'[gate]\nmode = "enforce"\n')
    rows = []
    try:
        baseline = snapshot.snapshot(str(root))
        t = time.monotonic()
        clean1 = full_suite(root)
        suite_seconds = time.monotonic() - t
        clean2 = full_suite(root)
        stable = {k for k in clean1 if clean1[k].outcome == "passed" and k in clean2 and clean2[k].outcome == "passed"}
        pool = []
        for rel in source_modules(root):
            try:
                pool += [(rel, *m) for m in mutants((root / rel).read_text())]
            except SyntaxError:
                continue
        # stratify by file, so a few big modules don't dominate
        by_file: dict[str, list] = {}
        for m in pool:
            by_file.setdefault(m[0], []).append(m)
        picks = []
        files = sorted(by_file)
        while len(picks) < n and files:
            rel = rng.choice(files)
            options = by_file[rel]
            picks.append(options.pop(rng.randrange(len(options))))
            if not options:
                files.remove(rel)
        for i, (rel, op, line, mutated) in enumerate(picks):
            path = root / rel
            original = path.read_text()
            path.write_text(mutated)
            try:
                after = full_suite(root)
                broken = sorted(t for t in stable if t not in after or after[t].bad)
                session = store.Session(session_id=f"recall-{i}-{time.time_ns()}", started=time.time(), baseline_tree=baseline,
                                        baseline_head=snapshot.head_tree(str(root)), baseline_source="session-start")
                start = time.monotonic()
                decision = gate.check(str(root), session)
                seconds = time.monotonic() - start
            finally:
                path.write_text(original)
            res = session.runs[-1].result if session.runs else {}
            found = [f for f in decision.findings if f.rule in ("test-regression", "test-run-broken", "test-failing",
                                                                "tests-failing-unverified")]
            blocked = any(f.severity == "block" for f in found)
            row = {"repo": root.name, "file": rel, "op": op, "line": line, "broken": len(broken), "broken_sample": broken[:3],
                   "broken_files": sorted({t.split("::")[0] for t in broken}), "seconds": round(seconds, 1),
                   "verdict": decision.verdict, "flagged": bool(found), "blocked": blocked,
                   "rules": sorted({f.rule for f in decision.findings}), "checks": res.get("checks", [])[:1],
                   "gaps": res.get("gaps", []), "not_checked": res.get("not_checked", [])}
            row["outcome"] = ("caught" if blocked else "flagged" if found else "missed") if broken else \
                             ("false-block" if blocked else "clean")
            rows.append(row)
            print(f"{root.name:8} {i:3} {row['outcome']:11} broke {len(broken):4} in {len(row['broken_files']):2} file(s) "
                  f"{seconds:5.1f}s {op:12} {rel}:{line}", file=sys.stderr, flush=True)
    finally:
        (root / ".notyet.toml").unlink(missing_ok=True)
        snapshot.git(str(root), "checkout", "-q", "--", ".")
    return {"suite_seconds": round(suite_seconds, 1), "stable_tests": len(stable), "rows": rows}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("repos", nargs="+")
    ap.add_argument("--mutants", type=int, default=30)
    ap.add_argument("--seed", type=int, default=3)
    ap.add_argument("--budget", type=int, default=90)
    args = ap.parse_args()
    with ProcessPoolExecutor(len(args.repos)) as pool:
        reports = list(pool.map(one_repo, args.repos, *([v] * len(args.repos) for v in (args.mutants, args.seed, args.budget))))
    report = {Path(r).name: rep for r, rep in zip(args.repos, reports)}
    Path(args.out).write_text(json.dumps(report, indent=1))
    print("repo       suite   mutants  break-suite  caught  flagged  missed  false-block   gate p50 / p95")
    for name, rep in report.items():
        rows = rep["rows"]
        n = {k: sum(r["outcome"] == k for r in rows) for k in ("caught", "flagged", "missed", "false-block", "clean")}
        secs = sorted(r["seconds"] for r in rows)
        print(f"{name:9} {rep['suite_seconds']:5}s  {len(rows):7}  {n['caught'] + n['flagged'] + n['missed']:11}  {n['caught']:6}  "
              f"{n['flagged']:7}  {n['missed']:6}  {n['false-block']:11}   {statistics.median(secs):.1f}s / {secs[int(.95 * (len(secs) - 1))]:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
