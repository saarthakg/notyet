---
description: Set notyet up in this repo (writes .notyet.toml, report mode)
argument-hint: "[--test-command 'python -m pytest']"
allowed-tools: Bash(python3 *scripts/notyet* init*)
---
Set up notyet, the completion gate, in this repository.

Run this from the repository's root, exactly as written:

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/notyet" init --yes $ARGUMENTS
```

It finds the repo's pytest command, writes `.notyet.toml` in report mode (it never blocks; it leaves a receipt after each
change) and says whether the test command works. Show the user what it printed, briefly: the test command it chose,
whether it works, and any warning. If it found no test command or the command doesn't work, suggest running this again
with `--test-command '<the right pytest invocation>'`. Don't edit `.notyet.toml` yourself, and don't change anything
else.
