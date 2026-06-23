# Test-suite restructure & dedup — design

> Captured 2026-06-22. Implements fable review decision **#5** (test tree
> split: unit / integration / e2e — "names must not lie") plus the associated
> dedup/consolidation, from
> [todo/fable_review_outcome.md](../../../todo/fable_review_outcome.md).
> The host-pool + broader parallelization ideas (the speed half of the original
> request) are **deferred to a follow-up brainstorm** — see §12.

---

## 1. Goal

Restructure otto's test tree into three honest tiers (`unit` / `integration` /
`e2e`), consolidate the scattered fixtures/helpers, and remove genuine
duplication — **without changing what any test exercises**.

### Hard constraints (non-negotiable)

1. **Coverage stays equivalent.** Total, per-line, and per-branch coverage after
   the work must equal the baseline. Every gate must select the same test set it
   selects today.
2. **Integration and e2e scenarios are preserved.** No real-world scenario (a
   distinct host × term × transfer × backend × failure-mode combination) is lost.
3. **Thoroughness is not traded for speed.** Tests are removed only when
   *provably* redundant, with documented evidence (see §8).

If a change cannot be proven to satisfy 1–3, it is not made.

---

## 2. The core principle: tier and gate-selection are orthogonal

The reason the current names "lie" is that two independent axes are conflated:

- **Tier** = *level of the test*: pure logic with mocked/local I/O (`unit`) →
  real Vagrant/QEMU bed through otto's **Python API** (`integration`) → driving
  the **`otto` CLI binary** end-to-end (`e2e`). This is an **organizational**
  axis. → belongs in **directories**.
- **Resource need** = *which gate runs it*: no-VM vs. needs Vagrant
  (`integration`) vs. needs Zephyr QEMU (`embedded`) vs. multi-hop (`hops`),
  plus heaviness (`stability`, `concurrency`). This is the **selection** axis. →
  belongs in **markers**.

These axes do not line up: an e2e CLI test can run entirely locally (no VM), and
a unit test never needs one. Today four gates select on the `tests/unit`
**path**, which couples the two axes and is the root cause of the misnaming.

**The fix that makes the whole restructure safe:** drive gate selection from
**markers**, never from the tier directory. Then moving a file between tiers can
never change what a gate runs, and equivalence (constraint 1) is preserved by
construction as long as every marker is preserved.

---

## 3. Target tree

```text
tests/
  unit/          # no VM; all I/O mocked or local; fast
    host/ cli/ cov/ monitor/ suite/ storage/ models/
    configmodule/ reservations/ logger/ scripts/ docker/
  integration/   # real Vagrant / Zephyr-QEMU bed via otto's PYTHON API
    host/         # embedded + hop + unix-host integration suites
    cov/ docker/
  e2e/           # drives the `otto` CLI binary end-to-end
    host/ cov/ docker/ suite/ configmodule/
  _fixtures/     # lab_data/ + shared test helpers (see §7)
  repo1/ repo2/ repo3/ custom_hosts/   # UNCHANGED — SUT fixture repos stay put
```

Subsystem subdirectories are created within each tier only as files land in
them (no empty scaffolding).

`testpaths` in `pyproject.toml` becomes
`["tests/unit", "tests/integration", "tests/e2e"]`.

---

## 4. File-move inventory

Derived from a full marker + CLI-usage audit. "Split" means the file mixes
tiers and must be divided, not relocated wholesale.

### 4.1 Clean whole-file moves (unit → integration)

| File | Tests | Notes |
|------|-------|-------|
| `unit/host/test_hop_integration.py` → `integration/host/` | 17 | module-level `integration` (+`hops` on multi-hop cases); drop explicit `integration`, keep `hops`/`timeout` |
| `unit/host/test_session_stability_integration.py` → `integration/host/` | 9 | module-level `integration`+`stability`; drop `integration`, keep `stability` |
| `unit/cov/test_coverage_pipeline.py` → `integration/cov/` | 3 | module-level `integration`; drop it |

