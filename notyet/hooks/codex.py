"""Codex CLI integration.

Codex's hooks (hooks.json, documented at learn.chatgpt.com/docs/hooks, read Nov 2026) have the
same shape as Claude Code's: SessionStart, UserPromptSubmit and Stop, JSON on stdin with
session_id, cwd, prompt and stop_hook_active, and {"decision": "block", "reason": ...} to send
the agent back. So the events are handled by hooks/claude.py; only installing differs. Codex
doesn't report background tasks, so a check always runs at Stop.

Not yet exercised against a running Codex: the tests feed it documented payloads.
"""
from pathlib import Path

from notyet.hooks import claude

NAME = "Codex"
AFTER_INSTALL = "Codex skips a new hook until you trust it: run /hooks in Codex and approve notyet's three."


def settings_path(root: str, scope: str) -> Path:
    if scope == "user":
        return Path.home() / ".codex" / "hooks.json"
    return Path(root) / ".codex" / "hooks.json"      # Codex has no per-user file inside a repo


def install(root: str, scope: str, stop_timeout: int, confirm) -> tuple[Path, bool]:
    return claude.write_hooks(settings_path(root, scope), "codex", stop_timeout, confirm)
