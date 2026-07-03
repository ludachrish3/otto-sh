# otto test — the test pipeline

`otto test` is a thin, deliberate bridge: suites are ordinary classes, tests
are ordinary async methods, and the runner underneath is stock pytest with an
otto plugin — not a bespoke test framework.

```{graphviz}
digraph testpipeline {
    rankdir=TB;
    node [shape=box];

    import [label="bootstrap phase 2 imports test files"];
    reg [label="OttoSuite.__init_subclass__\nTest*-named subclass →\nregister_suite_class → SUITES registry\n+ synthesized Typer subcommand"];
    suite [label="otto test <Suite> [flags]\nbuild Options instance → run_suite\none pytest session, the suite's file"];
    select [label="otto test --tests a,b / -m EXPR\nsuite-less selection run:\nresolve names → one pytest\nsession per matching repo"];
    pytest_ [label="pytest\ncollection · fixtures · parametrize · markers"];
    plugin [label="OttoPlugin\nper-test artifact dirs · stability\nmodes · retry · monitor events ·\ncoverage fetch after the session"];

    import -> reg;
    reg -> suite;
    reg -> select [style=dashed, label=" names feed\nresolution"];
    suite -> pytest_;
    select -> pytest_;
    pytest_ -> plugin;
}
```

## From class to subcommand

A suite extends {class}`~otto.suite.suite.OttoSuite` with a `Test`-prefixed
class name (matching pytest's own `python_classes = Test*` collection rule),
which triggers `__init_subclass__` to call
{func}`~otto.suite.register.register_suite_class`. Registration does three
things at import time (which, for repo test files, means during bootstrap
phase 2 — {doc}`index`):

1. Reads the suite's `Options` class — any dataclass works; an `@options`
   pydantic dataclass adds validation ({doc}`../../guide/options`) — and
   synthesizes a Typer subcommand whose flags mirror its fields: the same
   options-to-parameters machinery instructions use, so suites and
   instructions share option classes.
2. Registers the suite in the `SUITES` registry under its class name.
3. Makes re-registration idempotent *per source file*: pytest re-importing
   the same file is expected and harmless, while a second suite of the same
   name from a *different* file is a loud collision.

`otto test <SuiteName>` therefore gets registry-backed completion and
`--list-suites` for free, like every other registry ({doc}`../subsystems/registries`):

```{raw} html
:file: ../../_static/generated/termynal/complete-suites.html
```

## Handing off to pytest

The suite's synthesized subcommand builds the options instance and calls
`run_suite` (`otto/cli/test.py`), which invokes `pytest.main()` scoped to the
suite's source file, with otto's plugin installed. Conftest loading is cut at
the *owning repo's root* (`--confcutdir`), so the user repo's full conftest
hierarchy applies while otto's own never leaks in. pytest keeps what it is
good at — collection, fixtures, `parametrize`, markers, reporting — and the
plugin ({class}`~otto.suite.plugin.OttoPlugin`) layers on otto's concerns:

- **Artifacts** — each test gets its own directory under the invocation's
  output dir ({doc}`../utilities/logging`), exposed to the suite as `testDir`.
- **Stability modes** — `--iterations N` / `--duration` re-run tests via the
  runtest protocol and aggregate per-test pass rates, reporting `Unstable`
  rather than failing on the first flake.
- **Retry** — `@pytest.mark.retry(n)` re-runs a failing test in place.
- **Monitoring and coverage** — test start/end events are stamped onto the
  monitor timeline, and coverage runs fetch embedded counters after the
  session ({doc}`monitor`, {doc}`cov`).

## Selection runs: pytest-native, no suite required

`--tests NAME[,NAME…]` and `-m/--markers EXPR` also work *without* naming a
suite. The selection path resolves test names to exact pytest nodeids
(unknown names fail with a did-you-mean), then runs **one pytest session per
matching repo** — a repo with no match is skipped rather than reported as
"collected 0 items", and per-repo `--results` files fan out automatically.
Combining `--tests` with a suite subcommand is a loud usage error, not a
silent intersection. Suite `Options` are default-constructed per class in
selection runs; a suite whose options are *required* points you back at the
single-suite form.

This is the deliberate second door into the same pipeline: plain pytest
functions (no `OttoSuite` at all) are first-class here, which is what the
{doc}`init <init>` scaffold demonstrates.

`--tests` tab-completes, from a **static source scan** of `def test_*` /
`Test*` methods — bare functions and suite methods alike, and never running
your test code at tab time:

```{raw} html
:file: ../../_static/generated/termynal/complete-test-names.html
```

The honest boundary: a source scan can't see what only exists after pytest
collection — parametrized-only ids and dynamically generated tests
(`pytest_generate_tests`, conftest fixtures) aren't offered. When you need
the fully-expanded list, `otto test --list-tests` runs a real collection
pass and prints it. This is the standing trade-off in otto's completion: it
buys "never runs user code at tab time" by completing what's *statically or
cache-visible*, not what a live collection would enumerate.

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
({doc}`../../guide/options`).

## `otto test --help`

```{raw} html
:file: ../../_static/generated/termynal/help-test.html
```
