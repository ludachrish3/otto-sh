# Architecture overview

otto is an asyncio test orchestrator: a CLI and a Python library that drive
*labs* of remote hosts — Unix machines over SSH/Telnet, embedded targets over
serial consoles, Docker containers — to deploy products, run commands and test
suites, and collect metrics and coverage. One process, one event loop; hosts
are fanned out with asyncio, never threads.

## Layer map

The package splits into four layers. Dependencies point downward only —
a lower layer never imports from a higher one.

**Foundation** — small, dependency-light modules everything else builds on:

| Module | Responsibility |
| --- | --- |
| {mod}`otto.registry` | The generic named-registry engine behind every extension seam |
| {mod}`otto.result` | The {class}`~otto.result.Result` family: statuses, payloads, exit codes |
| {mod}`otto.utils` | `Status`, small shared helpers, CLI signature overlays |
| `otto.logger` | The `'otto'` logger tree, log levels, {class}`~otto.logger.mode.LogMode` |
| `otto.filesystem` | Network-filesystem detection (drives WAL-vs-DELETE and rotation choices) |
| `otto.console`, `otto.params`, `otto.version` | Rich console singleton, options→CLI parameter expansion, version resolution |

**Boundary** — where external data becomes trusted runtime objects:

| Module | Responsibility |
| --- | --- |
| `otto.models` | Pydantic specs for hosts.json, settings.toml, env vars, monitor records |
| `otto.storage` | The lab-repository (host source) protocol and the default JSON backend |
| `otto.configmodule` | Repo/lab discovery, settings parsing, fleet accessors |

**Domain** — the subsystems that do the actual work:

| Package | Responsibility |
| --- | --- |
| `otto.host` | Host classes, sessions, connections, transfers, privilege, power |
| `otto.suite` | Test-suite base class, registration, the pytest plugin, `expect()` |
| `otto.monitor` | Metric collection, parsers, SNMP, the live dashboard |
| `otto.coverage` | The embedded gcov pipeline: fetch, correlate, render, report |
| {mod}`otto.docker` | Image builds and compose lifecycles on parent hosts |
| {mod}`otto.reservations` | The reservation gate and its pluggable backends |

**Application** — composition and the CLI edge:

| Module | Responsibility |
| --- | --- |
| {mod}`otto.bootstrap` | Two-phase composition root: discovery, then contained user-code registration |
| {mod}`otto.context` | {class}`~otto.context.OttoContext`: the per-invocation runtime and host lifecycle scope |
| `otto.cli` | The Typer app, the command registry, lazy dispatch, shell completion |

Two support packages sit alongside: `otto.testing` (conformance helpers
for third-party backends) and `otto.examples` (small, copyable reference
implementations, conformance-verified in otto's own suite).

## The life of an invocation

What happens on `otto -l my_lab run deploy --debug`:

1. **Entry.** The console script calls `entry()` in `otto/cli/main.py`.
   Shell-completion invocations take a cache fast path that runs *zero user
   code*; every other invocation runs {func}`otto.bootstrap.bootstrap` before
   argv parsing so third-party commands exist when the root group is built.
2. **Bootstrap.** Phase 1 (*discovery*) parses `OTTO_*` environment variables
   and each repo's `.otto/settings.toml` — no user code runs. Phase 2
   (*registration*) imports each repo's init modules and test files; every
   user-module exec is wrapped so one broken file becomes a framed
   `BootstrapError` warning instead of bricking the process.
3. **Dispatch.** The root Typer group is registry-backed: `--help` renders
   every registered command from lightweight stubs without importing a single
   subcommand module. Only the command actually being dispatched (`run`) has
   its module imported and resolved.
4. **Preamble.** The invoke preamble runs for the leaf command: the lab is
   loaded *now* (lab loading is deliberately not part of bootstrap), an
   {class}`~otto.context.OttoContext` is built and installed in a
   context-variable, the per-command output directory and log sinks are wired,
   and the reservation gate runs (unless the command opted out or
   `--skip-reservation-check` was passed).
5. **Execution.** The `deploy` instruction coroutine runs. Hosts it obtains
   via the context are registered with the context's
   {class}`~otto.context.HostScope`.
6. **Teardown.** When the command returns, the scope closes every host that
   is still connected — deterministically, with no reliance on garbage
   collection. The process exit code is derived from the returned
   {class}`~otto.result.Result` when there is one.

`otto test TestDevice` follows the same shape through step 4, then hands off
to the test pipeline: the registered suite's synthesized subcommand builds the
suite's options object and invokes pytest with otto's plugin — see
{doc}`test-pipeline`.

## Where the boundaries are enforced

- **The CLI edge is lazy.** Importing `otto.cli` must never parse repo
  settings — a malformed `settings.toml` cannot brick `otto --help`. A bare
  `import otto` resolves its public names lazily (PEP 562), so library users
  pay only for what they touch.
- **The data edge is validated.** Everything that enters from JSON, TOML, or
  the environment passes through a pydantic spec in `otto.models` exactly
  once; see {doc}`data-boundary`.
- **User code is contained.** Bootstrap frames per-file failures;
  registries reject duplicate names loudly and attribute every entry to the
  module that registered it; see {doc}`registries`.

The remaining pages cover each subsystem in depth, and {doc}`principles`
collects the recurring design rules the codebase holds itself to.
