# The otto_kgcov compatibility matrix — design

**Date:** 2026-09-18
**Status:** approved in brainstorming (Chris, 2026-09-18: measured compilers only; a sibling of the host matrix, not a shared core; published beside the kernel-modules page); this document is the written form
**Depends on:** `2026-09-18-kgcov-toolchains-and-cross-compiling-design.md` (the `make kgcov` lane and its two proofs), `2026-06-25-pluggable-host-source-and-conformance-design.md` §5 and its artifact (`schemas/support_matrix.json`, `scripts/collate_support_matrix.py`, `scripts/render_support_matrix.py`, `scripts/check_matrix_downgrades.py`), whose rules this matrix copies

## 1. Problem

`make kgcov` proves otto_kgcov on gcc 9 to 14 and clang 18 on the beds, and cross-builds
it for x86_64, but the proof leaves nothing behind that a reader can consult: the
kernel-modules page says "any gcc from 4.7 to 15, or clang 11 and newer", which is the
vendored table's claim, not a measurement, and the parity between the two compiler
families rests on a green lane nobody outside the release sees. The host-class support
matrix (`docs/architecture/support-matrix.md`) answers the same kind of question for
hosts, cell by cell, with provenance, and its rules keep a verdict from being written by
hand. otto_kgcov has no such page.

## 2. Goal

A published matrix, `docs/cli/cov/instrumenting/kgcov-matrix.md`, that states per
compiler and per contract what the last measured `make kgcov` run observed, so that GCC
and clang parity is read off identical rows, with the host matrix's discipline: only a
run writes a verdict, every verdict carries its provenance, a lost verdict stops the
release.

## 3. Non-goals

- Compilers this dev VM cannot run: other clang versions, gcc majors outside 9 to 14, the
  vendored table's compile-only arms. The page states the table's range as the source's
  claim and the measured columns as measurements; it does not invent a third state for
  arms nobody ran.
- Any change to the host matrix's code (`tests/_fixtures/support_matrix.py`,
  `scripts/collate_support_matrix.py`, `scripts/render_support_matrix.py`). This matrix
  is a sibling with the same three rules, not a second consumer of a shared core.
  `scripts/check_matrix_downgrades.py` is reused as is: it is pure over two JSON files and
  reads only `cells[surface][profile].status`, which this artifact spells the same way.
- A CI path. The lane needs the beds and the dev VM's compilers; the observation records
  come from `make kgcov` there, nowhere else.

## 4. Design

### 4.1 Axes

**Rows (surfaces)** are the kgcov contracts, keyed by test nodeid without parameters:

| venue | id | contract |
|---|---|---|
| bed | `build-names-compiler` | `tests/e2e/cov/test_kgcov_toolchains_e2e.py::TestBuild::test_both_modules_name_the_requested_compiler` |
| bed | `build-init-array-bracket` | `…::TestBuild::test_the_init_array_is_bracketed_by_the_sentinels` |
| bed | `refuses-other-compiler` | `…::TestBuild::test_a_demo_from_another_compiler_is_refused_at_load` (new, §4.3; the column's positive control) |
| bed | `fetch-only-the-demo` | `…::TestCoverage::test_only_the_demo_is_fetched_from_the_two_hosts` |
| bed | `three-gcda-per-host` | `…::TestCoverage::test_three_gcda_per_host` |
| bed | `library-uninstrumented` | `…::TestCoverage::test_the_run_log_says_the_library_is_not_instrumented` |
| bed | `store-three-demo-files` | `…::TestCoverage::test_store_has_the_three_demo_files` |
| bed | `policy-hits` | `…::TestCoverage::test_policy_paths_have_the_expected_hits` |
| bed | `parse-hits` | `…::TestCoverage::test_parse_hits` |
| bed | `exit-routine-once-per-host` | `…::TestCoverage::test_exit_routine_lines_are_hit_once_per_host` |
| bed | `policy-branches` | `…::TestCoverage::test_branches_are_recorded_for_the_policy_switch` |
| bed | `mid-dump-no-double-count` | `…::TestCoverage::test_the_mid_suite_dump_does_not_double_count` |
| build | `cross-release` | `tests/e2e/cov/test_kgcov_cross_build.py::test_both_modules_carry_the_trees_release` |
| build | `cross-x86_64-objects` | `…::test_both_modules_are_x86_64_objects` |
| build | `cross-compiler-built` | `…::test_the_cross_compiler_built_them` |
| build | `cross-instrumented-bracketed` | `…::test_the_demo_is_instrumented_and_bracketed` |
| build | `cross-fixture-untouched` | `…::test_the_in_place_fixture_build_was_not_touched` |
| build | `cross-linked-against-library` | `…::test_the_demo_linked_against_the_library` |

The id and title of a row are labels and are written down, in
`tests/_fixtures/kgcov_matrix.py`; their agreement with the tree is not left to trust: a
discovery walk over `tests/e2e/cov/test_kgcov_*.py` collects every test that takes the
`built_with`, `coverage_run` or `cross_build` fixture, and a unit test asserts the two sets
are equal both ways.

**Columns (profiles)** are the compilers the lane measures: the bed columns are the
Makefile's default `KGCOV_TOOLCHAINS` (`gcc-9`, `gcc-10`, `gcc-11`, `gcc-12`, `gcc-13`,
`gcc-14`, `clang`), read from the `Makefile` by the fixture module rather than copied; the
one build column is `x86_64-cross`. A column id is the name the lane selects by, so the
`clang` column does not change id when the VM's clang does; the measured full version is
provenance on the cell (§4.4).

