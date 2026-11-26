# notyet

**When your coding agent says it's done, notyet checks whether it is.**

notyet is a completion gate for AI coding agents working in Python repos with pytest. When the
agent tries to stop, notyet runs the tests its change reaches, compares them with the tree as it
was when the session started, and sends the agent back if something that worked is broken, was
made to look like it works, or isn't finished: steps of its own plan left undone, TODOs and
placeholders, debugging left in, new code nothing calls. You get a short brief: what isn't done, what
changed in what the tests check, and what wasn't checked.

It is deterministic. No model judges anything; every block is backed by a test that passed before
and fails now. It has no dependencies, and keeps its state in `.git/notyet/`.

What you see at the end of a turn, from a real session ("change the lockout from 3 failed lookups to 5"):

```
notyet: NEEDS YOUR REVIEW. 222 tests pass (the whole suite); 114 of 119 earlier tests in edited files pass as
they were; 34 of 35 new or edited tests fail without the change; every changed line ran
  What the tests say has changed:
    tests/test_orders.py::test_three_failures_then_blocked is gone
      ~ now test_five_failures_then_blocked
      - assert errors == ["order_not_found", "order_not_found", "too_many_attempts"]
      + assert errors == ["order_not_found"] * 4 + ["too_many_attempts"]
```

And what an agent sees when it thinks it's done and isn't:

```
notyet: not done yet. 1 item(s) to resolve before stopping.

1. [block] tests/test_cart.py::test_checkout_total passed at session start and fails now  id=9c1f02aa
   assert 25.0 == 27.5
   -> Fix the code so it passes again. Don't change the test to match new behavior unless
      that behavior change was asked for.
```

## Why

The most reported failure of coding agents is the false "done": the tests never ran, ran the wrong
way, or were quietly changed to pass. The engineer finds out later. Telling the agent "always run
the tests" doesn't fix it, because agents skip instructions that are sitting in their context. The
check has to live in the harness, and it has to rest on execution rather than on the agent's account.
The evidence behind that is in [docs/RESEARCH.md](docs/RESEARCH.md).

## Install

In Claude Code:

```
/plugin install notyet --marketplace saarthakg/notyet
```

Or as a command, for Claude Code, Codex or CI:

```sh
pip install git+https://github.com/saarthakg/notyet      # or: uv tool install git+...
```

Then, in your repo:

```sh
notyet init                 # writes .notyet.toml: finds your pytest command, shows it, asks first
notyet install claude       # not needed with the plugin. Shows the settings diff, asks first
notyet install codex        # the same for Codex CLI (then trust the hooks with /hooks)
```

The gate begins in `report` mode: it never blocks, and leaves a receipt after each change. Set
`mode = "enforce"` in `.notyet.toml` when you trust it.

## How it decides

### Every test that passed at session start is accounted for

Once a session edits tests, "the tests pass" stops meaning anything. So notyet runs more than one
combination:

|  | code at session start | the session's code |
|---|---|---|
| **tests at session start** | the baseline | **would the original tests still pass?** |
| **the session's tests** | do the new tests fail without the change? | the ordinary run |

The *test side* that gets swapped is everything that decides what a test checks: test modules,
`conftest.py`, helpers, fixtures, golden files, plugins, pytest's configuration. It doesn't matter
whether the agent edited an assertion, loosened a fixture, regenerated a golden file or added a hook
that turns failures into passes: if the original would fail on the new code, that is what shows.

| Finding | Level |
|---|---|
| A test that passed at session start fails now, can't be collected, or never finishes | **block** |
| A test passes only because of the session's changes to the test side | **block** |
| A test is gone or skipped, and would fail on the new code | **block** |
| A new test fails | **block** |
| A test is gone or skipped, but would still pass | fix or justify |
| A test was edited because names or signatures moved; the values it checks are unchanged | fix or justify |
| No new test fails without the change, so none of them tests it | fix or justify |
| Changed lines that no test ran | fix or justify |
| Work from an earlier turn, or your uncommitted edits, was undone | fix or justify |
| Source that checks whether it's being tested; an `__eq__` that is always true | fix or justify |
| Steps of the agent's own to-do list still pending when it stops | fix or justify |
| Something new your request named (`--dry-run`, `start_return`, `returns.jsonl`) that exists nowhere; docs you asked for, untouched | fix or justify |
| New TODO/FIXME comments, empty functions, "rest of the code here" comments | fix or justify |
| `breakpoint()` and debugger calls, debug prints, commented-out code | fix or justify |
| New functions or classes nothing calls; new public ones only tests call | fix or justify |
| New ruff or pyright errors (also in the callers); new `# noqa`, `# type: ignore`, ... | fix or justify |
| Failures that were there before; flaky tests | note |

- **block:** cleared by a fix, or by putting it in front of you. Then the verdict is NEEDS YOUR REVIEW, never PASSED.
- **fix or justify:** shown to you; the agent may answer it with a reason.
- Levels can be changed per rule (`[rules]` in `.notyet.toml`).

### When it interrupts the agent

Only for what the agent **doesn't know or didn't say**. In recorded sessions agents nearly always told the user about the
tests they had changed, and almost never about the regressions they had caused, because they didn't know. So in enforce mode:

- **sent back:** broken or hanging tests, new code no test runs, new type errors, test changes the agent's closing
  message doesn't mention, and unfinished work: plan steps, TODOs and parts of the request it doesn't mention,
  placeholders, debugger calls and debug prints, new code nothing calls. Mentioning a test change, or what's left, is enough; no command needed.
- **not sent back, shown to you:** test changes and unfinished steps it did mention, moves and renames, lint,
  suppressions, commented-out code, new public code only tests call, undone work.

