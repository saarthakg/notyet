"""Every test that passed at session start, accounted for.

When a session changes the test side (test modules, conftest.py, helpers, golden
files, pytest's configuration), passing tests no longer say what they said. So
the session-start test side is put back, whole, next to the session's code, and
run there:

                       session-start code        the session's code
  session-start tests  the baseline              this engine
  the session's tests  vacuous.py                the ordinary run

A test that passed at session start and, as it was written then,
  - fails on the new code, while the session's version passes
                          test-changed-to-pass (block): whatever made the difference, an edited
                          assertion, fixture, helper, golden file or hook, was made to accept the
                          new behavior;
  - is skipped now        test-disabled: block if it would fail, otherwise fix or justify;
  - is gone now           test-removed: block if it would fail, otherwise fix or justify. A test that
                          only moved or was renamed (same body, passing) isn't gone.

This is execution, not pattern matching: it doesn't matter how a test was made
to pass. What it can't see is source code that behaves differently under test
(engines/gaming.py looks for the common forms of that).

A test that already failed at session start gets the same question. If it still fails as it was
written, and the session's version passes (or is skipped, or gone), then the test was changed and
the code it asked for wasn't: failing-test-changed, fix or justify. (Sometimes the test was wrong.)

Names moving is not behavior changing: when a session-start module can't even be
imported on the new code (a renamed import), its unedited tests are judged by
their new run, and only its edited ones are raised, as fix-or-justify.
"""
import os
import re
import time
from collections import Counter
from dataclasses import dataclass, field

from notyet import snapshot, testrun
from notyet.engines.integrity import changed_functions, constants_of, test_bodies
from notyet.findings import Context, EngineResult, Finding

MAX_ITEMS = 10
BATCH = 12


@dataclass
class Side:
    """The session's changes to the test side."""
    modules: dict[str, str | None] = field(default_factory=dict)   # session-start path -> path now (None: deleted)
    support: list[str] = field(default_factory=list)               # changed helpers, conftests, data (now or then)
    config: list[str] = field(default_factory=list)                # files whose pytest configuration changed
    restore: list[str] = field(default_factory=list)               # session-start paths to put back
    remove: list[str] = field(default_factory=list)                # paths that didn't exist then

    @property
    def infra(self) -> list[str]:
        return self.support + self.config


def side_of(ctx: Context) -> Side:
    from notyet.engines.execution import _pytest_config_changed
    lay, side = ctx.layout, Side()
    for d in ctx.deltas:
        old = d.old_path or (d.path if d.status != "A" else None)
        new = d.path if d.status != "D" else None
        if any(p and _pytest_config_changed(ctx, p) for p in (old, new)):
            side.config.append(new or old or "")
        elif not any(p and lay.is_test_side(p) for p in (old, new)):
            continue
        elif old and lay.is_test_module(old):      # renamed to something pytest doesn't collect: gone
            side.modules[old] = new if new and lay.is_test_module(new) else None
        elif old or _can_interfere(ctx, d.path):     # an added file mostly only adds
            side.support.append(new or old or "")
        if old:
            side.restore.append(old)
        if new and new != old:
            side.remove.append(new)
    return side


def _can_interfere(ctx: Context, path: str) -> bool:
    """Can a new test-side file change what existing tests do? A conftest.py can, and so can a module that
    shadows one of the source's (tests/shop/ in front of src/shop/)."""
    parts = path.split("/")
    if parts[-1] == "conftest.py":
        return True
    if ctx.layout.is_test_module(path) or not path.endswith(".py"):
        return False
    source = {q for p in ctx.layout.paths if p.endswith("/__init__.py") and not ctx.layout.is_test_side(p)
              for q in p.split("/")[:-1]}
    return bool(({parts[-1].removesuffix(".py")} | set(parts[:-1])) & source)


def impacted(ctx: Context, side: Side, selected: list[str]) -> list[str]:
    """Session-start test modules whose tests the changed test side decides, most direct first."""
    from notyet.engines.execution import _support_scope
    then = {p for p in snapshot.git(ctx.root, "ls-tree", "-r", "-z", "--name-only", ctx.baseline_tree).split("\0")
            if p} & (ctx.layout.tests() | set(side.modules))
    out = [p for p in side.modules if p in then]
    if side.infra:
        scope = {t for p in side.support for t in _support_scope(ctx.layout, p)}
        if side.config:
            scope |= ctx.layout.tests()
        moved = {new: old for old, new in side.modules.items() if new}
        ordered = [t for t in selected if t in scope] + sorted(scope - set(selected))
        out += [moved.get(t, t) for t in ordered]
    return [p for p in dict.fromkeys(out) if p in then]


