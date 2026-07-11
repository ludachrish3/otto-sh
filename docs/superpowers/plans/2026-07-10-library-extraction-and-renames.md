# Library Extraction + Breaking Renames Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make suite-run, coverage-collection, and reservation gating callable as plain Python (CLI becomes a thin consumer), apply the agreed breaking renames, and measurably improve import hygiene.

**Architecture:** Four phases on one worktree branch: Phase 0 mechanical renames → Phase A `otto.suite` run API → Phase B `otto.coverage` collection API → Phase C reservations library-first + logger hygiene. Spec: `docs/superpowers/specs/2026-07-10-library-extraction-and-renames-design.md`.

**Tech Stack:** Python 3.10+, Typer CLI, pytest/pytest-asyncio, pydantic, uv, nox.

## Global Constraints

- **Clean break:** no aliases, no deprecation shims, no old-format readers. Old names cease to exist; versioned files fail loud with a "re-capture/regenerate" message.
- **CLI behavior otherwise byte-identical:** same flags, exit codes, messages, swallow-vs-fail policies.
- **No bare tuples in interfaces:** callables return dataclasses; sequence fields are `list[X]`, never `tuple[X, ...]`.
- **Worktree setup:** create via superpowers:using-git-worktrees (branch `worktree-library-extraction` off main). Then run `uv sync` (required for ty/docs gates; does not dirty uv.lock). `make coverage` self-heals the web dist.
- **Commits:** conventional prefix, `-m` message, end body with `Assisted-by: Claude (Fable 5)`. Verify with `git log -1` after each commit (prepare-commit-msg hook needs /dev/tty and may interfere).
- **Gates:** per-task `make coverage`; per-phase full gate `make coverage && uv run nox -s lint typecheck && make docs`. `ty` runs ONLY at nox typecheck — always run it after src edits. `nox -s lint` = `ruff check` + `ruff format --check`.
- **Never** `git add -u` / `git add .`; add exact paths. No heavy parallel test loops on the dev VM (single `-n auto` passes only).
- **Rename sweeps must cover:** `src/`, `tests/`, `docs/`, `web/src/` (where noted), `src/otto/examples/`, in-tree fixture repos (`tests/repo*`, `tests/_fixtures/`). Verify each sweep with a final `grep -rn` count of 0 (excluding CHANGELOG.md and this plan/spec).

---

## Phase 0 — Breaking renames

### Task 1: Worktree, baseline metrics

**Files:**
- Create: none (setup only)

**Interfaces:**
- Produces: branch `worktree-library-extraction`; baseline snapshot copy at `/tmp/claude-1000/import-budget-baseline/`.

- [ ] **Step 1: Create worktree branch off current main** (superpowers:using-git-worktrees). Confirm: `git log -1 --oneline` shows main tip (`b80e236` or later).
- [ ] **Step 2: `uv sync`** in the worktree. Expected: exits 0, `.venv` present.
- [ ] **Step 3: Record import-budget baseline**

```bash
make import-snapshot
git status --short tests/unit/import_budget/   # expect: clean (no drift on main)
mkdir -p /tmp/claude-1000/import-budget-baseline
cp -r tests/unit/import_budget/snapshots /tmp/claude-1000/import-budget-baseline/
```

If `make import-snapshot` shows drift on a clean main checkout, STOP and report — do not commit drift as part of this branch.

- [ ] **Step 4: Sanity gate** — `make coverage`. Expected: pass at current main baseline.

### Task 2: Rename `otto/configmodule/` → `otto/config/`

**Files:**
- Move: `src/otto/configmodule/` → `src/otto/config/`; inner `configmodule.py` → `src/otto/config/fleet.py`
- Modify: every importer (`grep -rln "configmodule" src tests docs` — ~100+ files), `src/otto/__init__.py` `_LAZY_EXPORTS` (5 entries point at `otto.configmodule`)

**Interfaces:**
- Produces: `otto.config` package; `from otto.config import get_repos, get_host, get_lab, load_lab, all_hosts, run_on_all_hosts` (same names, new home). All later tasks import from `otto.config`.

- [ ] **Step 1: Move the package**

```bash
git mv src/otto/configmodule src/otto/config
git mv src/otto/config/configmodule.py src/otto/config/fleet.py
```

- [ ] **Step 2: Sweep references**

```bash
grep -rl "configmodule" src tests docs --include="*.py" --include="*.md" --include="*.rst" \
  | xargs sed -i 's/configmodule\.configmodule/config.fleet/g; s/from \.configmodule import/from .fleet import/g; s/configmodule/config/g'
```

Then hand-inspect `src/otto/config/__init__.py` (its internal `from .configmodule import ...` lines must now read `from .fleet import ...`) and `src/otto/__init__.py` `_LAZY_EXPORTS` values (`("otto.configmodule", ...)` → `("otto.config", ...)`).

