# #566 batch 2 — test burden and gating policy: design

Owner rulings (Chris, 2026-10-13 evening; memory feedback_566_decisions_2026_10_13):
1 docs/spec/web/test-only diffs get a focused retest; 2 `interpreter_agnostic` tests STAY in `make coverage`
and are not repeated by any other interpreter lane in the same gate set (clarified 2026-10-14); 3 release completeness first; 4 if coverage passed, release's
nox matrix runs 3.11–3.14 only; 5 no canary coverage floor; 6 import-budget growth fails the release, run
early; 7 per-commit scoped gates are the policy, the full gate is for the tip. Plus the agreed small items
(AGENTS.md command fix + `make quick`, show_contexts on, drop duplicate release `build` + `profile`, matrices
before docs, #9 differential reuse, drop `--doctest-modules`).

Evidence base: the 2026-10-13 21:47 gate (tree fe020ee00): `make coverage` 908 s (main leg 697 s, 18,359
cases / 3,123 s summed); pre-squash gate ≈ 29 min; the `interpreter_agnostic` family = 57 modules, 1,980
cases, 693 s summed in the coverage leg and 401 s in the 3.14 canary; the coverage floor without that
family, computed from the gate's own per-test contexts, is **96.43 %** (96.56 % with; 95.5 gate) — the
only files that lose lines are `_shim_complete.py` (−42 lines, the generated corpus's edge shapes),
`_shim.py` (−1) and `cli/main.py` (−5).

Non-goals: no product-code caches for test speed; no test is deleted or skipped. Stated consequences of
ruling 2: CI's `tests_hostless-3.14` job (ci.yml:109) and nightly's `unit-matrix` 3.14 leg (nightly.yml:
141–144, `--count=5`) no longer run the family; CI's 3.10 job does, once.

---

## Phase A — Release completeness (first)

**Problem.** `make release` runs none of `stability*`, `chaos`, `chaos-embedded`, `dashboard-soak`,
`conformance` (hermetic) or `busybox`; `make stability` runs tier 1 under `-@` (failure ignored,
Makefile:1266). "Releases lose no tests" has no mechanism. Until it does, "save it for release" means
"never".

**A1 — a release census, as a test.** It lives INSIDE tests/unit/test_lane_invariants.py, so it shares the
module-scoped `collect_marker_sets` child collections and the `lane_leg_membership` xdist group (a new
module would pay another whole-tree collection, 12–15 s instrumented). It does NOT interpret make: a literal RELEASE_LANES manifest in the test
lists every pytest leg release reaches as (make target, roots, `-m` text, venue), and two pins hold it to
reality: (i) each row's target is reached by `release` through a `$(MAKE) X` + prerequisite walker over
`_makefile_recipes()` (the flat target→recipe map, test_tier_marker_invariants.py:916; G12c at :2025–2054
already walks two hops by regex for one stage — generalised, since `release → release-campaign →
stability → stability-unit` is three hops), and the row's roots + `-m` appear verbatim in that target's
recipe; a nox row names its `python`, and the pin reads that session's branch AST the way
`test_the_hostless_session_runs_each_pair_on_its_own_pythons` does (the flat extractor cannot tell
branches apart, test_python_matrix_tiering.py:214–218); `make coverage`'s legs are `venue: stamp` rows
pinned against the `coverage-python`/`dashboard` recipes (Makefile:755–760, 881–884) rather than release
reachability; (ii) the item-level census: one `--collect-only` child per
distinct root set (a plugin dump of **node id + resolved markers**, extending `_MARKER_DUMP_PLUGIN`
which today emits marker names only, test_lane_invariants.py:703), each row's `-m` evaluated with
pytest's `Expression` against each item's markers, and the union over rows must equal every collected
item of `tests/unit`, `tests/integration`, `tests/e2e`. Trees whose collection depends on venue or
sampled cells — `tests/conformance` (conftest.py:86, `_cells.py:417`) and `tests/busybox` — are covered
at LANE level instead: the manifest must contain `conformance` (hermetic, `OTTO_CONFORMANCE_CELLS=all`),
`conformance-bed`, `busybox` and `doctest-src` (the src doctests run through the `docs` dependency,
outside `testpaths`). `collect_marker_sets` is session-scoped (test_lane_invariants.py:868) and its dump plugin gains the node
id (children run `-n0`, so no `@group` suffix); its return type becomes (nodeid, markers) and
`_lane_leg_offenders` adapts. The allowed-exception list is a literal, empty at landing —
the FIRST implementation step runs the census and reports the delta before A3 is wired. Collection proves
selection, not execution: skip/xfail outcomes are a lane's own business.

**A2 — `make stability` fails when any tier fails.** Today tier 1 is ignored (`-@`, Makefile:1297) and a
red tier 2 stops tier 3 (plain `$(MAKE)` lines, Makefile:1316–1322). New: every tier runs, each rc is
recorded, the target exits non-zero at the end if any failed, naming them.

**A3 — `make release-campaign`,** a new stage, a plain recipe of `$(MAKE)` lines: `stability COUNT=1`,
`dashboard-soak`, `conformance` (hermetic, `OTTO_CONFORMANCE_CELLS=all`), `busybox` (artifact tier;
cache-warm), then `chaos` and `chaos-embedded` LAST (exclusive bed; `chaos-embedded` can wedge a board).
COUNT=1 because "no tests lost" is a selection property; soak multiplicity stays nightly's job (Q3).
`scripts/stability_campaign.py` (the graduated 1→3→10 ladder) is NOT wired into the release and is not
deleted: the owner keeps it as the opt-in campaign "for large, risky changes where the test burden is
worth the cost" (2026-10-14); its two pins (test_tier_marker_invariants.py:1799, :2129) stay as they are.
`conformance-bed` is already `release-matrix`.

**A4 — release order** (also Phase C): coverage-stamp check (C2, ~1 s — a stale stamp must not be found
after a 7–33 min budget delta) → check-python → budget-delta (C1) → nox-release (C2) → web → dashboard-all → validate-ts → release-matrix → release-kmodcov-matrix →
release-campaign → docs → bump → wheel-check. Matrices before docs (the schema JSONs are Sphinx inputs;
pins test_support_matrix.py:4060, test_kmodcov_matrix.py:444). Chaos lanes last among bed lanes (exclusive
bed; `chaos-embedded` can wedge a board, so nothing bed-bound follows it). `make -n release` stays unsafe
as today (documented).

**Cost.** Unmeasured: no JUnit for the stability/chaos lanes exists on disk. First implementation step
is one measured run of each lane at COUNT=1, reported before the stage is wired in.

## Phase B — The `interpreter_agnostic` family runs on the primary interpreter only

**Owner clarification (2026-10-14):** the family STAYS in `make coverage` (3.10, instrumented; selection
and the 95.5 floor untouched, 96.56 % measured). What goes is its repetition by other interpreter lanes in
the same set of gates. Today those are the two 3.14 bookends: `nox -s tests_hostless-3.14` in the local
gate (1,980 cases, 401 s summed, ~105 s wall of its 497 s) and `tests_all-3.14` in release. The 3.11–3.13
middle pair already drops it (`HOSTLESS_MIDDLE_*`).

**B1 — the rule, in a scanner-visible shape.** Only the catch-all sessions can select the family —
`tests_unit`, `tests_integration`, `tests_hostless`, `tests_all` (the positive-selector sessions
`tests_unix`/`tests_embedded` cannot reach tests/unit items; `test_unit_tier_has_no_vm_markers`) — and
each of those gets a three-branch `if session.python == PRIMARY_PYTHON / elif == CANARY_PYTHON / else`
(the shape `test_the_hostless_session_runs_each_pair_on_its_own_pythons` already accepts,
test_python_matrix_tiering.py:211–257) over NAMED module constants, because `_python_pytest_invocations`
drops computed arguments (test_tier_marker_invariants.py:665–729) and inline literals would be
duplicated per session. New constant pairs per session family, e.g. `UNIT_TEST_ARGS/UNIT_SERIAL_ARGS`
(primary, with the family) and `UNIT_NONPRIMARY_TEST_ARGS/…_SERIAL_ARGS` (without), all added to
`_CONSTANT_PAIRS` (test_lane_invariants.py:217–221) so `noxfile_serial_lane_gaps` recognises them.
`HOSTLESS_CANARY_*` becomes the non-primary bookend pair without the family; `HOSTLESS_MIDDLE_*` and
the canary pair collapse to one non-primary pair unless a middle-only trim remains (today the middle
pair also drops `e2e` — kept as its own constant). `tests_all` on 3.11–3.13 (today the 92 floor on all
five via `nox-full`) becomes the same non-primary shape without coverage (C3). `make coverage` and every
`coverage-*` leg are unchanged.

**B1a — two mis-marked shim tests leave the family.** tests/unit/shim/test_refresh_spawn.py imports and
exercises `otto._shim_complete` (a product test, 10 cases, 0.2 s) and tests/unit/shim/test_differential.py
runs the completion shim — stdlib code the USER's interpreter executes — against Typer in-process: both
results depend on the interpreter, which is the opposite of the marker's meaning, and the differential
is the sole cover of 42 `_shim_complete.py` lines. Both lose `pytestmark = interpreter_agnostic` and run on
every Python (the differential costs ~86 s summed, ~20 s wall, per non-primary interpreter); the family
becomes 55 modules. `tests_unit_repeat` (CI's per-push `nox-unit-repeat`) deselects the marker precisely
to keep the differential out of its ×2 single-process repeat (~8 min each, pinned by
`test_the_isolation_repeat_deselects_the_marker`, test_python_matrix_tiering.py:258–279), so that
session gains `--ignore tests/unit/shim/test_differential.py` (`--ignore` is in `_VALUE_FLAGS`, so the
scanners see it) and that pin is rewritten; `_LOAD_BEARING_CARRIERS` (:53) changes. Q7 asks the owner
to confirm.

**B2 — pins and docs.** test_python_matrix_tiering.py: `canary_gaps`,
`test_the_canary_pair_is_its_bookend_twin_without_coverage` and `test_the_canary_scanner_observes_red`
invert ("the canary pair is the bookend pair minus the family and minus coverage"); a new pin: every
catch-all session's non-primary branch carries `not interpreter_agnostic`, the primary branch does not.
test_lane_invariants.py: `_CONSTANT_PAIRS` extended; its `tests_unit_repeat` docstring. The marker text in
pyproject ("runs once per gate set, on the primary interpreter"); ci.yml:81 comment; docs/architecture/
testing.md:329 ("the bookends still run all of it" → no longer), quality-gates.md:263, docs/contributing.md:
583 and 758. No file moves: the `tests/invariants` reclassification remains the separate, deferred item.

**B3 — gates.** Pre-squash/tip full gate = `make coverage` + `nox -s tests_hostless-3.14` (now ~105 s
shorter) + `make check-python` + `make docs` + `make gate-fresh`.

**Expected effect.** Local gate −~85 s wall (the family's ~105 s less the differential's ~20 s that
stays); release `tests_all-3.14` likewise; `make coverage` unchanged (908 s).

## Phase C — Release pipeline

**C1 — import-budget delta, early, failing.** Land the kit (measure_minors.py, budget_tool.py,
bucket_shuffles.py, repro_check.py + their self-tests) as `scripts/import_budget_delta.py` with
subcommands `measure`, `validate`, `tables`, `judge`; `make budget-delta` = measure base (`git describe
--tags --abbrev=0`) vs tip (HEAD) in pristine worktrees, 3.10 first then 3.11–3.14, each surface's file
ops by bucket; `judge` exits 1 on any growth that is not an owner-approved surface (`--approve`), on any
unattributed growth, or on a bucket shuffle that reproduces (the kit's rule). Base tables are cached
under `reports/budget/<base-tag>/` keyed by tag + interpreter (the base never changes between release
attempts; a pristine worktree + `uv sync` per interpreter is the cost). Order in the recipe: right after
`check-python`, minors sequential so the 3.10 pair (~7 min) fails first; the full pair is ~33 min
(Q4: twice, per the kit's noise rule, or once with a confirming second run only when growth appears).

**C2 — the coverage stamp and `nox-release`.** The stamp is a COMMITTED-tree contract, the same one
gate-fresh uses (scripts/gate_fresh.py:506–515): `make coverage` records at START `{tree: git rev-parse
HEAD^{tree}, python, threshold: COVERAGE_THRESHOLD, selection: each leg's roots + -m text, assets: sha256
over the built dist tree src/otto/_webassets/monitor/dist (there is no manifest file)}`, deletes any
prior stamp for that tree (make aborts at the first failing line, so "a failed leg deletes the stamp" is
only implementable as delete-at-start), and refuses to stamp — the legs still run — if any tracked file
is modified or staged, if any UNTRACKED file lies under `src/` or a `testpaths` root (unlike gate-fresh,
coverage runs the live checkout and would import it; untracked files elsewhere are listed), or if
`COVERAGE_THRESHOLD`/`DASHBOARD_BROWSERS`/`COVERAGE_WORKERS` came from the command line (the
`$(origin …)` idiom, Makefile:1536); at the END of the last leg it re-reads tree and dirt,
requires equality, and writes the stamp atomically (temp + rename) only when every leg passed, under
`$(git rev-parse --git-common-dir)/otto-coverage-stamps/<tree-hash>.json` — shared by every worktree of
the clone, so a tree gated in a worktree releases from the main checkout (`reports/` is per-checkout and
gitignored, .gitignore:21); any failed leg deletes the stamp for that tree. `make release` runs `scripts/coverage_stamp.py --check`
first: a stamp for HEAD^{tree} exists, tracked tree clean, same python, same selections, same assets →
`nox-release` = `tests_all-3.14 + tests_hostless-3.11/3.12/3.13` (all without the family, B1); otherwise
release runs `make coverage` itself, before any release commit, then the same nox-release (Q5: ruling 4
said "IF coverage passes" the matrix shrinks, i.e. otherwise the 3.10 selection runs — and `make coverage`
IS that selection clause-for-clause plus dashboard/TS/floor; Fable prefers refusing with the exact
command; Astra and I prefer running it). `make nox` stays as is (its recipe text is pinned,
test_python_matrix_tiering.py:281–289); `nox-release` is a new target beside it. `release-matrix`'s commits come
after nox, so the hash checked is the tree the developer gated. The budget delta (C1) measures the same
HEAD, so both judge one candidate. Ambient `PYTEST_ADDOPTS` is cleared for the stamped legs so an
environment override cannot narrow a selection the stamp names.

**C3 — canary without coverage.** `tests_all-3.14` runs `--no-cov` with no floor (`TESTS_ALL_CANARY_*`
constants, the fc6906813 pattern); the 92 floor is deleted from noxfile and its docs; test_coverage_floors
pins "primary enforces, canary does not".

**C4 — duplicates.** `profile` leaves the recipe (its 38 checks run inside tests_all-3.10 / coverage and
C1 is the stronger check); the trailing `build` goes (`wheel-check` already builds and checks the wheel
that is published).

## Phase D — Policy and the small items

**D1 — `make quick` and `make quick-guards`.** Two literal targets, no `M` variable (a `$(M)` in a leg
is unexpandable by `_makefile_marker_variables`, test_tier_marker_invariants.py:394, and
`Expression.compile` on it fails the lane-leg census, test_lane_invariants.py:901). `make quick
PATHS=<dirs>` = `pytest $(PATHS) -m "$(M_QUICK)" --no-cov -n $(QUICK_WORKERS)` + the serial twin `-n0`;
`M_QUICK := not serial_timing and not stability and not busybox and not conformance and not kmodcov and
not interpreter_agnostic` (a parallel leg must keep `not serial_timing`: tests/conftest.py:562 errors a
serial_timing test on a worker). `make quick-guards` = `pytest tests/unit -m interpreter_agnostic
--no-cov -n $(QUICK_WORKERS)`, single-legged (no carrier is serial_timing). `QUICK_WORKERS ?= min(4,
nproc)`; `_LEAK_ENV` on; `-p no:randomly` NOT forced. `quick` is parametric, so it is exempt from the
fixed-lane nonempty-leg pin; it tolerates exit 5 on ONE leg (`|| [ $$? -eq 5 ]`, printing which) but
fails when both legs selected nothing — an entirely empty retest is never green; fixed lanes keep the pin.

**D2 — `make gate-scope`** (`scripts/gate_scope.py --base origin/main`): classifies the whole diff from
the base and prints the exact commands for THIS commit and for the TIP. Data lives in the script, pinned
by a test that every `src/otto` package, every `tests/*` tree, `scripts/`, `web/`, `docs/` and the
build files have a row, and that every guard family (the `interpreter_agnostic` modules, `test_error_base`,
`test_declared_additions`, init-template, import_budget, api_snapshot) is a rider somewhere. Classes,
first match wins, union over files, unknown path → `full`:

| class | files (all must match) | per-commit | tip (landing) |
|---|---|---|---|
| none | docs/superpowers/specs/**, todo/** | nothing | nothing |
| docs | docs/** (not conf.py, `_ext/`, examples, captures scripts), *.md at root except AGENTS.md | `make docs` | `make gate-fresh` |
| web | web/src/**, web test files (not api/*.gen.ts, package*.json, vite/biome configs) | `make check-ts test-ts` | `make check-ts coverage-ts gate-fresh` |
| tests-leaf | tests/<tree>/<pkg>/test_*.py (no conftest, no `_fixtures`, no deleted test functions, no marker edits) | `make quick PATHS=<pkgs>` + ruff + `make lint-arch`; a changed module whose `pytestmark` carries `interpreter_agnostic` (29 of the 57 live under packages) runs `make quick-guards` at the commit instead of `quick` for that file | same + `make quick-guards` + `make gate-fresh` |
| src-leaf | src/otto/<pkg>/** in ≤ 2 packages, no trigger file | `make quick PATHS=tests/unit/<pkg> <mapped e2e/integration pkgs>` + `ty check <files>` + ruff + `make lint-arch` + `make quick-guards` | full gate |
| scripts | scripts/** | `make quick-guards` + ruff/ty | full gate |
| escalate | any conftest.py, tests/_fixtures/**, src/otto/{__init__,registry,invocation,context,bootstrap,lifecycle,_shim*}.py, any package `__init__.py`, pyproject/Makefile/noxfile/.coveragerc/ci.yml/.githooks, > 2 src packages | full gate at this commit (+ `make quick-guards` when build files changed; + `nox-unit-repeat` when conftest/_fixtures/registry changed) | full gate |

Every class, `none` included, also runs `make check-breaking` (~1.2 s per commit in range; it judges
each unpushed commit against its parent, so the mark is demanded at the commit that needs it). It stays
in `lint-arch`, gate-fresh, the pre-push hook and CI as today.

Ranges: the per-commit classification is of `HEAD~1..HEAD` (the commit); the tip classification is of
`$(git merge-base origin/main HEAD)..HEAD` (the landing). Both refuse a dirty tracked tree (commit
first), the same rule as gate-fresh. Specialized trees route to their own lane, never to `quick`:
`tests/conformance/**` → `make conformance` (hermetic) at the commit and `conformance-bed` at the tip;
`tests/integration/busybox_bed/**` → the bed lane whose recipe names that directory (not the
artifact-tier `make busybox`, which selects the `busybox` marker, Makefile:930–932); a test file carrying `stability`/`chaos` →
the lane whose `_selected_roots` contain the file (`stability-unit` names four explicit files,
Makefile:1237–1240, so a tree-level route would miss a new one), else `full`; `kmodcov` → `make kmodcov`; `browser` →
`make dashboard`. "No deleted test functions, no marker edits" is decided by an `ast` diff of each changed test file
(`git show <base>:<path>` vs the committed side): the set of class-qualified test identities (`Class.test_x`,
`test_y`; classes carry identity too — a renamed class hides every method) must be a superset of the
base's and every marker decorator/`pytestmark` unchanged; a removal or rename (a rename is a delete) or
a marker edit makes the class `tests-structural` = `tests-leaf` +
`make quick-guards` (the census, A1) at the commit.

"Full gate" = `make coverage` + `nox -s tests_hostless-3.14` + `make check-python` +
`make docs` + `make gate-fresh` (+ the bed lane a diff under host/link/tunnel/docker/console names).
Source→bed map (data in the script): cli→tests/e2e/cli; host→tests/integration/host, tests/e2e/host,
tests/e2e/console; docker→tests/e2e/docker, tests/integration/test_docker_*; link/tunnel→tests/e2e/
test_link_*, test_tunnel_*; cov/coverage/kgcov/kmodcov→tests/e2e/cov, tests/integration/cov;
monitor→tests/e2e/monitor; suite→tests/e2e/suite, tests/integration/suite; init→tests/e2e/test_init_e2e;
config/inventory/labs/lab→tests/e2e/config; session→tests/integration/host; logger→tests/integration/logger.

**D3 — AGENTS.md.** The squash-aware bullet becomes: "Gates are scoped by the diff. Run `make gate-scope`
and the lanes it names for every commit; the full gate runs once at the tip of a landing and per commit
only for the `escalate` class. Unsure → `make gate-scope` is the answer, not a bigger gate." The
`uv run pytest tests/unit -q` sentence goes (it was instrumented and errored `serial_timing`).

**D4** `.coveragerc [html] show_contexts = True` (+17 s render, +114 MB; measured).
**D5** `--doctest-modules` leaves addopts (0 doctests collected in tests/). The sibling-conftest regression
today INHERITS the flag from addopts (`_CHILD_ARGS`, test_conftest_doctest_collection.py:33–35), so the
same commit adds `--doctest-modules` to `_CHILD_ARGS` explicitly and the comment there flips from "stay
on" to "passed here"; test_lane_invariants' collection children do NOT add it — they must collect what real lanes collect
(its comment at :711–713), and after D5 no real lane passes the flag; that comment is rewritten; `doctest-src` and nox `docs` already restate the flag. `collect-check` (Makefile:766, gate-fresh)
passes `--doctest-modules` explicitly too: it is what makes collection IMPORT the 48 non-test `.py`
helpers under `testpaths`, which is the cheap half of what gate-fresh exists to catch. That keeps the
bare-conftest hazard reachable (collect-check walks tests/ with the flag), so `pytest_ignore_collect`
(tests/conftest.py:351) and its regression stay. Text edits: tests/_fixtures/support_matrix.py:643,
api_dump_surface.py:3 (both describe addopts).
**D6 — #9.** The seam is the module-global `otto.cli.expose.iter_exposed_verbs`, which `_ensure_dynamic`
resolves at call time (expose.py:263, 369–378) — not an instance attribute. Per chunk: one REAL call
captures the verb list; `monkeypatch.setattr(expose, "iter_exposed_verbs", lambda: list(captured))` for
the chunk; everything else (command synthesis, host scoping, the env variations of the corpus,
test_differential.py:273–293) stays real; at chunk end one more real call must equal the capture or the
chunk fails. `cli = typer.main.get_command(app)` is built once per `world` (test_differential.py:238), so the
`host` group instance persists across the chunk. Precondition the implementer proves first: no case in the corpus mutates `HOST_CLASSES` or a
host class between cases (if one does, that case runs unpatched). Acceptance: identical oracle answers on
the full corpus patched vs unpatched, once, in the task's own evidence. Deferred to the last commit; it
never delays A–C.

## Landing shape and gates for this work
Four logical commits (A, B, C, D), each one squash on a worktree branch from origin/main, spec committed
separately first. Per-commit gates per D2's own table (it exists from commit D on; before that, the
AGENTS.md reduced gate + the lane-invariant family for A–C since they touch build files = `escalate`).
Full gate at the tip of each; `make gate-fresh` before hand-back. Astra plan check; Opus task reviews;
Fable final review before the full gate.

## Owner decisions (Chris, 2026-10-14, after two Astra + Fable review rounds)
- Q1/Q2 withdrawn (the family stays in coverage; no separate lane).
- Q3 The release campaign runs every lane ONCE (`COUNT=1`, tunnel `CYCLES` default). The graduated
  1→3→10 ladder in `scripts/stability_campaign.py` stays an opt-in tool for large, risky changes where
  the burden is worth it; release does not climb it.
- Q4 The budget delta is measured once per interpreter pair; a second run only to confirm growth or
  unstable attribution.
- Q5 No valid stamp for HEAD's tree → `make release` runs `make coverage` itself, before any release
  commit, then continues.
- Q6 Hermetic conformance in release at `OTTO_CONFORMANCE_CELLS=all` (measured in the A-cost run first).
- Q7 `tests/unit/shim/test_refresh_spawn.py` and `test_differential.py` lose the `interpreter_agnostic`
  mark (B1a); `tests_unit_repeat` ignores the differential.
- Q8 `busybox` stays in the release campaign ("it's not run often").
