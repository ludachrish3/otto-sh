# Using otto as a library

otto is not limited to the `otto` CLI. You can use it directly in your own
async Python scripts — for example, one-off automation, CI tooling, or
integration scripts that operate on lab hosts without needing test suites or
instructions.

## Imports are side-effect-free; `open_context()` runs the composition root

`import otto` and `import otto.config` do no I/O and run no project code.
`import otto` implements PEP 562 lazy exports (each public name resolves its
source module only on first attribute access), so a bare import stays cheap even
in a process that never touches a lab. `import otto.config` is
side-effect-free (no repo discovery, no user code) but eagerly imports its
submodules. Nothing under `.otto/settings.toml` `init` is imported just because
`otto` is on `sys.path` — that happens in {func}`otto.bootstrap.bootstrap`.

The composition root — repo discovery plus importing every configured `init`
module and test file — is {func}`otto.bootstrap.bootstrap`, and it is
idempotent (repeated calls return the same cached result). `open_context()`
calls it for you before loading the lab, so any `@instruction`, `Test*`-named
`OttoSuite` subclass, `@cli_command()`, or `register_*_backend()` call in your
project's `init` modules has already run by the time the `async with` block
starts:

```python
async with otto.open_context(lab="mylab") as ctx:
    ...  # your project's registered components are all live here
```

If you're wiring up a custom embedding that bypasses `open_context()` — for
example, driving `OttoContext`/`set_context()` manually as shown below — call
`otto.bootstrap.bootstrap()` yourself first if you need those registrations
available. Skipping it isn't an error; it just means your script only sees
otto's own built-ins, not anything your project registers in `init`.

## Recommended: `open_context()`

`open_context()` is the single entry point for library use. It loads a lab,
installs the active context, enters the host lifecycle scope, yields the
context, and tears everything down on exit — even if your code raises.

```python
import asyncio
import otto

async def main():
    async with otto.open_context(lab="mylab", search_paths=[...]) as ctx:
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

| Parameter            | Type                        | Default | Description                               |
|----------------------|-----------------------------|---------|-------------------------------------------|
| `lab`                | `Lab \| str \| list[str]`   | —       | A `Lab` object, or lab name(s) to load    |
| `dry_run`            | `bool`                      | `False` | Log commands without executing them       |
| `log_command_output` | `bool`                      | `True`  | Stream command output to the otto logger  |
| `search_paths`       | `list[Path] \| None`        | `None`  | Paths to search for lab definitions       |

## Bring-your-own-CLI: lower-level primitives

otto's own CLI uses these three steps internally — `open_context` is just them
packaged across the callback/subcommand boundary:

1. Build an `OttoContext` with the chosen lab and runtime flags.
2. Install it as the active context with `set_context()`, which returns a reset
   token.
3. Enter `ctx.scope` as an async context manager; on exit it closes any
   still-connected hosts, then `reset_context(token)` restores the prior state.

```python
from otto.context import OttoContext, reset_context, set_context
from otto.config import load_lab

lab = load_lab("mylab", search_paths=[...])
ctx = OttoContext(lab=lab, dry_run=False)
token = set_context(ctx)
try:
    async with ctx.scope:
        # your work here
        ...
finally:
    reset_context(token)
```

This is exactly what `open_context` does under the hood. Use this form when
you need fine-grained control — for instance, when a framework drives the
event loop and you cannot use `async with` at the top level.

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
    await configure(host)      # pass it wherever you like
# scope.close() sweeps host when the block exits
```

**(c) Explicit `await host.close()`:**

```python
async with otto.open_context(lab="mylab") as ctx:
    host = ctx.get_host("router1")
    await host.run("reboot")
    await host.close()         # early close — idempotent; scope sweep is a no-op
```

`close()` is idempotent: calling it multiple times is safe.

## FD-model caveat

A host you construct **directly** (e.g. `UnixHost(...)`) outside any context
has no scope backstop — it is yours to close, exactly like an explicitly-opened
file descriptor. Use `async with`, `await h.close()`, or register it manually
with `ctx.scope.register(h)` inside an active context.

Reservation checks are a CLI concern — `open_context` does not gate on them.
If your script needs to verify reservations before running, call
`otto.reservations.check_reservations(...)` explicitly before entering the
block. For the full build-a-backend → resolve-identity → gate → present
walkthrough (including a complete, runnable example CLI to copy), see
{doc}`Using the reservation library in your own CLI <reservations>`.

## In-memory labs (no lab file)

You do not need a `lab.json` on disk. Build a `Lab` from host dicts, install
it as the active context, and the zero-argument selectors (`all_hosts`,
`get_host`) operate on it directly — useful for tests and ad-hoc scripts.
Selection touches no network, so this runs as-is:

