"""Commits that arrived during the session without the session writing them.

A `git pull`, a merge of someone's branch, a branch switch: the working tree changes,
but none of that is the agent's work, and blaming it for a colleague's removed test is
the kind of false block that gets a gate uninstalled. So when HEAD moves, the session's
starting point moves with the part of the move that came from elsewhere.

What the session committed itself is known from HEAD's reflog (commit, cherry-pick,
rebase picks, ...). Every other new commit counts as arriving from elsewhere only if
it's older than the session or is on a remote-tracking branch; anything else stays part
of the session's change. Moving *back* (git reset, checking out an ancestor) never moves
the starting point: work that disappears that way should be seen.

The new starting point is rebuilt with git's own three-way merge (git merge-tree, 2.38+),
keeping edits the user hadn't committed when the session began. If that conflicts, the
starting point stays where it was and the receipt says so.
"""
import os
import re
import subprocess
from typing import Optional

from notyet import snapshot, store

_LOCAL = re.compile(r"^(commit|cherry-pick|revert|am\b)|\((pick|reword|squash|fixup|edit|continue)\)|Merge made by")
_ID = {f"GIT_{who}_{what}": v for who in ("AUTHOR", "COMMITTER")
       for what, v in (("NAME", "notyet"), ("EMAIL", "notyet@localhost"))}


def head_commit(root: str) -> Optional[str]:
    try:
        return snapshot.git(root, "rev-parse", "--verify", "--quiet", "HEAD^{commit}").strip() or None
    except snapshot.GitError:
        return None


def follow(root: str, session: store.Session) -> None:
    """Move the session's starting point past commits that came from elsewhere. Updates `session` in place."""
    head = head_commit(root)
    if head is None or session.baseline_commit is None or head == session.head_seen:
        return
    session.head_seen = head
    arrived = _arrived(root, session, head)
    if not arrived:
        return
    try:
        frontier = _frontier(root, arrived, session.baseline_commit, head)
        base = _merged(root, frontier)
        tree = _tree(root, base)
        moved = _carry(root, session.baseline_commit, session.baseline_tree, base)     # the user's uncommitted edits
        if moved is None:
            raise snapshot.GitError("your uncommitted edits conflict with it")
    except snapshot.GitError as e:
        session.moves.append({"commits": len(arrived), "head": head, "ok": False, "why": str(e)[:200]})
        return
    for turn in session.turns:       # what earlier turns left, on the new starting point
        carried = _carry(root, session.baseline_commit, turn["tree"], base)
        if carried is None:
            turn["lost"] = True      # can't tell this turn's undoing from the move
        else:
            turn["tree"] = carried
    session.baseline_tree, session.baseline_head, session.baseline_commit = moved, tree, base
    session.moves.append({"commits": len(arrived), "head": head, "ok": True})


def _arrived(root: str, session: store.Session, head: str) -> list[str]:
    out = snapshot.git(root, "rev-list", "--format=%H %ct", "--no-commit-header", head, "--not", session.baseline_commit or head)
    new = [(sha, int(at)) for sha, at in (line.split() for line in out.splitlines() if line)]
    if not new:
        return []
    mine = _written_here(root, int(session.started))
    recent = [sha for sha, at in new if sha not in mine and at > session.started]
    on_remote: set[str] = set()
    if recent:
        not_on_remote = set(snapshot.git(root, "rev-list", head, "--not", "--remotes").split())
        on_remote = {sha for sha in recent if sha not in not_on_remote}
    return [sha for sha, at in new if sha not in mine and (at <= session.started or sha in on_remote)]


def _written_here(root: str, since: int) -> set[str]:
    try:
        out = snapshot.git(root, "reflog", "show", "--date=unix", "--format=%H%x00%gd%x00%gs", "HEAD")
    except snapshot.GitError:
        return set()
    mine = set()
    for line in out.splitlines():
        sha, when, what = (line.split("\0") + ["", ""])[:3]
        at = re.search(r"\{(\d+)\}", when)
        if at and int(at.group(1)) >= since and _LOCAL.search(what):
            mine.add(sha)
    return mine


def _frontier(root: str, arrived: list[str], base: str, head: str) -> list[str]:
    """The latest commits from elsewhere that HEAD contains, plus the old starting point if HEAD still does."""
    parents = {p for line in snapshot.git(root, "rev-list", "--no-walk", "--parents", "--stdin",
                                          input="\n".join(arrived) + "\n").splitlines() for p in line.split()[1:]}
    tips = [sha for sha in arrived if sha not in parents]
    try:
        snapshot.git(root, "merge-base", "--is-ancestor", base, head)
        tips.append(base)
    except snapshot.GitError:
        pass
    return snapshot.git(root, "merge-base", "--independent", *tips).split()


def _merged(root: str, commits: list[str]) -> str:
    """One commit holding all of `commits`."""
    result = commits[0]
    for other in commits[1:]:
        tree = _merge_tree(root, result, other)
        if tree is None:
            raise snapshot.GitError("the commits that arrived conflict with the starting point")
        result = _commit(root, tree, result, other)
    return result


def _carry(root: str, base: str, tree: str, onto: str) -> Optional[str]:
    """`tree` (edits made on top of commit `base`) moved onto commit `onto`, or None if that conflicts."""
    if tree == _tree(root, base):
        return _tree(root, onto)
    return _merge_tree(root, onto, _commit(root, tree, base), merge_base=base)


def _merge_tree(root: str, ours: str, theirs: str, merge_base: Optional[str] = None) -> Optional[str]:
    args = ["git", "-C", root, "merge-tree", "--write-tree", "--no-messages",
            *([f"--merge-base={merge_base}"] if merge_base else []), ours, theirs]
    proc = subprocess.run(args, capture_output=True, text=True, check=False)
    return proc.stdout.split()[0] if proc.returncode == 0 and proc.stdout.strip() else None


def _commit(root: str, tree: str, *parents: str) -> str:
    """A commit object nothing refers to (merge-tree wants commits); git collects it in time."""
    return snapshot.git(root, "commit-tree", tree, *[a for p in parents for a in ("-p", p)], "-m", "notyet",
                        env={**os.environ, **_ID}).strip()


def _tree(root: str, commit: str) -> str:
    return snapshot.git(root, "rev-parse", f"{commit}^{{tree}}").strip()
