"""Execution evidence: run the tests that exercise the change, and compare
failures with the tree as it was at session start.

Selection, most relevant first:
  1. test files the change added or modified;
  2. test files importing a changed module, directly or through other modules;
  3. test files named after a changed module (test_<name>.py);
  4. for a changed conftest.py or test helper, every test file under its directory.

Every failure is re-run once on the current tree (a pass means flaky, never a
block), then run on an isolated checkout of the session-start tree. A canary
test proves that checkout imports its own code rather than the working tree
(editable installs can silently redirect imports); without that proof a
failure can't be called a regression, and it's reported as fix-or-justify.

The session's new and edited tests are also run against the session-start
code, to see whether any of them tests the change (engines/vacuous.py).
"""
import ast
import hashlib
import json
import os
import tempfile
import time
from pathlib import PurePosixPath

from notyet import coverage, layout, pyimports, snapshot, store, testrun
from notyet.findings import Context, EngineResult, Finding
from notyet.layout import in_test_dir, is_test_module  # noqa: F401  (the eval scripts import them from here)
from notyet.pyresolve import find_python_source_roots

DOC_SUFFIXES = (".md", ".rst", ".txt", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico")
BATCH_FILES = 12
MAX_UNVERIFIED_ITEMS = 5
MAX_REGRESSION_ITEMS = 8
CANARY = "test_notyet_baseline_canary"


def source_path_env(root: str, env: dict | None = None) -> dict:
    """`env` with PYTHONPATH led by the checkout's own source roots. The
    session-start checkout is always run this way; running the working tree
    the same way means both sides import alike, and a venv whose install
    broke (or points elsewhere) mid-session isn't blamed on the session."""
    env = dict(env or {})
    rest = env.get("PYTHONPATH")
    env["PYTHONPATH"] = os.pathsep.join([str(r) for r in find_python_source_roots(root)] + ([rest] if rest else []))
    return env


def select_tests(ctx: Context, graph: pyimports.ImportGraph) -> tuple[list[tuple[str, int, str]], list[str]]:
    """[(test file, priority, why)] and notes about the selection."""
    notes: list[str] = []
    lay = ctx.layout
    current_tests = lay.tests(graph.blobs)     # what the repo's own pytest setup would run
    picked: dict[str, tuple[int, str]] = {}

    def pick(path: str, priority: int, why: str) -> None:
        if path in current_tests and (path not in picked or picked[path][0] > priority):
            picked[path] = (priority, why)

    changed = [d for d in ctx.deltas if d.status != "D"]
    changed_src = [d.path for d in changed if d.path.endswith(".py") and not lay.is_test_module(d.path)]
    gone = [p for d in ctx.deltas for p in [d.path if d.status == "D" else d.old_path] if p]

    # data, templates, config: whatever names the file is using it
    other = [p for p in [d.path for d in changed] + gone if not p.endswith(".py") and not p.endswith(DOC_SUFFIXES)]
    users: set[str] = set()
    if other:
        names = {PurePosixPath(o).name: o for o in other}
        for rel in graph.blobs:
            try:
                with open(os.path.join(ctx.root, rel), encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except OSError:
                continue
            for name in names:
                if name in text:
                    pick(rel, 1, f"mentions {name}")
                    users.add(rel)

    for d in changed:
        if lay.is_test_module(d.path):
            pick(d.path, 0, "changed test")
    for path, depth in graph.importers(changed_src + sorted(users)).items():
        pick(path, depth, "imports a changed module" if depth == 1 else f"imports a changed module ({depth} steps)")
    gone_py = [p for p in gone if p.endswith(".py") and not lay.is_test_module(p)]
    if gone_py:     # what used them can only be seen in the session-start tree
        before = pyimports.ImportGraph(ctx.root, ctx.baseline_tree, checkout=ctx.baseline_checkout())
        now_at = {d.old_path: d.path for d in ctx.deltas if d.old_path}
        for path, depth in before.importers(gone_py).items():
            pick(now_at.get(path, path), depth, "used a module that was removed or renamed")
    stems = {PurePosixPath(p).stem for p in changed_src + gone_py} - {"__init__"}
    for t in current_tests:
        stem = PurePosixPath(t).stem
        if stem.removeprefix("test_").removesuffix("_test") in stems:
            pick(t, 1, "named after a changed module")
    support = [p for p in [d.path for d in changed] + gone if lay.is_test_side(p) and not lay.is_test_module(p)]
    for path in support:
        for t in _support_scope(lay, path):
            pick(t, 2, f"under changed {PurePosixPath(path).name}")
    if any(_pytest_config_changed(ctx, p) for p in other):
        for t in current_tests:
            pick(t, 3, "pytest's configuration changed")
    ordered = sorted(((p, pr, why) for p, (pr, why) in picked.items()), key=lambda x: (x[1], x[0]))
    return ordered, notes


def _pytest_config_changed(ctx: Context, path: str) -> bool:
    return layout.config_changed(lambda p: snapshot.show(ctx.root, ctx.baseline_tree, p),
                                 lambda p: snapshot.show(ctx.root, ctx.current_tree, p), path)


def _support_scope(lay: layout.Layout, path: str) -> list[str]:
    """Test modules a test-support file may serve: the ones under its directory, or, when there are none
    (tests/data/x.json), under the nearest directory above that has some."""
    tests = lay.tests()
    folder = os.path.dirname(path)
    while True:
        here = sorted(t for t in tests if not folder or t.startswith(folder + "/"))
        if here or not folder:
            return here
        folder = os.path.dirname(folder)


def _batches(tests: list[tuple[str, int, str]]) -> list[list[str]]:
    """The most relevant files first, in one batch; the rest in chunks, so a
    time budget cut still leaves complete results for what ran."""
    # in path order, as a full run would have them: some tests only pass after others have run
    first = sorted(t for t, pr, _ in tests if pr <= 1)
    rest = [t for t, pr, _ in tests if pr > 1]
    batches = [first] if first else []
    batches += [sorted(rest[i:i + BATCH_FILES]) for i in range(0, len(rest), BATCH_FILES)]
    return batches


def run(ctx: Context) -> EngineResult:
    result = EngineResult()
    cfg, lay = ctx.config, ctx.layout
    if not ctx.deltas:
        result.checks.append("tests: not needed; the code is as it was at session start")
        return result
    if not cfg.test_command:
        result.not_checked.append("tests: no test command configured (run `notyet init`)")
        return result
    if all(d.path.endswith(DOC_SUFFIXES) for d in ctx.deltas):
        result.checks.append("tests: not needed; only documentation changed")
        return result

    command = testrun.anchored(cfg.test_command, ctx.root)
    started = time.monotonic()
    graph = pyimports.ImportGraph(ctx.root, ctx.current_tree)
    tests, notes = select_tests(ctx, graph)
    result.not_checked += notes
    changed_src = [d.path for d in ctx.deltas if d.status != "D" and d.path.endswith(".py") and not lay.is_test_side(d.path)]
    all_tests = lay.tests(graph.blobs)
    others = sorted(all_tests - {t for t, _, _ in tests})

    # ── run the selection on the current tree, recording which changed lines run ─
    current = testrun.Run()
    skipped_files: list[str] = []
    crashed: list[tuple[list[str], str]] = []
    ran_nothing: set[str] = set()
    widened: list[str] = []
    times = _Durations(ctx.root)
    with tempfile.TemporaryDirectory(prefix="notyet-lines-") as trace_dir:
        env, trace_out = coverage.setup(trace_dir, ctx.root, changed_src)
        env = source_path_env(ctx.root, env)
        stuck = ("", 0.0)

        def run_batch(batch: list[str], timeout: float) -> testrun.Run:
            run_ = testrun.run_pytest(command, ctx.root, batch, timeout=timeout, env=env, extra=["-p", coverage.PLUGIN])
            if not run_.crashed:
                current.results.update(run_.results)
                times.record(batch, run_)
                if not run_.timed_out:
                    seen = {n.split("::")[0] for n in run_.results}
                    ran_nothing.update(f for f in batch if f not in seen)
            return run_

        for batch in _batches(tests):
            remaining = cfg.budget_seconds - (time.monotonic() - started)
            if remaining <= 1:
                skipped_files += batch
                continue
            run_ = run_batch(batch, remaining)
            if run_.crashed:
                crashed.append((batch, run_.crashed))
            elif run_.timed_out:
                skipped_files += [f for f in batch if not any(n.startswith(f + "::") for n in run_.results)]
                stuck = max(stuck, coverage.running(trace_out), key=lambda s: s[1])

        # then the rest of the suite, as far as it's known to be quick: the selection follows what it can
        # read, and plugins, entry points and the like can't be read
        widen_until = min(cfg.budget_seconds - (time.monotonic() - started), cfg.widen_seconds)
        if others and not skipped_files and not crashed and widen_until > 2:
            widen_start = time.monotonic()
            for batch in times.affordable(others, widen_until):
                left = widen_until - (time.monotonic() - widen_start)
                if left < 1:
                    break
                run_ = run_batch(batch, left)
                done = {n.split("::")[0] for n in run_.results}
                widened += [f for f in batch if f in done or (not run_.crashed and not run_.timed_out)]
        times.save()
        hits = coverage.read(trace_out, ctx.root) if changed_src else {}

    if not tests and not widened:
        result.not_checked.append("tests: no tests exercise the changed files (none import them or are named for them)"
                                  if not all_tests else
                                  f"tests: nothing selects any of the {len(all_tests)} test file(s) for this change, and "
                                  f"they're too slow to run them all")
        result.findings += _coverage_findings(ctx, changed_src, {})
        return result
    ctx.shared["ran_nothing"] = ran_nothing
    coverage_check = None
    if changed_src:
        if hits is None:
            result.not_checked.append("coverage: couldn't record which changed lines ran")
        elif skipped_files or crashed:
            result.not_checked.append("coverage: not all selected tests ran, so changed lines that didn't run "
                                      "aren't reported")
        else:
            result.findings += _coverage_findings(ctx, changed_src, hits)
            coverage_check = f"coverage: recorded which changed lines in {len(changed_src)} file(s) ran"
    for batch, why in crashed:
        _report_crash(ctx, command, batch, why, result)
    if crashed and not current.results:
        return result
    ran_files = sorted({n.split("::")[0] for n in current.results})
    if skipped_files:
        selected = len({f for batch in _batches(tests) for f in batch})
        result.gaps.append(f"tests NOT run: the {cfg.budget_seconds}s budget ran out before any of the {selected} "
                           f"selected test file(s) finished" if not current.results else
                           f"tests incomplete: the {cfg.budget_seconds}s budget ran out; {len(skipped_files)} of the "
                           f"{selected} selected test file(s) didn't run")
        result.not_checked.append(f"tests: the {cfg.budget_seconds}s budget ran out before {len(skipped_files)} "
                                  f"selected test file(s) ran: " + ", ".join(skipped_files[:5])
                                  + (" …" if len(skipped_files) > 5 else ""))

    if stuck[0]:
        result.findings += _hang(ctx, command, *stuck)

    failing = current.failures
    flaky: list[str] = []
    if failing:  # re-run failures once: a pass means flaky, not broken
        rerun = testrun.run_pytest(command, ctx.root, [f.nodeid for f in failing], env=source_path_env(ctx.root),
                                   timeout=max(30, cfg.budget_seconds / 2))
        for f in list(failing):
            again = rerun.results.get(f.nodeid)
            if again is not None and not again.bad:
                flaky.append(f.nodeid)
                current.results[f.nodeid] = again
        failing = [f for f in failing if f.nodeid not in flaky]

    # ── compare with session start ───────────────────────────────────────────
    changed_tests = [d.path for d in ctx.deltas if d.status != "D" and lay.is_test_module(d.path)]
    baseline: dict[str, testrun.TestResult] = {}
    baseline_collected: dict[str, set[str]] = {}
    baseline_errors: set[str] = set()
    baseline_ok = False
    if failing:
        baseline_ok, baseline_note, baseline, baseline_collected, baseline_errors = _baseline(
            ctx, [f.nodeid for f in failing], [])
        if not baseline_ok:
            result.not_checked.append(f"comparison with session start: {baseline_note}")

    unverified: list[testrun.TestResult] = []
    regressions: list[Finding] = []
    before_too: list[Finding] = []
    renamed = {d.path: d.old_path for d in ctx.deltas if d.old_path}
    for f in [f for f in failing if "::" not in f.nodeid]:     # a test file that can't be imported/collected
        evidence = [line for line in f.message.splitlines() if line.strip()][:3]
        if baseline_ok and f.nodeid in baseline_errors:
            before_too.append(Finding(
                rule="test-failing-before", severity="note", location=f.nodeid, evidence=evidence,
                title=f"{f.nodeid} can't be collected, but it couldn't at session start either"))
        elif baseline_ok and baseline_collected.get(f.nodeid):
            regressions.append(Finding(
                rule="test-regression", severity="block", location=f.nodeid, evidence=evidence,
                title=f"{f.nodeid} can't be collected now, so none of its "
                      f"{len(baseline_collected[f.nodeid])} test(s) run; it could at session start",
                action="Fix the import or collection error."))
        elif baseline_ok and f.nodeid in changed_tests and not renamed.get(f.nodeid):
            regressions.append(Finding(
                rule="new-test-failing", severity="block", location=f.nodeid, evidence=evidence,
                title=f"{f.nodeid} is new in this session and can't be collected",
                action="Fix the import or collection error."))
        else:
            unverified.append(f)
    for f in [f for f in failing if "::" in f.nodeid]:
        test_file = f.nodeid.split("::")[0]
        existed = f.nodeid in baseline or testrun.function_of(f.nodeid) in {
            testrun.function_of(n) for n in baseline_collected.get(test_file, set())}
        before = baseline.get(f.nodeid)
        evidence = [line for line in f.message.splitlines() if line.strip()][:3]
        if baseline_ok and before is not None and before.bad:
            before_too.append(Finding(
                rule="test-failing-before", severity="note", location=f.nodeid,
                title=f"{f.nodeid} fails, but it already failed at session start", evidence=evidence))
        elif baseline_ok and before is not None and before.outcome == "passed":
            regressions.append(Finding(
                rule="test-regression", severity="block", location=f.nodeid,
                title=f"{f.nodeid} passed at session start and fails now", evidence=evidence,
                action="Fix the code so it passes again. Don't change the test to match new behavior "
                       "unless that behavior change was asked for."))
        elif baseline_ok and not existed:
            regressions.append(Finding(
                rule="new-test-failing", severity="block", location=f.nodeid,
                title=f"{f.nodeid} is new in this session and fails", evidence=evidence,
                action="Make the code pass the test (or fix the test if the test itself is wrong)."))
        elif baseline_ok and test_file in changed_tests:
            regressions.append(Finding(      # a new parametrize case, or one that was skipped before
                rule="new-test-failing", severity="block", location=f.nodeid,
                title=f"{f.nodeid} fails; it didn't run at session start", evidence=evidence,
                action="Make the code pass the test (or fix the test if the test itself is wrong)."))
        else:
            unverified.append(f)
    result.findings += _grouped(regressions)
    if len(before_too) > MAX_UNVERIFIED_ITEMS:
        before_too = [Finding(rule="test-failing-before", severity="note", location=before_too[0].location,
                              title=f"{len(before_too)} tests fail that already failed at session start",
                              evidence=[f.location for f in before_too[:5]], key="|".join(f.location for f in before_too))]
    result.findings += before_too
    if len(unverified) > MAX_UNVERIFIED_ITEMS:
        # one item, not hundreds: without a session-start comparison these are
        # as likely to be environment failures as anything this session did
        ids = sorted(f.nodeid for f in unverified)
        result.findings.append(Finding(
            rule="tests-failing-unverified", severity="resolve", location=ids[0],
            title=f"{len(ids)} selected tests fail, and they couldn't be compared with session start",
            evidence=[f"{f.nodeid}: {(f.message.splitlines() or [''])[0][:120]}" for f in unverified[:5]],
            action="Check whether this session caused them (run them, or `git stash` and re-run); "
                   "fix what it caused, then acknowledge the rest with what you found.",
            key="|".join(ids)))
    else:
        for f in unverified:
            result.findings.append(Finding(
                rule="test-failing", severity="resolve", location=f.nodeid,
                title=f"{f.nodeid} fails (couldn't confirm whether it passed at session start)",
                evidence=[line for line in f.message.splitlines() if line.strip()][:3],
                action="Fix it, or explain why it's expected to fail."))
    for nid in flaky:
        result.findings.append(Finding(rule="test-flaky", severity="note", location=nid,
                                       title=f"{nid} failed once and passed on re-run (flaky)"))

    # ── are the session's new tests collected at all? ─────────────────────────
    if changed_tests:
        from notyet.engines.integrity import test_names
        now_collected = testrun.collect(command, ctx.root, changed_tests, env=source_path_env(ctx.root), timeout=60)
        for d in ctx.deltas:
            if now_collected is None or d.path not in changed_tests:
                continue
            new = test_names(snapshot.show(ctx.root, ctx.current_tree, d.path) or "") - \
                test_names(snapshot.show(ctx.root, ctx.baseline_tree, d.old_path or d.path) or "")
            here = {testrun.function_of(n).split("::", 1)[1] for n in now_collected if n.startswith(d.path + "::")}
            if d.path in now_collected.errors:
                continue            # reported as failing
            for func in sorted(new - here):
                result.findings.append(Finding(
                    rule="test-not-collected", severity="resolve", location=f"{d.path}::{func}",
                    title=f"{d.path}::{func} was added but pytest doesn't collect it, so it never runs",
                    action="Check the file/function naming and pytest configuration."))

    # ── the session-start tests on the new code; the new tests on the session-start code ─
    from notyet.engines import crosscheck, vacuous
    if ctx.time_left() > 15:
        crosscheck.run(ctx, command, current, set(skipped_files), [t for t, _, _ in tests], result)
    else:
        result.not_checked.append("session-start tests on the new code: no time left in this check")
    if changed_tests and not skipped_files and ctx.time_left() > 10:
        vacuous.check(ctx, command, current.results, result)

    passed = sum(1 for r in current.results.values() if r.outcome == "passed")
    bad = sum(1 for r in current.results.values() if r.bad)
    skipped = sum(1 for r in current.results.values() if r.outcome == "skipped")
    why = sorted({w for _, _, w in tests})
    scope = "the whole suite" if set(others) <= set(widened) and not skipped_files else \
        f"{len(ran_files)} of {len(all_tests)} test files"
    result.checks.insert(0, f"tests: ran {len(current.results)} test(s) in {len(ran_files)} file(s), {scope} "
                         f"({passed} passed, {bad} failed, {skipped} skipped) in {time.monotonic() - started:.0f}s"
                         + (f"; selected because: {', '.join(why)}" if why else "")
                         + (f"; plus {len(widened)} other file(s) while time allowed" if widened and why else ""))
    if baseline_ok:
        result.checks.append("failures compared with an isolated checkout of the session-start tree")
    if coverage_check:
        result.checks.append(coverage_check)
    return result


HANG_SECONDS = 15


def _hang(ctx: Context, command: str, nodeid: str, seconds: float) -> list[Finding]:
    """A test the budget ran out in, if it finishes quickly on the session-start code: an endless loop or a
    deadlock the session introduced, not a slow suite."""
    seconds -= testrun.INTERRUPT_GRACE       # the part spent asking it to stop, at most
    if seconds < HANG_SECONDS or ctx.time_left() < 30:
        return []
    tmp = ctx.baseline_checkout()
    if not os.path.exists(os.path.join(tmp, nodeid.split("::")[0])):
        before = None
    else:
        run_ = testrun.run_pytest(command, tmp, [nodeid], timeout=HANG_SECONDS, env=source_path_env(tmp, {}))
        if run_.timed_out or testrun.outcome_of(nodeid, run_.results) != "passed":
            return []
        before = run_.seconds
    return [Finding(
        rule="test-hangs", severity="block" if before is not None else "resolve", location=nodeid,
        title=f"{nodeid} was still running after {seconds:.0f}s"
              + (f"; at session start it passes in {before:.1f}s" if before is not None else " (it's new in this session)"),
        action="Find the endless loop or the wait that never ends, and fix it.")]


def _grouped(findings: list[Finding]) -> list[Finding]:
    """One broken function can fail hundreds of tests. Past a handful, list a few and group the rest: a wall of
    near-identical items buries whatever else is wrong."""
    if len(findings) <= MAX_REGRESSION_ITEMS:
        return findings
    by_file: dict[tuple[str, str], list[Finding]] = {}
    for f in findings:
        by_file.setdefault((f.rule, f.location.split("::")[0]), []).append(f)
    out: list[Finding] = []
    for (rule, path), group in sorted(by_file.items(), key=lambda kv: -len(kv[1])):
        if len(group) == 1:
            out += group
            continue
        what = "passed at session start and fail now" if rule == "test-regression" else "are new in this session and fail"
        out.append(Finding(
            rule=rule, severity="block", location=path, key="|".join(sorted(f.location for f in group)),
            title=f"{len(group)} tests in {path} {what}",
            evidence=[f"{f.location.split('::', 1)[-1]}: {(f.evidence or [''])[0][:120]}" for f in group[:3]],
            action=group[0].action))
    return out


class _Durations:
    """How long each test file took last time, to know what else fits in the time left."""
    UNKNOWN = 1.0

    def __init__(self, root: str):
        self.path = store.state_dir(root) / "cache" / "durations.json"
        self.path.parent.mkdir(exist_ok=True)
        try:
            self.seconds: dict[str, float] = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.seconds = {}

    def record(self, files: list[str], run: testrun.Run) -> None:
        spent: dict[str, float] = {}
        for n, r in run.results.items():
            f = n.split("::")[0]
            spent[f] = spent.get(f, 0.0) + r.seconds
        for f, sec in spent.items():
            self.seconds[f] = round(sec, 2)
        if run.timed_out:
            for f in files:
                if f not in spent:       # whatever it needs, it's more than it got
                    self.seconds[f] = max(self.seconds.get(f, 0.0), round(run.seconds, 2)) * 2

    def affordable(self, files: list[str], budget: float) -> list[list[str]]:
        """Batches of `files`, quickest first, that should fit in `budget` together (with a margin, since
        pytest's own start-up and slow collection aren't in the per-test times)."""
        picked, total = [], 0.0
        for f in sorted(files, key=lambda f: (self.seconds.get(f, self.UNKNOWN), f)):
            total += self.seconds.get(f, self.UNKNOWN)
            if total > budget * 0.7:
                break
            picked.append(f)
        size = max(BATCH_FILES, (len(picked) + 3) // 4)
        return [sorted(picked[i:i + size]) for i in range(0, len(picked), size)]

    def save(self) -> None:
        self.path.write_text(json.dumps(self.seconds))


def _coverage_findings(ctx: Context, changed_src: list[str], hits: dict[str, set[int]]) -> list[Finding]:
    deltas = [d for d in ctx.deltas if d.path in set(changed_src)]
    added = snapshot.added_lines(ctx.root, ctx.baseline_tree, ctx.current_tree, deltas)
    sources = {p: snapshot.show(ctx.root, ctx.current_tree, p) or "" for p in changed_src}
    return coverage.findings(sources, added, hits)


def _report_crash(ctx: Context, command: str, batch: list[str], why: str, result: EngineResult) -> None:
    """pytest couldn't run these files now. If they collected at session start,
    this session broke them (a conftest, an import at module level)."""
    tmp = ctx.baseline_checkout()
    present = [p for p in batch if os.path.exists(os.path.join(tmp, p))]
    before = testrun.collect(command, tmp, present, env=source_path_env(tmp, {}), timeout=60) if present else None
    if before:
        result.findings.append(Finding(
            rule="test-run-broken", severity="block", location=batch[0],
            title=f"pytest can't run {len(batch)} test file(s) that it could run at session start",
            evidence=[why[:300]], key="|".join(sorted(batch)),
            action="Fix the import or configuration error so these tests run again."))
    else:
        result.not_checked.append(f"tests: couldn't run {len(batch)} test file(s), and they didn't run at "
                                  f"session start either: {why[:300]}")


def _baseline(ctx: Context, wanted: list[str], whole_files: list[str]):
    """(ok, why not, results of `wanted`, collected ids per file, files that can't be collected), on the
    session-start tree. `wanted` are node ids (a bare file for one that can't be collected); `whole_files`
    are run entirely. That tree never changes within a session, so everything learned about it is cached
    (per tree and test command) and later checks only run what's new."""
    cfg = ctx.config
    command = testrun.anchored(cfg.test_command or "", ctx.root)
    cache = _BaselineCache(ctx.root, ctx.baseline_tree, command)
    in_baseline = set(snapshot.git(ctx.root, "ls-tree", "-r", "-z", "--name-only", ctx.baseline_tree).split("\0"))
    files = sorted({n.split("::")[0] for n in wanted} | set(whole_files))
    present = [p for p in files if p in in_baseline]
    if not present:
        return True, "", {}, {}, set()                  # nothing existed then: everything is new
    dirs = sorted({os.path.dirname(p) for p in present})

    def to_run() -> list[str]:
        ids = list(wanted) + [n for p in whole_files for n in cache.collected.get(p, ())]
        known = {n for p in present for n in cache.collected.get(p, ())}
        return [n for n in dict.fromkeys(ids) if n in known and n not in cache.results]

    need_collect = [p for p in present if p not in cache.collected]
    if need_collect or to_run():
        tmp = ctx.baseline_checkout()
        env = source_path_env(tmp, {})
        if not cache.canary_ok(dirs):
            ok, why = _canary(ctx, tmp, present, env)
            if not ok:
                return False, why, {}, {}, set()
            cache.canary_passed(dirs)
        if need_collect:
            got = testrun.collect(command, tmp, need_collect, env=env, timeout=60)
            if got is None:
                return False, "couldn't collect the relevant test files at session start", {}, {}, set()
            for p in need_collect:
                cache.collected[p] = sorted(n for n in got if n.startswith(p + "::"))
                if p in got.errors:
                    cache.errors[p] = True
        if ids := to_run():
            run_ = testrun.run_tests(command, tmp, ids, env=env,
                                     timeout=max(30, min(cfg.budget_seconds, ctx.time_left() - 10)))
            if run_.crashed:
                return False, f"the test command failed on the session-start tree ({run_.crashed})", {}, {}, set()
            for n in ids:
                r = run_.results.get(n)
                if r is not None or not run_.timed_out:
                    cache.results[n] = [r.outcome, r.message] if r else ["missing", ""]
        cache.save()
    collected = {p: set(cache.collected.get(p, ())) for p in present}
    errors = {p for p in present if cache.errors.get(p)}
    ids = list(wanted) + [n for p in whole_files for n in cache.collected.get(p, ())]
    results = {n: testrun.TestResult(n, cache.results[n][0], cache.results[n][1]) for n in ids
               if n in cache.results and cache.results[n][0] != "missing"}
    return True, "", results, collected, errors


def passed_at_session_start(ctx: Context, command: str, node_ids: list[str], whole_files: list[str]) -> tuple[set[str], str]:
    """Which of `node_ids`, and of all tests in `whole_files`, passed on the session-start tree."""
    if not node_ids and not whole_files:
        return set(), ""
    ok, why, results, _, _ = _baseline(ctx, node_ids, whole_files)
    return ({n for n, r in results.items() if r.outcome == "passed"}, "") if ok else (set(), why)


def _code_fingerprint() -> str:
    """notyet's own code that produces cached results: a new version of it
    must not reuse results the old one computed."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    h = hashlib.sha1()
    for rel in ("testrun.py", "snapshot.py", "engines/execution.py", "engines/vacuous.py", "engines/crosscheck.py"):
        try:
            with open(os.path.join(here, rel), "rb") as f:
                h.update(f.read())
        except OSError:
            pass
    return h.hexdigest()[:12]


class _BaselineCache:
    KEEP = 8

    def __init__(self, root: str, tree: str, command: str):
        key = hashlib.sha1(f"{tree}|{command}|{_code_fingerprint()}".encode()).hexdigest()[:16]
        self.path = store.state_dir(root) / "cache" / f"baseline-{key}.json"
        self.path.parent.mkdir(exist_ok=True)
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            data = {}
        self.collected: dict[str, list[str]] = data.get("collected", {})
        self.errors: dict[str, bool] = data.get("errors", {})
        self.results: dict[str, list[str]] = data.get("results", {})
        self.canaries: list[str] = data.get("canaries", [])
        self.vacuous: dict[str, dict[str, list[str]]] = data.get("vacuous", {})   # see engines/vacuous.py

    def canary_ok(self, dirs: list[str]) -> bool:
        return all(d in self.canaries for d in dirs)

    def canary_passed(self, dirs: list[str]) -> None:
        self.canaries = sorted(set(self.canaries) | set(dirs))

    def save(self) -> None:
        self.path.write_text(json.dumps({"collected": self.collected, "errors": self.errors,
                                         "results": self.results, "canaries": self.canaries,
                                         "vacuous": self.vacuous}))
        old = sorted(self.path.parent.glob("baseline-*.json"), key=lambda p: p.stat().st_mtime)
        for p in old[:-self.KEEP]:
            p.unlink(missing_ok=True)


def _canary(ctx: Context, tmp: str, test_files: list[str], env) -> tuple[bool, str]:
    """Prove the session-start checkout imports its own copy of the changed
    modules. The canary sits next to each test file being compared, so pytest
    sets up imports exactly as it will for those tests."""
    command = testrun.anchored(ctx.config.test_command or "", ctx.root)
    roots = find_python_source_roots(tmp)
    modules = []
    for d in ctx.deltas:
        if not d.path.endswith(".py") or ctx.layout.is_test_module(d.path) or d.status == "A":
            continue
        full = os.path.join(tmp, d.path)
        for r in sorted(roots, key=lambda r: -len(str(r))):
            if full.startswith(str(r) + os.sep):
                modules.append(os.path.relpath(full, r)[:-3].replace(os.sep, ".").removesuffix(".__init__"))
                break
    modules = [m for m in dict.fromkeys(modules) if m and all(p.isidentifier() for p in m.split("."))][:5]
    if not modules:
        return True, ""   # only new or non-importable files changed: nothing that could be shadowed
    # find_spec, not import: where a module would come from is the question, and importing it can
    # need things (settings, a display) that only its own tests set up
    body = "import importlib.util, os\n\n" + f"ROOT = os.path.realpath({tmp!r})\n\n" + "".join(
        f"def test_canary_{i}():\n    spec = importlib.util.find_spec({m!r})\n"
        f"    if spec is not None and spec.origin:\n"
        f"        assert os.path.realpath(spec.origin).startswith(ROOT + os.sep), spec.origin\n\n"
        for i, m in enumerate(modules))
    canaries = []
    for n, folder in enumerate(sorted({os.path.dirname(os.path.join(tmp, p)) for p in test_files})):
        path = os.path.join(folder, f"{CANARY}_{n}.py")      # same-named files in two directories can't both import
        with open(path, "w") as f:
            f.write(body)
        canaries.append(os.path.relpath(path, tmp))
    run = testrun.run_pytest(command, tmp, canaries, env=env, timeout=60)
    for rel in canaries:
        os.remove(os.path.join(tmp, rel))
    if run.crashed or not run.results:
        return False, "couldn't verify the session-start checkout imports its own code"
    if any(r.bad for r in run.results.values()):
        return False, ("the session-start checkout imports the working tree's code instead of its own "
                       "(an editable install?), so failures can't be compared with session start")
    return True, ""
