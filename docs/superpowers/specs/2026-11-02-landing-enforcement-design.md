# Landing enforcement — design

**Superseded in part (2026-11-06):** `BED_REACH` no longer holds for the
`scripts` class as a whole, only for a changed script the bed half runs on
(`BED_SCRIPTS` in `scripts/gate_scope.py`). The current rule is in
`docs/contributing.md`, "Gating a branch before it lands".

## Why

Two facts about the landing flow of #795 and #797 came together on
2026-11-01.

The first is where verification belongs. The dev VM's lab is the only
thing the pull request's CI cannot reach; every other check the local gate
runs, the hostless suite, the docs build, the type and lint lanes, CI
reproduces on pristine runners across five interpreters before anything
lands. Running them locally too costs a `make coverage` of 1,369 s at the
last tip, serializes the author behind the bed, and certifies an
environment CI never sees. The maintainer's ruling: the local gate is
**cheap static checks plus bed-only testing**, and everything bed-agnostic
is CI's. "That keeps the dev host relatively unburdened except for the
jobs it needs to do, plus cheap checks to not waste CI runner time."

The second is how a landing reaches `main`. The flow landed by #795 is a
fast-forward `git push origin HEAD:main` of the gated, CI-green head, with
GitHub's merge button banned, because the client-side pre-push hook is the
only thing that knows whether the head's bed lanes are stamped and every
button method makes a sha the hook never sees. The maintainer finds the
button "much more convenient", used it for #822 (a rebase-merge whose tree
equalled the gated head because `main` had not moved, so nothing was lost,
and nothing checked that), and asked that every check the button skips
live in CI or in `main`'s ruleset instead: **all verification codified**.
He also asked for the reverse guarantee: a pull request's CI must not run
on a head whose local gate has not passed.

A third fact surfaced while this was reviewed. `main`'s ruleset (id
15843311) requires eighteen CI contexts on every ref update and has no
bypass actor. A ruleset's required-check rule refuses a push whose sha has
no passing checks, so the release's own `git push --follow-tags` of its
version-bump and matrix commits, which never ran CI, is refused today.
Nothing has released since #795 added the rule. This design has to settle
that too.

This design does the three. It moves the bed evidence to a GitHub commit
status the ruleset requires, lets the button land a pull request, gates
every branch push on the slim local gate, closes `main` to direct pushes
altogether and routes the release through a pull request of its own, and
documents what remains enforced only by convention. The maintainer's
words for the shape: "all changes pushed to PR branches, with a codified
set of local gates required before the changes can be pushed to a PR
branch; then the merge into main is just clicking the merge button",
which gives a maintainer the diff to review and a one-click landing. It is the bridge until the dedicated bed runner of #801, which
turns the bed lanes into ordinary CI jobs and retires every mechanism here
except the ruleset.

## Goals

