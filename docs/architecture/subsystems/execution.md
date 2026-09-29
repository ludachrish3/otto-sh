# Instructions and tests — the execution pipeline

`otto run` and `otto test` both dispatch ordinary Python. An instruction
({func}`@instruction() <otto.cli.run.instruction>`) is a *procedure*: one
async function with full lab access, one body, one outcome, published as a
registry entry and a synthesized Typer subcommand. A test is a *verdict*:
plain pytest classes and functions, run by stock pytest with otto's plugins
layered on, not a bespoke test framework. `otto test` is a single command
that takes test names, not a group with a subcommand per class.

```{graphviz}
digraph testpipeline {
    rankdir=TB;
    node [shape=box];

    cli [label="otto test NAMES [-m EXPR] [flags]\nbuild every class registered for test\n→ bind on OttoContext"];
    resolve [label="before any session, across every repo: the files\nthe test-names cache says hold a name,\nplus changed files (a name placed nowhere:\nthose files first, else a did-you-mean)"];
    session [label="one pytest session per repo over its test dirs:\nOttoPlugin prunes to those files, matches\nthe names, records what it collected"];
    pytest_ [label="pytest\ncollection · fixtures · parametrize · markers"];
    plugin [label="OttoPlugin · OttoFixturesPlugin\nfixtures · artifact dirs · stability ·\nretry · monitor events · per-loop host\nsweep · coverage fetch after the session"];

    cli -> resolve -> session -> pytest_ -> plugin;
}
```

## Options are registered per verb

Both verbs build their flags with the **same options-to-parameters
machinery**: each field of an options class becomes a flag, and the
populated instance is reconstructed at call time
({doc}`../../cookbook/authoring/options-classes`).

- **Verb-wide options** are classes registered with
  {func}`~otto.params.register_options` (or `@options(verbs=[...])`) from an
  init module, for `run`, `test` or both. They live in the `OPTIONS` registry
  ({doc}`registries`). A verb's flag set is the union of its registered
  classes, merged by declaring class: a field two classes inherit from one
  base is one flag, and one name from two unrelated classes is an
  `OptionsCollisionError` naming both. At dispatch, every class registered
  for the dispatched verb is built from the parsed flags and bound on the
  invocation's `OttoContext`, so a validation failure is an exit-2 error
  before any body runs. Classes registered only for the other verb are not
  built.
- **An instruction's own options** (`@instruction(options=...)`) are merged
  with the verb's, the own class first. A parameter annotated with a
  registered class is injected with `ctx.options(Cls)`; a parameter annotated
  `OttoContext` is stripped from the CLI signature and injected from the
  active context, the DI-friendly way for an instruction to reach hosts
  without global lookups ({doc}`../lifecycle`).
- **Tests** read their options with `ctx.options(Cls)` through the
  session-scoped `ctx` fixture. There is no per-test or per-class options
  class: pytest's options are one flat namespace, and a flag that only one
  test file reads is rare, while one that some tests ignore is harmless.

`@instruction()` stores an entry in the `INSTRUCTIONS` registry
({doc}`registries`) and builds a Typer sub-app around the function, so tab
completion of instruction names and `--list-instructions` come for free, as
for every other registry. Tests are not registered anywhere: pytest finds
them.

## Handing off to pytest

{func}`~otto.suite.run.run_tests` backs both `otto test` and the library call.
It runs one `pytest.main()` per repo, over the repo's test directories, and
that one session both finds the names and runs what they select, in
collection order. There is no separate collection pass: pytest collects
once. {class}`~otto.suite.plugin.OttoPlugin` does the selecting inside the
session:

- **Pruning.** `pytest_ignore_collect` skips every file that is not a
  *candidate* and every directory holding none, so pytest imports only the
  candidates. The candidates come from the test-names cache
  (`otto.config.collected_tests`): the files whose last pytest collection
  held one of the names, plus every file that changed since, which one
  `stat` per file, directory and dependency detects. A directory whose stat
  moved (a file was added, removed or renamed in it) is taken whole: every
  file and subdirectory in it that pytest would collect, which is how a new
  file is found without otto listing a directory or matching `python_files`
  itself. The session's arguments stay the test directories, never file
  paths, so a conftest's `collect_ignore` still applies. Only pytest ever
  writes the cache: a repo with no cache is collected whole by the run's
  own session, which then seeds it.
- **Matching.** Every collected item is matched against the names
  ({func}`~otto.suite.selection.matches_name`) as pytest reports it, and the
  ones no name selects are deselected before `-m` or `-k` apply, so a name
  the marker expression excludes is still a known name that selects nothing.
