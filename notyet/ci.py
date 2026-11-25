"""The gate without an agent session: `notyet check --base REF`, in CI or by hand.

The change is REF (or its merge base with HEAD) to the working tree. There's no agent to
send back to work, so findings are a verdict and an exit code; a reason for one is given in
a commit message of the change, where reviewers see it:

    Notyet-Ack: 1a2b3c4d,5e6f7a8b the message wording was changed on request
"""
import json
import os
import re
import time
from dataclasses import asdict

from notyet import config as config_mod
from notyet import gate, snapshot, store
from notyet.findings import Finding
from notyet.receipt import VERDICT_TEXT

FAIL_ON = {"block": ("block",), "resolve": ("block", "resolve"), "never": ()}
_TRAILER = re.compile(r"^Notyet-Ack:\s*([0-9a-f]{8}(?:\s*,\s*[0-9a-f]{8})*)\s+(\S.*)$", re.MULTILINE | re.IGNORECASE)


def session_for(root: str, base: str, merge_base: bool) -> store.Session:
    """A session that started at `base`. Raises GitError if there's no such commit."""
    commit = snapshot.git(root, "rev-parse", "--verify", f"{base}^{{commit}}").strip()
    if merge_base:
        commit = snapshot.git(root, "merge-base", commit, "HEAD").strip()
    tree = snapshot.git(root, "rev-parse", f"{commit}^{{tree}}").strip()
    session = store.start_session(root, f"check-{time.time_ns()}", "ref")
    session.baseline_tree = session.baseline_head = tree
    session.baseline_commit = commit
    for ids, reason in _TRAILER.findall(snapshot.git(root, "log", "--format=%B", f"{commit}..HEAD")):
        for fid in re.split(r"\s*,\s*", ids):
            session.acks[fid.lower()] = {"category": "commit-message", "reason": reason.strip(), "at": time.time()}
    store.save_session(root, session)
    return session


def settings(root: str, session: store.Session, test_command: str | None) -> config_mod.Config:
    """The base's config, so a change can't loosen its own gate; the current one if the base has none."""
    cfg = config_mod.load(root, session.baseline_tree)
    if cfg.source == "defaults":
        cfg = config_mod.load(root)
    cfg.test_command = test_command or cfg.test_command or config_mod.detect_test_command(root)
    cfg.mode = "report"
    return cfg


def failing(decision: gate.Decision, session: store.Session, fail_on: str) -> list[Finding]:
    # a commit-message reason clears either level here: the people reviewing the change are who it's for
    return [f for f in decision.findings if f.severity in FAIL_ON[fail_on] and f.id not in session.acks]


def as_json(decision: gate.Decision, session: store.Session, failed: list[Finding]) -> str:
    result = session.runs[-1].result if session.runs else {}
    return json.dumps({
        "verdict": "failed" if failed else decision.verdict, "failing": [f.id for f in failed],
        "findings": [{"id": f.id, **asdict(f), "reason": session.acks.get(f.id, {}).get("reason")} for f in decision.findings],
        "checks": result.get("checks", []), "not_checked": result.get("not_checked", []), "gaps": result.get("gaps", []),
        "receipt": decision.receipt_path}, indent=1)


def github(decision: gate.Decision, session: store.Session, failed: list[Finding]) -> str:
    """Workflow annotations on stdout, and the receipt as the job's summary."""
    out = []
    for f in decision.findings:
        if f.severity == "note":
            continue
        level = "error" if f in failed else "warning"
        path, line = _place(f)
        where = f" file={_prop(path)}" + (f",line={line}" if line else "") if path else ""
        reason = session.acks.get(f.id, {}).get("reason")
        text = f"{f.title}. {f.action}" + (f" (id {f.id})" if f in failed else "") + (f" Reason given: {reason}" if reason else "")
        out.append(f"::{level}{where}::{_message(text)}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary and decision.receipt_path:
        with open(decision.receipt_path) as receipt, open(summary, "a") as f:
            f.write(receipt.read() + "\nTo accept an item, give the reason in a commit message: "
                                     "`Notyet-Ack: <id> <reason>`\n")
    out.append(f"notyet: {'FAILED' if failed else VERDICT_TEXT.get(decision.verdict, decision.verdict)}, "
               f"{len(failed)} failing item(s)")
    return "\n".join(out)


def _place(f: Finding) -> tuple[str, str]:
    path = f.location.split("::")[0]
    path, _, line = path.partition(":")
    return path, line if line.isdigit() else ""


def _message(s: str) -> str:
    return s.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _prop(s: str) -> str:
    return _message(s).replace(":", "%3A").replace(",", "%2C")