- [ ] **Step 3: Verify zero stragglers**

Run: `grep -rn "configmodule" src tests docs web --include="*.py" --include="*.md" --include="*.rst" --include="*.ts" | grep -v CHANGELOG`
Expected: no output.

- [ ] **Step 4: Gate** — `make coverage && uv run nox -s typecheck`. Expected: pass.
- [ ] **Step 5: Commit** — `refactor(rename)!: otto.configmodule → otto.config (inner module → fleet.py)`

### Task 3: Rename `otto/storage/` → `otto/labs/`; move host factory to `otto/host/factory.py`

**Files:**
- Move: `src/otto/storage/` → `src/otto/labs/`; `src/otto/labs/factory.py` → `src/otto/host/factory.py`
- Modify: importers of `otto.storage` (`config/lab.py`, `config/repo.py`, `cli/invoke.py:334`, `cli/init.py`, `cli/tunnel.py`, `config/completion_cache.py`, `testing/conformance.py`, tests, docs); importers of `storage.factory` (`cli/tunnel.py:83`, `cli/init.py:392`, `completion_cache.py:816,867`)

**Interfaces:**
- Produces: `from otto.labs import build_lab_repository, LabRepository, LabRepositoryError, register_lab_repository, JsonFileLabRepository`; `from otto.host.factory import create_host_from_dict, validate_host_dict`.

- [ ] **Step 1: Move**

```bash
git mv src/otto/storage src/otto/labs
git mv src/otto/labs/factory.py src/otto/host/factory.py
```

- [ ] **Step 2: Sweep** — `grep -rln "otto.storage\|from ..storage\|from .storage\|otto\.labs\.factory" src tests docs` and fix each: `otto.storage`/`..storage` → `otto.labs`/`..labs`; factory imports → `otto.host.factory` / `..host.factory`. Remove any `factory` re-export from `src/otto/labs/__init__.py` and add the same names to `src/otto/host/__init__.py`'s re-exports if the old `storage/__init__.py` exported them.
- [ ] **Step 3: Verify** — `grep -rn "otto\.storage\|from \.\.storage\|labs\.factory" src tests docs | grep -v CHANGELOG` → no output. Check `docs/guide/host-database.md` + `extending-backends.md` updated (they document the lab-repository seam).
- [ ] **Step 4: Gate** — `make coverage && uv run nox -s typecheck`.
- [ ] **Step 5: Commit** — `refactor(rename)!: otto.storage → otto.labs; host factory → otto.host.factory`

### Task 4: Rename `otto/coverage/correlator/` → `otto/coverage/merge/`

**Files:**
- Move: `src/otto/coverage/correlator/` → `src/otto/coverage/merge/`
- Modify: importers (`coverage/reporter.py`, `coverage/__init__.py`, tests under `tests/*/coverage/`)

- [ ] **Step 1:** `git mv src/otto/coverage/correlator src/otto/coverage/merge`
- [ ] **Step 2: Sweep** — `grep -rl "correlator" src tests docs | xargs sed -i 's/correlator/merge/g'`; hand-check prose hits ("PathCorrelator" class name stays or renames? **Rename class too**: `PathCorrelator` → `PathRemapper` in `merge/paths.py` — sweep `PathCorrelator` → `PathRemapper`).
- [ ] **Step 3: Verify** — `grep -rni "correlator" src tests docs web | grep -v CHANGELOG` → no output.
- [ ] **Step 4: Gate + Commit** — `refactor(rename)!: coverage.correlator → coverage.merge; PathCorrelator → PathRemapper`

### Task 5: Rename `Host.oneshot()` → `Host.exec()`

**Files:**
- Modify: `src/otto/host/host.py` (Protocol + BaseHost), `unix_host.py`, `embedded_host.py`, `local_host.py`, `docker_host.py`, all callers (`grep -rln "\.oneshot(\|def oneshot\|oneshot(" src tests docs` — includes `tunnel/`, `link/`, `docker/`, examples, docs)

**Interfaces:**
- Produces: `Host.exec(...)` with the exact signature `oneshot` has today (only the name changes). CLI verb `otto host <id> exec` follows automatically via `@cli_exposed`.

- [ ] **Step 1: Sweep** — `grep -rl "oneshot" src tests docs | xargs sed -i 's/oneshot/exec/g'`; hand-inspect for prose casualties ("one-shot" hyphenated prose should stay prose — reword to "stateless exec" where it described the method).
- [ ] **Step 2: Handle flake8-builtins** — if `nox -s lint` flags A00x on `def exec`, add a narrow `# noqa: A001`/`A003` (whichever fires) on the `def exec` lines only, with comment `— deliberate: industry verb (docker/kubectl exec)`. Do NOT add a deny-list entry.
- [ ] **Step 3: Verify** — `grep -rn "oneshot" src tests docs | grep -v CHANGELOG` → no output; `uv run otto host --help` still lists verbs (run `uv run otto --help` smoke).
- [ ] **Step 4: Gate + Commit** — `refactor(rename)!: Host.oneshot → Host.exec`

