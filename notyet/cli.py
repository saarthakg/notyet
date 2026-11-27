"""notyet command line.

  notyet init                 write .notyet.toml (detects the test command; asks first)
  notyet install claude       add notyet's hooks to Claude Code's settings (shows the diff; asks first)
  notyet install codex        the same for Codex CLI (.codex/hooks.json)
  notyet status               the current session and its last check
  notyet receipt              print the latest receipt
  notyet check [--base REF]   run the gate now, outside a hook (by hand, or in CI)
  notyet stats                what findings led to: fixed, justified, handed over
  notyet export [--out F]     every finding from every session as CSV, for hand-labeling
  notyet ack ID[,ID] "reason" acknowledge findings (what agents run when blocked)
  notyet hook AGENT EVENT     hook entry point (used by the installed hooks)
"""
import argparse
import os
import re
import sys
import time
from pathlib import Path

from notyet import config as config_mod
from notyet import gate, snapshot, store
from notyet.findings import Finding, find_ack, group


def _root(path: str = ".") -> str:
    try:
        return snapshot.repo_root(path)
    except snapshot.GitError:
        print(f"{Path(path).resolve()} is not inside a git repository.", file=sys.stderr)
        raise SystemExit(2)


def _confirm(prompt: str, yes: bool) -> bool:
    if yes:
        return True
    if not sys.stdin.isatty():
        print("Not changing anything without confirmation; re-run with --yes.", file=sys.stderr)
        return False
    return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")


def cmd_init(args) -> int:
    root = _root(args.path)
    path = Path(root) / config_mod.CONFIG_FILE
    if path.exists() and not args.force:
        print(f"{path} already exists (use --force to overwrite).")
        return 1
    command = args.test_command or config_mod.detect_test_command(root)
    if not command:
        print("Couldn't find a pytest setup. Pass --test-command, e.g. --test-command 'python -m pytest'.")
        return 1
    text = config_mod.render_template(root, command)
    print(f"Will write {path}:\n\n{text}")
    if not _confirm("Write it?", args.yes):
        return 1
    path.write_text(text)
    print(_try_command(root, command))
    if os.environ.get("CLAUDE_PLUGIN_ROOT"):      # run by the plugin's /notyet:init: its hooks are already there
        print(f"Wrote {path}. The plugin's hooks check from the next prompt on, in report mode.")
    else:
        print(f"Wrote {path}. Review the test command, then run `notyet install claude` (or `notyet install codex`).")
    return 0


def _try_command(root: str, command: str) -> str:
    """Does the test command work at all? Better found out now than at the end of a session."""
    from notyet import testrun
    from notyet.engines.execution import source_path_env
    from notyet.pyresolve import find_python_source_roots
    found = testrun.collect(testrun.anchored(command, root), root, [], timeout=60, env=source_path_env(root))
    if found is None:
        return f"Warning: `{command} --collect-only` didn't work here. Fix the command in {config_mod.CONFIG_FILE} before relying on the gate."
    broken = f"; {len(found.errors)} file(s) can't be collected: {', '.join(sorted(found.errors)[:3])}" if found.errors else ""
    out = (f"The test command works: it finds {len(found)} test(s){broken}." if found else
           f"The test command runs but finds no tests{broken}. Check it in {config_mod.CONFIG_FILE}.")
    # notyet puts the source roots on PYTHONPATH itself; you and the agent, running the same command, don't
    plain = testrun.collect(testrun.anchored(command, root), root, [], timeout=60, env=dict(os.environ))
    if found and (plain is None or len(plain.errors) > len(found.errors)):
        roots = [os.path.relpath(r, root) for r in find_python_source_roots(root) if os.path.relpath(r, root) != "."]
        hint = f'`pythonpath = ["{roots[0]}"]` under [tool.pytest.ini_options]' if roots else "the package installed"
        out += (f"\nWarning: run by hand, the same command can't import the code (notyet sets PYTHONPATH for its own runs). "
                f"You and your agent will see import errors until the repo has {hint}, or `pip install -e .`.")
    return out


def cmd_install(args) -> int:
    from notyet.hooks import claude, codex
    root = _root(args.path)
    host = {"claude": claude, "codex": codex}[args.agent]
    cfg = config_mod.load(root)
    if not cfg.test_command:
        print("Note: no test command configured yet; the gate will report 'not checked' until you run `notyet init`.")

    def confirm(diff: str) -> bool:
        print(diff)
        return _confirm(f"Apply this change to {host.NAME}'s settings?", args.yes)

    path, changed = host.install(root, args.scope, stop_timeout=cfg.hook_timeout, confirm=confirm)
    print(f"Installed notyet's hooks in {path}." if changed else f"No change made to {path}.")
    if changed:
        print(f"Mode: {cfg.mode}. The gate takes effect in {host.NAME} sessions started from now on.")
        if host.AFTER_INSTALL:
            print(host.AFTER_INSTALL)
    return 0


