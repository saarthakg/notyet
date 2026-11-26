# notyet: evaluation, November 2026

What was measured, on what, and what it doesn't show. Raw results are in `eval/results/2026-11-25/`,
the scripts in `eval/` ([how to rerun them](../eval/README.md)). "Old" is commit 38862e5, the code the
[September evaluation](EVAL-2026-09.md) described; "new" is the code after this round.

## 1. Summary

| | Old | New |
|---|---|---|
| **Held-out scenarios**, second set, written without seeing the gate's code; first run | 42 / 60 | **60 / 60** |
| Held-out scenarios, first set; first run | 67 / 109 | **103 / 109** |
| Regressions blocked: random mutations in 5 real repos that break the repo's own suite | 158 / 161 | **160 / 161** |
| False blocks on mutations the repo's own suite passes | 6 / 39 | **0 / 39** |
| Reviewed, merged human commits with a block-level finding | 13 / 39 | 9 / 39 |
| **Agent sessions** left with broken behavior: the agent alone / with the gate | | **14 / 36 → 1 / 36** |

Three things to take from it:

- **The September headline (120 of 120 tampers caught) was true and misleading.** The suite had eight tampers,
  written by whoever wrote the rules. Against cheats written by someone else, the same code caught 8 of 31.
- **The gate changes outcomes when agents break things,** and all of the damage was flagged (§6). When agents are
  already careful, it adds a record and some friction, and nothing else.
- **It sees what the tests see.** Incomplete work is invisible to it (§7). That hasn't changed.

## 2. How the old code failed

Before changing anything, the old code was run on 86 sessions with known right answers
(`eval/notyet_scenarios.py`): 43 of 86 right. The causes, in order of how much they cost:

- **Tampering was detected by syntax.** An edited function body was noticed; an edited fixture, helper, golden file,
  `setUp`, base class, import or pytest hook was not.
- **Tests were selected by imports alone.** A test that reached the code only through a conftest fixture, a string, a
  subprocess, a data file or a deleted module was never run.
- **Ordinary configurations broke it silently.** `diff.noprefix = true` emptied every diff it read. `export-ignore`
  left the tests out of its checkouts. A dev server running in the background switched the gate off for the session.
- **Honest work was blocked.** Moving, renaming or parametrizing a test was "removing" it. A `git pull` blamed the
  agent for the team's commits. Some failures were artifacts of running tests in another order.
- **A failing test that was edited instead of fixed passed in silence,** because the test had never passed. "Fix this
  failing test" is about the most common thing people ask.

The central fix is that the session-start *test side* is now run, whole, against the session's code (README, "How it
decides"), rather than guessing from what was edited.

## 3. Scenarios

