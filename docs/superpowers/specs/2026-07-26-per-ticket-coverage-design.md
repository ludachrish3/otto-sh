# Per-ticket coverage — design

Successor to `2026-07-24-coverage-report-ui-rework-design.md` (Plans A–D, all merged).
Delivers that spec's §12.1 open item — the top entry in its future-work queue — plus the
standing `todo/TODO.md` item "per-ticket coverage report".

## 1. Goal

Answer, for every ticket in the SUT repo: **how much of the code this ticket wrote is
tested, by which tiers, and exactly which lines are still uncovered.** Program management
uses this to direct testing resources; developers use the missing-lines listing to comb out
the last uncovered lines without hunting through the tree, and pin a ticket context (§6.3)
to narrow the whole report to their own work. The same answer is available without a
browser as a machine-readable export (§7).

### Non-goals (documented, deliberately out)

- **Ticketing-system integration.** otto never talks to Jira/Linear/GitHub. It parses
  ticket ids out of commit messages and optionally renders them as links. No API, no auth,
  no network at report time.
- **Per-ticket *testing* attribution.** `RunRecord.ticket` (the manual-capture `--ticket`)
  already records "this session was run for ticket X". That is a different axis from
  ownership and stays where it is, on the runs page. See §5.
- **Ticket state, assignee, or workflow.** The report knows ids and coverage, nothing else.
- **Retrofitting attribution onto un-instrumented files.** Attribution runs over the files
  in the coverage store, not the whole tree.

## 2. Attribution model (ruling)

**A line belongs to the ticket(s) named by the commit that last touched it.** Ownership is
blame-shaped — "the code this ticket wrote" — which is the only reading that supports a
single ticket identity per line in the file-page gutter, and the reading the roadmap's
program-management framing asks for.

Whitespace-only edits must not re-attribute a line, matching the manual-validity contract:
attribution uses `-w -M`, the same flags and for the same reason as
`gitio.diff_worktree_file_u0` (`gitio.py:177-187`). `--ignore-blank-lines` stays off there
and stays off here.

**Multi-ticket.** A commit naming several ids attributes its lines to **all** of them.
Consequence, which the UI must state rather than hide: **per-ticket line sets overlap and
do not partition the repo.** The overall card is repo-truth; the rows sum to more.

**Traversal is `--first-parent`** (ruling). A line is attributed to the merge that brought
it to the mainline, not the topic commit that wrote it — the merge carries the PR/ticket
reference while topic commits are frequently "wip". On merge-heavy histories this diverges
from `git blame` **by design**; see §8 for how that divergence is pinned rather than
treated as a defect.

## 3. Engine: log walk + backward replay, not blame

### 3.1 Why not `git blame`

Blame was measured and rejected on **filesystem-operation count**, not wall clock — otto
targets NFS-backed checkouts (`otto/filesystem.py` `is_network_fs`), where per-operation
latency dominates and wall clock on a local SSD understates cost by orders of magnitude.
This is the same metric that drove Plan A's `cat-file --batch` decision.

Measured on `otto-sh` (739 commits, real history), `strace`-counted:

| Approach | procs | fs ops | per file | `.git` opens |
| --- | --- | --- | --- | --- |
| `git blame`, 50 files | 51 | 13,456 | 269 | 9,835 |
| **log walk, 50 files** | 1 | **766** | 15 | 504 |
| `git blame`, 189 files | 190 | 50,413 | 267 | 36,693 |
| **log walk, 189 files** | 1 | **1,804** | 9.5 | 690 |

Blame is **flat-linear at ~267 fs ops/file with zero amortization** — it re-opens the pack
indexes once per file, forever, because git ships no batch mode for it. The log walk is
**sublinear**: 3.8× more files cost 2.4× more ops, since its marginal cost is diff output,
not object lookups. At 189 files that is **28× fewer** fs ops from a single process, and
0.19 s vs 1.28 s wall clock as a bonus. Rename detection (`-M`) costs 5 additional fs ops
across the whole walk (1,804 → 1,809) against 26 real renames in otto's history.

Extrapolated to a 2000-file coverage store at a ~1 ms NFS round trip: **~9 minutes for
blame, ~10–15 s for the walk.**