### 4.2 Split files (mixed unit + VM-marked in one file)

| File | Split |
|------|-------|
| `unit/host/test_unix_host.py` (102 tests, 20 `@integration`) | 82 unit cases stay in `unit/host/test_unix_host.py`; the 20 integration cases move to `integration/host/test_unix_host_integration.py` |
| `unit/suite/test_import_and_register.py` (12 tests, 1 `@integration`) | 11 unit stay; the 1 integration case extracts to `integration/suite/` |
| `unit/logger/test_logger.py` (6 tests, some `integration`) | inspect during implementation; unit cases stay, integration case(s) move to `integration/...` |

> A whole-file move of a mixed file would silently pull its unit cases out of
> the no-VM gate. Splitting is mandatory for these.

### 4.3 CLI / e2e moves

| File | → | Resource need | Marker after |
|------|---|---------------|--------------|
| `unit/configmodule/test_completion_cache.py` | `e2e/configmodule/` | none (CLI only) | unmarked |
| `unit/cov/test_coverage_e2e.py` | `e2e/cov/` | none (CLI/local) | unmarked |
| `unit/host/test_interact_e2e.py` | `e2e/host/` | none (CLI/local) | unmarked |
| `unit/suite/test_stability_e2e.py` | `e2e/suite/` | none (CLI/local) | unmarked |
| `integration/test_docker_e2e_cli.py` | `e2e/docker/` | docker host | keep explicit `integration` |
| `integration/test_embedded_coverage_e2e.py` | `e2e/host/` | Zephyr QEMU | keep explicit `embedded` |