- **Recording.** What each file held (its tests, markers, or the error that
  stopped it) is written back to the cache after the session, so the next
  run and tab completion start from what pytest actually collected. So are
  its *dependencies*: the other Python files its tests come from or could
  come from, read without a file operation from each item's function and
  class hierarchy and from every class, function and module in the module's
  namespace (a base class that has no test yet, whatever a star-import
  brought in, a config module whose flag decides whether a test exists). The
  walk sorts each value by `type()` alone, so it never reads an attribute of
  a user object (a lazy proxy stays unevaluated) and a value that raises is
  skipped. A recorded source path with no file is not kept; a sourceless
  library is tracked through its `.pyc`. A change to a dependency marks
  every file that uses it changed, so a test a base class gains, or that a
  star-imported module gains, is collected everywhere it lands. Which
  dependencies are tracked is on {doc}`completion-cache` ("Dependencies",
  under "The test-names cache").

Which files hold the names is decided before any session starts, across
every repo, from the caches alone. A name no trusted record holds can only
be in a file the cache can't vouch for (changed, new, in a directory that
gained an entry, or anywhere in a repo whose cache is cold). With one repo
holding such files, its own session collects them and must match the name
(a missing one stops it with a usage error before any test) and runs before
every other repo's; with several, each is collected first by a
`--collect-only` session. Either way, a collection that can't finish ends
the run before any test, with its exit code and the reason logged. A name
still placed nowhere is an
{class}`~otto.suite.selection.UnknownSelectionError` with did-you-mean
suggestions (and the files that did not collect), and no test runs in any
repo. A test generated from a data file or a plain imported value, which no
Python file's stat follows, is not seen until its own file is next collected
(the cases, and what to do about them, are in
{doc}`../../cli/test/selection`, "What the cache can't follow").
The remote coverage pre-clean waits for the first session that is about to
run a test, so a run that ends in an unknown name never touches a host. Every session of a run uses one random seed, logged once. A file that fails to collect is
logged and, under `--continue-on-collection-errors`, doesn't stop the tests
that were asked for; the run exits 1, as pytest does. Conftest loading is cut
at the *owning repo's root* (`--confcutdir`), so the user repo's full
conftest hierarchy applies while otto's own never leaks in.

Every session runs on one session-wide event loop by default
(`asyncio_default_test_loop_scope` and `asyncio_default_fixture_loop_scope`
are both `session`), the way every other otto command runs on one loop. A
test may pin a narrower loop; {doc}`../../cookbook/host-scopes` has the rules.

pytest keeps what it is good at (collection, fixtures, `parametrize`,
markers, reporting) and two plugins layer on otto's concerns:

- **Fixtures**
  ({class}`~otto.suite.pytest_plugin.OttoFixturesPlugin`): `ctx`,
  `module_dir`, `test_dir`, `expect` and `monitor`, plus the `ensure`
  marker's converge. `module_dir` and `test_dir` mirror the pytest test ID
  under the run's output directory ({class}`~otto.suite.layout.ArtifactLayout`),
  with a repo layer on top when more than one repo takes part.
- **Stability modes** ({class}`~otto.suite.plugin.OttoPlugin`):
  `--iterations` / `--duration` re-run tests via the runtest protocol and
  aggregate per-test pass rates, reporting `Unstable` rather than failing on
  the first flake. Setup and teardown run once around the repeated call
  phase, so no fixture re-fires per iteration; the protocol hook re-points
  `test_dir` at an `iteration_N` subdirectory itself, giving each repeat its
  own artifacts.
- **Retry**: `@pytest.mark.retry(n)` re-runs a failing test in place.
- **Per-loop host cleanup**: `OttoPlugin` names each pytest-asyncio runner's
  loop and, just before the runner closes it, closes the hosts that loop owns
  ({mod}`otto.suite.loops`). A host registers with the scope of the loop
  that first uses it, and fails fast with a
  {class}`~otto.host.loop_owner.HostLoopError` from any other loop that is
  still running.
- **Monitoring and coverage**: every test, class or plain function, gets a
  start banner, and under `--monitor` its start and end are stamped on the
  timeline; coverage runs fetch counters after the session
  ({doc}`monitoring`, {doc}`coverage/index`).

otto's own per-test async work, the `ensure` converge and the monitor events,
runs on the test's own loop through {func}`~otto.suite.loops.runner_for`,
whatever that loop's scope, so the hosts a converge opens belong to the loop
the test body uses.

Test files are imported only inside these pytest sessions. While one loads
test files, the registries refuse any registration from outside the `otto`
package ({class}`~otto.registry.RegistrationRefused`), because a test file
that registered an extension would register it for `otto test` alone.

## Test names and completion

