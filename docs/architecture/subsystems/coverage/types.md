# Coverage types

Three vocabularies decide what a coverage report is able to say: the
**tier** a line's evidence came from, the **stat type** that evidence
measures, and the **state** a line resolves to when it is drawn. None of
the three is a code branch — tiers are free-form strings, stat types are a
declared list, and states are computed from data the store already
carries — which is why adding a tier or a threshold is a settings edit
rather than a patch.

## Tiers and what is committed

Coverage is organized into **tiers** — `system` (e2e), `unit`, `manual`, or
any other name — each with a `kind` (`e2e` / `unit` / `manual`) that selects
how otto collects that tier's data; declaring tiers and driving the
three-tier workflow is covered in {doc}`../../../guide/coverage`.

Only the **manual** tier's data is committed into the repo, even though
every tier's data is anchored to `base_commit`: e2e and unit data are
reproducible — a fresh `otto test --cov` or a rebuilt unit harvest
regenerates them — but a manual session (a human at a GDB prompt, poking at
running hardware) produces evidence nothing else can regenerate. Selecting a
manual-kind tier on `otto cov get` copies the capture into the repo's
committed store at `.otto/coverage/manual/` — proof of that session that
travels with the code and is PR-reviewable. E2e data instead lives in each
test run's output directory, and unit data is harvested fresh from the build
tree's `harvest_dirs` at report time, so neither needs a permanent home.

`otto cov report` assembles a store from all three sources per tier `kind`:
e2e captures from the given output directories (behind the base_commit
guard — {doc}`merging`), the unit harvest, and every committed manual capture — loaded
automatically, no path needed. Because manual evidence outlives the commit
it was captured against, a report-time **validity pass**
(`otto.coverage.validity`) re-anchors each manual capture's lines against the
current tree by git blob SHA rather than trusting the stored line numbers
forever: unchanged lines stay **valid**, changed/deleted lines go **stale**
(coverage revoked — the evidence no longer describes this code), and
valid-but-old lines past the tier's `max_age` are flagged **aging** without
losing coverage credit. {doc}`manual` covers that pass; the order the three
sources fold in is {doc}`merging`'s subject. See
{doc}`../../../guide/coverage` for the full valid/stale/aging/unverifiable
state table and how each renders in a report.

## Tier mechanics: precedence, kind, color, label

`otto.coverage.tiers.load_tiers` turns the raw `[coverage.tiers]` dict into
`TierConfig` values sorted by `precedence`, first entry highest. An
empty or missing table yields exactly one tier — `system`, `kind="e2e"`,
`precedence=1` — which is byte-for-byte pre-tier behavior: a repo that never
declares tiers never meets the machinery.

`kind` is the closed set (`e2e` / `unit` / `manual`, validated at
settings-parse time by `CoverageTierSpec`), and it only selects *collection*:
which command produces the tier's data and where that data lives. The tier
**name** is the identity everything downstream keys on — `LineHits.counts`,
`BranchHits.reachable`, `CoverageStore.tier_order`, `RunRecord.tier` — so
two tiers may share a kind, and a fourth or fifth tier costs the model
nothing. `CoverageReporter.run` seeds `tier_order` from the declared tiers
*before* any data folds, then appends legacy `--tier` specs not already
present: a tier with no data this run still gets its column and its
precedence slot rather than disappearing from the report.

Colors resolve twice, on purpose. At load, a tier declaring no `color` takes
`DEFAULT_TIER_COLORS[kind]` — e2e green, unit yellow, manual orange
(`otto.coverage.colors`) — and `CoverageReporter._fill_tier_colors` copies
each declared tier's name→color into `store.tier_colors`. At render,
`spa_data._resolve_tier_colors` falls back for any tier the store has no
color for, and that fallback is keyed by tier *name* against the same
kind-keyed dict, defaulting to green. Only tiers that reach the store
without a `TierConfig` — the `--tier NAME=PATH` escape hatch, or a
`store.json` reloaded by external tooling — ever take that path; the
conventional `system` tier is not a key in the dict and so lands on green,
which is what its `e2e` kind would have given it anyway. Labels degrade the
same way: `spa_data.TIER_LABELS` pretty-prints the three conventional names,
and anything else renders `name.replace("_", " ").title()`, so a tier named
`smoke_test` displays as "Smoke Test" with no configuration at all.

## Stat types: line, branch, decision

`STAT_TYPES = ("line", "branch", "decision")` is a module constant in
`otto.coverage.store.model`, written verbatim into `store.json`'s
`stat_types` and into the SPA's `IndexPayload.stat_types`. It is
**declarative and save-only**: the list states what the format's vocabulary
is, not what this particular store contains. Consumers must read it that
way.

