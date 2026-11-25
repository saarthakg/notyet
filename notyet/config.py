"""Configuration: `.notyet.toml` at the repo root.

During a session the gate reads the file as it was when the session started
(from the baseline snapshot), so an agent editing it mid-session can't
loosen its own gate; a changed config is reported on the receipt instead.
"""
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from notyet import snapshot

CONFIG_FILE = ".notyet.toml"
RULE_SETTINGS = ("block", "resolve", "note", "off")

TEMPLATE = """\
# notyet: a completion gate for AI coding agents. https://github.com/saarthakg/notyet

[test]
# How to run this repo's tests. notyet adds its own flags (test selection,
# junit output), so this must be a pytest invocation for now.
command = "{command}"
# Seconds for running the tests at each check. The whole check, with the comparisons against
# session start, takes at most twice that plus two minutes; usually far less.
budget_seconds = 60
# Of those, seconds for tests beyond the ones that use the changed code (0: never).
widen_seconds = 20

[static]
# Optional: linters/type checkers diffed against the session-start tree.
# Only diagnostics the session introduced are reported. Uncomment to enable.
{static}

[gate]
# "report": never block; leave a receipt and a short summary.
# "enforce": block the agent's stop on execution evidence (see docs/PLAN.md).
mode = "report"
# When enforcing, what the agent is sent back for: "unknown" (what it doesn't know or didn't say: broken tests,
# untested new code, tests it changed without telling you) or "all" (every finding; for unattended agents).
send_back = "unknown"

# [rules]
# How a rule's findings are treated: "block", "resolve" (fix or justify), "note", or "off".
# test-changed-to-pass = "resolve"
# untested-change = "note"
"""


@dataclass
class Config:
    test_command: Optional[str] = None
    budget_seconds: int = 60
    widen_seconds: int = 20                   # time for tests beyond the selected ones
    mode: str = "report"                      # report | enforce
    send_back: str = "unknown"                # unknown | all: what an enforcing gate sends the agent back for
    ruff: Optional[str] = None                # command that runs ruff, e.g. ".venv/bin/ruff"
    pyright: Optional[str] = None             # command that runs pyright
    static_budget_seconds: int = 60
    rules: dict[str, str] = field(default_factory=dict)   # rule -> block | resolve | note | off
    source: str = "defaults"                  # where the config was read from
    problems: list[str] = field(default_factory=list)

    @property
    def enforce(self) -> bool:
        return self.mode == "enforce"

    @property
    def check_seconds(self) -> int:
        """Upper bound for one whole check: the tests, their comparison with session start, the other engines."""
        return self.budget_seconds * 2 + self.static_budget_seconds + 60

    @property
    def hook_timeout(self) -> int:
        return self.check_seconds + 120


def parse(text: Optional[str], source: str) -> Config:
    cfg = Config(source=source if text is not None else "defaults")
    if text is None:
        return cfg
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        cfg.problems.append(f"{CONFIG_FILE} is not valid TOML ({e}); using defaults")
        return cfg
    def table(name: str) -> dict:
        value = data.get(name, {})
        if not isinstance(value, dict):
            cfg.problems.append(f"[{name}] isn't a table; ignored")
            return {}
        return value

    def seconds(where: dict, name: str, default: int) -> int:
        value = where.get(name, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            cfg.problems.append(f"{name} {value!r} isn't a number of seconds; using {default}")
            return default
        return int(value)

    def command(where: dict, name: str) -> Optional[str]:
        value = where.get(name) or ""
        if not isinstance(value, str):
            cfg.problems.append(f"{name} {value!r} isn't a command; ignored")
            return None
        return value.strip() or None

    test, gate, static = table("test"), table("gate"), table("static")
    cfg.test_command = command(test, "command")
    cfg.budget_seconds = seconds(test, "budget_seconds", cfg.budget_seconds)
    cfg.widen_seconds = seconds(test, "widen_seconds", cfg.widen_seconds)
    cfg.ruff, cfg.pyright = command(static, "ruff"), command(static, "pyright")
    cfg.static_budget_seconds = seconds(static, "budget_seconds", cfg.static_budget_seconds)
    for rule, severity in table("rules").items():
        if severity in RULE_SETTINGS:
            cfg.rules[rule] = severity
        else:
            cfg.problems.append(f"rules.{rule} {severity!r} isn't one of {', '.join(RULE_SETTINGS)}; ignored")
    mode = gate.get("mode", cfg.mode)
    if mode not in ("report", "enforce"):
        cfg.problems.append(f"gate.mode {mode!r} isn't report or enforce; using report")
        mode = "report"
    cfg.mode = mode
    cfg.send_back = gate.get("send_back", cfg.send_back)
    if cfg.send_back not in ("unknown", "all"):
        cfg.problems.append(f"gate.send_back {cfg.send_back!r} isn't unknown or all; using unknown")
        cfg.send_back = "unknown"
    return cfg


def load(root: str, tree: Optional[str] = None) -> Config:
    """Config from `tree` (a snapshot) if given, else from the working tree."""
    if tree is not None:
        return parse(snapshot.show(root, tree, CONFIG_FILE), f"{CONFIG_FILE} at session start")
    path = Path(root) / CONFIG_FILE
    return parse(path.read_text(errors="replace") if path.exists() else None, CONFIG_FILE)


def detect_test_command(root: str) -> Optional[str]:
    """A pytest command for this repo, if it looks like it uses pytest."""
    r = Path(root)
    uses_pytest = (
        (r / "pytest.ini").exists()
        or (r / "conftest.py").exists()
        or ("[tool.pytest" in _read(r / "pyproject.toml"))
        or ("[tool:pytest]" in _read(r / "setup.cfg"))
        or ("[pytest]" in _read(r / "tox.ini"))
        or any(r.glob("tests/test_*.py")) or any(r.glob("test/test_*.py"))
        or any(r.glob("tests/**/test_*.py"))
    )
    if not uses_pytest:
        return None
    for venv in (".venv", "venv", "env"):
        py = r / venv / "bin" / "python"
        if py.exists():
            return f"{venv}/bin/python -m pytest"
    return "python -m pytest"


def detect_static_tools(root: str) -> dict[str, str]:
    """ruff/pyright commands, for tools the repo configures and has installed."""
    r = Path(root)
    pyproject = _read(r / "pyproject.toml")
    configured = {
        "ruff": "[tool.ruff" in pyproject or (r / "ruff.toml").exists() or (r / ".ruff.toml").exists(),
        "pyright": "[tool.pyright" in pyproject or (r / "pyrightconfig.json").exists(),
    }
    found = {}
    for tool, wanted in configured.items():
        if not wanted:
            continue
        for venv in (".venv", "venv", "env"):
            if (r / venv / "bin" / tool).exists():
                found[tool] = f"{venv}/bin/{tool}"
                break
        else:
            if shutil.which(tool):
                found[tool] = tool
    return found


def render_template(root: str, command: str) -> str:
    tools = detect_static_tools(root)
    lines = [f'{t} = "{tools[t]}"' if t in tools else f'# {t} = "{t}"' for t in ("ruff", "pyright")]
    return TEMPLATE.format(command=command, static="\n".join(lines))


def _read(path: Path) -> str:
    try:
        return path.read_text()
    except OSError:
        return ""
