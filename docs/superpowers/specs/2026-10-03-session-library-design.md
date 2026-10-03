# `otto.session`: the library path makes every decision the CLI makes

**Status:** approved in chat section by section (Chris, 2026-10-03).
**Issues:** fixes #508; refs #525 (thin-CLI series, item 7). Follow-up: #588
(the reservation report and whether `open_context` applies the gate).
**Principle:** `docs/architecture/principles.md`, "Input rules live in the
library entry point". Worked examples: the item 2 spec
(`2026-09-29-run-options-own-their-rules-design.md`) and the item 6 spec
(`2026-10-03-init-library-design.md`).

## 1. Intent

`otto --lab X <verb>` and `otto.open_context(lab="X")` should prepare the
same run. Today they do not. The CLI preamble (`src/otto/cli/main.py` root
callback, `src/otto/cli/invoke.py` `command_preamble` and its helpers) makes
decisions that the library path skips:

- **Lab composition.** `build_lab_from_repos` (`cli/invoke.py`) merges every
  repo's `[host_preferences]` in `OTTO_SUT_DIRS` order, composes the repos'
  `[[lab.sources]]` (`otto.labs.build_lab_sources`), builds the process
  inventory, calls `load_lab`, and the preamble then registers the declared
  container placeholder hosts (`otto.docker.compose.register_declared_container_hosts`).
  `open_context` calls `load_lab(lab, search_paths or [], inventory=...)`: no
  repo sources (a single json source over `search_paths`, which defaults to
  empty), no preferences, no placeholders.
- **The bootstrap gate.** `fail_loud_on_bootstrap_errors` makes an active
  repo's init or settings failure fatal, demoting only the errors of repos that
  are inactive before the lab (excluded with `-E`, or out of the labs' project
  scope; `-I` keeps an error fatal; an error with no repo is always fatal).
  `open_context` never reads `bootstrap().errors`: an active repo's broken init
  module silently leaves its instructions, backends and options classes
  unregistered.
- **Project selection.** `-I`/`-E` are validated (`validate_project_switches`:
  unknown name with a did-you-mean, overlap) and carried on the context.
  `open_context` has no equivalent.
- **The dependency preflight.** `refuse_unsatisfied_dependencies` refuses when
  an active repo's Python requirement is unmet and prints the dependency
  warnings. The docs say "library callers are not checked".
- **The inactive-instruction refusal.** `refuse_inactive_instruction` refuses
  dispatching an instruction whose owning repo is inactive. `otto.run_instruction`
  does not.
- **Logging.** The preamble merges every repo's `[logging.levels]`
  (`merge_logging_levels`, refusing a conflict) and attaches the `HostFilter`
  console suppression. A library caller who installs otto's logging gets
  neither.

A `bootstrap()` → `run_tests()` script or an `open_context` block therefore runs
against a lab the CLI would not build, with registrations possibly missing.

## 2. Rulings (Chris, 2026-10-03)

1. **Parity scope: everything decision-like.** The library path shares the lab
   composition, the bootstrap gate, project selection, the dependency
   preflight and the inactive-instruction refusal with the CLI. The reservation
   gate stays CLI-only in this item (ruling 6); the output directory and the
   dry-run seam stay CLI-only.
2. **`search_paths` is deleted.** Labs come from the repos' `[[lab.sources]]`,
   exactly as on the CLI. A caller with a lab from elsewhere builds a `Lab` and
   passes the object, which is used as given.
3. **Logging is opt-in.** `open_context` has no logging side effects. The
   repo-aware opt-in merges `[logging.levels]` (refusing a conflict) and
   attaches the `HostFilter`.
4. **`otto.logger` stays a leaf.** The repo-aware opt-in is
   `otto.session.install_logging`; `otto.logger.install` stays the raw,
   dependency-free primitive (as `set_context` sits beside `open_context`).
5. **The shared code is a new module, `otto.session`.** `otto.config` does not
   grow more upward edges, and `otto.context` does not grow edges to labs,
   docker and logging.
