# Release deduplication: the release trusts CI's verdict on its tree, the matrices split into verify and certify, and budget growth becomes a CI gate

**Issues:** #1008 (the 0.20.0 evidence: stages replayed on a tree PR CI had proved; four budget-delta stops, each costing a full rerun), #948 (hostless lanes to the release's CI; the 96 % floor), #849 (conformance verify/certify split; date-only matrix commits), #523 (the file-operation guards off the push gate), #829 (context: docs-only PRs still run every Python leg).
**Owner decisions:** Chris, 2026-11-11 (this session's brainstorm, recorded in §4).
**Reviews:** builder check (Opus), Astra (Codex) and Fable, two rounds each, 2026-11-11; their refinements are merged into §5–§8, and the one point they left to the owner was decided (§6.2, §9).
**Evidence:** the 0.20.0 cut, 2026-11-11 (#1008's tables: four `make release` runs on one tree, ~2 h 10 min for the one that went through, 55–60 min of it on stages CI had already proved on the identical tree; both matrix commits dates only).

## 1. Intent

`make release` certifies a tree before it ships. It runs on a release branch at
`main`'s tip, a tree that `main`'s CI has already run to completion, and it begins by
replaying the stages CI ran there: the docs build, the Python static checks, the
hostless nox matrix on four interpreters, the all-browser dashboard e2e, the TypeScript
gate. The dev VM is the only machine that can run the bed, and the release spends its
first hour not running the bed.

The release's import-budget stage then stops the run on growth. It stops at the first
interpreter it cannot approve, throws away the tip measurements of the interpreters it
already judged, prints an approval string in a form that does not apply to the next
interpreter, and requires the string to match each interpreter's growth exactly. Every
stop restarts the run from the first stage. Growth that the stage catches at the cut
has usually been on `main` for weeks: 0.20.0's two otto-owned leaves were merged in
separate pull requests, and nothing between a merge and the release measures the delta.

Two matrix stages re-measure the bed and commit the result. Since September, 24 of 26
matrix commits on `main` changed nothing but `as_of` dates (#849), because the
conformance lane the per-change gate runs rewrites the committed matrix on every run,
not only at a release.

This design removes the duplicated work, not the wall clock as such. The release keeps
every check only the dev VM can make and every check whose combined verdict the owner
values; it stops repeating what CI proved on the same tree; the matrices are verified
on every landing that reaches them and certified, with a date, at the release; and
budget growth is caught on the pull request that introduces it, on every supported
interpreter, with the release's own measurement kept as the second environment.

## 2. Goals, non-goals, anti-goals

**Goals**

1. A `make release` run on a tree whose `main` CI is green runs no stage that CI ran on
   that tree. The skipped stages are named in one place and the release says, per
   stage, that it trusted CI and which run.
2. A release restart after a stop reaches the stage that stopped without re-measuring
   what an earlier run of the same tree measured: the base and the tip of the budget
   delta are cached by what they measured, and the coverage stamp already carries
   `make coverage`.
3. The budget delta judges every interpreter before it stops, stops once, prints one
   complete approval set, and an approval approves growth up to an amount per bucket.
   Approvals live in a committed file keyed by the base tag, not on the command line.
4. Growth is caught where it lands: a CI job per supported interpreter measures the
   last release tag against the pull request's head and fails on unapproved growth,
   on pull requests whose diff can change a startup path. The release keeps its local
   delta on the dev VM's architecture.
5. `make conformance-bed` and `make kmodcov` verify: they run the contracts, compare
   with the committed matrix, fail on a downgrade, write their observations under
   `reports/`, and leave the tree clean. Only `make release-matrix` and
   `make release-kmodcov-matrix` write and commit the matrices, so an `as_of` date means
   "certified at this release".
6. The combined coverage floor rises to 96 % (today's measured value is 96.92 %), and
   `make coverage` stays a local release stage so that one process on one machine
   judges hostless and bed together.
7. The web assets are built once per release run.

**Non-goals**

- Moving the bed half of coverage, the matrices or the campaign anywhere: they are the
  dev VM's job.
- #948's cross-machine coverage fold (CI's hostless data combined with the bed's). The
  owner keeps the one-process `make coverage` locally; its duplication with CI's
  hostless coverage is accepted for the sake of one combined number. This document
  supersedes #948's fold; #948's other items (trust CI, 96 %) land here.
- Changing what the per-change gates run beyond the matrix split: `make gate-tip`
  stays cheap static plus bed-only (the 2026-11-01 ruling).
- Resume stamps for the release's other stages. With the CI-proved stages skipped and
  the budget tip cached, a restart's replay is preflight plus a cached re-judge; the
  bed stages that remain are the work, and a stamp for them is #566's territory, not
  this document's.
- Per-change gating of the import ceilings (`make profile`); nightly's rung keeps it.
- Shortening the campaign or the matrices' measurement itself.

**Anti-goals**

- A release that ships a tree CI never ran green. Trusting CI's verdict must mean
  reading a green run of `ci.yml` on the identical commit, never "a run exists".
- A skipped stage whose skip is decided in the Makefile by hand-copied job names. The
  set of required CI jobs is `.github/required_contexts.json`; the evidence map is
  checked against it, and against the release census, by tests.
- A budget approval that approves more than a person wrote: "up to N" is a ceiling the
  owner typed, never a wildcard.
- A matrix verdict that only a release can produce. A pull request can still change
  the matrix by hand, downgrades included, and the verify lane compares against the
  matrix committed at that head.
- A second resolver for anything the release already resolves: the CI verdict reader,
  the base tag, the required contexts, the tree hash, the stage list.

## 3. Constraints

- Python 3.10 floor; no `from __future__ import annotations`; `ty` typechecks.
- ONE DECISION, ONE PLACE (AGENTS.md): the CI verdict is read by
  `scripts/local_gate_status.py ci-verdict` and nowhere else; the base tag by
  `import_budget_delta.base_ref`; the matrix downgrade rule by
  `scripts/check_matrix_downgrades.py`; the fold by the two collate scripts in
  `--write` mode; the diff classes by `scripts/gate_scope.py`.
- The release's landing shape is unchanged: a release branch at `main`'s tip, a pull
  request, rebase-merge, `make release-tag`.
- `scripts/local_gate_status.py` stays stdlib only (CI runs it with `--no-project`).
- CI runners are x86; the dev VM and Chris's host are ARM64. Each environment compares
  the tag against the tip on itself; an approval applies to both.
- Every bed lane takes the bed lease; none of this changes which lanes do.
- Documentation is part of the change: `docs/release_process.md`,
  `docs/contributing.md` ("Two lanes measure", "Gating a branch before it lands"),
  `docs/architecture/quality-gates.md`, `docs/architecture/testing.md`,
  `docs/architecture/startup-performance.md`, the support-matrix and kmodcov-matrix
  pages, and the Makefile help strings say the new shape.
- The lane-invariant tests (`tests/unit/test_lane_invariants.py`) pin which targets
  take the lease and which legs exist; they change with the targets.

## 4. Decisions (the owner's)

1. **The measure is duplicated work, not wall clock.** A stage is removed from the
   release when another run on the identical tree already produced its verdict, not
   because it is slow.
2. **Verify everywhere, certify at the release.** Conformance and kmodcov runs outside
   the release only verify: they fail on a downgrade against the committed matrix and
   write observations to `reports/`; they never write `schemas/support_matrix.json` or
   `schemas/kmodcov_matrix.json`. The release's two matrix stages fold and commit, and
   their `as_of` dates mean "certified at this release".
3. **The release reads `main`'s CI verdict on the tree it starts from** (the same
   reader `make release-tag` uses) and skips the stages that verdict covers:
   `check-python`, the first docs build, `nox-release`'s hostless part (3.11–3.13 and
   the hostless half of 3.14; the bed half on the canary stays, as a bed-only leg), the
   all-browser dashboard e2e, `validate-ts`. CI's green on the identical tree is sufficient; no
   local re-run is wanted. It keeps the full `make coverage` with the floor raised to
   96 %, the ARM budget delta, the two matrix stages, the campaign, the second docs
   build (it renders the re-dated matrices), and `wheel-check`.
4. **Budget growth is a CI gate.** A job per Python minor 3.10–3.14 measures the last
   tag against the pull request's head, on pull requests that touch a startup-path
   module. Approvals come from a committed file keyed by the base tag, and never carry
   across a tag: they expire at the tag (§6.2; decided 2026-11-11 over the
   alternatives in §9). The delta judges every minor, stops once with a complete approval set, caches
   the tip's measurements by tree, approves up to an amount, and attributes growth per
   module. The release keeps the local ARM delta as the second environment.
5. **The combined floor rises to 96 %** (0.5 % of room exists: 96.92 % at 0.20.0).

## 5. The release run

### 5.1 The verdict as a stage

`release-preflight` already refuses a branch that is not `origin/main`'s tip. The
release now also requires `main`'s newest `ci.yml` push run on that commit to be green
on every required context (`.github/required_contexts.json`) except the two the verdict
reader already excludes. The reader is `local_gate_status.ci_verdict`, the function
`release-tag` already calls, and it is read **once**, by `release_record.py pending`,
the stage that runs right after preflight and before any work: it calls `ci_verdict`
with `appear_s=0` (the run on `main`'s tip either exists or the cut is premature;
preflight never waits), refuses without writing on a missing, pending or red run, and
writes the pending record with the trusted run's id and the head it judged. The
refusal text is `_ci_refusal`, moved from `release_tag.py` into `local_gate_status.py`
(it only formats a `Verdict`; stdlib stays stdlib) and imported by both; the remedy for
a pending run is "wait for `<url>`", as `release-tag` prints today. Every later record
transition (`certify`, `landed`, `tip-certified`) builds on the read record and carries
the trusted-run fields, so the final record says which CI run the release trusted.

A red `main` therefore costs seconds, and the case `make release-tag` guarded alone (a
release that passed locally on a tree whose CI was red) is refused at the start.

### 5.2 Which stages the verdict replaces

The release recipe drops these stages. Each has evidence on the identical tree, named
per **leg** (a target's pytest roots and marker expression, or its nox session), not
per target, because two of the targets are proved by more than one thing:

| Release stage (leg) | Evidence on the same tree |
|---|---|
| `check-python`: lock check, tach, ast-grep, duplicate code, ruff, positional check, ty | CI `lint-python`, `typecheck-python` |
| `check-python`: `lint-arch`'s pytest legs (import rules, layer audits) | CI `tests_hostless-3.10` (they are unit tests) |
| first `docs`: docs-lint, html `-W`, sphinx doctest, source doctests | CI `docs` |
| `nox-release`: `tests_hostless_unit` on 3.11–3.13; the hostless half of `tests_all` on 3.14 | CI `tests_hostless-3.11` … `tests_hostless-3.14` |
| `dashboard-all`: chromium, firefox, webkit | CI `dashboard-e2e (chromium)`, `(firefox)`, `(webkit)` |
| `validate-ts`: `check-ts` | CI `check-ts` |
| `validate-ts`: `coverage-ts` (the merged vitest + e2e floor) | the coverage stamp (`make coverage` runs `coverage-ts`) |

**The bed half of `tests_all` on the canary stays.** `nox-release` is not hostless: on
3.14 it runs `tests_all`, every product test minus `interpreter_agnostic`, bed tests
included, under the lease. CI's hostless jobs do not prove the bed on 3.14; only the
primary's bed run (the coverage stamp) is proved elsewhere. Owner decision
(Chris, 2026-11-11): `nox-release` shrinks to a bed-only canary leg, the `coverage-bed`
selection on 3.14 under the lease (about ten minutes of the stamped `make coverage`),
uninstrumented, pass/fail. The target keeps its name and its place in the recipe; its
help string and `docs/release_process.md` say what it now runs. One decision, one
place: the canary leg selects the bed by the same expression `coverage-bed` uses (the
shared Makefile variable, not a restated marker string). A new lane-invariant test pins
the partition on the canary's expressions: hostless-unit ∪ hostless-e2e (roots and
`HOSTLESS_CLAUSES`, serial legs included) ∪ the bed selection = `tests_all`'s
collection, with the family excluded as today; an item in neither reddens it.

The map is one constant in one script (`scripts/release_stages.py`, or a section of
`release_record.py`; the builder's choice), keyed by leg, with "coverage stamp" as a
legal evidence value. Its tests: every CI job it names is in
`required_contexts.json`; every leg the release census (`test_lane_invariants.py`)
says the old release reached is either still in the release's walk or named in the
map. The release prints its skip lines from the map and from the verdict retained in
the record ("skipped check-python: proved by lint-python, typecheck-python in run
<url>"). `docs/architecture/quality-gates.md`'s local-target / nox-session / CI-job
table is rendered from, or tested against, the same constant (the support matrix
already does this with `scripts/render_support_matrix.py`); the Makefile help for
`release` lists the stages in order and refers to the doc for the evidence.

What stays, in order: preflight, `pending` (with the verdict), `clean-dist`,
`web-install`, `budget-delta`, the coverage stage under the stamp, `nox-release` (the
bed-only canary leg), `release-matrix`, `release-kmodcov-matrix`, `release-campaign`,
the second `docs` build (the whole target: it renders the re-dated matrices and is the
release's only `doctest-src` leg), changelog, bump, `wheel-check`, certify, `gate-tip`,
push, PR. The `coverage` stage's rule is unchanged: a valid stamp for HEAD's tree skips
it, otherwise it runs.

`dashboard-all` and `validate-ts` remain Makefile targets for by-hand use; the release
recipe no longer calls them. `docs/release_process.md`'s stage list is rewritten to the
new order and says which stages are CI's, by name.

**Landing the change itself.** `tests/unit/test_landing_ruleset.py` pins
`required_contexts.json` to the live `main` ruleset, and preflight runs it. The pull
request that adds the budget certificate job (§6.5) to `required_contexts.json` lands
with the live ruleset edited in the same step, or the first `make release` after it
refuses at preflight. The PR body says so.

### 5.3 What a restart costs

After this change a restart on the same tree replays: preflight and the verdict
(seconds), `npm ci`, and a budget re-judge from cache (§6.3). The coverage stamp
already covers `make coverage`. The canary bed leg, the matrix stages, the campaign and
the second docs build run again because they are the release's product, which is the
work the restart was for. No new stamp store.

### 5.4 One web build per run

The freshness decision for the built assets already exists: the grouped Make rule
`$(DASHBOARD_DIST) $(COVAPP_DIST) &: $(WEB_SRCS) $(WEB_NODE_MODULES)`, which
`docs-html`, `coverage-stamp-start` and `coverage-bed` consume. The one consumer that
bypasses it is `wheel-check`, whose prerequisites name the PHONY `web`; the release
recipe adds a second bypass with its standalone `web` stage. Both go:
`wheel-check: clean-dist $(DASHBOARD_DIST) $(COVAPP_DIST) build` (the DIST variables
move above `wheel-check` in the Makefile, since they are defined after it today), and
the `web` stage leaves the recipe. The DIST rule runs `make web`, so the
`gen_web_types.sh` drift gate against the live pydantic models and the air-gap and
brand gates still run once per release tree. The release's `clean-dist` must remove
both DIST files (or the first consumer rebuilds anyway), so the one build in a run is
a real build of the release tree. `make web` by hand still rebuilds. No stamp file: a
stamp keyed on `web/` would skip the drift gate, whose input is the Python models.

### 5.5 Coverage floor

`COVERAGE_THRESHOLD` becomes 96 (Makefile, one constant; the coverage stamp's
`--threshold` reads it). `CI_COVERAGE_THRESHOLD` (hostless only) is untouched: it is a
different number over a different selection, never raised by re-baselining. The
floor's documentation (`docs/contributing.md`, `docs/architecture/quality-gates.md`,
the release process) says 96 and why: 96.92 % measured at 0.20.0.

## 6. The import-budget delta

### 6.1 Judge every minor, stop once

`import_budget_delta.py measure` measures and judges every minor in `--minors` before
it returns. The verdict per minor prints as today; the final block prints one approval
set that covers every minor's growth, in the committed file's syntax (§6.2), ready to
paste into the file. The exit status is the worst verdict. The "primary first, stop at
the first failing minor" rule in the module docstring goes with it; the primary still
runs first so that the table a reader sees first is the one they tune for.

### 6.2 Approvals in a committed file

Approvals move from `BUDGET_APPROVE` on the command line to
`tests/unit/import_budget/approved_growth.json` (beside the ceilings; the ceilings dir
is the one home for budget data). Shape:

```json
{
  "base": "v0.20.0",
  "entries": [
    {"surface": "help", "minor": null, "buckets": {"stdlib": 8, "pydantic": 4, "pydantic_core": 6}, "why": "pydantic 2.14's _fields imports secrets and hmac"},
    {"surface": "bootstrap_project", "minor": "3.14", "buckets": {"other": 7, "stdlib": 12}, "why": "…"}
  ]
}
```

- `base` names the tag the entries were written against. Entries are **in force**
  when `base` equals the delta's base tag (`base_ref`, which gains a revision
  parameter so CI resolves it at the pull request's merge-base, §6.5). When `base` is
  any other tag the entries are **spent**: ignored, each printed as a NOTE ("entries
  for v0.20.0; the base is v0.21.0"), and the file is treated as empty. Nothing is
  invalid evidence but a file that does not parse or whose entries are malformed
  (exit 2). Owner decision (Chris, 2026-11-11): approvals **expire at the tag**; no
  reset is ever committed. This is how "cleared at the bump" (§4.4) is realised; the
  alternatives considered are recorded in §9.
- An entry approves growth **up to** each named bucket's amount for the surface, on
  every minor when `minor` is null (each minor judged against the same amounts
  independently), else on that minor. The amount is strict: noise is not added on top
  of it. A bucket the entry does not name may move within `BUCKET_NOISE`, as today,
  and no further. The amounts judged, and the amounts the stop prints, are the
  **deciding pair's** (the pair the two-pair confirmation settles on; totals must
  reproduce exactly between pairs as today, so "up to" is a ceiling over the pair the
  verdict rests on). Growth within the amounts on every bucket passes and is reported
  as approved, with the amounts used. One entry serves both environments (x86 CI and
  the ARM dev VM): the owner writes the larger of the two growths, and the entry is an
  honest ceiling on both. When records of both environments exist for the surface
  (the dev VM's cache after a release run, CI's artifact), the stop block prints both.
- `why` is required and non-empty: an approval is a reviewed decision with a reason,
  the way the matrix's accepted downgrades carry a body.
- `BUDGET_APPROVE` is deleted from the Makefile, the script, `COVERAGE_STAMP_HARMLESS`
  and its test (no hidden option; the file is the one place). The "stale approval"
  stop goes with it: an entry nothing grew for is a NOTE, everywhere, never a stop.
  Today's stale stop caught a mistyped surface name, but with every minor judged and
  the real growth then unapproved, the mistype stops the run by itself; a stale stop
  would only force a pull request to `main` and a re-cut for an entry that expires at
  the next tag anyway. (My call, 2026-11-11.)
- The release's own pull request does not touch the file (nothing resets it), so the
  merge-to-tag window and the recertification run judge the bumped tree against the
  old tag with the entries still in force, which is the growth they approved.

### 6.3 The tip cached by its measured tree

The approvals file is read by the judge only, never by a measurement, so a commit that
edits it must not re-measure. The tip's cache key is the **measured tree**: git's own
tree id of the commit's tree with the approvals path removed (`git read-tree` into a
temporary index, `git update-index --force-remove <path>`, `git write-tree`; a real
tree object, no hand-rolled hash), computed by one function in
`import_budget_delta.py` and used for `reports/budget/<measured-tree>/` (confirming
pairs `again-N/` under it), for `Provenance.tree` on both sides, and for the same-tree
short-circuit: base and tip with equal measured trees judge a zero delta from the
base's records (exit 0, with spent or unused entries as NOTEs), never exit 2, so an
approvals-only pull request gets a verdict. `Provenance` drops `sha` (identical trees
under different commits are the same evidence); the sha stays in the status file as
audit metadata. `python` (the full version) and `instrument` are unchanged. At measure
start the script prunes tip directories other than the current measured tree's, so
the store holds one tip; the base stays cached per tag as today. The record schema
change in §6.4 changes the instrument's provenance, so the first run after this lands
re-measures the base once; that is what the provenance check is for.

### 6.4 Per-module attribution

The stop message names the paths, not only the buckets. `parse_file_ops` already sees
every path and keeps only per-bucket and per-process totals; it gains
`FileOps.by_path`, the operation count per bucket-relative path (every path it counts,
directory stats included; a `.pyc` maps to its source path as the bytecode prefix
already does), written into the surface record. The delta diffs the two sides' counts
per surface and prints the paths whose count moved: new and vanished paths first, then
by size of the move. A bucket that grew with no path change says so. The judge,
`within_noise` and `reproduced` never read `by_path`: it is attribution, not evidence.
Raw traces are still discarded. This is what found 0.20.0's two leaves by hand (the
technique is in the wiki inbox note of 2026-11-11); the stop prints it so the next
person does not.

### 6.5 The CI gate

Three pieces in `ci.yml`:

1. **`budget-scope`**, one job that always runs. It checks out the pull request's head
   (not the synthetic merge) with full history and `fetch-tags: true`, asks
   `gate_scope` for the classified scope of the diff (the pull request's base to head;
   on a push to `main`, `github.event.before` to head, the range rule
   `check-breaking-marks` already has), and writes `run=true|false` to its outputs.
   The rule is a function in `gate_scope.py`, `budget_eligible(scope) -> bool`: true
   when the class set meets `{src-leaf, scripts, escalate, full}` (release-owned
   edits are already filtered before classification, so the bump's `pyproject.toml`
   and `uv.lock` do not count; `pyproject.toml` otherwise classifies `escalate` and
   `uv.lock` `full`, so a dependency move runs it with nothing named by path), or when
   the approvals file is in the diff (an entry written as an ARM-only stop's remedy is
   otherwise validated nowhere before the re-cut). The workflow names no path.
2. **`import-budget-delta`**, a matrix over the five supported minors, `needs:
   budget-scope`, `if: needs.budget-scope.outputs.run == 'true'`, `fail-fast: false`.
   Each leg installs strace, resolves the base with `base_ref` at the merge-base, runs
   `import_budget_delta.py measure --minors <one>` in the runner's checkout, and fails
   on exit 1 or 2 with the tables, the attribution and the approval set in the log.
   The base's records are restored from `actions/cache` keyed by (tag, minor,
   instrument, runner OS, runner image version); a hit still passes the provenance
   check, a miss re-measures. `timeout-minutes` is set from a measured cold leg with
   three pairs, not copied from the test jobs.
3. **`import-budget`**, the certificate: `if: always()`, `needs` the legs, one step
   that succeeds iff every leg's result is `success` or `skipped` (the
   `hostless-primary` pattern). It is the only new required context in
   `required_contexts.json`: a job-level skip passes on GitHub but reads as red to
   `ci_verdict`, and a leg absent from the run reads as "no job", so the required name
   must be a job that always runs.