### Task 6: Rename `Host.interact()` → `Host.login()`

**Files:**
- Modify: `src/otto/host/host.py:558` region (`@cli_exposed(name="login")` on `interact`), subclass overrides, callers, docs.

- [ ] **Step 1: Sweep** — `grep -rn "interact" src tests docs` → rename method defs/calls to `login`; change decorator to bare `@cli_exposed()` (name override gone).
- [ ] **Step 2: Verify** — `grep -rn "def interact\|\.interact(" src tests | grep -v CHANGELOG` → no output. `otto host <id> login` still synthesized (unit tests for host verb menus pass).
- [ ] **Step 3: Gate + Commit** — `refactor(rename)!: Host.interact → Host.login`

### Task 7: Coverage `context` → `run`

**Files:**
- Modify: `src/otto/coverage/store/model.py` (`ContextRecord`→`RunRecord` at :139; field `contexts`→`runs`; `context_hits`→`run_hits` at :196,218-219,301-302,503; JSON keys `"contexts"`→`"runs"` at :442,457 and per-line `"ctx"`→`"run"` at :302,503), `coverage/reporter.py`, `renderer/html_renderer.py` + templates (UI labels), `web/src/covreport/*.ts` (labels/fields if present), `cli/cov.py` help text, tests, `docs/guide/coverage.md`.

**Interfaces:**
- Produces: `RunRecord`, `LineRecord.run_hits`, store JSON `{"runs": [...]}` with per-line `"run"` key. Store format version bumps; old files fail loud.

- [ ] **Step 1: Bump the store format version** — locate the version constant/check in `store/model.py` `save`/`load` (grep `version` in that file). Increment it; ensure `load()` raises `ValueError("coverage store format vN required; found vM — regenerate with otto cov get/report")` on mismatch. If no version field exists today, add `"format": 2` to `to_dict` and a fail-loud check in `load` (missing/other → same ValueError).
- [ ] **Step 2: Rename sweep** — in `src/otto/coverage`, `web/src/covreport`, `tests`, `docs/guide/coverage.md`: `ContextRecord`→`RunRecord`, `context_hits`→`run_hits`, `contexts`→`runs` (scoped to coverage files ONLY — do not touch `OttoContext`/`get_context`/typer ctx). Use targeted greps per identifier, not a blanket `s/context/run/`.
- [ ] **Step 3: Failing-then-passing test** — update store round-trip tests to assert the new keys; add:

```python
def test_load_rejects_old_format(tmp_path):
    p = tmp_path / "store.json"
    p.write_text('{"format": 1, "contexts": []}')
    with pytest.raises(ValueError, match="regenerate"):
        CoverageStore.load(p)
```

- [ ] **Step 4: Verify** — `grep -rn "ContextRecord\|context_hits" src tests web | grep -v CHANGELOG` → no output; `make coverage` + `make web` (TS build) pass.
- [ ] **Step 5: Commit** — `refactor(cov)!: coverage 'context' → 'run' (RunRecord, run_hits, store v2)`

### Task 8: Capture `pin` → `base_commit`

**Files:**
- Modify: `src/otto/coverage/capture/model.py` (:4,23,39,99,126,132,164 — field, docstrings, kwarg), `capture/remap.py`, `capture/produce.py`, `capture/store_dir.py`, `coverage/validity.py`, tests, docs.

- [ ] **Step 1:** Bump the capture format version (same fail-loud pattern as Task 7 Step 1; message: "capture format vN required — re-capture with otto cov get"). Sweep `pin` → `base_commit` in `src/otto/coverage/capture/` + consumers + tests (targeted: `\bpin\b` within coverage files; check "pinned" prose reads sensibly — "pinned to base_commit" is fine).
- [ ] **Step 2:** Add old-format rejection test (mirror Task 7 Step 3 for `Capture.load`).
- [ ] **Step 3: Verify** — `grep -rn "\bpin\b" src/otto/coverage tests/unit/coverage tests/integration 2>/dev/null | grep -v CHANGELOG` → no output. Gate.
- [ ] **Step 4: Commit** — `refactor(cov)!: capture 'pin' → 'base_commit' (capture format bump)`

### Task 9: "stamp a capture" → "annotate"

**Files:**
- Modify: `src/otto/cli/cov.py` (`_capture_stamps`:418 → `_capture_annotations`, docstring), `src/otto/coverage/capture/produce.py:96` region (stamp wording), any other non-gcov "stamp" uses (`grep -rn "stamp" src/otto/coverage src/otto/cli/cov.py` — KEEP gcov `.gcno` stamp senses: `errors.py`, `merger/stamp` checks).