> **Correction (2026-07-26, found during implementation).** The table above measured a walk
> **restricted by pathspec to the covered files**, and that variant is *correctness-broken*:
> git's pathspec history-simplification selects commits before `-M` can pair a rename, so a
> rename renders as a fresh creation and all pre-rename history is invisible — the mover is
> silently credited with another author's lines. Measured on otto-sh it misattributes
> **6,958 of 47,408 lines (14.7%)**; on 25 sampled disputed lines `git blame` agrees with the
> unrestricted walk 24/25 and with the restricted walk 0/25. The shipped engine therefore runs
> an **unrestricted** walk. Re-measured on otto-sh (740 first-parent commits, 189 covered
> files): restricted 1,697 `.git` fs ops (wrong answer), **unrestricted 12,519**, blame ×189
> ~37,233, and a bounded *name-status discovery + expanded pathspec* variant **3,519**.
> The unrestricted walk's cost is fixed at whole-repo churn rather than scaling with the
> covered set, so below ~64 covered files it is worse than blame, and it materializes the
> whole history in RAM (~480 MB for otto-sh). The bounded variant — one `--name-status -M`
> pass to discover historical path names, then the `-p` walk over that expanded set — keeps
> rename correctness with 3.6× fewer fs ops and bounded memory, and is what the engine
> should use on any repo of real size.

Supporting measurements (local SSD wall clock, for the record): git process startup is
~1 ms, so there is no per-process tax worth amortizing; a 1-revision file costs 3–7 ms
regardless of size because blame halts once every line is attributed, so **total repo depth
does not leak in**; and blame cost is sublinear-saturating in per-file revisions — a
pathological 3200-revision/800-line file tops out at 253 ms, with ms/rev *falling* 0.30 →
0.08.

### 3.2 No cache

**Rejected.** A cache would add an invalidation-correctness surface (history rewrites that
leave content identical silently poison content-addressed keys) to save an operation that
already costs one process and one pack open. Revisit only if a real SUT drives the walk
above the lcov merge it sits beside.

### 3.3 The pipeline

```
git log --format=<sha>\0<subject>\0<body> -p -U0 -w -M --first-parent -- <store files>
   │  one process · one pack open · streamed, newest → oldest
   ▼
parse_multifile_u0()             ← exists: capture/treediff.py
   ▼
LineRemapper.new_to_old()        ← exists: capture/remap.py (bidirectional)
   │  a HEAD line landing in a changed region is attributed to that commit;
   │  attributed lines are retired from the frontier, and the walk stops
   │  early once every line is claimed — blame's own termination rule,
   │  amortized across all files in one process
   ▼
line → commit → ticket ids
   ▼
working-tree overlay: diff_worktree_file_u0()   ← exists: capture/gitio.py
   │  uncommitted edits attribute to (uncommitted), never to whoever
   │  last committed the line
   ▼
LineRecord.ticket: list[str]
```

This is blame's algorithm with blame's process model removed. Every parsing and remapping
primitive already exists and is production-tested by the manual-validity pass; the new code
is the frontier bookkeeping and the ticket extraction.

### 3.4 Module layout

- `capture/gitio.py` — add `log_walk_u0(repo_root, relpaths, *, first_parent=True)`,
  streaming commit records. Consistent with the module's existing role as the only place
  that spawns git.
- `coverage/attribution.py` (new) — the backward replay and working-tree overlay;
  returns `dict[relpath, dict[lineno, str]]` (line → commit sha).
- `coverage/tickets.py` (new) — `TicketSpec`: compiled pattern, `extract(message)`,
  `url_for(id)`.
- `coverage/report_config.py` — load `[coverage.tickets]` via the existing raw-dict loader
  pattern established by `CoverageReportSpec`. Config loading stays out of `models/`.

## 4. Configuration

```toml
[coverage.tickets]
pattern = "#(?P<num>[0-9]+)"
url = "https://github.com/org/repo/issues/{num}"
```

- **Display label is the whole match** (`#1204`), so the gutter shows what the commit
  actually wrote.
- **`url` is a `str.format` template over the named groups**, plus `{0}` for the whole
  match. This is what lets GitHub's `#1204` link to `.../issues/1204` while Jira's
  `pattern = "(?P<key>[A-Z]{2,10}-\\d+)"` uses `{key}` untrimmed — the URL can consume a
  *part* of the match.
