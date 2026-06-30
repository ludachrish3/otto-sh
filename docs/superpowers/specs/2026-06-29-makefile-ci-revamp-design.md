# Makefile & CI revamp — design

**Date:** 2026-06-29
**Status:** approved pending review

## Goal

Fold the new **linting** and **profiling** capabilities into the release flow and
CI, fix a coverage-threshold drift between `nox` and the `make` targets, and
declutter the `make help` menu. Four TODO items drive this (from `todo/TODO.md`):

1. Add profiling and linting to `make release`.
2. `nox` minimum coverage is 92% (like `make coverage`). **Only the `*-unit`
   targets carry the 85% floor.**
3. The help menu is unwieldy — collapse the granular docs sub-targets and group
   the rest.
4. Add a lint check to the nightly and main-branch CI checks.

This is a focused build-tooling change. No production/library code changes.

## Current state (the gaps)

- **`make release`** chains `clean-dist → typecheck → docs → nox → bump → build`.
  It never runs `lint` (and `make nox` = `nox -s tests_all`, which does **not**
  include the `lint` nox session), and never runs `profile`.
- **nox thresholds drift from the make targets:**
  - `noxfile.py` `tests_all` gates at **85%** (should be 92%, matching `make coverage`).
  - `noxfile.py` `tests_unit` gates at **80%** (the make `coverage-unit` equivalent
    gates at **85%**).
- **`make help`** prints a hardcoded `Testing` block plus a flat, grep-driven
  `Other targets` list (~24 entries), including five granular `docs-*` /
  `doctest*` sub-targets.
- **`ci.yml`** runs `tests_unit`, `typecheck`, `docs` jobs — **no lint job** —
  even though `nox -s lint` exists and is in `nox.options.sessions`.
- **`nightly.yml`** runs a stability matrix and a unit matrix — no lint.

Note on the import budget: the **gate logic** (denylist + non-stdlib count cap +
golden otto-module snapshot) currently lives **only** in the
`tests/unit/import_budget/test_import_budget.py` test, which runs inside
`tests_unit` (CI) and `tests_all` (`make release` via `make nox`). The standalone
`make profile` (`scripts/import_budget.py --hyperfine`) **does not** enforce
those caps — `main()` always returns 0; it just prints the per-surface count
table + hyperfine wall-clock. That is the gap this design closes: `make profile`
should itself be able to **fail** on a module-count / snapshot / denylist
regression (wall-clock never gates — it is host-dependent noise), so it can serve
as the *performance* gate at the end of the release ordering: **well-formed
(lint/typecheck) → functional (coverage/nox) → performant (profile)**.

## Changes

### 1. `make release` — add lint + profile

Insert two steps into the existing `&&` chain:

- `$(MAKE) lint` immediately after `clean-dist` (fast fail-fast check before the
  heavy typecheck/docs/nox steps).
- `$(MAKE) profile` immediately after `nox` and before the version bump — the
  **performance gate**, last in the well-formed → functional → performant
  ordering. It now **gates** (see §1a): a module-count / snapshot / denylist
  regression fails the release before the bump. It also prints the hyperfine
  wall-clock (informational; never gates). Its `~30–60s` runtime is negligible
  against the full multi-Python nox matrix that precedes it.

Resulting order:
`clean-dist → lint → typecheck → docs → nox → profile → changelog → bump → build`.

### 1a. Make `make profile` actually gate (the import budget)

Today the cap / snapshot / denylist checks live only in `test_import_budget`. To
make `make profile` a real gate **without duplicating** that logic:

- **Extract a shared checker** in `scripts/import_budget.py`:
  `check_surface(surface, result) -> list[str]` returning human-readable
  violation strings (empty list = pass). It runs the same three checks the test
  does — denylist, `len(non_stdlib) <= surface.cap`, exact golden snapshot —
  against the same `SURFACES` / `cap` data and `read_snapshot`.
- **Test delegates to it:** `test_import_budget` becomes
  `violations = harness.check_surface(surface, result); assert not violations,
  "\n".join(violations)`. One source of truth — the script and the test cannot
  drift.
- **Add a `--check` flag** to `main()`: measure each surface, run
  `check_surface`, print the table (annotating any violation), and after the full
  pass `return 1` if any surface failed. `--check` and `--hyperfine` are
  independent: `--check` gates on counts/snapshots/denylist; `--hyperfine` only
  adds the wall-clock report. The flagless `python scripts/import_budget.py`
  stays report-only (ad-hoc eyeballing).
- **`make profile`** becomes `... import_budget.py --check --hyperfine` — it
  enforces the budget AND prints wall-clock. Its `##` help text changes from
  "read-only" to note that it now enforces the import budget.

The cap gate therefore runs in two independent places — the unit test (CI /
coverage / nox) and `make profile` (release) — both reading the same
`check_surface`. The redundancy is intentional: a dev can run `make profile` as a
standalone performance gate without the whole suite, and release fails fast at
the performant step on a perf regression.

### 2. nox coverage thresholds — align with the make targets

In `noxfile.py`:

- `tests_all`: `--cov-fail-under=85` → **`92`** (matches `make coverage` /
  `COVERAGE_THRESHOLD`).
- `tests_unit` (`UNIT_TEST_ARGS`): `--cov-fail-under=80` → **`85`** (matches
  `make coverage-unit` / `CI_COVERAGE_THRESHOLD`; the `*-unit` floor).