`send_back = "all"` under `[gate]` sends it back for everything: for agents nobody is watching.

### What it won't blame on the agent

- **Your own edits** from before the session, committed or not: the starting point is a snapshot of
  the working tree, not `HEAD`.
- **Failures that were already there.** Failing tests are re-run on an isolated checkout of the
  starting point: the same files, in the same order, because some tests only pass after others.
- **Flaky tests.** A regression has to fail every time it's repeated before it blocks.
- **Commits that arrived mid-session.** A `git pull`, a rebase onto upstream or a branch switch moves
  the starting point. Commits the session made itself don't.
- **Tests that moved.** Same body, passing, somewhere else or under another name: not removed.
- **A broken environment.** A canary first proves that the starting-point checkout imports its own
  code. An editable install can otherwise test the new code twice without anyone noticing.

### What it will tell you it didn't check

Anything notyet couldn't check is listed, never reported as passed. If the tests didn't run, or the
time budget cut them short, the verdict is INCOMPLETE and the one-line summary leads with that.

## In CI

The same checks on a pull request, agent or not, against the branch point:

```yaml
- uses: actions/checkout@v4
  with: {fetch-depth: 0}
- run: pip install -e ".[dev]"
- uses: saarthakg/notyet@main      # annotates the diff, writes the receipt to the job summary
```

On a pull request it also keeps one comment up to date with the brief (what isn't done, what broke, what the tests now
check), and reads the description as the request: a flag or a function it says the change adds has to exist. The job
needs `permissions: pull-requests: write` for the comment; set `comment: false` to leave it out.

or `notyet check --base origin/main --merge-base [--format json|github] [--fail-on block|resolve|never]
[--request-file PR.md] [--comment-file OUT.md]`.
An item is accepted by giving the reason where reviewers see it, in a commit message:

```
Notyet-Ack: 9c1f02aa totals no longer include tax, as decided in #412
```

## Does it work?

Measured in November 2026, against ground truth that doesn't come from notyet itself:

| | |
|---|---|
| **Agents.** Small tasks with hidden couplings, agents that can edit but not run anything (Haiku, Sonnet): sessions left with broken behavior | alone **14 / 36**, with notyet **1 / 36** (that one handed to the user for review) |
| **Held-out scenarios.** 60 cheats, regressions, honest sessions and odd setups, written by an agent that hadn't seen notyet's code; first run | **60 / 60** (September's version: 42) |
| **Mutations** of flask, click, attrs, httpx and rich that break their own suite: blocked | **160 / 161** |
| **False blocks** on mutations their suite passes | **0 / 39** |
| Reviewed human commits that get a block-level finding (deliberate behavior changes) | 9 / 39 |
| Time per check on those repos, median | 6–16s |
| **Unfinished-work checks** on 600 merged human commits: commits with an item (2 real leftovers, 2 deliberate TODOs) | **4 / 600** |

What it doesn't do matters as much. It can't see a part of the request that no test, plan step or leftover shows; how
often agents stop short in real use isn't measured (in eight multi-part turns every agent finished); the agent tasks were
built to have couplings; and real use over weeks hasn't been measured yet. The method, the caveats and the mistakes are in
[docs/EVAL.md](docs/EVAL.md).

## Commands

```sh
notyet status               # the current session and its last check
notyet receipt              # the latest receipt
notyet check [--base REF]   # run the checks now, without an agent
notyet stats                # what findings led to: fixed, justified, handed over
notyet export               # every finding as CSV, for labeling
```

## Configuration

```toml
[test]
command = ".venv/bin/python -m pytest"   # notyet adds test selection and a junit report
budget_seconds = 60                      # time for tests at each check
widen_seconds = 20                       # of that, for tests beyond the ones that use the changed code

[static]                                 # optional
ruff = ".venv/bin/ruff"
pyright = ".venv/bin/pyright"

[gate]
mode = "report"                          # or "enforce"
send_back = "unknown"                    # or "all"

[rules]                                  # block | resolve | note | off
untested-change = "note"
```

It is read as it was at session start, so an agent can't loosen its own gate. A change to it is
reported.

## How it works

- **Snapshots.** The working tree as a git tree object, through a temporary index. Your index, `HEAD`
  and files aren't touched.
- **Test selection.** Tests that use a changed module, directly or through others: imports, conftest
  fixtures, `mock.patch` and plugin strings, `python -m` programs, data files, the modules a deleted
  one had as users. Then the rest of the suite, as far as it is known to be quick: for most repos that
  is all of it.
- **Loop safety.** It runs only when the tree changed. The same findings block at most twice, then the
  stop is allowed and reported as UNRESOLVED. It waits for subagents and running builds, but not for a
  dev server that's simply there.
- **Never in the way.** An internal error is logged and reported, and the agent is allowed to stop.

## Limits

- Python and pytest only, for now.
- It knows what the tests know, what the agent planned, and what the code says about itself. Work that is
  incomplete against your request in a way none of those shows (a requirement nobody wrote down as a step or a
  test), or hard-coded to the exact inputs the tests use, gets through.
- Tampering with `.git/notyet/` or the hook settings is not prevented, only harder than fixing the bug.
- Your own edits *during* a session count as part of the change.

## Development

```sh
pip install -e ".[dev]"
pytest -q -n auto
PYTHONPATH=. python eval/notyet_scenarios.py /tmp/out.json      # sessions with known right answers
```

[docs/EVAL.md](docs/EVAL.md) has the results and how they were measured, [eval/README.md](eval/README.md)
how to rerun them, and [docs/PLAN.md](docs/PLAN.md) what's next.
