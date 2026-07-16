# Instructions and suites — the execution pipeline

`otto run` and `otto test` both dispatch ordinary Python through the same
shape: a registry entry and a synthesized Typer subcommand. An instruction
({func}`@instruction() <otto.cli.run.instruction>`) is a *procedure* — one
async function with full lab access, one body, one outcome. A suite
({class}`~otto.suite.suite.OttoSuite`) is a *verdict* — many independent
async test methods, run by the runner underneath: stock pytest with an otto
plugin layered on, not a bespoke test framework.

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

## Registration synthesizes the CLI

Both paths transform a plain signature into CLI flags with the **same
options-to-parameters machinery**: a parameter annotated with an options
dataclass has its fields — including inherited ones, which is how
repo-wide `RepoOptions` bases work ({doc}`../../guide/run/options`) —
expanded into individual flags, and the populated instance is reconstructed
at call time. One options hierarchy serves both instructions and suites.

For an **instruction**, `@instruction()` stores an entry in the
`INSTRUCTIONS` registry ({doc}`registries`) and builds a Typer sub-app around
the function. Besides options expansion, a parameter annotated `OttoContext`
is stripped from the CLI signature and injected from the active context at
call time — the DI-friendly way for an instruction to reach hosts without
global lookups ({doc}`../lifecycle`).

For a **suite**, a class extends {class}`~otto.suite.suite.OttoSuite` with a
`Test`-prefixed name (matching pytest's own `python_classes = Test*`
collection rule), which triggers `__init_subclass__` to call
{func}`~otto.suite.register.register_suite_class`. Registration does three
things at import time — for repo test files, during bootstrap phase 2
({doc}`../lifecycle`):

1. Reads the suite's `Options` class — any dataclass works; an `@options`
   pydantic dataclass adds validation — and synthesizes a Typer subcommand
   whose flags mirror its fields, via the options-to-parameters machinery
   above.
2. Registers the suite in the `SUITES` registry under its class name.
3. Makes re-registration idempotent *per source file*: pytest re-importing
   the same file is expected and harmless, while a second suite of the same
   name from a *different* file is a loud collision.

Because both live in a registry, tab completion of instruction and suite
names, and `--list-instructions` / `--list-suites`, come for free — like
every other registry ({doc}`registries`). See it captured live in
{doc}`../../guide/run/index` and {doc}`../../guide/test`.

## Handing off to pytest

A suite's synthesized subcommand builds the options instance and calls
`run_suite` ({func}`otto.suite.run.run_suite`), which invokes `pytest.main()`
scoped to the suite's source file, with otto's plugin installed. Conftest
loading is cut at the *owning repo's root* (`--confcutdir`), so the user
repo's full conftest hierarchy applies while otto's own never leaks in.
pytest keeps what it is good at — collection, fixtures, `parametrize`,
markers, reporting — and the plugin ({class}`~otto.suite.plugin.OttoPlugin`)
layers on otto's concerns:

- **Artifacts** — each test gets its own directory under the invocation's
  output dir ({doc}`../utilities/logging`), exposed to the suite as
  `testDir`.
- **Stability modes** — `--iterations` / `--duration` re-run tests via the
  runtest protocol and aggregate per-test pass rates, reporting `Unstable`
  rather than failing on the first flake.
- **Retry** — `@pytest.mark.retry(n)` re-runs a failing test in place.
- **Monitoring and coverage** — test start/end events are stamped onto the
  monitor timeline, and coverage runs fetch embedded counters after the
  session ({doc}`../subsystems/monitoring`, {doc}`../subsystems/coverage`).

## Selection runs

`--tests NAME[,NAME…]` and `-m`/`--markers` also work *without* naming a
suite: the selection path resolves test names to exact pytest nodeids
(unknown names fail with a did-you-mean), then runs **one pytest session per
matching repo** — a repo with no match is skipped rather than reported as
"collected 0 items". `--tests`/`-m` live on the parent `otto test` command,
while a suite name dispatches a distinct synthesized subcommand, so
combining the two is a loud usage error rather than a silent intersection.

This is the deliberate second door into the same pipeline: plain pytest
functions (no `OttoSuite` at all) are first-class here, which is what the
`otto init` scaffold demonstrates
([getting started](../../getting-started.md#your-first-test-suite)).

`--tests` tab-completion is fed by two layers. The always-available **floor**
is a static `ast` scan of `def test_*` / `Test*` methods — instant, never
runs your test code. On top of it sits a **pytest-collected** set that also
includes *dynamically generated* tests (`pytest_generate_tests`, conftest
fixtures) that a source scan can't see. That set is warmed by any real
collection: an `otto test --list-tests` run fills it for free, and otherwise
the first `--tests` TAB spawns a single bounded collection in the background
(a one-time slower TAB — capped, and it falls back to the floor if it can't
finish in time) and caches the result, so every later TAB is fast and
complete. A test-file edit invalidates the cache via the same fingerprint
the rest of the cache uses, so the collected set never goes stale silently.
The completer itself still **never runs user code** — the collection happens
in a disposable subprocess, never in the process answering the keystroke.
The behavior this feeds — base-name matching, `TestClass::test_name`
disambiguation — is documented in {doc}`../../guide/test`.

## Non-fatal assertions

`self.expect(...)` records a failed expectation — with the captured source
line and locals — and *keeps the test running*; the accumulated failures
raise one combined `AssertionError` at the end
({class}`~otto.suite.expect.ExpectCollector`). This exists because hardware
tests are expensive to reach: when a board takes minutes to provision, "check
everything, then fail with the full list" beats fail-fast.

## Suites vs instructions

Both ride the standard invoke preamble unmodified ({doc}`../lifecycle`);
what differs is the body. An instruction's body is just the user's coroutine
on the invocation's event loop, and its returned {class}`~otto.result.Result`
(if any) becomes the process exit code ({doc}`../utilities/results`);
artifacts belong in `get_context().output_dir` ({doc}`../../guide/run/index`).
A suite's body hands off to pytest, as above.

Both are registered callables with option classes; the split is intent.
Instructions ({func}`~otto.cli.run.instruction`) are *procedures* — deploy,
flash, collect — with one body and an exit code from their returned
{class}`~otto.result.Result`. Suites are *verdicts*: many independent test
methods, pytest semantics, stability statistics, per-test artifacts. Shared
repo-wide options classes keep the two consistent
({doc}`../../guide/run/options`).

## Where the code lives

- {mod}`otto.cli.run` — the `@instruction` decorator, the `INSTRUCTIONS`
  registry, and context injection
- `otto.suite` — `OttoSuite`, suite registration, `run_suite`,
  `OttoPlugin`, and `ExpectCollector`
- {mod}`otto.result` — the `Result` family that becomes an instruction's
  exit code
