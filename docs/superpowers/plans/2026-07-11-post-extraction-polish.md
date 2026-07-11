# Post-Extraction Polish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Land the 13 follow-ups Chris triaged from the library-extraction branch: two window-priced API-shape fixes, the WARN/CRIT log-alignment feature, coverage/suite API polish, hardening tests, and small fixes.

**Architecture:** Small independent tasks on `worktree-post-extraction-polish` (based off `worktree-library-extraction` tip `690c53e`; rebase onto main after Chris's squash-merge). Same conventions as the parent branch.

**Tech Stack:** Python 3.10+, pytest, pydantic, Typer (CLI layer only).

## Global Constraints

- Clean break — no aliases/shims for renamed API (`ReservationGateOutcome` ceases to exist).
- No bare tuples in interfaces; sequence fields `list[X]`.
- Library raises library exceptions; CLI adapters own typer vocabulary. CLI output byte-identical except where a task explicitly changes it (T2 level column).
- Gates per task, FOREGROUND only: **do NOT run `make coverage` — known performance issue right now (Chris, 2026-07-11)**. Instead: targeted `uv run pytest <task's covering test paths> -n auto` (single pass), plus `uv run nox -s lint typecheck`; `make docs` when docs/docstrings-rendered-by-Sphinx change. Full-suite + coverage verification is DEFERRED to branch end, gated on Chris clearing the perf issue.
- Commits: conventional prefix + `Assisted-by: Claude (Fable 5)` trailer, verified via `git log -1`.
- Rename sweeps verified with case-insensitive greps (excluding CHANGELOG.md, docs/superpowers/, web/node_modules).

---

### Task 1: Window renames — `ReservationGateResult` + `SelectionMatch`

**Files:** Modify: `src/otto/reservations/check.py`, `src/otto/reservations/__init__.py`, `src/otto/cli/invoke.py` (docstring refs), `src/otto/examples/reservations_cli.py`, `docs/guide/reservations.md`, `docs/api/reservations.rst` (if it names the class), `src/otto/suite/selection.py`, `src/otto/suite/run.py` (run_selection loop), tests (`tests/unit/reservations/test_gate.py`, `tests/unit/cli/test_preamble_reservation_gate.py`, `tests/unit/suite/test_run_api.py`, `tests/unit/cli/test_selection_resolve.py`, `tests/unit/examples/`).

**Interfaces — Produces:** `ReservationGateResult` (former `ReservationGateOutcome`, fields unchanged: checked/skipped/warning); `SelectionMatch(repo: Repo, targets: list[str])` frozen dataclass; `resolve_selection(...) -> list[SelectionMatch]`.

- [ ] Sweep `ReservationGateOutcome` → `ReservationGateResult` (case-insensitive verify → zero hits).
- [ ] Add `SelectionMatch` to `selection.py`; `resolve_selection` returns `list[SelectionMatch]`; update `run_selection`'s per-repo loop (`for match in per_repo: match.repo / match.targets`) and the `-m`-alone branch to build `SelectionMatch` too; update tests to attribute access.
- [ ] Gates green. Commit: `refactor(api)!: ReservationGateOutcome → ReservationGateResult; SelectionMatch record`

### Task 2: Logger — wire WARN/CRIT fixed-width levels + name unification

**Files:** Modify: `src/otto/logger/__init__.py` (eager `from . import levels` — stdlib-only side-effect module, safe pre-lazy-table), `src/otto/logger/levels.py` (docstring: purpose = 5-char aligned level column), `src/otto/logger/formatters.py:81` (`_default_log_format = "{asctime} [{levelname:^7}] {message}"` → width-5 scheme: `{levelname:<5}`), any other `levelname` format sites (grep `levelname` in src/otto). Sweep ~10 remaining emitter sites using `logging.getLogger("otto")` directly → `logging.getLogger(__name__)` (`host/host.py`, `suite/suite.py`, `config/lab.py`, `monitor/{db,collector,snmp}.py`, `cli/invoke.py:256,380` — EMITTERS only; sites that attach handlers/configure the `'otto'` logger in `logger/management.py` and tests that save/restore `'otto'` state STAY).
**Test:** `tests/unit/logger/test_levels.py` (new): after `import otto.logger`, `logging.getLevelName(logging.WARNING) == "WARN"`, `logging.getLevelName(logging.CRITICAL) == "CRIT"`; format each of DEBUG/INFO/WARN/ERROR/CRIT through the default file formatter and assert the message column index is identical across all five (all level names ≤5 chars).

- [ ] TDD the levels test (RED: getLevelName returns "WARNING") → wire → GREEN.
- [ ] Verify the three-sink integration tests: log-file content changes (WARNING→WARN etc. + column shift) — update ONLY assertions that pin the old literal level strings; flag any other fallout in the report.
- [ ] Unification sweep (behavior-neutral; no formatter renders `%(name)s`).
- [ ] Gates green. Commit: `feat(logger): fixed-width WARN/CRIT level column; unify emitters on getLogger(__name__)`

### Task 3: Coverage API polish — named exceptions, overwrite_cov_dir, tier dedup

**Files:** Modify: `src/otto/coverage/errors.py` (add `CoverageConfigError(ValueError)`, `NoCoverageDataError(ValueError)` with docstrings), `src/otto/coverage/collect.py` (raise them at the no-config and no-host-data sites — message strings UNCHANGED; `tier` param widened to `str | TierConfig | None`, using a passed TierConfig directly), `src/otto/coverage/__init__.py` (export both errors), `src/otto/cli/cov.py` (`_do_get` passes its already-resolved `TierConfig` object — removes the double resolve; its `except ValueError` arm still catches the subclasses → CLI parity automatic), `src/otto/suite/run.py` (`run_suite` pre-phase: `if opts.cov and opts.cov_dir: prepare_empty_dir(opts.cov_dir, overwrite=opts.overwrite_cov_dir, flag_name="cov_dir")` inside the swallow-adjacent placement — CLI path no-ops because the callback already cleared the dir; add `overwrite_cov_dir: bool = False` to `RunOptions`), `docs/guide/library-usage.md` (exceptions table + overwrite_cov_dir sentence).
**Test:** `tests/unit/cov/test_collect.py` — `pytest.raises(CoverageConfigError)` / `(NoCoverageDataError)` at the two sites (and both `isinstance(..., ValueError)`); TierConfig-passthrough resolves once (mock `resolve_get_tier`, assert single call across a full `cov get`-shaped flow); `tests/unit/suite/test_run_api.py` — non-empty cov_dir without overwrite raises ValueError; with `overwrite_cov_dir=True` proceeds.

- [ ] TDD each addition; existing cov-get failure-message tests pass UNMODIFIED.
- [ ] Gates green (+docs). Commit: `feat(cov): named CoverageConfigError/NoCoverageDataError; overwrite_cov_dir; single tier resolve`

### Task 4: Suite polish — re-exports, breadcrumb, `_session_context` hardening

**Files:** Modify: `src/otto/suite/__init__.py` (re-export `run_selection`, `NoTestsMatchedError`, `UnknownSelectionError`), `src/otto/config/lab.py` or wherever the unknown-host lookup error is raised (grep `get_host` raise site): when `lab.name == "<library>"` append ` — no lab is loaded; run inside \`async with otto.open_context(lab=...)\``, `src/otto/suite/run.py` `_session_context` (save `prior = active.output_dir` and restore that, not literal None).
**Test:** `tests/unit/suite/test_run_api.py` — exception-during-session restore (patch `_run_pytest_session` to raise; assert `try_get_context()` is None / output_dir restored after); `run_selection` no-context install/restore test; breadcrumb asserted in the raised message when using the minimal context; import test for the three re-exports.

- [ ] TDD → implement → gates green. Commit: `fix(suite): export selection API from otto.suite; open_context breadcrumb; session-context hardening`

### Task 5: Rich-markup fix + prose sweep

**Files:** Modify: markup-eaten log sites — start at `src/otto/coverage/collect.py` (the `"No [coverage] section found"` warning) and `src/otto/cli/cov.py`; then AUDIT: `grep -rnE "logger\.(debug|info|warning|error|critical)\(.*\[[a-z_]+\]" src/otto --include="*.py"` — escape literal brackets as `\[` wherever the bracket is data, not intended markup (intended markup like `[magenta][DRY RUN]` stays). Prose sweep: camelCase `ConfigModule`/`configModule` remnants (`src/otto/config/completion_cache.py:812,859`; `tests/e2e/cov/test_embedded_coverage_e2e.py:66`; `tests/integration/host/conftest.py:57,61`; `tests/unit/config/test_repo_git_subprocess_leak.py:80`), stale `_resolve_selection` comment (`tests/e2e/test_selection_runs.py:120`), `todo/fable_review_outcome.md:122` + `todo/ty_vs_pylance_eval.md` dead `otto/storage` links, `src/otto/coverage/config.py` module docstring ("three functions" → current truth), `src/otto/cli/test.py` stale "cached after the above" comment, `tests/integration/host/test_session_stability_integration.py:188` ruler repad, `store_dir.py` `captured_at` local → `captured_at_slug`, `store/model.py` `found_label` wrong-typed-format label.
**Test:** rendered-output test proving a console-handler ERROR containing `[coverage]` displays the literal text; one assertion pinning `_run_tooltip`'s `commit <sha12>` prefix.

- [ ] TDD the markup test (RED shows the eaten bracket) → escape → GREEN; sweep; gates green.
- [ ] Commit: `fix(logging): escape literal brackets in log messages; prose/comment sweep`

### Task 6: bootstrap→run_suite e2e

**Files:** Create: `tests/e2e/suite/test_library_run_e2e.py` — subprocess test (marker `hostless`, follow `tests/e2e/_otto_subprocess.py` env conventions but drive `python -c`/a script instead of the otto binary): env `OTTO_SUT_DIRS=tests/repo_e2e`, script does `bootstrap(); cls = find_suite("TestE2EFixture"); r = run_suite(cls, options=cls.Options(), output_dir=<tmp>)`; assert exit 0, printed `r.passed is True`, junit exists, and `try_get_context() is None` after. Second case: `OTTO_E2E_FAIL=1` → `r.passed is False`, `exit_code == 1`.

- [ ] TDD (the test IS the drive); gates green. Commit: `test(suite): e2e for the bootstrap→run_suite library path`

---

## Self-check before finish
Case-insensitive grep zero-hits for `ReservationGateOutcome`; `make import-snapshot` reviewed (T2's eager `levels` import adds `otto.logger.levels` to surfaces that import otto.logger — expected +1, stdlib-only); full gate + `make docs` at branch end; final review per SDD.