1. **One local gate for every class: static plus bed.** `make gate-tip`'s
   tip list is `make check-breaking` over the landing range (live, at the
   push), the static stamped lanes for any diff that touches Python
   (`make check-python`, made tree-pure, and `make collect-check`), and
   the bed lanes: `make coverage-bed` for any diff the bed half can reach,
   the diff's `BED_LANES`, and the bed members of the special rows. `make
   coverage`, `make quick`, `make quick-guards`, `make check-ts`, `make
   coverage-ts`, `make docs` and `make gate-fresh` leave every tip: CI
   runs their hostless substance, and `make coverage` plus `make
   coverage-ts` stay the release certificates. A `none`, `docs` or `web`
   diff, the release's own commits among them, has the tip `make
   check-breaking` alone (plus a special row's bed lane when its inputs
   change): the web code is small and cheap to test and CI covers it; a
   future change can give it Python's treatment once its scale justifies
   the complexity (D9). The commit column keeps its scoped lines and loses
   `make coverage` and `make gate-fresh`; it remains the author's working
   check, never a gate.
2. **The local gate is a GitHub commit status.** When every stamped lane
   of the tip is green for the head's tree, the gate posts a `local-gate`
   status on the head sha carrying the tree hash, the oracle base and a
   digest of the lane keys. `main`'s ruleset requires it beside the CI
   contexts. A rebase that keeps the tree keeps the stamps; re-posting for
   the new sha is the seconds `make gate-tip` costs on a stamped tree.
3. **No validation job before the local gate.** CI's first job,
   `gate-admission`, waits for a `local-gate` status on the pull request's
   head whose tree, base and lane digest match what the job computes
   itself at the head revision, and fails otherwise; every validation job
   depends on it. On a push to `main` it succeeds at once, so `main`'s CI
   is unchanged. An ungated push costs the admission job's polling window
   and no validation job.
4. **The pre-push hook gates every branch push, and refuses `main`.** A
   push of any branch whose tip's stamped lanes are missing is refused,
   with the remedy; `check-breaking` runs over the landing range of the
   checked-out tip; a push to `refs/heads/main` is refused outright with
   "open a pull request", and the ruleset refuses it too (its
   `pull_request` rule; no actor may bypass it for a push). `git push --no-verify` remains
   the documented escape hatch for the branch push, and the status is
   what catches a push that used it.
5. **The bed lease is taken only for bed lanes.** `make gate-tip` runs the
   static lanes first and acquires the lease only when the tip list holds
   a bed lane; a hostless tip never waits behind a peer's bed run.
6. **The button lands.** Repository merge methods become "Rebase and merge"
   only; `required_linear_history` stays; "require branches to be up to
   date" stays off. The maintainer merges; authors push branches and open
   pull requests. The docs say so, and say what the off switch gives up.
7. **The release lands through a pull request.** `make release` runs on a
   release branch, commits what it commits today without tagging, pushes
   the branch and opens the pull request; the maintainer rebase-merges it;
   a new `make release-tag` tags `main`'s tip and pushes the tag, which
   fires the release workflow as today. No actor may bypass the ruleset
   for a push, so nothing reaches `main` but the button.
8. **Every check the button skips is named, with where it now lives.** The
   audit table below; the docs carry the same table.
9. **The censuses pin all of it**: the tip composition, the hook's ref
   policy, the status contents, CI's job graph and the ruleset's required
   contexts.

## Non-goals

- Stitching coverage across environments. Per pull request, CI's primary
  hostless lane enforces its own floor (94.75 today, measured, not a
  target); the combined 95.5 floor over hostless plus bed is judged by
  `make coverage` at release only. The bed lanes stay uninstrumented.
  Agreed as "a healthy balance" on 2026-11-02.
- A merge queue or strict up-to-date checks. The gap they would close (a
  rebase-merge of a branch behind `main` lands a tree no gate ran on) is
  documented, not closed; CI on `main` runs after the landing and the
  landing rate is low. #801's runner is the structural fix. The admission
  job compares the status's base to the merge base it computes, never to
  `main`'s tip: that is which side of the ruling it sits on.
- A conventional-commit lint in CI. Rebase-merge keeps the pull request's
  commits and messages; a check that every landing commit parses as
  conventional and carries its trailer is a follow-up issue.
- Making the status unforgeable. It is an attestation anyone with push
  access can post with `gh api`, as `--no-verify` skips the hook. The
  trust model is unchanged: author honesty plus CI; the status makes the
  honest path checkable, it does not prove a bed run. The docs say so.
- Changing `make coverage`, the coverage stamp, the release's stages or
  its stamp exemption (`gate_scope.release_owned`, which classifies the
  release's version and matrix edits `none`; `CHANGELOG.md` is `docs`;
  with goal 1 neither asks a stamp, so a release branch whose matrices
  only re-dated pushes on `check-breaking` alone and `test_gate_tip.py`'s
  no-stamp release case stays green; a matrix whose verdicts improved is
  an ordinary `tests-leaf` change and the release's `make gate-tip` runs
  its static lanes). What changes in the release is where its commits go,
  that its branch is gated like any branch, and when the tag is made
  (below).
- The dedicated runner (#801), Terraform (#802, #803), capability pools
  (#804).

## The audit: what the hook enforced, and where it lives now

| Check | Today (hook on a push to `main`) | After this design |
| --- | --- | --- |
| bed lanes stamped for the tree | `gate_tip.missing_stamps` in the hook | the same check in the hook, on every branch push; the `local-gate` status, required by the ruleset and verified by `gate-admission` |
| ruff, ty, tach, ast-grep, the canaries | `gate-fresh` in a pristine worktree | `make check-python`, a stamped lane of every Python-touching tip (tree-pure once `lint-arch` stops pulling in `check-breaking`); CI `lint-python`, `typecheck-python` (the nox `typecheck` session is changed to run `make typecheck-python`, so CI checks the registry typing fixtures as the target does; today it runs `ty check` alone) |
| `check-breaking` over the landing range | `gate-fresh` → `lint-arch` | the hook, over `merge-base..tip` of the checked-out tip; CI `lint-python`'s `check-breaking-marks` over the PR's base..head. After the split `make release` reaches no `check-breaking` (it reached it only through `check-python`, over an empty range on a caught-up `main`); the release docs stop implying it runs |
| `check-api-snapshot`, `check-api-teaching` | `gate-fresh` | CI `tests` (the snapshot test), CI `lint-python` (teaching) |
| `collect-check` (every module under the test roots imports, `tests/integration` and `tests/conformance` and the `--doctest-modules` helpers included) | `gate-fresh`, in a pristine worktree | `make collect-check` as a stamped lane of every Python-touching tip, and as a step of CI `lint-python` (a pristine, assets-absent runner); CI's hostless lanes collect only `tests/unit` and `tests/e2e` |
| `uv sync --locked` (the lock matches `pyproject.toml`) | `gate-fresh`, before its targets | `make lock-check` (`uv lock --check`), the first recipe line of `check-python` (a recipe line, not a prerequisite, so the order holds under `make -j`) and the first step of CI `lint-python`; `make gate-tip`, `make gate-status` and the hook enter Python through `uv run --locked`, so nothing in the gate can rewrite the lock before it is checked |
| `docs` (strict Sphinx, doctests) | `gate-fresh` | CI `docs` |
| fast-forward of `main` | the hook | moot: the hook refuses every push to `main` and the ruleset's `pull_request` rule, which no actor may bypass for a push, refuses it too; the button's rebase-merge is linear by `required_linear_history`; the behind-`main` gap is documented |
| the pushed sha is the gated sha | the hook (same sha) | the status binds tree, base and lanes; `gate-admission` recomputes all three at the head revision; the button's new shas carry the same tree only when the branch is up to date with `main` |
| `gate-fresh`'s reason to exist: a local tree carrying gitignored build artifacts CI lacks | the pristine worktree | CI itself: every PR job runs on a pristine runner before the landing |
| (not enforced today either) `busybox-artifacts (…)` is not in the ruleset's required list | — | unchanged; noted so the audit is complete |

`make gate-fresh`, `scripts/gate_fresh.py`'s pristine-worktree gate and its
stamp store are retired (D1). The hook keeps the one thing from that
script that is not a simulation: the pre-push stdin parsing, which moves
to `gate_tip.py`.

## Design

### The plan: one function

`gate_scope.plan(repo, base_sha, tip_sha)` returns a `Plan`: the
immutable identity it was computed for (`base_sha`, `tip_sha`, `tree`),
the live line with its range already formatted for that tip (the range
formatting that lives in `_with_range` today, given the tip instead of
`HEAD`), the stamped lane keys in order, and whether any key is a bed
goal. It is the one place the tip is decided, and the three consumers call
it with the same arguments: `make gate-tip` (to run and stamp), the hook
and `make gate-status` (to check stamps and post) and `gate-admission` (to
recompute the digest). Stamp validation (`missing_stamps(repo, plan)`)
reads the plan's `tree` and `base_sha`, never `HEAD`'s, so a non-`HEAD`
tip is judged on its own tree, as today. **Validation takes no lock of
its own**: the caller owns the store lock around it, shared for the hook's
read, exclusive for a publisher, so no caller nests the lock the store
warns against. The goal
classification it needs (`goals()`, `is_live()`, `lane_key()`,
`LIVE_GOALS`) moves from `gate_tip.py` to `gate_scope.py` beside
`STATIC_GOALS` and `BED_GOALS`, and `gate_tip` imports them: `gate_tip`
already imports `gate_scope`, so the plan can be decided in one module
and the admission job never imports the runner. `missing_stamps` takes
the plan's keys and returns the missing ones; it never decides the list.
The lane digest is `sha256("\n".join(keys))[:12]` over the plan's stamped
keys, computed in `scripts/local_gate_status.py` from the keys the caller
passes; an empty key list (a `none`, `docs` or `web` tip) has a digest
like any other, and "every stamped lane is green" is vacuously true for
it, so a docs-only pull request earns its status too. An **empty range**
(`base == tip`: the first push of a release branch, a tree identical to
`main`) is a plan with the live line alone; `check_breaking_marks` exits
0 on it, the hook validates nothing and still lets `gate-status` post,
and `gate-scope`'s "HEAD is the merge base, nothing to land" output and
its pin stay as they are. `make gate-tip`'s default base goes through the
same base resolver as the hook (`git fetch <remote> main`, `FETCH_HEAD`),
so the three consumers see one `main`; `BASE=` stays the override and
`--dry-run` fetches nothing.

### The tip, per class

`gate_scope` gains three constants and loses the three-way split.

- **`STATIC_GOALS = ["check-python", "collect-check"]`**: tree-pure,
  stamped, emitted when **`STATIC_REACH`** holds: the tip's classes
  include `tests-leaf`, `tests-structural`, `src-leaf`, `scripts`,
  `escalate` or `full` (`PYTHON_CLASSES`), or any changed path ends in
  `.py` (a Python file under `docs/examples/` is classed `docs` for its
  bed selection and still gets the cheap static lanes). The release's
  `pyproject.toml` and `uv.lock` bumps are `release_owned` and class
  `none`, so they trigger neither. `make check-python`'s recipe runs
  `lock-check` first and then `$(MAKE) lint-python typecheck-python
  lint-arch` as recipe lines, not prerequisites (prerequisites would run
  before the lock check, and in any order under `-j`); `lint-arch` drops its
  `check-breaking` prerequisite so the goal depends on the tree alone (the
  Makefile's "check-breaking is a prerequisite here" rationale, the
  quality-gates row that says `lint-arch` pulls it in, the contributing
  sentence "`check-python` carries a history check" and the nox `lint`
  docstring follow). `check-breaking` keeps its own live line in every
  column, so it runs once per tip.
- **`BED_GOALS = ["coverage-bed", "conformance-bed", "stability-unix",
  "stability-tunnel", "stability-embedded", "docs-captures-check",
  "kmodcov", "chaos", "chaos-embedded"]`**: the lanes that take the bed
  lease (the lease census pins that each calls `bed_acquire`). `kmodcov`
  carries host-side cross-build proofs beside its bed execution; it stays
  whole, a stated exception. `gate_tip.STAMPED_GOALS` becomes
  `STATIC_GOALS + BED_GOALS`, derived, and `LIVE_GOALS` becomes
  `["check-breaking"]` (`LIVE_GOALS == RANGE_TARGETS` after the split).
- **`COMMIT_ONLY_GOALS = ["quick", "quick-guards", "check-ts", "test-ts",
  "docs", "lint-arch", "ruff", "ty", "conformance", "busybox",
  "coverage-unix", "dashboard", "dashboard-soak", "stability-unit"]`**:
  what the commit column may emit beyond the tip's goals; never stamped,
  never in a tip. `coverage`, `coverage-ts` and `gate-fresh` appear in no
  column. Venues for what leaves the stamped set: CI runs
  `tests_hostless`, `check-ts`, `dashboard-e2e`, `busybox-artifacts`;
  nightly's `stability-matrix` runs `stability-unit`; nightly gains a
  `dashboard-soak` job (`make dashboard-soak`, chromium) so the soak keeps
  a venue; `coverage` and `coverage-ts` are the release's.

The tip for a diff is, in order: `make check-breaking`; when
`STATIC_REACH` holds, `make check-python` and `make collect-check`; `make
coverage-bed` when **`BED_REACH`** holds; `scope.bed_lanes`;
`scope.special_tip` filtered to `BED_GOALS`. `BED_REACH` is one
predicate: the classes include `src-leaf`, `scripts`, `escalate` or
`full`; or a changed `tests-leaf`/`tests-structural` module is in the bed
half (under `tests/integration`, or any of its tests carries an
`integration` or `embedded` mark in the oracle's complete routing view,
which reads module, class, function and parameter marks; a module mixing
hostless and bed tests is in the bed half, and a pin uses one); or the
oracle recorded a `full_at_commit` reason for the diff
(an unreadable `pytestmark` or decorator, no surviving root, trees gone),
the conservative fallback, pinned with synthetic unreadable-mark cases. A
hostless unit-test change takes no lease. A bed test module's scoped
`make quick` at the tip is replaced by the whole bed half: coarser and
longer (416 s against a few), the price of one lane. The
`tests/integration/busybox_bed/*` special row's tip becomes empty (its
items are `integration`-marked, so `coverage-bed` runs them) and
`_route_lane`'s full-at-tip branch goes. A `docs` diff whose files are
capture inputs still adds `docs-captures-check` through its special row.

The commit column keeps its scoped lines (`quick` over `quick_paths`,
ruff, ty, `lint-arch`, `quick-guards`, `docs`, `check-ts`, `test-ts`, the
special rows) and its "full column" for `escalate`, `full` and the
no-surviving-root reasons, which becomes `STATIC_LANES` (`make
check-python`, `make check-ts`, `make docs`) plus `make coverage-bed` plus
the bed lanes: `TEST_SUITE_LANES` is `["make coverage-bed"]`. An
escalating commit therefore has no local hostless run; the docs name `make
coverage-hostless` as the by-hand working check for one, and CI owns it.
`full_gate()` and `FULL_GATE` stay for the commit column; `full_at_tip`,
`SRC_LEAF_TIP_LANES`, `Scope.quick_tip`, `_tip_quick` and `consumers()`
go (the tip runs no `quick`, and the consumer scan served only it); the
`(src-leaf-tip)=` prose goes with them.
`TestConsumers.test_the_gate_pin_reader_shares_the_import_walk` moves out
of that class before it is deleted (`_imports_a_gate_pin` still serves
`matrix_readers`).

Pins, by name (the builder finds them; line numbers drift):
`test_the_full_gate_is_pinned_literally`,
`test_the_full_gate_runs_every_static_lane_before_any_test_suite`; the tip
census (`test_every_item_in_a_src_rows_universe_is_selected_by_its_tip_lanes`)
becomes two falsifiable claims: every `BED_REACH` tip emits `coverage-bed`,
and every item of the row that `make coverage` selects and the tip does
not is in CI's `tests_hostless-3.10` selection, read from the noxfile as
the partition pin reads it; the commit census
(`test_every_collected_item_is_selected_by_the_oracles_commit_lanes`)
accepts the same two venues, local lines plus that CI selection, for the
hostless fallback cases, and its negative control
(`test_the_census_reddens_when_a_full_column_hides_a_lane`) stays, so
losing a special lane still reddens the census; `test_no_tip_list_runs_the_hostless_matrix_locally`
becomes: every tip line's goal is in `STATIC_GOALS + BED_GOALS +
["check-breaking"]`, and every commit line's goal is in that set plus
`COMMIT_ONLY_GOALS`; `TestConsumers` (deleted after the move);
`_RANGED_TARGET_LINES`, `_OUT_OF_SCOPE_LINES`,
`test_live_lanes_are_range_targets_plus_docs` (now `LIVE_GOALS ==
RANGE_TARGETS`), `test_every_live_goal_is_reachable_from_gate_fresh`
(deleted), the goal census's seen-set, `test_lane_invariants`'s lease
census and aggregator pins (read `BED_GOALS`; the aggregator takes the
lease conditionally, below), its exhaustive Makefile-target census (the
new targets `lock-check`, `gate-status`, `release-tag`; `check-python`
becomes a recipe-only target, so the census and every `make_reachable`
pin in `test_gate_scope.py` that read its prerequisite list follow),
`test_gate_tip.py`'s no-stamp release case (unchanged, now a pin of goal
1's `none` rule), and a hook pin for the identical-tree first push of a
branch (empty range).

The measured cost of `check-python` and `collect-check` in the working
tree is recorded in the pull request before the docs promise a number;
`lint-arch` runs a `uv run --group lint` sync and two pytest legs, so "a
minute" is a guess until measured.

### The lease, only for bed lanes

The `gate-tip` recipe stops wrapping the whole run in `bed_acquire`; the
runner takes the lease itself, through `scripts/bed_lease.py`'s existing
acquire and release, with its own pid as the holder and the `--lab` it
already receives, **immediately before the first missing bed lane that
will actually run**, and keeps it through the last (the `quick` recipe's
conditional acquire is the precedent). A plan whose bed lanes are all
stamped takes no lease, and `--dry-run` takes none (its pin requires no
runner calls and no writes). The interrupt path reaps and releases only
when the lease was taken; the two interrupt pins that expect cleanup
before any acquisition change accordingly. The aggregate ownership the
lease census pins (one `gate-tip` holder, lanes re-entrant under it) is
unchanged while bed lanes run. The lease is released before publication
(the POST never holds the bed). Pins: a static-only gate goes green while
a fake holder owns the bed; a gate whose bed lanes are already stamped
never calls acquire.

### The status: one script

`scripts/local_gate_status.py`, stdlib only, owns everything about the
status, so Python writes it and Python reads it, and no bash parses it.

- **Format** (`format_description(tree, base, lane_digest)`): `v1
  tree=<40 hex> base=<40 hex> lanes=<12 hex>`, 113 characters exactly;
  GitHub refuses descriptions over 140 and a pin holds the length.
  `parse_description()` returns `(tree, base, lane_digest)`; the keys are
  not recoverable, only compared by digest. `lane_digest(keys)` is the
  one digest function.
- **`post`**: given a sha, the tree, the base and the digest, `POST
  repos/<owner>/<repo>/statuses/<sha>` through `gh api` with
  `context=local-gate`, `state=success` and the description. The
  owner/repo come from the URL of the remote the caller names (the hook's
  `$1`, `gate-tip`'s remote, default `origin`), parsed in
  `gate_stamp_common`. A 422 whose message says no commit was found is
  reported as "push the branch, then `make gate-status`"; any other 422,
  a 4xx or no `gh` is reported in its own words. The caller decides the
  exit status: `make gate-tip` exits 0 on a green gate whose post could
  not land (the gate passed), `make gate-status` exits 1 (posting was its
  job).
- **`fail`**: the same POST with `state=failure`. `make gate-tip` calls it
  when a lane goes red on a tree for which `HEAD` already carries a
  `local-gate` success (one `GET .../commits/<sha>/status` decides; a red
  marker invalidates the local evidence and the remote evidence must
  follow). Otherwise nothing is posted. The limit: a success posted on
  another sha of the same tree (an amended message after `gate-status`)
  is not found by that GET; the red marker still refuses the hook and
  `gate-status` for that tree, and the docs say so. The failure POST
  follows the same protocol as the success POST (below): under the
  exclusive lock, after a re-check that the red marker is still the
  latest evidence for the tree, so a red that a later green re-run
  superseded is never published after the green.
- **`await`**: run by `gate-admission`: `GET
  repos/<owner>/<repo>/commits/<sha>/status`, up to thirty times ten
  seconds apart, for a `local-gate` context in state `success`; then `GET
  repos/<owner>/<repo>/git/commits/<sha>` for the head's tree; then
  compares tree, base and digest to the values the job passes. A mismatch
  or a timeout prints the remedy (`make gate-tip` on this head, then `make
  gate-status`) and exits 1.

**Publication is serialised with the evidence.** One protocol for every
POST, success or failure: the publisher takes the stamp store's lock
exclusively, validates the plan against the store (the lock-free
validation core above), writes nothing but the red marker it may be
publishing, POSTs under a 30 s timeout, and releases the lock on every
path, error included. A `gate-status` that validated green therefore
cannot post after a forced re-run recorded red (it posts before the red
is written, or re-validates and refuses after), and a red that a later
green superseded is not published. The hold is bounded by the POST's
timeout (`store_lock`'s contract is short steps; every peer's stamp write
and every hook's read waits behind it), and the bed is never held during
a POST. Pins drive the green-then-red interleaving, the
red-then-green-then-delayed-failure interleaving, and a hanging POST with
a waiting peer.

**Who posts.** Posting a status needs write access to the repository.
The author posts when they have it (every contributor today works on the
maintainer's dev VM with the maintainer's `gh` login); for a pull request
from a fork the maintainer fetches the head, runs `make gate-tip` and
`make gate-status` on it, and the status lands on the head sha whoever
posts it. The docs say so beside the attestation sentence.

`make gate-status` runs `gate_tip.py --status`: it fetches `<remote> main`
(the base resolver below), computes the plan over `base..HEAD`, refuses
when any stamped lane is missing or carries a red marker, posts
otherwise, and then handles CI: it looks up the `ci.yml` run for this
pull request's head (`gh run list --repo <owner/repo> --workflow ci.yml
--commit <sha> --event pull_request`, the repository being the one the
status went to), takes the newest run when several share the sha, waits
while that run's `gate-admission` job is in progress, and re-runs the run
(`gh run rerun --repo <owner/repo> <id> --failed`) when the job ended
failed, because a late status starts no run by itself. No run (the author
flow posts before the pull request exists) is success. Fake-`gh` pins
cover no run, several runs, an in-progress job and a failed one. Posting
twice is harmless: GitHub keeps the latest state per context.

The author flow: `make gate-tip` (stamps; its post 422s on a branch's
first gate, which the docs say to expect), `git push -u origin <branch>`
(the hook checks the stamps and runs `check-breaking`), `make
gate-status`, `gh pr create`. No push wrapper (D3).

### CI's admission job

`.github/workflows/ci.yml` gains `gate-admission` (`runs-on:
ubuntu-latest`, `permissions: statuses: read, contents: read`,
`timeout-minutes: 8`, `env: GH_TOKEN: ${{ github.token }}`). It runs on
every event. On `push` its one step is a no-op that succeeds (`if:
github.event_name != 'pull_request'`), so `main`'s CI runs exactly as
today. On `pull_request` it checks out **the head sha** (`ref: ${{
github.event.pull_request.head.sha }}`, `fetch-depth: 0`), not the merge
commit, so the oracle that computes the digest is the head revision's own
(a pull request that changes `gate_scope.py`, this one included, would
otherwise mismatch for no fault of the author); installs uv; runs
`gate_scope.plan` over `merge-base(<base sha>, <head sha>)..<head sha>`
with `uv run --no-project --with tomli python` (the oracle and
`gate_stamp_common` import `tomli`; the project is not synced); and runs
`scripts/local_gate_status.py await` with the head sha, the merge base
and the digest. The job's measured time is recorded in the pull request.
Every validation job gains `needs: [gate-admission]`; `report-failure`'s
`needs` list gains it too and keeps its full condition, `failure() &&
github.event_name == 'push'`, so a failed admission on a pull request
never files a "CI failed on main" issue. Because the job always runs and
never skips, no dependent needs an `if: always()`. The job and the status
have different names on purpose: GitHub treats a check run and a status
that share a name as one required check, and the ruleset requires both.

A workflow-shape test, `tests/unit/test_ci_gate_admission.py` (beside
`test_ci_web_gate.py`), parses the YAML and pins: the job exists and has
no job-level `if`; its polling step is conditioned on `pull_request`; its
checkout ref and depth; its `permissions`; every other job's `needs`
includes it; the script it invokes is `scripts/local_gate_status.py`;
`report-failure`'s condition is unchanged.

### The ruleset and the repository

Applied by `gh api` and recorded in the spec and the docs as the live
configuration. Ruleset 15843311: `required_status_checks` becomes
**`REQUIRED_CONTEXTS`**, one literal list in
`.github/required_contexts.json` that `tests/unit/test_landing_ruleset.py`
reads: the eighteen contexts required
today (`tests_hostless-3.10` … `3.14`, `dashboard-e2e (chromium|firefox|webkit)`,
`check-ts`, `lint-python`, `typecheck-python`, `docs`, `wheel-smoke`,
`api-dump-invariance-3.10` … `3.14`) plus `local-gate` and
`gate-admission`, kept in `.github/required_contexts.json` and read by
the census and by the release workflow's guard; `busybox-artifacts (…)`
and `report-failure` are excluded on purpose, and the test says so. `strict_required_status_checks_policy`
stays `false`; `required_linear_history` and `non_fast_forward` stay;
`bypass_actors` holds one entry, `{"actor_type": "RepositoryRole",
"actor_id": 5, "bypass_mode": "pull_request"}` (5 is the admin repository
role; the repository is user-owned, so no organization role applies, and
the owner holds it) (D10): the maintainer can merge a pull request without
its required checks, and nothing else; `bypass_mode: always` would reopen
direct pushes and the census refuses it. A bypass merge ignores every
rule of the ruleset, `allowed_merge_methods` included, which is why the
repository-level merge settings stay beside the ruleset rule: they are
what still restricts a hotfix merge to rebase. **The ruleset gains the `pull_request` rule** with its
full parameter object, `required_approving_review_count:
0`, `dismiss_stale_reviews_on_push`, `require_code_owner_review`,
`require_last_push_approval` and `required_review_thread_resolution` all
`false`, and `allowed_merge_methods: ["rebase"]`, so the merge-method
restriction lives in the ruleset the census reads as well as in the
repository setting; the rule is what forbids a direct push: required checks, linear history
and an empty bypass list alone still admit a fast-forward push whose sha
carries green checks (the #820 landing was exactly that). With it,
nothing reaches `main` but a merged pull request, the release included,
and the hook's refusal of `main` is a courtesy copy of what GitHub
enforces. The census pins the rule, and a negative case documents that a
CI-green direct push is refused by it. The repository: `allow_squash_merge`
becomes `false` (`allow_merge_commit` is already `false`,
`allow_rebase_merge` already `true`). A squash is what would rewrite the
landing commits' messages and lose the breaking marks the changelog
census reads.

`test_landing_ruleset.py` reads the live ruleset and repository settings
through `gh api` and pins `REQUIRED_CONTEXTS`, the one bypass actor and
its `pull_request` mode, the `pull_request` rule, the merge methods and
the linear-history rules; the workflow test cross-checks that
every context in `REQUIRED_CONTEXTS` other than `local-gate` is a job the
workflow declares, matrix names expanded. It is skipped unless
`OTTO_RULESET_CHECK=1`, and `make release-preflight` sets that variable
and runs it, so it has a venue: every release. It is also run by hand
after every ruleset edit, and this pull request records its first run.

### The hotfix: a pull request that skips the checks

Extreme cases only: `main` is broken for users or a release is wedged,
and the gate or CI cannot be made green in the time the fix can wait. The
mechanism is the same pull request with two bypasses, each already
provided for and each leaving a trace:

1. The author pushes the branch with `git push --no-verify` (the hook's
   documented escape hatch) and opens the pull request with `hotfix:` at
   the start of its title and the `hotfix` label.
2. The maintainer merges it with the button's "merge without waiting for
   requirements" option, which the ruleset's bypass actor permits for a
   pull request and for nothing else. GitHub records every bypass in the
   ruleset's rule insights (available on this repository: the rule-suites
   API lists its evaluations), and the pull request's `hotfix:` title plus
   a `hotfix` label make `gh pr list --label hotfix --state merged` the
   count the docs name; the push-to-`main` CI run then runs as on any
   landing and `report-failure` files the "CI failed on main" issue if it
   is red.
3. Afterwards, the ordinary gate runs on `main`'s tip (`make gate-tip
   BASE=<the hotfix's base>` on a branch at that tip, to learn whether
   the bed lanes pass on the hotfixed tree; it stamps, and the status it
   would post is informational, since the ruleset evaluates pull-request
   heads) and the follow-up that makes the checks green lands through the
   normal flow. The release workflow's guard still requires
   green CI on the tip it tags, so a hotfix that leaves `main` red blocks
   releases until the follow-up, which is the right order.

No code changes for this beyond the bypass actor and the docs: the
contributing guide gets a "Hotfix" section with the three steps and the
sentence that a hotfix is a pull request like any other, only its checks
are skipped, and that the insights page is where the skips are counted.

### The hook

`.githooks/pre-push` keeps its shape; its exec line becomes `uv run
--locked python scripts/gate_tip.py --pre-push --remote <name>` (the
third locked entry, pinned with the other two). The order inside:

1. **`pushed_refs(stdin)`** decides what is gated: every line whose remote
   ref is under `refs/heads/` and whose local sha is not the zero sha;
   tags and deletions are skipped (nothing lands by a tag; `make
   release-tag`'s push is a tag alone). A gated ref that is
   `refs/heads/main` is refused at once: "main takes no direct push; open
   a pull request" (the ruleset refuses it too; the remedy adds that a
   non-canonical remote, a backup mirror, takes `--no-verify`, since the
   ruleset and not the hook is what protects the canonical `main`). No
   gated ref: exit 0
   before anything else, so a tag-only or delete-only push fetches
   nothing.
2. **The base resolver**, one function shared with `make gate-status` and
   `make release-tag`: `git fetch <remote> main` once per hook run and
   read `FETCH_HEAD` into a local, never a tracking ref (a URL or path
   remote has no `refs/remotes/<remote>/main`; the existing bare-path push
   test is the precedent). A remote with no `main` is a refusal with the
   remedy. The base of each gated ref is `merge-base(<that sha>, tip)`.
3. For each gated ref, the plan is computed and **`missing_stamps(repo,
   plan)`** under a shared store lock lists the missing stamped lanes and
   refuses with the remedy, whose text no longer says "to main" or claims
   CI never runs the beds. `_ancestry_refusal`, the "no main to compare
   against" case, `gate_tip.py`'s `--check REMOTE_SHA TIP_SHA` mode and
   their pins are deleted, not moved: no push to `main` is gated any more,
   only refused.
4. **The plan's live line** (`make check-breaking RANGE=<base>..<tip>`)
   runs for the gated ref whose tip is `HEAD`, because the check reads the
   checked-out tree (`require_range_ends_at_head`), and the hook runs the
   plan's line rather than spelling its own. Before it runs, the hook
   refuses the dirt that invalidates a committed-tree check, through the
   stamp store's own reader (`gate_stamp_common.dirt_refusals` /
   `blocking_untracked`: tracked dirt, plus untracked files under `src/`
   or a test root), so the hook and the stamp contract agree on one
   definition; the release's broader `READ_ROOTS` (which covers `docs/`
   and `schemas/` because the release builds docs in place) stays the
   release's, or an untracked spec under `docs/` would block every branch
   push. That shared reader gains one clause for both callers: an
   **ignored** `*.py` under `src/` or a test root is blocking too, read
   with `git ls-files -o -i --exclude-standard -- src/ <test roots>`
   filtered to `.py` (not `git status --ignored`, which collapses an
   ignored directory to one entry and would hide a file inside it; build
   assets are never `.py`, so the ordinary gitignored artifacts stay
   invisible), because the check
   resolves imports from the tree on disk and a restoration the author
   gitignored would otherwise be the one dirt the reader cannot see. The
   reason for all of it: an uncommitted restoration of a deleted public
   name would satisfy the v1 path's "the import still resolves" and hide a
   committed removal; a note is not enough, and regression pins plant
   that restoration untracked, and ignored inside an ignored directory. A gated ref whose tip is not `HEAD` (pushing a peer's
   branch, `git push --all`) gets a note that CI's `lint-python` runs
   `check-breaking-marks` over the pull request's range, and the push
   continues.

The hook runs no lint and reads no working tree beyond that: the static
lanes are stamps now. A push takes seconds on a stamped tip, which is what
the SSH-idle budget note from `gate_fresh.py` asked for; that note moves
with the stdin parsing.

Removed: `scripts/gate_fresh.py`, `make gate-fresh`, the
`otto-gate-fresh-stamps` store, `tests/unit/scripts/test_gate_fresh.py`.
Moved first: the `_bare`, `_commit_file`, `_published`, `_stamp_tip` and
`_repo` helpers that `test_gate_tip.py` imports from `test_gate_fresh.py`
(to `tests/unit/scripts/_gate_repos.py`, beside their users and outside
`tests/_fixtures/`, so a later edit does not escalate the diff), the
stdin-parsing pins (to `test_gate_tip.py`; the ancestry and
fast-forward pins are deleted with the behaviour, and new pins cover the
`main` refusal and the missing-`main` fetch refusal), the tree-state
tests (they test `gate_stamp_common`, where they stay). `GateFreshError`
becomes `GateError` in `gate_stamp_common`; its importers follow:
`gate_scope.py`, `coverage_stamp.py` (which imports and catches it, not
only names it), `test_gate_stamp_common.py`, `test_coverage_stamp.py`,
`test_gate_tip.py`, `test_gate_scope.py`,
`tests/unit/api_snapshot/test_api_gate_wiring.py`.
`scripts/lint_docs_citations.py`'s command inventory,
`scripts/release_preflight.py`'s and `scripts/coverage_stamp.py`'s
docstrings, `tests/unit/test_lane_invariants.py`'s target lists and the
Makefile comments that name `gate-fresh` are updated; historical specs and
the changelog are not.

### The release lands through a pull request

Today `make release` runs on `main`: its stages validate the tree, then
`release-matrix` and `release-kmodcov-matrix` commit the re-measured
matrices, git-cliff rewrites `CHANGELOG.md`, `bump-my-version` commits the
bump and creates the tag (`[tool.bumpversion]`: `commit = true`, `tag =
true`), and the recipe prints `git push --follow-tags`. With `main`
closed, the same stages run on a branch and the tag moves after the merge:

- **Step 1, the branch.** `git checkout -b release/next && git push -u
  origin release/next` from `main`'s tip (the version is not known until
  `release_bump.py` runs, so the branch name does not carry it; the push
  is a tree identical to `main`, which the hook passes with an empty
  range). `release-preflight` refuses `main` and a detached `HEAD` (the
  push and `gh pr create` need a branch), keeps its unpushed-commit
  refusal (a fresh branch with no upstream is refused with "push -u
  first", so its "release from main tracking origin/main" message and
  the module docstring are reworded, and `test_release_preflight.py`
  follows), and gains one check through the shared base resolver: `HEAD`
  **equals** the fetched `main` sha (equality, not ancestry: a branch
  ahead of `main` with already-pushed feature commits is not a release
  branch either, and the pin covers the ahead and the behind case),
  because a branch that is not `main`'s tip rebase-merges to a tree the
  hour-long run never certified and `release-tag` would refuse
  afterwards. The one exception is the recertification run below, which
  starts from `main`'s tip by construction.
- **Step 2, `make release`.** The stages are unchanged through
  `wheel-check`; `bump-my-version` runs with `tag = false` in
  `pyproject.toml`, so the bump is a commit and no tag (`commit` and
  `tag` are independent settings; a pin asserts the bump still rewrites
  `current_version` and commits). **The release record**,
  `reports/release/record.json`, has a lifecycle like the coverage
  stamp's, which invalidates its previous evidence before it re-runs: the
  recipe writes it as `pending` as its first step, after
  `release-preflight`, so a run that fails anywhere leaves a pending
  record and never an older certificate. The run's identity in the record
  is the bed lease's: the top-level `make release` process, `$$PPID` in a
  recipe line (the convention `bed_acquire` already uses), as `pid` and
  `pid_started`, the start time `process_group.process_start` reads from
  `/proc` (field 22 of `/proc/<pid>/stat`, jiffies since boot; Linux
  only, which the release already is). "That very process, still
  running" is decided today inside `bed_lease.holder_alive`, which takes
  the lease's `Record` and lives in a module that imports `otto`; the
  core predicate (`process_start(pid) == started` and the state is not
  `Z`) moves to `process_group.alive(pid, started)`, `holder_alive`
  delegates to it, and the release record and the watcher call that one
  function, so there is no third spelling. `watch_release.sh` replaces
  its `kill -0` liveness loop with that predicate through `python -c`
  on `scripts.process_group`, run as `"$REPO/.venv/bin/python"` with
  `"$REPO"` as the working directory (the watcher resolves `REPO` at its
  start and today changes into it only after the release exits; never
  `uv run`, whose sync could reshape the venv the release is testing),
  capturing the identity when it attaches and checking it on every poll,
  so a reused pid ends the watch as a failure instead of a wait on a
  stranger; pins drive pid reuse, the process vanishing between reads,
  and an invocation from outside the checkout, and the scratch fixture in
  `test_watch_release.py`, which copies the shell script alone, gains the
  Python helper and an interpreter. The recipe
  refuses to write `pending` when `process_start` returns `None` (the
  lease records `0` there and stays usable because its liveness is a
  takeover question; a release with an unknowable identity would fail
  every check at once, so it stops before the first stage). The record
  is rewritten as `certified` (the head sha,
  its tree, the version, the same run identity) atomically after
  `wheel-check`, the last validation stage in both modes (in the
  ordinary release that is after the bump commit), before the push; the
  states `release-tag` accepts are the ones step 4 lists, and that list
  is the one definition. A pin drives: a good
  certificate for an untagged tip, a recertification of that tip that
  fails early, `release-tag` refuses. After the bump the recipe releases
  the outer bed lease (today it holds it through its final echoes; nothing
  after the bump needs the bed, and a POST never holds it) and then
  **lands the branch like any branch**: `make gate-tip` on the head
  (the release's branch push is a branch push; usually its plan is
  `check-breaking` alone, because the matrices' re-dating and the bump
  are `release_owned` and `CHANGELOG.md` is `docs`, but a matrix whose
  verdicts improved is an ordinary `tests-leaf` change and earns the
  static lanes, a minute or two, and the recipe does not special-case
  it; the release's own command-line variables, `BUMP=`, `NEW_VERSION=`,
  `BUDGET_APPROVE=`, reach the nested `gate-tip` through `MAKEFLAGS`, so
  `GATE_TIP_OVERRIDES` filters out `BASE` and `COVERAGE_STAMP_HARMLESS`,
  the list the coverage stamp already keeps for the same reason; a pin
  runs a `BUMP=minor` release through a green `gate-tip`, and the two
  `test_make_release_lanes.py` pins that today expect the opposite, that
  `BUMP=minor` reaches the runner as an override and that the exclusion
  is `BASE` alone, are inverted, while the arbitrary-variable refusal pin
  stays), `git push
  -u origin <branch>`, `make gate-status`, then `gh pr
  create --title "chore(release): v<version>" --body-file "$$body"`
  (`$$body` a temp file holding `git-cliff --tag v<version> --unreleased
  --strip header`; recipes run under `/bin/sh`, so no process
  substitution; the tag does not exist yet, so `--latest` would be
  wrong). On success it rewrites the record once more as `landed` with
  the pull request URL, which is the completion state
  `scripts/watch_release.sh` reads (below), and prints the two steps
  left: merge, then `make release-tag`. The old
  "push with `git push --follow-tags`" echo block goes. CI runs on the
  release head as on any pull request; the coverage stamp the release
  trusted stays local evidence, as today.
- **Step 3, the merge.** The maintainer reviews the diff (the changelog,
  the bump, the re-measured matrices: the review the old flow never had)
  and rebase-merges. Nothing else should land while the release pull
  request is open; the docs say so.
- **Step 4, `make release-tag`**, from the same checkout: it refuses
  when `reports/release/record.json` is missing or not `certified`,
  `landed` or `tip-certified` (a mutable branch tip is not evidence of
  the certified tree, so there is no fallback, and a pending record is a
  failed run); fetches
  `main` (`FETCH_HEAD`, the base resolver); refuses unless `main`'s tip
  tree equals the record's tree (the rebase-merge changed the shas, never
  the tree, as long as nothing else landed between `make release` and the
  merge) and `pyproject.toml` at that tip carries the record's version;
  then **waits for `main`'s CI** on that tip through the shared CI-verdict
  reader (below), because the release workflow's guard checks the same
  verdict and would see it pending for the 25 minutes the run takes; the
  wait has a ceiling of 70 minutes (the `tests` job's own
  `timeout-minutes` is 60), and on expiry it refuses naming the run's
  URL. The
  landing freeze extends through the tag: after the wait it **fetches
  `main` again and refuses unless the fetched sha is the sha the reader
  judged**, because a push workflow certifies a commit, not a tree (a
  landing of a change and its reversal advances `main` to a new sha with
  the same tree and version, whose CI nobody has checked), so a `main`
  that advanced during the wait is a refusal, not a tag on an unchecked
  commit. Pins advance `main` during the wait with a different tree and
  with an identical tree. On a match it creates the annotated tag on
  that sha with the name and
  message `[tool.bumpversion]` still declares (`tag_name`, `tag_message`,
  read from the config with the version substituted, so the format has
  one home) and pushes it; the tag push fires
  `.github/workflows/release.yml` (the hook skips tags; the ruleset
  targets branches; the workflow checks out the tag's commit, and tree
  equality is what makes the local coverage stamp's trust carry to it).
  `github-release` and the changelog's compare links are tag-based, so
  tagging the rebase-merged commit changes nothing they read.
- **Recovery: the recertification run.** If something else landed
  between `make release` and the merge, `main`'s tip tree is not the
  record's and `release-tag` refuses. The bump is already in `main`'s
  history untagged, so an ordinary `make release` cannot repeat it:
  git-cliff's census skips `chore(release)` commits and would name the
  version already configured, and `bump-my-version` returns before
  committing while the recipe has already regenerated `CHANGELOG.md`.
  `make release` therefore detects the state at its start: the remote has
  no tag `v<current_version>` (`git ls-remote --tags <remote>
  refs/tags/v<current_version>` against the base resolver's remote, not
  the local tags, which are stale when `release-tag` ran in another
  checkout), and the census (`release_bump.py`) names a version. When the
  census **equals** `current_version`, it runs in **recertification
  mode**. The maintainer repeats step 1 for it (`git checkout -b
  release/recert && git push -u origin release/recert` at the fetched
  `main` tip; the recipe still refuses `main`) and `make release` detects
  the mode itself: every validation stage as usual, the matrices
  re-measured and committed when they changed, the changelog regenerated
  (`git-cliff --tag v<current_version>`, so the section keeps its
  heading) and committed when it changed, **no bump**, the record
  certified for the resulting head.
  When the census **exceeds** it (a `feat!` landed between the bump and
  the merge), the ordinary release runs and bumps again to the higher
  version, which `bump-my-version` performs because the versions differ.
  When it is below or empty, the recipe refuses and says why. When the
  branch has no commit (the tree is `main`'s), there is nothing to merge:
  the recipe rewrites the record as `tip-certified` (the fourth state,
  the certificate's fields with `main`'s tip as the head), prints "main's
  tip is certified; run make release-tag", and `release-tag`, which
  accepts `certified`, `landed` and `tip-certified`, tags it. When it has
  commits, the branch lands through the pull request as in step 2 and
  `release-tag` follows the merge. Pins drive, each through the
  step-1 branch sequence: merged untagged bump,
  `main` advanced, census version unchanged, recertification commits
  nothing and `release-tag` tags; the same with a matrix change that
  commits; and the census above `current_version`, which bumps again.
- **`scripts/watch_release.sh`** declares success when `git tag
  --points-at HEAD` is non-empty after `make release`; with `tag = false`
  that is never, and "HEAD moved to a bump commit" is not completion
  either (push, status and pull request follow the bump and can fail).
  Its criterion becomes the record's state for **the run it watches**: the
  watcher attaches to a release already running (its tests launch the
  release first), so freshness is identity, not time: the record's run
  identity (`pid`, `pid_started`) must be the watched process's, and
  the state must be `landed` or `tip-certified` (a field of the record,
  which is what the watcher reads; the printed line is for the person);
  a pending or certified record at exit is a failure. Its make-goal stage census gains `gate-tip` and `gate-status`
  and stays scoped to `$(MAKE)` goals; pull request creation is a
  separately detected stage (`gh pr create`, beside the three programs
  the watcher already recognises), with its own behavioural pin.
  `test_watch_release.py` pins successful late attachment, a record from
  another run, pid reuse during the watch, and each post-bump failure as
  not success.
- **One CI-verdict reader.** `scripts/local_gate_status.py ci-verdict
  --repo <owner/repo> --sha <sha>` (stdlib, `gh api` underneath) finds the
  `ci.yml` run for the `push` event on that sha (`gh run list --workflow
  ci.yml --event push --commit <sha>`; the newest attempt when several;
  absent for up to two minutes of polling, because the webhook takes
  seconds to create it, then a refusal), reads that run's jobs (`gh run
  view <id> --json jobs`, which scopes the verdict to the workflow and the
  event, unlike the commit's check-runs endpoint, which mixes every
  suite), and judges them against `.github/required_contexts.json` minus
  `local-gate` and `gate-admission`: `local-gate` is a pull-request
  status, and `gate-admission` runs on a push as a no-op success that
  validates nothing, so neither says anything about a `main` tip. Every
  listed context must be `completed` with `success`; a missing context,
  a pending job or a failure is "not green"; a job outside the list
  (`busybox-artifacts`) does not count either way. `make release-tag`
  and the release workflow's guard both call it, so there is one
  definition of green; pins drive an optional-job failure (green), a
  missing required context, a pending rerun, and a same-named job in
  another workflow (ignored). It needs `actions: read` on the token,
  declared on the guard job.
- **The release workflow checks the landing.** `release.yml`'s `guard`
  job, which today checks only the tag's spelling and has no checkout,
  gains `actions/checkout` with `fetch-depth: 0`, `permissions:
  actions: read, contents: read`, `GH_TOKEN: ${{ github.token }}`, and
  verifies that the tag's peeled commit is on `main` (`git merge-base
  --is-ancestor <commit> origin/main`) and that the CI-verdict reader
  says green for that commit. A tag made by hand off `main`, or on a
  `main` tip whose CI is red or pending, publishes nothing; `make
  release-tag`'s wait is what makes the ordinary release pass this guard
  first time. **The required-context list has one home**,
  `.github/required_contexts.json`: the ruleset census pins the live
  ruleset against it, the reader reads it, and the workflow-shape test
  pins that the guard invokes the reader (a workflow cannot import a test
  module's literal). The workflow-shape test pins the guard's steps.
- **Pins.** `test_make_release_lanes.py` gains: the `main` and detached
  refusals; the equality check (ahead and behind); the `tag = false`
  setting and that no recipe line tags; the lease released before the
  landing steps; that the recipe runs `gate-tip`, the push, `gate-status`
  and `gh pr create` in that order, and writes the record pending at the
  start, certified before the push and landed with the URL after; the
  early-failed recertification leaves no certificate; an
  improving-verdict matrix (the release head earns the static lanes and
  `gate-tip` runs them); `release-tag`'s refusals (no record; pending
  record; tree mismatch; version mismatch; `main` advanced during the CI
  wait, with a different tree and with the same tree) and its success,
  in a scratch repository with a fake `gh`; the
  CI-verdict reader gives `release-tag` and the guard one answer.
  `docs/release_process.md` is rewritten around the four steps and states
  the new wall clock: `make release` (about an hour), the release pull
  request's CI (about 25 minutes) and the merge, then `make release-tag`,
  which waits the 25 minutes of `main`'s own CI run before tagging; today
  the tag followed the run at once. Its "push the tag" step and the
  pre-push paragraph go.

The ruleset makes this the only path: with no push bypass, a push of a
bump commit to `main` that no check ran on is refused by GitHub as well
as by the hook. Nothing else in the repository pushes to `main` (the
scripts, the Makefile and the workflows were checked; only the recipe's
echo and the docs said to).

### The docs

- `docs/contributing.md`: "Gating a branch before it lands" (the flow:
  gate, push the branch, `make gate-status`, open the PR, CI, the
  maintainer's rebase-merge; the admission job; the behind-`main` gap; the
  attestation sentence and who posts; `main` takes no direct push); the
  class table's tip column (`check-breaking`, the static lines for the
  Python classes, then the bed list per class, `BED_REACH` spelled); the
  full-gate definition paragraph under the table and the special-row
  explanation (rewritten for the slim composition); "The landing's gate
  is stamped" (the static lanes are stamped, `check-breaking` is the one
  live goal, the status, the conditional lease); the `(src-leaf-tip)=`
  section (deleted; its three referrers, two in quality-gates.md and one
  under "Where the test suite's time goes", repointed); the `gate-fresh`
  block under `(gate-stability-lanes)=` (deleted); the stamp-and-rebase
  paragraph and the "what a gate costs" comparison (rewritten); "Pull
  requests" and the "PR checklist" (the second "never by GitHub's merge
  button" goes; `make gate-status` enters the checklist); the coverage
  stamp section says a landing never writes one; the by-hand `make
  coverage-hostless` sentence for an escalating commit.
- `.github/pull_request_template.md`: the `make gate-status` line.
- `docs/architecture/quality-gates.md`, "What binds each gate": the
  `coverage-bed` row's CI column ("every `BED_REACH` tip's lane"); the
  `gate-tip` row (the `local-gate` status, `gate-admission`, the hook on
  every branch push); `quick` and `quick-guards` rows ("the commit column
  only"); the breaking-marks row (no longer "pulled in by `lint-arch`");
  new `local-gate`, `collect-check` and `lock-check` rows; the
  `gate-fresh` sentence; "The last two rows are the point" rewritten
  around the status.
- `docs/release_process.md`: rewritten around branch, merge, tag (the
  section above); the `gate-fresh` mention in the preflight paragraph; the
  pre-push paragraph goes; `check-breaking` is not part of the release's
  stages; the coverage advice near its top.
- AGENTS.md: the gating bullet (the tip is `check-breaking`, the static
  lanes for a Python-touching diff, and the bed lanes a diff reaches; the
  landing is the maintainer's rebase-merge of a CI-green head carrying the
  `local-gate` status; `main` takes no direct push); the hand-back bullet
  (`make gate-tip` then `make gate-status`, not `make gate-fresh`);
  `test_agents_md.py` follows.
- The coverage-bed spec (#797) gains a one-line note that this design
  supersedes its full-gate goal and removes the consumer scan.

## Decisions put to the maintainer

- **D1. Retire `gate-fresh`.** Recommended: yes. Its purpose was to
  simulate the pristine runner; every pull request now runs on one before
  the landing. The two checks it ran that CI did not, `collect-check` and
  the locked sync, become a stamped lane and a `lock-check` in both venues.
- **D2. Drop `make quick`, `quick-guards`, `check-ts` and `docs` from the
  tip, and `make coverage` from every column.** Recommended: yes. They are
  hostless; CI runs them. The scoped `quick` stays in the commit column as
  the working check.
- **D3. `make gate-status` as a separate step after the branch push**,
  not a push wrapper.
- **D4. Strict up-to-date off**, the gap documented. Agreed 2026-11-01.
- **D5. Squash merge disabled; rebase-merge only.** Agreed.
- **D6. The conventional-commit lint in CI** is a follow-up issue.
- **D7. No direct push to `main`, by anyone; the release lands through a
  pull request.** Decided by the maintainer 2026-11-02: "all changes
  pushed to PR branches, with a codified set of local gates required
  before the changes can be pushed to a PR branch; then the merge into
  main is just clicking the merge button". No push bypass (the one bypass
  actor is D10's, for a pull request's checks only); the hook refuses
  `main`; `make release` runs on a branch and `make release-tag`
  tags after the merge.
- **D8. `coverage-bed` for a test-only change only when a changed module
  is in the bed half** (`BED_REACH`), with the conservative fallback for a
  module the oracle cannot read. Agreed 2026-11-02: "a fair balance of
  tight scope and thorough testing".
- **D9. Static stamps only for Python-touching diffs.** A `none`, `docs`
  or `web` tip keeps `check-breaking` alone. Agreed 2026-11-02: Python
  has the subsystems and tests to justify it; the web code is smaller and
  cheaper to test, and can follow Python's approach once its scale
  justifies the complexity.
- **D10. A hotfix mechanism for extreme cases only.** Asked for by the
  maintainer 2026-11-02. It is the pull request with its checks skipped:
  `--no-verify` for the branch push, the ruleset's one bypass actor (the
  repository-admin role, `pull_request` mode) for the merge, every skip
  recorded by GitHub, the normal gate run on `main`'s tip afterwards.
  Direct pushes stay closed even for a hotfix: the pull request is what
  keeps the diff reviewable and the skip visible.

## Done when

- `make gate-scope` on an `escalate` diff prints `make check-breaking`,
  `make check-python`, `make collect-check`, `make coverage-bed` and the
  diff's bed lanes at the tip; on a `docs` diff `make check-breaking`
  alone; on a hostless `tests-leaf` diff the three static lines and no bed
  lane; on a diff with an unreadable mark, `make coverage-bed`.
- `make gate-tip` on a hostless tip finishes green while a fake holder
  owns the bed; on a bed tip it takes the lease after the static lanes;
  on a stamped tree it posts the status, and on a red lane with a prior
  success it posts the failure; `make gate-status` posts alone, refuses an
  unstamped or red tree, waits out an in-progress admission and re-runs a
  failed one (all seen in a scratch repository with a fake `gh` on
  `PATH`, including the post-between-final-poll-and-exit interleaving and
  the green-then-red interleaving).
- The hook refuses an unstamped branch tip and any push to `main`; skips
  a tag-only and a delete-only push without fetching; passes a stamped tip
  pushed from another checkout with the CI note; runs `check-breaking`
  for the `HEAD` ref of a two-ref push and checks stamps for both;
  resolves the base through `FETCH_HEAD` on a path remote.
- On a docs-only diff `make gate-status` posts a status over the empty
  key list and `gate-admission` admits it.
- `release-preflight` refuses `main`, a detached `HEAD`, a branch with no
  upstream and a branch that is not `origin/main`'s tip (ahead or
  behind); `make release` tags nothing, releases the bed before the
  landing steps, runs `gate-tip`, pushes the branch, posts the status and
  opens the pull request, writing the record (fake `gh`); the
  recertification run commits nothing on an unchanged tree and writes
  a `tip-certified` record; `make release-tag` refuses a missing or pending
  record, a tree that is not the record's, a version that is not the
  record's and a `main` that advanced during the CI wait (the same tree
  included), and tags and pushes on a match (scratch repository); the
  CI-verdict reader is the
  one definition of green for `release-tag` and the guard; the
  `watch_release.sh` pins accept late attachment and reject another
  run's record; the ruleset pin
  shows the one bypass actor in `pull_request` mode and the
  `pull_request` rule; the release
  workflow's guard refuses a tag off `main` and a tag on a red or pending
  `main` tip (workflow-shape test).
- `gate-admission` fails a pull request without the status, with a
  mismatched tree, base or digest, and after the window; passes with it;
  succeeds at once on a push to `main`, and every validation job runs on
  `main`; the workflow test pins the graph, the checkout ref and
  `report-failure`'s condition.
- The ruleset lists `REQUIRED_CONTEXTS`, carries the `pull_request` rule
  and one bypass actor in `pull_request` mode, and the repository allows
  rebase-merge only (`make release-preflight` runs the pin).
- Every census is green; the docs name the new flow, the gap, the
  attestation and who posts, and `main`'s closure; `gate_fresh.py` and its
  test are gone; the `check-python`, `collect-check` and admission-job costs are
  measured and recorded.
- This pull request lands by the maintainer's rebase-merge, with the
  status on its head and `gate-admission` green, as the first use of the
  flow.
