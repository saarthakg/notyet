"""Did the agent already tell the user it changed a test?

Agents that change what a test expects nearly always say so in their closing message
(docs/EVAL.md §6: 43 of 43 in recorded sessions). Sending one back only to repeat it costs the user's
time and buys nothing: the change goes straight to the user, with what it was and what it is now.
One that isn't mentioned is sent back.

The message never clears anything: an item that was mentioned is shown to the user for review, not passed.
"""
import re
from pathlib import PurePosixPath
from typing import Optional, Sequence

_CHANGE = re.compile(r"\b(updat|chang|edit|modif|remov|delet|renam|adjust|drop|replac|rewr[io]t|rework|loosen|relax|skip|disabl|"
                     r"convert|mov|merg|split|fold|now (assert|expect|check|require)|expect\w* (now|is now|to be)|sign.?off|"
                     r"no longer)", re.IGNORECASE)


def disclosed(message: str, tests: Sequence[str]) -> Optional[str]:
    """The passage of `message` that says one of `tests` (node ids) was changed, or None. The passage has to name
    the test or its file, and have a word for changing it in the same paragraph or in the heading above its list."""
    if not message or not tests:
        return None
    names = set()
    for nodeid in tests:
        path, _, func = nodeid.partition("::")
        names.add(PurePosixPath(path).name.lower())
        if func:
            names.add(func.split("::")[-1].split("[")[0].lower())
    heading = ""
    for block in re.split(r"\n\s*\n", message):
        low = block.lower()
        if any(re.search(rf"(?<![\w.]){re.escape(n)}(?!\w)", low) for n in names) and _CHANGE.search(heading + "\n" + block):
            line = next((x for x in block.splitlines() if any(n in x.lower() for n in names)), block)
            return re.sub(r"\s+", " ", line).strip(" -*|#")[:240]
        if len(block) < 300 and block.rstrip().endswith((":", ":**")) or block.lstrip().startswith("#"):
            heading = block
        elif not re.match(r"\s*([-*|]|\d+\.)", block):
            heading = ""
    return None