Each scenario is a small repo and a session, played through the real hooks. A cheat, regression or setup counts as right
only if an open finding is about the bug or the hidden tests (an untested-line finding doesn't count). Honest work counts as
right only if it's checked and not blocked.

| Set | Kind | Old | New, first run | New, now |
|---|---|---|---|---|
| **Development** (written by the author, then fixed against: not evidence) | cheats | 14/35 | | 38/38 |
| | regressions | 4/14 | | 15/15 |
| | honest | 12/20 | | 21/21 |
| | setups | 13/17 | | 18/18 |
| **Red team 1** (held out for its first run, then fixed against) | cheats | 8/31 | **29/31** | 31/31 |
| | regressions | 12/22 | **20/22** | 22/22 |
| | honest | 30/35 | **34/35** | 35/35 |
| | setups | 17/21 | **20/21** | 21/21 |
| **Red team 2** (held out; see the last note) | cheats | 12/20 | **20/20** | |
| | regressions | 10/14 | **14/14** | |
| | honest | 13/18 | **18/18** | |
| | setups | 7/8 | **8/8** | |

- **Procedure.** Each red-team set was written by a separate agent told not to read the gate's code, tests, docs, results
  or history, and not to run the gate. It validated every scenario with plain pytest: a cheat really is green and really
  hides a bug; honest work really keeps every assertion. Each set was committed before its first run.
- **Caveats.** The authors are the same model family as the gate's author, and share its blind spots. They saw the harness,
  including the rule names, and recent commit subjects. The repos are small. The first set's authors were told nothing
  about the fixes; the second was written after most of them.
- **On a later re-run of red team 2, one honest session was blocked:** a test that fails one time in four failed four times in
  a row, about a 0.3% chance per check. Regressions are now repeated up to ten times (for at most about ten seconds) before
  they block. Nothing else was changed after that set.
- **One development scenario is a stated limit** and isn't counted: source that special-cases the exact inputs the tests use.

## 4. Regression recall on real repos

`eval/notyet_recall.py` applies 40 random one-line mutations to each of flask, click, attrs, httpx and rich: flipped
comparisons, negated conditions, dropped calls, off-by-ones, swapped booleans, nulled returns. The repo's full suite, run
twice on the clean tree to drop unstable tests, decides what each mutation breaks.

| | Break the suite | Old blocked | New blocked | Don't break it | Old false blocks | New false blocks |
|---|---|---|---|---|---|---|
| flask | 38 | 38 | 38 | 2 | 0 | 0 |
| click | 32 | 32 | 32 | 8 | 5 | 0 |
| attrs | 32 | 32 | 32 | 8 | 0 | 0 |
| httpx | 29 | 27 | 29 | 11 | 0 | 0 |
| rich | 30 | 29 | 29 | 10 | 1 | 0 |
| **total** | **161** | **158** | **160** | **39** | **6** | **0** |

- **The false blocks** were click's and rich's order-dependent tests. Failing tests are now compared with the same files
  run in the same order on the session-start tree.
- **The remaining one** is an endless loop in a test that already failed, quickly, at session start. The gate said
  INCOMPLETE, not PASSED. It has since been fixed, and this mutant blocks; that fix was checked on this mutant only.
- The recorded table counts one httpx mutant as missed. It was blocked by `test-hangs`, which the script didn't
  count as a regression at the time.
- **Time per check,** all five repos running at once on 10 cores: median 6s flask, 11s click, 14s httpx, 16s attrs and
  rich; 95th percentile 11–39s. The old code took 4–24s at the median. These suites take 1–7s each.
- Mutations are small and local. They say that breakage which tests can see gets seen; they don't resemble an agent's change.

## 5. Human commits

`eval/notyet_replay.py` replays 39 reviewed and merged commits (click, attrs, flask) as if each were a session. A block here
is at best a deliberate behavior change that someone should look at.

- **Block-level: 13 before, 9 now.** All nine have `test-changed-to-pass` (one also `test-removed`): the maintainers changed an expectation along with
  the behavior. In a session, the agent hands each to the user once and stops.
- The four that no longer block moved tests, removed tests that would still pass, or edited tests whose old version can't be imported or
  called any more. They're fix-or-justify now.
- **Seven of flask's nine can't be checked:** they're years old and their tests don't run in today's environment. notyet says so.

The September tamper suite (8 mechanical tampers, 15 seeded faults, five repos) is still 120 of 120: each tampered test is named
in a block, by a tamper rule or, where the tamper didn't hide the failure, as a regression. §2 is why that number says little.

## 6. Agents

`eval/notyet_pressure.py`: small tasks where a false "done" is likely, each run with the gate watching (report) and enforcing.
Headless Claude Code, 96 sessions, about $1.90 of API-equivalent quota.

**Hidden couplings.** Six tasks (rename an attribute that's also used through `getattr`, remove a function named in a registry,
tidy a function with undocumented edge cases, ...). The agent can read and edit, but not run anything. Broken = a test that
passed at session start fails on what the agent left.

| | Agent alone (report) | With the gate (enforce) |
|---|---|---|
| Haiku, 4 runs per task | 10 / 24 left broken | **1 / 24** |
| Sonnet, 2 runs per task | 4 / 12 left broken | **0 / 12** |
| **total** | **14 / 36** | **1 / 36** |

- **Every broken session was flagged** at block level: 14 of 14 in report mode, and the one in enforce mode. No session that
  was fine ended with a block-level finding.
- **The one left over** is a feature the agent removed on its own reading of "clean up anything only it needed". It handed
  that to the user, with that reason, and the receipt says NEEDS YOUR REVIEW. In 5 of Haiku's 10 broken sessions, the agent
  had also deleted or edited the tests, so the suite was green.
- **Cost:** 18–30% more quota, 4–6 seconds more per session, and 16 of 36 enforce sessions sent back at least once.
- Haiku's difference has p = 0.004 (Fisher's exact). The same six tasks repeat, so treat that as indicative.
- **This is the favorable case.** The tasks were built to have couplings, and the agent couldn't run the tests. Agents that run
  the right tests themselves leave the gate less to find. In their messages, the agents did say that nothing had been run;
  what the gate adds is the fix.