def run(ctx: Context, command: str, current: testrun.Run, unfinished: set[str], selected: list[str],
        result: EngineResult) -> None:
    from notyet.engines.execution import passed_at_session_start, source_path_env
    side = side_of(ctx)
    if not side.modules and not side.infra:
        return
    # a file this check had no time for now can't be judged by its old version either
    files = [f for f in impacted(ctx, side, selected) if side.modules.get(f, f) not in unfinished]
    if not files:
        return
    started = time.monotonic()
    limit = max(30.0, min(ctx.config.budget_seconds, ctx.time_left() - 20))
    old = testrun.Run()
    cut: list[str] = []
    crash = ""
    hooks = [p for p in side.infra if p in side.config or os.path.basename(p) == "conftest.py"]
    dropped: set[str] = set()
    with snapshot.checkout(ctx.root, ctx.current_tree, "oldtests") as tmp:
        for p in side.remove:
            if os.path.lexists(os.path.join(tmp, p)):
                os.remove(os.path.join(tmp, p))
        snapshot.materialize(ctx.root, ctx.baseline_tree, tmp, paths=side.restore)
        env = source_path_env(tmp)
        queue = [files[i:i + BATCH] for i in range(0, len(files), BATCH)]
        cannot_run: list[str] = []
        while queue:
            batch = queue.pop(0)
            left = limit - (time.monotonic() - started)
            if left < 2:
                cut += batch
                continue
            r = testrun.run_pytest(command, tmp, batch, timeout=ctx.cap(left), env=env)
            if r.crashed and len(batch) > 1:       # one bad conftest takes its whole run down: go file by file
                queue = [[f] for f in batch] + queue
                continue
            if r.crashed:
                crash = r.crashed
                cannot_run += batch
                continue
            if r.timed_out:
                cut += [f for f in batch if f not in {n.split("::")[0] for n in r.results}]
            old.results.update(r.results)
        shaky = [r.nodeid for r in old.failures if "::" in r.nodeid][:200]
        if shaky:      # fail twice before it counts
            again = testrun.run_pytest(command, tmp, shaky, timeout=ctx.cap(limit), env=env)
            for nid in shaky:
                if nid in again.results and not again.results[nid].bad:
                    old.results[nid] = again.results[nid]
        if hooks and ctx.time_left() > 30:      # does a bare `pytest` still find them?
            then_ids = testrun.collect(command, tmp, [], env=env, timeout=ctx.cap(90))
            now_ids = testrun.collect(command, ctx.root, [], env=source_path_env(ctx.root), timeout=ctx.cap(90))
            if then_ids is None or now_ids is None:
                result.not_checked.append(f"test collection with {', '.join(hooks)} changed: couldn't collect the suite "
                                          f"{'now' if now_ids is None else 'with the session-start test setup'}")
            else:
                now_fns = {testrun.function_of(n) for n in now_ids}
                for n in then_ids:
                    then, _, name = testrun.function_of(n).partition("::")
                    now = side.modules.get(then, then)        # by the name its file has now
                    if now is not None and f"{now}::{name}" not in now_fns:
                        dropped.add(f"{then}::{name}")
    if cut:
        result.gaps.append(f"session-start tests incomplete on the new code: {len(cut)} of {len(files)} test file(s) "
                           f"didn't run in time: {', '.join(cut[:5])}")
    if cannot_run:
        _support_broken(side, crash, cannot_run, current, result)
        files = [f for f in files if f not in cannot_run]
    if not old.results:
        return

    uncollected = [f for f in files if f in old.results and old.results[f].bad]
    bad = [r.nodeid for r in old.failures if "::" in r.nodeid]
    passed_before, why = passed_at_session_start(ctx, command, bad, uncollected)
    if why:
        uncollected = []
    if why:
        result.not_checked.append(f"session-start versions of changed tests: {why}")

    # ── one verdict per test function ────────────────────────────────────────
    fails = {testrun.function_of(n): n for n in bad if n in passed_before or why}
    holds = {testrun.function_of(n) for n, r in old.results.items() if r.outcome == "passed"} - set(fails)
    # in a module that can't be imported any more, what it held is known from session start
    lost = {testrun.function_of(n) for n in passed_before if n.split("::")[0] in uncollected}
    edits: dict[str, dict[str, str]] = {}
    for then in files:
        now = side.modules.get(then, then)
        edits[then] = {} if now is None else changed_functions(
            snapshot.show(ctx.root, ctx.baseline_tree, then) or "", snapshot.show(ctx.root, ctx.current_tree, now) or "")
    moved = _moved(ctx, side, current)

    now_fns = {testrun.function_of(n) for n in current.results if "::" in n}
    known = {n.split("::")[0] for n in current.results} - unfinished
    never_fixed: list[tuple[str, str]] = []
    already = {testrun.function_of(n) for n in bad} - set(fails)
    by_support: list[str] = []
    skip_to_confirm: list[str] = []
    tally: Counter = Counter()
    for fn in sorted(set(fails) | holds | lost):
        then, _, name = fn.partition("::")
        now_file = side.modules.get(then, then)
        here = f"{now_file}::{name}" if now_file else None
        # run in this check, or collected by nothing pytest runs any more
        never_runs = fn in dropped or now_file is None or not ctx.layout.in_testpaths(now_file) \
            or now_file in ctx.shared.get("ran_nothing", ())
        if now_file not in known and not never_runs:
            continue
        now = "missing" if here is None or fn in dropped else testrun.outcome_of(here, current.results)
        edited = edits[then].get(name) == "edited"
        evidence = _evidence(old.results, fails.get(fn), then)
        if now == "failed":
            tally["failing"] += 1         # reported as a regression by the ordinary run
        elif fn in holds and now == "passed":
            tally["as written"] += 1
        elif now == "missing" and moved.get(fn) and fn not in fails:
            tally["moved"] += 1
        elif now == "missing":
            _removed(fn, now_file, here in now_fns, fn in holds, side, evidence, result)
            tally["gone"] += 1
        elif now == "skipped":
            if fn in holds and not edited and not side.infra:
                continue                  # an untouched test skipping here and not there is the environment
            if fn in holds and not edited:
                skip_to_confirm.append(fn)
                continue
            _disabled(fn, here, fn in holds, evidence, result)
            tally["switched off"] += 1
        elif fn in lost and fn not in fails:
            if edited:
                result.findings.append(Finding(
                    rule="test-changed-to-pass", severity="resolve", location=here or fn,
                    title=f"{here} was edited, and its session-start version can't be imported on the new code, so "
                          f"whether its expectations were loosened can't be checked by running it", evidence=evidence,
                    action="Check that only names changed, not what the test expects; acknowledge that, or restore "
                           "the expectations."))
            tally["import moved"] += 1
        elif fn in fails and edited and _cannot_call(evidence) and not _lost_constants(ctx, then, now_file, name):
            result.findings.append(Finding(
                rule="test-changed-to-pass", severity="resolve", location=here or fn, evidence=evidence,
                title=f"{here} was edited, and its session-start version can't call the new code (names or signatures "
                      f"changed); the values it checks are unchanged",
                action="If the old way of calling it was meant to keep working, restore it; otherwise acknowledge the change."))
            tally["call changed"] += 1
        elif fn in fails and (edited or not side.infra):
            result.findings.append(Finding(
                rule="test-changed-to-pass", severity="block" if not why else "resolve", location=here or fn,
                title=f"{here} as it was at session start fails on the new code; "
                      + ("the test was edited instead" if edited else f"changes elsewhere in {now_file} make it pass")
                      + (" (couldn't confirm it passed at session start)" if why else ""),
                evidence=evidence,
                action="Fix the code so the original test passes. If the behavior change was asked for, "
                       "hand it to the user: `ack <id> --needs-human \"the change that was requested\"`."))
            tally["changed"] += 1
        elif fn in fails:
            by_support.append(fn)
            tally["changed"] += 1

    for fn in sorted(already):
        then, _, name = fn.partition("::")
        now_file = side.modules.get(then, then)
        if now_file is not None and now_file not in known and fn not in dropped:
            continue
        now = "missing" if now_file is None or fn in dropped else testrun.outcome_of(f"{now_file}::{name}", current.results)
        if now != "failed":
            never_fixed.append((fn, {"passed": "passes", "skipped": "is skipped", "missing": "is gone"}[now]))
    if never_fixed:
        first, how = never_fixed[0]
        rest = f" (and {len(never_fixed) - 1} more like it)" if len(never_fixed) > 1 else ""
        result.findings.append(Finding(
            rule="failing-test-changed", severity="resolve", location=first, key="|".join(fn for fn, _ in never_fixed),
            title=f"{first} failed at session start and {how} now, but as it was written then it still fails on the new "
                  f"code{rest}",
            evidence=_evidence(old.results, next((n for n in bad if testrun.function_of(n) == first), None), first)
            + [fn for fn, _ in never_fixed[1:4]],
            action="Fix the code so the original test passes. If the test itself was wrong, acknowledge that and say why."))
        tally["failing before, only the test changed"] += len(never_fixed)

    _group_edited(result)
    confirmed = _skips_here(ctx, command, skip_to_confirm)
    for fn in skip_to_confirm:
        if fn in confirmed:
            _disabled(fn, fn, True, [], result)
            tally["switched off"] += 1
    if by_support:
        # a setup that can't construct or import something any more says names moved, not that behavior did
        calls = all(_cannot_call(_evidence(old.results, fails.get(fn), fn)) for fn in by_support)
        shown = ", ".join(side.infra[:4]) + (" …" if len(side.infra) > 4 else "")
        result.findings.append(Finding(
            rule="test-changed-to-pass", severity="block" if not why and not calls else "resolve",
            location=by_support[0] if len(by_support) == 1 else side.infra[0],
            key="support|" + "|".join(by_support[:50]),
            title=f"{len(by_support)} test(s) pass only with this session's changes to {shown}; with the session-start "
                  f"test setup they " + ("can't call the new code (names or signatures changed)" if calls else "fail on the new code"),
            evidence=[f"{fn}: {(_evidence(old.results, fails.get(fn), fn) or [''])[0][:140]}" for fn in by_support[:3]],
            action="If the old way of calling it was meant to keep working, restore it; otherwise acknowledge the change."
            if calls else "Fix the code so they pass with the original setup. If the behavior change was asked for, hand it "
                   "to the user: `ack <id> --needs-human \"the change that was requested\"`."))
    if tally:
        result.checks.append(
            f"session-start tests on the new code: {sum(tally.values())} test(s): "
            + ", ".join(f"{n} {k}" for k, n in tally.most_common()))
    ctx.shared["crosscheck"] = {f.location for f in result.findings if f.rule == "test-disabled"}


