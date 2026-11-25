# notyet

**When your coding agent says it's done, notyet checks whether it is.**

notyet is a completion gate for AI coding agents working in Python repos with pytest. When the
agent tries to stop, notyet runs the tests its change reaches, compares them with the tree as it
was when the session started, and sends the agent back if something that worked is broken, or was
made to look like it works. You get a receipt: what was checked, what was found, what wasn't checked.

It is deterministic. No model judges anything; every block is backed by a test that passed before
and fails now. It has no dependencies, and keeps its state in `.git/notyet/`.

```
notyet: not done yet. 2 item(s) to resolve before stopping.

1. [block] tests/test_cart.py::test_checkout_total passed at session start and fails now  id=9c1f02aa
   assert 25.0 == 27.5
   -> Fix the code so it passes again. Don't change the test to match new behavior unless
      that behavior change was asked for.
2. [block] 3 test(s) pass only with this session's changes to tests/conftest.py; with the
   session-start test setup they fail on the new code  id=4be7d310
   -> Fix the code so they pass with the original setup. If the behavior change was asked for,
      hand it to the user: `ack <id> --needs-human "the change that was requested"`.
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
| New ruff or pyright errors (also in the callers); new `# noqa`, `# type: ignore`, ... | fix or justify |
| Failures that were there before; flaky tests | note |

- **block:** cleared by a fix, or by handing it to you with a reason. Then the receipt says
  NEEDS YOUR REVIEW, never PASSED.
- **fix or justify:** the agent may acknowledge it with a reason, which you see.
- Levels can be changed per rule (`[rules]` in `.notyet.toml`).

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

or `notyet check --base origin/main --merge-base [--format json|github] [--fail-on block|resolve|never]`.
An item is accepted by giving the reason where reviewers see it, in a commit message:

```
Notyet-Ack: 9c1f02aa totals no longer include tax, as decided in #412
```

## Does it work?

EVAL_PLACEHOLDER

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
- It knows what the tests know. Work that is *incomplete* in a way no test expresses, or hard-coded to
  the exact inputs the tests use, gets through.
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