- `finditer` over subject + body; all matches attributed, deduped per commit.
- **Absent block → feature absent.** No gutter column, no tickets page, no walk, report
  byte-identical to today. Nothing regresses for repos that never opt in.

**Validation, loud at config load**: `pattern` must compile; `url`'s referenced fields must
all exist as named groups in `pattern` (or be `0`). A URL template naming a group the
pattern doesn't define is a config error, not a render-time `KeyError`.

**Two synthetic rows** keep every line represented, consistent with the "all tickets found"
ruling: `(uncommitted)` (working-tree lines, from the §3.3 overlay) and `(no ticket)`
(commit matched nothing).

## 5. Data contract

`store.json` bumps **v4 → v5** (exact-match loud-fail, no migration shim — the established
policy):

- `LineRecord.ticket` becomes **`list[str]`**. The v4 slot was reserved single-valued;
  multi-ticket outgrows it. This settles the deferred Plan B nit *"empty-string ticket
  serializes"* — the guard becomes omit-when-empty-list.
- New top-level `tickets` table: id → `{url, commits, rollups}`.
- `RunRecord.ticket` is untouched (different axis, §1 non-goals).

SPA chunks follow **the chunking discipline already established**, so boot cost stays
constant no matter how many tickets a mature repo yields:

- `cov_data/index.js` — per-ticket rollups precomputed in Python, one small row each,
  exactly as per-directory rollups already are.
- `cov_data/tickets/<mangled-id>.js` (new) — the missing-lines detail for one ticket,
  loaded on row expand. Mirrors the per-file chunk pattern.
- `cov_data/files/<mangled-path>.js` — gains per-line ticket ids.

Every chunk carries the report stamp; the existing stamp-mismatch guard screen applies
unchanged.

## 6. Pages

### 6.1 Tickets page (`#/tickets`)

Outside the `#/coverage/…` namespace so it can never collide with a real directory —
the same reasoning that placed `#/runs`.

```
⬡ otto coverage · myproduct        [◉ ctx: manual-bench2 ✕] [⬡ PROJ-412 ✕]  ☾ ⋮

┌─ All attributed lines ────────────────────────┐
│ Tier      Line          Branch    Decision    │   ← StatsCard, reused verbatim
│ unit      88% 4.2k/4.8k   71%     no data     │
│ system    41% 1.9k/4.8k   33%     no data     │
│ manual     6%  290/4.8k    —      no data     │
│ All tiers 94% 4.5k/4.8k   79%     no data     │
└───────────────────────────────────────────────┘

[ search tickets… ]                    sort: uncovered ↓

TICKET        OWNED  LINE %       UNCOV   unit  system  manual
▸ PROJ-412      284   91% ▓▓▓▓▓▓▓▓▓░  26   88%    41%     0%
▾ PROJ-388       97   63% ▓▓▓▓▓▓░░░░  36   63%     0%     0%
    ├ Missing lines
    │   src/net/arp.c      142–158, 204, 219–221   →
    │   src/net/route.c    88–91                   →
▸ #1204          51   76% ▓▓▓▓▓▓▓▓░░  12   76%    12%     0%
▸ (no ticket)    12k  94% ▓▓▓▓▓▓▓▓▓▓ 740   91%    38%     2%
▸ (uncommitted)  18    0% ░░░░░░░░░░  18    0%     0%     0%
```

- **Overall breakdown above the table**: `chrome/StatsCard.tsx` reused verbatim — it is
  already parameterized (`keyColumnLabel`, generic `rows`), scoped here to all attributed
  lines.
- **Search**: free-text over ticket id, filtering rows.
- **Default sort**: uncovered-line count descending, so the worst-tested work floats up
  regardless of age. All columns sortable.
- **Row expansion**: that ticket's own tier breakdown, plus the **missing-lines listing
  grouped by file as line ranges**, each range linking into the code (spec §12.1's explicit
  requirement).
- **Overlap caption** on the table: rows overlap and do not sum to the card above,
  because a commit can name several tickets (§2).
