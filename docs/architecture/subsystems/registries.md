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
| `CLI_COMMANDS` | top-level CLI command | {func}`otto.cli.registry.register_cli_command` / {func}`~otto.cli.registry.cli_command` | the fourteen first-party verbs (`run`, `test`, `host`, …) |
| `INSTRUCTIONS` | `otto run` subcommand | {func}`~otto.cli.run.instruction` | — |
| `PROJECT_INSTRUCTIONS` | project instruction (a `ProjectActions` method) | {func}`~otto.cli.run.instruction` on a `ProjectActions` method | `install`, `uninstall`, `status`, `cleanup`, `get-logs`, `install-tools` (when `otto.project.actions` is imported) |
| `PROJECT_ACTIONS` | a repo's `ProjectActions` subclass | `otto.project.actions.register_project_actions` | — |
| `SUITES` | `otto test` subcommand | {func}`~otto.suite.register.register_suite_class` (auto-called by {class}`~otto.suite.suite.OttoSuite`'s `__init_subclass__`) | — |
| `HOST_CLASSES` | host class | `otto.host.os_profile.register_host_class` | `unix`, `embedded`, `zephyr` |
| `OS_PROFILES` | `os_type` profile | `otto.host.os_profile.register_os_profile` | `unix`, `embedded`, `zephyr`, `busybox` |
| `LOGIN_PROXIES` | login proxy | `otto.host.login_proxy.register_login_proxy` | `su` |
| `TERM_BACKENDS` | term (connection) backend | `otto.host.connections.register_term_backend` | `ssh`, `telnet`, `console` |
| `TRANSFER_BACKENDS` | transfer backend | `otto.host.transfer.register_transfer_backend` | `sftp`, `scp`, `ftp`, `nc`, `shell`, `console`, `tftp` |
| `FRAME_CLASSES` | command frame | `otto.host.command_frame.register_command_frame` | `bash`, `ash`, `zephyr`, `zephyr-serial`, `raw` |
| `LOADER_CLASSES` | binary loader | `otto.host.binary_loader.register_binary_loader` | `llext-hex` |
| `FILESYSTEM_CLASSES` | embedded filesystem type | `otto.host.embedded_filesystem.register_filesystem` | `fat-ram`, `littlefs`, `none` |
| `POWER_CONTROLLERS` | power controller | `otto.host.power.register_power_controller` | `command` |
| `SESSION_SETUPS` | session setup hook | `otto.host.session_setup.register_session_setup` | — |
| `PRODUCT_KINDS` | settings-declared product kind | `otto.host.product.register_product_kind` | `shell`, `kmod`, `llext`, `docker_image` |
| `DEV_TOOL_KINDS` | settings-declared dev tool kind | `otto.host.dev_tool.register_dev_tool_kind` | `shell`, `kmod`, `kgcov` |
| `LAB_REPOSITORIES` | lab repository (host source) | {func}`otto.labs.register_lab_repository` | `json` |
| `INVENTORY_BACKENDS` | inventory backend | `otto.inventory.register_inventory_backend` | `json`, `netbox` |
| `RESERVATION_BACKENDS` | reservation backend | `otto.reservations.registry.register_reservation_backend` | `json`, `none` |
| `CREDS_BACKENDS` | creds store | `otto.creds.register_creds_backend` | `json` |
| `IMPAIRERS` | link impairer | `otto.link.register_impairer` | `netem` |
| `CARRIERS` | tunnel carrier | `otto.tunnel.register_carrier` | `socat` |
| `COMPOSE_ADAPTERS` | a repo's compose adapter, per use case | `otto.docker.register_compose_adapter` | — |
| `HOST_PARSERS` | monitor parser set, per host id | `otto.monitor.parsers.register_host_parsers` | — (a host with no entry uses the default `/proc` parsers) |
| `HOST_PATTERN_PARSERS` | monitor parser set, per host-id pattern | `otto.monitor.parsers.register_host_parsers` with a compiled pattern | — |
| `PROJECT_PARSERS` | project-level monitor parser | `otto.monitor.parsers.register_parsers` | — |
| `SNMP_METRICS` | SNMP metric descriptor | `otto.monitor.snmp.register_snmp_metric` | `sysUpTime`, plus CPU, heap and thread OIDs |

(Product and dev-tool providers are the two seams that are lists, not named
registries — every registered provider runs for every host; see {doc}`hosts`.)

## References

A registry entry may be a real object or a {class}`~otto.registry.Ref`
wrapping a `"package.module:attribute"` string that names one without
importing it. What each read costs:

- **Listing and attribution are free.** `names()`, `in`, `len()`,
  `origin()` and `unregister()` never import a `Ref`'s target, so help and
  completion can list every built-in without loading one. A registry with a
  loader runs it first on every read, these included; see
  [Lazy loaders](#lazy-loaders-and-what-test-files-may-register) below.
- **`get(name)` resolves one entry.** Its first call imports the target,
  runs the registry's *validate* hook (if it has one) on the object, and
  caches the object in place of the `Ref`, so later reads are dictionary
  lookups. If the import or the check fails, the error propagates and the
  entry stays a `Ref`, to be tried again on the next lookup.
- **`items()` resolves every entry,** the same way, so it costs every
  import. Code that only needs names calls `names()`.

A plugin may register either form through the registry's own `register`
(the public `register_*` wrappers take real objects); a real object is
validated at registration instead of at first lookup. By reference:

```python
from otto.host.command_frame import FRAME_CLASSES
from otto.registry import Ref

FRAME_CLASSES.register("fish", Ref("my_plugin.frames:FishFrame"))
```

The import runs, and the check with it, at the first `get("fish")`. A name
that is already registered (such as the built-in `ash`) raises unless you pass
`overwrite=True`. See {class}`otto.registry.Ref` and {meth}`otto.registry.Registry.get` for the full contract.

## Built-ins register by reference

Every built-in entry is registered in the module that **defines its
registry**, as a {class}`~otto.registry.Ref` naming the object's real home
(`TRANSFER_BACKENDS` holds `Ref("otto.host.transfer.nc:NcFileTransfer")` under
`nc`). Importing a registry therefore lists all of its built-ins without
importing one implementation, and a lookup imports only the entry it names:
`otto host --help` pays for no transfer backend, and building an `scp`
transfer imports `scp` alone.

Two consequences shape the code:

- **No implementation module registers itself, and no package imports one
  to make it register.** A registration left behind in a backend's own
  module would collide loudly with the reference the first time the backend
  is imported. Each entry keeps the origin it had before: the implementation
  module for the entries that used to register themselves (transfer
  backends, product and dev-tool kinds, `netem`, `socat`), the registry's own
  module for the rest (host classes; the inventory, creds, lab and
  reservation backends).
- **Object checks live in the registry.** Each registry's checks of an entry
  (a `type_name` that matches, declared host families, a transfer backend's
  progress granularity) are its *validate* hook, which runs on a third
  party's object at registration and on a built-in at its first lookup.
  Wrappers keep what is more than a check of the object:
  `register_host_class` finds the nearest spec, writes the same-named
  profile and warns on overriding a built-in; `register_term_backend` builds
  the `TermBackend` value. `OS_PROFILES` has no validator, so
  `register_os_profile` keeps its checks (defaults against the base class's
  fields, the prompt regexes); the built-in profiles, written straight into
  the registry, are held to those checks by a unit test instead.

Built-ins whose value is built beside the registry itself — an `os_type`
profile, the `su` login proxy, the SNMP descriptors, the term backends (whose
`ConnectionManager` class lives in the registry's own module) — are
registered as values: there is no implementation module for a reference to
defer.

Downstream repos keep registering real objects through the public `register_*`
functions, from their init modules (the `init` list in `.otto/settings.toml`),
which bootstrap imports in phase 2 ({doc}`../lifecycle`). Two guards in
`tests/unit/test_registry_refs_guard.py` hold the shape: every reference
resolves, and a fresh interpreter importing only a registry's defining module
lists every built-in, each still an unresolved reference. Both check the
built-in names they know. A new built-in that registers itself on import is
missing from their tables, so an ast-grep rule,
`.ast-grep/rules/builtin-registers-by-reference.yml`, names that line
instead: a registration that runs at import must pass a `Ref`, unless its
file defines the registry.

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
  imported; `otto.project.actions`, for example, registers the built-in
  project instructions.
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
  {mod}`otto.host.os_profile`, {mod}`otto.host.login_proxy`,
  {mod}`otto.host.connections`, `otto.host.transfer.registry`,
  {mod}`otto.host.command_frame`, {mod}`otto.host.binary_loader`,
  {mod}`otto.host.embedded_filesystem`, {mod}`otto.host.power`,
  {mod}`otto.host.session_setup`, {mod}`otto.host.product`,
  {mod}`otto.host.dev_tool`
- `otto.project.actions`, `otto.labs.registry`, `otto.inventory.registry`,
  {mod}`otto.reservations.registry`, `otto.creds.registry`,
  `otto.link.impairer`, `otto.tunnel.carrier`, `otto.docker.adapter`,
  {mod}`otto.monitor.parsers`, {mod}`otto.monitor.snmp` — the remaining
  registries in the inventory table