```{doctest}
>>> import re
>>> from otto.host.factory import create_host_from_dict
>>> from otto.config.lab import Lab
>>> from otto.context import OttoContext, set_context, reset_context
>>> from otto.config import all_hosts, get_host
>>> hosts = [create_host_from_dict(spec) for spec in [
...     {"ip": "10.0.0.11", "element": "carrot", "creds": [{"login": "admin", "password": "x"}], "labs": ["veg"]},
...     {"ip": "10.0.0.12", "element": "tomato", "creds": [{"login": "admin", "password": "x"}], "labs": ["veg"]},
... ]]
>>> lab = Lab(name="veg", hosts={h.id: h for h in hosts})
>>> token = set_context(OttoContext(lab=lab))
>>> [h.element for h in all_hosts(re.compile("tomato"))]
['tomato']
>>> get_host("carrot").element
'carrot'
>>> reset_context(token)
```

The trailing `reset_context` restores the prior active context — always pair it
with `set_context` (or use `otto.open_context`, which does both for you).

## Running suites from Python

`otto test` is a thin CLI wrapper: {func}`~otto.suite.run.run_suite` runs one
`OttoSuite` subclass through `pytest.main()` and returns a
{class}`~otto.suite.run.SuiteRunResult` instead of exiting the process.
`run_suite` and `RunOptions` are exported at the top level (`otto.run_suite`,
`otto.RunOptions`); the rest of the suite-run API —
{func}`~otto.suite.run.run_selection`, {func}`~otto.suite.run.find_suite`, and the
exceptions below — stays one level down, at `otto.suite` / `otto.suite.run` /
`otto.suite.selection`.

A suite class only exists once your project's `init` modules have imported the
`test_*.py` file that defines it — the same composition root described above.
`open_context()` runs it for you; a script that skips `open_context()` should
call `bootstrap()` itself first. This matters most for
{func}`~otto.suite.run.find_suite`: it looks a class up in the suite registry
directly and does **not** trigger discovery itself, so calling it before
`bootstrap()`/`open_context()` has run raises `LookupError` even for a suite
that would otherwise be found.

```python
import otto
from otto.bootstrap import bootstrap
from otto.suite import find_suite

bootstrap()  # or: async with otto.open_context(lab="mylab") as ctx: ...

# Dynamic lookup by class name -- e.g. the suite came from a config file or
# CLI argument. Raises LookupError (listing every registered suite) on a
# typo. Skip this and pass the class directly if you imported it normally.
suite_cls = find_suite("TestDevice")

run_options = otto.RunOptions(markers="not integration", cov=True)
options = suite_cls.Options(firmware="2.1")

result = otto.run_suite(suite_cls, options=options, run_options=run_options)

if not result.passed:
    raise SystemExit(result.exit_code)

print(f"{len(result.junit_paths)} JUnit file(s) under {result.output_dir}")
for junit in result.junit_paths:
    print(junit)
```

### `output_dir` precedence

`run_suite`/`run_selection` write `junit.xml` (and, in stability mode,
`stability_report.txt`) under an output directory resolved in this order:

1. The `output_dir=` keyword argument, if given.
2. The active context's `output_dir` (`get_context().output_dir`), if a
   context is open.
3. The current working directory.

