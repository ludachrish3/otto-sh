# Logger standardization — remove the `OttoLogger` subclass — design

> Captured 2026-06-27. Resolves the duplicate-module test hazard
> ([todo/doctest-modules-duplicate-import-hazard.md](../../../todo/doctest-modules-duplicate-import-hazard.md))
> at its root *and* makes otto's logging standard / library-friendly.
>
> **Scope split:** the broader import-light / lazy `otto/__init__.py` work is
> deliberately **split into its own workstream**
> ([todo/import-light-otto-init.md](../../../todo/import-light-otto-init.md)) —
> it carries registration-ordering risk and is *not* needed for correctness once
> the subclass is gone. This spec covers only the logger refactor.

---

## 1. Context & motivation

Two goals, one root cause.

**(a) The duplicate-import hazard.** Under `pytest --cov=otto`, the `otto` package
is executed twice per worker — once early by coverage's module resolution
(`coverage/inorout.py → find_spec`, cascading through the eager
`src/otto/__init__.py` which calls `get_otto_logger()`), once by normal
conftest/collection import. Two executions produce **two distinct `OttoLogger`
class objects**; the logging-manager singleton `'otto'` is an instance of one
generation while test code imports the other. Result: `type(get_otto_logger())
is not OttoLogger`, breaking `is`/`isinstance`/module-global monkeypatching on
the singleton (this is what made the NFS breadcrumb test flaky). Confirmed by
instrumentation: one `sys.modules['otto.logger.logger']`, two class objects;
`create_output_dir.__globals__ is not logger_mod.__dict__`. Import-mode,
`--doctest-modules`, and removing the eager call individually do **not** fix it.

**(b) Library-unfriendly logging.** otto wants to be usable as a library where
consumers can configure/replace logging. A custom `Logger` *subclass* plus a
process-global singleton created eagerly at import is the opposite of standard.

**The shared root cause is that `OttoLogger` is a `logging.Logger` subclass.**
Remove the subclass and both dissolve: `logging.getLogger('otto')` always returns
the Manager's single standard logger (stable identity → double-import is
harmless, no `setLoggerClass`), and it's a plain logger a consumer can configure.

## 2. Goals / non-goals

**Goals**
- Delete the `OttoLogger` subclass. The `'otto'` root logger is a **plain
  `logging.Logger`**.
- **Library/application split** (standard Python idiom):
  - *otto-the-library* only **emits** (via `logging.getLogger('otto'…)`) and
    attaches a single `NullHandler` — it never configures handlers/levels.
  - *otto-the-CLI* is the application that **configures** handlers/formatters +
    per-run output dir + rotation, via explicit setup functions.
- Preserve the CLI invariant: when the CLI configures logging, **all `otto.*`
  logging flows through otto's handlers/formatters** (already structurally true
  via the `otto.*` hierarchy; the only non-otto grab is a deliberate uvicorn
  *filter* in `monitor/server.py`).
- **Finish the data-ownership split:** the per-run output directory moves onto
  `OttoContext` (`ctx.output_dir`); `management` owns only logging mechanics and
  *creates*/returns the dir. "Where artifacts live" = context; "how logs are
  formatted" = logger.
- Remove the now-obsolete eager `get_otto_logger()` call from `otto/__init__.py`
  (it existed only to `setLoggerClass(OttoLogger)` before submodules created
  child loggers — pointless once the subclass is gone). This is a one-line
  removal, distinct from the deferred import-light work below.

**Non-goals**
- No change to log *output* for CLI users (same Rich console + file log, same
  per-run dir, same 5s rotation budget from the NFS work).
- **Import-light / lazy (PEP 562) `otto/__init__.py`** — split into its own
  workstream ([todo/import-light-otto-init.md](../../../todo/import-light-otto-init.md)).
  This spec leaves the eager `cli`/`configmodule`/`context` imports in
  `otto/__init__.py` **as-is** (deferring them carries registration-ordering
  risk and is unnecessary for correctness here).
- Not migrating the per-run output dir onto `OttoContext` (the purest home; left
  as a possible follow-up — see §8).
- No new logging features (no JSON logs, no config files).

## 3. Design

### 3.1 Delete `OttoLogger`; `'otto'` is a plain logger

`src/otto/logger/logger.py`: remove `class OttoLogger(Logger)` and all
`setLoggerClass`/`getLoggerClass` usage. The otto-specific methods/attributes
(`create_output_dir`, `remove_old_logs`, `output_dir`, `xdir`, `keep_seconds`,
`rich_logging`, `_add_log_handlers`, `_command_to_dir_name`, the
`LOG_ROTATE_BUDGET_SECONDS` constant + `_LOG_DIR_NAME_RE`) move to
`otto.logger.management` (§3.3). `logger.py` is left holding only the emit-side
helper (§3.2) — or may be collapsed into `otto/logger/__init__.py` if it ends up
trivially small.

### 3.2 `otto.logger` — the library emit surface

