"""Git plumbing: snapshot the working tree, diff snapshots, materialize one.

A snapshot is a git tree object holding every file in the working tree,
including uncommitted and untracked (non-ignored) ones. It's built with a
temporary copy of the index, so the user's index, HEAD and files are never
touched; the only side effect is new objects in the object store, which git
garbage-collects in time if nothing references them.
"""
import os
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional


class GitError(RuntimeError):
    pass


# Output is parsed, so the user's own git configuration mustn't change its shape: diff.noprefix
# alone used to empty every diff notyet read, and silently.
_STABLE = [a for kv in ("core.quotePath=false", "diff.noprefix=false", "diff.mnemonicPrefix=false", "diff.renames=true",
                        "diff.external=", "diff.orderFile=/dev/null", "diff.suppressBlankEmpty=false", "diff.relative=false",
                        "diff.srcPrefix=a/", "diff.dstPrefix=b/", "color.ui=false")
           for a in ("-c", kv)]
# set when notyet runs inside a git hook: they'd point git at another index or repo
_LEAKING = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_PREFIX", "GIT_EXTERNAL_DIFF", "GIT_DIFF_OPTS")


def git(root: str, *args: str, env: Optional[dict] = None, input: Optional[str] = None) -> str:
    base = {k: v for k, v in os.environ.items() if k not in _LEAKING}
    try:
        return subprocess.run(
            ["git", *_STABLE, "-C", root, *args], capture_output=True, text=True, check=True, encoding="utf-8",
            errors="surrogateescape", env={**base, **(env or {})}, input=input,
        ).stdout
    except FileNotFoundError:
        raise GitError("git is not installed") from None
    except subprocess.CalledProcessError as e:
        raise GitError(f"git {' '.join(args[:2])} failed: {e.stderr.strip()[:300]}") from None


def repo_root(path: str = ".") -> str:
    return git(str(Path(path).resolve()), "rev-parse", "--show-toplevel").strip()


def git_dir(root: str) -> Path:
    """The repo's .git directory (a worktree's own git dir for linked worktrees)."""
    return Path(git(root, "rev-parse", "--absolute-git-dir").strip())


def head_tree(root: str) -> Optional[str]:
    try:
        return git(root, "rev-parse", "--verify", "--quiet", "HEAD^{tree}").strip() or None
    except GitError:
        return None  # no commits yet


EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def snapshot(root: str) -> str:
    """Tree id of the current working tree (tracked, modified and untracked
    files; .gitignore'd files excluded)."""
    real_index = git_dir(root) / "index"
    with tempfile.TemporaryDirectory(prefix="notyet-index-") as tmp:
        index = Path(tmp) / "index"
        if real_index.exists():
            # Reuse its stat cache so unchanged files aren't rehashed. copy2 keeps the
            # index's mtime, which git's racy-git check relies on: with a fresh mtime,
            # a same-size edit made in the same second as the index was written would
            # look unchanged.
            shutil.copy2(real_index, index)
        env = {"GIT_INDEX_FILE": str(index)}
        _unhide(root, env)
        git(root, "add", "--all", env=env)
        return git(root, "write-tree", env=env).strip()


def _unhide(root: str, env: dict) -> None:
    """Clear `assume-unchanged` (and `skip-worktree` on files that are present) in the temporary
    index: with either bit, git would report an edited file as unchanged."""
    if not os.path.exists(env["GIT_INDEX_FILE"]):
        return
    hidden, skipped = [], []
    for entry in git(root, "ls-files", "-v", "-z", env=env).split("\0"):
        tag, path = entry[:1], entry[2:]
        if tag.islower():
            hidden.append(path)
        if tag in ("S", "s") and os.path.lexists(os.path.join(root, path)):
            skipped.append(path)     # an absent one is a sparse checkout doing its job
    for flag, paths in (("--no-assume-unchanged", hidden), ("--no-skip-worktree", skipped)):
        if paths:
            git(root, "update-index", flag, "-z", "--stdin", env=env, input="\0".join(paths) + "\0")


@dataclass
class FileDelta:
    status: str             # A, M, D, R (rename), T (type change)
    path: str               # path in the newer tree
    old_path: Optional[str] = None


def diff_trees(root: str, old: str, new: str) -> list[FileDelta]:
    out = git(root, "diff-tree", "-r", "-M", "--name-status", "--no-commit-id", "-z", old, new)
    parts = [p for p in out.split("\0") if p]
    deltas: list[FileDelta] = []
    i = 0
    while i < len(parts):
        status = parts[i]
        if status[0] in ("R", "C"):
            deltas.append(FileDelta("R", parts[i + 2], parts[i + 1]))
            i += 3
        else:
            deltas.append(FileDelta(status[0], parts[i + 1]))
            i += 2
    return deltas


def unified_diff(root: str, old: str, new: str, *paths: str, context: int = 0) -> str:
    return git(root, "diff", f"-U{context}", "-M", "--no-color", "--no-ext-diff", old, new, "--", *paths)


