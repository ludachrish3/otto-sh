# Landing throughput — design

## Why

The landing flow of #824 is live: a landing is a pull request whose tip is
gated locally (`make gate-tip`, stamps per committed tree and lane, the
`local-gate` status), pushed, and rebase-merged by the maintainer once CI is
green. 0.19.0 is about thirty landings. Two costs dominate that schedule,
measured on 2026-11-02 (CI run 41552061698 on `main` at edec03048; the dev VM
for the local numbers):

1. **CI wall clock is 28.5 minutes per pull request**, and one job sets it:
   `tests_hostless-3.10`, the primary leg, runs the hostless unit tier, the
   hostless e2e tier and the `interpreter_agnostic` family under coverage.
   The canary (`tests_hostless-3.14`: the same paths minus the family, no
   coverage) takes 16 minutes, each interior leg 7, and every other job is
   under 6. Locally the family is 7.6 CPU minutes over 2,349 tests with
   single tests of 50 s, the e2e tier 10 CPU minutes over 196 tests, and
   the rest of the unit tier about 14 CPU minutes per interpreter.
2. **A rebase changes the tree, so every stamp misses.** A tip whose plan
   has no bed lane re-gates in about 50 s (the two static lanes). A tip
   whose plan has one re-gates in about 8 minutes (`make coverage-bed` is
   405 s of it) and takes the bed lease, serialized behind every other bed
   lane. So the expensive event is a landing merging in front of a bed
   landing that sits between its gate and its merge: the bed landing
   rebases and runs the bed again for a change that did not move.

The gate's words matter here. "Hostless" names a test venue; the oracle
classes a `src/otto` fix with unit tests as `src-leaf`, and its tip plan
has a bed lane (`make coverage-bed`). A landing's cost class is what
`make gate-scope` prints for its tip: **a plan with no bed lane** (docs,
web, hostless test changes, release-owned edits) or **a plan with one**
(any source, script or escalating change). This document uses those two
phrases and never "hostless" for a landing.

The maintainer approved the short-term strategy for living with the costs
(batch branches, bed landings first in a wave, a lighter review tier for
small changes; the "Landing a batch" text below) and asked for the
mechanisms that remove them, landed before the first 0.19.0 batch. This
design does both, plus the contributing text. Two independent design
reviews (2026-11-02) reshaped it: the patch identity, the guards leg, the
CI job graph and the required-context handoff are theirs; the docs-only
skip they both found unsafe is out, filed as an issue.

## Goals

- A bed stamp survives a rebase when the landing's patch replays onto the
  new base to exactly the rebased tree and what `main` gained is, by the
  oracle's own classification, a delta whose plan has no bed lane.
- The pull request's CI long pole drops from 28.5 minutes to the longest
  of the primary's three legs run in parallel, the hostless coverage floor
  judged over the fold of the three; the canary's two tiers split the same
  way so it does not become the new long pole.
- Today's required contexts keep their names and their meaning ("the
  bookend's whole hostless selection is green"), so the ruleset needs no
  edit and no pull request blocks or under-enforces in a window.
- `docs/contributing.md` says how to land a batch, in what order, and what
  a rebase costs, in the gate's words, so the 0.19.0 integrator sessions
  follow one text.

## Non-goals