_CALL_ERROR = re.compile(r"^(TypeError: .*(positional argument|keyword argument|required argument)"
                         r"|AttributeError: .*has no attribute|ImportError|ModuleNotFoundError|NameError: name)")


_AT_SETUP = re.compile(r'^failed on (?:setup|teardown) with "')


def _cannot_call(evidence: list[str]) -> bool:
    return bool(evidence) and bool(_CALL_ERROR.match(_AT_SETUP.sub("", evidence[0])))


def _lost_constants(ctx: Context, then: str, now: str | None, name: str) -> bool:
    """Is a value the test used to mention (an input or an expectation) gone from it now?"""
    before = constants_of(snapshot.show(ctx.root, ctx.baseline_tree, then) or "", name)
    after = constants_of(snapshot.show(ctx.root, ctx.current_tree, now) or "" if now else "", name)
    return bool(before - after)


def _group_edited(result: EngineResult) -> None:
    """Past a handful of edited tests, one item per file and level: the same decision, asked once."""
    mine = [f for f in result.findings if f.rule == "test-changed-to-pass" and "::" in f.location]
    if len(mine) <= MAX_ITEMS // 2:
        return
    by_file: dict[tuple[str, str], list[Finding]] = {}
    for f in mine:
        by_file.setdefault((f.location.split("::")[0], f.severity), []).append(f)
    for (path, severity), group in by_file.items():
        if len(group) == 1:
            continue
        names = [f.location.split("::", 1)[1] for f in group]
        result.findings = [f for f in result.findings if f not in group]
        result.findings.append(Finding(
            rule="test-changed-to-pass", severity=severity, location=path, key=f"{path}|" + "|".join(sorted(names)),
            title=f"{len(group)} edited tests in {path} fail on the new code as they were at session start"
                  if severity == "block" else f"{len(group)} edited tests in {path} can't be run on the new code as they were",
            evidence=[f"{n}: {(f.evidence or [''])[0][:120]}" for n, f in list(zip(names, group))[:4]]
            + ([f"and: {', '.join(names[4:16])}"] if len(names) > 4 else []),
            action=group[0].action))


