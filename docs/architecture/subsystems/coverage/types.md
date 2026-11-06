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
three-tier workflow is covered in {doc}`../../../cli/cov/index`.

Only the **manual** tier's data is committed into the project's git repository, even though
every tier's data is anchored to `base_commit`: e2e and unit data are
reproducible — a fresh `otto test --cov` or a rebuilt unit harvest
regenerates them — but a manual session (a human at a GDB prompt, poking at
running hardware) produces evidence nothing else can regenerate. Selecting a
manual-kind tier on `otto cov get` copies the capture into the project's
committed store at `.otto/coverage/manual/` — proof of that session that
travels with the code and is PR-reviewable. E2e data instead lives in each
test run's output directory, and unit data is harvested fresh from the build
tree's unit views at report time, so neither needs a permanent home.

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
{doc}`../../../cli/cov/index` for the full valid/stale/aging/unverifiable
state table and how each renders in a report.

## Tier mechanics: precedence, kind, color, label

`otto.coverage.tiers.load_tiers` validates each entry of the raw
`[coverage.tiers]` dict through `CoverageTierSpec` against the project root
(the refusals of settings load) and turns them into `TierConfig` values
sorted by `precedence`, first entry highest. An empty or missing table yields exactly one tier — `system`, `kind="e2e"`,
`precedence=1` — which is byte-for-byte pre-tier behavior: a project that never
declares tiers never meets the machinery.

`kind` is the closed set (`e2e` / `unit` / `manual`, validated at
settings-parse time by `CoverageTierSpec`), and it only selects *collection*:
which command produces the tier's data and where that data lives. The tier
**name** is the identity everything downstream keys on — `LineHits.counts`,
`BranchHits.reachable`, `CoverageStore.tier_order`, `RunRecord.tier` — so
two tiers may share a kind, and a fourth or fifth tier costs the model
nothing. `CoverageReporter.run` seeds `tier_order` from the declared tiers
*before* any data folds, then appends `--tier NAME=PATH` tracefile tiers
not already present: a tier with no data this run still gets its column and its
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
| excluded | at report, by the exclusion filter | `FileRecord.excluded_lines` (a set) |
| uncovered | at render | nothing — a `LineRecord` with no tier hit |
| uncoverable | at render | nothing — no `LineRecord` at all |

An excluded line is `uncoverable` by the third column's test — the filter
deleted its `LineRecord` — so the two are told apart by `excluded_lines`
alone, and excluded is checked first.

`LineRecord.state` is single-valued, which is exactly why exclusion is not
one of its values: "excluded always wins" cannot coexist with
covered/stale/aging in one slot. Exclusion sidesteps the problem instead of
encoding it — `otto.coverage.exclusions.apply.apply_exclusions` **deletes**
the excluded `LineRecord`s from the merged store, in the reporter, before
attribution and before rendering. There is no state to rank once the record
is gone, and every stats path is correct without knowing exclusion exists.

What survives on the record is `FileRecord.excluded_lines` (and
`branch_excluded_lines`), the rules' verdict on the source. Those hold line
numbers with no `LineRecord` behind them, which is what the grey rows and
the per-file excluded count are rendered from — see {doc}`renderer`.

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

## The store

`store.json` is the versioned artifact `otto cov report` writes beside the
report for downstream tooling; the renderer reads the same `CoverageStore` in
memory. Its schema — version, top-level keys, per-run and per-line fields —
has one home: {ref}`coverage-store-json`.
