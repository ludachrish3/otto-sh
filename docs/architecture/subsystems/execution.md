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
repo-wide `RepoOptions` bases work ({doc}`../../library/options-classes`) —
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
{doc}`../../cli/run/index` and {doc}`../../cli/test/index`.

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
  output dir ({doc}`../utilities/logging`), requested by a test as the
  `test_dir` fixture (`suite_dir` for the suite-wide one).
- **Stability modes** — `--iterations` / `--duration` re-run tests via the
  runtest protocol and aggregate per-test pass rates, reporting `Unstable`
  rather than failing on the first flake. Setup and teardown run once around
  the repeated call phase, so no fixture re-fires per iteration; the protocol
  hook re-points `test_dir` at an `iteration_N` subdirectory itself, giving
  each repeat its own artifacts.
- **Retry** — `@pytest.mark.retry(n)` re-runs a failing test in place.
- **Monitoring and coverage** — test start/end events are stamped onto the
  monitor timeline, and coverage runs fetch embedded counters after the
  session ({doc}`monitoring`, {doc}`coverage/index`).

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
([getting started](../../getting-started/running-test-suites.md)).

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
disambiguation — is documented in {doc}`../../cli/test/index`.

## Non-fatal assertions

The `expect` fixture records a failed expectation — with the captured source
line and locals — and *keeps the test running*; the accumulated failures
fail the test at the end of its body, in the call phase
({class}`~otto.suite.expect.ExpectCollector`). This exists because hardware
tests are expensive to reach: when a board takes minutes to provision, "check
everything, then fail with the full list" beats fail-fast.

## Suites vs instructions

Both ride the standard invoke preamble unmodified ({doc}`../lifecycle`);
what differs is the body. An instruction's body is just the user's coroutine
on the invocation's event loop, and its returned {class}`~otto.result.Result`
(if any) becomes the process exit code ({doc}`../utilities/results`);
artifacts belong in `get_context().output_dir` ({doc}`../../cli/run/index`).
A suite's body hands off to pytest, as above.

Both are registered callables with option classes; the split is intent.
Instructions ({func}`~otto.cli.run.instruction`) are *procedures* — deploy,
flash, collect — with one body and an exit code from their returned
{class}`~otto.result.Result`. Suites are *verdicts*: many independent test
methods, pytest semantics, stability statistics, per-test artifacts. Shared
repo-wide options classes keep the two consistent
({doc}`../../library/options-classes`).

## Project instructions

The six first-party project instructions — `install`, `uninstall`,
`cleanup`, `get-logs`, `install-tools`, `status` ({doc}`../../cli/run/defaults`)
— are declared with the same decorator and table a repo uses, so nothing about
them is special-cased and a repo extends the interface by the mechanism otto
used to write it.

A repo customizes `install` and the other project instructions only through
its `ProjectActions` subclass; a standalone instruction of the same name is
refused at startup. `otto run install`, `await otto.project.install()` and
`@pytest.mark.ensure("installed")` therefore run the same bodies, so the lab a
test converges is the lab a person installed by hand. The first declaration
of a project instruction also fixes its *walk shape* — the walk direction,
whether it continues past a failing repo, whether it requires dependencies,
and how results combine and render — and a later declaration may not change
any of it, so no repo can reshape the six first-party instructions.

The rest of the design follows from what a walk across many repos must never
do:

- **Never build on a known gap, never strand a teardown.** Composition is by
  iteration over resolved repos in dependency order, not cross-repo
  subclassing — a class that may be absent cannot be subclassed, and an
  optional dependency may well be. Building is fail-fast because installing a
  dependent on top of a dependency known to be missing produces a lab nobody
  can reason about; tearing down is best-effort because a repo that will not
  come down must not strand the ones behind it.
- **Never let one repo reshape another's command.** An override's options
  class must inherit otto's class for that name; otherwise one repo overriding
  `install` would take `--ensure` off the command for everyone, and
  `super().install(opts)` would have no field to read. Two repos declaring the
  same flag from different classes is a bootstrap error for the whole
  invocation: a flag whose meaning depends on which repo reads it is not
  something to resolve silently, and a flag set the user cannot see is not
  something to degrade around. Options modules are leaves — `typer` and
  `otto.options` — so a shared base placed in a required repo or a library
  package can never create an import cycle.
- **Never report success over nothing.** A `pattern=` that selects no host
  raises, because a silently empty sweep is the one failure worse than a
  crash: it reports success over a lab nothing happened on.
- **Never let scoping lock anyone out.** Explicit targeting (`otto host <id>`,
  `ctx.get_host`) is never scoped: a repo that has to hop through a machine it
  does not own must still be able to name it, and a scoping typo must never
  brick the one command that could diagnose it. A driving project whose
  declaration admits no host aborts, but a dependency's is only skipped — one
  project's scoping must not veto another project's run.
- **Never blur install state.** `status` exits with three codes rather than a
  boolean, for the same reason the state is a tri-state: a half-installed lab
  and a clean one need different handling, and reporting them alike is how
  remnants get installed over. `status --full` leaves that exit code alone —
  scripts branch on it, and folding a second axis in would change the answer
  to a question nobody re-asked. `is_clean()` raises on a state nobody could
  read, because a converge must not clean on a non-fact, while the `--full`
  display shows it as `unknown`, because an unreachable host must not hide the
  others; both read the same probes, so the display is not `is_clean()` with
  the exception swallowed. In the `cleanliness()` aggregate a dirty row
  outranks an unreadable one: an answer already in hand is not discarded for a
  scan that fell short.
- **Never bury a real refusal in noise.** `cleanup` drops links that could
  never have been impaired before reporting; otherwise `Success` would be
  unreachable on every real lab (an N-host lab resolves at least N implicit
  ids), each message would carry N lines nobody can act on, and a genuine
  foreign-qdisc refusal would be indistinguishable from that standing noise.
- **Never cut the path teardown still needs.** `cleanup`'s tunnel reap runs
  last because a tunnel can *be* the access path to a host, and reaping it
  earlier would sever the connection the repo walk, the log sweep and the
  toolchain removal still need; the impairment reset sits immediately before
  it because clearing delay and loss only improves the path everything above
  ran over.

## Where the code lives

- {mod}`otto.cli.run` — the `@instruction` decorator, the `INSTRUCTIONS`
  registry, and context injection
- `otto.suite` — `OttoSuite`, suite registration, `run_suite`,
  `OttoPlugin`, and `ExpectCollector`
- {mod}`otto.result` — the `Result` family that becomes an instruction's
  exit code