6. **Reservations are a separate item (#588, P0, Ready):** one
   `otto.reservations` report that `otto reservation check` renders and the
   gate reads, and the decision whether `open_context` applies the gate.
7. **`--log-level` completes**, offering lowercase as well as uppercase
   (people type lowercase). Its own commit; unrelated to #508.

## 3. Shape

### 3.1 `otto.session`

A new library module (`src/otto/session.py`, lazy exports from `otto` where the
public API needs them). It decides; it never prints, never exits, and every
refusal is a typed `OttoError` whose message carries no rich markup and no CLI
flag spelling.

```python
def select_projects(
    repos: list[Repo], include: list[str], exclude: list[str]
) -> ProjectSelection: ...
```
The `-I`/`-E` rules: a name that is no discovered repo's (normalised) name is
refused with a did-you-mean; a name in both lists is refused. Returns the
normalised selection the context carries.

```python
def check_repos(
    result: BootstrapResult, labs: list[str], selection: ProjectSelection
) -> RepoCheck: ...  # RepoCheck(demoted: list[BootstrapError])
```
The bootstrap gate, before the lab exists (`otto.config.scope.inactive_before_lab`
is the authority, as today). An error whose repo is active, an error forced by
`include`, or an error with no repo (an unparsable `settings.toml`) is fatal:
raises `RepoLoadError(errors=[...])` carrying every fatal error. Errors of
repos inactive before the lab come back as `demoted`. Bootstrap warnings never
gate. Lab-free CLI leaves run this with the labs they were given (possibly
none), as today.

```python
def build_lab(repos: list[Repo], labs: list[str]) -> Lab: ...
```
`[host_preferences]` merged in `OTTO_SUT_DIRS` order (lists atomic, last wins;
tables merged per key), the composite of the repos' `[[lab.sources]]`, the
process inventory, `load_lab(labs, preferences=, repository=, inventory=)`, then
the declared container placeholder hosts. No labs selected, an unknown lab, a
bad source or unknown backend, and a broken inventory declaration each raise
`LabBuildError`. `otto.inventory` stays a function-local import (import
budget).

```python
def check_dependencies(ctx: OttoContext) -> list[str]: ...
```
The dependency preflight, AFTER the context is installed: activation is decided
by `otto.config.scope.active(owner, ctx)`, which needs the lab's scope
verdicts (the CLI runs it after the session for the same reason; the pre-lab
projection would refuse a host-starved repo). An active repo with an unmet
Python requirement raises `DependencyRefusedError`; otherwise returns the
dependency warnings.

```python
def install_logging(
    *, log_level: str = "INFO", output_dir: Path | None = None,
    overrides: dict[str, str] | None = None,
) -> ...: ...
```
Merges the bootstrapped repos' `[logging.levels]` (two repos naming one logger
at different levels raise `LoggingLevelsConflictError`), lets an explicit
`overrides` entry win over the repos', calls `otto.logger.install`, and
attaches the `HostFilter` to the console handler it installed. Returns what
`otto.logger.install` returns.

`merge_logging_levels`, `LoggingLevelsConflictError` and the
`[host_preferences]` merge move out of `otto.cli.invoke` into `otto.session`.

### 3.2 Errors

| Error | Base | Raised by | `field` |
|---|---|---|---|
| `ProjectSelectionError` | `FieldError` | `select_projects` | `include_projects` / `exclude_projects` |
| `RepoLoadError` | `OttoError` | `check_repos` | none; carries `errors: list[BootstrapError]` |
| `LabBuildError` | `FieldError` | `build_lab` | `labs` for no lab / an unknown lab; none otherwise |
| `DependencyRefusedError` | `OttoError` | `check_dependencies` | none; carries the unsatisfied requirements |
| `InstructionInactiveError` | `OttoError` | `otto.run_instruction` (§3.4) | none; names the owning repo and why |
| `LoggingLevelsConflictError` | `OttoError, ValueError` | `install_logging` | none (moved, unchanged) |

Messages name fields (`include_projects`, `exclude_projects`, `labs`), never
flags, so the CLI's `spell_flags` / `usage_error_from` translation spells them.
A message never quotes a user path or repo name where a whole-word field name
could appear inside it (the item 2 / #535 lesson: spelling is word-by-word).

### 3.3 `open_context`

```python
@asynccontextmanager
async def open_context(
    *, lab: Lab | str | list[str],
    include_projects: list[str] | None = None,
    exclude_projects: list[str] | None = None,
    dry_run: bool = False, log_command_output: bool = True,
) -> AsyncIterator[OttoContext]: ...
```
1. `result = bootstrap()`.
2. `selection = select_projects(result.repos, include, exclude)`.
3. `check = check_repos(result, labs, selection)`; each demoted error is logged
   as a `logging` warning on otto's logger (otto installs no handler for it; it
   reaches the caller's logging or `install_logging`'s console).
