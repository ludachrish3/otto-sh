# Manual coverage tracking

A manual capture is the one piece of coverage evidence otto cannot
regenerate: a human at a GDB prompt or a serial console, driving a path no
automated suite reaches. That makes it the only tier whose data is committed
to the SUT repo ({doc}`types`) — and it creates the problem the rest of this
page is about. Committed evidence outlives the commit it was taken against,
so every report has to decide, from scratch, whether a session recorded
months ago still describes the code in front of it.

## The committed store

`otto cov get --tier <name>` against a manual-kind tier requires
`--ticket REF` (`otto.cli.cov` refuses before it fetches anything): manual
evidence with no ticket is unreviewable. Retrieval itself is the same code
path every tier uses — fetch `.gcda`, merge, produce one `capture.json` per
board — and the manual kind adds exactly one step at the end:
`capture.store_dir.write_manual_capture` copies each capture into
`.otto/coverage/manual/<captured_at>-<ticket-slug>-<board-slug>.json`, where
it is `git add`-able and shows up in a PR next to the code it proves.

The artifact is `capture.model.Capture` (schema v2, pydantic
`extra="forbid"`, exact-match version check on load): tier, `base_commit`,
`dirty_remap`, `captured_at`, tester, ticket, note, labs, board,
display_name, and per file a `{blob, lines, branches}` record in
**base_commit coordinates**. Two of those fields are what make it proof
rather than a number:

- **`base_commit`** — `build_capture` anchors to `gitio.head_commit` at
  capture time, and every line number in the file records means that
  commit's numbering.
- **the per-file `blob`** — each file's blob SHA at that commit
  (`gitio.blob_sha`). Sources outside the repo root are skipped with a
  warning, and so are sources with no committed blob at all (untracked or
  generated files): otto will not commit evidence about code that isn't in
  the repo.

Tester identity is manual-only. `_resolve_tester` fills the name from
`getpass.getuser()` and the email from `git config user.email`, with CLI
flags winning over both; `_capture_annotations` passes `None` for every
other kind, because an automated run has no human session to attribute.
Ticket and note, by contrast, annotate every kind.

## The validity engine

Nothing about a committed capture is trusted from disk.
`validity.apply_manual_capture` re-anchors every file of every manual
capture on **every report**, through an `AnchorResolver` built once per
capture, and the revocation policy is precisely what git's own diff reports:

```text
git diff -M -w -U0 --relative <base_commit> -- .
```

(`capture.gitio.diff_tree_u0`). Each flag is a policy decision:

- **`-w`** — whitespace and line-ending churn are invisible. A reindent or a
  reformat never revokes evidence anchored to untouched code. It is also
  safe for the remapper, because a whitespace-only modification is one line
  to one line and shifts no counts (`--ignore-blank-lines` would *not* be
  safe: it hides count-changing hunks). The SUTs are C/C++, where the one
  case `-w` also equates — spacing inside a string literal — is not
  coverage-relevant, so it is an accepted, immaterial false-valid.
- **`-M`** — git's rename tracking *is* the rename policy. A file renamed as
  far as `-M` follows keeps its evidence at the new path. Anything `-M` does
  not call a rename is a new file with no evidence attached: a split, or a
  copy (`-C` is deliberately not passed), has to be re-proved by a new
  session.
- **no normalization beyond `-w`** — anything else git reports as a modified
  line revokes that line, a text-mode re-encode (a BOM, a mostly-ASCII
  transcode) included: it revokes exactly the lines whose bytes differ. The
  exception is a conversion that trips git's *binary* heuristic — UTF-16, or
  anything introducing NUL bytes — which git reports as `Binary files ...
  differ` with no hunks at all, so the file reads as unchanged and its
  credits stay silently valid; that hole is undetected today and is called
  out as a limitation in {doc}`../../../guide/cli/cov/index`.