def added_lines(root: str, old: str, new: str, deltas: list[FileDelta]) -> dict[str, set[int]]:
    """Line numbers (in the current tree) each changed .py file gained."""
    diff = unified_diff(root, old, new, *[d.path for d in deltas],
                                 *[d.old_path for d in deltas if d.old_path])
    out: dict[str, set[int]] = {}
    path, line = None, 0
    for text in diff.splitlines():
        if text.startswith("+++ "):
            path = text[6:] if text.startswith("+++ b/") else None
        elif text.startswith("@@"):
            m = re.match(r"@@ -\S+ \+(\d+)", text)
            line = int(m.group(1)) if m else 0
        elif text.startswith("+") and path:
            out.setdefault(path, set()).add(line)
            line += 1
        elif text.startswith(" "):
            line += 1
    return out


def show(root: str, tree: str, path: str) -> Optional[str]:
    """File contents at `path` in `tree`, or None if absent or binary."""
    try:
        text = git(root, "cat-file", "-p", f"{tree}:{path}")
    except GitError:
        return None
    return None if "\0" in text else text


def materialize(root: str, tree: str, dest: str, paths: list[str] | None = None) -> None:
    """Write the files of `tree` (or just `paths` in it) into `dest`, as a checkout would.

    Not `git archive`: that leaves out `export-ignore` paths (often the tests) and rewrites
    `export-subst` ones."""
    if paths is not None and not paths:
        return
    with tempfile.TemporaryDirectory(prefix="notyet-index-") as tmp:
        env = {"GIT_INDEX_FILE": os.path.join(tmp, "index")}
        git(root, "read-tree", tree, env=env)
        prefix = f"--prefix={os.path.join(dest, '')}"
        if paths is None:
            git(root, "checkout-index", "--all", "--force", prefix, env=env)
        else:
            git(root, "checkout-index", "--force", prefix, "-z", "--stdin", env=env, input="\0".join(paths) + "\0")


# Ignored directories a checkout must not share with the working tree.
_NOT_SHARED = {"__pycache__", ".git", ".venv", "venv", "env", ".tox", ".nox", ".pytest_cache", ".ruff_cache",
               ".mypy_cache", ".hypothesis", "build", "dist", "htmlcov", "site-packages"}
MAX_IGNORED_FILES = 4000
MAX_IGNORED_BYTES = 60_000_000


def add_ignored(root: str, dest: str) -> list[str]:
    """Add to the checkout at `dest` the gitignored files a fresh one lacks but the code may need: a
    generated `_version.py`, a compiled extension, local test data. Copied, never linked: a test that
    writes to or deletes its data must not reach the user's. Returns what was left out (too much to copy)."""
    try:
        listing = git(root, "ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z")
    except GitError:
        return []
    left_out, room, files = [], MAX_IGNORED_BYTES, MAX_IGNORED_FILES
    for rel in filter(None, listing.split("\0")):
        parts = rel.rstrip("/").split("/")
        if set(parts) & _NOT_SHARED or parts[-1].endswith((".egg-info", ".pyc", ".log")) or parts[-1].startswith(".coverage"):
            continue
        src, target = os.path.join(root, rel.rstrip("/")), os.path.join(dest, rel.rstrip("/"))
        if os.path.lexists(target) or not os.path.isdir(os.path.dirname(target)):
            continue
        size, count = _measure(src, room, files)
        if size > room or count > files:
            left_out.append(rel)
            continue
        room, files = room - size, files - count
        try:
            if os.path.isdir(src) and not os.path.islink(src):
                shutil.copytree(src, target, symlinks=True, ignore=shutil.ignore_patterns(*_NOT_SHARED))
            else:
                shutil.copy2(src, target, follow_symlinks=False)
        except (OSError, shutil.Error):
            left_out.append(rel)
    return left_out


def _measure(path: str, room: int, files: int) -> tuple[int, int]:
    """(bytes, files) under a path, stopping once either is over its limit."""
    if os.path.islink(path) or not os.path.isdir(path):
        try:
            return os.lstat(path).st_size, 1
        except OSError:
            return 0, 1
    size = count = 0
    for folder, dirs, names in os.walk(path):
        dirs[:] = [d for d in dirs if d not in _NOT_SHARED]
        for name in names:
            count += 1
            try:
                size += os.lstat(os.path.join(folder, name)).st_size
            except OSError:
                pass
            if size > room or count > files:
                return size, count
    return size, count


@contextmanager
def checkout(root: str, tree: str, label: str) -> Iterator[str]:
    """A throwaway directory holding `tree` (plus the ignored files it needs to run). The path is
    resolved: macOS temp dirs live behind /var -> /private/var, and every path check compares with it."""
    with tempfile.TemporaryDirectory(prefix=f"notyet-{label}-") as tmp:
        tmp = os.path.realpath(tmp)
        materialize(root, tree, tmp)
        add_ignored(root, tmp)
        yield tmp
