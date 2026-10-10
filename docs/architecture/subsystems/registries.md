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

## The engine

{mod}`otto.registry` builds four kinds of table, and the rules below live in
the engine, not in each seam's wrapper:

- a {class}`~otto.registry.Registry` of named records;
- a {class}`~otto.registry.BackendRegistry`, which also parses a backend's
  configuration once ({meth}`~otto.registry.BackendRegistry.prepare`) and
  builds the backend from it ({meth}`~otto.registry.BackendRegistry.build`),
  raising the seam's own construction error with the failing stage named;
- a {class}`~otto.registry.Subscription`, an ordered collection whose
  repeated values stay repeated;
- a {class}`~otto.registry.RegistryView`, a read-only table derived from
  other tables.

**Records.** A registry declares its `entry` type, and every registration is
a complete record of it: a frozen dataclass, or a frozen model. The record's
own type must be frozen, so a subclass of the entry type that thaws itself is
refused. A bare class,
a dict, a record of another type, or a record holding a `list`, `dict`, `set`
or `bytearray` anywhere raises {class}`~otto.registry.IncompleteRegistration`;
records hold tuples and {class}`~otto.registry.FrozenMap`s instead
(`FrozenMap.freeze_json` converts JSON-shaped data). A class or a callable in a
field (a factory function, a partial, an object with `__call__`) is behaviour,
not data: the check never looks inside it. A
{class}`~otto.registry.Ref` lives only in a record field, never on its own:
`ClassEntry(Ref("my_plugin.frames:FishFrame"))` is a registration, a bare
`Ref` is refused. A registry whose records may hold a `Ref` declares a
`check_resolved` hook; a registry without one refuses a record holding a
`Ref`, so no lazy field goes unchecked. At a record's first `get()`, each
top-level `Ref` field is imported, and then the record is checked, once. A
`Ref` nested inside a field (in a tuple or a `FrozenMap`) stays opaque: `get()`
never imports it, and the seam's own code resolves it when it needs the
object. `check_resolved` still runs once for such a record, at its first
`get()`, on the record as stored. A failing check caches nothing, so the next
`get()` runs it again.

**Duplicates.** A taken name raises
{class}`~otto.registry.DuplicateRegistration`, naming both registering
modules, unless the caller passes `overwrite=True`, which replaces the entry
completely, in place. A batch (`register_many`) checks every name before it
writes any, and `overwrite=True` never covers a name repeated within one
batch. A failed registration changes nothing.

**Attribution.** The engine records who registered each entry; no caller
supplies it. A direct `.register()` credits the module that called it. Every
`register_*` wrapper and registering decorator is a transparent boundary, so
an entry registered through one is credited to the module that called the
wrapper. A helper of your own that wraps a wrapper is credited as the
registrant. The repo comes from the init import that is running.

