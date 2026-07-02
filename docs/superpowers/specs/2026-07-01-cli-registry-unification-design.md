# CLI command registration & registry unification — design

**Date:** 2026-07-01
**Status:** Approved design, pending implementation plan
**Origin:** Chris's ask: let otto users register their own CLI commands — even
top-level — with the first-party/third-party symmetry paradigm otto already
uses for backends; unify the multiple CLI registration mechanisms and the
hand-rolled registry copies noted in
[todo/fable_review_verification.md](../../../todo/fable_review_verification.md);
fold in adjacent review follow-ups (F3 bootstrap, F5 dead knobs, F7 API tidy,
the lab-free-flag token-sniffing suspicion, the missing did-you-mean
suggestions). A secondary first-class goal: use the registry to make CLI
startup and tab completion lazier.

## Design principle: Typer-native, registry-deferred

Typer remains the engine for everything user-visible — parameter parsing,
annotation-driven type enforcement, `--help` formatting, completion protocol.
The registry never reimplements CLI functionality; it is a deferred-mounting
index *in front of* Typer that decides **when** command objects are
constructed. The lazy hook is the one click natively provides and otto already
uses for host verbs (`HostGroup`, `cli/expose.py`): a custom `TyperGroup`
overriding `list_commands()` / `get_command(name)`. All custom groups subclass
Typer's re-exported `TyperGroup` — never real `click.Group` (Typer 0.26
vendors its own click fork).

## Goals

- Users register top-level CLI commands and command groups from init modules,
  through the same public API first-party subcommands use (full symmetry; the
  hard-coded `_SUBCOMMAND_MODULES` table dies).
- One storage idiom (a generic `Registry`) and one attachment idiom (lazy
  `TyperGroup` resolution) across the root app, `otto run` instructions,
  `otto test` suites, and host verbs.
- The ~13 hand-rolled backend registries adopt the same generic `Registry`
  (public accessors, uniform fail-loud errors **with did-you-mean
  suggestions**), fixing the review's private-dict imports and the
  lab-repo/reservations builder bypass.
- An explicit `bootstrap()` composition root replaces `configmodule`'s
  import-time exec of user code (review F3), with per-file error containment.
- Lazier startup and completion: `otto --help` imports zero command modules;
  dispatch imports only the target command's module; completion imports zero
  user code (cache) and zero command modules (stubs).
- Ride-alongs: F5 (honor `Arg(name=)`/`Opt(name=)`), F7 (export `load_lab`,
  one logger accessor, cookbook import paths), declarative `lab_free` command
  metadata replacing token sniffing.

## Non-goals

