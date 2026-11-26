"""The gate: run the engines on the session's change and decide whether the
agent may stop.

Loop safety (docs/PLAN.md §3.6):
- engines run only when the tree changed since the last check; an unchanged
  tree is re-decided from the saved result and the current acknowledgments;
- a finding set that comes back unchanged is blocked at most twice, then the
  stop is allowed and the receipt leads with "unresolved";
- never more than MAX_BLOCKS consecutive blocks (Claude Code's own cap is 8);
- never blocks while background work is running.
"""
import re
import time
import traceback
from dataclasses import asdict, dataclass, field, replace
from typing import Callable, Optional

from notyet import config as config_mod
from notyet import receipt as receipt_mod
from notyet import plan as plan_mod
from notyet import snapshot, store
from notyet.disclosure import disclosed
from notyet.source import printable
from notyet.findings import Context, EngineResult, Finding, find_ack, group

MAX_BLOCKS = 6
SEVERITY_ORDER = {"block": 0, "resolve": 1, "note": 2}
MAX_SAME_SET_BLOCKS = 2

# What an agent can't know or hasn't said is worth sending it back for. The rest is for the user to see: interrupting
# for it mostly produced acknowledgments, and once worse code (docs/EVAL.md §6).
SEND_BACK = {"untested-change", "test-failing", "tests-failing-unverified", "type-error-new", "test-not-collected",
             "failing-test-changed", "test-removed", "test-disabled", "assertions-removed",      # when not told to the user
             "source-detects-tests", "always-equal",
             # unfinished work: the most reported way a "done" isn't (docs/RESEARCH.md); sent back unless the closing
             # message says what's left
             "plan-unfinished", "todo-added", "placeholder", "debugger-left", "debug-print", "unwired-code",
             "asked-missing", "docs-missing"}
TEST_CHANGES = {"test-changed-to-pass", "test-removed", "test-disabled", "failing-test-changed", "assertions-removed"}
HANDED_OVER = ("needs-human", "disclosed")
# what the agent can hand to the user by saying it in its closing message
SAID_LEFT = {"plan-unfinished", "todo-added", "asked-missing", "docs-missing"}
# handed over, the user has to look before calling the work done
REVIEW = {"failing-test-changed", "plan-unfinished", "asked-missing", "docs-missing"}

Engine = Callable[[Context], EngineResult]


def default_engines() -> list[Engine]:
    from notyet.engines import asked, execution, finish, gaming, history, integrity, static, undone
    return [execution.run, integrity.run, gaming.run, finish.run, asked.run, static.run, undone.run, history.run]


@dataclass
class Decision:
    verdict: str        # passed | incomplete | needs-review | blocked | reported | unresolved | not-checked | no-change
    block_reason: Optional[str] = None    # text for the agent when blocking
    summary: str = ""                     # short text for the user
    receipt_path: Optional[str] = None
    findings: list[Finding] = field(default_factory=list)


def handed_over_now(session: store.Session, ack: Optional[dict]) -> bool:
    """Was this item handed to the user in the current turn? One from an earlier turn was on the receipt and in the
    summary the user saw before prompting again: it stays listed, but it isn't news about this turn."""
    if not ack or ack.get("category") not in HANDED_OVER:
        return False
    return not session.turns or ack.get("at", float("inf")) >= session.turns[-1]["at"]


def outstanding(findings: list[Finding], acks: dict[str, dict]) -> list[Finding]:
    """Findings that still stand: blocks not marked needs-human, resolves not acknowledged."""
    out = []
    for f in findings:
        ack = find_ack(f, acks)
        if f.severity == "block" and not (ack and ack.get("category") in HANDED_OVER):
            out.append(f)
        elif f.severity == "resolve" and not ack:
            out.append(f)
    return out