- `get_otto_logger(name: str | None = None) -> logging.Logger` — returns
  `logging.getLogger('otto')` (or `logging.getLogger(f'otto.{name}')`). **Plain
  `logging.Logger`, no `setLoggerClass`.** Kept (62 call sites) to minimize churn;
  all existing `.info/.debug/.warning` calls are unchanged.
- On import of `otto.logger`, attach exactly one `logging.NullHandler()` to the
  `'otto'` logger (standard library-citizen pattern: silent unless an app
  configures handlers; suppresses "No handlers could be found"). Idempotent — do
  not add a second NullHandler if one is already present.
- **Remove** the `OttoLogger` re-export from `otto/logger/__init__.py`.
- `otto/logger/__init__.py` re-exports the public surface:
  `get_otto_logger` (emit) and, for the CLI, the `management` module.

### 3.3 `otto.logger.management` — CLI-called setup/dir/rotation

New module `src/otto/logger/management.py`. **Scope guardrail:** strictly
logging *config* + mechanics (handlers, the per-run dir's *creation*, rotation) —
not a catch-all. It is **context-free** (does not import `otto.context`): it
*creates* and *returns* the per-run dir; ownership of that path moves to
`OttoContext` (§3.4). Holds a small module-private state object for logging
**config** only (no `Logger` subclass, no `output_dir`):

```
_state: _LogConfig  # xdir, keep_seconds, rich_log_file (module-private)

init_cli_logging(xdir: Path, log_level: str, keep_days: float,
                 rich_log_file: bool = False, verbose: bool = False) -> None
    # was init_otto_logger(): sets level + attaches the Rich console handler to
    # 'otto', stores xdir/keep_seconds/rich flag in _state.
create_output_dir(command: str, subcommand: str | None = None) -> Path
    # creates xdir/<command>/<timestamp[_sub]>, triggers rotation + wires the
    # file handler under it, and RETURNS the path (caller sets ctx.output_dir).
remove_old_logs(seconds: float, *, time_budget: float = LOG_ROTATE_BUDGET_SECONDS) -> None
    # the time-boxed rotation from the NFS work, preserved verbatim.
reset() -> None                        # test helper: clears _state + otto handlers
```

These functions are **called only by `otto/cli/*.py`** (and tests). `management`
lives in the low `otto.logger` layer (not `otto.cli`) so it sits below the CLI;
keeping it context-free means it doesn't even need `otto.context`.

### 3.4 Per-run output dir → `OttoContext`

To finish the data-ownership split — *logging mechanics* in `management`, *where
this run's artifacts live* in the runtime context — the per-run output directory
becomes a field on `OttoContext` (`src/otto/context.py`):

```python
@dataclass
class OttoContext:
    lab: "Lab"
    dry_run: bool = False
    log_command_output: bool = True
    output_dir: "Path | None" = None      # NEW: this run's artifact dir
    scope: HostScope = field(default_factory=HostScope)
```

Flow: each CLI command callback does
`get_context().output_dir = management.create_output_dir(command, sub)`.
`management` stays context-free (it just makes the dir + returns it); the CLI
(which owns the context lifecycle) records it. Consumers read `ctx.output_dir`
via the existing `get_context()` / `try_get_context()` accessors — no new
dependency edges, no cycle (`otto.context` does not import `otto.logger`).
Library mode without a CLI leaves `output_dir = None` (consumers already handle
the no-dir case — `host/interact.py` does today).

### 3.5 Call-site migration map

| Today | After |
|---|---|
| `init_otto_logger(...)` (`cli/main.py:278`, test fixtures, `tests/conftest.py`) | `management.init_cli_logging(...)` |
| `logger.create_output_dir(c, s)` (7 files in `cli/*.py`) | `get_context().output_dir = management.create_output_dir(c, s)` |
| `logger.output_dir` reads (`cli/test.py:219`, `suite/suite.py:163/188/193`) | `get_context().output_dir` |
| `logger.output_dir` w/ graceful-None (`host/interact.py:181`) | `try_get_context()` then `.output_dir` (None when no context) |
| `logger.xdir` (reads) | internal to `management` only — no external consumers, no migration |
| `logger.remove_old_logs(...)` (internal + tests) | `management.remove_old_logs(...)` |
| `logger.keep_seconds` / `logger.rich_logging` | `init_cli_logging(...)` params / `_state` |
| `from otto.logger.logger import OttoLogger`; `logger: OttoLogger = getLogger('otto')` (`suite.py:12,21`) | drop import; `logger = logging.getLogger('otto')` |
| `getLogger('otto')` emit grabs (`host/host.py:45`, `collector.py:55`, `snmp.py:39`) | unchanged (plain logger; no subclass needed) |
| `get_otto_logger().info(...)` etc. (62 sites) | unchanged (plain logger has these) |

Tests: `tests/unit/cli/conftest.py` patches move from
`otto.logger.logger.OttoLogger.create_output_dir`/`.remove_old_logs` to
`otto.logger.management.create_output_dir`/`.remove_old_logs`;
`tests/conftest.py` singleton-leak fixture (line ~420) calls `management.reset()`
instead of poking the `OttoLogger` singleton; `tests/unit/logger/test_logger.py`
(incl. the NFS rotation/`time_budget` tests) migrates to `management.*`.

### 3.6 `otto/__init__.py` — minimal touch only

This spec makes **one** change to `otto/__init__.py`: delete the eager
`get_otto_logger()` **call** on line 4 (and the obsolete "initialized correctly
before anything else" comment on line 1). **Keep** the `from otto.logger import
get_otto_logger as get_otto_logger` re-export (line 2) — it's part of the public
surface. The call only existed to `setLoggerClass(OttoLogger)` before submodules
created child loggers; with the subclass deleted it does nothing useful, and
`getLogger('otto')` returns the plain logger regardless.

The eager `from otto.cli import app`, `from .configmodule import …`, and
`from .context import …` imports stay **exactly as they are** — converting those
to PEP 562 lazy exports is the separate import-light workstream (§8), which
carries the registration-ordering risk and is unnecessary for the hazard fix.

## 4. Behavior: two modes

| | Library mode (`import otto`, embed) | CLI mode (`otto …`) |
|---|---|---|
| Handlers on `'otto'` | only `NullHandler` (otto stays silent unless the app configures) | Rich console + file (via `init_cli_logging` / `create_output_dir`) |
| Who configures | the embedding application (swap handlers/levels/formatters freely) | otto's CLI |
| `otto.*` logs | flow to whatever the app configured | flow through otto's handlers (the invariant) |

## 5. Why this kills the hazard

No custom `Logger` class ⇒ nothing whose *identity* can differ across a
double-import. `logging.getLogger('otto')` returns the Manager's single standard
`Logger` regardless of how many times `logger.py`'s body executes; handlers
attached to it persist. Tests that compare class identity or monkeypatch a
module global the singleton reads are no longer order/coverage-sensitive. This
holds **even with the eager `otto/__init__` imports left in place** — the fix
does not depend on the (separately-scoped) import-light work.

## 6. Testing

- **Guard test** (locks the fix): after `management.init_cli_logging(...)`, assert
  the `'otto'` logger has otto's handlers wired and is a plain `logging.Logger`
  (`type(logging.getLogger('otto')) is logging.Logger`). A second test asserts
  that importing `otto.logger` *without* `init_cli_logging` leaves only a
  `NullHandler` (library mode) and emits nothing to the console.
- **Re-add the duplicate-detection probe** as a permanent regression test: under
  the real `--cov`/xdist config, assert `getLogger('otto')` identity is stable
  across modules (the scenario that was flaky before).
- **Migrate** the existing logger tests (including the NFS `time_budget`/rotation
  tests) to `management.*`; behavior assertions unchanged.
- **`ctx.output_dir` wiring:** assert `create_output_dir()` returns a real dir and
  that a CLI command sets `get_context().output_dir` to it; assert consumers
  (`suite` dirs, `host/interact._session_log_path`) read it from the context, and
  that `host/interact` returns `None` cleanly when no context / no dir.
- All unit-tier, deterministic, `tmp_path`, no VMs.

## 7. Risks & verification

- **Call-site migration correctness** (the main risk here). ~a couple dozen real
  sites move from `logger.<method>`/`logger.output_dir` to `management.*` (§3.4),
  plus test patch-target updates. Mechanical but wide; the full unit suite +
  behavior assertions guard it.
- **Import cycles.** `management` lives in `otto.logger` (low layer) and stays
  **context-free**; consumers read `ctx.output_dir` via `get_context()`. Confirm
  no new cycle (`otto.context` does not import `otto.logger`, verified).
- **Context availability for `output_dir`.** Consumers now read `ctx.output_dir`,
  so an `OttoContext` must be active when they run. The CLI sets it per command;
  `host/interact.py` already tolerates "no dir" (→ `try_get_context()`). Verify
  `suite.py`'s `setupClass`/`teardownClass` classmethods run under an active
  context (they execute within a CLI-driven run) and that tests reading
  `ctx.output_dir` establish a context (the `tests/conftest.py` context fixture).
- **Eager `otto/__init__` left intact** — because this spec does **not** defer the
  `cli`/`configmodule`/`context` imports, the registration-ordering risk does not
  apply here; it moves with the separate import-light workstream.
- **No behavior change for CLI users** — same console/file output, per-run dir,
  rotation budget.
- Full gate: `make coverage`, `make typecheck` (ty), `make docs`; smoke
  `otto --help` + `python -c "from otto import all_hosts, app"`. Live
  `make coverage`/`make nox` = Chris.

## 8. Out of scope / future (separate workstreams)

- **Import-light / lazy (PEP 562) `otto/__init__.py`**
  ([todo/import-light-otto-init.md](../../../todo/import-light-otto-init.md)).
  Defer the eager `cli`/`configmodule`/`context` imports so `import otto` is
  side-effect-free (startup-cost win the fable review flagged). Split out here
  because of registration-ordering risk; unnecessary for the hazard fix. Should
  be sequenced **after** this refactor lands.
- Capturing third-party (non-`otto.*`) logs through otto's handlers in CLI mode
  (would mean configuring the root logger) — not requested; deferred.