4. A `Lab` object is used as given; a name or list of names goes through
   `build_lab(result.repos, labs)`. (`labs` for steps 3–4 is the given names, or
   the object's `component_names`.)
5. `OttoContext(lab, dry_run, log_command_output, include_projects=...,
   exclude_projects=...)`, installed with `set_context`.
6. `check_dependencies(ctx)`; its warnings are logged as warnings. On
   `DependencyRefusedError` the context is reset before the error propagates.
7. Yield; on exit, the existing sweep and reset.

`search_paths` is gone. The reservation gate is still not applied (#588).

### 3.4 `otto.run_instruction` refuses an inactive instruction

`otto.instructions.run_instruction(ctx, name, ...)` refuses a repo-owned
instruction whose owner is not `active(owner, ctx)`, raising
`InstructionInactiveError` with the same facts the CLI prints today (switched
off by `exclude_projects`, or out of the labs' project scope, and what would
activate it, in field names). First-party and hand-registered instructions are
never refused. The CLI's `refuse_inactive_instruction` keeps its position in the
preamble (after the session, before the body) but decides through the same
library predicate and renders the library's facts.

### 3.5 The CLI preamble

The preamble calls the same functions in the same order and keeps only the
rendering:

- root callback / `command_preamble`: `select_projects` (a `ProjectSelectionError`
  goes through `usage_error_from`: `include_projects` → `-I`, exit 2);
  `check_repos` (a `RepoLoadError` renders today's frame, "Cannot run commands
  while a repo fails to load" plus each error, exit 1; each demoted error is
  today's stderr `warning:` line).
- `ensure_cli_session`: `install_logging(...)` replaces the inline levels merge
  and the `HostFilter` attach; the CLI-only parts (`init_cli_logging`'s file
  sinks, the dry-run notice) stay.
- `ensure_lab_context` and `ensure_inline_lab`: `build_lab` (a `LabBuildError`
  with `field="labs"` goes through `usage_error_from` → `--lab`; others render
  today's lab-error frame, escaped, with today's exit codes). The reservation
  gate build stays here.
- after the session: `check_dependencies` (warnings printed as today's stderr
  `warning:` lines; `DependencyRefusedError` renders today's refusal, exit 1),
  then the inactive-instruction refusal through §3.4's predicate.
- `cli/remote_completion.py` builds its lab with `build_lab`.

Every exit code and every stderr/stdout line the CLI prints today stays the
same; only where the decision is made moves. `build_lab_from_repos`,
`merge_logging_levels`, `LoggingLevelsConflictError` and `LabContextError` leave
`otto.cli.invoke` (the latter replaced by the library errors; the CLI's
rendering helper stays).

### 3.6 `--log-level` completion

- The option's vocabulary is the level names `[logging.levels]` accepts: the
  stdlib names plus `otto.logger.levels.LEVEL_ALIASES` (`DEBUG INFO WARNING WARN
  ERROR CRITICAL CRIT`), from that one source of truth. An unknown level is a
  usage error (exit 2). The value is still upper-cased before use, so
  `--log-level debug` and `OTTO_LOG_LVL=debug` keep working.
- Completion matches the fragment's case: `deb<TAB>` → `debug`, `DEB<TAB>` →
  `DEBUG`, an empty fragment lists the canonical uppercase names.
- The completion shim's `static` source gains one flag for "answer in the
  fragment's case"; Typer's own completer for the option does the same, so the
  shim-vs-Typer completion differential stays exact. A warm TAB stays O(1).

### 3.7 Layering

`otto.session` is a new tach module (edited by hand, never `tach sync`) with
explicit edges to the modules it calls (`otto.bootstrap`, `otto.config`,
`otto.labs`, `otto.inventory`, `otto.docker`, `otto.logger`, `otto.host`,
`otto.context`, `otto.env`, `otto.errors`). `otto.context` gains the
`otto.session` edge (`open_context`); `otto.cli` already may import anything it
needs and gains `otto.session`; `otto.instructions` gains whatever §3.4's
predicate needs. Every heavy import stays function-local: a `Lab`-object caller
of `open_context` still never imports `otto.inventory`.

## 4. Testing

- **Unit tests per function:** `select_projects` (unknown name + suggestion,
  overlap, normalisation), `check_repos` (every fatal / demoted case the CLI's
  `TestDemotion` pins, now against the library), `build_lab` (the cases
  `tests/unit/cli/test_lab_source_wiring.py` pins, moved), `check_dependencies`
  (active vs host-starved vs excluded), `install_logging` (merge, conflict,
  override precedence, filter attached to the console only), the
  `run_instruction` refusal.
- **`open_context`:** the lab equals the CLI's for a repo with `[[lab.sources]]`;
  each refusal raises; a demoted error is logged, not raised; a `Lab` object
  still never imports `otto.inventory`; the context is reset when
  `check_dependencies` refuses.
- **The differential** (`tests/unit/cli/test_session_differential.py`), in the
  series' shape:
  1. lab parity: for a matrix of repo layouts (several `[[lab.sources]]`, a
     later repo overriding an earlier one, `[host_preferences]` in two repos,
     declared containers, inventory references) `otto --lab X` and
     `open_context(lab="X")` build the same lab (host ids, merged preferences,
     placeholders);
  2. refusal parity: for each refusal (broken init in an active / excluded /
     out-of-scope / `-I` repo, unparsable settings, unmet dependency, unknown
     `-I` name, missing / unknown lab) the library raises the typed error and
     the CLI exits with today's code printing the library's message in flag
     spelling;
  3. demotion parity: the library's demoted list matches the CLI's `warning:`
     lines.
  The module docstring records the mutation that proved it red.
- **`--log-level`:** the vocabulary refusal, the case-matching completion in the
  shim and in Typer, and the completion differential.
- **Sweeps:** every test tree (unit, integration, e2e, conformance) for
  `search_paths=` and for the moved CLI names.
- **Gates:** per task, the touched tests (CLI files also under
  `GITHUB_ACTIONS=true TERM=dumb FORCE_COLOR=`) and ruff; once at the end,
  `make typecheck`, `make lint-arch`, `make docs`, `make check-api-snapshot`,
  `nox -s tests_hostless-3.14`, `make coverage` (bed coordinated) and
  `make gate-fresh`.

## 5. Documentation

- `docs/cookbook/python-library.md`: the `open_context` section and parameter
  table (no `search_paths`; `include_projects` / `exclude_projects`; what it
  refuses); the logging section points at `otto.session.install_logging`; the
  lower-level recipe uses `otto.session.build_lab`; the `bootstrap()` →
  `run_tests` section says which checks a script without `open_context` skips.
- `docs/architecture/lifecycle.md` ("Library use: `open_context()`") and
  `docs/cli/env/index.md` (the "library callers are not checked" lines).
- `docs/examples/getting-started/collect_metrics.py` and `as_root.py` drop
  `search_paths`.
- API pages for `otto.session`; the API snapshot regenerated.
- `--log-level` help and the CLI reference list the levels.

## 6. BREAKING (migration recipe for the squash message)

- `open_context(search_paths=...)` is gone: labs come from the repos'
  `[[lab.sources]]`; pass a `Lab` object for a lab from elsewhere.
- `open_context` builds the CLI's lab (sources, `[host_preferences]`, container
  placeholders) and refuses where the CLI refuses: `RepoLoadError`,
  `ProjectSelectionError`, `LabBuildError`, `DependencyRefusedError`. It gains
  `include_projects` / `exclude_projects`.
- `otto.run_instruction` raises `InstructionInactiveError` for an instruction
  whose repo is inactive.
- `otto.cli.invoke.build_lab_from_repos`, `merge_logging_levels`,
  `LoggingLevelsConflictError` and `LabContextError` are gone: use
  `otto.session.build_lab`, `otto.session.install_logging` and the
  `otto.session` errors.
- `--log-level` accepts only the known level names (any case); anything else
  exits 2.

## 7. Out of scope

- The reservation report and whether `open_context` applies the gate (#588).
- Options metadata leaving typer (item 8, #513).
- The output directory and the dry-run seam (CLI concerns).
