# Library extraction + breaking renames — design

**Date:** 2026-07-10
**Status:** approved design, pending implementation plan
**Branch plan:** one worktree branch off main (`b80e236`), four phases, one squash-merge.
**Origin:** whole-repo structure review (2026-07-10); this is item 1 (extract CLI-trapped
workflows into core) plus the selected breaking renames and the reservations
library-first aspect.

## Goals

1. Every core workflow (suite run, coverage collection, reservation gating) is callable
   from plain Python with no Typer/CLI machinery; the CLI is a thin consumer.
2. `otto.reservations` becomes a self-contained library so otto users can build their
   own CLI apps on the same reservation plumbing.
3. Apply the agreed breaking renames while the breaking window is open.
4. Measurable import hygiene: the import-budget module counts must not regress, and the
   reservations/logger entry points must drop.

## Global principles

- **Clean break.** No backwards compatibility anywhere in this workstream: no aliases,
  no deprecation shims, no old-format readers. Renamed APIs cease to exist under old
  names. Versioned artifacts (coverage store JSON, capture.json) fail loud on old
  formats with a clear "re-capture/regenerate" message. No silent migration.
- **CLI behavior is otherwise byte-identical.** Same flags (minus renames), same exit
  codes, same messages, same swallow-vs-fail policies. Extraction is a relocation of
  logic, not a behavior change.
- **Docs ride each phase** and are written once, with final names.
- Each rename and each extraction is its own conventional commit
  (`refactor(rename)!:` / `refactor(suite)!:` etc.) so review and bisect stay sane.

## Phase 0 — breaking renames

| Current | New | Notes |
| --- | --- | --- |
| `Host.oneshot()` | `Host.exec()` | Watch ruff flake8-builtins (A003) — prefer a narrow per-site `noqa` on the definition over a deny-list entry. |
| `Host.interact()` + `@cli_exposed(name="login")` | `Host.login()` | Method name matches CLI verb; the name override disappears. |
| `otto/configmodule/` | `otto/config/` | Inner `configmodule.py` → `config/fleet.py` (fleet dispatch: `all_hosts`/`get_host`/`run_on_all_hosts`). All import sites, docs, examples updated. |
| `otto/storage/` | `otto/labs/` | Truthful name: read-only lab repositories/backends. `storage/factory.py` (host construction) moves to `otto/host/factory.py` in the same commit; callers that bypassed `LabRepository` to reach it (`cli/tunnel.py`, `cli/init.py`, `completion_cache.py`) update. |
| `otto/coverage/correlator/` | `otto/coverage/merge/` | lcov merge + `.info` load + path remap. |
| coverage `context` | `run` | `ContextRecord` → `RunRecord`, `context_hits` → `run_hits`, store JSON keys `contexts` → `runs`, renderer/UI labels. Store format version bump; old files fail loud. Kills the context/coverage.py-context/OttoContext triple overload. |
| capture `pin` | `base_commit` | capture.json format bump. Committed manual captures under `.otto/coverage/manual/` in user repos fail loud with a re-capture instruction (clean break — no reader shim). |
| "stamp a capture" wording | "tag" / "annotate" | `stamp` stays reserved exclusively for the gcov `.gcno`/`.gcda` header stamp (produce.py, cov.py wording + any function names). |

Explicitly **kept** (decided, do not revisit during implementation): `term` (evaluated
`protocol` — collides with `Tunnel.protocol`; `terminal` — cosmetic), `element`/`board`/
`slot` (deferred to a future window), `repair` (pairs with `impair`), `docker/` (the
remote-daemon redesign will make the name accurate again), `tier`.

Rename mechanics: whole-tree grep sweeps (src, tests, docs, fixtures, examples, web
where applicable), `make schema` regeneration, completion-cache `SCHEMA` version bump
where cached names change.

## Phase A — `otto.suite` run API

New module `otto/suite/run.py`, re-exported from `otto.suite`:

