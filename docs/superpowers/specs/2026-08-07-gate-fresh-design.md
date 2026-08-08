# `make gate-fresh` — gate the committed tree in a pristine worktree

**Status:** approved 2026-08-07, not yet implemented.

## Problem

Local gates run in a working tree that has accumulated gitignored build
outputs and caches. CI starts from a clean checkout. The dev tree is therefore
a strict **superset** of CI's environment, and a superset certifies nothing
about a subset: any test whose outcome depends on an artifact CI lacks passes
locally and fails on push.

`src/otto/_webassets/*/` is the repeat offender. It is gitignored and built
only by `make web` — never by `pytest` — so a tree that built once looks
correct forever. Instances to date:

- **Issue #196 (2026-08-07)** — a `--collect-only` over `tests/e2e` tripped the
  browser suites' missing-build gate. Green on every dev machine; red on all
  five `tests_hostless` lanes and `unit-repeat`, which never run `make web`.
- **Issue #131** — browser assertions verified green three times against a
  bundle that was never rebuilt; CI was the first place they actually ran.
- A bare `uv build` shipping frontend-less wheels.
- `make docs` timing out on a stale web dist.

The mechanism is always the same, so the fix has to be environmental rather
than another in-tree assertion: **no check that runs inside the dirty tree can
avoid inheriting the dirty tree.**

A fresh git worktree is free of every gitignored artifact by construction —
there is no allowlist to write and no allowlist to keep in sync — and it
additionally catches two classes nothing else does: an unsynced `uv.lock`, and
a test that only passes because of a file that was never `git add`ed.

Measured on the #196 commit: `make coverage-hostless` in a pristine worktree
reproduced the CI failure exactly (`1 failed, 5157 passed`) in ~90 seconds,
while the same command in the dev tree was green.

## Scope

**In:** the CI jobs that run with **no** `make web`, which is exactly the set
that failed in #196 — `lint-python`, `typecheck-python`, `coverage-hostless`.
None of the three depend on `$(WEB_NODE_MODULES)`, so the fresh tree needs no
`npm ci`.

**Out, deliberately:**

- **No rebuild prerequisite on `make coverage` or any other gate.** Bundle
  staleness is already handled twice — the grouped file target
  `$(DASHBOARD_DIST) $(COVAPP_DIST) &: $(WEB_SRCS)` (Makefile) rebuilds
  incrementally whenever a web source is newer, and `_stale_dist_reason()`
  (`tests/e2e/monitor/dashboard/conftest.py`) hard-exits rc=1 for any path that
  bypasses make. Forcing assets *present* in the hostless lane would push it
  **further** from its CI twin and re-hide the #196 class. The requirement for
  that lane is that assets are ABSENT.
- **No TS, docs, or browser lanes.** They need `npm ci` + `make web` +
  `make browsers` in the fresh tree, pushing the gate to 10–15 minutes on a
  3GB dev VM. Their CI twins run with assets *present*, so the dev tree
  already matches them.
- No changes to CI itself. This gate mirrors CI; it does not replace it.

**Guiding principle, worth stating because it is the part that generalises:**
each gate should reproduce **its own CI twin's environment** — matched, not
maximal, and not whatever happens to be lying around.

## Design

### 1. `scripts/gate_fresh.py`

Owns preconditions and worktree lifecycle. Python rather than shell because
the branching is worth testing, and `tests/unit/scripts/` is the established
home for tested scripts (`test_lab_health.py`, `test_stability_campaign.py`).

Interface: `python scripts/gate_fresh.py [--ref REF]`, default ref `HEAD`.
Exit 0 = pass, 1 = precondition refusal or gate failure.

### 2. Preconditions, evaluated in the invoking repo

- **Tracked files modified or staged → refuse, exit 1, naming them.** The
  worktree is built from a commit and cannot see uncommitted work, so
  proceeding would silently gate different content than the developer is
  looking at. That silent mismatch is the exact failure mode this gate exists
  to remove, so it must not be reintroduced by the gate itself.