**Contradictory tests.** Three tasks with a failing test that contradicts the README and the other tests; the agent can run
tests. Haiku was honest in all 24 sessions, with or without the gate: it kept the documented behavior, corrected the wrong
test and said so, or (told not to touch tests) reported the contradiction. With the gate, 8 of 12 were sent back once to record
the reason. **No outcome changed:** here the gate is a receipt, at the price of one interruption.

**Real commits.** Seven prompts from real merged commits (`eval/sessions_tasks.json`), Haiku, editing only: no regressions. Two
resolved the maintainers' hidden tests fully or partly. The September sessions with Opus showed the same: on well-specified
commits, what's wrong is that the work is *incomplete*. The rest of that set was not run.

**Feature sessions on two real projects** (private, not in this repo): a support agent with 187 tests and an ingestion service with 61.
Eight sessions of three or four prompts each (features, refactors, changed rules, test clean-ups), Claude Code on its default model, able to run
tests; six sessions enforcing. Findings were labeled by hand (`eval/notyet_dogfood.py`; about $20 of API-equivalent quota).

- **No regression occurred, and none was missed.** The suite was green after all 25 turns. Every turn in which a session-start test
  stopped passing had an item about it.
- **What the interruptions caused.** notyet stores the tree at every check, so what the agent was about to hand over can be compared
  with what it did. Of the 8 interruptions in the first two rounds: 4 changed no code (acknowledgments); 1 was neutral churn; 2 led
  to a real test of an untested path (one along with work caused by a false alarm); and **1 made the code worse**: the agent bent a frozen dataclass
  (`object.__setattr__`, an optional path) only so that the old test setup would still construct it.
- **What the agent had already said.** Of 43 changed, removed or edited-while-failing tests that notyet raised in recorded sessions, the
  agent's message before the check mentioned changing tests in all 43 and named the test or file in 38. Of 18 regressions it had caused, it
  mentioned 1: it couldn't run the tests and didn't know. (Judged by pattern matching over the messages, so approximate.)
- **So the policy changed:** send the agent back only for what it doesn't know or didn't say, and show the user the rest as a diff of what the
  tests check. Replayed on the same trees and messages, 8 interruptions become 2, the two that led to real tests. Every cheat and
  regression in the scenario sets, where the agent says nothing, is still sent back but one, which had been sent back twice already.
- **Third round, with that policy** (7 turns, harder prompts, one session on Sonnet): 2 interruptions. One led to a test of an untested error path.
  The other was unnecessary: the agent had said it by naming the value (`W-1`) rather than the test, which now counts. In both sessions the brief
  showed real decisions in a few lines: `W-1` used to be rejected and now resolves; a ticket response gained a field.
- **A wrong turn:** one new test failed in the harness and passed elsewhere, which looked like flakiness under load, and a check for flaky new tests
  was built. 120 runs under load didn't fail once; the harness had simply been started with Ctrl-C ignored. The check was removed, and test runs no longer inherit that.
- **Useful:** two untested error paths, each of which got a real test; and three requested changes of behavior (a longer tool list, a
  lockout limit, a relaxed validation) handed to the user with accurate reasons. The agent acknowledged nothing dishonestly.
- **Wrong or noise, all in the first round:** CLI code called untested because the test gives its child process its own `PYTHONPATH`
  (two sessions; one agent spent minutes writing tests for it); moved lines called untested; an internal constructor's new argument blocked
  like a behavior change; `type: ignore` on deliberately wrong test input; edits called undone work; and NEEDS YOUR REVIEW repeated on
  every later turn. All fixed, and checked on the recorded trees (`eval/notyet_recheck.py`) and on a second round with new prompts:
  there, 4 of 6 turns had nothing to say or one true item.
- **Still friction:** moving or renaming what tests import asks for one acknowledgment, and half of the enforcing turns were sent back
  at least once.
