"""Undone work: this turn removed lines that earlier work put there.

"Earlier work" is what existed when the current prompt was submitted and
wasn't there before the session: the agent's fixes from earlier turns, and
edits the user made between turns. The user's own uncommitted edits from
before the session count too. A line that was *replaced* is work going on, not
work undone: what counts is a removal with nothing in its place, or a change that
puts back text the earlier work had replaced. Imports, and a name that is now
assigned somewhere else, have only moved. Lines that were only moved
(the same text added elsewhere in this turn) and trivial lines (blank,
comments, short tokens like `else:`) are ignored. Snapshots, not git blame:
most of this work was never committed.

Removing earlier work can be right, so this is fix-or-justify, never a block.
"""
import hashlib
import re
from collections import Counter

from notyet import snapshot
from notyet.findings import Context, EngineResult, Finding

MIN_IN_TESTS = 3  # tests get tidied; a line or two there says little
MIN_LINE = 8   # shorter stripped lines (`return`, `else:`, `})`) say nothing about intent


def run(ctx: Context) -> EngineResult:
    result = EngineResult()
    s = ctx.session
    turn_start = s.turns[-1]["tree"] if s.turns else ctx.baseline_tree
    if turn_start == ctx.current_tree:
        return result

    if s.turns and s.turns[-1].get("lost"):
        result.not_checked.append("undone work: commits arrived during this turn and couldn't be told apart from it")
        return result
    protected: dict[str, Counter] = {}
    origin: dict[str, str] = {}
    if s.baseline_head and s.baseline_source == "session-start" and s.baseline_head != ctx.baseline_tree:
        _add(protected, origin, _lines(ctx.root, s.baseline_head, ctx.baseline_tree, "+"),
             "your uncommitted edits from before the session")
    if turn_start != ctx.baseline_tree:
        _add(protected, origin, _lines(ctx.root, ctx.baseline_tree, turn_start, "+"), "an earlier turn of this session")
    if not protected:
        return result

    removed, plus_back = _undoings(ctx.root, turn_start, ctx.current_tree, _texts_before(ctx, turn_start))
    moved = Counter(text for lines in _lines(ctx.root, turn_start, ctx.current_tree, "+").values() for text in lines)
    assigned = {n for n in map(_name, moved) if n}
    for path in sorted(removed):
        keep = protected.get(path)
        if not keep:
            continue
        undone = []
        for text in removed[path]:
            if text.startswith(("import ", "from ")) or _name(text) in assigned:
                continue
            if keep[text] > 0 and moved[text] <= 0:
                keep[text] -= 1
                undone.append(text)
            elif moved[text] > 0:
                moved[text] -= 1
        reverted = set(plus_back.get(path, ()))
        if not undone or (ctx.layout.is_test_side(path) and len(undone) < MIN_IN_TESTS and not reverted
                          and not any(_DEFINES.match(t) for t in undone)):
            continue
        digest = hashlib.sha1("\n".join(undone).encode()).hexdigest()[:10]
        result.findings.append(Finding(
            rule="undone-work", severity="resolve", location=path,
            title=f"{len(undone)} line(s) in {path} from {origin[path]} were removed in this turn",
            evidence=[t[:120] for t in undone[:3]], key=f"{path}|{digest}",
            action="Restore them if removing them wasn't part of the request; otherwise acknowledge why."))
    return result


_DEFINES = re.compile(r"(async )?def |class ")
_ASSIGNS = re.compile(r"([A-Za-z_]\w*)\s*(?::[^=]+)?=(?!=)")


def _name(text: str) -> str:
    """The name a line assigns, if it does."""
    m = _ASSIGNS.match(text)
    return m.group(1) if m else ""


def _texts_before(ctx: Context, turn_start: str) -> dict[str, set[str]]:
    """Per file, the lines earlier work removed: putting one back is undoing it."""
    out: dict[str, set[str]] = {}
    s = ctx.session
    for old, new in ((s.baseline_head, ctx.baseline_tree), (ctx.baseline_tree, turn_start)):
        if old and old != new:
            for path, texts in _lines(ctx.root, old, new, "-").items():
                out.setdefault(path, set()).update(texts)
    return out


def _undoings(root: str, old: str, new: str, before: dict[str, set[str]]) -> tuple[dict[str, list[str]], dict[str, set[str]]]:
    """Non-trivial lines removed between two trees, in hunks that add nothing of substance in their place,
    or that add back a line earlier work had removed; and those lines."""
    out: dict[str, list[str]] = {}
    back: dict[str, set[str]] = {}
    diff = snapshot.git(root, "diff", "-U0", "--no-color", "--no-ext-diff", "--no-renames", old, new)
    path, minus, plus = None, [], []

    def flush() -> None:
        restored = set(plus) & before.get(path or "", set())
        if path and minus and (not plus or restored):
            out.setdefault(path, []).extend(minus)
            back.setdefault(path, set()).update(restored)
        minus.clear()
        plus.clear()

    for line in diff.splitlines():
        if line.startswith(("@@", "diff ")):
            flush()
        elif line.startswith("--- "):
            path = line[6:] if line.startswith("--- a/") else None
        elif line.startswith("+++ "):
            continue
        elif line[:1] in "+-" and line:
            text = line[1:].strip()
            if len(text) >= MIN_LINE and not text.startswith(("#", "//")):
                (minus if line[0] == "-" else plus).append(text)
    flush()
    return out, back


def _add(protected: dict[str, Counter], origin: dict[str, str], lines: dict[str, list[str]], label: str) -> None:
    for path, texts in lines.items():
        protected.setdefault(path, Counter()).update(texts)
        origin.setdefault(path, label)


def _lines(root: str, old: str, new: str, sign: str) -> dict[str, list[str]]:
    """Non-trivial added ("+") or removed ("-") lines per file between two trees."""
    diff = snapshot.git(root, "diff", "-U0", "--no-color", "--no-ext-diff", "--no-renames", old, new)
    out: dict[str, list[str]] = {}
    path = None
    header = "+++ " if sign == "+" else "--- "
    prefix = "+++ b/" if sign == "+" else "--- a/"
    for line in diff.splitlines():
        if line.startswith(header):
            path = line[len(prefix):] if line.startswith(prefix) else None
        elif line.startswith(("+++ ", "--- ")):
            continue
        elif path and line.startswith(sign):
            text = line[1:].strip()
            if len(text) >= MIN_LINE and not text.startswith(("#", "//")):
                out.setdefault(path, []).append(text)
    return out