def run_engines(ctx: Context, engines: Optional[list[Engine]] = None) -> EngineResult:
    result = EngineResult()
    for engine in (engines if engines is not None else default_engines()):
        if ctx.time_left() < 5:
            name = getattr(engine, "__module__", "engine").rsplit(".", 1)[-1]
            result.not_checked.append(f"{name}: skipped; the check used its {ctx.config.check_seconds}s limit")
            continue
        started = time.monotonic()
        try:
            result.extend(engine(ctx))
        except Exception as e:  # an engine failure is reported, never fatal
            name = getattr(engine, "__module__", "engine").rsplit(".", 1)[-1]
            store.log_error(ctx.root, f"{name}: {e}\n{traceback.format_exc()}")
            result.gaps.append(f"the {name} check failed: {e}")
        name = getattr(engine, "__module__", "engine").rsplit(".", 1)[-1]
        if name != "execution":      # times its own parts
            result.timings[name] = result.timings.get(name, 0.0) + time.monotonic() - started
    for f in result.findings:       # text from the repo can hold bytes that aren't UTF-8
        f.title, f.location = printable(f.title), printable(f.location)
        f.evidence, f.diff = [printable(e) for e in f.evidence], [printable(e) for e in f.diff]
    return result


def check(root: str, session: store.Session, engines: Optional[list[Engine]] = None,
          background_busy: bool = False, cfg: Optional[config_mod.Config] = None, message: str = "",
          transcript: Optional[str] = None) -> Decision:
    """`message`: what the agent told the user as it tried to stop. `transcript`: the session's transcript file,
    for the agent's own plan."""
    if background_busy:
        return Decision(verdict="no-change")  # the session isn't done; check when it is
    current = snapshot.snapshot(root)
    planned, plan_facts = plan_mod.findings(plan_mod.read(transcript))
    # back where the session started: nothing to check, unless this turn got there by undoing earlier ones,
    # or the agent's own plan says it isn't done
    undid = bool(session.turns) and session.turns[-1]["tree"] != current
    unchanged = current == session.baseline_tree and not undid
    previous = session.runs[-1] if session.runs else None
    again = previous is not None and previous.tree == current and previous.verdict == "blocked"
    open_plan = any(f.rule == "plan-unfinished" for f in planned)
    if unchanged and (not open_plan or not again and not _plan_news(session, planned, previous)):
        return Decision(verdict="no-change")
    cfg = cfg or config_mod.load(root, session.baseline_tree)
    started = time.monotonic()
    ctx = Context(root=root, session=session, config=cfg, baseline_tree=session.baseline_tree,
                  deadline=time.monotonic() + cfg.check_seconds,
                  current_tree=current, deltas=snapshot.diff_trees(root, session.baseline_tree, current))

    if unchanged:
        result = EngineResult()
    elif previous is not None and previous.tree == current:
        if previous.verdict != "blocked" and not _plan_news(session, planned, previous):
            return Decision(verdict="no-change")      # this exact tree was already checked
        result = _result_from(previous.result)       # re-decide with current acknowledgments and plan
    else:
        try:
            result = run_engines(ctx, engines)
        finally:
            ctx.close()
        result.findings = _as_configured(result.findings, cfg.rules)
        result.not_checked = cfg.problems + result.not_checked
        live = config_mod.load(root)
        if _gate_settings(live) != _gate_settings(cfg):
            result.advice.append(f"{config_mod.CONFIG_FILE} changed during this session; "
                                 f"the gate used the version from session start")

    result.findings = [f for f in result.findings if f.rule not in PLAN_RULES] + \
        _as_configured(planned, cfg.rules)
    result.facts = {k: v for k, v in result.facts.items() if not k.startswith("plan")} | plan_facts
    result.findings.sort(key=lambda f: SEVERITY_ORDER.get(f.severity, 9))   # blocks first, for agent and receipt
    for f in result.findings:       # already told to the user: theirs to review, no reason to send the agent back
        if f.severity == "note" or find_ack(f, session.acks):
            continue
        if f.rule in TEST_CHANGES and (said := disclosed(message, f.tests or ([f.location] if "::" in f.location else []), f.diff)):
            session.acks[f.id] = {"category": "disclosed", "reason": said, "at": time.time(), "title": f.title,
                                  "rule": f.rule, "tests": f.tests or [f.location]}
        elif f.rule in SAID_LEFT and (said := plan_mod.mentioned(_docs_as_one_word(message) if f.rule == "docs-missing"
                                                                 else message, _left_items(f))):
            session.acks[f.id] = {"category": "disclosed", "reason": said, "at": time.time(), "title": f.title}
            if f.covers:
                session.acks[f.id].update(group=group(f), covers=f.covers)
    standing = outstanding(result.findings, session.acks)
    send = [f for f in standing if cfg.send_back == "all" or f.severity == "block" or f.rule in SEND_BACK]
    repeats = _same_set_streak(session, [f.id for f in send])

    if not standing and not result.checks and not result.gaps:
        verdict = "not-checked"
    elif not send and any((f.severity == "block" or f.rule in REVIEW)
                          and handed_over_now(session, find_ack(f, session.acks)) for f in result.findings):
        verdict = "needs-review"      # the agent may stop, but this is not a pass
    elif not send:
        verdict = "incomplete" if result.gaps else "passed"     # nothing wrong in what ran isn't a pass
    elif not cfg.enforce:
        verdict = "reported"
    elif repeats >= MAX_SAME_SET_BLOCKS or session.consecutive_blocks >= MAX_BLOCKS:
        verdict = "unresolved"
    else:
        verdict = "blocked"

    path = receipt_mod.write(root, session, ctx, result, standing, verdict)
    session.runs.append(store.GateRun(
        tree=current, at=time.time(), verdict=verdict, finding_ids=[f.id for f in standing],
        sent_ids=[f.id for f in send], receipt=path, result=_result_to(result), seconds=round(time.monotonic() - started, 1)))
    session.consecutive_blocks = session.consecutive_blocks + 1 if verdict == "blocked" else 0
    store.save_session(root, session)

    decision = Decision(verdict=verdict, receipt_path=path, findings=result.findings,
                        summary=receipt_mod.summary(verdict, session, result, standing, path))
    if verdict == "blocked":
        decision.block_reason = receipt_mod.agent_message(send, last_chance=repeats + 1 >= MAX_SAME_SET_BLOCKS,
                                                            advice=result.advice)
    return decision