- A merge queue (the recommendation's 2c): later, once `gate-admission`
  can re-attest `local-gate` on a merge-group sha.
- Bed cells and the dedicated bed runner (#801, #804).
- Carrying static stamps: they cost under a minute and are what detects
  `main`'s delta interacting with the landing; they always re-run.
- Skipping the Python legs on a docs-only or web-only pull request. Thirty
  `tests/unit` modules read docs or web files without the family mark (the
  vocabulary guard, the docs path and gap-sync tests among them), so the
  skip is unsafe until they are audited into the family and a guard pins
  that any module reading `docs/` or `web/` carries the mark. Filed as a
  follow-up issue with the list; the saving is a few minutes on docs-only
  pull requests only.
- `concurrency: cancel-in-progress` for pull requests: `ci.yml` already
  carries it.
- Reconciling the Makefile's `coverage-hostless` target with the noxfile
  bundles. It stays the by-hand floor certificate and a hand-kept pair.

## Part 1 — rebase-tolerant bed stamps

### The rule

A stamp is evidence for a tree other than its own in exactly two cases.
The first exists: the trees differ only by measured outputs whose blobs
some stamp of the stamped tree witnessed (`gate_tip.satisfies`). The
second is new, **the carry**: for a **bed** lane key (`gate_scope.is_bed`),
a stamp of tree `T0` with base `B0` is evidence for tree `T1` with base
`B1` when all four hold, checked in this order (cheapest first, the next
only when the previous holds):

1. `B0` is an ancestor of `B1` (`git merge-base --is-ancestor B0 B1`,
   run through a status-returning spawn as `_pruned` runs `cat-file -e`:
   exit 1 is "not an ancestor", exit 128 the unknown base the per-candidate
   catch covers) and `B0 != B1`: `main` advanced, the stamp is from before
   the advance.
2. **The replay**: the landing's patch at `T0`, `git diff-tree -r -p
   --binary --full-index --no-renames B0 T0`, applied with `git apply
   --cached --whitespace=nowarn` onto `B1` read into a throwaway index
   (`GIT_INDEX_FILE`, fresh per candidate), writes (`git write-tree`)
   exactly `T1`. An empty diff never carries. A user's `apply.*` config can
   only make the replay miss (the tree must still equal `T1`), never hit;
   the blob and tree objects a replay leaves in the object store are
   garbage for `git gc`.
3. No red marker of the key replays onto `B1` to `T1` (a red marker gains
   the `base` field the stamp already has, so its own diff is known): the
   lane went red on this same patch after a rebase, and a second rebase
   does not launder it. This scan proves an absence, so it is never
   capped: every marker of the key whose base is an ancestor of `B1` is
   replayed, and a marker whose replay cannot be decided (a pruned base or
   tree) refuses the carry rather than counting as absent; a marker written
   before this change has no `base`, blocks only its own tree as today,
   and is not a reason to refuse. The positive scan may miss a candidate (an optimization lost);
   the negative scan may not.
4. `main`'s delta has no bed lane: `gate_scope.plan(repo, B0, B1).bed` is
   `False`, the oracle judging `B0..B1` as if it were a landing, sources
   read at `B1`. A plan it cannot compute (`GateScopeError`,
   `UnknownGoalError`, a `GateError` from git) is "has a bed lane".

The replay is git's own answer to "is this the same patch on the new
base": it is what the rebase did, reproduced. A spike on 2026-11-02 over a
scratch repository (`replay_spike.py`) confirmed it: a clean rebase over an
unrelated change, over a change in the same file above the hunk, with a
binary blob, a mode change, an added and a deleted file all replay to the
rebased tree; an amended landing, a whitespace-only amendment and the same
edit moved to an identical block elsewhere in the file (where `git
patch-id` would collide) all refuse. A `main` edit inside a hunk's three
context lines that the rebase merged cleanly also refuses: a false miss,
on the conservative side, documented. No `patch-id`, and no git version
floor: every command predates git 2.5.

Static lane keys are never carried. The trust argument is the one
`docs/contributing.md` already states for the behind-`main` gap: a
rebase-merge lands a tree no gate ran on and CI on `main` catches it; the
carry accepts the same gap one step earlier, at the rebase, for bed lanes
only, and only when `main`'s delta would itself have landed without a bed
run by the same oracle's judgement. Named plainly: CI on `main` covers its
own hostless selections; a bed-only interaction between the landing and a
bed-free delta is an accepted, unvalidated gap until the bed runner (#801)
gates the merge result, the same gap the behind-`main` paragraph accepts. That judgement has two deltas worth
naming because they carry: a measured-matrix content change (`tests-leaf`
by the oracle's measured rows; the bed collators read those files) and a
release-owned version bump (`none`). Both are accepted as the oracle's
existing policy, not re-decided here.

### Where it lives

`gate_tip.lookup` stays the one place that decides whether the store holds
evidence for `(tree, key)`. The carry is a second promotion rule inside
it, behind a `carry: bool = False` parameter that only the runner's
`_already_stamped` passes as `True` (through `find_stamp`, which gains
`base` and `carry`). The push check, `make gate-status` and the publisher
keep calling `lookup` without it and see the stamp the runner materialized,
exactly. The reason: those three hold the store lock (shared or exclusive)
and, in the hook, git's SSH connection, for the whole scan, and the
contract in the module docstring is "short steps". The runner computes the
replays and the plans before it takes the exclusive lock; under it, it
re-reads only the exact path and checks that the set of marker files for
the key (names and mtimes) is what the scan saw, else it abandons the
carry and runs the lane. Never a replay or a plan under the lock.

Per lookup the runner tries at most the eight newest candidates by
`written_at` with the key and a base other than the caller's (the marker
scan is complete, above), memoizes the
plan per `B0`, stops at the first carry, and lets no candidate's exception
end the scan for the others. A carried stamp is materialized as a witnessed
one is today: `promoted_from = T0`, `measured = {}`, no timing, `base = B1`.
`--timings` lists it as `promoted`; the runner's line says `already stamped
(promoted from <T0[:12]>: the patch replays onto <B1[:12]>, main's delta
<B0[:12]>..<B1[:12]> has no bed lane)`, and for the witnessed kind
`(promoted from <T0[:12]>: measured outputs witnessed)`. When no candidate
carries, the reason names the first condition the nearest candidate failed
(`the patch differs on <B1[:12]>` or `main's delta <B0[:12]>..<B1[:12]>
reaches the bed: <classes>`).

### What the stamp format records

`FORMAT` stays 1; `promoted_from` is the existing field. A red marker
gains `base` (the plan's base when the lane went red); a marker without it
(written before this change) is still a marker and blocks only its own
tree, as today.

### Tests

Real scratch repositories (`tests/unit/scripts/_gate_repos.py`), a landing
whose plan has a bed lane (a `src/otto` change) stamped at `T0`, then
`main` advances and the branch is rebased:

- `main` gained a docs-only commit: the runner carries, writes
  `promoted_from`, prints the line; the push check then finds the stamp
  exactly (no candidate scan: pinned by counting git spawns through the
  existing spawn counter).
- the branch's commit is amended (one more line): no carry.
- the amendment is whitespace only: no carry.
- the same edit moved to an identical block elsewhere in the file: no
  carry.
- `main` gained a `src/otto` commit: no carry, reason names the classes.
- `main` gained a hostless test-module commit: carry.
- `main` gained a measured-matrix edit: carry (pins the accepted policy).
- the candidate's base is not an ancestor of the new base: no carry.
- the candidate's base is unknown to the repository: no carry, no
  exception, the scan continues to the next candidate.
- the lane went red on `T1`, the branch is rebased again onto a still
  bed-free `main`: no carry to `T2`; the same with eight newer red markers
  of unrelated patches for the key (the scan is complete); the same when
  `T1`'s marker has a pruned base (indeterminate refuses); a marker that
  lands between the unlocked scan and the lock abandons the carry.
- the static keys of the same landing are never carried.
- `lookup` without `carry` (the hook's call) never carries.
- a ninth candidate is never tried; the plan for one `B0` is computed once.

## Part 2 — faster CI

### 2.1 One session per tier

The hostless selection is partitioned by tier and family into three
sessions. The interpreter decides instrumentation, as today: the primary
runs instrumented, every other interpreter with coverage off.

| session | pythons | bundle pair | paths | marker expression |
|---|---|---|---|---|
| `tests_hostless_unit` | all | `HOSTLESS_UNIT_TEST_ARGS` / `_SERIAL_ARGS` on the primary; the existing `HOSTLESS_MIDDLE_*` pair elsewhere | `tests/unit` | the hostless clauses `and not interpreter_agnostic` |
| `tests_hostless_e2e` | the bookends | `HOSTLESS_E2E_TEST_ARGS` / `_SERIAL_ARGS` on the primary; `HOSTLESS_E2E_CANARY_*` on the canary | `tests/e2e` | the hostless clauses |
| `tests_hostless_guards` | the primary | `HOSTLESS_GUARDS_ARGS`, one parallel leg, no serial twin | `tests/unit` | the hostless clauses `and interpreter_agnostic`, no `serial_timing` clause |

"The hostless clauses" are `not integration and not embedded and not
stability and not browser and not busybox and not conformance and not
kmodcov`, with `and not serial_timing` on a parallel leg and `serial_timing
and` on its `-n0` twin. The resource clauses are spelt **once**, as a module-level string constant
the bundles compose by concatenation (`HOSTLESS_CLAUSES + " and not
serial_timing"`); the `serial_timing` clause and the family clause stay
literal in each bundle, because the serial-twin scanner in
`tests/unit/test_lane_invariants.py` reads source text, not resolved
tokens. One shared resolver in
`tests/unit/test_tier_marker_invariants.py` decides which string
expressions a bundle element may be (a literal, a module-level string
constant, `+` over those); `_bundles` in
`tests/unit/test_python_matrix_tiering.py`, `_python_marker_expressions`
and the bundle expansion in `_python_pytest_invocations` all call it, and
a pin drives the three readers with one composed and one literal bundle
and requires identical tokens. No second concatenation evaluator. `catch_all_branch_gaps`'s rule that every non-primary
bundle ends in `not interpreter_agnostic` exempts the e2e bundles, where
the clause would restate the unit-only declaration. The guards leg has no twin because
`tests/unit/test_make_quick.py` pins that no family test carries
`serial_timing` (the `quick-guards` shape); its bundle carries no
`serial_timing` clause, so the serial-twin scanner asks nothing of it, and
the root conftest would error any such test in a worker. The family is
declared under `tests/unit` only (pinned), so the e2e bundles need no
family clause. The unions: the primary's three sessions select exactly
today's primary pair; the canary's two select exactly today's canary pair,
whose constants go; the interior legs are unchanged. The `tests_hostless`
session name goes with the split (`tests_hostless_unit` is what it ran on
3.11–3.13 all along). Every caller of the old session migrates with its
interpreter's selection preserved, never widened or narrowed: a call for
an interior Python becomes `tests_hostless_unit-<python>`; a call for a
bookend becomes that bookend's session union (`tests_hostless_unit` +
`tests_hostless_e2e`, + `tests_hostless_guards` on the primary). The
callers: `make nox-hostless`, `make nox` and `make nox-release`
(`Makefile`), `nox.options.sessions`, nightly's matrix rows and its
future-interpreter rung (`.github/workflows/nightly.yml`, whose
`--extra-python` behaviour is kept), the environment advice
`scripts/import_budget.py` prints, the release manifest in
`tests/unit/test_lane_invariants.py`, the `make nox` pins in
`tests/unit/test_python_matrix_tiering.py`, the Makefile help,
`scripts/gate_scope.py`'s venue text and the docs. The required-context
strings never change; only sessions are renamed.

No bundle carries a positive coverage floor. The primary's parallel legs
start clean with `--cov-fail-under=0`, the serial legs `--cov-append` to
them, and the floor moves to the fold (2.2). `make coverage-hostless` stays
the by-hand floor certificate; `uv run nox -s tests_hostless_unit` enforces
none, and the docs say so.

### 2.2 The hostless floor is judged on the fold

The three primary jobs upload `reports/coverage/.coverage*` as an artifact
named after the session (`actions/upload-artifact@v7` with
`include-hidden-files: true` and `if-no-files-found: error`: the data file
is a dot-file, and an absent one must never make a green producer). A new
job `coverage-hostless` (`needs` the primary job and `gate-admission`,
`actions/download-artifact@v8` into one directory per session, the same
`github.sha` checked out, the dev group synced) runs `make
coverage-hostless-fold`, a new Makefile target: `uv run coverage combine
--keep` over the three downloaded **files** named explicitly (a directory
argument globs `.coverage.*` and would miss a file named `.coverage`), then
`uv run coverage report --show-missing --fail-under=$(CI_COVERAGE_THRESHOLD)`.
`.coveragerc`'s `relative_files = true`, `branch = true` and `precision =
2` apply, so the fold is the union of the three jobs' (file, line, arc,
context) data and equals folding once in one job; the number is recorded
on the pull request against the 94.75 floor before the docs say where the
floor lives. The floor has one home, the Makefile's
`CI_COVERAGE_THRESHOLD`; `tests/unit/test_coverage_floors.py` pins the new
shape (no floor in any noxfile bundle, the fold target and
`coverage-hostless` both reading the variable, in their two spellings).
The target joins `HOSTLESS_TARGETS` in the Makefile census.

### 2.3 The job graph and the bookend certificates

| job id | name (= context) | runs | required |
|---|---|---|---|
| `tests-primary` | `tests_hostless_unit-3.10`, `tests_hostless_e2e-3.10`, `tests_hostless_guards-3.10` (matrix of sessions) | the session, instrumented; uploads the data | no |
| `tests-canary` | `tests_hostless_unit-3.14`, `tests_hostless_e2e-3.14` | the session, uninstrumented | no |
| `tests` | `tests_hostless-3.11` … `-3.13` | `tests_hostless_unit-<python>` | yes (unchanged) |
| `coverage-hostless` | `coverage-hostless` | the fold | no |
| `hostless-primary` | `tests_hostless-3.10` | `needs: [gate-admission, tests-primary, coverage-hostless]`, `if: always()`; one step that fails unless every need's `result` is `success` (`failure`, `cancelled` and `skipped` all fail it) | yes (unchanged) |
| `hostless-canary` | `tests_hostless-3.14` | `needs: [gate-admission, tests-canary]`, the same step | yes (unchanged) |

The two certificate jobs keep today's required contexts and their
meaning: `tests_hostless-3.10` green means the primary's whole hostless
selection ran green and the fold met the floor. Because they need
`gate-admission` under `if: always()`, a head the admission refuses now
turns them red within seconds, where today every required context stays
pending; the docs state that. The interior legs keep their context names
(`tests_hostless-3.11` … `-3.13`) while running `tests_hostless_unit-<python>`;
the primary and canary legs are named by their session. That asymmetry is
the price of leaving the ruleset alone, and the Makefile help names the
sessions, not the contexts. `.github/required_contexts.json`
is unchanged, ruleset 15843311 is unchanged, and no pull request blocks or
under-enforces in any window. The `if: always()` is what makes a failed or
skipped producer a red certificate instead of a skipped one (GitHub treats
a skipped required check as passing; the certificate must never be
skipped). The three legs share the setup steps of today's `tests` job
(the BusyBox cache, strace, the kmodcov compilers) through a reusable
sequence rather than three copies: the implementer picks the YAML shape
(one job with a session matrix per interpreter group, as the table shows,
keeps the setup in one place per job). `report-failure` needs every job
id, the new ones included. Every validation job still `needs:
gate-admission`.

### 2.4 Expected effect, measured on this pull request's run

The long pole becomes the slowest of `tests_hostless_e2e-3.10` (about 10
minutes), `tests_hostless_guards-3.10` (about 8) and the unit leg (about 7
plus the tracer), then the fold (about a minute) and the certificate
(seconds): 12–14 minutes against 28.5, with the canary's halves at about
7 and 9. The docs record the numbers the pull request's own CI run shows,
not these estimates, and the measured fold number.

### Tests

- `tests/unit/test_ci_gate_admission.py`: the matrices expand to the named
  contexts; the primary legs upload with the two `with` keys and the fold
  downloads and runs the target; the certificates `need` their producers,
  carry `if: always()` and fail on any non-success result; every required
  context but the status is a declared job name; `report-failure` needs
  every job id.
- `tests/unit/test_python_matrix_tiering.py`: the three sessions' pythons
  and branches; the family is selected by the guards bundle alone; the
  canary's two pairs are the middle pair and the e2e pair without
  coverage; the union of the primary's bundles equals the former primary
  pair (read from git history once, then pinned as a literal).
- `tests/unit/test_lane_invariants.py`: `_CONSTANT_PAIRS` learns the new
  pairs; the guards bundle is a sanctioned single leg; "the primary CI
  selection" gets one reader, the union of the `@3.10` legs of the three
  sessions, which `test_the_session_reader_follows_the_interpreter`,
  `test_the_coverage_bed_lane_is_the_bed_half_of_the_coverage_selection`
  and `tests/unit/scripts/test_gate_scope.py`'s `_ci_hostless_legs` all
  call.
- `tests/unit/test_coverage_floors.py`: as in 2.2.
- The scanners' resolver: a bundle composed from a string constant and `+`
  resolves to the same tokens as its literal spelling.

## Part 3 — the contributing text

`docs/contributing.md`:

1. "Gating a branch before it lands" gains **"Landing a batch"**: a batch
   branch is one landing with one commit per fix (conventional subjects),
   one tip gate over the final tip, one pull request, one rebase-merge;
   the integrator owns the branch; a red fix is dropped from the batch
   before the batch's tip gate (a drop after it is a new tip and a new
   gate) and re-queued with anything that depended on it, never fixed in
   place by someone else. A breaking (`!`) commit may ride a batch whose
   plan has a bed lane, because a rebase-merge keeps every commit's
   message and the changelog and `check-breaking-marks` read each one; the
   pull request's title and body list every `!` subject.
2. The same section gains the **ordering rule**, in the gate's words: in a
   wave, landings whose tip plan has a bed lane merge before landings whose
   plan has none; a landing whose plan has none never merges while a bed
   landing sits between its gate and its merge. It is a cost rule (it
   avoids a bed re-run) that the carry makes unnecessary exactly when the
   intervening landings were bed-free by the oracle. Bed fixes for the
   same lab hosts may be grouped into one batch landing, whose one tip gate
   takes one lease across its lanes; separate landings take separate
   leases, no lease is held while waiting for CI or the merge, and
   grouping never narrows the selection.
3. "The landing's gate is stamped" states the **rebase cost** (static
   about 50 s, bed about 8 minutes on the one bed) and the **carry rule**
   with its four conditions, its false miss (context lines) and its trust
   argument, beside the behind-`main` paragraph it extends, and that the
   runner alone carries (the hook sees the materialized stamp).
4. "What a gate costs" adds the **batch arithmetic**: a batch of *n* fixes
   whose plans have a bed lane costs one bed run and one CI run instead of
   *n*; the CI numbers of 2.4, before and after.
5. "AI-Assisted Contributions" gains the **review tiers** (new text: the
   ladder is not in the docs today), as the project's process for
   AI-assisted work: every landing gets a design review by two independent
   models, a plan check, an implementation review per task and a final
   branch review by two models; a batch of XS/S changes (each under about
   two hundred changed lines in one package, no public-API or breaking
   change, no bed lane in its own plan) gets one design check per batch
   plan, the implementation review per fix, and one final branch review of
   the batch. The tier is decided from the whole landing, not per fix, and
   is independent of the gate class.

`docs/architecture/quality-gates.md` (the hostless-matrix and floor rows),
`docs/architecture/testing.md` (the cross-Python matrix), the
`tests_hostless_unit` docstring, the Makefile help, `scripts/gate_scope.py`'s
venue text and `docs/contributing.md` lines 475 and 1449 describe the
three sessions, the bookend certificates and the fold.

## Done when

- The Part 1 scratch-repository cases pass; `make gate-tip` on this
  branch after a rebase over a docs-only `main` commit carries its bed
  stamp and the hook accepts the push without a scan.
- The pull request's CI reports the seven leg and fold contexts and the
  two certificates; the fold's number is at or above 94.75 and is
  recorded in the pull request; `tests_hostless-3.10` and `-3.14` are
  green with the same names the ruleset requires today.
- The measured job times and the fold number are in the docs.
- The docs-only-skip follow-up issue exists with the thirty modules.
- One pull request, one commit per part, each with its own tests.

## Left to the builder

- The exact YAML shape that keeps the legs' setup steps in one place.
- The artifact and directory names, as long as the session names them.
- The wording of the runner's lines and the reasons beyond what is quoted.