- **Untracked, non-ignored files → report and continue.** These are not in the
  gated tree by design. If one was actually needed, the gate goes red and
  names the failure — that is the "forgot to `git add`" class being caught, not
  an error to block on. Refusing here would mean never seeing it fail that way.
- Not a git repo → refuse with a clear message.

### 3. Flow

1. Resolve `--ref` to a sha; refuse if it does not resolve.
2. `git worktree add --detach <tmpdir> <sha>`, where `<tmpdir>` comes from
   `mktemp -d` — deliberately **not** `.claude/worktrees/`, which is
   agent-owned, already 20+ entries deep, and whose retirement sweeps are
   already a recurring chore.
3. `uv sync` in the worktree. Always, never reuse a venv: reuse would carry a
   warm dependency set and let a stale `uv.lock` hide.
4. `make lint-python typecheck-python coverage-hostless` in the worktree.
5. Teardown (see below).

### 4. Teardown: remove on success, keep on failure

On success, `git worktree remove --force <tmpdir>`. On failure, **leave it in
place and print the path** — a red gate should hand back a live tree to debug
in, with the failing artifacts (`reports/junit/`, coverage HTML) still on disk.
A stale-worktree note is printed so it is obvious it needs cleaning up.

Cleanup is wrapped in `try/finally` so an interrupt or an exception in the
harness does not leak a registered worktree into `git worktree list`.

### 5. `make gate-fresh`

Thin wrapper with a `## (Quality)` help line, forwarding an optional `REF`:

```make
gate-fresh: ## (Quality) Run the assets-absent CI Python lanes (lint + typecheck + coverage-hostless) in a throwaway pristine worktree at REF (default HEAD) — catches gitignored-artifact, unsynced-lock and forgotten-`git add` failures that the dev tree hides
	@uv run python scripts/gate_fresh.py $(if $(REF),--ref $(REF),)
```

### 6. `.githooks/pre-push`

`.githooks/` already exists (`prepare-commit-msg`) and `core.hooksPath` is
wired by `make dev`, so this is a new file in an established mechanism —
`make dev` needs no change.

The hook reads its stdin lines (`<local ref> <local sha> <remote ref>
<remote sha>`) and runs the gate **only when the push updates
`refs/heads/main`**, passing the local sha actually being pushed rather than
`HEAD` — those differ whenever main is not the checked-out branch. Feature
branches that sit for review do not pay the ~3 minutes.

Deletions (all-zero local sha) are skipped. `git push --no-verify` is the
escape hatch and, unlike `prepare-commit-msg`, pre-push honours it.

## Agent adoption

A gate nobody runs is a gate that does not exist. Agents do most of the work
in worktrees, so they have to reach for this without being told each time.

### Why `AGENTS.md` is the primary surface

**A worktree checkout contains exactly the tracked files.** `.claude` is
gitignored in full (`.gitignore:44`) and nothing beneath it is tracked, so any
hook- or settings-based reminder lives only in the main checkout and is absent
precisely where it is needed — inside the worktree the agent is working in.

`AGENTS.md` is tracked, so it is present in every worktree from the moment
`git worktree add` creates it, and every agent session loads it. It already
carries a worktree-specific bullet, so this joins an established pattern
rather than inventing a surface.

### The bullet

Added next to the existing worktree bullet in `AGENTS.md`:

> - When work in a worktree is ready to hand back — and always before any
>   squash onto `main` — run `make gate-fresh`. Both the main checkout and
>   your worktree accumulate gitignored build artifacts (notably
>   `src/otto/_webassets/*/`) that CI does not have, so a green local run can
>   certify an environment CI will never reproduce. `gate-fresh` re-runs CI's
>   assets-absent Python lanes against your **committed** tree in a throwaway
>   pristine worktree. It refuses if tracked files are modified or staged —
>   commit first, then gate.

