# Per-ticket attribution

Per-ticket coverage (`#/tickets`, the file-page gutter, `tickets.json`) all
read from one answer: **which commit last touched this line, and which
ticket ids did that commit's message name?** This page is about how that
answer is produced — one bounded git log walk, replayed backward from
HEAD — and why that engine exists instead of the obvious one, `git blame`.

## The shape of the problem

`git blame`'s algorithm is exactly right for "which commit wrote this
line": start at HEAD, walk history, and for each commit re-diff the file
against its parent — any line inside a changed hunk is claimed by that
commit and removed from further consideration, so the walk can stop early
once every line has an owner. The only thing wrong with `git blame` for
otto's purposes is its **process model**: it spawns one subprocess per
file and re-opens the repository's pack indexes from scratch every time,
with no batch mode. On a coverage store with hundreds of files that is
hundreds of pack opens for an answer the log itself only has to produce
once — and, as {doc}`manual`'s anchor resolution makes the same argument
for a different subsystem, that cost is filesystem operations, not CPU:
otto targets NFS-backed checkouts (`otto.filesystem.is_network_fs`) where
every `.git` touch is a network round trip, so a wall-clock measurement on
local SSD understates the real cost by orders of magnitude.

`otto.coverage.attribution` is blame's algorithm with blame's process
model removed: one log walk, replayed backward in memory across every
covered file at once.

## The bounded, two-pass walk

A pathspec-restricted `git log -- <covered files>` would be the obvious
way to keep a single walk cheap — restrict the payload to only the files
that matter — but it is **correctness-broken**, not merely an
optimization tradeoff: git's pathspec-based history simplification
selects which commits to show *before* `-M` gets a chance to pair a
rename, so a file that was renamed at some point in its history shows up
as a fresh creation at the rename commit, and everything before that point
becomes invisible. The mover silently inherits every line's ticket credit;
the original author's commits are never consulted. Measured on `otto-sh`,
this misattributes 6,958 of 47,408 lines (14.7%); checked against `git
blame` on 25 sampled disputed lines, the unrestricted walk agrees with
blame 24/25 and the restricted one 0/25.

The engine instead runs two passes, both in `attribution.py`:

1. **Discovery** (`_expand_historical_paths`, backed by
   `gitio.name_status_walk_u0`) — an unrestricted, whole-history `git log
   --name-status -M -w`, deliberately dropping `-p` (patch output) so it
   costs a small fraction of a full diff walk for the same commit count.
   It replays the rename graph backward from each covered file's HEAD
   name, tracking path identity only (no line numbers, no hunks): whenever
   a commit's rename records show the current name on the *new* side, the
   old side joins the result and becomes the name to look for one commit
   further back. A file renamed twice pulls in both ancestors,
   transitively; a file that was never renamed contributes nothing beyond
   itself.
2. **The patch walk** (`gitio.log_walk_u0`) — `git log -p -U0 -w -M
   --first-parent`, restricted to the **expanded** path set discovery
   just produced, not to `.` To this expanded set, `-M` always has both
   sides of a rename already present, so it pairs correctly — the walk
   gets rename-correctness without paying for the whole repository's
   churn.

Measured on `otto-sh` (740 first-parent commits, 189 covered files, real
history, `strace`-counted `.git` filesystem operations):

| Approach | `.git` fs ops | Correct on renames? |
| --- | --- | --- |
| `git blame` × 189 files | ~37,233 | yes |
| Pathspec-restricted walk (rejected) | 1,697 | **no** — 14.7% misattributed |
| Unrestricted whole-repo walk | 12,519 | yes |
| **Bounded two-pass walk (shipped)** | **3,519** | yes |

The bounded walk is 3.6× cheaper than the unrestricted whole-repo
alternative it replaced, with bounded memory to match (the unrestricted
walk materializes the entire history in RAM — roughly 480 MB on
`otto-sh`) — and both are an order of magnitude cheaper than `git blame`'s
per-file fan-out. One caveat the numbers carry honestly: the unrestricted
walk's cost is fixed by whole-repo churn rather than by the size of the
covered set, so on a very small covered set (below roughly 64 files on
`otto-sh`'s history) it can cost more than `blame` would have — which is
exactly why the shipped engine is the bounded variant, not the
unrestricted one, on any repository of real size.

`-w` (whitespace-insensitive) and `-M` (rename-tracking) on both passes
mirror the same flags and the same reasoning as the manual-validity
pass's anchor diffing ({doc}`manual`) — a reformat must not re-attribute a
line, and both passes must agree on which commits are renames, or the
patch walk would be missing a path the discovery pass found.

## Backward replay

`attribute_lines` holds a **frontier**: for each covered file, a map from
"this line's number at HEAD" to "this line's number in the tree currently
under consideration" — both equal at the start. Before the historical walk
even runs, one working-tree diff (`gitio.diff_worktree_u0` — one process
for every file, not one per file) resolves the frontier against whatever
is on disk right now: a line with no counterpart at HEAD is claimed by the
synthetic `(uncommitted)` sentinel immediately, and every surviving line's
"current" position is rebased into HEAD's coordinates. This is what keeps
an uncommitted edit from silently inheriting whoever last committed that
line's ticket — the same correctness concern `dirty_remap` addresses for
manual captures ({doc}`manual`), applied here to the walk's starting
point instead of a single capture.

From there the walk proceeds purely over committed history, newest commit
first:

```text
for each commit, newest → oldest:
    for each file still in the frontier:
        diff this commit's hunks against the file
        a frontier line landing inside a changed hunk → claimed by this commit
        a frontier line outside every hunk → remapped to its position one commit further back
        a commit with no old-side path at all (a file creation) → claims every surviving line
    stop early once the frontier is empty
```

`LineRemapper.new_to_old` (`capture/remap.py`) — already production-tested
by the manual-validity pass — does the actual hunk-boundary arithmetic; a
rename mid-walk swaps the frontier's tracked path (via `parse_commit_diff`,
which — unlike the manual-capture parser it's modeled on — keeps file
*creations* rather than dropping them, since a creating commit must claim
every line it added or those lines fall through unattributed). Anything
the walk never explains — an untracked file, or a line the history
genuinely doesn't account for — is not in git at all, and is treated as
`(uncommitted)` rather than silently dropped.

