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
| `FRAME_CLASSES` | command frame | `otto.host.command_frame.register_command_frame` | `bash`, `zephyr` |
| `LOADER_CLASSES` | binary loader | `otto.host.binary_loader.register_binary_loader` | `llext-hex` |
| `FILESYSTEM_CLASSES` | embedded filesystem type | `otto.host.embedded_filesystem.register_filesystem` | FAT-on-RAM, LittleFS, none |
| `POWER_CONTROLLERS` | power controller | `otto.host.power.register_power_controller` | — |
| `LAB_REPOSITORIES` | lab repository (host source) | {func}`otto.labs.register_lab_repository` | `json` |
| `RESERVATION_BACKENDS` | reservation backend | `otto.reservations.registry.register_reservation_backend` | `json`, `none` |
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
again. See {doc}`../../guide/extending-cli` for the how-to.

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
Completion invocations first try a cache
(`otto/config/completion_cache.py`) of command, suite, instruction, and
host names snapshotted on previous runs. On a cache hit, completion runs
*zero user code* — no bootstrap, no init modules. Cached third-party command
names still appear in listings even though their registrations never ran;
only actually dispatching one triggers the real import. Any discovery failure
in completion mode is swallowed and falls back to the slow path.

The payoff is registry-shaped completion everywhere — captured live from a
scaffolded demo repo at docs build time:

```{raw} html
:file: ../../_static/generated/termynal/complete-host-ids.html
```

More showcases live elsewhere: suite names and `--tests`
({doc}`../../guide/test`), instruction names ({doc}`../../guide/run/index`),
per-class host verbs ({doc}`../../guide/hosts/index`) plus registry-backed
option values ({doc}`../../guide/hosts/connections`), and `--lab`
({doc}`../lifecycle`).

The consistent rule behind all of them: the process answering the keystroke
**never runs user code**. Registry names come from the cache the slow path
already wrote; host ids and lab names are read from `lab.json` data;
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
- {mod}`otto.cli.run` / {mod}`otto.suite.register` — the `INSTRUCTIONS` and
  `SUITES` registrations (`@instruction()`, `OttoSuite.__init_subclass__`)
- `otto.config.completion_cache` — the completion fast path's cache
- the host-side registries live beside the strategy they select:
  {mod}`otto.host.os_profile`, {mod}`otto.host.connections`,
  {mod}`otto.host.transfer`, {mod}`otto.host.command_frame`,
  {mod}`otto.host.binary_loader`, {mod}`otto.host.embedded_filesystem`,
  {mod}`otto.host.power`
- `otto.labs`, {mod}`otto.reservations.registry`,
  {mod}`otto.monitor.parsers`, {mod}`otto.monitor.snmp` — the remaining
  registries in the inventory table
