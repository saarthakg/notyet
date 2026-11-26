"""What the user asked for that nothing in the change answers.

No model reads the request. Two things in it can be checked as they are:

- **New names.** A flag (`--dry-run`), a file (`returns.jsonl`), an identifier (`start_return`, `retryPolicy`, anything
  in backticks) that the request names and the repo didn't have at session start is something the user asked to
  exist. If it still appears nowhere in the repo when the agent stops, that part wasn't done. A name the repo already
  had is context ("the same check as lookup_order"), not a request, so it's never counted.
- **Documentation.** A request that asks for docs ("document it", "update the README", "docs as before") and a change
  that touches no documentation file.

A name right after a negation ("don't add a --force flag", "without a config.py") isn't asked for. Fenced code blocks
in the request (pasted logs, tracebacks) are left out. This only ever reports what's missing; whether what exists is
right is for the tests and the user.
"""
import re
from pathlib import PurePosixPath

from notyet import snapshot
from notyet.findings import Context, EngineResult, Finding, digests

_FENCE = re.compile(r"```.*?(```|$)", re.DOTALL)
_TICKED = re.compile(r"`([^`\n]{2,60})`")
_FLAG = re.compile(r"(?<![\w-])(--[a-z][a-z0-9]*(?:-[a-z0-9]+)*)\b")
_FILE = re.compile(r"(?<![\w/.-])((?:[\w-]+/)*[\w-]+\.(?:py|md|rst|txt|json|jsonl|toml|ya?ml|cfg|ini|csv|sql|sh|html|css|js|ts))\b")
_IDENT = re.compile(r"(?<![\w.-])([a-z][a-z0-9]*(?:_[a-z0-9]+)+|[a-z]+(?:[A-Z][a-z0-9]+)+)(?![\w-])")
_NEGATED = re.compile(r"\b(don'?t|do not|doesn'?t|never|without|no need|instead of|rather than|avoid|not|no|remove|delete|drop|stop using)\b"
                      r"(\W+\w+){0,4}\W*$", re.IGNORECASE)
# asking for docs, not mentioning them: "document it", "update the README", "tests and docs", "docs as before"
_DOC = r"(the |a |its |some )?(readme|docs?|documentation|changelog|usage notes|decisions? (log|record)s?)"
_DOCS = re.compile(r"\bdocument(s|ed|ing)?\b(?! (says?|said|states?))|"
                   r"\b(update|add|write|extend|mention|note|describe|explain|record|log)\b[^.\n]{0,40}?\b(in|to|into)? ?" + _DOC + r"\b|"
                   r"\b(and|plus|with|also|,)\s+" + _DOC + r"\b|"
                   r"^\s*" + _DOC + r"\b[^.\n]{0,20}(too|as well|as before|update|\.)", re.IGNORECASE | re.MULTILINE)
_DOC_PATH = re.compile(r"(^|/)(docs?|documentation)/|\.(md|rst|txt|adoc)$|(^|/)(README|CHANGELOG|CHANGES|HISTORY|NEWS)[^/]*$",
                       re.IGNORECASE)
MAX_NAMES = 40


def requested(prompt: str) -> tuple[list[str], bool]:
    """The new-looking names a request asks for, and whether it asks for documentation."""
    text = _FENCE.sub(" ", prompt)
    names: list[str] = []
    for pattern in (_TICKED, _FLAG, _FILE, _IDENT):
        for m in pattern.finditer(text):
            if _negated(text, m.start()):
                continue
            name = m.group(1).strip()
            if pattern is _TICKED and not re.fullmatch(r"[\w./-]+(\(\))?", name):
                continue        # a phrase or an expression in backticks, not a name
            name = name.removesuffix("()")
            if pattern is _FILE or "/" in name:
                name = PurePosixPath(name).name
            if name not in names:
                names.append(name)
    docs = any(not _negated(text, m.start()) for m in _DOCS.finditer(text))
    return names[:MAX_NAMES], docs


def _negated(text: str, at: int) -> bool:
    """Does a negation lead up to position `at`, within its own sentence?"""
    before = re.split(r"[.;:!?\n]", text[max(0, at - 60):at])[-1]
    return bool(_NEGATED.search(before))


def run(ctx: Context) -> EngineResult:
    result = EngineResult()
    prompts = ctx.session.prompts
    if not prompts:
        return result
    asked: list[str] = []
    docs = False
    for prompt in prompts:
        names, wants_docs = requested(prompt)
        asked += [n for n in names if n not in asked]
        docs = docs or wants_docs
    if asked:
        had = _found(ctx, ctx.baseline_tree, asked)
        new = [n for n in asked if n not in had]
        missing = [n for n in new if n not in _found(ctx, ctx.current_tree, new)] if new else []
        if missing:
            result.findings.append(Finding(
                rule="asked-missing", severity="resolve", location="the request",
                title=f"you asked for {_list(missing)}; {'it appears' if len(missing) == 1 else 'they appear'} "
                      f"nowhere in the repo",
                evidence=[f"`{n}`" for n in missing[:8]], key="asked|" + "|".join(sorted(missing)),
                covers=digests(missing),
                action="Do that part, or say in your closing message why it's left out: the user will see it."))
        result.checks.append(f"the request: {len(new)} new name(s) it asks for, {len(new) - len(missing)} present")
    if docs and not any(_DOC_PATH.search(d.path) for d in ctx.deltas) and _has_docs(ctx):
        result.findings.append(Finding(
            rule="docs-missing", severity="resolve", location="the request",
            title="you asked for documentation; no documentation file changed",
            key="docs-missing",
            action="Update the docs the request asked for, or say in your closing message why not."))
    return result


def _list(names: list[str]) -> str:
    shown = ", ".join(f"`{n}`" for n in names[:4])
    return shown + (f" and {len(names) - 4} more" if len(names) > 4 else "")


def _found(ctx: Context, tree: str, names: list[str]) -> set[str]:
    """Which of `names` appear anywhere in `tree`'s tracked text files (as words, flags and file names as written)."""
    args = []
    for n in names:
        args += ["-e", n]
    try:
        out = snapshot.git(ctx.root, "-c", "grep.column=false", "-c", "grep.fullName=false", "grep", "-I", "-h", "-o",
                           "-F", "--no-color", *args, tree)
    except snapshot.GitError:       # exit 1: no match at all
        out = ""
    found = set(out.splitlines())
    try:      # a file the request names counts if it exists, whatever its content
        paths = {PurePosixPath(p).name for p in snapshot.git(ctx.root, "ls-tree", "-r", "--name-only", tree).splitlines()}
    except snapshot.GitError:
        paths = set()
    return {n for n in names if n in found or n in paths}


def _has_docs(ctx: Context) -> bool:
    """Is there any documentation to update? A repo without docs can't be asked to keep them current."""
    try:
        files = snapshot.git(ctx.root, "ls-tree", "-r", "--name-only", ctx.baseline_tree).splitlines()
    except snapshot.GitError:
        return False
    return any(_DOC_PATH.search(p) for p in files)
