# Directory-level test targets — design

**Date:** 2026-06-30
**Status:** Approved (brainstorming), pending spec review
**Branch / worktree:** `worktree-directory-level-test-targets`

## Problem

The `make`/`nox` test grid currently exposes a single axis of suffixes — `unit`,
`unix`, `embedded` — which select tests by **pytest marker** (a *resource* axis:
no-VM / Linux-VM / Zephyr). There is no way to run tests by **tier** (the
directory the test lives in). We want directory-based, cumulative *level*
targets:

- `unit` → `tests/unit` only
- `integration` → `tests/unit` + `tests/integration`
- *(bare, no suffix)* → `tests/unit` + `tests/integration` + `tests/e2e`

This must coexist with the existing resource targets, not replace them.

## Findings (current state)

Two orthogonal axes already exist in the suite, but only one is exposed:

- **Level = directory:** `tests/unit/` (145 files), `tests/integration/` (14),
  `tests/e2e/` (6).
- **Resource = marker:** `integration` ("requires Vagrant VMs"), `embedded`
  ("requires Zephyr"), `hops`, `stability`, `concurrency`.

Key mechanics discovered:

1. **The integration tier is already directory-driven.**
   [`tests/integration/conftest.py`](../../../tests/integration/conftest.py)
   has a `pytest_collection_modifyitems` hook that auto-stamps the `integration`
   marker on every test under that tree. No test there writes
   `@pytest.mark.integration`; the directory is the source of truth. **The
   `integration` marker is kept** — it is what lets the orthogonal resource
   targets (`unix`/`embedded`) select by marker.

2. **The e2e tier has no identity of its own (the one real gap).**
   `tests/e2e/` has *no* conftest and no auto-stamp. Its VM-needing tests are
   *manually* tagged `@pytest.mark.integration` (11 of them); there is no `e2e`
   marker. The e2e tier currently masquerades as `integration`.

3. **The resource axis is finer than the directory axis.** Inside
   `tests/integration/` live three different infra needs: Linux VMs
   (`hops`/integration), **Zephyr (`embedded`, in `tests/integration/host/`)**,
   and Docker. A directory-cumulative `integration` target therefore needs the
   *full* lab.

4. **CI does not run `make coverage-unit`.**
   [`.github/workflows/ci.yml`](../../../.github/workflows/ci.yml) runs
   `nox -s tests_unit-<py>`. Today [`tests_unit`](../../../noxfile.py) is the
   *no-testbed set*: `pytest tests/unit tests/e2e -m "not integration and not
   embedded" --cov-fail-under=85`. It already includes the no-testbed e2e test
   `completion_cache` and would auto-include any future no-testbed e2e test.
   So the name `unit` is doing double duty: CI no-testbed gate **and** the thing
   we want to mean level-unit.

## Decisions

| # | Decision |
|---|----------|
| D1 | **Two axes side by side.** Add a directory-based *level* axis; keep the marker-based *resource* axis (`unix`/`embedded`) unchanged. |
| D2 | **Level axis is cumulative by directory**, applied to the `coverage-*` and `nox-*` families only. |
| D3 | **Keep the `integration` marker** (auto-stamped from the directory). No removal. |
| D4 | **Mirror the auto-stamp for e2e.** Add `tests/e2e/conftest.py` that stamps a new `e2e` marker from the path; register `e2e` in `pyproject.toml`. Existing manual `integration`/`embedded` markers on e2e tests stay (they are the resource axis; `e2e` is the level axis — a test may carry both). |
| D5 | **`stability-*` family is untouched** (separate soak axis). |
| D6 | **Split the CI gate from level-unit.** `nox-unit`/`coverage-unit` become level-unit (`tests/unit`). A new no-testbed gate (`tests_hostless` / `coverage-hostless`) takes over the CI role; `ci.yml` and `nightly.yml` repoint to it. |
| D7 | **The CI gate selects the no-testbed set** — `pytest tests/unit tests/e2e -m "not integration and not embedded and not stability"`, identical to today's `tests_unit` set. The marker expression *already* captures `completion_cache` and any future `hostless`-marked e2e test (they live in `tests/e2e/` and are not integration/embedded). No CI rewiring is needed when the other agent's `hostless` marker lands — it becomes a positive convenience label. This keeps the change merge-order-safe and the coverage floor unchanged. |

## Target grid (end state)

### Axis 1 — Level (directory, cumulative)

Selection by path; carries `-m "not stability"`.

| make | nox session | pytest | Gate |
|------|-------------|--------|------|
| `coverage-unit` | `tests_unit` | `pytest tests/unit -m "not stability"` | report-only |
| `coverage-integration` *(new)* | `tests_integration` *(new)* | `pytest tests/unit tests/integration -m "not stability"` | report-only |
| `coverage` *(bare)* | `tests_all` | `pytest -m "not stability"` (all dirs) | **94 / 92** (unchanged) |

Partial-level targets are **report-only**, matching the existing `unix`/`embedded`
precedent (a single tier cannot meet the whole-repo floor). The authoritative
gates remain bare `coverage` (94) and the CI gate (below).

### Axis 2 — Resource (marker, orthogonal) — UNCHANGED

| make | nox session | pytest |
|------|-------------|--------|
| `coverage-unix` | `tests_unix` | `pytest -m "integration and not embedded"` |
| `coverage-embedded` | `tests_embedded` | `pytest -m "embedded"` |
| `stability-{unit,unix,embedded}` | — | unchanged |

### CI gate — no-testbed set (NEW, decoupled)