**Revision.** Every change to what a table holds (a register, an unregister,
a subscribe, a token's cancel, a test restore) bumps its `revision`, and a
cache derived from a table keys on
it. The first `get()` of a record holding a `Ref` caches the resolved record
without a bump: it changes no observable entry.

**Capabilities.** A registry follows every rule above unless it declares a
deviation, at its definition, with its reason:
`capabilities=[Justified(RequireRepo(), reason="...")]`. The set is closed;
its one member, {class}`~otto.registry.RequireRepo`, refuses a registration
made outside a repo's init import. Each declaration is also listed in a
shrink-only approved list in the tests (`tests/unit/registry/approved_capabilities.py`),
which fails on any difference from the live declarations. Whether a
declaration is justified is review's call ([What remains
policy](#what-remains-policy)).

**Final types.** `Registry`, `BackendRegistry`, `Subscription` and
`RegistryView` cannot be subclassed (`@final`; a `Registry` subclass also
fails when constructed). A seam's own rules live in its record type and its
`validate` and `check_resolved` hooks.

**Views.** A {class}`~otto.registry.RegistryView` has no `register` or
`unregister`. Its entries are derived from its source tables, each with the
origin and repo the derivation gives it, and the derivation is cached until a
source's `revision` moves; the view's own `revision` moves with it. A rule
that spans the sources lives in their `validate` hooks, which read the other
source and refuse a registration the view could not derive, so a view never
holds an entry that breaks it. `INSTRUCTIONS` is one: every `otto run`
command, derived from `PROJECT_INSTRUCTIONS` (itself a view over
`PROJECT_ACTIONS` and otto's own bodies) and `STANDALONE_INSTRUCTIONS`.

## The registry inventory

| Registry | Kind | Register via | Built-ins |
| --- | --- | --- | --- |
| `CLI_COMMANDS` | top-level CLI command | {func}`otto.cli.registry.register_cli_command` / {func}`~otto.cli.registry.cli_command` | the fourteen first-party verbs (`run`, `test`, `host`, …) |
| `STANDALONE_INSTRUCTIONS` | `otto run` subcommand on a plain function | {func}`~otto.instructions.instruction` | — |
| `PROJECT_ACTIONS` | a repo's `ProjectActions` subclass and the bodies it declares, keyed by the repo (only from that repo's init import) | `otto.project.actions.register_project_actions` | — |
| `PROJECT_INSTRUCTIONS` (view) | project instruction (a `ProjectActions` method), derived from otto's own bodies and `PROJECT_ACTIONS` | {func}`~otto.instructions.instruction` on a `ProjectActions` method | `install`, `uninstall`, `status`, `cleanup`, `get-logs`, `install-tools` |
| `INSTRUCTIONS` (view) | `otto run` subcommand, derived from `PROJECT_INSTRUCTIONS` and `STANDALONE_INSTRUCTIONS` | — | the six project instructions |
| `OPTIONS` | options class, with the verbs (`run`, `test`) whose flags it joins | {func}`otto.params.register_options` / `@options(verbs=[...])` | — |
| `HOST_CLASSES` | host class | `otto.host.os_profile.register_host_class` | `unix`, `embedded`, `zephyr` |
| `OS_PROFILES` | `os_type` profile registered by code (a repo's `[os_profiles]` tables and the host classes' own profiles are separate layers) | `otto.host.os_profile.register_os_profile` | — |
| `LOGIN_PROXIES` | login proxy | `otto.host.login_proxy.register_login_proxy` | `su` |
| `TERM_BACKENDS` | term (connection) backend | `otto.host.connections.register_term_backend` | `ssh`, `telnet`, `console` |
| `TRANSFER_BACKENDS` | transfer backend | `otto.host.transfer.register_transfer_backend` | `sftp`, `scp`, `ftp`, `nc`, `shell`, `console`, `tftp` |
| `FRAME_CLASSES` | command frame | `otto.host.command_frame.register_command_frame` | `bash`, `ash`, `zephyr`, `zephyr-serial`, `raw` |
| `LOADER_CLASSES` | binary loader | `otto.host.binary_loader.register_binary_loader` | `llext-hex` |
| `FILESYSTEM_CLASSES` | embedded filesystem type | `otto.host.embedded_filesystem.register_filesystem` | `fat-ram`, `littlefs`, `none` |
| `POWER_CONTROLLERS` | power controller | `otto.host.power.register_power_controller` | `command` |
| `SESSION_SETUPS` | session setup hook | `otto.host.session_setup.register_session_setup` | — |
| `PRODUCT_KINDS` | settings-declared product kind | `otto.host.product.register_product_kind` | `shell`, `kmod`, `embedded`, `docker_image` |
| `DEV_TOOL_KINDS` | settings-declared dev tool kind | `otto.host.dev_tool.register_dev_tool_kind` | `shell`, `kmod`, `kmodcov` |
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
| `PRODUCT_PROVIDERS` (subscription) | product provider | `otto.host.register_product_provider` | — |
| `DEV_TOOL_PROVIDERS` (subscription) | dev tool provider | `otto.host.register_dev_tool_provider` | — |

The two provider seams are subscriptions, not named registries: every
subscribed provider runs for every lab-ingested host, in subscription order,
and each keeps the repo that registered it; see {doc}`hosts`.

## References

A {class}`~otto.registry.Ref` wraps a `"package.module:attribute"` string
that names an object without importing it. It sits in a field of the
record: a class seam stores
{class}`~otto.registry.ClassEntry` records, whose `cls` is the class or a
`Ref` naming it, and the kinds and session setups store a `factory` or an
`fn` the same way, and a backend seam its `cls` or its `config` and
`factory`. What each read costs:

- **Listing and attribution are free.** `names()`, `in`, `len()`,
  `origin()`, `peek()` and `unregister()` never import a `Ref`'s target, so
  help and completion can list every built-in without loading one.
- **`get(name)` resolves one entry.** Its first call imports each top-level
  `Ref` field, runs the registry's `check_resolved` on the resolved record,
  and caches that record in place of the stored one, so later reads are
  dictionary lookups. If the import or the check fails, the error propagates
  and nothing is cached, so the next lookup tries again.
- **`items()` resolves every entry,** the same way, so it costs every
  import. Code that only needs names calls `names()`.

`get()` returns the record. Its `Ref`-able field is typed as "the object or a
`Ref`", so code that reads it states the narrowing with
{func}`~otto.registry.resolved`: `resolved(FRAME_CLASSES.get("bash").cls)`.
The seams' own build functions (`build_command_frame`, `build_impairer`, …)
do this for you.

A plugin registers either form through the public wrapper; a real object is
checked at registration, a `Ref` at its first lookup, by the same check. By
reference:

```python
from otto.host.command_frame import register_command_frame
from otto.registry import Ref

register_command_frame("fish", Ref("my_plugin.frames:FishFrame"))
```

The import runs, and the check with it, the first time lab data builds a
`fish` frame. The raw path needs the record: `FRAME_CLASSES.register("fish",
ClassEntry(Ref("my_plugin.frames:FishFrame")))`. A name that is already
registered (such as the built-in `ash`) raises unless you pass
`overwrite=True`. See {class}`otto.registry.Ref` and
{meth}`otto.registry.Registry.get` for the full contract.

## Built-ins register by reference

Every built-in entry is registered in the module that **defines its
registry** (the built-in CLI commands in `otto.cli.builtin_commands`), with
a {class}`~otto.registry.Ref` naming the object's real home in its record
(`TRANSFER_BACKENDS`' entry under `nc` is a class backend whose
class is `Ref("otto.host.transfer.nc:NcFileTransfer")`). Importing a registry
therefore lists all of its built-ins without importing one implementation,
and a lookup imports only the entry it names:
`otto host --help` pays for no transfer backend, and building an `scp`
transfer imports `scp` alone.

Two consequences shape the code:

- **No implementation module registers itself, and no package imports one
  to make it register.** A registration left behind in a backend's own
  module would collide loudly with the reference the first time the backend
  is imported. The engine credits each built-in to the module that
  registered it (the registry's own module, or `otto.cli.builtin_commands`
  for the built-in CLI commands), not to a backend's own implementation
  module.
- **Object checks live in the registry.** Each registry's checks of an entry
  (a `type_name` that matches, declared host families, a transfer backend's
  progress granularity) run on a third party's object at registration and on
  a built-in at its first lookup. That is one check with two call sites:
  `validate` runs it on an object registered eagerly, `check_resolved` on
  the object a `Ref` names once it is imported.
  Wrappers keep what is more than a check of the object:
  `register_host_class` finds the nearest spec and warns on overriding a
  built-in; `register_transfer_backend`
  copies a class's declarations into its metadata. A built-in transfer
  backend states its declarations beside its `Ref`, so they are read without
  importing it; `check_resolved` refuses a class that disagrees with them.
  A `HOST_CLASSES` record carries the class, its spec and its own profile;
  its one check covers all three. `OS_PROFILES` checks a profile's defaults
  against its base class's fields and its prompt regexes.

Built-ins whose value is built beside the registry itself — the `su` login proxy, the SNMP descriptors, the term backends (whose
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

## What the checks enforce

The engine applies its rules at runtime. Around it, the tests and the
ast-grep rules ({doc}`../quality-gates`) keep the code from going around it:

- **Every table is found and covered.** A source scan finds every
  module-level `Registry(`, `BackendRegistry(`, `Subscription(` and
  `RegistryView(` in `src/otto` and must equal the engine's own list of live
  tables (`tests/unit/test_registry_loading.py`); every table has a
  conformance case (`tests/unit/registry/test_conformance.py`); and a test
  session fails if two live tables share a module and a kind, which means a
  module was imported twice.
- **Every built-in can be replaced.** For every built-in name of every
  class-valued and backend table, a replacement differential registers a
  replacement with `overwrite=True` and builds through the real product path
  (`tests/unit/registry/test_replacement_*.py`). The set of names is
  computed from the tables, so a new built-in without one fails.
- **No seam goes around the engine.** ast-grep rules refuse a literal
  `overwrite=True` in otto (`registry-overwrite-only-approved`), a
  registration function writing a plain module-level dict or list
  (`no-adhoc-registry`), a subclass of an engine type
  (`no-registry-subclass`, with an AST binding scan for aliased imports), a
  comparison against a backend's name (`backend-no-name-dispatch`), and a
  built-in backend constructed outside its registered factory
  (`backend-construction-through-entry`). ty refuses a subclass of the four
  engine types, and no new registration or construction surface may take or
  return `Any`, `Callable[..., Any]`, a bare `Registry` or a bare `Ref`.

## What remains policy

The checks cannot decide these; review and the replacement differential
cover them, and nothing here claims otherwise:

- whether a reason is a good reason: a `Justified(..., reason=...)`
  capability declaration states one, and only a reviewer can judge it;
- whether an approved-list addition is accepted
  (`tests/unit/registry/approved_capabilities.py` fails on any difference,
  but cannot say whether the addition should be made);
- whether an arbitrary helper eventually constructs a built-in: the
  construction rule sees direct calls in the orchestration modules only, and
  the replacement differential is the guard for everything else;
- whether a module keeps a registry in a plain dict written from an
  unconventionally named function: `no-adhoc-registry` sees only
  registration-shaped names.

## The options registry

`OPTIONS` ({mod}`otto.params`) holds the options classes registered for a
verb. Its entry is an `OptionsEntry`: the class, or a `Ref` to it, together
with the verbs it serves, so the verbs are known without importing the class;
the repo that registered it is the engine's (`OPTIONS.repo(key)`). That is
what lets `otto host` and every other command stay free of options modules
registered by string.

- **The key is the class's `module:qualname`,** and the registry's
  *validate* refuses a record whose class has another key. A class
  therefore has one key, by object or by string, and registering it again
  follows the engine's [duplicate rule](#the-engine); `overwrite=True`
  replaces the verb list. A string that resolves to a re-export (a
  class whose own `module:qualname` differs) is refused by the registry's
  *check_resolved* when it is resolved, since it would otherwise be a
  second key for one class.
- **Verbs are an `OptionVerb`** (`Literal["run", "test"]`, listed in
  `OPTION_VERBS`), checked at registration: an unknown verb, an empty list
  or a repeated verb raises there, not at dispatch.
- **Reading a verb resolves only that verb's classes**
  (`verb_option_classes`), in registration order: otto's own first, then
  each repo's in dependency order. `merge_option_params` then merges their
  fields by declaring class, the same rule project instructions use.

The architecture of the verbs themselves, and how the built instances reach
tests and instructions, is on {doc}`execution`.

## What test files may register

Test files and conftests load only inside a pytest session: `otto test`'s
collection and run, and the completion cache's bounded collection. A
registration made there would exist for `otto test` and for no other command,
so it is refused. While otto's sessions import test files
({func}`~otto.registry.loading_test_files`), every registry refuses an entry
whose origin is outside the `otto` package, with
{class}`~otto.registry.RegistrationRefused`, whose message says to register
from an init module instead.

- **The check is on origin, not only on the phase.** A test file is often the
  first thing to import an otto module that registers its own entries when
  imported; `otto.host.connections`, for example, registers the built-in
  term backends.
  That registration belongs to otto and succeeds. The flip side is that every
  public `register_*` wrapper must record the module that *called* it as the
  origin: an entry attributed to the wrapper's own `otto.*` module would pass
  the check.
- **The two provider subscriptions refuse the same way.** `PRODUCT_PROVIDERS`
  and `DEV_TOOL_PROVIDERS` are engine tables too, so
  `register_product_provider` and `register_dev_tool_provider` are refused
  while test files load, keyed on the module that called them, not on the
  module the provider function was defined in.
- **A `Ref` resolved during a session imports outside the phase.** The first
  `get()` of an entry registered by reference can happen inside a pytest
  session, for example when a test's first host command looks up a transfer
  backend. Its target was named by an init module or by otto, so {meth}`Ref.resolve <otto.registry.Ref.resolve>` lifts the phase for
  the import, and whatever that module registers when imported belongs to
  the init module. The phase is back in force when the import returns.
- **A guard test** (`tests/unit/test_registry_loading.py`) finds every
  registry, backend registry and subscription otto constructs and asserts
  that each one refuses, so a table added later is covered without anyone
  remembering to list it.

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

A duplicate command name raises `DuplicateRegistration` unless the second
registration passes `overwrite=True`, as in every other registry; see
[Collisions](../../cookbook/extending/extending-cli.md#collisions).

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

More showcases live elsewhere: test names
({doc}`../../cli/test/selection`), instruction names ({doc}`../../cli/run/index`),
per-class host verbs ({doc}`../../cli/host/index`) plus registry-backed
option values ({doc}`../../cli/host/connections`), and `--lab`
({doc}`../lifecycle`).

The consistent rule behind all of them: a keystroke answered from the cache
**never runs user code**, and one that finds the cache missing or stale runs
it once, to rebuild the cache. Registry names come from the completion cache
({doc}`completion-cache`); host ids and lab names are read from `lab.json` data;
test names come from each repo's per-file table of what pytest last
collected, which runs and listings keep up to date. Test names are the one
case that genuinely needs a live pytest collection, and it is handled without
breaking that rule: when a table must be seeded or refreshed, the collection
runs in a disposable, timeout-bounded *subprocess*, and later completions read
its result from the cache ({doc}`completion-cache`, "The test-names cache").

## Where the code lives

- {mod}`otto.registry` — the `Registry` engine underneath every entry in the
  inventory: loud duplicates, did-you-mean lookups, attribution
- {mod}`otto.cli.registry` — `CommandSpec`, the CLI command registry, and
  lazy dispatch
- {mod}`otto.instructions` — the `@instruction` decorator, the instruction
  tables (`STANDALONE_INSTRUCTIONS`, `PROJECT_ACTIONS` and the two views
  derived from them), and `run_instruction`, deliberately CLI-free
  (`InstructionEntry` is plain data with no Typer field; the CLI builds the
  command from it) so core readers — `Repo`'s instruction panel, the
  completion cache — see the registered set without importing `otto.cli`
- {mod}`otto.cli.run` — `build_instruction_app`: the Typer projection of an
  entry, built when `otto run` resolves it
- {mod}`otto.params` — the `OPTIONS` registry, `register_options` and
  `@options(verbs=[...])`
- `otto.config.completion_cache` — the completion cache
  ({doc}`completion-cache`)
- the host-side registries live beside the strategy they select:
  {mod}`otto.host.os_profile`, {mod}`otto.host.login_proxy`,
  {mod}`otto.host.connections`, `otto.host.transfer.registry`,
  {mod}`otto.host.command_frame`, {mod}`otto.host.binary_loader`,
  {mod}`otto.host.embedded_filesystem`, {mod}`otto.host.power`,
  {mod}`otto.host.session_setup`, {mod}`otto.host.product`,
  {mod}`otto.host.dev_tool`
- `otto.labs.registry`, `otto.inventory.registry`,
  {mod}`otto.reservations.registry`, `otto.creds.registry`,
  `otto.link.impairer`, `otto.tunnel.carrier`, `otto.docker.adapter`,
  {mod}`otto.monitor.parsers`, {mod}`otto.monitor.snmp` — the remaining
  registries in the inventory table