def cmd_status(args) -> int:
    root = _root(args.path)
    session = store.latest_session(root)
    if session is None:
        print("No sessions yet.")
        return 0
    cfg = config_mod.load(root, session.baseline_tree)
    print(f"Session {session.session_id} · started {time.strftime('%Y-%m-%d %H:%M', time.localtime(session.started))} "
          f"· baseline: {session.baseline_source} · mode: {cfg.mode}")
    if session.stops:
        skipped = sum(1 for st in session.stops if st.get("busy"))
        print(f"Stop hook fired {len(session.stops)} time(s); {skipped} skipped because background tasks were running")
    checked = session if session.runs else _last_checked(root)
    if checked is None:
        print("No checks run yet.")
        return 0
    last = checked.runs[-1]
    if checked is not session:
        print(f"No checks in this session yet. The last check was in session {checked.session_id}:")
    print(f"Last check: {last.verdict} · {len(last.finding_ids)} open finding(s) · receipt: {last.receipt}")
    text = Path(last.receipt).read_text() if last.receipt and Path(last.receipt).exists() else ""
    short = re.search(r"^## In short\n+```\n(.*?)```", text, re.MULTILINE | re.DOTALL)
    if short:
        print("\n" + short.group(1).rstrip())
    open_items = re.findall(r"^- \[(\w+)\] `([0-9a-f]+)` (.*): open$", text, re.MULTILINE)
    if open_items:
        print("\nOpen items (accept one with `notyet ack <id> <reason>`):")
        for severity, fid, title in open_items:
            print(f"  {fid} [{severity}] {title}")
    return 0


def _last_checked(root: str) -> "store.Session | None":
    """The latest session that has a check: a new session (a /notyet:status in a fresh window) has none yet."""
    latest = store.latest_session(root)
    if latest is not None and latest.runs:
        return latest
    return next((s for s in reversed(store.all_sessions(root)) if s.runs), None)


EXPORT_COLUMNS = ["repo", "session", "check", "at", "verdict", "finding_id", "rule", "severity", "location",
                  "title", "ack_category", "ack_reason", "label", "label_note"]


def cmd_export(args) -> int:
    """Every finding from every check, one row each, for hand-labeling (dogfooding).
    `label` is left empty: fill in true-positive / false-positive / unclear."""
    import csv
    root = _root(args.path)
    out = open(args.out, "w", newline="") if args.out else sys.stdout
    writer = csv.DictWriter(out, fieldnames=EXPORT_COLUMNS)
    writer.writeheader()
    rows = 0
    for session in store.all_sessions(root):
        for n, run in enumerate(session.runs, 1):
            for d in run.result.get("findings", []):
                f = Finding(**d)
                ack = find_ack(f, session.acks) or {}
                writer.writerow({
                    "repo": Path(root).name, "session": session.session_id, "check": n,
                    "at": time.strftime("%Y-%m-%d %H:%M", time.localtime(run.at)), "verdict": run.verdict,
                    "finding_id": f.id, "rule": f.rule, "severity": f.severity, "location": f.location,
                    "title": f.title, "ack_category": ack.get("category", ""), "ack_reason": ack.get("reason", ""),
                    "label": "", "label_note": ""})
                rows += 1
    if args.out:
        out.close()
        print(f"Wrote {rows} finding(s) to {args.out}.", file=sys.stderr)
    return 0


def cmd_receipt(args) -> int:
    root = _root(args.path)
    latest = store.receipts_dir(root) / "latest.md"
    if not latest.exists():
        print("No receipts yet.")
        return 1
    print(latest.read_text())
    return 0


def cmd_check(args) -> int:
    from notyet import ci
    root = _root(args.path)
    try:
        session = ci.session_for(root, args.base, args.merge_base)
    except snapshot.GitError as e:
        print(str(e), file=sys.stderr)
        return 2
    cfg = ci.settings(root, session, args.test_command)
    if args.request_file:       # what was asked for (a pull request's description): checked like a prompt
        session.prompts = [Path(args.request_file).read_text(errors="replace")[:4000]]
    decision = gate.check(root, session, cfg=cfg)
    failed = ci.failing(decision, session, "resolve" if args.strict else args.fail_on)
    if args.comment_file:
        Path(args.comment_file).write_text(ci.comment(decision, session, failed))
    if args.format == "json":
        print(ci.as_json(decision, session, failed))
    elif args.format == "github":
        print(ci.github(decision, session, failed))
    elif decision.verdict == "no-change":
        print(f"No changes compared with {args.base}.")
    elif decision.receipt_path:
        print(Path(decision.receipt_path).read_text())
    return 1 if failed else 0