Per line, the outcome is: `LineRemapper` maps the capture's OLD coordinates
to the working tree's NEW ones; a line with a NEW position keeps its hits
(credited under the capture's tier exactly like fresh data), while a line
with no NEW position and a nonzero count is recorded stale at its own
*base_commit* line number — a nearby-enough anchor for a human to find — and
the run's id is appended to `LineRecord.stale_runs`. The stale *state* is
only written when nothing else already covers that line ({doc}`merging`),
but the revoked-run record is unconditional. A later capture that validly
credits a line clears a `stale` state an earlier capture left there, so
re-proving code restores its colour without a store rebuild.

Whole-file degradation is loud: a file that resolves to no new path —
deleted, absent from this tree, or with both its `base_commit` and its blob
gone — has each of its executed lines marked stale under a warning naming
the capture's ticket and the first twelve characters of `base_commit`.
Evidence is never dropped quietly.

Supersede runs before any of this: two captures of the same `(tier, label,
host)` context never accumulate, and the older one leaves the runs table
entirely — see {doc}`merging`.

## Anchor resolution: two paths, one contract

The validity pass's dominant cost is git subprocess fan-out, not computation —
remaps are in-memory over `-U0` hunks, and history length is irrelevant to a
tree-to-tree diff — so `AnchorResolver` (`otto.coverage.anchor`) resolves a
whole capture per subprocess batch instead of per file. That distinction
matters most on NFS-mounted repos, where every git spawn re-touches `.git`
metadata over the wire.

**Two-path resolution.** For each capture, `AnchorResolver` runs one
tree-wide `git diff -M -w -U0 --relative <base_commit> -- .` against the
working tree. When that resolves — the common case — it answers every
file at once: a path absent from the diff is unchanged (unless it is also
absent from the working tree, e.g. a capture carried over from a different
repo, in which case it is unverifiable), a path present is modified or
renamed (`-M` is git's rename tracking, taken as policy), and a path with
no `new_path` was deleted. When `base_commit` itself cannot be resolved —
the tree diff raises `GitUnavailableError` because the commit was
squash-merged away and garbage-collected, or the repo is a shallow clone
that never fetched it — the resolver builds a batched fallback index from
each capture entry's recorded blob SHA instead.

**Batched fallback pipeline** (`otto.coverage.capture.gitio`), in order:
`hash_objects()` hashes every fallback candidate (a capture entry with a
recorded blob and a file still present in the working tree) in one `git
hash-object --stdin-paths` spawn, fast-pathing the files whose hash already
matches the capture's blob. The remaining changed files' base blobs are
existence-checked in one `git cat-file --batch-check` (`blobs_exist()`) —
a blob missing from the object database degrades that file to
unverifiable/stale rather than erroring — then fetched in one `git cat-file
--batch` (`cat_blobs()`). Base and current contents are materialized into
two sibling temp directories and diffed with a single `git diff --no-index
-w -U0` (`diff_no_index_dir_u0()`), parsed by the same multi-file `-U0`
parser (`parse_multifile_u0`) the tree path uses. A capture entry with no
recorded blob at all — or any resolution requested without a capture-wide
file map in the first place — is left to the lazy, unbatched per-file
chain (blob fast-path → blob diff → unverifiable) that the batched index
otherwise short-circuits.

**Parity contract.** Both paths return the same `AnchorResult` (a new
path or `None`, hunks, and a `verifiable` flag) for every row of the
semantics table in `AnchorResolver`'s docstring, and whitespace immunity
comes from `-w` on every diff flavor the resolver runs — tree, per-file
blob, and batched dir-level alike. `validity.apply_manual_capture`
consumes `AnchorResult` as its only resolution currency; it never branches
on which path produced one.

**Spawn budgets are pinned contracts, not aspirations.** ≤2 git spawns per
fold on the tree path and ≤6 on the fallback path are enforced by test:
`tests/unit/cov/test_git_spawn_budget.py` counts spawns through the
process chokepoint (`gitio._run_raw`) on the tree path, and
`tests/integration/cov/test_validity_scale.py` does the same for a
100-file fallback fold. Spawn count stands in for NFS round-trip cost —
deterministic on any machine, where a wall-clock budget would just be
runner noise.

That the fix is batching and not a cache is a deliberate, checkpointed
call: a content-addressed validity cache was designed, then descoped at
the 2026-07-24 checkpoint once profiling showed the batched fallback
resolving a synthetic 2,000-file, 10%-churn repo in 5 flat spawns / 49 ms
— down from 2,602 spawns / 1.53 s for the pre-batching per-file chain.
A cache could not have touched the O(N) hashing spawns that dominated
that cost (every current file still has to be hashed to know what
changed), so its residual value fell below the carrying cost of a
persistent store's pruning/corruption/atomicity surface. See §9 of
`docs/superpowers/specs/2026-07-24-coverage-report-ui-rework-design.md`
for the full ruling and numbers.

**Reverting resurrects credit.** The fallback path is content-addressed, not
history-addressed: `hash_objects` hashes the file as it stands now and
compares against the blob the capture recorded, so a file that was edited
and later reverted hashes back to the recorded blob and resolves verbatim —
full credit, no diff needed. The tree path reaches the same answer by a
different route (a reverted file is simply absent from the base diff), which
is the parity contract doing its job.

**Degradation announces itself.** An unresolvable `base_commit` logs a
warning naming the commit and, when `gitio.is_shallow` confirms it, tells
the reader to `git fetch --unshallow` before falling back to blobs. A base
blob missing from the object database downgrades that one file rather than
raising. The single case re-raised instead of degraded is "not a git
repository" — a wrong repo root is a caller bug, not a repo that has moved
on.

**RepoTimeline** (`tests/_fixtures/_repo_timeline.py`) is the executable
spec for anchor and aging behavior: it scripts a real git repo through
`commit → capture → mutate → fold` and asserts the resulting per-line
disposition, table-driven across renames, squash-merge/GC, shallow
clones, reverts, and aging boundaries.

## Aging

`[coverage.tiers.<name>] max_age = "<days>d"` (settings validates the
`^\d+d$` shape, which is what makes `TierConfig.max_age_days`'s
`int(max_age[:-1])` safe) declares how long a manual session stays
believable. `validity._is_aging` parses `captured_at` and compares
`(now - captured).days > max_age_days`; a blank or unparseable stamp warns
and is treated as **not** aging, so an undated capture is never silently
downgraded.

Aging costs no coverage credit. The capture's hits are inserted exactly as a
fresh one's would be; what changes is that `RunRecord.aging` is set for the
run and every line the capture validly credits takes `state = "aging"` if it
has no state yet. Because the row walk tries tier hits before line states
({doc}`types`), those rows keep their tier colour — aging surfaces through
the run chip's `· aging` suffix, the runs page's status, and the
`flags.aging` count rolled up onto every directory node. It is a prompt to
re-run the session, not a revocation of it.

## Dirty remap

`dirty_remap` records that the SUT tree had uncommitted changes when the
capture was taken. `build_capture` responds by remapping every file's
coordinates the *other* direction from the report-time chain — worktree
(NEW) back to `base_commit` (OLD), via `_remap_file` — and dropping the
lines that have no OLD counterpart, because a line that exists only in an
uncommitted edit cannot be anchored to a commit.

At report time the flag rides through to `RunRecord.dirty_remap` and renders
as `✎ remapped` on the run table and the runs page (`contexts.ts` ORs it
across a context's member runs). The uncertainty it encodes is specific: the
capture is a complete statement about the committed code it names, and says
nothing at all about whatever was uncommitted in the tree when it was taken.