**Safety invariant for the unmarked e2e moves:** a test that passes in CI today
(no VMs) needs no VM, so it stays unmarked and continues to run in the no-VM
gate. Implementation verifies each file's actual resource use before moving;
anything that genuinely needs a VM is marked accordingly (and that would be a
pre-existing latent bug we'd surface, not introduce).

---

## 5. Marker & gate-selection model

### 5.1 Marker rules

- **`integration` is auto-applied from the directory.** A `tests/integration/`
  conftest stamps `pytest.mark.integration` on every item it collects (via
  `pytest_collection_modifyitems`). The **explicit `@integration` decorators
  inside `tests/integration/` are removed** — the directory is now the single
  source of truth.
- **Type/heaviness markers stay explicit everywhere:** `embedded`, `hops`,
  `stability`, `concurrency`. A directory can't express these, so they remain on
  the test.
- **`tests/e2e/` is NOT auto-stamped.** e2e (CLI-level) is orthogonal to
  VM-need: a VM-requiring e2e test keeps an explicit `integration`/`embedded`
  marker; a local-only e2e test stays unmarked.
- **`tests/unit/` carries no VM marker, ever.**

### 5.2 Gate conversions (path → marker)

| Gate | Today | After |
|------|-------|-------|
| `coverage-unit` | `tests/unit -m "not integration"` | `tests/unit tests/e2e -m "not integration and not embedded"` |
| `nox-unit` (`UNIT_TEST_ARGS`) | `tests/unit -m "not integration"` | `tests/unit tests/e2e -m "not integration and not embedded"` |
| CI (`nox tests_unit`) | inherits `nox-unit` | inherits `nox-unit` |
| `repeat` | `tests/unit` (no marker) | **full local suite — all testpaths**, `--no-cov`, `--count=N` (per decision) |

The no-VM gates scope to **`tests/unit tests/e2e`** (not all testpaths) for two
reasons: they keep their current property of **never importing the
`tests/integration/` tree** (faster collection, no risk a VM-tree import does
work at import time), while still collecting the **local CLI e2e tests that
moved out of `tests/unit`** (the marker then deselects the two VM-requiring e2e
files). This reproduces today's no-VM set exactly: unit cases + local CLI e2e
cases, minus anything integration/embedded.

All other gates already select purely by marker and are unchanged:
`coverage` (`-m "not stability"`), `coverage-unix` (`-m "integration and not
embedded"`), `coverage-embedded` (`-m embedded`), `stability-unit`
(`-m concurrency`), `stability-unix` (`-m "stability and integration and not
embedded"`), `stability-embedded` (`-m "stability and embedded"`), `nox-unix`,
`nox-embedded`, `nox`.

> Why `not integration and not embedded` and not just `not integration`: embedded
> tests must be excluded from the unit gate too. Today the `tests/unit` path
> excluded them by location; the marker switch makes that explicit. (If audit
> shows every embedded test is also `integration`-stamped, the `and not embedded`
> is harmless redundancy — kept for clarity and drift-safety.)

### 5.3 Guard tests (drift prevention)

Cheap, no-VM assertions added to the unit tier:

- **G1:** every collected item under `tests/integration/` carries the
  `integration` marker (trivially true via auto-stamp; guards the hook).
- **G2:** no collected item under `tests/unit/` carries `integration`,
  `embedded`, or `hops`.

### 5.4 Pre-flight equivalence check (before flipping gates)

Before converting the gates, prove no test currently under `tests/integration/`
is *unmarked* — such a test is invisible to today's `coverage-unit` (wrong path)
but would be pulled into the marker-based unit gate. The audit indicates none
exist; this check makes it a verified precondition, not an assumption.

---

## 6. Worked equivalence example — the no-VM gate flip

Concrete proof that the riskiest conversion preserves its set. Today
`coverage-unit` = `pytest tests/unit -m "not integration"`. Its set is:

- all pure-unit tests under `tests/unit/` (incl. `concurrency`-marked, which are
  no-VM), **plus**
- the no-marker `*_e2e.py` CLI tests that currently live under `tests/unit/`.

After the move, those `*_e2e.py` files relocate to `tests/e2e/` (still
unmarked), and the 20 `@integration` methods leave `test_unix_host.py`. The new
gate `pytest tests/unit tests/e2e -m "not integration and not embedded"`
selects:

- the same pure-unit tests (now the *only* thing left in `tests/unit/`), **plus**
- the same local CLI e2e tests (now in `tests/e2e/`, still unmarked → selected),
  **minus** the two VM-requiring e2e files (marked `integration`/`embedded` →
  deselected, exactly as their VM nature requires and as they were excluded
  before by living in `tests/integration/`).

Net: identical function set. The Phase-0 `--collect-only` diff (§8.4) verifies
this mechanically rather than by argument.

---

## 7. `tests/_fixtures/` consolidation + lab-data path helper

### 7.1 Moves

- `tests/lab_data/` → `tests/_fixtures/lab_data/`
- `tests/_loop_reaper.py` → `tests/_fixtures/_loop_reaper.py`
- `tests/integration/host/_console_lock.py` → `tests/_fixtures/_console_lock.py`
- `tests/mockrepo.py` → `tests/_fixtures/mockrepo.py`

SUT repos (`repo1/2/3`, `custom_hosts`) stay where they are.

### 7.2 Single source of truth for lab-data paths

Add `tests/_fixtures/labdata.py` exposing `lab_data_path()`, `host_data(ne)`,
and `make_host(ne, **kw)`. This **eliminates the three depth-adjusted
`_LAB_DATA` constructions** and the brittle `parents[N]` arithmetic that would
otherwise break on every move:

- `tests/conftest.py:387`
- `tests/integration/conftest.py:24` (+ `_host_data` duplicate at :87)
- `tests/unit/models/test_jsonschema_validation.py:12`

The root `tests/conftest.py` **re-exports** `host_data` / `make_host` /
`active_context` / `EMBEDDED_BACKENDS` / `embedded_param_id` / `remote_name`, so
the 20+ existing `from tests.conftest import ...` call sites **do not change**.

### 7.3 Centralize repeated setup

- The `custom_hosts` `sys.path.insert` block (3 copies:
  `integration/host/conftest.py:41`, `unit/host/test_zephyr_inline_frame.py:21`,
  `unit/host/test_custom_hosts_module.py:19`) → one helper in `_fixtures/`.
- The `OTTO_SUT_DIRS` setdefault (2 copies: `integration/conftest.py:13`,
  `unit/cov/conftest.py:13`) → one helper, called from the relevant conftests
  (kept as an import-time call — it must run before any `otto` import).

### 7.4 Importers to update (exhaustive)

- `_loop_reaper`: `tests/conftest.py:31`, `tests/unit/test_loop_reaper.py:14`
  (and the test file moves with the rename consideration).
- `_console_lock`: `tests/integration/host/conftest.py:30`,
  `tests/unit/host/test_console_lock.py:8` → `tests/_fixtures._console_lock`.
- `mockrepo`: `tests/unit/configmodule/test_repo.py:8`.
- Non-test references to lab_data: `Makefile:196`, `scripts/lab_health.py:43`
  (`DEFAULT_HOSTS`). Update both to the new path.

---

## 8. Dedup methodology + the coverage / redundancy proof protocol

Three escalating tiers, each with a mechanical gate. Nothing escalates until the
prior tier is green.

### 8.1 Tier 1 — fixture / helper / conftest collapse (safe by construction)

Test *functions are untouched*, so coverage is bit-identical. Concrete clusters:

- `_host_data` (`integration/conftest.py:87`) → use shared `host_data`.
- Duplicate `carrot` / `tomato` fixtures (`integration/conftest.py:95,112`;
  `unit/cov/conftest.py:32,40`) → one parametrized/shared definition.
- `host2` / `host3` (`conftest.py:539,551`) — identical control flow, NE differs
  → a small fixture factory.
- The `sys.path` / `OTTO_SUT_DIRS` / `_LAB_DATA` consolidations from §7.

### 8.2 Tier 2 — parametrize duplicate test bodies

Merge near-identical functions (e.g. `test_*_ssh` + `test_*_telnet` asserting
the same behavior across a term) into one parametrized case **only when**:

- the merged parametrization yields the **same number of cases**,
- **every assertion** from each original is retained, and
- **per-file coverage delta == 0**.

Test IDs may change; scenarios stay distinct cases.

### 8.3 Tier 3 — remove provably-redundant tests

A test may be deleted **only** when all hold and are documented per deletion:

- **(a)** total + per-line + per-branch coverage is byte-identical with the test
  removed (it covered nothing unique);
- **(b)** no unique scenario tuple is lost (host × term × transfer × backend ×
  failure-mode) — coverage parity alone is insufficient, since two tests can
  cover the same lines while asserting different real-world behaviors;
- **(c)** the evidence (coverage diff + scenario rationale) is recorded in the
  commit/PR for that deletion.

### 8.4 Baseline & equivalence protocol

1. **Baseline (before any change):** capture `pytest --collect-only -q` (the full
   test-ID set) and a coverage report (line+branch) for each gate's set.
2. **After structural move (no deletions):** assert the collected test-ID set is
   identical *modulo path prefix* (same test functions, relocated) and coverage
   is unchanged. This proves §3–§7 lost nothing.
3. **After each dedup tier:** re-run the relevant gate set; assert the coverage
   invariant for that tier (§8.1 identical / §8.2 delta-0 / §8.3 byte-identical).

The dev VM reaches the live Vagrant/QEMU bed, so the worker runs the **full
real-bed baseline** (`make coverage` + `coverage-unix` + `coverage-embedded`) and
re-verifies integration/e2e parity **in-loop per task** — not just the no-VM
subset. Binding live-bed rules: a down bed must **fail loudly (never `skip`)**;
no VM is powered/restarted without asking; live-bed runs are never killed at a
tight timeout. The only deferred gate is the all-Pythons `make nox` matrix
(heavy; restructure is version-agnostic). Chris still commits (stage-only).

---

## 9. Execution plan

Isolated git worktree, **subagent-driven**, **staged-only** (Chris commits — the
`prepare-commit-msg` hook needs `/dev/tty` for AI-assist attribution). A fresh
worktree needs `uv sync` before any `ty`/sphinx/docs gate. `git mv` preserves
blame on these churn-heavy files.

- **Phase 0 — Baseline.** Capture the §8.4 baselines (collect-only set + full
  real-bed coverage) so every later phase can prove equivalence.
- **Phase 1 — `_fixtures/` consolidation + lab-data helper (§7).** Pure
  mechanical; no test bodies change. Gate: `coverage-unit` set identical + live
  `coverage-unix`/`coverage-embedded` parity (the helpers back those), `ty`, docs.
- **Phase 2 — tier restructure (§3–§5).** Split mixed files, `git mv` into
  tiers, add the auto-stamp conftest, remove redundant `integration` decorators,
  flip the four gates, add guard tests, run the §5.4 pre-flight. Gate:
  collect-only set identical modulo path; guards pass; full live-bed `make
  coverage` parity.
- **Phase 3 — dedup (§8).** Tier 1 → 2 → 3, each with its proof gate.

Per-task gate: targeted runs of the touched tier **including integration/e2e
against the live bed**, plus `make coverage-unit` + `ty`. Each phase boundary
runs the full live-bed `make coverage` and checks parity against the Phase-0
real-bed baseline. The all-Pythons `make nox` matrix is the only deferred gate.

---

## 10. Risks & mitigations

| Risk | Mitigation |
|------|------------|
| A gate silently selects a different set after the path→marker flip | §5.4 pre-flight + Phase-0 baseline + per-gate collect-only diff |
| A mixed file's unit cases drop out of the no-VM gate | Mandatory split (§4.2), not whole-file move |
| Depth `parents[N]` arithmetic breaks on move | §7.2 single path helper removes the arithmetic before moving |
| An "unmarked" e2e move actually needs a VM | Verify per file; CI-passes-today ⇒ no-VM invariant (§4.3) |
| A dedup removal loses a scenario coverage parity can't see | §8.3(b) scenario-tuple check, documented per deletion |
| `repeat`'s set changes meaning | Decided: `repeat` = full local suite (all testpaths) |

---

## 11. Out of scope (this effort)

- Relocating the SUT fixture repos (`repo1/2/3`, `custom_hosts`).
- The host-pool / dynamic host selection and broader parallelization/speed work
  (§12) — separate follow-up brainstorm.
- Any change to product (`src/otto/`) code.

---

## 12. Deferred — host-pool & speed (follow-up brainstorm seeds)

The original request also asked to brainstorm pulling integration/e2e targets
from a **dynamic pool of available hosts** rather than hardcoding a "favorite
VM," for parallel speedup. Deferred by decision, but seeded here so the
follow-up starts warm:

- **The favorite-VM concentration is real:** `host1`, `transfer_host`, and the
  hop fixtures all funnel onto **`carrot`**; `host2`→`tomato`, `host3`→`pepper`;
  **`basil`** sits nearly idle (only the embedded SSH hop). Pool selection would
  spread Unix load across `carrot/tomato/pepper/basil`.
- **Embedded backends are NOT poolable:** each `sprout*` is a distinct coverage
  scenario (fs variant × os_version × command_frame). They must stay
  scenario-parametrized — pooling them would lose coverage.
- **Reuse the existing idiom:** a cross-worker host *lease* can mirror the
  writer-fair file lock in `_fixtures/_console_lock.py`; lab data already carries
  `resources` and `labs` tags and there's a `remote_name(worker_id, …)`
  namespacing helper.
- **Health-aware routing:** a lease could route around the reactive bed-wedge
  gate (`integration/host/conftest.py`) so a sick host is skipped, not waited on.
- **Constraint to carry forward:** otto stays server-less (fable #6) — any pool
  is a *test-harness* lease, not a coordinator service, and must preserve the
  resource-contention groups (`--dist loadgroup`, the console lock).

A `todo/` note mirrors these seeds.