**Line** coverage is lcov's `DA:` — an execution count per source line, kept
per tier in `LineHits.counts` and summed across everything that folds into
that tier. **Branch** coverage is lcov's `BRDA:` — a `(block, branch,
taken)` triple per line, where `taken == "-"` means the branch was never
reached at all. `BranchHits` keeps that distinction as tri-state
reachability *per tier* (`True` = observed reachable, `False` = observed
only as unreachable, key absent = no data yet), and reachability is
monotone: `set_reachable` flips `False` → `True` and never back, because one
run reaching a branch is proof for every run. The default denominator is
conservative — `branch_coverage_pct(conservative=True)` counts only branches
reachable in the tier being measured, matching genhtml — so unreachable
branches don't quietly depress a number.

**Decision** is a declared slot with no producer. Nothing in
`otto.coverage` writes decision data, and the frontend renders it as
absence rather than zero: `format.ts` sets `decision: null` on every stats
row it builds, and `StatsCard`'s nullable cell draws "—" for a `null`. That
is the contract a `store.json` consumer inherits — a declared stat type is a
vocabulary entry, never a promise that values exist.

## Line states and precedence

A line's rendered state is decided at draw time from data that is stored in
three different places, and the split matters:

| State | Computed | Stored as |
| --- | --- | --- |
| covered by a tier | at render, per tier | `LineHits.counts[tier]` (counts, not a state) |
| `stale` | at report, by the manual validity pass | `LineRecord.state` |
| `aging` | at report, by the manual validity pass | `LineRecord.state` |
| excluded | at render, by a source scan | `FileRecord.excluded_lines` (a set) |
| uncovered | at render | nothing — a `LineRecord` with no tier hit |
| uncoverable | at render | nothing — no `LineRecord` at all |

`LineRecord.state` is single-valued, which is exactly why exclusion is not
one of its values: "excluded always wins" cannot coexist with
covered/stale/aging in one slot. So the reporter never bakes
`state == "excluded"` into the store; `otto.coverage.exclusions.scan_excluded_lines`
re-scans each file's source for the `LCOV_EXCL_*` markers (plus any extra
markers from `[coverage.exclusions]`) as the renderer builds that file's
chunk, and annotates `FileRecord.excluded_lines` — see {doc}`renderer` for
why that side effect dictates the save order.

The precedence walk itself lives in the frontend (`rowClassFor`,
`web/src/covapp/pages/FilePage.tsx`) and reads: **excluded** beats the
**highest-precedence tier with a hit** — the first entry of `tier_order`
with a nonzero count wins, winner-take-all, no blending — beats **aging**
beats **stale** beats **uncovered**; a source line with no `LineRecord` at
all is **uncoverable** and renders muted, never red. Uncoverable is the
quiet one: a line the compiler never instrumented (a declaration, a comment,
a `#define`) has no evidence to report either way, and colouring it red
would be a lie about the test suite.

Note what the walk implies for `aging`: an aging capture's lines still carry
their tier hits — aging costs no credit — so those rows keep their tier
colour, and aging surfaces through the run chip's `· aging` suffix, the runs
page status, and the `flags.aging` rollup on every directory node.

## Thresholds

Percentage colouring is data, end to end. `Thresholds` (`high=80.0`,
`medium=70.0` by default) lives in `otto.coverage.store.model`;
`otto.coverage.report_config.load_report_thresholds` re-reads
`[coverage.report]` from the raw settings dict at report time — mirroring
`otto.coverage.tiers`, since the pydantic spec (`CoverageReportSpec`) exists
to validate the block at parse time (both values 0–100, `medium` never above
`high`), not to be the runtime carrier. From there the value travels:
`CoverageReporter.thresholds` → `store.thresholds` → `store.json`'s
top-level `thresholds` → `IndexPayload.thresholds` →
`pctClass(pct, thresholds)`.

The frontend takes thresholds as an *argument* at every call site, never as
a module constant, so a report always colours against the cutoffs it was
generated with — including a report opened years later from a bundle built
against different settings. Persisting the cutoffs in the store is what
makes that possible.

## The store (v6)

`store.json` is the canonical, versioned artifact `otto cov report`
writes for downstream consumers — external tooling, a foreign report
viewer — to read back; the in-process renderer consumes the same store
directly, in memory, before it is ever serialized. `CoverageStore.save`/
`.load` (`otto.coverage.store.model`) stamp every file with a top-level
`"format"` key equal to `STORE_FORMAT_VERSION` (`6`). The loader is
**exact-match**: a file whose `"format"` is missing, the wrong type, or
any version other than the one the running otto expects fails loud with
a `ValueError` naming both versions and telling the caller to
regenerate, rather than attempting to read renamed or reshaped keys
under old assumptions. There is no migration shim, by design —
`store.json` is a cheap-to-regenerate report artifact, not a long-lived
source of truth, so "delete and regenerate" beats accreted,
rarely-exercised migration code.

This is otto's own internal, renderer-shaped schema — free to reshape on
every `STORE_FORMAT_VERSION` bump. The one export built for consumers otto
does not control, `tickets.json` (`--tickets-json` / `--cov-tickets-json`),
deliberately does **not** share this version counter; see
{ref}`coverage-tickets-json`.

### Top-level keys

`CoverageStore.save` writes exactly ten:

