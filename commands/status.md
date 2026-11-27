---
description: What notyet found at the last check, and the receipt
allowed-tools: Bash(python3 *scripts/notyet* status*), Bash(python3 *scripts/notyet* receipt*)
---
!`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/notyet" status`

Above is notyet's status for this session. Summarize it for the user in a few lines: the verdict, what isn't done or
was broken, and which items still need a reason. If they want the details, run
`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/notyet" receipt`.