def cmd_stats(args) -> int:
    from notyet import stats
    print(stats.render(store.all_sessions(_root(args.path))))
    return 0


def cmd_ack(args) -> int:
    root = _root(args.path)
    session = store.load_session(root, args.session) if args.session else _last_checked(root)
    if session is None or not session.runs:
        print("No gate run to acknowledge findings from.", file=sys.stderr)
        return 1
    findings = {f.id: f for f in (Finding(**d) for d in session.runs[-1].result.get("findings", []))}
    ids = [i for i in args.id.split(",") if i]
    missing = [i for i in ids if i not in findings]
    if missing:
        print(f"No finding {', '.join(missing)} in the latest check. Open findings: {', '.join(findings) or 'none'}.",
              file=sys.stderr)
        return 1
    reason = " ".join(args.reason).strip()
    if len(reason) < 15:
        print("Give a specific reason (at least a sentence); it's shown to the user on the receipt.", file=sys.stderr)
        return 1
    blocking = [i for i in ids if findings[i].severity == "block"]
    if blocking and not args.needs_human:
        print(f"{', '.join(blocking)}: [block] finding(s): fix them, or hand them to the user with --needs-human.",
              file=sys.stderr)
        return 1
    category = "needs-human" if args.needs_human else args.category
    for i in ids:
        session.acks[i] = {"category": category, "reason": reason, "at": time.time(), "title": findings[i].title}
        if findings[i].tests:
            session.acks[i].update(rule=findings[i].rule, tests=findings[i].tests)
        if findings[i].covers:
            session.acks[i].update(group=group(findings[i]), covers=findings[i].covers)
    store.save_session(root, session)
    print(f"Recorded: {', '.join(ids)} ({category}). {'They' if len(ids) > 1 else 'It'} will appear on the receipt.")
    return 0


def cmd_hook(args) -> int:
    if args.agent not in ("claude", "codex"):
        print(f"Unknown agent {args.agent!r}; supported: claude, codex", file=sys.stderr)
        return 0  # never fail a host's hook
    from notyet.hooks import claude     # one contract, two hosts: see hooks/codex.py
    return claude.main(args.event)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="notyet", description="A completion gate for AI coding agents.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="Write .notyet.toml")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--test-command", help="How to run this repo's tests (a pytest invocation)")
    p.add_argument("--force", action="store_true")
    p.add_argument("--yes", action="store_true", help="Don't ask for confirmation")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("install", help="Install notyet's hooks for an agent")
    p.add_argument("agent", choices=["claude", "codex"])
    p.add_argument("--scope", choices=["local", "project", "user"], default="local",
                   help="local: just you, this repo (default; Claude Code only); project: this repo; user: every repo")
    p.add_argument("--path", default=".")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_install)

    for name, func, help_ in (("status", cmd_status, "The current session and its last check"),
                              ("stats", cmd_stats, "What findings led to, over all sessions"),
                              ("receipt", cmd_receipt, "Print the latest receipt")):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--path", default=".")
        p.set_defaults(func=func)

    p = sub.add_parser("export", help="All findings from all sessions as CSV, for hand-labeling")
    p.add_argument("--out", help="CSV file to write (default: stdout)")
    p.add_argument("--path", default=".")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("check", help="Run the gate now, against a git ref")
    p.add_argument("--base", default="HEAD", help="what to compare the working tree with (default: HEAD)")
    p.add_argument("--merge-base", action="store_true", help="compare with where HEAD branched off BASE (for pull requests)")
    p.add_argument("--fail-on", choices=["block", "resolve", "never"], default="block",
                   help="which findings exit 1: block (default), resolve (block and fix-or-justify), or never")
    p.add_argument("--strict", action="store_true", help="the same as --fail-on resolve")
    p.add_argument("--format", choices=["text", "json", "github"], default="text")
    p.add_argument("--test-command", help="a pytest invocation, when the repo has no .notyet.toml")
    p.add_argument("--request-file", help="what the change was asked to do (e.g. the pull request's description)")
    p.add_argument("--comment-file", help="also write the brief as a pull request comment (Markdown) to this file")
    p.add_argument("--path", default=".")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("ack", help="Acknowledge a finding with a reason")
    p.add_argument("id", help="finding id, or several separated by commas")
    p.add_argument("reason", nargs="+")
    p.add_argument("--needs-human", action="store_true", help="Hand it to the user (required for [block] findings)")
    p.add_argument("--category", default="intended",
                   choices=["intended", "false-positive", "out-of-scope", "follow-up"])
    p.add_argument("--session")
    p.add_argument("--path", default=".")
    p.set_defaults(func=cmd_ack)

    p = sub.add_parser("hook", help=argparse.SUPPRESS)
    p.add_argument("agent")
    p.add_argument("event", choices=["session-start", "prompt", "stop"])
    p.set_defaults(func=cmd_hook)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
