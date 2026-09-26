# Registries and the pluggable CLI

Every place otto can be extended — new transfer protocols, host classes,
reservation backends, CLI commands — stores its entries in the same engine:
{class}`otto.registry.Registry`. One storage idiom buys uniform behavior
everywhere:

- **Loud duplicates.** Registering a taken name raises, and the error names
  the module that owns the existing entry (every registration records its
  origin via the call stack). Accidental double-registration cannot silently
  shadow a backend.
- **Did-you-mean lookups.** An unknown name fails with the closest registered
  match, the full list of known names, and the exact `register_*` call that
  would add a custom one.
- **Attribution and introspection.** `names()`, `items()`, and `origin(name)`
  make `--list-*` flags and debugging cheap.

Domain modules keep their own public `register_*` / `build_*` wrapper
functions; the class is the shared engine behind them.

## The registry inventory

| Registry | Kind | Register via | Built-ins |
| --- | --- | --- | --- |
| `CLI_COMMANDS` | top-level CLI command | {func}`otto.cli.registry.register_cli_command` / {func}`~otto.cli.registry.cli_command` | the nine first-party commands — see {doc}`../overview` |
| `INSTRUCTIONS` | `otto run` subcommand | {func}`~otto.cli.run.instruction` | — |
| `SUITES` | `otto test` subcommand | {func}`~otto.suite.register.register_suite_class` (auto-called by {class}`~otto.suite.suite.OttoSuite`'s `__init_subclass__`) | — |
| `HOST_CLASSES` | host class | `otto.host.os_profile.register_host_class` | `unix`, `embedded` |
| `OS_PROFILES` | `os_type` profile | `otto.host.os_profile.register_os_profile` | `unix`, `embedded`, `zephyr` |
| `TERM_BACKENDS` | term (connection) backend | `otto.host.connections.register_term_backend` | `ssh`, `telnet` |
| `TRANSFER_BACKENDS` | transfer backend | `otto.host.transfer.register_transfer_backend` | `sftp`, `scp`, `ftp`, `nc`, `console`, `tftp` |
| `FRAME_CLASSES` | command frame | `otto.host.command_frame.register_command_frame` | `bash`, `ash`, `zephyr`, `zephyr-serial`, `raw` |
| `LOADER_CLASSES` | binary loader | `otto.host.binary_loader.register_binary_loader` | `llext-hex` |
| `FILESYSTEM_CLASSES` | embedded filesystem type | `otto.host.embedded_filesystem.register_filesystem` | FAT-on-RAM, LittleFS, none |
| `POWER_CONTROLLERS` | power controller | `otto.host.power.register_power_controller` | — |
| `SESSION_SETUPS` | session setup hook | `otto.host.session_setup.register_session_setup` | — |
| `LAB_REPOSITORIES` | lab repository (host source) | {func}`otto.labs.register_lab_repository` | `json` |
| `RESERVATION_BACKENDS` | reservation backend | `otto.reservations.registry.register_reservation_backend` | `json`, `none` |
| `CREDS_BACKENDS` | creds store | `otto.creds.register_creds_backend` | `json` |
| `HOST_PARSERS` | monitor parser set | `otto.monitor.parsers.register_host_parsers` | default `/proc` parsers |
| `SNMP_METRICS` | SNMP metric descriptor | `otto.monitor.snmp.register_snmp_metric` | standard OIDs |

(Product providers are the one seam that is a list, not a named registry —
every registered provider runs for every host; see {doc}`hosts`.)

## Registration symmetry

Built-in backends register through the **same public functions** third-party
code uses — `sftp` goes through `register_transfer_backend` exactly like a
custom protocol would. There is no privileged private path, which keeps the
public seams honest: if a registration API is awkward for otto's own
built-ins, it is awkward for everyone, and it gets fixed rather than bypassed.
Downstream repos register from their init modules (the `init` list in
`.otto/settings.toml`), which bootstrap imports in phase 2
({doc}`../lifecycle`).

## Lazy loaders, and what test files may register

A registry may name a *loader*: a `"module:function"` string it resolves and
calls at the start of every read (`get`, `names`, `items`, `origin`,
`unregister`, `in` and `len`). The function decides whether there is anything
left to load, so every read after the first costs almost nothing. A read made
from inside the loader does not call it again, and
{func}`~otto.registry.suspend_loaders` reads without calling it; the test
harness's registry snapshots use it so that saving a registry never loads a
repo's test files as a side effect.

`SUITES` is the only registry with a loader. Its loader,
{func}`otto.bootstrap.load_test_suites`, imports each repo's top-level test
files on the first read after bootstrap, so only the commands that read suites
pay for those imports or fail on them ({doc}`../lifecycle`).

A test file may therefore register suites and nothing else; why is on
{doc}`../lifecycle`. The mechanism: while test files load
({func}`~otto.registry.loading_test_files`), every registry except `SUITES`
refuses an entry whose origin is outside the `otto` package, with
{class}`~otto.registry.RegistrationRefused`. Bootstrap frames the refusal like
any other load failure, naming the file and saying to register the entry from
an init module instead.

- **The check is on origin, not only on the phase.** A test file is often the
  first thing to import an otto module that registers its own entries when
  imported; `otto.host.llext_kind`, for example, registers a product kind.
  That registration belongs to otto and succeeds. The flip side is that every
  public `register_*` wrapper must record the module that *called* it as the
  origin: an entry attributed to the wrapper's own `otto.*` module would pass
  the check.
- **The two provider lists that are not registries** (product and dev-tool
  providers) apply the same check, keyed on the provider's module.
- **A guard test** (`tests/unit/test_registry_loading.py`) finds every
  `Registry` otto constructs and asserts that each one except `SUITES`
  refuses, so a registry added later is covered without anyone remembering to
  list it.

## The CLI command registry

The top-level CLI is itself registry-backed. A
{class}`~otto.cli.registry.CommandSpec` describes one command:

- `name` — what the user types (`run`, `flash`, …).
- `loader` — a `typer.Typer` app, a plain or async function, or a *lazy
  string* `"pkg.mod:attr"` that is imported only on dispatch.
- `help` — the one-liner for `otto --help`, rendered without importing the
  command's module.
- `lab_free` — the command never needs a lab (e.g. `schema`), so the preamble
  skips lab loading entirely.
- `output_dir` / `gate` — whether invocations create a per-command output
  directory and run the reservation gate.

First-party commands in `otto/cli/builtin_commands.py` and third-party
commands both go through {func}`~otto.cli.registry.register_cli_command` (or
the {func}`~otto.cli.registry.cli_command` decorator) — the symmetry rule
again. See {doc}`../../cookbook/extending/extending-cli` for the how-to.

The backend registries' `register_*()` functions take `overwrite=True` to
replace an existing entry; `register_cli_command()` has no such escape hatch. A
top-level command name is part of the CLI's surface: letting a second
registration replace it would make `otto --help` and tab completion depend on
init-module import order, so a duplicate always fails loud and a project that
wants different behavior picks a different name.

### Lazy dispatch

The root group resolves commands in two tiers:

- **Enumeration** (`otto --help`, completion listings) uses lightweight stubs
  built from each spec's stored help line. No subcommand module is imported —
  `otto --help` imports *zero* subcommand modules.
- **Dispatch** resolves the real command — importing the loader's module,
  flattening single-command Typer apps, and wrapping leaf callbacks with the
  invoke preamble — only for the one token actually being executed or
  completed.

### The completion fast path

Shell completion must be low-latency and must never traceback into the shell.
Completion and the root help screen are answered from the completion cache,
so on a hit no user code runs and a cached third-party command still appears
in listings although its registration never ran; what the cache holds, when
it is trusted and who rebuilds it is on {doc}`completion-cache`.

Completion must never print a warning either: text written into a completing
shell corrupts the candidate list the shell is parsing, so a lab entry it
cannot build is skipped silently, and `otto cache info` is where those skips
are explained.

The payoff is registry-shaped completion everywhere — captured live from a
scaffolded demo repo at docs build time:

```{raw} html
:file: ../../_static/generated/termynal/complete-host-ids.html
```

More showcases live elsewhere: suite names and `--tests`
({doc}`../../cli/test/index`), instruction names ({doc}`../../cli/run/index`),
per-class host verbs ({doc}`../../cli/host/index`) plus registry-backed
option values ({doc}`../../cli/host/connections`), and `--lab`
({doc}`../lifecycle`).

The consistent rule behind all of them: a keystroke answered from the cache
**never runs user code**, and one that finds the cache missing or stale runs
it once, to rebuild the cache. Registry names come from the completion cache
({doc}`completion-cache`); host ids and lab names are read from `lab.json` data;
`--tests` names come from a static `ast` scan of the test sources. The one
case that genuinely needs a live pytest collection — dynamically generated
tests — is handled without breaking that rule: the collection runs in a
disposable, timeout-bounded *subprocess* (warmed for free by any real `otto
test --list-tests`, or by a one-time slow first TAB), and its result is cached
under a reserved key so later completions are a plain read. The static scan
stays as the always-available floor, so `--tests` completion is never empty.

## Where the code lives

- {mod}`otto.registry` — the `Registry` engine underneath every entry in the
  inventory: loud duplicates, did-you-mean lookups, attribution
- {mod}`otto.cli.registry` — `CommandSpec`, the CLI command registry, and
  lazy dispatch
- `otto.instructions` — the `INSTRUCTIONS` registry itself, deliberately
  CLI-free (its `typer.Typer` field is a `TYPE_CHECKING`-only annotation) so
  core readers — `Repo`'s instruction panel, the completion cache — see the
  registered set without importing `otto.cli`
- {mod}`otto.cli.run` / {mod}`otto.suite.register` — the `@instruction()`
  decorator and the `SUITES` registration
  (`OttoSuite.__init_subclass__`)
- `otto.config.completion_cache` — the completion cache
  ({doc}`completion-cache`)
- the host-side registries live beside the strategy they select:
  {mod}`otto.host.os_profile`, {mod}`otto.host.connections`,
  {mod}`otto.host.transfer`, {mod}`otto.host.command_frame`,
  {mod}`otto.host.binary_loader`, {mod}`otto.host.embedded_filesystem`,
  {mod}`otto.host.power`
- `otto.labs`, {mod}`otto.reservations.registry`,
  {mod}`otto.monitor.parsers`, {mod}`otto.monitor.snmp` — the remaining
  registries in the inventory table