A row and a column meet only in the same venue. The artifact holds cells for those pairs
alone, so the page shows two grids: the bed grid (twelve rows by seven compilers), where
parity is read as the `clang` column against each `gcc-N` column, and the build grid (six
rows by one column).

### 4.2 Records

The kgcov e2e tree gains a `pytest_runtest_makereport` hook (`tests/e2e/cov/conftest.py`,
new; the tree has only `tests/e2e/conftest.py` today) that writes one JSON record per
(test, compiler) at the TEARDOWN report into `reports/kgcov-observations/`
(`PROJECT_ROOT`-anchored, git-ignored, removed by `make clean`, the shape
`tests/conformance/_observation.py` uses and for the same reasons: the outcome is read
from pytest's report, never from the test body, and a skip is its own outcome that no
collation reads as evidence). A record:

```json
{"kind": "observation", "format": 1,
 "nodeid": "tests/e2e/cov/test_kgcov_toolchains_e2e.py::TestCoverage::test_parse_hits",
 "profile": "gcc-12", "venue": "bed", "outcome": "passed",
 "compiler_version": "12.3.0", "kernel_release": "6.8.0-86-generic",
 "as_of": "2026-09-18", "run_id": "<one id per pytest session>"}
```

`profile` is the parametrised `built_with` id (`x86_64-cross` for the build module),
`compiler_version` is the compiler's `-dumpfullversion` (clang: `-dumpversion`) as the
build helper already probes it, and `kernel_release` is the release the modules were built
for (the bed's running kernel; the cross tree's `include/config/kernel.release`). The hook
takes both from the fixture's `Toolchain` value through the item's callspec, not by probing
again. Records from one session share a `run_id`, so the collate step can require the
control and the contract to come from the same run.

The old records of a directory are removed at session start by the same hook, so a folded
run is never contaminated by an earlier one's leftovers.

### 4.3 The positive control

A pass is evidence only if the instrument could have said no. Each bed column's control is
the library's own refusal, which spec `2026-09-18-kgcov-toolchains-and-cross-compiling`
§3 states and nothing measures today: a new test in `TestBuild`,
`test_a_demo_from_another_compiler_is_refused_at_load`, builds a second copy of the demo
with another compiler (for a `gcc-N` column the nearest other gcc major the lane's list
holds, so the same-major rule is what refuses it; for the `clang` column, the system gcc,
so the family rule is), loads this column's library on one bed host, attempts to load the
foreign demo, and asserts the load fails and the kernel log carries the documented text
(the registration refusal "compiled by gcc N, otto_kgcov by gcc M" or the loader's
`Unknown symbol`), then unloads the library. It runs BEFORE the coverage run, on a library
the coverage run then reloads, so nothing it leaves behind can leak into the counters.

The row `refuses-other-compiler` is both a contract (it appears in the grid) and the
column's control: the collate step refuses `measured-ok` for any cell in a column whose
control did not pass in the same `run_id`. The build column has no control and no
`measured-ok` requires one there; its cells say so in provenance (`"control": null`), and
the page's legend explains that a build-only column proves what a build can prove.

### 4.4 Artifact

`schemas/kgcov_matrix.json`, committed, validated against `schemas/kgcov-matrix.schema.json`:

```json
{"$schema": "./kgcov-matrix.schema.json", "format": 1,
 "surfaces": [{"id": "parse-hits", "title": "…", "venue": "bed", "contract": "<nodeid>", "control": false}, …],
 "profiles": [{"id": "gcc-12", "title": "gcc-12", "venue": "bed"}, …],
 "cells": {"parse-hits": {"gcc-12": {
     "status": "measured-ok", "nodeid": "<nodeid>", "venue": "bed", "as_of": "2026-09-18",
     "outcome": "passed", "compiler_version": "12.3.0", "kernel_release": "6.8.0-86-generic",
     "control": "<the control's nodeid>"}}}}
```

`status` is one of `measured-ok`, `measured-broken`, `untested`. An `untested` cell carries
`status` alone. The `cells` nesting and the `status` word are the host matrix's, which is
what lets `scripts/check_matrix_downgrades.py --baseline OLD --candidate schemas/kgcov_matrix.json`
gate this artifact unchanged.

### 4.5 Collate

`scripts/collate_kgcov_matrix.py`, run as `uv run python -m scripts.collate_kgcov_matrix`
(`-m`, for the `sys.path` reason the host collator gives), is the only writer of a
`measured-*` verdict. Report by default; `--write` folds. Rules, each structural:

1. A record naming a nodeid no surface holds, or a profile no column holds, is discarded
   and counted in the report with its reason.