| make | nox session | pytest | Gate |
|------|-------------|--------|------|
| `coverage-hostless` *(new)* | `tests_hostless` *(new)* | `pytest tests/unit tests/e2e -m "not integration and not embedded and not stability"` | **90 (make) / 85 (nox)** — same set as today's `tests_unit`, so the floors are unchanged |

- `.github/workflows/ci.yml`: `nox -s tests_unit-<py>` → `nox -s tests_hostless-<py>`.
- `.github/workflows/nightly.yml`: the nox unit matrix repoints `tests_unit` → `tests_hostless`.
- `noxfile.py` `nox.options.sessions`: default `tests_unit` → `tests_hostless`, so a bare local `nox` equals the CI gate.

## Concrete changes

### `pyproject.toml`
- Add marker: `"e2e: end-to-end-tier test, auto-stamped from tests/e2e/ (level axis; orthogonal to the integration/embedded resource markers)"`.

### `tests/e2e/conftest.py` (new)
Mirror `tests/integration/conftest.py`:

```python
"""End-to-end tier conftest — auto-stamp the ``e2e`` marker from the path.

Mirrors tests/integration/conftest.py: the ``tests/e2e/`` directory is the
single source of truth for the e2e tier (level axis). Resource markers
(``integration``/``embedded``) stay explicit on the tests that need a testbed.
"""
from pathlib import Path

_E2E_ROOT = Path(__file__).parent


def pytest_collection_modifyitems(config, items):
    for item in items:
        if _E2E_ROOT in item.path.parents:
            item.add_marker("e2e")
```

(Verify it composes with the LIFO hook ordering note in
[`tests/conftest.py`](../../../tests/conftest.py); `add_marker("e2e")` is
additive and independent of the xdist_group logic there.)

### `Makefile`
- Drop `M_UNIT`; keep `M_UNIX`, `M_EMBEDDED`; add `M_HOSTLESS := not integration and not embedded and not stability`.
- Redefine `coverage-unit` → `pytest tests/unit -m "not stability"` (report-only).
- Add `coverage-integration` → `pytest tests/unit tests/integration -m "not stability"` (report-only).
- Add `coverage-hostless` → `pytest tests/unit tests/e2e -m "$(M_HOSTLESS)" --cov-fail-under=$(CI_COVERAGE_THRESHOLD)`.
- Leave bare `coverage`, `coverage-unix`, `coverage-embedded`, `stability-*` as-is.
- Update `.PHONY`, the help block (show both axes: *Level* unit/integration/(all) and *Resource* unix/embedded), and the `M_*` comment block.
- `ci` target: `validate COVERAGE_TARGET=coverage-unit` → `COVERAGE_TARGET=coverage-hostless`.

### `noxfile.py`
- `UNIT_TEST_ARGS` → level-unit (`tests/unit`, drop the `--cov-fail-under` from the level session).
- Add `tests_integration` (`pytest tests/unit tests/integration -m "not stability"`).
- Add `tests_hostless` (`pytest tests/unit tests/e2e -m "not integration and not embedded and not stability" --cov-fail-under=85`).
- Repoint `nox.options.sessions` default `tests_unit` → `tests_hostless`.
- Keep `tests_unix`, `tests_embedded`, `tests_all`.

### Workflows
- `ci.yml`, `nightly.yml`: `tests_unit` → `tests_hostless` in the matrix run lines.

### Docs / ripple sweep
- `docs/contributing.md` test-tier section.
- `scripts/stability_campaign.py` and any other hard-coded target/session names (grep the tree).
- (CHANGELOG is auto-managed — do not touch.)

## Coordination / merge-order safety

This change overlaps two in-flight efforts: the `makefile-test-target-rename`
branch and another agent's `hostless`-marker e2e work. Safety properties:

- The CI gate uses `not integration and not embedded and not stability`, so it
  **already runs any no-testbed e2e test** the other agent adds — no rewiring of
  `ci.yml` is required when their `hostless` marker lands. Their marker becomes a
  positive label that can *optionally* simplify the expression later.
- The e2e auto-stamp adds a *new* `e2e` marker and does not change the meaning of
  `integration`/`embedded`, so it does not disturb the resource targets.
- Makefile/noxfile edits will likely textually conflict with the rename branch;
  reconciliation is a merge-time concern for the controller (Chris).

## Verification plan

- **TDD** the e2e auto-stamp: a test under `tests/e2e/` is collected with the
  `e2e` marker; `-m e2e` selects the e2e tier; `-m "not e2e"` deselects it;
  `completion_cache` is `e2e` **and** still matches the hostless expression.
- `pytest --markers` lists `e2e`.
- `nox -l` shows `tests_unit`, `tests_integration`, `tests_hostless`.
- `make coverage-unit` (tests/unit) passes; `make coverage-hostless` passes at 90
  (identical set to today's `coverage-unit`, which passed at 90).
- Full gate per repo convention: `make coverage` (needs full lab, run on dev VM)
  + `make nox` + `make typecheck` + `make docs`.

## Open items (flag during spec review)

- **CI gate name:** proposed `hostless` (aligns with the other agent's marker).
  Alternative: `ci` (`coverage-ci` / `tests_ci`).
- **Level-target gates:** proposed report-only. Alternative: give
  `coverage-integration` a measured floor since it covers most of the suite.
- **Bare `coverage` form:** kept as `pytest -m "not stability"` (functionally =
  all dirs) rather than an explicit three-dir path list, to minimize churn.