PLAN_RULES = {"plan-unfinished", "plan-dropped"}


def _plan_news(session: store.Session, planned: list[Finding], previous: Optional[store.GateRun]) -> bool:
    """Is there something in the plan that the last check didn't already decide?"""
    open_ = {f.id for f in planned if f.rule == "plan-unfinished"}
    return bool(open_) and (previous is None or not open_ <= set(previous.finding_ids) | set(session.acks))


def _docs_as_one_word(message: str) -> str:
    return re.sub(r"\b(readme|documentation|changelog|design notes|decisions(\.md)?)\b", "docs", message, flags=re.IGNORECASE)


def _left_items(f: Finding) -> list[str]:
    """What a closing message has to name to hand `f` over: each unfinished step, or the TODO comments."""
    if f.rule == "docs-missing":
        return ["docs"]
    if f.rule == "asked-missing":
        return [e.strip("`") for e in f.evidence]
    texts = [e.split(": ", 1)[-1] for e in f.evidence]
    if f.rule == "todo-added":
        return [re.sub(r".*?\b(TODO|FIXME|XXX|HACK)\b[:\s]*", "", t) or t for t in texts]
    return texts


def _as_configured(findings: list[Finding], rules: dict[str, str]) -> list[Finding]:
    out = []
    for f in findings:
        setting = rules.get(f.rule, f.severity)
        if setting == "off":
            continue
        # a finding already at note level is information (a flaky test, say); a setting can lower a rule, not raise those
        out.append(f if f.severity == "note" else replace(f, severity=setting))
    return out


def _gate_settings(cfg: config_mod.Config) -> tuple:
    return (cfg.test_command, cfg.mode, cfg.budget_seconds, cfg.widen_seconds, cfg.ruff, cfg.pyright, cfg.static_budget_seconds, cfg.send_back,
            tuple(sorted(cfg.rules.items())))


def _same_set_streak(session: store.Session, ids: list[str]) -> int:
    """How many of the latest runs in a row were blocked with exactly this finding set."""
    want, n = sorted(ids), 0
    for run in reversed(session.runs):
        if run.verdict == "blocked" and sorted(run.sent_ids) == want:
            n += 1
        else:
            break
    return n


def _result_to(result: EngineResult) -> dict:
    return {"findings": [asdict(f) for f in result.findings], "checks": result.checks,
            "not_checked": result.not_checked, "advice": result.advice, "gaps": result.gaps,
            "timings": {k: round(v, 1) for k, v in result.timings.items()}, "facts": result.facts}


def _result_from(data: dict) -> EngineResult:
    return EngineResult(findings=[Finding(**f) for f in data.get("findings", [])],
                        checks=data.get("checks", []), not_checked=data.get("not_checked", []),
                        advice=data.get("advice", []), gaps=data.get("gaps", []), timings=data.get("timings", {}), facts=data.get("facts", {}))