- [ ] **Step 1:** Rename `_capture_stamps` → `_capture_annotations`; reword non-gcov "stamp" docstrings/comments to "annotate"/"tag". Verify remaining `grep -rn "stamp" src/otto/coverage` hits are all gcov-header senses.
- [ ] **Step 2: Gate + Commit** — `refactor(cov): reserve 'stamp' for gcov header; capture metadata is 'annotations'`

**Phase 0 exit:** full gate (`make coverage && uv run nox -s lint typecheck && make docs`), then `make import-snapshot` — commit snapshot changes if any as `chore(imports): refresh snapshot after renames`.

---

## Phase A — `otto.suite` run API

### Task 10: `otto/suite/run.py` — RunOptions, SuiteRunResult, run_suite, find_suite

**Files:**
- Create: `src/otto/suite/run.py`
- Modify: `src/otto/suite/__init__.py` (re-exports), `src/otto/cli/test.py` (delete moved code in Task 12)
- Test: `tests/unit/suite/test_run_api.py` (create)

**Interfaces:**
- Consumes: `SUITES` registry (`otto/suite/register.py:41`), `OttoPlugin`/`OttoOptionsPlugin`, `get_repos` (`otto.config`), `get_context`/`try_get_context` (`otto.context`).
- Produces (exact, used by Tasks 11-13, 16):

```python
RunOptions            # frozen dataclass; fields exactly = today's TestRunOptions (cli/test.py:168-193)
SuiteRunResult        # frozen dataclass: exit_code: int, junit_paths: list[Path],
                      #   stability_report: Path | None, stability_unstable: bool, output_dir: Path
                      #   property passed -> bool  (exit_code == 0)
def run_suite(suite: type, *, options: object | None = None,
              run_options: RunOptions = RunOptions(), output_dir: Path | None = None) -> SuiteRunResult
def find_suite(name: str) -> type
def resolve_output_dir(output_dir: Path | None) -> Path   # precedence helper, reused by run_selection
```

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/suite/test_run_api.py
from pathlib import Path
import pytest
from otto.suite.run import RunOptions, SuiteRunResult, find_suite, resolve_output_dir

def test_run_options_defaults_match_cli():
    o = RunOptions()
    assert o.cov_clean is True and o.threshold == 100.0 and o.project_name == "Coverage Report"

def test_suite_run_result_passed():
    r = SuiteRunResult(exit_code=0, junit_paths=[Path("j.xml")],
                       stability_report=None, stability_unstable=False, output_dir=Path("."))
    assert r.passed
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.exit_code = 1  # type: ignore[misc]

def test_final_exit_code_stability_failure():
    # threshold violation on an otherwise-green run must fail the invocation
    assert _final_exit_code(rc=0, unstable=True) == 1
    assert _final_exit_code(rc=0, unstable=False) == 0
    assert _final_exit_code(rc=5, unstable=False) == 5  # NO_TESTS_COLLECTED stays a failure

def test_find_suite_unknown_lists_registered():
    with pytest.raises(LookupError, match="registered"):
        find_suite("TestNoSuchSuite")

def test_resolve_output_dir_explicit_wins(tmp_path):
    assert resolve_output_dir(tmp_path) == tmp_path