A name is a test, a class, or a `Class::test` path, matched against each
collected test's classes and base name
({func}`~otto.suite.selection.matches_name`); unknown names fail with a
did-you-mean. `--list-tests` prints the collected selection grouped by repo,
module and class, and a dry run prints the same tree: both take the run's own
sessions with `--collect-only`, so they import the files a run would and run
nothing.

Test-name and `-m` completion read the same test-names cache a run
uses, so they offer what pytest collected, inherited and generated tests
included; nothing but pytest ever reads a test file for it. The process
answering a keystroke never runs user code: a cold cache is seeded, and a
warm one refreshed, by a disposable, time-bounded child process. What a user
sees is on {doc}`../../cli/test/selection` ("Tab-completing names"), and how
the per-file tables, the check window and the child work is on
{doc}`completion-cache` ("The test-names cache"). The path from `otto init` to a
first run is in [getting started](../../getting-started/running-tests.md).

## Non-fatal assertions

The `expect` fixture records a failed expectation — with the captured source
line and locals — and *keeps the test running*; the accumulated failures
fail the test at the end of its body, in the call phase
({class}`~otto.suite.expect.ExpectCollector`). This exists because hardware
tests are expensive to reach: when a board takes minutes to provision, "check
everything, then fail with the full list" beats fail-fast.

## Tests vs instructions

Both ride the standard invoke preamble unmodified ({doc}`../lifecycle`);
what differs is the body. An instruction's body is just the user's coroutine
on the invocation's event loop, and its returned {class}`~otto.result.Result`
(if any) becomes the process exit code ({doc}`../utilities/results`);
artifacts belong in `get_context().output_dir` ({doc}`../../cli/run/index`).
`otto test`'s body hands off to pytest, as above.

The split is intent. Instructions ({func}`~otto.cli.run.instruction`) are
*procedures* (deploy, flash, collect) with one body and an exit code from
their returned {class}`~otto.result.Result`. Tests are *verdicts*: many
independent tests, pytest semantics, stability statistics, per-test
artifacts. Options classes registered for both verbs keep the two consistent
({doc}`../../cookbook/authoring/options-classes`).

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
  something to degrade around. Flags merge by declaring class. Under
  `otto test`, an `ensure` converge builds each body's options from the
  `test` verb's parsed flags, by field name, exactly as `otto run install`
  builds them from its own; the collision rule is what makes the name
  unambiguous, and a body field with no `test` flag takes its default.
  Options modules are leaves — `typer` and `otto.options` — so a
  shared base placed in a required repo or a library package can never
  create an import cycle.
- **Never let one repo act for another.** No default `ProjectActions` body
  spells `owner=`; the instance gets it from the repo view `actions_for`
  builds (`ctx.for_repo(repo.name)`). That is why constructing one by hand
  with a plain `OttoContext` raises `TypeError`: the object would walk the
  whole union *and* call every host verb with `owner=None`, which the host
  layer reads as **every** owner's products, so its `cleanup()` would
  uninstall the neighbours' products and report success.
- **Never let a repo own what a host or the lab owns.** The defaults refuse
  per-repo debug logs and toolchain tools: N repos each sweeping one host's
  debug logs is N transfers each overwriting the last, and one toolchain
  serves every owner on a host, so a repo removing it would take its
  neighbours' tooling with it. Impairments and tunnels belong to the lab,
  and nothing in a repo's products or dev tools put them there. All of these
  run once, above the repo walk.
- **Never report success over nothing.** A `pattern=` that selects no host
  raises, because a silently empty sweep is the one failure worse than a
  crash: it reports success over a lab nothing happened on.
- **Never let scoping lock anyone out.** Explicit targeting is never scoped
  ({doc}`bootstrap`, "Project activation"). A driving project whose
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
- **Never let a probe and its remedy drift.** `is_clean()` answers for exactly
  what `cleanup` removes. A *foreign* qdisc therefore leaves the lab "clean":
  `cleanup` will not remove one, and reporting it dirty would send every
  `clean` ensure step into a cleanup that cannot change the answer. Once one
  axis cannot answer, the axes after it are not read: they could only
  strengthen a verdict that is already unavailable.
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
- {mod}`otto.params` — `@options`, `register_options`, the `OPTIONS`
  registry and the per-verb merge
- `otto.suite` — `run_tests`, name resolution (`otto.suite.selection`),
  `OttoPlugin`, `OttoFixturesPlugin`, the artifact layout, the per-loop
  sweep (`otto.suite.loops`), the `monitor` fixture's `MonitorHandle`, and
  `ExpectCollector`
- {mod}`otto.cli.test` — the `otto test` command
- {mod}`otto.result` — the `Result` family that becomes an instruction's
  exit code
