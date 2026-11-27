---
description: Accept a notyet finding, with the reason
argument-hint: "<finding id> <reason>"
allowed-tools: Bash(python3 *scripts/notyet* ack*)
---
The user accepts a notyet finding and gives the reason: `$ARGUMENTS` (a finding id, or several separated by commas,
then the reason).

If the arguments don't start with an id, ask for it: `/notyet:status` lists them. Otherwise run, with the reason
quoted as one argument:

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/notyet" ack <id> "<reason>"
```

Say in one line what was accepted. The reason goes on the receipt, where reviewers see it.