The job runs on pull requests and on pushes to `main` alike, so the push run's verdict
that the release reads (§5.1) includes it. The run set is conservative by design
(`scripts` and `escalate` include tooling far from the startup path); the job's comment
says so.

The release's `budget-delta` stage is unchanged in role: the dev VM's measurement is
the ARM environment. Both environments read the same approvals file; a growth that
shows only on one is still caught on that one.

### 6.6 What `make release` prints on a stop

One stop, one block: the per-minor tables, the path attribution, both environments'
numbers when both exist, then "to approve, write tests/unit/import_budget/
approved_growth.json:" followed by the whole file as JSON (`base` set to the base tag,
every entry the deciding pairs warrant, `why` left as `"<reason>"`). The remedy is a
file edit and a pull request to `main` (the budget job re-judges it on x86), after
which the release branch is re-cut at `main`'s new tip, as any fix during a release
already is.

## 7. The matrices: verify and certify

### 7.1 Verify lanes

`make conformance-bed` and `make kmodcov` end with a verify step that leaves the tree
clean and adds no flag to the collate scripts, whose `--matrix PATH --write` is already
"fold into this file":

```
cp schemas/<m>.json reports/.<m>-candidate.json
uv run python -m scripts.collate_<m> --matrix reports/.<m>-candidate.json --write
uv run python scripts/check_matrix_downgrades.py --baseline schemas/<m>.json --candidate reports/.<m>-candidate.json
```

