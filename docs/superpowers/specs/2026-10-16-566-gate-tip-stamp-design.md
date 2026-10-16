# #566 follow-up — the landing's gate is stamped and the push demands it: design

Owner rulings (Chris, 2026-10-15/16): the scoped gates apply to the commits
that PERSIST on `main` (AGENTS.md, landed as 88c9d3410); intermediate commits
that a squash erases are the contributor's business and nothing enforces
them; the commit that lands must have been gated, mechanically; when the
evidence is missing the push is REFUSED and names the command to run — the
hook never runs the heavy lanes itself; for a push carrying several commits,
only the new tip is checked. Approach chosen from three (2026-10-16): one
runner that stamps each lane it runs, over per-target Makefile wrapping and
over a single whole-run stamp.

Revisions (the ledger at `.superpowers/sdd/2026-10-16-566-gate-tip/`
holds every review): 2 (2026-10-16, after the Astra and Fable round-1 reviews): live
vs stamped lanes, the lane key, no assets field, the coverage lane, measured
outputs, revision-aware helpers, invalidation, module direction, census,
landing sequence. 3 (same day, after round 2): command-line overrides are
refused by make's own inventory and environment knobs are an explicitly
accepted boundary shared with the coverage stamp; measured-output reuse is
bound to the exact blobs the run produced; a re-run invalidates every stamp
that could satisfy the tree; the stale-base argument is replaced by "the
hook's list is authoritative"; the census derives the tip vocabulary and
its control removes a real goal from the literal; a lane starts only on a
clean tree, so a second measured lane needs a commit first; recursive
diff-tree, the lookup, statuses and the import arrows corrected. 4 (same
day, after round 3): the runner reads the coverage checker's status
directly (make folds every failure into 2) through a `coverage-gate-args`
target; promotion is materialised at the new tree so it composes across
successive measurements; `GATE_TIP_OVERRIDES` filters nothing but `BASE`;
the remote git handed the hook flows through the refusal, the remedy and
the runner's `BASE`; `--force` re-runs coverage; the range checks are
ordered exists-then-ancestor; a red measuring lane's rewritten matrix is
named; a pruned tree is an unreadable stamp. 5 (same day, after round
4): invalidation deletes every stamp for the key under every tree
(conservative, so a revoked lineage cannot be re-materialised); the
remedy fetches and gates against `FETCH_HEAD`, which a path remote also
has; the coverage-lane tests name the direct checker; `BASE` is in the
lane-env removal pin; the one-hop claim is qualified.

Supersedes `todo/566-per-commit-gate-stamp.md` (recorded 2026-10-15), whose
`gate-scope --run` mode would have stamped every commit's "this commit"
lines — the per-intermediate gate the policy no longer asks for. Also
supersedes `todo/conformance-bed-dirties-support-matrix.md` (2026-10-16):
the bed's rewrite of `schemas/support_matrix.json` is by design (the
Makefile help says "review the diff and commit it"); what needed solving is
only that committing the measurement must not void the gate (§2, measured
outputs). The landing deletes both files.

