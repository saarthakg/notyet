"""Feature work on real projects with notyet installed, as a user would have it.

A session spec (JSON) is a list of {"id", "repo", "mode", "prompts": [...]}. Each session runs in a
fresh copy of BENCH/<repo> (a clone with its .venv, set up by `notyet init` and `notyet install claude`),
one `claude -p` turn per prompt, resumed; the agent can edit and run Python and git. After each turn:
what notyet said (runs, findings, acks, receipt), and, independently, the suite as the agent left it and
the session-start tests on the agent's code.

  python eval/notyet_dogfood.py SPEC.json OUT.json [--only ID,...] [--model M] [--jobs 2]

Runs on the subscription (quota); each turn is capped at $4 API-equivalent.
"""
import argparse
import json
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import notyet_sessions as ns  # noqa: E402

from notyet import testrun  # noqa: E402
from notyet.engines.execution import source_path_env  # noqa: E402

TOOLS = ["Read", "Edit", "Write", "Glob", "Grep", "Bash(.venv/bin/python:*)", "Bash(git diff:*)", "Bash(git status:*)",
         "Bash(git log:*)", "Bash(git show:*)", "Bash(git mv:*)", "Bash(git rm:*)", "Bash(ls:*)", "Bash(wc:*)", "Bash(grep:*)",
         "Bash(PYTHONPATH=src .venv/bin/python:*)", f"Bash({ns.NOTYET_PY} -m notyet ack:*)"]


def suite(work: Path) -> dict[str, str]:
    run = testrun.run_pytest(f"{work}/.venv/bin/python -m pytest", str(work), [], timeout=300, env=source_path_env(str(work)))
    return {n: r.outcome for n, r in run.results.items()}


def original_tests(work: Path) -> dict[str, str]:
    """The session-start tests directory on the code as it is now, in a copy."""
    copy = work.parent / f"{work.name}-truth"
    shutil.rmtree(copy, ignore_errors=True)
    shutil.copytree(work, copy, symlinks=True, ignore=shutil.ignore_patterns(".venv", ".claude"))
    (copy / ".venv").symlink_to(work / ".venv")
    shutil.rmtree(copy / "tests")
    ns.git(copy, "checkout", "-q", "notyet-start", "--", "tests")
    out = suite(copy)
    shutil.rmtree(copy, ignore_errors=True)
    return out


def play(spec: dict, root: Path, model: str) -> dict:
    work = root / spec["id"]
    shutil.rmtree(work, ignore_errors=True)
    shutil.copytree(root / spec["repo"], work, symlinks=True)
    config = work / ".notyet.toml"
    config.write_text(config.read_text().replace('mode = "report"', f'mode = "{spec["mode"]}"'))
    with open(work / ".git/info/exclude", "a") as f:
        f.write("\n.claude/\n.venv/\n")
    ns.git(work, "add", "-A")
    ns.git(work, "commit", "-qm", "notyet")
    ns.git(work, "tag", "-f", "notyet-start")
    subprocess.run(["rm", "-rf", str(work / ".git/notyet")], check=True)
    before = suite(work)
    row = {"id": spec["id"], "repo": spec["repo"], "mode": spec["mode"], "tests_before": len(before), "turns": []}
    session_id, seen_runs, seen_stops = None, 0, 0
    for prompt in spec["prompts"]:
        cmd = ["claude", "-p", prompt, *(["--resume", session_id] if session_id else []), "--output-format", "json",
               "--permission-mode", "acceptEdits", "--setting-sources", "project,local", "--strict-mcp-config",
               "--max-budget-usd", "4", *(["--model", model] if model else []), "--allowedTools", *TOOLS]
        start = time.monotonic()
        try:
            proc = ns.sh(cmd, cwd=work, timeout=2400, check=False)
            agent = json.loads(proc.stdout) if proc.stdout.strip().startswith("{") else {"result": proc.stdout[-800:] + proc.stderr[-800:]}
        except subprocess.TimeoutExpired:
            agent = {"result": "timed out"}
        seconds = time.monotonic() - start
        session_id = session_id or agent.get("session_id")
        view = ns.notyet_view(work, session_id, seen_runs, seen_stops)
        seen_runs, seen_stops = view["total_runs"], view["total_stops"]
        session = next((s for s in ns.store.all_sessions(str(work)) if s.session_id == session_id), None)
        for run, stored in zip(view["runs"], session.runs[-len(view["runs"]):] if session and view["runs"] else []):
            run.update(seconds=stored.seconds, timings=stored.result.get("timings", {}), gaps=stored.result.get("gaps", []),
                       advice=stored.result.get("advice", []), receipt=Path(stored.receipt).read_text() if stored.receipt else "")
        now, truth = suite(work), original_tests(work)
        turn = {"prompt": prompt, "seconds": round(seconds), "cost": agent.get("total_cost_usd"), "agent_turns": agent.get("num_turns"),
                "says": (agent.get("result") or "")[:6000], "notyet": view,
                "diffstat": ns.git(work, "diff", "--stat", "notyet-start"),
                "suite": {o: list(now.values()).count(o) for o in set(now.values())},
                "failing": sorted(n for n, o in now.items() if o in ("failed", "error"))[:20],
                "original_tests_not_passing": sorted(n for n, o in before.items() if o == "passed" and truth.get(n) != "passed")[:60]}
        row["turns"].append(turn)
        print(f"{spec['id']:12} {spec['mode']:8} turn {len(row['turns'])}: {seconds:4.0f}s ${turn['cost'] or 0:.2f} suite={turn['suite']} "
              f"original-not-passing={len(turn['original_tests_not_passing'])} "
              f"notyet={[(r['verdict'], round(r.get('seconds', 0))) for r in view['runs']]} stops={view['stops']}", file=sys.stderr, flush=True)
    return row


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("spec")
    ap.add_argument("out")
    ap.add_argument("--only", default="")
    ap.add_argument("--model", default="")
    ap.add_argument("--jobs", type=int, default=2)
    ap.add_argument("--root", default=str(ns.BENCH / "dogfood"))
    args = ap.parse_args()
    specs = [s for s in json.loads(Path(args.spec).read_text()) if not args.only or s["id"] in args.only.split(",")]
    out = Path(args.out)
    rows = json.loads(out.read_text()) if out.exists() else []
    with ThreadPoolExecutor(args.jobs) as pool:
        for row in pool.map(lambda s: play(s, Path(args.root).resolve(), args.model), [s for s in specs if s["id"] not in {r["id"] for r in rows}]):
            rows.append(row)
            out.write_text(json.dumps(rows, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