The candidate lives beside the baselines the release already writes
(`reports/.matrix-baseline.json`), never under `reports/*-observations/`, which both
collates read whole as records. The collate's exit statuses are what they are today
(a refusal 1, a schema problem 2, nothing written) and the lane's own pytest status is
preserved ahead of them (the lane fails on the first failure; sequencing must not mask
a measurement failure behind a green collate). A downgrade fails the lane; allowed
changes print. `make support-matrix` and `make kmodcov-matrix` remain the by-hand
folds into the committed matrices, their help saying "to accept a measured change in a
commit of your own".

### 7.2 Certify at the release

`make release-matrix` and `make release-kmodcov-matrix` run the verify lane, and when
it passes copy the judged candidate over `schemas/<m>.json` and commit it when it
differs: the committed matrix is byte-for-byte the fold the downgrade check judged
(re-folding from the records directory would be equally honest, as the collate is
pure, but copies what was judged rather than what the directory holds a moment later).
The commit subjects stay. `as_of` is documented as the date of the release that
certified the cell. No `WRITE=1`.

### 7.3 Consequences in the gate

Nothing in a landing's gate writes a tracked file any more, so `gate_tip.py`'s
`MEASURED_OUTPUTS`, the blob-witnessed promotion (`witnesses`, `satisfies`, the
`"witnessed"` promotion) and the measured-dirt split go, with their tests and the
contributing text "Two lanes measure" and "This has one accepted imprecision". The
rebase carry (`_carry`, promotion `"carried"`) and the red-marker protection stay.
Tracked dirt of any kind refuses the gate, as it should; a test over the two verify
targets asserts they write nothing under `schemas/`. `RELEASE_OWNED`'s
`_redated_matrix` stays (the release's commits are still date-only refreshes), and
`RELEASE_OWNED` gains nothing for the approvals file because the release never
touches it (§6.2). The per-change gate no longer needs the first measuring lane's
commit before the second runs.

`todo/support-matrix-refresh-at-release-2026-08-25.md` is deleted (#849: it says no
release refreshes the matrix, which `release-matrix` made untrue).

## 8. Documentation and tests

- `docs/release_process.md`: the stage list, the verdict refusal and its remedy, the
  approvals file (in force, spent), the stop block, the matrix certify meaning, the
  96 % floor, the ruleset step for the PR that lands the certificate job.
- `docs/contributing.md`: "Gating a branch before it lands" loses the measured-lane
  paragraphs; the conformance and kmodcov rows say verify; a short paragraph on the
  approvals file and the CI budget job.
- `docs/architecture/quality-gates.md` and `testing.md`: the release's stages, the
  evidence table rendered from or tested against the map, the budget jobs in CI's job
  table, `as_of`.
- `docs/architecture/startup-performance.md`: the delta's new verdict shape and the
  approvals file replace the `BUDGET_APPROVE` text.
- Tests: `tests/unit/import_budget/test_import_budget_delta.py` (judge-all, up-to
  approvals over the deciding pair, in-force versus spent, measured-tree key and the
  zero-delta short-circuit, path attribution), `test_import_budget.py` (`by_path`),
  `test_release_record.py` (pending reads the verdict, refuses, carries the fields),
  `test_release_tag.py` and `test_local_gate_status.py` (the shared formatter),
  `test_release_preflight.py` (unchanged behaviour pinned), `tests/unit/scripts/
  test_gate_tip.py` (promotion gone, carry kept), `test_gate_scope.py`
  (`budget_eligible`), `tests/unit/test_lane_invariants.py` (the lane table, the
  lease set, the canary partition pin, the certificate job present and required, the
  evidence map against `required_contexts.json` and the census), a workflow test that
  `budget-scope` calls `budget_eligible` and names no path, and the verify targets'
  clean-tree test.

## 9. How approvals expire: the alternatives considered, and what is left to the builder

The owner's decision read "cleared at the bump". The reviewers found that a reset
committed at the bump breaks the merge-to-tag window and the recertification run (the
file names a tag that does not exist, and the entries the release judged against are
gone). Three ways keep the decision's effect, that approvals never carry across a tag;
the owner chose the first (2026-11-11):

1. **Expire at the tag (§6.2, chosen).** No reset is committed.
   Entries are in force while the file's `base` is the current base tag and spent,
   with a NOTE, once a newer tag exists. The release's pull request stays the bump
   alone; recertification and the window judge against the old tag with the entries
   still in force. The next approval rewrites the file with the new base. Cost: a
   spent file can sit in the tree until someone needs an approval, which is harmless
   and printed.
2. **Reset at the bump, read the prior revision.** The bump commits an empty file
   naming the unborn tag; the delta recognises that shape and reads the entries from
   the commit before the bump. Keeps "cleared at the bump" literally; costs a resolver
   that reads git history inside the judge and a `RELEASE_OWNED` shape for the reset.
3. **Reset at the bump, carry the old entries in the file** (`"carried": {...}`).
   Same effect as 2 with the evidence in the committed file; costs a wider shape.

Left to the builder:

- Where the evidence map lives and the exact wording of the skip line.
- Whether `measure` writes the approval set to a file under `reports/budget/` as well
  as printing it.
- The CI leg's measured runtime and the `timeout-minutes` it sets.
- The exact `gate_scope.py` entry point `budget-scope` calls, and how the pull
  request's head and merge-base reach it.
- The record's field names for the trusted run.

## 10. Out of this document's reach, for the record

- #523's remainder (moving `make profile`'s ceilings out of any per-change path) is
  unchanged by this; nightly keeps rung 10.
- #829 (docs-only pull requests running every Python leg) is the same `gate_scope`
  classification applied to CI's test jobs; `budget-scope` is a first use of it in CI
  and a pattern #829 can follow.