def test_resolve_output_dir_falls_back_to_cwd(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    # no explicit dir, no context output_dir
    assert resolve_output_dir(None) == tmp_path
```

- [ ] **Step 2: Run** `uv run pytest tests/unit/suite/test_run_api.py -v` — Expected: FAIL (`ModuleNotFoundError: otto.suite.run`).
- [ ] **Step 3: Implement `src/otto/suite/run.py`.** Move, verbatim except as noted, from `cli/test.py`: `TestRunOptions` (:168-193, renamed `RunOptions`, docstring loses the ctx.meta framing), `_run_pytest_session` (:421-509), `_print_stability_report` (:623-660 — **change**: return `bool` (any_unstable) instead of calling `SystemExit`; keep writing `stability_report.txt`), `resolve_suite` (:241-251), `_repo_confcutdir` (:254-267), `_pre_run_cov_clean` (:345-362, temporarily imported from its current home; moves to `otto.coverage` in Task 15). New code:

```python
def resolve_output_dir(output_dir: Path | None) -> Path:
    """Explicit param → context output_dir → CWD (xdir-defaults-to-CWD philosophy)."""
    if output_dir is not None:
        return output_dir
    from ..context import try_get_context
    ctx = try_get_context()
    if ctx is not None and ctx.output_dir is not None:
        return ctx.output_dir
    return Path.cwd()

def find_suite(name: str) -> type:
    """Resolve a registered OttoSuite subclass by class name via SUITES."""
    from .register import SUITES
    if name not in SUITES:
        registered = ", ".join(sorted(SUITES.names())) or "<none>"
        raise LookupError(f"unknown suite {name!r}; registered: {registered}")
    return SUITES.get(name).cls   # cls added to SuiteEntry in Step 3a

def _final_exit_code(rc: int, unstable: bool) -> int:
    """Threshold violations fail an otherwise-green run; pytest rc wins otherwise."""
    return 1 if (unstable and rc == 0) else int(rc)

def run_suite(suite: type, *, options: object | None = None,
              run_options: RunOptions = RunOptions(),
              output_dir: Path | None = None) -> SuiteRunResult:
    import asyncio, inspect
    from ..config import get_repos
    repos = get_repos()
    suite_file = inspect.getfile(suite)
    log_dir = resolve_output_dir(output_dir)
    results_path = run_options.results or str(log_dir / "junit.xml")
    asyncio.run(_pre_run_cov_clean(repos, run_options))
    outcome = _run_pytest_session(
        [suite_file], suite.__name__, _repo_confcutdir(suite_file, repos),
        run_options, options, results_path,
        [p for r in repos for p in r.tests], log_dir, suite.__name__,
    )
    asyncio.run(_post_run_coverage(repos, log_dir, run_options))  # Task 15 re-homes this onto otto.coverage
    return SuiteRunResult(exit_code=_final_exit_code(outcome.rc, outcome.unstable),
                          junit_paths=[Path(results_path)],
                          stability_report=outcome.report,
                          stability_unstable=outcome.unstable,
                          output_dir=log_dir)
```

**Step 3a (required):** add `cls: type` to `SuiteEntry` (`suite/register.py:28-33`) and pass `suite_class` at registration (`register.py:129-134`) so `find_suite` returns `SUITES.get(name).cls` directly — no re-import gymnastics. `_run_pytest_session` return changes from `int` to `tuple`? **No tuples** — return a small frozen dataclass `_SessionOutcome(rc: int, unstable: bool, report: Path | None)` defined beside it.
- [ ] **Step 4: Run tests** — Expected: PASS. Also `uv run nox -s typecheck`.
- [ ] **Step 5: Commit** — `feat(suite)!: otto.suite.run — RunOptions, SuiteRunResult, run_suite, find_suite`

### Task 11: `run_selection` + selection resolution move

**Files:**
- Create: `src/otto/suite/selection.py` (move `_resolve_selection`, `_repos_with_marker_matches`, `_base_test_name` (:270-272), `_absolute_nodeid` (:275+) from `cli/test.py`)
- Modify: `src/otto/suite/run.py` (add `run_selection`)
- Test: extend `tests/unit/suite/test_run_api.py`

**Interfaces:**
- Produces: `run_selection(*, run_options: RunOptions, output_dir: Path | None = None) -> SuiteRunResult` — raises `ValueError("No tests matched the selection.")` when nothing matches; `junit_paths` has one entry per participating repo (multi-repo fan-out logic moved verbatim from `cli/test.py:592-616`).

- [ ] **Step 1: Failing test** — `run_selection(run_options=RunOptions(tests="test_nonexistent_zzz"))` raises `ValueError, match="No tests matched"`.
- [ ] **Step 2:** Move code; `run_selection` mirrors today's `cli/test.py:566-620` minus typer: no `rprint`, raise `ValueError` instead of `Exit(1)`; return `SuiteRunResult` with `exit_code=worst`.
- [ ] **Step 3:** Tests pass; gate; **Commit** — `feat(suite)!: run_selection as library API; selection resolution moves to otto.suite`

### Task 12: Rewire CLI to consume `otto.suite`

**Files:**
- Modify: `src/otto/cli/test.py` (delete moved code; callback at :963 constructs `otto.suite.RunOptions` into `ctx.meta[RUN_OPTIONS_KEY]`; `run_selection` branch at :995 becomes wrapper), `src/otto/suite/register.py:97-108` (runner calls `otto.suite.run` — core-to-core, the `from ..cli.test import run_suite` lazy import dies)
- Test: existing CLI tests (`tests/unit/cli/`, `tests/integration/`)

**Interfaces:**
- Consumes: Task 10/11 exact signatures.

- [ ] **Step 1:** In `register.py`'s `runner()` replace the lazy import + call with:

```python
from .run import RUN_OPTIONS_KEY, RunOptions, run_suite
stored = ctx.meta.get(RUN_OPTIONS_KEY)
run_options = stored if isinstance(stored, RunOptions) else RunOptions()
from ..context import get_context
result = run_suite(_suite_cls, options=opts_instance, run_options=run_options,
                   output_dir=get_context().output_dir)
if result.exit_code != 0:
    raise typer.Exit(code=result.exit_code)
```

(`RUN_OPTIONS_KEY` moves to `suite/run.py` as the single definition; `cli/test.py` imports it from there.)
- [ ] **Step 2:** `cli/test.py`: delete `TestRunOptions`, `run_suite`, `run_selection`, `_run_pytest_session`, `_print_stability_report`, `resolve_suite`, `_repo_confcutdir`, selection helpers; callback builds `RunOptions(...)` (same field mapping as :963-981); the `tests or markers` branch calls `otto.suite.run.run_selection` in `try/except ValueError` → `rprint("[red]...")` + `Exit(1)`.
- [ ] **Step 3:** Sweep test imports: `grep -rn "from otto.cli.test import\|cli\.test\._" tests` → update to `otto.suite.run`/`otto.suite.selection` equivalents.
- [ ] **Step 4:** Full `make coverage` (CLI behavior identical: exit codes for failing suite, rc=5, stability threshold, `--tests` fan-out junit naming). **Commit** — `refactor(cli)!: otto test consumes otto.suite.run; CLI-side engine deleted`

### Task 13: Suite library docs + top-level exports

**Files:**
- Modify: `src/otto/__init__.py` (add `run_suite`, `RunOptions` to `__all__` + `_LAZY_EXPORTS` → `("otto.suite.run", ...)`), `docs/guide/library-usage.md` (new "Running suites from Python" section), `docs/guide/test.md` (cross-link)

- [ ] **Step 1:** Exports + docs section with a complete worked example (sync script, `output_dir` precedence, `asyncio.to_thread` note for async callers, `find_suite` for dynamic callers).
- [ ] **Step 2:** `make docs` (nitpicky gate). **Commit** — `docs(suite): running suites from Python; export run_suite/RunOptions`

**Phase A exit:** full gate + `make import-snapshot` (commit refresh if changed).

---

## Phase B — `otto.coverage` collection API

### Task 14: `otto/coverage/config.py`

**Files:**
- Create: `src/otto/coverage/config.py` — move verbatim from `cli/test.py`: `_has_cov_config` (:1272-1276 → `has_cov_config`), `_get_cov_repo` (:1279-1284 → `get_cov_repo`), `_get_cov_config` (:1287+ → `get_cov_config`)
- Modify: `src/otto/cli/test.py`, `src/otto/cli/cov.py:472` (`from .test import _get_cov_config, _get_cov_repo` → `from ..coverage.config import ...`), `cli/cov.py:196` region (`_resolve_cov_settings`)
- Test: `tests/unit/coverage/test_config.py` (move/adapt any existing tests of these helpers; else add: repo-with-`[coverage]` found; no-config returns `{}`/`None`)

- [ ] **Step 1:** failing import test → move → pass → gate.
- [ ] **Step 2: Commit** — `refactor(cov)!: [coverage] config resolution → otto.coverage.config`

### Task 15: `otto/coverage/collect.py`

**Files:**
- Create: `src/otto/coverage/collect.py`
- Modify: `src/otto/coverage/__init__.py` (export `collect_coverage`, `CollectResult`, `clean_remote_gcda`), `src/otto/cli/test.py` (delete `_run_coverage`:1158-1269, `_write_cov_metadata`, `_pre_run_cov_clean` body), `src/otto/suite/run.py` (import `clean_remote_gcda`/post-run from here)
- Test: `tests/unit/coverage/test_collect.py`

**Interfaces:**
- Produces (exact, consumed by Task 16 and `otto.suite.run`):

```python
@dataclass(frozen=True)
class CollectResult:
    cov_dir: Path
    host_dirs: dict[str, Path]
    captures_written: list[Path]

async def clean_remote_gcda(repos: list[Repo] | None = None) -> None
async def collect_coverage(cov_dir: Path, *, repos: list[Repo] | None = None,
                           tier: str | None = None, ticket: str | None = None,
                           note: str | None = None, tester: dict[str, str] | None = None,
                           display_names: dict[str, str] | None = None) -> CollectResult
# Raises: ValueError (no [coverage] config / tier resolution), GitUnavailableError,
# CoverageDataMismatchError, CoverageToolVersionError, RuntimeError (merge) — FAIL LOUD.
```

- [ ] **Step 1: Failing test** — `collect_coverage(tmp_path)` with no `[coverage]` config raises `ValueError, match=r"\[coverage\]"`.
- [ ] **Step 2: Implement** by composing the moved bodies: `_run_coverage` (fetch stage), `_write_cov_metadata` (private `_write_metadata` here), capture tail from both `_run_coverage`:1247-1269 and `_do_get`:617-633 — single implementation, **no try/except swallowing inside** (the "collected nothing" case raises `ValueError(f"no .gcda counters retrieved from any host ({where})")` using `_do_get`'s message). `clean_remote_gcda` = `_pre_run_cov_clean` minus the `opts` gate (caller decides).
- [ ] **Step 3:** `otto.suite.run`'s post-run becomes:

```python
async def _post_run_coverage(repos, log_dir, opts) -> None:
    if opts.cov:
        from ..coverage.collect import collect_coverage
        try:
            await collect_coverage(opts.cov_dir or log_dir / "cov", repos=repos)
        except (ValueError, RuntimeError, FileNotFoundError) as e:
            logger.warning("Coverage collection failed (%s); raw artifacts remain", e)
    if opts.cov_report:
        _run_post_report(repos, log_dir, opts)   # cli/test.py:370-418 moved VERBATIM into this module
                                                 # (already swallow-safe: its own try/except logs and continues)
```

(pre-run: `if opts.cov and opts.cov_clean: await clean_remote_gcda(repos)`.)
- [ ] **Step 4:** Tests pass; existing integration/e2e coverage flows green (`make coverage`). **Commit** — `feat(cov)!: otto.coverage.collect — composed fetch/metadata/capture workflow`

### Task 16: `cli/cov.py` consumes `collect_coverage`; docs

**Files:**
- Modify: `src/otto/cli/cov.py` — `_do_get` (:511-654) shrinks to: `_connect_cov_hosts`-derived validation it still owns (tier resolve + manual-tier `--ticket` guard + git preflight + output-dir resolve), then ONE `collect_coverage(...)` call in try/except mapping the raised family to `_GetError(str(e))`; manual-store copy + `clean` block stay (clean uses `CollectResult.host_dirs` to scope); delete its inline fetch/metadata lines and the `from .test import _write_cov_metadata` import.
- Modify: `docs/guide/library-usage.md` (+ "Collecting coverage from Python" section), `docs/guide/coverage.md` cross-link.
- Test: existing `tests/unit/cli/test_cov*.py` behavior unchanged.

- [ ] **Step 1:** Rewire; verify identical failure messages (tests assert them).
- [ ] **Step 2:** Docs section with worked example (`collect_coverage` → `run_coverage_report`). `make docs`.
- [ ] **Step 3:** Gate. **Commit** — `refactor(cli)!: otto cov get consumes otto.coverage.collect; docs`

**Phase B exit:** full gate + `make import-snapshot` refresh. Verify: `grep -rn "from .test import\|from ..cli" src/otto/coverage src/otto/suite` → no output (no core→cli imports).

---

## Phase C — Reservations library-first + logger hygiene

### Task 17: `ReservationGate.evaluate()`

**Files:**
- Modify: `src/otto/reservations/check.py` — delete `import typer` (:20); rename `ReservationState` → `ReservationGate`; delete `gate(ctx)` (:130-171); add `ReservationGateOutcome` + `evaluate()`
- Modify: `src/otto/reservations/__init__.py` — exports: `ReservationGate`, `ReservationGateOutcome`; `build_reservation_state` (:133) → `build_reservation_gate` returning `ReservationGate`
- Test: `tests/unit/reservations/test_gate.py`

**Interfaces:**
- Produces (exact, consumed by Task 18):

```python
@dataclass(frozen=True)
class ReservationGateOutcome:
    checked: bool
    skipped: bool
    warning: str | None

@dataclass(frozen=True)
class ReservationGate:
    backend: "ReservationBackend | None" = None
    identity: "ResolvedIdentity | None" = None
    skip_check: bool = False
    backend_factory: "Callable[[], ReservationBackend] | None" = None

    def evaluate(self) -> ReservationGateOutcome:
        from ..config import get_lab
        if self.skip_check:
            lab = get_lab()
            username = self.identity.username if self.identity is not None else "<unknown>"
            needed = required_resources(lab)
            warning = (
                f"\N{WARNING SIGN}  Reservation check SKIPPED for user {username!r} "
                f"on lab {lab.name!r}. Required resources: {sorted(needed)!r}"
            )
            logger.warning("Reservation check skipped for user %r on lab %r. Required: %r",
                           username, lab.name, sorted(needed))
            return ReservationGateOutcome(checked=False, skipped=True, warning=warning)
        if self.backend is None:
            return ReservationGateOutcome(checked=False, skipped=False, warning=None)
        lab = get_lab()
        if self.identity is None:
            raise RuntimeError("identity must be resolved before evaluate() runs")
        check_reservations(lab, self.identity.username, self.backend)
        return ReservationGateOutcome(checked=True, skipped=False, warning=None)
```

(The rich markup `[bold red]...[/bold red]` wrapper is applied by the CLI adapter, not stored in `warning`.)

- [ ] **Step 1: Failing tests** — outcome matrix: skip→`skipped=True` + warning text contains "SKIPPED"; no-backend→all-False/None; backend+missing→raises `MissingReservationError`; backend+held→`checked=True`. Use `NullReservationBackend`/a stub backend and a monkeypatched `get_lab`.
- [ ] **Step 2:** Implement; sweep `ReservationState`/`build_reservation_state` across `cli/invoke.py:359-385`, `config/completion_cache.py`, `cli/reservation.py`, tests.
- [ ] **Step 3:** Import-hygiene test (in `tests/unit/reservations/test_gate.py`):

```python
def test_reservations_import_is_typer_free():
    import subprocess, sys
    code = "import sys, otto.reservations; sys.exit(1 if 'typer' in sys.modules else 0)"
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0
```

- [ ] **Step 4:** Gate. **Commit** — `feat(reservations)!: ReservationGate.evaluate() — typer-free library gate`

### Task 18: CLI adapter for the gate

**Files:**
- Modify: `src/otto/cli/invoke.py` — `command_preamble` (:464-467) replaces `from ..reservations import gate; gate(ctx)` with:

```python
if spec.gate:
    res = ctx.meta.get("otto_reservation")
    if res is not None:
        outcome = res.evaluate()
        if outcome.warning:
            from rich import print as rprint
            rprint(f"[bold red]{outcome.warning}[/bold red]")
```

- Modify: `src/otto/cli/monitor.py` (same replacement at its `gate(ctx)` call site — grep `reservations import gate`), `ensure_lab_context` stores a `ReservationGate` (rename only).
- Test: existing CLI gate tests (skip-warning text, missing-reservation exit) stay green — `MissingReservationError` propagates exactly as before (same handler as today; verify with `grep -rn "MissingReservationError" src/otto/cli`).

- [ ] **Step 1:** Rewire both call sites; run reservation CLI tests.
- [ ] **Step 2:** Gate. **Commit** — `refactor(cli): reservation gate presentation moves to cli/invoke adapter`

### Task 19: Logger — delete `get_logger`, stdlib idiom, lazy management

**Files:**
- Modify: `src/otto/logger/__init__.py` — remove `from . import management` (:5) and `get_logger` re-export; PEP-562 lazy `__getattr__` for `management`; move the NullHandler block (:10-12) OUT
- Modify: `src/otto/__init__.py` — drop `get_logger` from `__all__`/`_LAZY_EXPORTS`; add at module top (before the lazy table):

```python
import logging as _logging

_otto_logger = _logging.getLogger("otto")
if not any(isinstance(h, _logging.NullHandler) for h in _otto_logger.handlers):
    _otto_logger.addHandler(_logging.NullHandler())
```

- Delete: `src/otto/logger/logger.py` (`get_logger`)
- Sweep: ~30 sites `from ..logger import get_logger` / `from otto.logger import get_logger` → `import logging` + module-level `logger = logging.getLogger(__name__)` (keep existing `logger = ...` variable names; `get_logger()` (bare) → `logging.getLogger(__name__)`; `get_logger("x")` → `logging.getLogger("otto.x")` — grep shows whether any named calls exist).
- Test: `tests/unit/logger/` — update; add import-hygiene test mirroring Task 17 Step 3: `import otto.logger` leaves `rich` out of `sys.modules`; and `import otto` attaches exactly one NullHandler to `'otto'`.

- [ ] **Step 1:** Failing hygiene tests → implement → pass.
- [ ] **Step 2:** Sweep call sites; `grep -rn "get_logger" src tests docs | grep -v CHANGELOG` → no output.
- [ ] **Step 3:** Full gate (three-sink logging integration tests must be untouched — no formatter renders `%(name)s`, verified in design). **Commit** — `refactor(logger)!: delete get_logger; stdlib getLogger(__name__); NullHandler at otto/__init__; lazy management`

### Task 20: Reservations docs + example CLI; final metrics + verify

**Files:**
- Create: `src/otto/examples/reservations_cli.py` — complete, runnable third-party Typer app (~40 lines): builds a backend via `build_backend`, resolves identity via `resolve_username`, constructs `ReservationGate`, calls `evaluate()`, prints outcome/warning, exits 1 on `MissingReservationError`.
- Modify: `docs/guide/reservations.md` — new "Using the reservation library in your own CLI" section embedding the example and pointing custom-backend authors at `otto.testing.assert_reservation_backend_conforms`; `docs/guide/library-usage.md` cross-link.
- Test: `tests/unit/examples/` — import + smoke-run the example's check function with the Null backend.

- [ ] **Step 1:** Example + docs + test; `make docs`.
- [ ] **Step 2: Final metrics** — `make import-snapshot`; produce the before/after table:

```bash
diff -ru /tmp/claude-1000/import-budget-baseline/snapshots tests/unit/import_budget/snapshots
```

Record module counts per tracked entry point (`import otto`, CLI startup, completion fast path, `import otto.reservations`, `import otto.logger`) in the PR notes. **Acceptance: no entry point regresses; reservations and logger drop.**
- [ ] **Step 3: Full gate** — `make coverage && uv run nox -s lint typecheck && make docs`.
- [ ] **Step 4: verify skill** — drive for real: `uv run otto test <suite> --cov` on an in-tree repo lab, `uv run otto cov get`, `uv run otto run <instruction>`, and `uv run python -m otto.examples.reservations_cli` (or its documented invocation).
- [ ] **Step 5: Commit** — `docs(reservations): build-your-own-CLI guide + example; import-budget results`

**Branch exit:** superpowers:finishing-a-development-branch (Chris squash-merges; hand him the branch summary + metric table).