Every parsing and remapping primitive here — `parse_multifile_u0`'s
sibling `parse_commit_diff`, `LineRemapper`, the working-tree overlay
technique — already existed and was already proven by the manual-validity
pass before this feature reused it; the attribution-specific code is the
frontier bookkeeping and the ticket-id extraction below.

## From commits to tickets

`attribute_tickets` shares **one** log walk between both of its jobs —
resolving each line to a commit sha, and resolving each commit to the
ticket ids its subject and body name (`TicketSpec.extract`,
`otto.coverage.tickets`) — so ticket attribution costs no more than
line-only attribution would: both share the same fixed **four**-subprocess
budget per report (the discovery pass, `log_walk_u0`'s metadata and diff
calls, and the worktree overlay's diff — pinned by the test
`test_git_subprocess_count_is_constant_in_file_count`). A commit matching
`[coverage.tickets].pattern` nowhere contributes lines to the synthetic
`(no ticket)` row; a commit naming several ids attributes its lines to
**all** of them (the guide's {ref}`coverage-tickets-overlap`) — this is
why a ticket's owned lines are not a partition of the repository.

## Why `--first-parent`

The walk always passes `--first-parent`: a line is credited to the merge
that brought it to the mainline, never the topic commit that originally
wrote it. This is a deliberate ruling, not an oversight — on a linear
history (no merge commits) first-parent and full history are identical,
so it costs nothing there; on a merge-heavy history, the merge commit is
where the PR/ticket reference actually lives, while the topic commits
behind it are frequently unattributable on their own ("wip", "fixup!",
bare one-word messages). Following full history instead of first-parent
would occasionally find an *earlier*, topic-branch commit touching a line
and credit whatever that commit's message happened to say — which, for
squashed or heavily-amended topic branches, is often nothing at all, or
stale wording later corrected in the merge/PR description. First-parent is
the reading that recovers a ticket id reliably.

## Testing: `git blame` as oracle, never as engine

`tests/integration/cov/test_attribution_oracle.py` builds a real git
repository (`RepoTimeline`-adjacent, table-driven commits) and asserts the
replay engine's output matches `git blame -w -M --porcelain` **line for
line** — turning "this is blame's algorithm without blame's process
model" from a comment into a standing invariant, on the linear histories
where the two are supposed to agree exactly. The `--first-parent`
divergence above is real and deliberate, so it is not folded into that
oracle test (which would then have to special-case it away): it gets its
own dedicated pin, on a merge-history fixture, asserting the *intended*
attribution against first-parent rather than against blame's answer.

`tests/unit/cov/test_git_spawn_budget.py`-style spawn-count assertions (see
`test_git_subprocess_count_is_constant_in_file_count`) pin the process
budget itself — a constant number of git subprocesses regardless of how
many files are covered — the same discipline `manual.md`'s validity-pass
spawn budgets use, and for the same reason: a regression to per-file
process spawning is exactly the failure mode this design exists to
prevent, and it would not show up in a functional test that only checks
the attributed answer is correct.

## Where the code lives

- `otto.coverage.capture.gitio` — `log_walk_u0` (the bounded patch walk),
  `name_status_walk_u0` (the discovery pass), `diff_worktree_u0` (the
  working-tree overlay) — new entry points on the module that already owns
  every git spawn in this subsystem ({doc}`manual`).
- `otto.coverage.attribution` — the frontier replay, the working-tree
  overlay, and `attribute_tickets`, the entry point `CoverageReporter`
  calls.
- `otto.coverage.tickets` — `TicketSpec`: compiles `[coverage.tickets]`'s
  `pattern`, extracts ids from a commit message, and renders a `url` from
  its named groups; `load_ticket_spec` re-reads the raw `[coverage]` dict
  at report time, mirroring the established pattern for
  `[coverage.report]`/`[coverage.tiers]` (config loading stays out of
  `models/`). `otto.coverage.report_config` re-exports both for the
  reporter's convenience, alongside its own render-threshold loader.
- `otto.coverage.ticket_export` — builds and writes `tickets.json`; see
  the guide's {ref}`coverage-tickets-json` for the export's own
  compatibility contract.