- Renaming `configmodule` (F7's "later" `otto.fleet` migration) — out.
- Entry-points (`importlib.metadata`) plugin discovery — out, but deliberately
  not precluded: a `CommandSpec` `"module:attr"` loader string is exactly an
  entry-point value, so it can arrive later as an additive discovery source.
- Third-party injection into first-party groups (e.g. adding
  `otto docker flash`) — closed by decision. Third parties get the top level
  (commands and whole groups) plus the existing domain seams
  (`@instruction`, `@register_suite`, `@cli_exposed` on custom host classes).
- Unifying per-registry **build semantics** (class vs instance vs
  settings-kwargs returns) — preserved as-is behind the existing
  `register_*`/`build_*` wrappers; only storage, errors, and accessors unify.
- The result-type unification (separate in-flight effort; sequencing
  dependency only — see Migration).

## 1. Generic `Registry` — `src/otto/registry.py`

```python
class Registry(Generic[T]):
    """Named registry of pluggable components; fail-loud lookups with suggestions."""

    def __init__(self, kind: str, *, register_hint: str) -> None: ...
        # kind="term backend"; register_hint="otto.register_term_backend()"

    def register(self, name: str, obj: T, *, overwrite: bool = False) -> None
    def get(self, name: str) -> T
    def names(self) -> list[str]          # registration order
    def __contains__(self, name: str) -> bool
```

- Each entry records its origin (the registering module, captured from the
  caller frame at `register()` time; for `"module:attr"` loaders, the module
  string itself) so errors can attribute entries.
- `register()` with a duplicate name and `overwrite=False` raises `ValueError`
  naming both origins — collisions are loud by default; explicit
  `overwrite=True` supports deliberate replacement (the documented
  re-register-`json` customization).
- `get()` on an unknown name raises `ValueError` listing registered names,
  adding `difflib.get_close_matches` did-you-mean suggestions, and pointing at
  the register function via `register_hint`. This closes the review's "errors
  name the offending key but don't propose the fix" miss for every registry at
  once — including the pydantic validators that validate host-spec selectors
  against live registries.
- Registration happens at import/bootstrap time on one thread — no locking.
- The class is public and documented (the CLI registry and all backend
  wrappers build on it), but third parties keep using the domain
  `register_*` functions.

**Adoption.** Each backend registry module replaces its private dict with a
module-level `Registry` instance; the existing public `register_*`/`build_*`
functions become thin wrappers (zero caller churn, build semantics
preserved):

| Registry | Module | Private dict replaced |
| --- | --- | --- |
| Term backends | `host/connections.py` | `_TERM_BACKENDS` |
| Transfer backends | `host/transfer/registry.py` | `_TRANSFER_BACKENDS` |
| Command frames | `host/command_frame.py` | `_FRAME_CLASSES` |
| Binary loaders | `host/binary_loader.py` | `_LOADER_CLASSES` |
| Embedded filesystems | `host/embedded_filesystem.py` | `_FILESYSTEM_CLASSES` |
| Power controllers | `host/power.py` | `_POWER_CONTROLLERS` |
| Host classes | `host/os_profile.py` | `_HOST_CLASSES` |
| OS profiles | `host/os_profile.py` | `_OS_PROFILES` |
| Lab repositories | `storage/registry.py` | `_LAB_REPOSITORIES` |
| Reservation backends | `reservations/registry.py` | `_RESERVATION_BACKENDS` |
| Monitor host parsers | `monitor/parsers.py` | `_host_parser_registry` |
| SNMP metrics | `monitor/snmp.py` | `_SNMP_METRICS` |

**Documented exception (found during implementation, 2026-07-01):**
`host/product.py`'s `_PRODUCT_PROVIDERS` is an *unkeyed ordered list* of
anonymous provider callables — not a name-keyed registry — so it does not
adopt `Registry` (a synthetic key would invent semantics its API never had).
The adoption table is 12 name-keyed registries, not 13.

Two review findings fixed by adoption:

- **Private-dict reaches die:** `cli/host.py` and `models/host.py` switch from
  importing `_TERM_BACKENDS`/`_TRANSFER_BACKENDS` to the public `names()`
  accessor.
- **Builder bypass dies:** `build_lab_repository` (`storage/__init__.py`) and
  reservations' `build_backend` stop hard-coding built-in branches; built-ins
  resolve through the registry like third-party entries (re-registering
  `json` becomes effective, per the WS#4 contract).

## 2. The CLI command registry — `src/otto/cli/registry.py`

```python
@dataclass(frozen=True)
class CommandSpec:
    name: str                              # CLI name: "flash", "run", "mytool"
    loader: typer.Typer | Callable | str   # live app/function, or lazy "pkg.mod:attr"
    help: str | None = None                # root --help line WITHOUT importing the module
    lab_free: bool = False                 # command runs without lab bootstrap
    origin: str = ""                       # auto-captured registering module
```

Stored in a module-level `Registry[CommandSpec]`. The spec-level `help` string
is what lets `otto --help` render the full command table without importing any
subcommand module — the job today's placeholder subapps fake.

**Public API** (lazily exported from `otto` top level, like the backend
`register_*` family):

- `register_cli_command(name, loader, *, help=None, lab_free=False)` — a
  `typer.Typer` app for groups, a plain/async function for leaf commands, or a
  `"module:attr"` string for deferred loading. Init modules typically pass
  live objects (they are already executing); `"module:attr"` primarily serves
  first-party laziness and future entry points.
- `@cli_command(...)` decorator sugar for plain async functions, reusing
  `@instruction`'s wrapper machinery — factored into a shared helper module:
  `async_typer_command`, `OttoContext` injection (an `OttoContext`-annotated
  param is stripped from the CLI signature and injected), and `options=`
  pydantic-dataclass expansion. Writing a top-level command feels exactly like
  writing an instruction.

**Collision policy.** Duplicate command name at registration = immediate
`ValueError` naming both origins ("`flash` already registered by
`repo_a.init`; second registration from `repo_b.init`"). No shadowing of
first-party names, no first-wins silence, and **no** `overwrite=True` escape
hatch for CLI commands (unlike backend registries).

**Root assembly** (`cli/main.py`). `_OttoGroup` becomes registry-backed:
`list_commands()` returns registry names in pure registration order — builtins
first (registered by `register_builtin_commands()`), then third-party in
init-module load order (amended 2026-07-02 to match implementation); `get_command(name)` resolves the spec's
loader (importing `"module:attr"` only then), converts the Typer app via
Typer's app→click converter, and caches. Deleted outright:
`_SUBCOMMAND_MODULES`, `_LAB_FREE_SUBCOMMANDS`, `_placeholder_subapp`, and the
wanted/placeholder dispatch dance in `_register_subcommands()`.

**First-party composition** — `src/otto/cli/builtin_commands.py`: one readable
module registering the eight groups through the public API with
`"module:attr"` strings, spec help lines, and `lab_free=True` for `schema`.
This is the single successor to the old table and the direct analog of the
backend registries' `_register_builtin_*()` functions.

## 3. Front-end re-plumbing (not replacement)

- **`@instruction`** keeps its exact user-facing API but stops mutating
  `run_app` at import time: it registers into an instructions
  `Registry`, and the `run` group resolves from it via the same lazy-group
  pattern. `--list-instructions` reads the registry instead of introspecting
  `run_app.registered_groups`.
- **`@register_suite`** likewise: `_SUITE_REGISTRY` (list) and `_SUITE_FILES`
  (side dict) collapse into one suites `Registry` whose spec carries the
  source file for attribution; `suite_app` resolves lazily, replacing the
  drain-loop in `cli/test.py`.
- **`@cli_exposed`** host-verb synthesis already is the lazy-group pattern and
  keeps its mechanism (its invoke preamble changes per §4) — plus **F5**:
  `build_cli_binding()`
  (`cli/param_synth.py`) honors `Arg(name=...)`/`Opt(name=...)` instead of
  silently ignoring them, with an end-to-end test proving a renamed option
  works. (Honor, not delete: a host-class author renaming a CLI flag away
  from the Python param name is the overlay's whole point.)

## 4. `bootstrap()` — the explicit composition root

**New module `src/otto/bootstrap.py`** with an idempotent `bootstrap()`,
replacing the import-time side effects in `configmodule/__init__` (repo
discovery at lines 52-53, `apply_repo_settings` at 130-154). `import
otto.configmodule` becomes side-effect-free, killing the import-order comment
gymnastics the review flagged.

Phases:

1. **Discovery** *(no user code)* — env parse, repo discovery, `settings.toml`
   parse.
2. **Registration** *(user code, contained)* — `add_libs_to_pythonpath()`,
   `import_init_modules()`, `import_test_files()`. Every user-module exec is
   wrapped; a failure becomes `BootstrapError("repo <sut_dir>: failed to load
   <file>: …")` chaining the original.
3. **Lab** *(deferred — not part of `bootstrap()`)* — lab/hosts loading moves
   to first access, wherever that happens: command dispatch when the resolved
   spec has `lab_free=False`, `HostGroup` resolving a host id to its class at
   parse time, or `open_context()` for library use. Help/discovery paths
   never reach any of these.

**Containment policy** (the F3 fix):

- **Help / discovery / completion degrade gracefully:** a broken user file
  prints one framed warning line to stderr and the command tree renders with
  first-party plus whatever registered successfully. A syntax error in
  someone's `test_*.py` can never again brick `otto --help`, `--version`, or
  tab completion.
- **Real command dispatch fails loud** with the framed `BootstrapError` — no
  running with half a registry (never-skip ethos). The framed message names
  the repo/file to fix; the raw traceback goes to the log.

**Callers.** The CLI entrypoint calls `bootstrap()` before handing argv to
Typer (the registry must be populated before `list_commands()` is consulted);
`open_context()` calls it lazily behind a `_bootstrapped` guard; the
completion entry skips it entirely via the cache.

**Two structural deletions this enables:**

- **`_is_lab_free_flag_invocation` token sniffing dies.** Lab load is lazy, so
  help/discovery paths never touch the lab — no raw-token intersection, no
  false positive on an option value equal to `--help`. Command lab-freeness is
  spec metadata; flag lab-freeness falls out of laziness.
- **The `ctx.meta["_help_or_discovery"]` verdict plumbing (from `9b7b0c4`)
  dies.** Per-command output-dir creation **and the reservation gate** move
  out of group callbacks into the leaf invocation preamble (the
  `async_typer_command` / `@cli_exposed` invoke path) — which click never runs
  on a `--help` — so help paths are structurally incapable of creating output
  dirs or gating on reservations, rather than being guarded by a precomputed
  verdict. Semantics preserved: the gate still runs for every *real* verb
  invocation; `@cli_exposed(output_dir=False)` verbs and the no-dir groups
  (`cov`, `reservation`) behave exactly as today. The `9b7b0c4` e2e regression
  suite (`--help` across groups, `assert_no_output_dir`) is the acceptance
  contract.

## 5. Completion & startup performance

- The completion cache stops introspecting `run_app.registered_groups` ad-hoc
  and serializes the registries directly: command name + spec help +
  `lab_free` + per-command option metadata for stubs. One uniform collector;
  third-party top-level commands appear in tab completion for free. Cache
  schema version bumps; invalidation mechanism unchanged.
- Steady-state import profile: `otto <cmd>` imports otto core + bootstrap
  (user init modules) + **only the target command's module**; `otto --help`
  imports **zero** command modules; completion imports zero user code (cache)
  and zero command modules (stubs).
- Import-budget golden snapshots regenerate (configmodule losing import-time
  side effects changes the profile); the guard keeps the floor honest.
  `bootstrap.py` itself must be import-light.

## 6. Ride-along: F7 public API tidy

- Export `load_lab` from `otto` top level (lazy `_LAZY_EXPORTS` entry) — the
  library-usage bring-your-own-CLI recipe requires it.
- **Delete `get_otto_logger`**, keeping `get_logger` as the one accessor
  (delete-first, pre-freeze, consistent with the result-type spec's no-shims
  policy).
- Fix the cookbook's inner-path teaching
  (`from otto.configmodule.configmodule import ...`) to the package path.

## 7. Documentation (required deliverable)

- New `docs/guide/extending-cli.md`: registering commands and groups,
  `@cli_command`, `lab_free`, collision behavior, when init modules run
  (bootstrap timing), completion behavior. Executable examples where
  practical (docs gate runs doctests).
- `docs/guide/extending-backends.md`: registry errors now include
  did-you-mean suggestions; note the public `names()` accessors.
- `docs/guide/library-usage.md`: the explicit `bootstrap()`/`open_context()`
  story and side-effect-free imports.
- Docstrings throughout satisfy the pydocstyle gate; no
  `from __future__ import annotations` anywhere.

## 8. Testing

- **Unit — `tests/unit/registry/`:** generic `Registry` (register/get/names,
  duplicate + overwrite semantics, error text: known names, suggestions,
  register hint).
- **Unit — CLI registry:** lazy `"module:attr"` resolution (module imported
  only on `get_command`), collision `ValueError` naming both origins,
  first-party-then-third-party ordering, `help` rendering without import.
- **Unit — bootstrap:** idempotence, phase ordering, containment framing
  (repo/file named, original chained), degraded-help vs fail-loud-dispatch
  policy split.
- **Unit — param_synth (F5):** `Opt(name="--dest")` renames the flag
  end-to-end; `Arg(name=...)` likewise.
- **e2e (hostless, existing CLI-subprocess harness):** `tests/repo_e2e` grows
  a top-level third-party command fixture — `otto <cmd>` dispatches, appears
  in `otto --help` and completion cache, collides loudly on double
  registration; a broken-init-module fixture asserts `--help` degrades with
  the framed warning while real dispatch fails framed.
- **Regression contract:** the `9b7b0c4` e2e suite (per-group `--help`,
  `assert_no_output_dir`) and the exit-code e2e stay green untouched.
- **Full gate:** `make coverage`, nox lint (ruff check + format --check),
  typecheck (`ty` only runs here — budget a round after src edits), docs.

## 9. Migration (delete-first) & sequencing

- **Deleted, not aliased** (pre-freeze, zero-users policy):
  `_SUBCOMMAND_MODULES`, `_LAB_FREE_SUBCOMMANDS`, `_placeholder_subapp`,
  `_is_lab_free_flag_invocation`, the `ctx.meta["_help_or_discovery"]`
  verdict, `_SUITE_REGISTRY`/`_SUITE_FILES`, the 13 private registry dicts,
  `get_otto_logger`, configmodule's import-time side effects.
- **Sequencing:** this effort starts **after** the in-flight result-type
  unification lands on main (both touch `cli/expose.py`/`cli/run.py`); fresh
  worktree, `uv sync` first. The efforts compose: the new renderer's
  documented plain-value fallback is exactly what third-party command output
  rides on.
- Conversion order inside this effort: generic `Registry` + backend adoption
  (pure refactor, everything stays green) → CLI registry + root assembly →
  bootstrap + lazy lab + verdict/token-sniffing deletion → front-end
  re-plumbing (instructions/suites/F5) → completion cache → F7 + docs → e2e +
  full gate.

## Decision log

| Decision | Choice | Alternatives rejected |
| --- | --- | --- |
| Registry scope | Generic `Registry` infra adopted by all 13 backend registries + CLI | CLI-only unification; targeted per-finding fixes without shared infra |
| First-party symmetry | Full — first-party groups register lazily by `"module:attr"` through the public API; hard-coded table dies | API-symmetry-only (re-creates the built-ins-bypass wart); eager first-party decorators (kills the lazy-startup win) |
| Third-party surface | Top-level commands + groups; domain decorators stay as front-ends | Open injection into first-party groups; leaf-commands-only |
| Core architecture | Declarative `CommandSpec` registry, Typer-native lazy groups | Typer-native `add_command` mounting (no metadata home, ad-hoc completion); entry-points as primary channel (wrong distribution model; left open as future additive source) |
| Bootstrap | Full composition-root `bootstrap()` with phase split + containment | Containment-only wrapping at current import-time site; out of scope |
| Containment policy | Help/discovery/completion degrade gracefully with framed stderr warning; real dispatch fails loud | Always-loud (F3's --help bricking survives); always-graceful (silently missing commands) |
| Lab-free handling | Lazy lab load + declarative `lab_free` spec field; token sniffing deleted | Keep/patch `_is_lab_free_flag_invocation` token set |
| Output-dir/gate timing | Leaf invocation preamble; verdict plumbing deleted | Keep `ctx.meta` verdict precomputation in root callback |
| CLI collision policy | Loud `ValueError` naming both origins; no overwrite for CLI commands | First-wins; `overwrite=True` parity with backend registries |
| F5 | Honor `Arg/Opt(name=...)` | Delete the knobs |
| F7 logger | Keep `get_logger`, delete `get_otto_logger` | Deprecation alias (pointless pre-freeze) |
| Ride-alongs | F5, F7, lab-free metadata, did-you-mean errors | bug#3 (verified already fixed in `9b7b0c4`, e2e-guarded) |