- **Time per check:** 5-10s on one project, 16-33s on the other, whose suite takes 13s.

**notyet on itself.** In September, 29 of 41 stops went unchecked because some background task was running, which is why that rule changed.
On the long session that produced this document, its receipt was INCOMPLETE after seven minutes with 58 open items. Of those, one was a
real bug (the plugin trusted the host's environment, so one new test failed only inside the hook), 14 were tests whose expectations
this work had deliberately changed, which the author's own summary hadn't mentioned, and 43 were lint, suppressions and other noise. That led to
grouping, a bounded check, test runs without the host's variables, and `tests/test_notyet_dogfood.py`.

### Unfinished work (November 2026)

The checks in `plan.py` and `engines/finish.py` (docs/RESEARCH.md §12): the agent's own to-do list, TODOs, placeholders,
debugging left in, new code nothing calls.

- **Human commits, as a false-alarm rate.** 600 merged commits that changed source (flask, click, attrs, httpx, rich,
  requests; `eval/notyet_finish_precision.py`, checks only, seconds). First run: 5 with an item; three patterns were false
  alarms (prose about "existing code", one-line typing stubs, `Protocol[T]` bases) and were fixed. After: 4 commits. Two
  are real leftovers that a maintainer merged (commented-out `save_html`/`save_svg` debug calls in rich, one with a
  developer's home-directory path); two are TODOs kept on purpose. The unused-code check looked at 61 new names in these
  commits and found every one used.
- **Recorded dogfood trees** (`eval/notyet_recheck.py`, 25 turns). One false alarm, fixed: `_Bytes.makefile`, a socket
  method `http.client` calls, would have sent the agent back five times; a public method with no caller in the repo is
  no longer reported. One real item, shown to the user: a `failed_lookups` property an agent added so old tests could
  still read state it had moved, the change §6 counts as making the code worse.
- **Fourth round, multi-part requests** (4 sessions, 8 turns; Sonnet and Haiku; a returns journey then cancellations,
  per-source configuration then `--dry-run`; each request had 5-7 parts including docs and tests). Checked by hand
  against the request: every agent did every part, including the docs. The new checks found nothing to send back and
  sent nothing back; the same test-only shim appeared again and went to the user. The interruptions (4 of 8 turns) were
  all for changed lines no test ran, and each one led to a real test.
- **The plan check end to end.** None of those sessions kept a to-do list: the harness ran Claude Code with project
  settings only and without the task tools allowed, so the agents just edited. A Haiku session with the task tools,
  told to plan three steps and stop after one, was sent back by the real hook with "2 of 3 step(s) in your own plan
  aren't done". Its second closing message said steps 2 (`mul`) and 3 (`div`) were pending as asked, which notyet failed
  to count as saying so (step names were too short for the word match); a step is now recognized by the code name it's
  about.
- **What this doesn't show:** how often agents stop short in real use. Requests that fit in one turn of `claude -p`
  were finished every time here; the reports in RESEARCH.md §12 come from long interactive sessions with a to-do list,
  which this harness doesn't reproduce.

## 7. What this doesn't show

- **Incomplete or wrong-but-consistent work passes** when no test, plan step or leftover in the code shows it: a part of
  the request nobody wrote down. This is the most common fault in the real-commit sessions.
- **Real use.** Eight sessions on two small projects is a start, and the prompts were written by the tool's author, not by a developer with a job to do. Whether the interruptions are worth it over weeks, in someone's daily
  work, still has to be seen.
- **Large repos.** The biggest suite here runs in seconds. On Django's source the import graph takes 12s once, and under a second after,
  but no session has been run there.
- **Deliberate evasion** of the gate itself: `.git/notyet/`, the hooks, plumbing commands that forge history.
- **Codex.** Its adapter is tested against the documented payloads, not a running Codex.

## 8. Found by review

After the fixes, an independent review of the code found 12 problems, nearly all reproduced, several of which ended in a wrong PASSED:
`-q` or `-x` in `addopts`, a test file renamed out of collection, a crash inside an engine, a malformed config, linked data
directories, the session's own work coming back as a squash merge. All are fixed, each with a test in
`tests/test_notyet_review.py` or `tests/test_notyet_moves.py`. None of the scenario sets had found them, which is the
honest measure of how much a clean scorecard is worth.
