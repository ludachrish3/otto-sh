# Using otto as a Python library

otto is not limited to the `otto` CLI. You can use it directly in your own
async Python scripts — for example, one-off automation, CI tooling, or
integration scripts that operate on lab hosts without needing tests or
instructions. otto ships inline type annotations under [PEP 561](https://peps.python.org/pep-0561/)
(a `py.typed` marker in the installed package), so a consumer's type checker
sees otto's real signatures rather than treating `import otto` as untyped.

## Imports are side-effect-free; `open_context()` runs the composition root

`import otto` and `import otto.config` do no I/O and run no project code.
`import otto` implements PEP 562 lazy exports (each public name resolves its
source module only on first attribute access), so a bare import stays cheap even
in a process that never touches a lab. `import otto.config` is
side-effect-free (no repo discovery, no user code) but eagerly imports its
submodules. Nothing under `.otto/settings.toml` `init` is imported just because
`otto` is on `sys.path` — that happens in {func}`otto.bootstrap.bootstrap`.

The composition root — repo discovery plus importing every configured `init`
module — is {func}`otto.bootstrap.bootstrap`, and it is idempotent (repeated
calls return the same cached result). `open_context()` calls it for you
before loading the lab, so any `@instruction`, `@cli_command()`, or
`register_*_backend()` call in your project's `init` modules has already run
by the time the `async with` block starts. Test files are not imported
here; only a pytest session, such as the one {func}`~otto.suite.run.run_tests`
starts (see below), imports them:

```python
async with otto.open_context(lab="mylab") as ctx:
    ...  # your project's registered components are all live here
```

If you're wiring up a custom embedding that bypasses `open_context()` — for
example, driving `OttoContext`/`set_context()` manually as shown below — call
`otto.bootstrap.bootstrap()` yourself first if you need those registrations
available. Skipping it isn't an error; it just means your script only sees
otto's own built-ins, not anything your project registers in `init`.

## Logging

Logging follows the same rule as everything above: importing otto
configures nothing. A bare `import otto` attaches only a `NullHandler` to
the `'otto'` logger — no handlers on the root logger, no logger levels
touched. Opt in with one of two calls; either is undone with
{func}`reset <otto.logger.management.reset>`.

A script that works with your repos calls
{func}`otto.session.install_logging`, the repo-aware opt-in: otto's console
plus every repo's {ref}`[logging.levels] <logging-levels>` and the host-output
filter, as the CLI installs them:

```python
from otto.session import install_logging

install_logging(log_level="INFO")
```

Underneath it is {func}`install <otto.logger.management.install>`, the raw
primitive, which knows nothing of repos:

```python
import logging

from otto.logger import install, reset

install(log_level="INFO")

logger = logging.getLogger(__name__)
logger.info("reachable through otto's console the moment install() returns")
```

`install()` attaches otto's console handler to the **ROOT** logger — the
same handler topology the CLI uses. Library mode is one of three postures
covering every way otto's process gets configured; see
[Root capture: three postures](../architecture/utilities/logging.md#root-capture-three-postures)
for how it fits alongside the CLI and inner-pytest postures. Every logger
in the process (your script, otto's own code, any library either imports)
is captured with nothing to register. Pass `output_dir=` to add the
`console.log` / `verbose.log` file pair otto's CLI writes; `overrides=`
merges a per-logger noise-floor table over otto's own defaults, the same
table {ref}`[logging.levels] <logging-levels>` configures for the CLI —
and ACCUMULATES across repeated `install()` calls, so a later call that
omits a name the first one set does not retract it. Calling `install()`
again is otherwise safe — a repeat call re-levels or re-wires in place
rather than duplicating handlers.

`reset()` undoes it: root goes back to what `install()` found (handler set
and level), and every logger the noise floor touched — defaults and
`overrides=` alike — returns to `NOTSET`. Useful between runs in a
long-lived process, and in tests that want a clean slate.

## Recommended: `open_context()`

`open_context()` is the single entry point for library use. It prepares the
run `otto --lab` would, installs the active context, enters the host
lifecycle scope, yields the context, and tears everything down on exit — even
if your code raises.

```python
import asyncio
import otto


async def main():
    async with otto.open_context(lab="mylab") as ctx:
        results = await ctx.run_on_all_hosts("uname -a")
        for host_id, result in results.items():
            print(host_id, result)
    # every host opened in the block is closed here, deterministically


asyncio.run(main())
```

Inside the block the context is the active one, so the zero-argument accessors
work without passing `ctx` around:

```python
async with otto.open_context(lab="mylab") as ctx:
    # explicit path
    for host in ctx.all_hosts():
        await host.run("uptime")

    # or the zero-argument bare accessors — same result
    for host in otto.all_hosts():
        await host.run("uptime")
```

`open_context` accepts:

| Parameter                | Type                         | Default | Description                               |
|--------------------------|------------------------------|---------|-------------------------------------------|
| `lab`                    | `Lab \| str \| list[str]`    | —       | Lab name(s) as `--lab` takes them (`"a+b"`, or a list, each item like one `--lab`), or a `Lab` object, used as given |
| `include_projects`       | `list[str] \| None`          | `None`  | Force these projects active (one name per item, no comma lists) |
| `exclude_projects`       | `list[str] \| None`          | `None`  | Switch these projects off (one name per item) |
| `variant`                | `"debug" \| "field" \| None` | `None`  | The product variant, as `otto --field/--debug` selects it ({ref}`variants <product-variants>`); `None` keeps the one already set (`"debug"` by default) |
| `dry_run`                | `bool`                       | `False` | Log commands without executing them       |
| `log_command_output`     | `bool`                       | `True`  | Stream command output to the otto logger  |
| `holder`                 | `str \| None`                | `None`  | Check reservations as this user, as `--holder` does |
| `skip_reservation_check` | `bool`                       | `False` | Skip the reservation check, as `-R` does; logs a warning |

A `Lab` you pass keeps the products it was built with, so build it under the
same `variant` (`set_variant` before `build_lab`), while containers started in
the block (`compose_up`) are ingested under the variant you pass.

The project switches follow {doc}`../cli/projects`. Given names, `open_context`
builds the lab `otto --lab` builds: from the repos' `[[lab.sources]]`, their
merged `[host_preferences]`, the inventory, and the placeholder hosts of their
declared containers (see {doc}`../configuration/host-sources`). It refuses
where the CLI refuses, before your block runs:
{class}`~otto.session.ProjectSelectionError`,
{class}`~otto.session.RepoLoadError`, {class}`~otto.session.LabBuildError` or
{class}`~otto.session.DependencyRefusedError`; then, once the lab is known,
{class}`~otto.reservations.check.MissingReservationError` or
{class}`~otto.reservations.check.ReservationBackendError`.
{doc}`../api/session` says what each session error carries. A `[reservations]`
table that is misconfigured (no `[reservations.json] path`, an unknown backend
name) raises `ValueError` from the backend factory. A malformed lab string
(`""` or `"a++b"`) raises `ValueError`, and lab data that is malformed only
when it loads propagates as
{class}`otto.labs.LabRepositoryError <otto.labs.errors.LabRepositoryError>`.
For a lab from somewhere else — built in memory (see
[In-memory labs](#in-memory-labs-no-lab-file)) or loaded by your own code —
pass the `Lab` object. It is used as given — no sources, preferences or
placeholders are added — and the checks still run against it.

What does not refuse is logged as a warning on the `otto.context` logger: the
load error of a repo that is inactive for this run, and the dependency
preflight's warnings. Because otto attaches only a `NullHandler` to the
`'otto'` logger (see [Logging](#logging)), you see them once you configure logging yourself or call
`otto.session.install_logging`. A refusal carries them too, so they are not lost
when the block never runs: `RepoLoadError.demoted` and
`DependencyRefusedError.warnings`.

## Bring-your-own-CLI: lower-level primitives

`open_context` runs these steps, and each is public, so you can run them
yourself (the `otto.session` functions are on {doc}`../api/session`):

1. `bootstrap()` discovers the repos and imports their `init` modules.
2. {func}`~otto.session.select_projects` validates `include_projects` /
   `exclude_projects`.
3. {func}`~otto.session.check_repos` refuses if an active repo failed to load, and
   returns the load errors of inactive repos, which `open_context` logs.
4. For the field variant, call `otto.context.set_variant("field")` and keep
   the token it returns. The lab build picks each product's
   {ref}`variant <product-variants>` entry, and providers read the variant
   while the work runs, so set it before step 5 and reset it after step 10.
5. {func}`~otto.session.build_lab` builds the lab, unless you passed a `Lab` object.
   It takes component lab names already split (`["a", "b"]`, not `"a+b"`).
6. {func}`~otto.reservations.build_reservation_gate` builds the reservation
   gate from the repos' `[reservations]` (an unbuildable backend refuses here,
   before any context exists). Skip it, as `-R` does, with
   `skip_reservation_check=True`.
7. Build an `OttoContext` with that lab, the selection's include / exclude
   (`include_projects=`, `exclude_projects=`) and the runtime flags, and
   install it as the active context with `set_context()`, which returns a
   reset token.
8. {func}`~otto.session.check_dependencies` runs the
   [dependency preflight](../cli/env/index.md#the-dependency-preflight). It needs
   the installed context, because whether a repo is active depends on the
   lab's hosts.
9. `gate.evaluate()` ({meth}`~otto.reservations.check.ReservationGate.evaluate`)
   refuses with `MissingReservationError` when you do not hold what the lab
   needs. It runs last, as on the CLI.
10. Do the work. Each host that connects joins the host scope of the event loop
   it connects on. On the way out, `ctx.sweep_loop(...)` closes the hosts the
   running loop owns, then `reset_context(token)` restores the prior state,
   and `otto.context.reset_variant(variant_token)` restores the prior variant
   if you set one.

The smallest version keeps steps 1, 5, 7 and 10:

```python
import asyncio

from otto.bootstrap import bootstrap
from otto.context import OttoContext, reset_context, set_context
from otto.session import build_lab

lab = build_lab(bootstrap().repos, ["mylab"])
ctx = OttoContext(lab=lab, dry_run=False)
token = set_context(ctx)
try:
    # your work here
    ...
finally:
    try:
        await ctx.sweep_loop(asyncio.get_running_loop(), label="my script")
    finally:
        reset_context(token)
```

Skipping `check_repos` never widens a fleet walk. The context's first fleet
walk raises {class}`~otto.session.RepoLoadError` for a load error that
`check_repos` would have refused, and the error from a broken environment
(such as `OTTO_SUT_DIRS` naming a directory that does not exist) propagates as
raised.

Add the checks you want back in the same order. If you add
`select_projects`, pass its result into the context as
`OttoContext(..., include_projects=tuple(selection.include),
exclude_projects=tuple(selection.exclude))`: `check_dependencies` decides which repos
are active from the context's switches. Use this form when you need
fine-grained control — for instance, when a framework drives the event loop
and you cannot use `async with` at the top level.

To dispatch a registered instruction by name instead of importing and calling
it directly, see "Calling an instruction by name" in
{doc}`authoring/writing-instructions`.

## Host lifetimes

There are three patterns for managing individual host connections inside an
`open_context` block. All three are safe — the scope provides the backstop.

**(a) Tight scoping with `async with`:**

```python
async with otto.open_context(lab="mylab") as ctx:
    async with ctx.get_host("router1") as host:
        await host.run("show version")
    # host.close() was called here; connection is gone
```

**(b) Pass the host around; let the scope close it:**

```python
async with otto.open_context(lab="mylab") as ctx:
    host = ctx.get_host("router1")
    await configure(host)  # pass it wherever you like
# the scope sweep closes host when the block exits
```

**(c) Explicit `await host.close()`:**

```python
async with otto.open_context(lab="mylab") as ctx:
    host = ctx.get_host("router1")
    await host.run("reboot")
    await host.close()  # early close — idempotent; scope sweep is a no-op
```

`close()` is idempotent: calling it multiple times is safe.

## FD-model caveat

A host joins a scope when it first connects, on whatever event loop it connects
on, and only while a context is active. A host you construct **directly**
(e.g. `UnixHost(...)`) and use outside any context has no scope backstop — it
is yours to close, exactly like an explicitly-opened file descriptor. Use
`async with` or `await h.close()`.

A host's connection belongs to the event loop that opened it. Using the host
from a different event loop while that one is still running raises
{class}`~otto.host.loop_owner.HostLoopError`; close the host on its own loop
first. Once the loop that opened it has closed, the next use simply reconnects.

`open_context` applies the reservation gate the way `otto --lab …` does: when a
repo's `[reservations]` configures a backend and you do not hold what the lab
needs, it raises {class}`~otto.reservations.check.MissingReservationError`,
whose `.report` lists each missing resource and its holders. `holder="name"`
checks as another user (`--holder`); `skip_reservation_check=True` is the `-R`
break-glass, and logs a loud warning. With no `[reservations]` anywhere, the
gate does nothing. To build and present the gate in your own CLI, see
{doc}`Using the reservation library in your own CLI <extending/reservation-backends>`.

## In-memory labs (no lab file)

You do not need a `lab.json` on disk. Build a `Lab` from host dicts, install
it as the active context, and the zero-argument selectors (`all_hosts`,
`get_host`) operate on it directly — useful for tests and ad-hoc scripts.
A host dict describes the host alone; its element is a separate argument, the
same `Element` every host of that element shares. Selection touches no
network, so this runs as-is:

```{doctest}
>>> import re
>>> from otto.host import Element, create_host_from_dict
>>> from otto.lab import Lab
>>> from otto.context import OttoContext, set_context, reset_context
>>> from otto.lab import all_hosts, get_host
>>> hosts = [create_host_from_dict(spec, element=element) for spec, element in [
...     ({"ip": "10.0.0.11", "creds": [{"login": "admin", "password": "x"}]}, Element("test1")),
...     ({"ip": "10.0.0.12", "creds": [{"login": "admin", "password": "x"}]}, Element("test2")),
... ]]
>>> lab = Lab(name="unix", hosts={h.id: h for h in hosts})
>>> token = set_context(OttoContext(lab=lab))
>>> [h.element.name for h in all_hosts(re.compile("test2"))]
['test2']
>>> get_host("test1").element.name
'test1'
>>> reset_context(token)
```

The trailing `reset_context` restores the prior active context — always pair it
with `set_context` (or use `otto.open_context`, which does both for you).

## Running tests from Python

`otto test` is a thin CLI wrapper around {func}`~otto.suite.run.run_tests`,
which runs tests through `pytest.main()` and returns a
{class}`~otto.suite.run.SuiteRunResult` instead of exiting the process.
{func}`~otto.suite.run.run_tests` and {class}`~otto.suite.run.RunOptions` are
exported at the top level (`otto.run_tests`, `otto.RunOptions`); the
exceptions below stay one level down, at
`otto.suite` / `otto.suite.run` / `otto.suite.selection`. `RunOptions` is
the `otto test` options class itself: what a field implies on the command
line (`cov_report_dir` implies `cov_report` implies `cov`) it implies for a
Python caller, and a contradiction (`cov=False` with a destination, a
`cov_report_dir` that is or contains `cov_dir`, a `seed` with
`random_order=False`) is refused at construction.

```python
import otto
from otto.bootstrap import bootstrap

from acme_instructions import DeviceTestOptions  # registered for "test"

bootstrap()  # or: async with otto.open_context(lab="mylab") as ctx: ...

result = otto.run_tests(
    ["TestDevice", "test_login"],
    run_options=otto.RunOptions(markers="not integration", cov=True),
    options=[DeviceTestOptions(firmware="2.1")],
)

if not result.passed:
    raise SystemExit(result.exit_code)

print(f"{len(result.junit_paths)} JUnit file(s) under {result.output_dir}")
for junit in result.junit_paths:
    print(junit)
```

- **Names** take the same forms as on the command line: a test
  (`test_login`), a class (`TestDevice`, every test in it), or a class path
  (`TestB::test_plain`); see {doc}`../cli/test/selection`. `run_tests` runs
  one pytest session per repo with a match, on the matched tests only, and
  folds the results into one `SuiteRunResult`. Tests run in collection order
  when `random_order=False`.
- **A name or a marker expression is required.** Called with neither names
  nor `run_options.markers`, `run_tests` raises `ValueError`.
- **`options=`** takes instances of options classes registered for the `test`
  verb ({doc}`authoring/options-classes`); tests read them with
  `ctx.options(Cls)`. A registered class you don't pass is built from its
  defaults, as the command line would, and a required field raises. An
  instance of a class that isn't registered for `test` raises
  {class}`~otto.params.OptionsRegistrationError`.
- **Registrations happen in `bootstrap()`.** It imports each repo's init
  modules, which is where options classes are registered, so call it (or
  `open_context()`, which calls it for you) before `run_tests`.
- **`bootstrap()` alone skips the CLI's checks.** A script without
  `open_context` gets no project selection (`-I` / `-E`), no bootstrap gate (a
  repo whose `init` module failed to import does not stop the run; whatever it
  would have registered is simply missing), no dependency preflight, and no
  reservation gate. Call {func}`~otto.session.select_projects`, {func}`~otto.session.check_repos` and
  {func}`~otto.session.check_dependencies` yourself if you want them (see
  [Bring-your-own-CLI](#bring-your-own-cli-lower-level-primitives) for their
  order), or run under `open_context()`.

### `output_dir` precedence

`run_tests` writes `junit.xml` (and, in stability mode,
`stability_report.txt`) under an output directory resolved in this order:

1. The `output_dir=` keyword argument, if given.
2. The active context's `output_dir` (`get_context().output_dir`), if a
   context is open.
3. The current working directory.

This is the same default the CLI's `--xdir` has (see
[Output directories](../cli/index.md#output-directories)) — pass
`output_dir=` explicitly, or open a context first, if a script shouldn't drop
artifacts next to whatever its caller's CWD happens to be.

### Context handling

otto's fixtures (the `ctx` fixture, and the options tests read through it)
use the active {class}`~otto.context.OttoContext`. When no context is
active (the plain `bootstrap()` → `run_tests()` script above), `run_tests`
installs a minimal lab-less context for the duration of the session and
restores the prior state afterwards. That minimal context carries no hosts,
so a test that calls `ctx.get_host(...)` under it fails loud with the normal
unknown-host error; tests that need lab hosts should run under
`open_context()` with the `asyncio.to_thread` pattern shown below. If a
context is already active, it is used as-is (its `output_dir` is only filled
in, temporarily, when it has none).

### Preflight

{func}`~otto.suite.run.prepare_run` prepares an explicit `cov_dir` and
`cov_report_dir` (created, or cleared under `overwrite_cov_dir` /
`overwrite_cov_report_dir`), proves they can be written, and refuses
`cov_tickets_json` without a `[coverage.tickets]` table. `run_tests` calls
it twice, both times before any host is touched: check-only right after
binding the verb options and checking there is something to run, so a bad
destination is refused before the instrumentation scan; then for real once
coverage is decided on, so a run refused for coverage (no `[coverage]`
table configured, say) never clears a destination. Leave `cov_dir` /
`cov_report_dir` unset (the default) to collect into `<output_dir>/cov` /
`<output_dir>/cov_report`, which are always fresh — the default
destinations need no check, since creating the run's own output directory
already was one. A script can call `prepare_run` itself before a long run.

A refusal is a {class}`~otto.coverage.config.DestinationError` (names the
field and the path, and — only when the destination is non-empty — the
`overwrite_*` field that would clear it) or an
{class}`~otto.params.OptionsValidationError` (a `cov_tickets_json` with no
`[coverage.tickets]` table); `otto test` prints the same message spelled as
flags. A script that calls `prepare_run` itself clears an `overwrite_*`
destination on the spot:

```python
from pathlib import Path

from otto.coverage import DestinationError
from otto.suite import RunOptions, prepare_run

cov_dir = Path("./coverage-run/cov")
opts = RunOptions(cov_dir=cov_dir, overwrite_cov_dir=True)
try:
    prepare_run(opts)
except DestinationError as e:
    raise SystemExit(f"{e.field}: {e}") from e
```

`prepare_run(opts, dry_run=True)` checks without creating or clearing
anything.

### Sync API, async callers

`run_tests` is synchronous, even though the tests it drives are
`async def`: it calls `asyncio.run()` internally (for the pre-run coverage
cleanup and post-run coverage collection), and `asyncio.run()` raises if a
loop is already running. Calling it directly from `async def main()` will
fail; hand it to a thread instead:

```python
import asyncio

result = await asyncio.to_thread(otto.run_tests, ["TestDevice"])
```

### Exceptions

- {func}`~otto.suite.run.run_tests` raises
  {class}`~otto.suite.run.NoTestsMatchedError` (a `ValueError`) when the
  selection matches nothing at all — no repos, or no repo with a matching
  test/marker.
- `run_tests` raises
  {class}`~otto.suite.selection.UnknownSelectionError` (also a `ValueError`,
  carrying did-you-mean suggestions) when a name is a genuine typo
  against a non-empty test universe. Catch it *before* `NoTestsMatchedError`
  if you handle both — both subclass `ValueError`, and the narrower one needs
  to win.
- Before any of the above, the {func}`~otto.suite.run.prepare_run` preflight
  (see [Preflight](#preflight)) can raise
  {class}`~otto.coverage.config.DestinationError` or
  {class}`~otto.params.OptionsValidationError` — both are `ValueError`
  subclasses too, so the same narrower-first ordering applies if you catch
  `ValueError` broadly.

```python
from otto.suite import NoTestsMatchedError, UnknownSelectionError, run_tests

try:
    result = run_tests(["test_login"])
except UnknownSelectionError as e:  # a typo: the message carries did-you-mean
    raise SystemExit(f"otto: {e}")
except NoTestsMatchedError:  # nothing to run at all
    raise SystemExit("otto: no tests matched")
```

## Collecting coverage from Python

Both `otto cov get` and the `otto test --cov` tail wrap one async library
function: `collect_coverage()` fetches `.gcda` counters from the lab's coverage
hosts (Unix hosts and containers over the network, embedded boards over the
console), walking each host's instrumented **products**; it writes the
`.otto_cov_meta.json` sidecar and produces a `capture.json` per host per
product — returning a `CollectResult`. A second async call, `run_coverage_report()`,
renders those captures into a multi-tier HTML report. `collect_coverage`,
`clean_coverage`, `CollectResult`, and the two named exceptions below
(`CoverageConfigError`, `NoCoverageDataError`) are exported at `otto.coverage`;
`run_coverage_report` lives at `otto.coverage.reporter`.

`tier=` accepts either a tier name (`str`) or an already-resolved
{class}`~otto.coverage.tiers.TierConfig` object — pass the object when you've
already called {func}`~otto.coverage.tiers.resolve_get_tier` yourself (as
`otto cov get` does, to validate the manual-tier `--ticket` requirement
before fetching) so `collect_coverage` does not re-resolve it a second time.

```python
import asyncio
from pathlib import Path

import otto
from otto.bootstrap import get_repos
from otto.coverage import collect_coverage, resolve_report_inputs, run_coverage_report


async def main():
    async with otto.open_context(lab="mylab") as ctx:
        cov_dir = Path("./coverage-run/cov")

        # Fetch .gcda from every [coverage] host's instrumented products, write
        # the metadata sidecar, and produce one capture.json per (host, product)
        # against the resolved tier.
        result = await collect_coverage(cov_dir, tier="manual", ticket="PROJ-123")
        print(f"{len(result.captures_written)} capture(s) under {result.cov_dir}")
        for (host_id, product), product_dir in result.product_dirs.items():
            print(host_id, product, product_dir)

        # Resolve the same [coverage]-derived report inputs `otto cov report`
        # does, then render an HTML report from the collected cov/ directory.
        # Pass overwrite=True to re-render into a directory that already
        # holds a report (it is cleared first); without it, a non-empty
        # report_dir raises DestinationError.
        inputs = resolve_report_inputs(get_repos())
        store = await run_coverage_report(
            [cov_dir], Path("./coverage-run/report"), inputs, overwrite=True
        )
        if store is not None:
            print(f"{store.overall_pct():.1f}% overall ({store.file_count()} files)")


asyncio.run(main())
```

### `CollectResult`

`collect_coverage` returns a `CollectResult` with three fields:

| Field              | Type              | Description                                                                                              |
|--------------------|-------------------|---------------------------------------------------------------------------------------------------------|
| `cov_dir`          | `Path`            | The directory the coverage landed in (the argument you passed).                                         |
| `product_dirs`     | `dict[tuple[str, str], Path]` | Each contributing `(host id, product)` pair → its staging directory.                         |
| `captures_written` | `list[Path]`      | The `capture.json` files produced, one per `(host, product)` pair (empty when no `[coverage]` repo resolved a git root). |

### Fails loud — exceptions to handle

Unlike the CLI, `collect_coverage` never swallows. Wrap it if a collection
failure should not abort your script:

- `otto.coverage.errors.CoverageConfigError` (a `ValueError`) — no `[coverage]`
  section is configured for any of the resolved repos.
- `otto.coverage.errors.NoCoverageDataError` (a `ValueError`) — no `.gcda` was
  retrieved from any matched product. The message names every
  `host:product:cov_dir` triple it searched, or says that no host carried an
  instrumented product at all.
- `ValueError` — the requested tier name is ambiguous or unknown (only
  reachable when `tier=` is a name or `None`; a `TierConfig` object passed
  directly skips resolution).
- `otto.coverage.capture.gitio.GitUnavailableError` — the SUT checkout is not a
  git repository, so captures cannot be anchored to `base_commit`.
- `otto.coverage.errors.CoverageDataMismatchError` — the fetched `.gcda` no
  longer matches the current build's `.gcno` notes (the product was rebuilt
  after collection).
- `otto.coverage.errors.CoverageToolVersionError` — the `gcov` tool cannot read
  this build's coverage format (e.g. a clang build captured with GNU `gcov`).
- `RuntimeError` — an lcov/merge failure.

`CoverageConfigError` and `NoCoverageDataError` both subclass `ValueError`, so
an existing `except ValueError` handler keeps working unmodified; catch them
by name first if you want to distinguish the two fail-loud sites.
`GitUnavailableError`, `CoverageDataMismatchError`, and
`CoverageToolVersionError` all subclass `RuntimeError`, so catch them *before* a
bare `except RuntimeError` if you want to distinguish them. The `otto test
--cov` tail logs these and leaves the test run's verdict alone, whereas `otto
cov get` surfaces each as a clean, single-line error. Every exception otto
*defines* also subclasses `otto.errors.OttoError`, so a single `except
OttoError` clause catches all of them when you don't need to distinguish —
except `SyncPhaseInterrupt`, which is a plain `KeyboardInterrupt`.

`except OttoError` catches otto's *named* failures, not every exception otto
raises: a rejected argument is usually a plain `ValueError`, and `run_command`
and `run_tests` can raise `SystemExit`, which `except Exception` does not
catch either. See {mod}`otto.errors` for which clause
reaches which exceptions.

### `clean_after_fetch`

By default `collect_coverage` zeroes every contributing host's counters —
Unix and container `.gcda`, an embedded board's `reset_fn` alike —
immediately after a successful fetch, as `otto test --cov` does. Pass
`clean_after_fetch=False` to skip that internal clean when you want to own the
post-fetch reset yourself, as `otto cov get` does: its `--clean` flag zeroes
only the hosts that actually fetched a product this call. To zero the
counters *before* a run instead, call `clean_coverage()`.

See {doc}`../cli/cov/index` for the full CLI workflow, tier configuration, and
the report format.

## Coverage: get, clean, report

The coverage verbs are library functions first; `otto cov ...` calls them.

```python
from pathlib import Path

import otto.coverage

report = await otto.coverage.get_coverage(
    output_dir=Path("coverage-out"), tier="manual", ticket="JIRA-123", clean=True
)
print(report.captures, report.manual_captures)
if not report.ok:
    for failed in report.clean.failed:
        print(failed.host, failed.product, failed.reason)

cleared = await otto.coverage.clean_coverage()
assert cleared.ok, cleared.failed
```

Every input rule is the library's: an unknown tier, a manual-kind tier without a
ticket, or no output directory raises
{class}`~otto.coverage.errors.CoverageInputError` with a `field`. `output_dir`
may be left out only inside an otto invocation, which supplies its own output
directory; a standalone script passes one, as above. A failed
counter reset is never a warning: `clean_coverage` reports it, and `otto test`
raises {class}`~otto.coverage.errors.CoverageCleanError`.

## Docker: build, deploy, tear down

The docker verbs are library functions first; `otto docker ...` calls them.

```python
import otto.docker

report = await otto.docker.compose_build("integration", provide={"db": "real"})
for failed in report.failed:
    print(failed.repo, failed.image, failed.result.value)

stack = await otto.docker.deploy("integration", build=False)
await stack.hosts["api"].run("./run-tests")

torn = await otto.docker.teardown("integration")
if not torn.ok:
    for host, results in torn.failed.items():
        print(host, [r.command for r in results])
```

`build_on(parent="test3", images=["api"])` builds one parent's images without a
use-case, the way `otto docker build --parent test3 api` does; leave `parent`
out and the library names it ({ref}`Which host <docker-which-host>`).
Every input rule is the library's: a missing or tied parent, an unknown repo or
image name raises {class}`~otto.docker.observe.DockerVerbError` with a `field`.

The observe verbs are the same shape: {func}`~otto.docker.observe.list_containers`,
{func}`~otto.docker.observe.compose_logs` and the rest return an
{class}`~otto.docker.observe.ObserveReport` holding docker's text per host, whole.

As a fixture, {func}`~otto.docker.deployed` deploys on entry and tears down
on exit; a failed teardown raises `HostCommandError` naming the host, so a
stack cannot be left standing silently.