Both `*-unit` paths now share the 85% floor; every full-suite path gates at 92%.
A short comment in `noxfile.py` records the "keep in sync with the Makefile
`COVERAGE_THRESHOLD` / `CI_COVERAGE_THRESHOLD`" contract, mirroring the existing
marker-sync comment. Current measured coverage (~88% unit, ~92.5% full) clears
both new floors.

### 3. `make help` — sectioned + collapsed docs

Keep the existing `Testing` block as-is (it already groups the
unit/unix/embedded matrix with its scope legend). Replace the flat
`Other targets` grep with **labeled sections**, in this fixed order:

```
Build & Release   all, ci, validate, build, changelog, release
Quality           lint, format, typecheck
Docs              docs   (collapsed; sub-targets named in its description)
Lab               vm-health, qemu-restart
Dev               dev, profile, schema, import-snapshot, clean
```

**Mechanism — opt-in section tag in the `##` comment.** Each target that should
appear in `Other` gets a `(Section)` prefix on its `##` description, e.g.:

```make
lint: ## (Quality) Run ruff lint + format checks (part of validate/ci/all)
```

The help recipe's `awk`:
- matches only lines of the form `target: ## (Section) description`,
- extracts the section name and the remaining description,
- buckets targets by section and prints sections in the fixed order above.

This keeps each description next to its target (single source of truth, no
drift), and makes the section tag the **opt-in** mechanism — untagged `## `
targets (the `nox-*`/`coverage-*`/`stability-*`/`repeat` matrix, shown via the
Testing block) and tag-less targets are automatically excluded. The old
`grep -vE '^(nox|coverage|stability)...'` exclusion list is **removed** —
opt-in by tag replaces opt-out by name.

**Docs collapse.** Drop the `##` comment from `docs-lint`, `docs-html`,
`doctest`, `doctest-src`, and `docs-inventories` (they remain valid targets,
just unlisted), and enrich the `docs` `##` to name them, e.g.:

```make
docs: docs-lint docs-html doctest doctest-src ## (Docs) Build HTML docs + Sphinx & src doctests (sub-targets: docs-lint, docs-html, doctest, doctest-src, docs-inventories)
```

**Also unlisted:** `hyperfine` loses its `##` (it is an internal sub-step that
`dev` and `profile` auto-invoke as a prerequisite; both mention it in their own
descriptions). `clean-dist` is already unlisted (no `##`).

### 4. CI — add lint to main-branch CI and nightly

**`ci.yml`:** add a `lint` job mirroring the existing `typecheck`/`docs` jobs
(checkout → setup-uv → `uv python install 3.10` → `uv run nox -s lint`). Add
`lint` to the `report-failure` job's `needs`, and update its issue body text
from "tests / typecheck / docs" to "lint / tests / typecheck / docs".

**`nightly.yml`:** add a single (non-matrix) `lint` job with the same steps. Add
`lint` to the `report-failure` job's `needs`, and lightly adjust its issue body
to mention lint.

No CI profiling step is added: the import-budget gate (now centralized in
`check_surface`) already runs in CI as part of `tests_unit` (the
`tests/unit/import_budget/` suite). `make profile`'s *additional* output is the
hyperfine wall-clock (host-dependent, noisy) — that belongs only in the
human-driven `make release`, per TODO item 1.

## Out of scope

- Other TODO items (option-decorator discoverability, lab renames, `stat()`,
  etc.). Strictly the four build-tooling items above.
- No new Makefile targets and no target renames — only recipe edits, `##`
  comment edits, and the help recipe rewrite.

## Testing / verification

This is build-tooling; verification is by running the affected commands:

- `make help` — visually confirm the sectioned layout, the collapsed `docs`
  line, and that no granular `docs-*`/`hyperfine` lines appear.
- `make lint` — confirm it runs standalone (unchanged recipe).
- `make profile` — confirm it now runs `--check` (gates) + hyperfine; verify a
  deliberately-lowered cap makes it exit non-zero, then that the real caps pass.
- `uv run pytest tests/unit/import_budget` — confirm the refactored test
  (delegating to `check_surface`) still passes across the surfaces.
- `make coverage-unit` and a scoped `uv run nox -s tests_unit-3.10` — confirm
  the raised 85% nox-unit floor passes.
- `uv run nox -s tests_all` is **not** run here (needs the full lab); the 92%
  change is asserted by inspection against the known ~92.5% full-suite figure,
  and Chris runs the full `make release` / `make nox` on the bed pre-merge.
- `make release` is **not** executed (it bumps the version and builds dist) —
  the chain edit is reviewed by inspection; the individual inserted steps
  (`lint`, `profile`) are verified to run standalone.
- CI YAML: validate by inspection (and the next push exercises the new `lint`
  job). The lint job is a structural mirror of the green `typecheck`/`docs` jobs.

## Risks

- **awk in a Make recipe** is finicky (escaping `$$`, line continuations). Test
  `make help` output directly after the edit; the section ordering and tag
  parsing must render correctly under the repo's `NO_COLOR`-free terminal.
- **Raising nox `tests_unit` to 85%** could fail if a Python version in the
  matrix dips below 85% where 80% passed. Margin is ~3pt (measured ~88%);
  coverage is largely version-independent. Watch the CI matrix on the first run.
- **`check_surface` refactor** must be behavior-preserving: the test's three
  assertions are folded into one delegated call, so the unit test must stay green
  with identical failure semantics. Verify with `pytest tests/unit/import_budget`
  before and after.
