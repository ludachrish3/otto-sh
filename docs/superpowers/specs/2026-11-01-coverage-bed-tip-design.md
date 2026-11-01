# The coverage-bed tip (#797) — design

## Why

A `src-leaf` change (one or two `src/otto/` packages) runs the full gate at
its tip: `make coverage`, 22–24 minutes on the dev VM (1343 s and 1415 s on
the last two gated tips), plus the package's bed lane. The commit column of
the same change already computes a precise scope, the package's unit and bed
trees in `UNIT_TREES`, and the tip throws it away.

What `make coverage` runs splits by venue. Of its 5,721 test-seconds in the
last green run (2026-11-01, 22,887 tests), `tests/unit` is 3,542 and the
rest, the `tests/integration` tree and the e2e tests marked `integration` or
`embedded`, is about 2,180 (docker e2e 381, console e2e 344, integration/host
292, cov e2e 251, cli e2e 215, busybox_bed 101, link impair 97, host e2e 92,
tunnel e2e 125, the rest under 70 each). Only that second part needs the lab.
The first part is what the pull request's CI runs on five interpreters before
anything lands (#795).

What a `src-leaf` landing gives up by not running `make coverage` at its tip
is therefore not the bed and not the hostless tests, but two certificates:
the Python coverage floor over the whole selection on the primary
interpreter, and the merged TypeScript floor (`coverage-ts`, folded through
the `dashboard` prerequisite). CI runs neither (its Python matrix trims the
non-primary selections, and it has no merged TS gate). Both stay with
`make coverage`, which `make release` runs itself when no stamp exists; this
design moves them from every `src-leaf` tip to the release, deliberately.

## Goals

1. **A lane that runs exactly the marker-defined bed half of `make
   coverage`'s selection**: `make coverage-bed`, selecting
   `(integration or embedded) and $(M_COVERAGE)` in the two-leg shape every
   coverage lane has (parallel leg excludes `serial_timing`, the `-n0` twin
   re-appends it), under the bed lease and the reap, uninstrumented. The
   selection reuses `M_COVERAGE`, the one spelling the coverage stamp
   records, and the census pins the partition: `coverage-bed`'s expression
   is `(integration or embedded) and $(M_COVERAGE)`, and `M_HOSTLESS` is
   `not integration and not embedded and` the body of `M_COVERAGE`, so bed
   plus hostless is the whole and the two are disjoint, textually.
2. **The `src-leaf` tip stops running the full gate.** It becomes the
   commit column with a wider `make quick` (the row's unit roots, the
   package's direct consumers, the tree-wide guards), plus
   `make coverage-bed`, the package's `BED_LANES` entry, and
   `make gate-fresh`. `escalate`, `full` and `scripts` keep the full gate.
3. **Consumers are derived, not listed.** A hostless unit test module
   outside the package's mapped trees that imports `otto.<pkg>` rides the
   tip's `quick`. The rule is an AST read of the module at the tip revision,
   through the oracle's existing reader and parser.
4. **`make coverage` and its stamp do not change.** It stays the release
   certificate; a bed-only run writes no coverage stamp and gates no floor.
5. **The census pins all of it**, with one constant naming the new lane that
   the oracle, the lease census and the goal census all read.

## Non-goals

- Changing what `make coverage`, `coverage-unix`, `coverage-embedded` or
  `coverage-integration` run. The new lane is a fourth slice beside them.
- A per-package bed slice. The bed half runs whole: at 2,180 test-seconds it
  is 6–9 minutes under `COVERAGE_WORKERS`, and the largest serialised group
  (`docker_e2e`, ~230 s) is the floor a narrower selection could not lower.
- Transitive consumers. A test of `otto.cli` that reaches `otto.host` through
  the CLI without importing it is not a direct importer and does not ride;
  the PR's CI runs it. The local tip is the fast signal, CI is the hostless
  certificate (#795).
- The nightly combined floor the issue mentions "once a lab venue exists".
  That venue is #801; until then the floor is the release's.
- The capability pools (#804) and lab cells (#801), which will run this lane
  per cell. Its prologue is the lease they build on (#794).

## The lane

```make
coverage-bed: ## Run the bed half of `make coverage`'s selection (tests/integration and the e2e tests marked integration or embedded), uninstrumented, pass/fail: no floor, no stamp. Requires lab VMs. JUnit XML in reports/junit/coverage-bed/ and reports/junit/coverage-bed-serial/.
	$(call bed_acquire,coverage-bed)
	$(call bed_reap)
	@$(SAY) "pytest: the bed half of the coverage selection (-n $(COVERAGE_WORKERS), no coverage)"
	@$(LEAK_DETECT) $(TIMEOUT_CMD) uv run pytest -m "(integration or embedded) and $(M_COVERAGE) and not serial_timing" -n $(COVERAGE_WORKERS) --no-cov $(call junitxml,coverage-bed)
	@$(SAY) "pytest: serial_timing twin, -n0"
	@$(LEAK_DETECT) $(TIMEOUT_CMD) uv run pytest -m "serial_timing and (integration or embedded) and $(M_COVERAGE)" -n0 --no-cov $(call junitxml,coverage-bed-serial)
	$(call bed_reap)
	$(call bed_release)
```

Decisions inside it:

- **`--no-cov`, not `--cov-fail-under=0`.** `quick` is the precedent: with
  `--no-cov` pytest-cov returns before starting the tracer, so pyproject's
  `--cov` addopts are inert and nothing invites reading a partial floor.
  `--cov-append` is absent (after `--no-cov` it only warns). The lane does
  not `export override PYTEST_ADDOPTS :=` as `coverage` does: it is not a
  stamped selection, and `gate_tip` clears the variable for every lane.
- **Marker selection over the whole tree, no path arguments.** Every item
  under `tests/integration` carries `integration` (the tree's conftest adds
  it at collection; the few hostless modules there carry it too and so ride
  the bed lane, as they ride `coverage-unix` today). Every e2e item declares
  exactly one of `hostless`, `integration`, `embedded` (the e2e conftest
  enforces it on every collected item, past `-m` deselection). No
  `tests/unit` module carries either mark. `busybox_bed` items carry
  `integration` and ride, as they ride `make coverage`. So the expression
  is the marker-defined bed half of the coverage selection, the complement
  of `M_HOSTLESS` within `M_COVERAGE`.
- **Workers**: `COVERAGE_WORKERS`, like `coverage-python`; the slice is
  xdist-grouped by the tests' own marks.
- **No `$(DASHBOARD_DIST)` prerequisite.** No integration- or
  embedded-marked module imports the web assets today (measured); a future
  monitor integration test that does must add the prerequisite here.
- **Lease and reap** like every bed lane (#794, #759).
- **No `COVERAGE_STAMP` marks.** The stamp records `make coverage`'s legs
  only. `gate_tip`'s stamp special case is `goals == ["coverage"]`, so this
  lane is stamped like any other goal.

### The serial-twin comparator

`tests/unit/test_lane_invariants.py`'s `_conjunction_clauses` refuses any
`or` or parenthesis by design ("teach this pin the shape rather than
dropping the pair"). This lane teaches it the shape: a parenthesised
disjunction of bare markers, `(a or b)`, is one clause, compared as a set of
alternatives; anything else inside parentheses stays a refusal. Tests: the
positive pair, a twin whose group differs (`(integration or hostless)`),
nested parentheses and `or` outside a group all refuse. No lane is exempted.

## The tip

`commands()` today sets `full` at a tip for `SRC_LEAF` and `SCRIPTS`. After
this change `SRC_LEAF` alone no longer does. The `src-leaf` tip is the commit
column with these differences, in the commit column's order:

```
make check-breaking                                   (live at push)
make quick PATHS="<unit roots of UNIT_TREES[pkg]> <consumers> <GUARD_RIDERS>"
uv run ty check <changed src files>
uv run ruff check <changed files>
make lint-arch                                        (live at push)
make quick-guards
make coverage-bed                                     (SRC_LEAF_TIP_LANES)
<BED_LANES[pkg]>                                      (when the package has one)
make docs-captures-check                              (the captures rider every src change carries today)
make gate-fresh                                       (live at push)
```

- **Unit roots only in `quick`.** The row's `tests/integration` and
  `tests/e2e` entries are `coverage-bed`'s at the tip: `M_QUICK` excludes
  neither `integration` nor `embedded`, so leaving them in would run the
  bed trees twice. They stay in the commit column, where there is no
  `coverage-bed`. A row whose unit root is `tests/unit` whole (`utils`,
  `result`, `params`, `errors`, `layout`, `console` and the other top-level
  modules) gains nothing from scoping: its `quick` would be the whole unit
  tree, 3,542 test-seconds, about as long as `make coverage` uninstrumented.
  Such a row keeps the full gate at the tip, by the existing `full_at_tip`
  mechanism, and the class table says so.
- **No `make check-python`, `make check-ts`, `make docs`.** `gate-fresh`
  runs `lint-python`, `typecheck-python`, `lint-arch` and `docs` at the
  push, so `check-python` would be the same work twice (the full gate keeps
  it as its cheap first line for runs by hand). `check-ts` is not on it
  because a `src-leaf` diff touches no TypeScript, and the one way it can
  (a pydantic model change drifting `web/src/api/*.gen.ts`) is caught at
  the push: `gate-fresh` → `docs` → `$(DASHBOARD_DIST)` → `make web`, whose
  drift check fires in the throwaway worktree where the dist is absent. A
  mixed diff adds the `web` lines by its own class.
- **`scripts` keeps the full gate.** Scripts drive the bed lanes
  (`bed_lease`, `bed_reap`, `gate_tip`, `coverage_stamp`) and there is no
  per-script bed map; `make coverage` is what exercises them whole.
- **Mixed classes compose as today.** `src-leaf` + `tests-leaf` (code and
  its tests, the common landing): one `quick` line over the union of the
  paths, the rest by class. `src-leaf` + `web` or `docs`: their lines are
  added. `src-leaf` + `scripts`, `escalate` or `full`, or a `full_at_tip`
  row (`busybox_bed`): the full gate, as now. Two packages: the union of
  their rows' unit roots and consumers minus every path inside either row's
  trees. Three or more packages is `escalate`.
- **One place for the lane.** `SRC_LEAF_TIP_LANES = ["make coverage-bed"]`
  in `scripts/gate_scope.py`; `commands()` appends it, the lease census
  (`test_the_oracle_names_no_bed_lane_the_makefile_does_not_lease`) reads it
  beside `FULL_GATE` and `BED_LANES`, and `gate_tip.STAMPED_GOALS` gains
  `coverage-bed`, which the goal census then demands. `BED_TARGETS_DIRECT`
  and `.PHONY` gain the target.
- **Stamps and keys.** `lane_key` is the whole line, so a consumer added in
  the range re-keys the `quick` lane, correctly: the tree changed. `make
  gate-scope` and `gate_tip`'s table print the line whole; a `host` tip's
  `quick` is about 170 paths and 7 KB, within every limit, and that is
  accepted rather than folded.

## Consumers

A consumer of `otto.<pkg>` is a `tests/unit` test module whose AST has an
import of the package, in any of three forms and at any depth (a
function-local import is a direct import): `import otto.<pkg>[.x] [as y]`,
`from otto.<pkg>[.x] import …`, `from otto import <pkg> [as y]`. Attribute
access through a bare `import otto` is not read (one module does it, for a
lazy-export test, and it is the transitive case). The dotted name is matched
whole, so `otto.lab` is not `otto.labs`. Excluded: every path inside the
package's `UNIT_TREES` row (its trees already run), `GUARD_RIDERS` (already
on every src-leaf `quick`), and a module whose every test carries
`interpreter_agnostic` (`carries_family_mark`: it is `quick-guards`'s, and
its root would add a leg that selects nothing under `M_QUICK`).

Mechanics: `classify` already holds the reader and the tip revision. One
`git grep -l -E 'otto(\.|\s+import\s+)<pkg>' <rev> -- tests/unit` per
changed package narrows the candidates (a subprocess per package, not per
module), and each candidate is read through the reader and parsed with the
oracle's `_parse` to confirm the import form. The result is a new `Scope`
field, `consumers` (sorted module paths), that `commands()` adds to the
`quick` line at the tip only. Paths go through `_outermost` with the rest.

Measured at 9d40f801c over 952 unit test modules, under exactly this rule:

| package | importing modules | consumers (outside its row) |
| --- | --- | --- |
| host | 346 | 171 |
| config | 174 | 126 |
| labs | 117 | 94 |
| models | 104 | 80 |
| logger | 89 | 76 |
| cli | 111 | 42 |
| reservations | 35 | 19 |
| link | 42 | 18 |
| inventory | 32 | 18 |
| suite | 44 | 17 |
| check | 24 | 16 |
| coverage | 83 | 15 |
| docker | 41 | 14 |
| monitor | 53 | 8 |
| tunnel | 25 | 5 |
| utils, result, params, errors, layout, console, … | — | 0 (whole-tree rows) |

A `host` change rides 171 extra modules, roughly two fifths of the unit
tree's test-seconds, about five minutes under `QUICK_WORKERS`; `monitor`
rides eight. This is the rule the issue called `CONSUMER_RIDERS`, made a
function instead of a table: a table of 171 paths would be wrong within a
week, and the census proves the mapped trees, not the importers. Tests: a
synthetic reader per import form (including the alias, the dotted-prefix
non-match and the function-local import), the exclusions, and one real-tree
pin that `tests/unit/cli/test_host.py` is a consumer of `host`.

### The tip census

The existing census proves every collected item is selected by a commit
lane. A tip census for `src-leaf` has a bounded universe: every item under
the row's trees and its consumers is selected by the tip's `quick`,
`quick-guards` or `coverage-bed` legs (read through `make_legs`, as the
commit census reads them). Over the whole tree it would be red by design:
hostless non-consumers are CI's.

## What stays the same

`make coverage` (selection, legs, stamp, floor), `nox-release`, every other
class's lines, the commit column of `src-leaf`, `UNIT_TREES`, `BED_LANES`,
`GUARD_RIDERS`. One consequence to state: a `src-leaf` landing no longer
leaves a coverage stamp, so `make release` runs `make coverage` itself
unless someone ran it on the release tip beforehand; the release section of
docs/contributing.md ("Version management") says to.

## Measurement (acceptance)

- **Partition**: `pytest --collect-only -q` three times, `-m "$(M_COVERAGE)"`,
  `-m "(integration or embedded) and $(M_COVERAGE)"`, `-m "$(M_HOSTLESS)"`:
  coverage equals bed plus hostless and bed meets hostless in nothing. Run
  once at the landing, recorded in its report; the standing proof is the
  textual pin in Goal 1.
- **Wall time.** This landing edits the Makefile and `scripts/`, so its own
  tip is `escalate` and runs the full gate; it cannot measure the new
  composition on itself. Before: the two stamped `make coverage` timings
  (1343 s, 1415 s) plus the bed lane's `elapsed_s` from the same trees'
  stamps (`gate_tip.py --timings <rev>`). After: `make gate-tip` on a
  representative committed `src-leaf` range (a `host` and a `monitor`
  change), the sum of its lanes' `elapsed_s` (they run serially, so the sum
  is the wall), recorded in the landing report. Expected: `coverage-bed`
  6–9 min, `quick` 1–5 min by package, `quick-guards` about a minute.

## Docs

docs/contributing.md: the class table's `src-leaf` tip cell (and the
whole-tree-row exception); "Gating a branch before it lands": "the local tip
adds what CI cannot run: `make coverage`…" becomes the bed half for
`src-leaf` and the full gate for `escalate`/`full`/`scripts`; the full-gate
order paragraph's "`make gate-tip` stamps `make check-ts`, the suite and the
bed lanes"; the sentence "`make gate-fresh` is the full gate's last static
lane (`make coverage` follows it, …)"; the pre-push example's `make
coverage: no stamp`; "The landing's gate is stamped" gains `coverage-bed`;
"Where the test suite's time goes" gets the venue split above; "Version
management" gets "run `make coverage` on the release tip first".
docs/architecture/quality-gates.md: "the tip of a landing runs the full
gate" and the row after it; the binding table gains `coverage-bed`.
scripts/gate_scope.py: the `commands()` and `FULL_GATE` docstrings.
AGENTS.md's gate bullet: a `src-leaf` tip runs the bed half and its scoped
`quick`, not `make coverage`; `make coverage` is the release certificate.