### Timing: at hand-back, not at creation

Running it the moment a worktree is created would gate only the base commit,
which CI has already certified — pure cost, no signal. What happens at
creation is that the *instruction is already present*, because `AGENTS.md` is
in the checkout; the *run* belongs at hand-back, when there is finally
something to gate.

### The hook is the enforcement; the docs are the habit

`AGENTS.md` makes it routine, but it cannot make it certain — an agent can
skip a bullet. The `pre-push` hook is what makes it binding: work that skipped
the gate is still caught when the push to `main` happens, which is the only
path to shared history and is always Chris's. Docs and hook are layered
deliberately, not redundantly.

### Considered and not recommended: a worktree-creation hook

A `WorktreeCreate` / `SessionStart` hook in `.claude/settings.json` could echo
the reminder at creation time. Rejected as a load-bearing mechanism for the
reason above — it is untracked, unreviewable, unversioned, and does not exist
inside worktrees. It may be added as a convenience in the main checkout, but
nothing in this design may depend on it.

## Testing

**Acceptance test — the guard must be proven red.** Revert the #196 fix
(`bf268af0`) on a scratch commit and confirm `make gate-fresh REF=<that
commit>` fails, and passes on `bf268af0` itself. A gate that has never been
observed failing is the recurring defect this repo already tracks; this is the
observation.

**`tests/unit/scripts/test_gate_fresh.py`** — drives the module's functions
directly against `tmp_path` git repos (never the dev repo):

- refuses on a modified tracked file, and the message names the file
- refuses on a staged tracked file
- an untracked non-ignored file is reported but does **not** block
- a gitignored file is neither reported nor blocks
- `--ref` resolution: a valid sha/branch is used; an unresolvable ref refuses
- worktree is removed on success
- worktree is kept on failure and its path is printed
- `try/finally` cleanup: an exception mid-run leaves no registered worktree

**Hook test:** the ref-selection logic (main vs feature branch, deletion
sentinel) is a pure function over stdin lines, tested directly rather than by
invoking git push.

**Adoption pin:** a cheap assertion that `AGENTS.md` still names
`make gate-fresh`. The instruction is the mechanism here, so it should not be
able to vanish in an unrelated edit without something going red.

## Docs

- `AGENTS.md` — the worktree hand-back bullet above. This is the surface that
  reaches agents inside worktrees; see Agent adoption.
- Makefile help line (above) — appears in `make help`.
- A short subsection in the contributing/testing docs stating the principle
  ("each gate reproduces its own CI twin's environment") and when to run
  `gate-fresh` by hand versus relying on the pre-push hook.

## Decisions log

- **Scope = Python assets-absent set** (Chris, 2026-08-07). Full CI equivalence
  and a packaging/wheel check were both considered and dropped: they require
  npm in the fresh tree and take the gate to 10–15 minutes.
- **Dirty tree = refuse on tracked edits, report untracked** (Chris,
  2026-08-07). "Refuse on any dirt" was rejected because it blocks precisely
  the forgotten-`git add` case the gate should expose; "never refuse" was
  rejected because a green gate would then not cover the edits on screen.
- **Trigger = manual target + pre-push hook on main** (Chris, 2026-08-07).
  Making it a prerequisite of `make coverage` was rejected: it slows the inner
  loop by ~3 minutes per run, and a slow inner loop gets bypassed.
- **Agent adoption via `AGENTS.md`, not a worktree-creation hook** (Chris asked
  for adoption, 2026-08-07; mechanism chosen here). A worktree checkout
  contains exactly the tracked files, and `.claude` is gitignored in full, so
  a settings/hook reminder is absent inside the worktree where it is needed.
  The tracked instruction file is the only surface guaranteed to be there.
- **Rejected: making every gate rebuild the frontend first.** This was the
  original instinct and it is the wrong direction — see Scope. Staleness is
  already solved; absence is the gap, and the two requirements are opposites.