```python
@dataclass(frozen=True)
class RunOptions:            # today's cli/test.py TestRunOptions, moved verbatim
    markers: str = ""
    tests: str = ""
    iterations: int = 0
    duration: int = 0
    threshold: float = 100.0
    results: str = ""
    cov: bool = False
    cov_dir: Path | None = None
    cov_clean: bool = True
    cov_report: bool = False
    cov_report_dir: Path | None = None
    overwrite_cov_report_dir: bool = False
    project_name: str = "Coverage Report"
    monitor: bool = False
    monitor_interval: float = 5.0
    monitor_output: Path | None = None
    monitor_hosts: str | None = None

@dataclass(frozen=True)
class SuiteRunResult:
    exit_code: int                    # pytest rc; NO_TESTS_COLLECTED(5) stays a failure
    junit_paths: list[Path]           # selection runs fan out one per repo
    stability_report: Path | None
    stability_unstable: bool
    output_dir: Path
    # .passed property: exit_code == 0

def run_suite(suite: type[OttoSuite], *, options: object | None = None,
              run_options: RunOptions = RunOptions(),
              output_dir: Path | None = None) -> SuiteRunResult: ...
def run_selection(*, run_options: RunOptions,
                  output_dir: Path | None = None) -> SuiteRunResult: ...
def find_suite(name: str) -> type[OttoSuite]: ...   # SUITES lookup; error lists registered names
```

Decisions:

- **Sync entry points, async featureset untouched.** `run_suite` is already sync today;
  `pytest.main` cannot run inside a live event loop (pytest constraint, not otto's).
  User suites' async tests/fixtures still run under pytest-asyncio `asyncio_mode=auto`;
  the `OttoPlugin` monitor lifecycle is unchanged. Async callers use
  `await asyncio.to_thread(run_suite, ...)` (documented).
- **`output_dir` precedence:** explicit param → `get_context().output_dir` if set → CWD
  (consistent with the `--xdir`-defaults-to-CWD philosophy). The hard dependency on CLI
  logging setup (`create_output_dir`) is gone.
- **Suite file derived from the class** via `inspect.getfile`; the `suite_file` string
  parameter disappears. Selection resolution (`_resolve_selection`, `resolve_suite`,
  marker matching) moves into `otto/suite/` as core logic. The `--tests` tab-completer
  stays in `cli/`.
- **The buried `SystemExit(1)` in `_print_stability_report` is removed.** The stability
  report becomes data: the library writes the report file and reflects threshold
  violations in `exit_code`/`stability_unstable`.
- **Errors:** the library raises (`ValueError` for "no tests matched", etc.) and never
  raises `typer.Exit`. `cli/test.py`'s callback still builds `RunOptions` into
  `ctx.meta` (the key stays CLI-internal); the runner translates `SuiteRunResult` into
  `typer.Exit(rc)` and renders messages. `suite/register.py`'s lazy import of
  `cli.test.run_suite` becomes a core-to-core import (layering violation removed).
- `run_suite`/`run_selection` keep the composed workflow for CLI parity: pre-run gcda
  clean, pytest session, post-run coverage collection + optional report — the coverage
  steps call the Phase B API with the existing "never fail a successful test run"
  swallow policy.

## Phase B — `otto.coverage` collection API

New `otto/coverage/collect.py` and `otto/coverage/config.py`:

```python
# config.py — today's cli/test.py privates, moved verbatim
def get_cov_repo(repos) -> Repo | None: ...
def get_cov_config(repos) -> dict[str, Any]: ...
def has_cov_config(cfg) -> bool: ...

# collect.py
@dataclass(frozen=True)
class CollectResult:
    cov_dir: Path
    host_dirs: dict[str, Path]          # host id → collected dir
    captures_written: list[Path]        # capture.json tail output

async def clean_remote_gcda(repos: list[Repo] | None = None) -> None: ...
async def collect_coverage(
    cov_dir: Path, *,
    repos: list[Repo] | None = None,    # default get_repos()
    tier: str | None = None,            # None → resolved default e2e-kind tier
    ticket: str | None = None,
    note: str | None = None,
) -> CollectResult: ...
```

`collect_coverage` composes today's `_run_coverage` + `_write_cov_metadata` + capture
tail once. Policy split: **the library fails loud** (raises the coverage error family);
callers own the policy — `otto.suite.run_suite`'s post-run call keeps the swallow
policy (warn + leave raw artifacts), `otto cov get` propagates to a red message + exit.
`cli/cov.py:_do_get` shrinks to argument handling + one `collect_coverage` call; its
cross-CLI imports of `cli/test.py` privates disappear. `_write_cov_metadata` moves into
`otto/coverage/` as an internal of the collect module.

## Phase C — reservations library-first + logger import hygiene

- `reservations/check.py` drops `import typer` entirely.
- `ReservationState` is renamed **`ReservationGate`** and absorbs the gate behavior
  (state class and gate function merge into one concept):

```python
@dataclass(frozen=True)
class ReservationGate:
    backend: ReservationBackend | None = None
    identity: ResolvedIdentity | None = None
    skip_check: bool = False
    backend_factory: Callable[[], ReservationBackend] | None = None

    def evaluate(self) -> ReservationGateOutcome: ...
    # raises MissingReservationError / ReservationBackendError

@dataclass(frozen=True)
class ReservationGateOutcome:
    checked: bool          # check_reservations ran and passed
    skipped: bool          # skip_check path taken
    warning: str | None    # skip-warning text, for the caller to present
```

- The `ctx.meta["otto_reservation"]` read, rich printing, and exit-code mapping move to
  a small adapter in `cli/invoke.py` (next to `command_preamble`). The CLI stores a
  `ReservationGate` in `ctx.meta` and calls `evaluate()`.
- Documented public surface: `ReservationGate`, `ReservationGateOutcome`,
  `check_reservations`, `required_resources`, `build_backend`,
  `build_reservation_gate` (today's `build_reservation_state`, renamed with the class),
  `register_reservation_backend`, backends, errors, identity resolution.
- New docs guide: *Build your own CLI on otto's reservation plumbing* — worked
  third-party Typer example, paired with a copy-me sample in `otto/examples/` and
  pointing at `assert_reservation_backend_conforms`.
- **Logger hygiene (explicitly in scope):**
  - `get_logger()` is **deleted** (clean break): it is a trivial wrapper around
    `logging.getLogger("otto")`. Internal modules switch to stdlib
    `logging.getLogger(__name__)` (~30 import sites) — module-precise child loggers
    under the `otto` namespace, propagating to the `'otto'` handlers as before. No
    formatter renders `%(name)s`, so log output is unchanged. User docs document the
    stdlib convention: log under the `otto` namespace via
    `logging.getLogger("otto")` / `logging.getLogger("otto.<yours>")`.
  - The `NullHandler` attach moves from `otto/logger/__init__.py` to
    `otto/__init__.py` (the requests/urllib3 idiom; stdlib-only, runs on any
    `import otto`, no longer depends on something importing `otto.logger`).
  - `logger/__init__.py` stops eagerly importing `management` — lazy PEP-562
    re-export — so importing `otto.logger` for its remaining public names
    (`LogMode`, formatters, levels) no longer pulls rich + the global `Console`.
    `management` itself does not move. `otto.__init__`'s lazy `get_logger` export
    is removed.

## Public exports

Top-level `otto` (lazy PEP-562 table) gains only `run_suite` and `RunOptions`.
Everything else is documented at its subpackage home: `otto.suite`, `otto.coverage`,
`otto.reservations`.

## Docs

- Guide: *Running suites from Python* (script example, `output_dir` precedence, the
  sync/event-loop constraint, `asyncio.to_thread` pattern).
- Guide: *Collecting coverage from Python* (`collect_coverage` + existing
  `run_coverage_report` as one story).
- Guide: *Build your own CLI on otto's reservation plumbing* (see Phase C).
- Sphinx API reference entries for the new modules; rename sweeps update existing pages
  in the same commits.

## Verification

- Existing unit/integration/e2e suites stay green with rename-only updates.
- New unit tests: `SuiteRunResult` exit-code mapping (rc=5-is-failure, stability
  threshold), `find_suite` error listing, `collect_coverage` fail-loud vs. suite-path
  swallow, `ReservationGate.evaluate()` outcome matrix.
- Import-hygiene assertions in the import-budget guard: `otto.reservations` without
  typer in `sys.modules`; `import otto.logger` without rich.
- Full gate per phase (`make coverage`, nox lint/typecheck, docs); live-bed e2e at the
  end; a `verify` pass driving `otto test`, `otto cov get`, and the third-party
  mini-CLI example.

## Import-budget metric

`make import-snapshot` baseline on main immediately before Phase 0; re-snapshot after
each phase; final before/after table in the PR for the tracked entry points
(`import otto`, CLI startup, completion fast path, `import otto.reservations`,
`import otto.logger`). Acceptance: no entry point regresses; reservations and logger
drop measurably.

## Sequencing

All previously-pending prerequisite work is on main (link impairment landed as
`b80e236`, tree-identical to the link worktree tip; coverage run-contexts landed as
`5c9cf24`) — the branch starts from current main immediately. Phase order: 0 (renames)
→ A (suite) → B (coverage) → C (reservations + logger). Renames go first so every new
API and docs page is born with final names.
