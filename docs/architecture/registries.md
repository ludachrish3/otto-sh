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
| `CLI_COMMANDS` | top-level CLI command | {func}`otto.cli.registry.register_cli_command` / {func}`~otto.cli.registry.cli_command` | `run`, `test`, `host`, `monitor`, `cov`, `docker`, `reservation`, `schema`, … |
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
| `LAB_REPOSITORIES` | lab repository (host source) | {func}`otto.storage.register_lab_repository` | `json` |
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
({doc}`lifecycle`).

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
again. See {doc}`../guide/extending-cli` for the how-to.

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
(`otto/configmodule/completion_cache.py`) of command, suite, instruction, and
host names snapshotted on previous runs. On a cache hit, completion runs
*zero user code* — no bootstrap, no init modules. Cached third-party command
names still appear in listings even though their registrations never ran;
only actually dispatching one triggers the real import. Any discovery failure
in completion mode is swallowed and falls back to the slow path.