def _removed(fn: str, now_file, still_there: bool, holds: bool, side: Side, evidence: list[str], result: EngineResult) -> None:
    where = " (its file was deleted)" if now_file is None else \
            f" (file renamed to {now_file})" if now_file != fn.split("::")[0] else \
            f" ({', '.join(side.infra[:3])} changed)" if side.infra and fn.split("::")[0] not in side.modules else ""
    if holds:
        result.findings.append(Finding(
            rule="test-removed", severity="resolve", location=fn, evidence=evidence,
            title=f"{fn} existed at session start and is gone now{where}; it would still pass",
            action="Restore it, or acknowledge what covers the same behavior now."))
    else:
        result.findings.append(Finding(
            rule="test-removed", severity="block", location=fn, evidence=evidence,
            title=f"{fn} existed at session start and is gone now{where}; it passed then and fails on the new code",
            action="Restore it and fix the code, or if the behavior was removed on request, hand it to the user: "
                   "`ack <id> --needs-human \"why\"`."))


def _disabled(fn: str, here, holds: bool, evidence: list[str], result: EngineResult) -> None:
    result.findings.append(Finding(
        rule="test-disabled", severity="resolve" if holds else "block", location=here or fn, evidence=evidence,
        title=f"{here or fn} ran at session start and is skipped now; "
              + ("it would still pass" if holds else "as it was then, it fails on the new code"),
        action="Remove the skip/xfail." if holds else
               "Remove the skip/xfail and fix the code. If the test should really be off, hand it to the user: "
               "`ack <id> --needs-human \"why\"`."))


