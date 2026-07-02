# The test pipeline

`otto test` is a thin, deliberate bridge: suites are ordinary classes, tests
are ordinary async methods, and the runner underneath is stock pytest with an
otto plugin — not a bespoke test framework.

## From class to subcommand

A suite extends {class}`~otto.suite.suite.OttoSuite` and registers with
{func}`~otto.suite.register.register_suite`. The decorator does three things
at import time (which, for repo test files, means during bootstrap phase 2 —
{doc}`lifecycle`):

1. Reads the suite's `Options` class (an `@options` pydantic dataclass —
   {doc}`../guide/options`) and synthesizes
   a Typer subcommand whose flags mirror its fields — the same
   options-to-parameters machinery instructions use, so suites and
   instructions share option classes ({doc}`../guide/options`).
2. Registers the suite in the `SUITES` registry under its class name.
3. Makes re-registration idempotent *per source file*: pytest re-importing
   the same file is expected and harmless, while a second suite of the same
   name from a *different* file is a loud collision.

`otto test <SuiteName>` therefore gets registry-backed completion and
`--list-suites` for free, like every other registry ({doc}`registries`).

## Handing off to pytest

The suite's synthesized subcommand builds the options instance and calls
`run_suite` (`otto/cli/test.py`), which invokes `pytest.main()` scoped to the
suite's source file, with otto's plugin installed. pytest keeps what it is
good at — collection, fixtures, `parametrize`, markers, reporting — and the
plugin ({class}`~otto.suite.plugin.OttoPlugin`) layers on otto's concerns:

- **Artifacts** — each test gets its own directory under the invocation's
  output dir ({doc}`results-and-logging`), exposed to the suite as `testDir`.
- **Stability modes** — `--iterations N` / `--duration` re-run tests via the
  runtest protocol and aggregate per-test pass rates, reporting `Unstable`
  rather than failing on the first flake.
- **Retry** — `@pytest.mark.retry(n)` re-runs a failing test in place.
- **Monitoring and coverage** — test start/end events are stamped onto the
  monitor timeline, and coverage runs fetch embedded counters after the
  session ({doc}`monitoring-and-coverage`).

## Non-fatal assertions

`self.expect(...)` records a failed expectation — with the captured source
line and locals — and *keeps the test running*; the accumulated failures
raise one combined `AssertionError` at the end
({class}`~otto.suite.expect.ExpectCollector`). This exists because hardware
tests are expensive to reach: when a board takes minutes to provision, "check
everything, then fail with the full list" beats fail-fast.

## Suites vs instructions

Both are registered callables with option classes; the split is intent.
Instructions ({func}`~otto.cli.run.instruction`) are *procedures* — deploy,
flash, collect — with one body and an exit code from their returned
{class}`~otto.result.Result`. Suites are *verdicts*: many independent test
methods, pytest semantics, stability statistics, per-test artifacts. Shared
repo-wide options classes keep the two consistent
({doc}`../guide/options`).