2. A cell the run drew becomes `measured-ok` on `passed` and `measured-broken` on `failed`
   or `error`; a `skipped` record is discarded with its reason and moves nothing.
3. A bed cell becomes `measured-ok` only if the column's control record of the same
   `run_id` is `passed`; otherwise the cell is written `measured-broken` with the reason in
   the report, because a contract whose instrument did not demonstrate it could fail is
   not evidence.
4. A cell the run did not draw is copied across unchanged; the collator never downgrades
   what it did not measure.
5. Nothing here runs `git`.

`make kgcov` ends by folding (`$(MAKE) kgcov-matrix` after the pytest leg, the lane's exit
code preserved as `conformance-bed` does), so a hand run leaves the diff to review.

### 4.6 Render and publish

`scripts/render_kgcov_matrix.py` (`-m`; `--check` reports and writes nothing) renders the
artifact into `docs/cli/cov/instrumenting/kgcov-matrix.md`, git-ignored and generated by a
second `builder-inited` hook in `docs/conf.py` beside `_generate_support_matrix`, every
builder. The page:

- opens with what the matrix claims (what the last measured lane observed, on this dev
  VM's beds and cross tree, for the compilers installed there) and what it does not (the
  vendored table's range is the source's claim);
- the bed grid, rows in §4.1 order, one column per compiler, a cell rendered from its
  status with a symbol and the phrasing living in the renderer, never in the artifact;
- the build grid;
- provenance per column: measured compiler version, kernel release, `as_of`, and for a bed
  column the control's own status;
- a legend for the three states and for the control rule.

The kernel-modules page's "Another kernel, ISA or compiler" section links to it in one
sentence after the compiler list; `docs/architecture/support-matrix.md`'s renderer is
untouched, so the sibling link lives in the page that names it,
`docs/architecture/testing.md`'s conformance paragraph, and in `docs/contributing.md`'s
regression-category row for `make kgcov`.

### 4.7 Release

`make release-kgcov-matrix` mirrors `release-matrix`: snapshot `HEAD:schemas/kgcov_matrix.json`,
run `make kgcov` (which folds), `scripts/check_matrix_downgrades.py --baseline <snapshot>
--candidate schemas/kgcov_matrix.json`, and commit a changed artifact as
`chore(matrix): re-measure the kgcov matrix` with hooks disabled, exactly as the host
stage does. `make release` runs it in the position `kgcov` holds today. A downgrade stops
the release; the person then re-runs or records the break on purpose.

### 4.8 Guards (`tests/unit/test_kgcov_matrix.py`, default gate)

- The committed artifact validates against its schema; `format` is 1.
- Row axis equals the discovered contracts, both ways; column axis equals the Makefile
  default plus `x86_64-cross`, both ways; every `cells` key pairs a row and a column of
  the same venue and every such pair exists.
- Every `measured-*` cell carries the full provenance of §4.4; every bed `measured-ok`
  names a control nodeid that is the column's control surface.
- `render_kgcov_matrix --check` agrees with the committed artifact.
- The tier-marker invariants gain the row that `make release` invokes
  `release-kgcov-matrix` (in place of the existing "release invokes `kgcov`" row).
- The collator and the hook are unit-tested over synthetic records and a synthetic
  matrix: each of §4.5's five rules has a test that fails without it.

## 5. Files

- New: `tests/_fixtures/kgcov_matrix.py` (axes, discovery, schema/matrix paths,
  `rewrite_matrix_axes`), `tests/e2e/cov/conftest.py` (the record hook),
  `tests/e2e/cov/_kgcov_observation.py` (record shape and writer),
  `scripts/collate_kgcov_matrix.py`, `scripts/render_kgcov_matrix.py`,
  `schemas/kgcov-matrix.schema.json`, `schemas/kgcov_matrix.json` (axes with every cell
  `untested` at first; the first `make kgcov` fills it), `tests/unit/test_kgcov_matrix.py`,
  `tests/unit/test_collate_kgcov_matrix.py`.
- Modified: `tests/e2e/cov/test_kgcov_toolchains_e2e.py` (the control test),
  `tests/e2e/cov/_repo5_build.py` (a foreign-demo build and the version/release fields on
  `Toolchain`), `Makefile` (`kgcov-matrix`, `release-kgcov-matrix`, `kgcov` folds,
  `release` order), `docs/conf.py`, `.gitignore` (the generated page and
  `reports/kgcov-observations/`; the schema and artifact stay tracked as the host matrix's
  are), `docs/cli/cov/instrumenting/kernel-modules.md`, `docs/architecture/testing.md`,
  `docs/contributing.md`, `tests/unit/test_tier_marker_invariants.py`.

## 6. Tests

Unit: §4.8. Live: one `make kgcov` on the dev VM after the change fills the artifact; the
resulting `schemas/kgcov_matrix.json` is committed with the branch as the first measured
state, and `make docs` renders the page from it.

## 7. Compatibility

Nothing existing changes shape. The host matrix, its scripts and its schema are untouched;
`check_matrix_downgrades.py` gains a second caller, not a change. `make kgcov` keeps its
knobs and its exit code and additionally folds.