def _support_broken(side: Side, crash: str, files: list[str], current: testrun.Run, result: EngineResult) -> None:
    """The session-start test setup doesn't even start on the new code (its conftest imports something gone)."""
    moved = {old: new for old, new in side.modules.items()}
    if any(r.bad and n.split("::")[0] in {moved.get(f, f) for f in files} for n, r in current.results.items()):
        return                            # these files' failures are already reported
    result.findings.append(Finding(
        rule="test-changed-to-pass", severity="resolve", location=(side.infra or files)[0],
        key="setup|" + "|".join(sorted(side.infra or files)),
        title=f"the session-start versions of {', '.join((side.infra or files)[:3])} can't run on the new code, so "
              f"whether this session's versions are looser can't be checked by running them",
        evidence=[crash[:300]],
        action="Check that only names changed, not what the tests expect, and acknowledge that."))


def _evidence(results: dict[str, testrun.TestResult], nodeid: str | None, file: str) -> list[str]:
    r = results.get(nodeid) if nodeid else results.get(file)
    return [line for line in r.message.splitlines() if line.strip()][:3] if r and r.bad else []


def _moved(ctx: Context, side: Side, current: testrun.Run) -> dict[str, bool]:
    """Session-start tests (by node id) whose exact body is a new, passing test now."""
    new_bodies: Counter = Counter()
    for d in ctx.deltas:
        if d.status == "D" or not ctx.layout.is_test_module(d.path):
            continue
        before = snapshot.show(ctx.root, ctx.baseline_tree, d.old_path or d.path) or "" if d.status != "A" else ""
        after = snapshot.show(ctx.root, ctx.current_tree, d.path) or ""
        fresh = {n for n, kind in changed_functions(before, after).items() if kind == "new"}
        for name, body in test_bodies(after).items():
            if name in fresh and testrun.outcome_of(f"{d.path}::{name}", current.results) == "passed":
                new_bodies[body] += 1
    out = {}
    for then, now in side.modules.items():
        before = snapshot.show(ctx.root, ctx.baseline_tree, then) or ""
        still = set(test_bodies(snapshot.show(ctx.root, ctx.current_tree, now) or "")) if now else set()
        for name, body in test_bodies(before).items():
            if name not in still and new_bodies[body] > 0:
                new_bodies[body] -= 1
                out[f"{then}::{name}"] = True
    return out


def _skips_here(ctx: Context, command: str, fns: list[str]) -> set[str]:
    """Of tests that skip in the working tree, the ones that also skip in a checkout of it: the session's
    doing, not a difference between the two directories."""
    if not fns or ctx.time_left() < 10:
        return set()
    from notyet.engines.execution import source_path_env
    with snapshot.checkout(ctx.root, ctx.current_tree, "newtests") as tmp:
        env = source_path_env(tmp)
        got = testrun.run_tests(command, tmp, fns, env=env, timeout=ctx.cap(max(30, ctx.config.budget_seconds / 2)))
    return {f for f in fns if testrun.outcome_of(f, got.results) == "skipped"}