Non-goals: no change to what the oracle (`scripts/gate_scope.py`) decides
for a diff; no CI change (CI runs its own lanes after every push, never the
local-only ones); no expiry or garbage collection of stamps (a sweep of the
clone's git dir means re-running `gate-tip`); no sanitising of the
contributor's shell environment beyond what the coverage stamp does today
(§3, knobs).

Refs: #566; `docs/superpowers/specs/2026-10-14-566-test-burden-gating-design.md`
(Phase D: the oracle, the policy); `scripts/coverage_stamp.py` (the stamp
this one generalises).

---

## 1. The gap

Phase D codified the per-landing policy three ways — the AGENTS.md bullet,
`make gate-scope` as the executable oracle, and the tests that keep the
oracle complete — and the pre-push hook runs `gate-fresh` on every push to
`main`. `gate-fresh` runs lint, the API snapshot and teaching checks,
typecheck, collect-check and docs in a pristine worktree. It has NO evidence
that the heavy lanes the oracle's tip list names for the landing's diff
(`make coverage`, `nox -s tests_hostless-3.14`, `make nox-unit-repeat` when
a fixture moved, the bed lanes when a bed package moved, the `make quick`
selections of a tests-only landing) ran on the tree being pushed. Only
`make coverage` leaves a tree-keyed stamp, and nothing reads it at push
time. The oracle's tip list is advisory: a committer can read it and push.

The pre-push hook sees only the commits actually being pushed to `main`. A
squash branch pushes one commit. So "every commit in the pushed range" is
already "every commit as it will appear on main", and intermediate commits
never enter the check, with no special-casing. That is the simplification
this design rests on.

## 2. Lanes, and the lane stamp

**A lane is one line the oracle prints for the tip.** Lanes come in two
kinds, decided by two literals in `scripts/gate_tip.py`:

- **Live lanes** — `LIVE_GOALS = ["check-breaking", "lint-arch",
  "check-python", "docs", "gate-fresh"]` (every `RANGE_TARGETS` entry plus
  docs). They are history-dependent (`check-breaking` reads the commits'
  messages over a RANGE, and `lint-arch`, `check-python` and `gate-fresh`
  run it as a prerequisite) or already run by the push itself (`docs`,
  `gate-fresh`). A tree-keyed stamp is not evidence for a history check — a
  squash or an amended message keeps the tree and changes the answer — so
  live lanes are NEVER stamped. They are satisfied by the push: `gate-fresh
  --pre-push` puts `RANGE=<remote-sha>..HEAD` on the argv of the one `make`
  it runs in the pristine worktree (HEAD there is the pushed sha, detached,
  so check-breaking's "RANGE must end at HEAD" holds; the Makefile's
  `RANGE ?=` default yields to the command line and `lint-arch` passes it
  to the checker; `check-breaking` is a prerequisite of `lint-arch`, a
  `GATED_TARGETS` entry, so it runs over the real pushed pair without any
  change to `GATED_TARGETS`). A census test (§7) holds `LIVE_GOALS` to
  `RANGE_TARGETS ∪ {docs}` and holds every live goal to gate-fresh by
  REACHABILITY: it is `gate-fresh` itself, a `GATED_TARGETS` entry, or a
  target the Makefile's prerequisite graph reaches from a `GATED_TARGETS`
  entry (`check-breaking` through `lint-arch`), or an aggregate
  (`check-python`) whose prerequisites are all reached that way.
- **Stamped lanes** — every other line: `make coverage`, `nox -s
  tests_hostless-3.14`, `make nox-unit-repeat`, the bed lanes
  (`conformance-bed`, `stability-*`), `make kmodcov`, `make dashboard`,
  `make busybox`, `make quick PATHS=…`, `make quick-guards`, the ruff
  lines, `make check-ts coverage-ts`. The runner stamps them; the check
  demands the stamps. `STAMPED_GOALS` is the second literal.

**Goals.** The census and the runner parse a line the same way: for `make
…` every word after `make` that holds no `=` is a goal (`make check-ts
coverage-ts` has two, both must be known); for `nox -s X` the goal is the
session name; for `uv run ruff …` / `uv run ty …` the goal is the tool.
Every goal of a line must be in exactly one of the two literals, and a line
is live when any of its goals is live.

**Lane key.** The oracle line verbatim, minus any ` RANGE=…` decoration
(`_with_range` adds it only to live goals, so no stamped line carries one;
the strip is defined so the key is base-independent by construction). A
`PATHS=…` line is keyed on its exact path list: the key carries the
base-equality requirement (§4).

**Identity.** One JSON file per (tree hash, lane key):
`<git common dir>/otto-gate-stamps/<tree-hash>/<slug>-<8 hex>.json`
(`git rev-parse --git-common-dir`, as the coverage stamp resolves it, so
every worktree of the clone shares the store), beside today's
`otto-coverage-stamps/`. The
slug is the key's first 40 characters with everything outside
`[A-Za-z0-9.-]` as `_`, for the reader; the 8 hex are the first 8 of
`sha256(key)`. The `command` field holds the key and is what the check
compares, so a digest collision is reported as a command mismatch, never
accepted.

**Fields.** `format` (1), `tree`, `command` (the lane key), `base` (the sha
the runner classified against — recorded for the refusal's diagnosis, never
compared), `measured` (a map `path → blob sha` for each `MEASURED_OUTPUTS`
file the lane rewrote; empty for every other lane), `written_at` (UTC ISO
8601), `checkout` (where the run happened; printed in a refusal, never
compared). No interpreter field: the lanes that care (`make coverage`'s
stamp, nox) pin their own. No assets field: the only lane that imports the
built web assets is `make coverage`, whose own stamp digests them (§3).

**Written when.** Only when the lane exited 0, AND the tree was clean when
the lane STARTED (tracked dirt, blocking untracked files under `src/` or a
test root — `coverage_stamp.py`'s rules, moved to the shared module), AND
after the lane the tree hash is unchanged and the only tracked dirt is in
`MEASURED_OUTPUTS`. Before the lane runs, EVERY stamp with this key in the
store is DELETED, under every tree directory — exact, promoted copies and
their originals alike — as `coverage_stamp.py start` deletes its own, so
a red re-run never leaves older evidence usable and nothing can be
re-materialised from a lineage the re-run revoked. The cost is that a
re-run of lane K on one tree discards K's evidence for every other gated
tree in the clone; that evidence belonged to other landings, which re-run
K once. Conservative by design: lineage-aware deletion would need
`promoted_from` chains to be walked and is not worth the subtlety.
`promoted_from` stays recorded for diagnosis only.

**Measured outputs.** `MEASURED_OUTPUTS = ["schemas/support_matrix.json",
"schemas/kmodcov_matrix.json"]`: files a lane rewrites with what it measured
(`conformance-bed` → `support-matrix`, `kmodcov` → `kmodcov-matrix`; the
Makefile says review the diff and commit it). The lane's tests ran on the
tree as it was when the lane started, so the stamp is written for THAT tree
and records, in `measured`, the blob sha (`git hash-object`) of each such
file the lane changed; the runner prints them with "review and commit, or
restore".

**Promotion, and `satisfies(stamp, tree)`.** A gate run on tree S that
measured produces, in the stamps of S, a record of the exact blobs it
wrote. Committing those blobs makes a tree T that differs from S only in
measured outputs; EVERY stamp of S is then honoured at T ("promoted"), not
only the measuring lane's: the measurement is an output of the gate run,
reviewed by the contributor, and re-running an hour of lanes because a
date field moved is the burden this design removes. `satisfies(stamp,
tree)` is true when `stamp.tree == tree`, or when every path in `git
diff-tree -r -z --name-only <stamp.tree> <tree>` is a `MEASURED_OUTPUTS`
file whose blob in `tree` equals the sha some stamp under
`<stamp.tree>/` recorded for it in `measured`. So committing exactly the
matrix the run produced keeps the evidence of the whole run; an edited
matrix, or a matrix-only commit with no run behind it, does not (the
oracle classifies the `schemas/` change as `full`, and no stamp of the
earlier tree satisfies). The accepted imprecision, stated in contributing:
a test that READS a measured matrix (the matrix provenance tests) ran at S
against the pre-measurement matrix; CI runs it again at T. One function
decides this for the runner's skip and the hook's check; invalidation
never consults it (it deletes every stamp for the key across all trees,
§2 above). A `diff-tree` that fails (the stamp's tree object pruned by
`git gc` after its branch went away) makes the stamp "unreadable", never
an exception out of the hook.

**Promotion is materialised.** When the runner skips a lane at tree T
because a stamp of S satisfies T, it writes an exact-tree copy of that
stamp under `<T>/` (`tree: T`, `measured: {}`, `promoted_from: S`). So a
second measuring lane at T records its blob under T, and a tree U built
from T's measurement is satisfied from T's directory: promotion composes
one edge at a time (S → T → U), the expensive early lanes are never
re-run for a measurement, and the hook's lookup almost always hits the
exact tree. One hop always suffices: a measurement only ever happens
inside a `gate-tip` run, and that run materialises every satisfied stamp
under the tree it ran on before it records the new blob there, so the
tree a push lands on is at most one measured commit away from a directory
holding the whole set — provided that run materialised the keys the push
needs (a `gate-tip` run skips and copies exactly the lanes its own list
names; a push whose range names more lanes finds them missing and says
so). The hook never writes a copy; it only reads. Invalidation (above)
deletes by key across every tree, copies and originals included.

**Lookup.** For a (tree, key): the file under `<tree>/` first; if absent,
every other tree directory under the stamps root is a candidate when its
stamps record a non-empty `measured`, and its `<slug>-<hex>.json` for the
key, if present, is tested with `satisfies`; an unreadable candidate is
skipped (and named only if nothing else satisfies). Cost: one small read per
STAMP in the clone's history (finding the measuring stamps means reading
each), once per lane without an exact-tree stamp — the one-edge case a push of T without a `gate-tip` run at T hits;
there is no index and no expiry, and that is the stated bound.

**Why the tree hash.** A squash built from the gated tip's tree, or a
rebase or message amend that keeps the tree, inherits the evidence for every
stamped lane; what a changed history can alter is exactly the live set,
which the push re-runs over the real range.

**What a stamp does not say.** It does not say the lane is the right one;
the check recomputes the oracle's list at push time, so a lane the tables
started requiring after the stamp was written shows up as missing. Stamps
do not expire; a re-run on the same tree replaces its stamp.

## 3. `make gate-tip` — the runner (`scripts/gate_tip.py`)

**The list.** The runner imports `gate_scope` and asks it for the tip list
over `merge-base(--base, HEAD)..HEAD` (`--base` defaults to `origin/main`)
through the same `classify` + `commands(scope, tip=True, base=…)` path
`make gate-scope` prints, then partitions it by goals. It prints the
oracle's base line (`gate-tip: base origin/main (merge-base <sha9>), N
commit(s) to land`) so a stale fetch is visible by eye; the hook's list is
the authoritative one (§4).

**Overrides and knobs.** `make gate-tip` passes `--overridden NAME` for
every variable whose `$(origin)` is `command line` — a new
`GATE_TIP_OVERRIDES` built from the Makefile's existing `.VARIABLES`
inventory (`COVERAGE_STAMP_OVERRIDE_NAMES`) with NO harmless-name filter
(`BUMP=minor` is refused here; the release's allowlist is coverage's
alone), except `BASE`, which is the runner's own control (honoured from
the command line the way `gate-scope` honours it, passed as `--base`, and
recorded in every stamp's `base`) — and the runner refuses to run anything
when the list is non-empty (status 2: "came from the command line, so
this run is not the declared gate"). Each lane then runs with `MAKEFLAGS`, `MFLAGS` and
`PYTEST_ADDOPTS` removed from its environment; the only command-line
variables a lane sees are the ones the oracle wrote into its line
(`PATHS=`). Environment knobs (`CONFORMANCE_CELLS=1 make gate-tip`, a
`?=` default taken from the shell) are NOT detected, exactly as
`coverage_stamp.py` does not detect them today (its tests pin that `?=`
accepts an environment value without recording an override); this is a
stated boundary of both stamps, documented in contributing, and the
contributor is trusted on it as on intermediate commits.

**The run.** Before the first lane the runner reads the tree state and
refuses on dirt (status 2, the files named, the shared wording). Then, in
the oracle's order, each STAMPED lane: the tree must be clean when the lane
starts — if an earlier lane left measured dirt, the runner stops here with
`commit the measured outputs (<files>) and run make gate-tip again; the
stamps already written stay valid for that commit` (status 1); every
stamp for its key across all trees is deleted; the line runs as a
subprocess from the repo
root through the shell, with `MAKEFLAGS`, `MFLAGS`, `PYTEST_ADDOPTS` and
`BASE` removed from its environment (make exports every command-line
variable into recipe environments, so `BASE` would otherwise reach the
lanes), stdout and stderr inherited; after exit 0 the
runner re-reads the tree hash and the dirt: unchanged, dirt confined to
`MEASURED_OUTPUTS` → the stamp is written atomically with `measured`
filled; else → `NOT STAMPED (the tree changed under the lane: …)`, stop
(status 1). Exit non-zero → `RED (exit N)`, no stamp, stop (status 1); the
lanes after it do not run — and because a measuring lane collates its
matrix even when its tests failed, a red one leaves the matrix rewritten:
the runner says so (`the lane failed and still rewrote <file>: restore it
(git checkout -- <file>) or review it before the next run`) rather than
leaving the next run to refuse on generic dirt. Live lanes are printed as
`live at push` and never run here.

**The coverage lane** follows the release's own idiom (`Makefile`'s
`release:` line branches on the checker's status 0/1/2). `make` folds
every failing recipe status into 2, so the runner never reads the
checker's status through a make target: a new internal target
`coverage-gate-args` prints the Makefile's declared gate — the words of
`$(COVERAGE_STAMP_GATE)` ONE PER LINE (`printf '%s\n' --threshold
"$(COVERAGE_THRESHOLD)" --selection "$(M_COVERAGE)" …`, so a selection
with spaces is one line and no shell quoting is re-parsed) — and the
runner runs `uv run python scripts/coverage_stamp.py check <those lines
as argv>` itself and reads the status directly. It passes no
`--overridden`: the runner has already refused every command-line
variable except `BASE`, which the checker never sees, so the checker's
override inventory is empty by construction. (The recipe is `printf
'%s\n' $(COVERAGE_STAMP_GATE)`: the shell's own quoting of the gate's
values yields one argv element per line, nothing is restated; the runner
strips `MAKEFLAGS` for that call as for a lane and keeps empty lines as
empty elements; a gate value containing a newline cannot be framed this
way and the runner refuses it — none does today, and the Makefile pin
says so.) 0 → the coverage stamp already
proves this tree: the lane stamp is written without running coverage —
unless `--force`, which still runs the up-front check (so an incurable 2
is RED before, not after, the 40 minutes) but then runs coverage
regardless of a 0 (`coverage_stamp start` deletes the old stamp itself),
so a forced run really re-runs the tests.
2 → an incurable refusal (a foreign primary, dirt): the lane is RED at
once, before a 40-minute run, with the check's reasons. 1 → `make
coverage` runs, then the check again, and only its 0 writes the lane
stamp. The `release:` line is unchanged. So the lane stamp is the single
thing the hook reads: no alias, one reader of the coverage stamp
(`check`). `coverage_stamp.py check` itself stays exact-tree: after a
matrix commit `make release` re-runs `make coverage` — acceptable, since
`release-matrix` commits the matrix after that check anyway, and said in
the docs so nobody "fixes" it.

**Skips.** A stamped lane whose stamp `satisfies` the current tree is
reported `already stamped` and not re-run. `--force` invalidates and
re-runs every stamped lane. `--dry-run` prints the base line and each lane
with its state (`stamped` / `missing` / `live at push`) and runs nothing;
it exits 0 when nothing is missing, 1 otherwise. `--check <remote-sha>
<tip-sha> [--remote NAME]` (default `origin`) runs the hook's
`missing_stamps` and prints exactly what the hook would print (§4), exit
0/1.

**Bed lanes.** They run exactly when the oracle names them. On a box
without the lab VMs the lane is RED and no stamp is written; the push then
refuses, which is the honest outcome.

**Output and statuses.** `gate-tip: [2/5] nox -s tests_hostless-3.14 …` as
a lane starts; `… stamped (396 s)` / `already stamped` / `live at push` /
`RED (exit N, 42 s)` / `NOT STAMPED (…)` as it ends; then `gate-tip: N
stamped, N already stamped, N live at push, N red`. The script's statuses:
0 every stamped lane is stamped; 1 a red lane, a tree change under a lane,
a stop for measured dirt, or a `--dry-run`/`--check` with something
missing; 2 a refusal before anything ran (dirt, overrides). `make
gate-tip` exits with make's own status (2) whenever the script does not
exit 0; the script's status is in the output above it.

**Makefile.**

    gate-tip: ## (Quality) The landing's gate: run the oracle's tip lanes (make gate-scope, BASE=origin/main) and stamp the committed tree per lane on green; the push to main refuses a tip whose lanes are not stamped. Lanes already stamped for this tree are skipped. Refuses any other command-line variable. `uv run python scripts/gate_tip.py --force` re-runs, `--dry-run` only lists.
    	@uv run python scripts/gate_tip.py --repo . --base "$(if $(filter command line,$(origin BASE)),$(BASE),origin/main)" $(GATE_TIP_OVERRIDES)

`make gate-scope` prints one trailing line, `run them and stamp the tree:
make gate-tip`, except when its tip line is "nothing to land".
`coverage-gate-args` is added as described above; `gate-tip` and
`coverage-gate-args` join `.PHONY` and the pin that lists it.

## 4. The pre-push check

**Placement.** In `scripts/gate_fresh.py --pre-push`, right after
`ref_to_gate` resolves the pushed pair and before the pristine-worktree run:
the refusal belongs ahead of a three-minute gate. `ref_to_gate` returns
`(remote_sha, local_sha)` for the protected ref (today the local sha
alone); its three scalar tests change, the feature-ref, delete and
empty-input tests keep `None`.

**Where the logic lives.** `gate_tip.missing_stamps(repo, remote,
remote_sha, tip_sha) -> Verdict` (`remote` is the name for the messages;
the `Verdict` holds the missing lanes, the refusal lines and the remedy); `gate_fresh.main` imports `gate_tip` inside the
`--pre-push` branch (a function-level import: `gate_scope` imports
`gate_fresh`, `gate_tip` imports both, so the top level stays acyclic).

**The range.** The remote is whatever git handed the hook (`pre-push`'s
first argument, passed through to `--pre-push` as `--remote NAME`; the
refusal names it, never a hard-coded `origin`). In this order: the check
first requires `remote_sha` to exist locally (`git cat-file -e`);
otherwise: `<remote>'s main (<sha12>) is not in this clone: git fetch
<remote> main, then push again`. Then it requires `remote_sha` to be an
ancestor of `tip_sha` (`git merge-base --is-ancestor`, which exits 128 on
an unknown object — hence the order); otherwise: `not a fast-forward of
<remote>'s main (<sha12>): fetch, rebase, re-gate` — which also refuses
`git push --force <remote> main` without `--no-verify`, intended.
The all-zero remote sha (a remote with no `main`) is refused with its own
line; this repository's `main` exists and the check does not pretend to a
first-push mode. It then classifies `remote_sha..tip_sha` reading sources
AT `tip_sha`: `gate_scope` gains a `tip` revision parameter (default HEAD)
used by `head_tree`, the reader and `classify`'s head side, and
`tree_hash(repo, rev)` in the shared module takes the revision; nothing in
the check reads HEAD or the working tree, so `git push origin <sha>:main`
from a checkout whose HEAD is elsewhere is checked on `<sha>`.

**The hook's list is authoritative.** The runner classifies against the
local `origin/main`; the hook against the remote's real `main`. The two
diffs can differ in either direction (a stale fetch; a remote reset to an
earlier commit, after which the local ref is not even an ancestor of the
remote; a change and its reversal both inside one range and not the
other), and because `commands()` composes columns rather than nesting
them, a larger diff's list does not contain a smaller one's `PATHS=` keys.
No invariant is claimed: the hook recomputes, every lane it names must
have a satisfying stamp, and the refusal prints the base the hook used and
the `base` recorded in the stamp nearest the key (same goal) so the cause
is visible. Remedy, with the remote git handed the hook (a name or a
path — git passes either as the hook's first argument): `git fetch
<remote> main && make gate-tip BASE=FETCH_HEAD` — `FETCH_HEAD` is what
the fetch just wrote, so it works for a named remote whose fetch mapping
does not create `<remote>/main` and for a bare path alike; the second run
skips every lane whose key still satisfies, and a push to a remote that
is not `origin` gates against that remote's `main`, never silently
against `origin/main`.

**The refusal.** Every stamped lane in the hook's list without a satisfying
stamp is a `Missing(lane, reason)`; reasons: `no stamp`, `stamped for tree
<sha12>, which differs from the tip's beyond the measured outputs` (with
the stamp's `checkout` and `base`), `stamped for a different path list
(base <base12> vs the remote's <remote12>)`, `unreadable stamp`. On any,
stderr gets:

    gate-tip: refusing to push <sha12> to main — its gate is not stamped on tree <tree12> (range <remote12>..<sha12>):
      nox -s tests_hostless-3.14: no stamp
      make quick PATHS=tests/unit/foo: stamped for a different path list (base <base12> vs the remote's <remote12>)
    run: git fetch <remote> main && make gate-tip BASE=FETCH_HEAD   (on commit <sha12>), then push again.
    git push --no-verify skips this check; CI then runs only its own lanes — never make coverage, nox-unit-repeat or the beds.

and the hook exits 1 before any worktree is created. Non-main refs and
deletes are unchanged: no check.

**Cost.** A `git diff --name-status` over the range, the oracle's source
reads at the tip, one file read per stamped lane plus, for a lane without
an exact-tree stamp, the lookup's scan (§2: one read per stamp in the
store, plus the git comparisons for each candidate). Seconds in practice;
the scan's growth is the stated bound, before a three-minute gate.

## 5. The shared module (`scripts/gate_stamp_common.py`)

A leaf: it imports nothing from the other scripts. It holds what the two
stamps share: `GateFreshError`, `TreeState` and `tree_state` (moved from
`gate_fresh`, which re-exports all three so its callers — `coverage_stamp`,
`gate_scope` — and tests are unchanged), `tree_hash(repo,
rev="HEAD")`, `blob_sha(repo, path)`, `test_roots`, `blocking_untracked`,
`dirt_refusals(state, roots)` (the dirt half of `coverage_stamp.refusals`;
the override half stays in `coverage_stamp`, and `gate_tip` applies its
own no-allowlist rule), `write_atomically`, `stamps_root(repo, name)`.
`coverage_stamp` imports these and keeps its verbs and `Gate` unchanged;
its tests stay green unchanged, and one new test pins that both stamps use
the shared `dirt_refusals`. Import direction (importer → imported):
`coverage_stamp` → `gate_stamp_common`; `gate_fresh` → `gate_stamp_common`;
`gate_scope` → `gate_fresh` (as today); `gate_tip` → `gate_scope`,
`gate_fresh`, `gate_stamp_common`; `gate_fresh.main --pre-push` →
`gate_tip` lazily.

## 6. Policy and docs

- **AGENTS.md**, the gating bullet, one added sentence: "The landing's tip
  is what is gated and stamped: `make gate-tip` runs the oracle's tip lanes
  and stamps the committed tree per lane, and a push to `main` refuses a tip
  whose lanes are not stamped (the history checks and docs run at the push
  itself)." The intermediate-commit wording stays.
- **docs/contributing.md**, the gating section: what `gate-tip` does and
  skips, live versus stamped lanes, where stamps live and why the tree
  hash, measured outputs and the commit-then-rerun step, the overrides rule
  and the environment-knob boundary, the stale-fetch case, the refusal
  text and the two ways out (`git fetch origin main && make gate-tip`;
  `--no-verify`, with the honest note that CI never runs coverage or the
  beds). The release workflow's coverage-stamp mention links here instead
  of restating.
- **docs/architecture/quality-gates.md**: a `gate-tip` row in the lanes
  table ("not in CI — enforced by the pre-push hook on every push to
  main"); the coverage-stamp paragraph becomes the lane-stamp paragraph
  with coverage as one stamped lane of it and the live set named.
- **`.githooks/pre-push`** header: one sentence that the hook also demands
  the landing's lane stamps and runs the history checks over the pushed
  range.
- `todo/566-per-commit-gate-stamp.md` and
  `todo/conformance-bed-dirties-support-matrix.md` are deleted.

## 7. Tests

- `tests/unit/scripts/test_gate_tip.py` — a fake lane runner records
  argv, cwd and env; a real temporary git repo (the `TmpGitRepo` fixture
  pattern of `test_gate_fresh.py`) supplies trees and histories:
  - the runner executes exactly the oracle's STAMPED lines in order, never a
    live one, with `MAKEFLAGS`, `MFLAGS`, `PYTEST_ADDOPTS` and `BASE`
    absent from each lane's env while present in the runner's;
    `--overridden X` refuses with status 2 before any lane;
  - stops at the first red: no stamp for it, none for the lanes after, and
    every stamp FOR THAT LANE'S KEY in the store is gone — under the
    current tree, under another tree that promoted it, and under the tree
    it was promoted from (red after `--force` leaves no evidence for that
    key anywhere, while the lanes before it keep theirs); an interrupted
    lane (the fake raises) likewise leaves none for its key; the lineage
    sequence is pinned: a red K at U, then a plain `gate-tip` at T whose
    K run is ALSO red (or interrupted), does NOT skip K (nothing is left
    to promote) and `--check` at U still names K; while a GREEN K at T
    is new evidence and legitimately satisfies U again through T's
    measurement witness;
  - `--force` with both a lane stamp and a valid coverage stamp present
    runs `make coverage` (the fake records the call), not only the check;
  - a lane whose stamp `satisfies` the tree is skipped; one whose key
    differs (a different `PATHS=` list) is run; `--force` runs all;
    `--dry-run` runs nothing and exits 0/1 by what is missing; `--check`
    prints the hook's text;
  - refuses on dirt before running anything (status 2); withholds the stamp
    and stops when a lane changes the tree hash, or dirties a tracked file
    outside `MEASURED_OUTPUTS`; stamps with `measured` filled and prints
    the review note when the only dirt is a measured output; stops before
    the NEXT lane when measured dirt is present, with the commit-then-rerun
    line;
  - `satisfies`: the exact tree; a tree that differs only by the recorded
    blob of a measured output (true); the same path with a different blob
    (false); a matrix-only commit with no run behind it (false); a
    difference outside the measured outputs (false); `diff-tree` is
    recursive (a tree whose only change is under `schemas/` is compared by
    file, not by subtree);
  - the coverage lane: the fake runner answers `make -s
    coverage-gate-args` with lines and asserts the checker call is `uv run
    python scripts/coverage_stamp.py check` followed by exactly those
    lines as argv, no `--overridden`; the fake checker returning 0 up front
    stamps the lane without running coverage; 2 is RED before coverage
    runs; 1 runs `make coverage` then the checker again, and only its 0
    stamps; a green `make coverage` with the checker still failing is RED
    with the checker's output shown;
  - promotion: a stamp of a NON-measuring lane (coverage) at S is honoured
    at T = S + the recorded matrix blob, through another stamp's `measured`
    record; the skip at T writes the exact-tree copy under `<T>/` with
    `promoted_from: S`; the S → T → U sequence (an early non-measuring
    lane, then two measuring lanes each committed in turn) keeps every
    early lane satisfied at U, and a `--force` red at U removes the copies
    and the originals for that key;
  - `coverage-gate-args` is the only thing the runner asks make for in the
    coverage lane, never a status;
  - the file name is `<slug>-<8 hex>`; two keys differing only in
    characters the slug folds get two files; a stamp file whose `command`
    is not the key is a mismatch, not evidence; a malformed or
    missing-field stamp is `unreadable stamp`;
  - a stamp written in one worktree is honoured from another worktree of
    the same clone (real `git worktree add`);
  - statuses and the summary line pinned.
- `tests/unit/scripts/test_gate_fresh.py` additions — `missing_stamps` over
  a real temporary repo with a bare "remote" repo:
  - a stamp for tree T is honoured by a different commit with tree T (a
    squash built with `commit-tree`, and a message amend); the same message
    amend that drops a breaking marker is REJECTED by the live history
    check — the pristine `make` argv carries `RANGE=<remote>..HEAD` and
    `lint-arch` (argv pin), `check-breaking` is reachable from `lint-arch`
    in the Makefile's prerequisite graph (reachability pin), and an
    end-to-end case runs the real `check_breaking_marks.py` over the
    amended range;
  - a push to a remote named other than `origin` (a second bare repo
    named `upstream`) and a push to a bare PATH with no remote name: the
    refusal and the remedy name what git handed the hook, the remedy's
    exact text ends in `make gate-tip BASE=FETCH_HEAD`, and following it
    (a real fetch, then the runner with `--base FETCH_HEAD`) classifies
    the range the hook classified;
  - a tip that differs from the stamped tree only in a measured output with
    the recorded blob is honoured; one that differs elsewhere, or whose
    matrix blob differs, is named with the stamp's checkout and base;
  - the check reads sources and the tree at the PUSHED sha while HEAD is
    another commit;
  - a lane the oracle started requiring after the stamp (a monkeypatched
    table) is `no stamp`;
  - a multi-commit push demands the last tree only; a non-fast-forward, a
    remote sha absent locally, a remote reset behind the local tracking ref
    (the hook's larger list wins and names the missing lanes), and the
    all-zero sha each refuse with their line;
  - live goals are never demanded; the refusal is printed and the hook
    returns before `gate()` is called (a sentinel `gate` that fails the
    test if reached);
  - the refusal text is pinned including both remedies; a push to a
    non-main ref runs no check; `ref_to_gate`'s pair;
  - the existing dirty-pre-push integration test gets the stamps it needs
    (or a stub checker) AND a real remote sha on its stdin line (today it
    feeds the all-zero sha, which the new check refuses first) so it still
    reaches the behaviour it pins; the executable-hook and `--pre-push`
    wiring pins stay, and the hook's argv pin gains `--remote`.
- `tests/unit/scripts/test_coverage_stamp.py` stays green unchanged; one new
  test pins the shared `dirt_refusals`.
- **Census** (in `test_gate_tip.py`): derive the tip vocabulary by driving
  `commands(tip=True)` with synthetic scopes for every class, every
  `SPECIAL_TREES` row, every `LANE_MARKERS` mark (a changed test source
  carrying the mark), `STABILITY_FILES`, each `BED_LANES` package,
  `REPEAT_TRIGGERS` and `BUILD_FILES`, with the default base and a sha base;
  parse every emitted line's goals with the runner's parser and assert each
  goal is in exactly one of `STAMPED_GOALS` and `LIVE_GOALS`. Controls
  through the same checker: removing a real goal (`conformance-bed`,
  `kmodcov`, `dashboard`) from `STAMPED_GOALS` reddens the census — proving
  the route that emits it is visited; and removing `check-breaking` from
  `LIVE_GOALS` reddens it. A second pin: `LIVE_GOALS == RANGE_TARGETS ∪
  {"docs"}`, and every live goal is reachable from gate-fresh: it is
  `gate-fresh`, or a target the Makefile's prerequisite graph (from the
  recipe map the release-lanes tests already parse) reaches from a
  `GATED_TARGETS` entry, or an aggregate whose prerequisites are all so
  reached. Unknown goal at run time: the runner refuses the whole run
  (status 2, "the oracle emitted a lane this runner does not know: …"),
  never silently stamping or skipping it.
- Makefile pins (in the module that pins make recipes today): `gate-tip`
  passes `--base` from `BASE`'s command-line origin and
  `$(GATE_TIP_OVERRIDES)`, which filters nothing but `BASE`;
  `coverage-gate-args` prints the words of `$(COVERAGE_STAMP_GATE)` one
  per line and a real `make -s coverage-gate-args` round-trips a
  selection with spaces as one argv element; `.PHONY`
  and the release-lanes pins updated; `gate-scope` prints the trailing
  remedy line only when there is something to land; end-to-end against the
  real Makefile with the script stubbed to print its argv: `make gate-tip
  X=1` shows `--overridden X`, `make gate-tip BUMP=minor` shows
  `--overridden BUMP` (a name coverage's allowlist would drop), and
  `make gate-tip BASE=upstream/main` shows `--base upstream/main` and no
  `--overridden`.

## 8. Open question for the owner (from the Fable round-4 review)

A committed measurement is a change to `schemas/support_matrix.json` or
`schemas/kmodcov_matrix.json`, paths with NO oracle row, which the oracle
classifies as `full`. For a landing that was already `full` nothing
changes and promotion carries everything. But a measuring lane is also
reached WITHOUT a full gate: `make kmodcov` from a tests-leaf landing
through a test file carrying the `kmodcov` mark, and `conformance-bed`
from a conformance-only landing through its `SPECIAL_TREES` row; the
matrix commit then turns such a tip into `full`, owing `make coverage`
and the nox hostless lane at T, which never ran at S and cannot be
promoted — the cases where a measurement does cost the heavy lanes.

**Ruling (Chris, 2026-10-16, at the spec review): add the oracle row.**
The two measured outputs get a row of their own in `scripts/gate_scope.py`,
class `tests-leaf`-shaped, owing `make quick PATHS=<the unit modules that
read the matrices>` — today `tests/unit/test_support_matrix.py`,
`tests/unit/test_matrix_downgrades.py`, `tests/unit/test_kmodcov_matrix.py`
and `tests/unit/test_collate_kmodcov_matrix.py` — plus `make gate-fresh`
at the tip like every tests-leaf tip; so a measurement commit owes exactly
the tests that read the matrix. The row is pinned the way every oracle
row is (the table pin and the census in `tests/unit/scripts/test_gate_scope.py`),
and the reader list is derived in the pin from a grep of the test tree for
the two file names, so a new reader cannot appear without the row growing.

## 9. Landing shape

One squash branch off `origin/main`. Commits in order: this spec (and its
revisions) alone; the shared module (coverage stamp unchanged in
behaviour, its tests green); the oracle's `tip` revision parameter and
`tree_hash(rev)`; the runner + Makefile targets + tests; the pre-push
check + `ref_to_gate`'s pair + `--remote` + `RANGE` on the pristine
run + tests; docs + AGENTS.md + the two todo deletions.

The hook executes the script of the PUSHING checkout, not of the pushed
commit, so the first landing is checked by the new code only when pushed
from a checkout that holds it. The sequence: final commit on the branch →
`make gate-tip` there (the new runner gating its own tree; the oracle's
list for `scripts` + `docs` + `.githooks` + `Makefile` changes is the full
gate, with `nox-unit-repeat` if a fixture moved) → `make gate-fresh` (the
AGENTS.md hand-back rule) → the squash built with `commit-tree` from that
tree → `scripts/gate_tip.py --check <remote-sha> <squash-sha>` → `git push
origin <squash-sha>:main` FROM THIS WORKTREE, so the new check and the new
`RANGE`-carrying gate-fresh run on the push. `build(gates)!:` with a
BREAKING CHANGE footer: a push to `main` now refuses an unstamped tip.