- **`format`** — `STORE_FORMAT_VERSION`, the exact-match key above.
- **`tier_order`** — the tier precedence list, first entry highest. Drives
  column order and the winner-take-all row colouring.
- **`tier_colors`** — tier name → colour string, seeded from each
  `TierConfig.color`.
- **`thresholds`** — `{"high", "medium"}`, sourced from `[coverage.report]`
  (see {doc}`../../../guide/coverage`); the render cutoffs above.
- **`stat_types`** — the type-extensible stats vocabulary `("line",
  "branch", "decision")`. `decision` is a declared slot with no producer,
  so a consumer should render "no decision data" rather than assume every
  declared type carries values.
- **`runs`** — the run table, one `RunRecord` per capture or synthetic
  per-tier load; a record's `id` is its index here.
- **`tickets`** — ticket id → `TicketRecord`: `id`, `url` (`None` when
  `[coverage.tickets]` configures no template, or the id doesn't match
  one), and `commits`, the shas that named it.
- **`files`** — one `FileRecord` each: `path`, `lines`, and
  `excluded_lines` (the render-time marker scan, {doc}`renderer`).
- **`overrides`** — the asserted-entry table, one `OverrideRecord` per
  entry loaded from the override file ({doc}`../../../guide/coverage`):
  `id` (what per-line `asserted` refs point at, so a `reason` is stored
  once rather than repeated on every line it marks), `tier`, `key` (the
  entry's display identity, `ticket:PROJ-412` or `commit:<full sha>`),
  `reason`, and `as_of` (`None` for a commit entry; required on a ticket
  entry, which would otherwise bless future commits under an old ticket).
- **`overrides_file_active`** — true whenever an override file is
  configured and loaded, **independent of whether it carries any asserted
  entries**. Deliberately not `bool(overrides)`: a reattribute-only file,
  or one whose every entry is currently inert, is still active — the file
  is present and validated. `tickets.json`'s `overrides_active` reads this
  flag for exactly that reason.

### Per-run fields (`RunRecord`)

- **`id`**, **`tier`** — index into `runs`, and the tier this run credits.
- **`label`** — what a drilldown chip shows: the host display name when
  the capture carries one, else the board id, else the tier name.
- **`board`**, **`host`** — the capture's board id. `host` is the explicit
  host identity, `""` for a synthetic or legacy-merged run with no single
  host behind it; together with `tier` and `label` it forms the
  `(tier, label, host)` context identity supersede keys on ({doc}`merging`).
- **`labs`**, **`captured_at`**, **`tester`**, **`ticket`**, **`note`**,
  **`base_commit`**, **`dirty_remap`**, **`aging`** — capture provenance,
  carried straight through from `capture.json`.

### Per-line fields (`LineRecord`, under each file's `lines`)

- **`hits`** — tier name → execution count; **`branches`** — one entry per
  `(block, branch)` on the line, each with its own per-tier counts and
  tri-state reachability (above).
- **`state`** — `"stale"`, `"aging"`, or absent, set by the manual-capture
  validity pass ({doc}`manual`).
- **`run`** — run id → hit count, written only for runs that actually
  executed the line; **`stale_run`** — ids of runs whose evidence for this
  line was revoked.
- **`ticket`** — the ticket id(s) named by the commit that last touched the
  line. A **list**, because a commit may name several ids and every one of
  them owns the line ({doc}`attribution`'s "overlap" ruling).
- **`asserted`** — tier name → the `overrides` ids that asserted this line
  in that tier. Present **only while the tier's sole hits are
  override-sourced**: a real run covering the line clears the mark, so the
  key answers "is this tier's evidence here manual?" rather than "was an
  override ever written for it".

`run`, `stale_run`, `ticket`, and `asserted` are omitted from the
serialized line entirely when empty, rather than written as `null`, `[]`,
or `{}`.

`ticket` and the `tickets` table are populated by exactly one thing:
`[coverage.tickets]` being configured at all ({doc}`attribution`). A report
without it still writes the full shape — a present-but-empty `tickets`
table and every `LineRecord` omitting `ticket` — and the coverage numbers
are identical either way, since attribution annotates lines and never
changes what counts as covered.

### Per-host breakdowns are derived, not stored

The schema carries no per-line host data at all. One capture is exactly one
host and exactly one run, so grouping a line's `run` map (run id → hit
count) by that run's `RunRecord.host` reconstructs per-host line counts
without a persisted per-host table; this is pinned by
`tests/unit/cov/test_model.py::TestRunHost::test_per_host_lines_derivable_from_run_hits`.

**Known limitation:** the legacy multi-host `.gcda`-merge fallback
(board directories with no `capture.json` — back-compat with pre-tier
output directories) still collapses every host it merged into one
synthetic run with `host = ""`. `lcov`'s counter merge combines hosts
before otto ever sees per-board data, so there is no host identity left
to attribute by the time a run is registered; host attribution
requires the per-board capture path (`otto cov get` / `otto test
--cov`, one `capture.json` per board). A report built solely from the
legacy fallback therefore has no per-host data to derive.