- Ticket ids render as tracker links when `url` is configured, plain text otherwise.

### 6.2 File page

- The **gutter column reserved by the previous spec** (left of the line numbers, collapsed)
  now populates with the line's ticket chip(s), linked when `url` is set. Multi-ticket
  renders first + `+N`.
- **Line anchors are new work.** `FilePage.tsx` has none today. A range link routes to
  `#/coverage/<file>?lines=142-158`, which scrolls to and highlights the span. `?lines=`
  rides the existing `parseHashQuery`/`setHashQuery` machinery in `focus.tsx`; the
  scroll-and-highlight behavior is net-new.

### 6.3 Ticket context (report-wide)

Run-focus narrows the **numerator** (only that run's hits count). Ticket-context narrows the
**denominator** (only that ticket's lines are in scope). These are **two distinct chips that
compose**, because "PROJ-412's lines, as proven by the manual run" is a real question that
one merged chip cannot express.

Implemented as a new `?ticket=` param alongside `?ctx=`, sharing `focus.tsx`'s
entry-stamping history contract and per-stamp `localStorage` namespacing.

With a ticket pinned, the whole report becomes a narrow view of that ticket's work:

- **The tree hides, rather than dims.** Files with no line attributed to the ticket are
  removed from the directory pages, and directories left empty disappear with them — so
  what remains is solely the ticket's source files. This is a **deliberate divergence from
  run-focus**, which dims non-participating rows to neutral. Rationale: run-focus answers
  "how much of *this code* did that run prove", so the code must stay on screen; ticket
  context answers "where is *my* work", so unrelated files are noise.
- **Percentages recompute over the ticket's lines only.** Every per-file and per-directory
  `hit/total`, bar, and tier column takes the ticket's attributed lines as its denominator,
  so a file where the ticket touched 12 lines reports coverage of those 12 — not of the
  file. The stats card scope line names the ticket.
- **The narrowing is never silent.** A row above the tree reports what was hidden
  ("142 files hidden · 1 ticket pinned"), and the app-bar chip clears it. Silent truncation
  reading as "this is everything" is the failure mode being avoided.
- **The file page dims, it does not hide.** Non-ticket lines stay rendered but
  de-emphasized — you cannot read code with lines removed from the middle of it — while
  the file's header stats still count only the ticket's lines. Hiding a *file* is fine;
  hiding *lines inside* a file is not.

## 7. JSON export (`--tickets-json`)

otto ships **no coverage export formats today** — `otto cov report` takes only `--dir`,
`--project-name`, `--prefix`, `--tier`, and the sole JSON on disk is `store.json`, which is
the *internal* store (exact-match loud-fail, shaped for the SPA renderer, versioned for
otto's convenience rather than a consumer's). `--tickets-json` is therefore otto's **first
public export**, and is specified as a stable contract rather than a dump of internal shapes.

`otto cov report --tickets-json PATH` (omitted → not emitted; no implicit default location,
so nothing is written that was not asked for). Mirrored on `otto test` as
`--cov-tickets-json PATH`, matching the established `--cov-*` parity.

```json
{
  "format": 1,
  "generated": "2026-07-26T21:00:00Z",
  "otto_version": "0.8.0",
  "project": "myproduct",
  "traversal": "first-parent",
  "thresholds": { "high": 80, "medium": 70 },
  "tiers": ["unit", "system", "manual"],
  "totals": { "owned": 17284, "covered": 16240, "uncovered": 1044 },
  "tickets": [
    {
      "id": "PROJ-388",
      "url": "https://jira.example.com/browse/PROJ-388",
      "commits": ["a1b2c3d4e5f6..."],
      "lines": { "owned": 97, "covered": 61, "uncovered": 36 },
      "per_tier": { "unit": 61, "system": 0, "manual": 0 },
      "branches": { "total": 12, "hit": 7 },
      "files": [
        {
          "path": "src/net/arp.c",
          "owned": 64,
          "covered": 41,
          "missing": [[142, 158], [204, 204], [219, 221]]
        }
      ]
    }
  ]
}
```

Contract rules:

- **Its own `format` integer**, independent of `STORE_FORMAT_VERSION`. The store may be
  reshaped freely for the renderer's benefit; this file has consumers otto does not control,
  so it versions on its own schedule and its compatibility policy is documented in the guide.
- **Deterministic output** — tickets sorted by id, files by path, ranges ascending. A
  machine-readable export that reorders between runs is useless in CI diffs.
- **`missing` ranges are inclusive `[start, end]` pairs**, the same grouping the UI renders
  (§6.1). One representation, two consumers — the ranges cannot drift apart.
- **Loud-fails without `[coverage.tickets]`.** The flag asks for ticket data; producing an
  empty file instead of an error would read as "this project has no uncovered ticket work".
- `(uncommitted)` and `(no ticket)` appear as ordinary entries, so the export sums the same
  way the page does.

This is also the natural substrate for the deferred `--cov-fail-under` item (§10 group C) to
grow a per-ticket variant later; that is noted, not scoped here.

## 8. Testing strategy

- **`git blame` becomes the oracle, not the engine.** The `RepoTimeline` harness
  (`tests/_fixtures/_repo_timeline.py`) builds a history; the pin asserts replay output
  matches `git blame -w -M` line-for-line. This converts "replaying hunks reimplements
  blame" from a hand-wave into a standing invariant.
- **Oracle scope is explicit**: exact equality holds on **linear** histories (what
  RepoTimeline builds, and what otto-sh is — 0 merge commits). On merge histories
  `--first-parent` diverges from blame **by design** (§2), so that divergence gets its own
  dedicated pin asserting the *intended* attribution, and the oracle test is scoped to
  linear timelines rather than being allowed to fail.
- **Timeline cases**: whitespace-only edit does not re-attribute; rename followed across
  `-M`; multi-ticket commit attributes to all ids; commit matching no pattern lands in
  `(no ticket)`; uncommitted edit lands in `(uncommitted)` and never inherits the previous
  committer's ticket.
- **Config validation**: bad regex and URL-template-names-unknown-group both fail loud at
  load, each pinned.
- **Scale**: extend `tests/integration/cov/test_validity_scale.py`'s sibling pattern with a
  **fs-operation-count assertion**, not a wall-clock one — the walk must stay single-process
  and sublinear. A regression to per-file process spawning is the exact failure this design
  exists to prevent, and wall clock on CI's SSD would not catch it.
- **Frontend**: tickets page render/sort/search/expand; gutter chips incl. multi-ticket
  overflow; `?lines=` deep link scrolls and highlights; ticket context composes with run
  focus. Browser lane runs the full matrix (`nox -s dashboard`), not bare pytest.
- **Ticket context (§6.3)**: a pinned ticket removes non-participating files *and* the
  directories they empty; per-file percentages recompute against the ticket's lines, pinned
  with a file where the ticket owns a known subset (12-of-400 reports the 12, not the 400);
  the hidden-count row is asserted present, since a silent narrowing is the failure mode;
  and the file page is pinned to **dim rather than hide** its non-ticket lines.
- **JSON export (§7)**: schema round-trip against a fixture report; determinism pinned by
  generating twice and asserting byte equality (ordering regressions are invisible to a
  field-by-field assertion); `missing` ranges asserted **identical to the ranges the UI
  renders**, so the two consumers cannot drift; loud failure without `[coverage.tickets]`;
  and absent flag → no file written.
- **Empty/absent**: no `[coverage.tickets]` → no page, no walk, no gutter, report identical
  to today (pinned).

## 9. Rollout

- Store v5 with the established exact-match loud-fail; regenerate, no shim.
- `make web` builds the new page into the existing covapp bundle — no new build artifact,
  so the `otto._webassets` registry, wheel-check, and asset-less-build guard are unaffected.
- Docs: coverage guide gains a per-ticket section (config, the `--first-parent` ruling, the
  overlap caveat, the NFS/fs-ops rationale) **and a documented `tickets.json` schema with
  its compatibility policy — otto's first public export contract**;
  `architecture/coverage/` gains an attribution subpage; `cli-reference` gains
  `--tickets-json` / `--cov-tickets-json`; screenshots regenerated for the new page and for
  a pinned ticket context, matching the three existing SPA pages.

## 10. Triage — open coverage items (as of 2026-07-26)

Swept from `todo/coverage_roadmap.md`, `todo/coverage-validity-followups.md`,
`todo/gcno_mismatch_error.md`, `todo/TODO.md`, the prior spec's §12, and the workstream
history. **This spec covers group A only**; the rest is recorded here so it stops being
scattered across five files.

### Delivered — retire from the todo files

| Item | Landed |
| --- | --- |
| Dark-mode toggle for coverage reports | SPA app bar (Plan C) |
| `--report` → `--dir` | Plan D `6770c49c` |
| Per-run annotation as a context; per-line expander with tier colors | Plan C contexts expander + focus filter |
| gcc **and** clang support (roadmap's #1 priority) | clang-gcov-support: stamp discovery + llvm-cov |
| `.gcno` stamp + clang-pairing mismatch errors | typed errors; GNU function-count variant still open |

**Correction to the record**: `todo/coverage_roadmap.md` describes git blame annotation as
"currently implemented but can be expensive", with deferred opt-in/batching/caching bullets.
**It was never implemented.** The only `blame` ever present in `src/` was a placeholder
comment on a store field in the initial commit, deleted by `09e5e25d` when the blob-anchored
validity pass replaced the idea; `annotate_blame` appears solely in the roadmap document
itself. Manual coverage ages via **blob anchoring and diff replay** (`AnchorResolver` +
`LineRemapper`), which answers "did this line change since I proved it?" — a content
question — and never needs authorship. That roadmap section should be deleted, not deferred.

### A. This spec

Per-ticket coverage report — `todo/TODO.md`, roadmap §Per-Ticket-Coverage-Breakdown, prior
spec §12.1.

### B. Validity follow-ups (separate branch, standing ruling)

`todo/coverage-validity-followups.md`: binary-transcode hole at three parse sites; §10
timeline-case gaps; 7 code-polish nits. **Contains one decision owed**: the
aging-recovered semantic — a line freshly re-covered can still carry the aging flag — must
be ruled on before it can be pinned.

### C. CLI and usability

`--cov-fail-under` + `coverage.options.fail_under`; Rich console coverage summary;
`--project-name` → `--report-name` defaulted from repo info; coverage collection as a
`test` subpage; `otto cov capture` for manual testers; manual-capture accumulation and
tester identity; PathMapping auto-discovery (interactive confirm, multiple build roots,
cached mappings); symlink `.gcno` instead of copying; GNU function-count mismatch error;
wire `[coverage.exclusions] markers` into lcov `rc` so they affect **percentages**, not
just rendering; embedded counter reset for `cov clean` (needs product-side `cov_reset`).

### D. R&D

Decision-coverage producer for the reserved `decision` slot (prior spec §12.2; branch→clause
P2/P3 deferred as cost>benefit); merge-conflict-resolution validity; report keyboard
bindings; **the tiers-model rethink** (roadmap L11 — whether `.info`-derived tiers remain
the long-term model across on-host, unit, and manual testing).

### E. Residuals from the merged work

`hermetic_monitor_dist` is a latent instance of the shared-monkeypatch teardown-LIFO
pattern; the armed TS-coverage lane is CI-invisible; the asset-less-build guard proves
bundle *presence*, not *freshness*; `scripts/` lands on the build-env `sys.path`; per-run
**branch** contribution is absent from store v4.

## 11. Open items after this

1. Per-ticket **trend** over time (is this ticket's coverage improving?) — needs run
   history the store does not retain.
2. Ticket attribution for **non-git** SUTs (none today; the walk is git-only by
   construction).
3. A general "missing lines" view — the prior spec noted it may fall out of this machinery;
   §6.1's listing is deliberately built as one instance of it.
4. **Additional report formats for CI and repo hosting** (Chris, 2026-07-26). `tickets.json`
   (§7) is otto's first export and deliberately ticket-shaped; the general need is the
   formats CI pipelines and hosting services already consume — Cobertura XML (GitLab,
   Jenkins), Coveralls, Codecov, LCOV passthrough, and a plain whole-report JSON summary
   distinct from the internal `store.json`. Design them as one export layer with a shared
   writer interface rather than accreting one flag at a time; §7's contract rules
   (independent `format` version, deterministic ordering, loud-fail on missing inputs) are
   the precedent to follow.