Same `--xdir`-defaults-to-CWD philosophy the CLI uses (see
[Output directories](cli-reference.md#output-directories)) — pass
`output_dir=` explicitly, or open a context first, if a script shouldn't drop
artifacts next to whatever its caller's CWD happens to be.

### Context handling

Suite internals (the per-test artifact directories, the `ctx` fixture) read
the active {class}`~otto.context.OttoContext`. When no context is active —
the plain `bootstrap()` → `run_suite()` script above — `run_suite` and
`run_selection` install a minimal lab-less context for the duration of the
session and restore the prior state afterwards. That minimal context carries
no hosts, so a suite that calls `ctx.get_host(...)` under it fails loud with
the normal unknown-host error; suites that need lab hosts should run under
`open_context()` with the `asyncio.to_thread` pattern shown below. If a
context is already active, it is used as-is (its `output_dir` is only filled
in, temporarily, when it has none).

### Suite-less selections

{func}`~otto.suite.run.run_selection` mirrors `otto test --tests`/`-m`
without a suite name: set `tests=`/`markers=` on `RunOptions` and it runs one
pytest session per repo with a match, folding the results into a single
`SuiteRunResult`.

```python
from otto.suite.run import run_selection

result = run_selection(
    run_options=otto.RunOptions(tests="test_login,TestB::test_plain"),
)
```

`run_selection` requires at least one of `tests=`/`markers=` on `RunOptions`;
called with both empty (a bare `RunOptions()`) it raises `ValueError` rather
than silently matching every test in every repo — mirroring the `otto test`
callback, which only takes the suite-less path once `--tests`/`-m` is given.

### `cov_dir` overwrite guard

When `RunOptions.cov` is set together with an explicit `cov_dir`, both
`run_suite` and `run_selection` validate it up front, the same way the CLI's
`--cov-dir`/`--overwrite-cov-dir` pair does: a non-empty target raises
`ValueError` naming the flag (via `otto.coverage.config.prepare_empty_dir`)
unless `overwrite_cov_dir=True` is also set, in which case its contents are
cleared before the run starts.
This runs before the pre-run remote `.gcda` clean, so a bad `cov_dir` fails
before any host is touched. Leave `cov_dir` unset (the default) to collect
into `<output_dir>/cov`, which is always fresh.

### Sync API, async callers

`run_suite`/`run_selection` are synchronous, even though the suites and
instructions they drive are `async def` — both call `asyncio.run()`
internally (for the pre-run coverage cleanup and post-run coverage
collection), and `asyncio.run()` raises if a loop is already running. Calling
either directly from `async def main()` will fail; hand it to a thread
instead:

```python
import asyncio

result = await asyncio.to_thread(otto.run_suite, suite_cls, run_options=run_options)
```

### Exceptions

- {func}`~otto.suite.run.find_suite` raises `LookupError` for an unregistered
  class name; the message lists every currently-registered suite.
- {func}`~otto.suite.run.run_selection` raises
  {class}`~otto.suite.run.NoTestsMatchedError` (a `ValueError`) when the
  selection matches nothing at all — no repos, or no repo with a matching
  test/marker.
- `run_selection` raises
  {class}`~otto.suite.selection.UnknownSelectionError` (also a `ValueError`,
  carrying did-you-mean suggestions) when a `tests=` name is a genuine typo
  against a non-empty test universe. Catch it *before* `NoTestsMatchedError`
  if you handle both — both subclass `ValueError`, and the narrower one needs
  to win.

## Collecting coverage from Python

Both `otto cov get` and the `otto test --cov` tail wrap one async library
function: `collect_coverage()` fetches `.gcda` counters from the lab's coverage
hosts (Unix hosts over the network, embedded boards over the console), writes
the `.otto_cov_meta.json` sidecar, and produces a `capture.json` per board —
returning a `CollectResult`. A second async call, `run_coverage_report()`,
renders those captures into a multi-tier HTML report. `collect_coverage`,
`clean_remote_gcda`, `CollectResult`, and the two named exceptions below
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
from otto.coverage import collect_coverage
from otto.coverage.reporter import run_coverage_report


async def main():
    async with otto.open_context(lab="mylab") as ctx:
        cov_dir = Path("./coverage-run/cov")

        # Fetch .gcda from every [coverage] host, write the metadata sidecar,
        # and produce one capture.json per board against the resolved tier.
        result = await collect_coverage(cov_dir, tier="manual", ticket="PROJ-123")
        print(f"{len(result.captures_written)} capture(s) under {result.cov_dir}")
        for host_id, host_dir in result.host_dirs.items():
            print(host_id, host_dir)

        # Render an HTML report from the collected cov/ directory.
        store = await run_coverage_report([cov_dir], Path("./coverage-run/report"))
        if store is not None:
            print(f"{store.overall_pct():.1f}% overall ({store.file_count()} files)")


asyncio.run(main())
```

### `CollectResult`

`collect_coverage` returns a `CollectResult` with three fields:

| Field              | Type              | Description                                                                                              |
|--------------------|-------------------|---------------------------------------------------------------------------------------------------------|
| `cov_dir`          | `Path`            | The directory the coverage landed in (the argument you passed).                                         |
| `host_dirs`        | `dict[str, Path]` | Each contributing host id → its per-host `.gcda` directory.                                              |
| `captures_written` | `list[Path]`      | The `capture.json` files produced, one per board (empty when no `[coverage]` repo resolved a git root). |

### Fails loud — exceptions to handle

Unlike the CLI, `collect_coverage` never swallows. Wrap it if a collection
failure should not abort your script:

- `otto.coverage.errors.CoverageConfigError` (a `ValueError`) — no `[coverage]`
  section is configured for any of the resolved repos.
- `otto.coverage.errors.NoCoverageDataError` (a `ValueError`) — no `.gcda` was
  retrieved from any matched host (the message names the hosts it searched).
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
bare `except RuntimeError` if you want to distinguish them. Swallowing-and-logging
these is exactly what the `otto test --cov` tail does — a coverage-collection
failure must never turn an otherwise-green test run red — whereas `otto cov get`
surfaces each as a clean, single-line error.

### `clean_after_fetch`

By default `collect_coverage` zeroes the Unix hosts' remote `.gcda` counters
immediately after a successful fetch — the `otto test --cov` behavior that keeps
the next run from mixing in stale data. Pass `clean_after_fetch=False` to skip
that internal clean when you want to own the post-fetch reset yourself. That is
what `otto cov get` does, so its `--clean` flag can be scoped to just the Unix
host ids that actually fetched — never zeroing an embedded board on a mixed lab.
To zero the counters *before* a run instead, call `clean_remote_gcda()`.

See {doc}`coverage` for the full CLI workflow, tier configuration, and the
report format.
