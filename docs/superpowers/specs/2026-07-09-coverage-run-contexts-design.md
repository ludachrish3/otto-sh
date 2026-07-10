# Coverage Run Contexts (Per-Capture Line Traceability) — Design

**Date:** 2026-07-09
**Status:** Approved in brainstorming; awaiting spec review
**Scope:** Follow-on to the 2026-07-02 tier/collection model. Adds a per-run
("context") dimension to the coverage report so each covered line can be traced
back to the specific capture(s) — manual session or e2e run — that hit it,
rendered as a per-line expandable drilldown in the HTML report.

## 1. Context and problem

The store aggregates hits per **tier** (`LineHits.counts: dict[tier, int]`).
Each capture file (`capture.json`) is a complete per-run record — pin, blob
anchors, per-line hits, ticket/note/board/date — but that identity is lost the
moment the reporter folds captures into the store: two manual sessions and an
e2e run all collapse into per-tier sums. `store.provenance` lists the captures
that contributed, but nothing links a *line* back to *which* capture(s) covered
it.

For traceability — "which run/ticket/session covered this line?" — the report
needs a per-line, per-run breakdown, in the spirit of coverage.py's dynamic
contexts.

## 2. Decisions log (from brainstorming Q&A)

| # | Question | Decision |
| --- | --- | --- |
| 1 | Which sources carry a context | Manual **and** e2e captures (one context per capture), **plus** one synthetic context per non-capture load: each unit-kind tier, the legacy merged-`.gcda` system load, and each explicit `--tier NAME=path` `.info` load (label = tier name) — so every tier shows up in the drilldown uniformly. Per-board synthetic contexts for the legacy path are not possible: the cross-host lcov merge collapses per-board identity before loading; board-level traceability comes from the capture path |
| 2 | Annotation CLI | `--ticket` and `--note` already exist on `otto cov get` but are stamped only into manual-kind captures (silently dropped otherwise); change = stamp them for **all** capture-producing tiers. `--ticket` remains *required* only for manual-kind; `tester` stays manual-only |
| 3 | Context display label | The host's **display name** (`host.name`) — disambiguates elements/hosts that are a slot in a server rack. Captures gain an optional `display_name` field stamped at production time (`board` holds `host.id` today); fallback chain: `display_name` → `board` → tier name (synthetic contexts). Ticket/note/date/pin render in the chip tooltip and the index run table, not in the label |
| 4 | Granularity | Line-level only for v1. Branch pills keep aggregated per-tier rendering; per-context branch data can layer on later (captures already record per-context branch triples) |
| 5 | Where the dimension lives | In the store model (run table + per-line context hits), derived **fresh at report time** from the capture inputs. Nothing new is persisted to the repo; captures remain the only durable truth. Report-time sidecar and contexts-as-pseudo-tiers rejected |
| 6 | Provenance table | `CoverageStore.contexts` **replaces** `store.provenance` outright (hard cutover); the index page's Captures table reads the run table |
| 7 | Stale interplay | A line staled under one capture records that context id as revoked; the drilldown lists the revoked run marked STALE — the "here's the ticket to re-verify" workflow |
| 8 | UI mechanism | Pure-CSS `<details>` expander in a new right-most source-table column; no JavaScript |
| 9 | Frontend look | Pure-CSS drilldown is fine for now; unifying the coverage and monitoring frontend looks is a later work item, explicitly **not** in scope here |

## 3. Concept and data flow

A **context** = one run's worth of coverage input.

- Manual captures: committed in `.otto/coverage/manual/`, loaded automatically
  at every report — unchanged from today. One context per capture.
- E2e captures: `capture.json` per board dir under the given output dir(s) —
  unchanged from today. One context per capture.
- Unit harvest: one **synthetic** context per unit-kind tier, registered on
  its first successful `harvest_dirs` load; label = tier name, no
  pin/board/annotation.
- Legacy loads: the merged-`.gcda` system pipeline and each explicit
  `--tier NAME=path` `.info` load likewise get one synthetic context per
  tier (label = tier name). The lcov merge combines all hosts **before**
  loading, so per-board identity is unrecoverable on this path.
- The report remains a pure report-time stitch: as the reporter folds each
  capture in, it allocates a small integer context id and appends a
  `ContextRecord` to the store's run table. Per-line hits are recorded under
  that id **through the same remap/anchor chain the tier hits already
  travel** (dirty-tree remap for pin==HEAD e2e captures; blob-diff anchor
  chain for pinned manual captures), so context data is drift-corrected
  identically — a manual hit on old line 5 that remaps to current line 12
  carries its context to line 12.
- `store.json` (a report artifact, not a repo artifact) carries the run table
  and the per-line context data for downstream consumers.

Same-capture dedupe: if the identical capture is folded twice (two cov dirs
referencing one board dir), the reporter dedupes by
`(tier, pin, board, captured_at)` before allocating a context.

## 4. Capture schema and CLI

`capture.json` gains one optional field (schema stays v1 — the field defaults
to `None`, so new readers load old captures unchanged):

- `display_name: str | None` — the host's display name (`host.name`), stamped
  at production time. `board` continues to hold the staging dir name
  (`host.id`); the capture must stay self-contained because the reporting
  machine may not have that lab's host database. `otto cov get` builds the
  id → name map from the resolved coverage hosts and passes it through
  `produce_captures` to `build_capture`.

`ticket` and `note` fields already exist on the model and are simply `None`
for e2e today. CLI policy changes in `otto cov get`:

- `--ticket STR` and `--note STR` (both existing flags) are stamped into the
  captures of every tier kind, not just manual. For manual-kind tiers
  `--ticket` remains **required** (unchanged).
- `tester` attribution remains manual-only — an automated run has no human
  session to attribute; its `--ticket` typically names a CI run or issue.

## 5. Store model changes (`store/model.py`)

New dataclass:

```python
@dataclass
class ContextRecord:
    id: int                       # index into CoverageStore.contexts
    tier: str
    label: str                    # display_name → board → tier name (synthetic)
    board: str = ""               # host.id staging-dir name; "" for synthetic
    labs: list[str] = field(default_factory=list)
    captured_at: str = ""         # "" for synthetic contexts
    tester: dict[str, str] | None = None
    ticket: str | None = None
    note: str | None = None
    pin: str = ""                 # "" for synthetic contexts
    dirty_remap: bool = False
    aging: bool = False           # capture-level, set by the validity pass
```

(`tester` and `dirty_remap` carry over from today's provenance rows so the
index Captures table keeps its columns.)

Labels may repeat (two runs on the same host) — that is fine: the chip
tooltip and the index run table carry ticket/note/date/pin to tell runs
apart. No collision-suffix logic.

- `CoverageStore.contexts: list[ContextRecord]` — the run table.
  `store.provenance` is **removed**; the index page's Captures table renders
  from `contexts` (same rows plus id/label). `store.json` drops the
  `"provenance"` key and gains `"contexts"` (hard cutover; the legacy
  list-only load shape keeps working with an empty run table).
- `LineRecord` gains:
  - `context_hits: dict[int, int]` — ctx id → hit count for this line.
  - `stale_contexts: list[int]` — runs whose evidence for this line was
    revoked by the validity pass.
- Merges are dict-add (`context_hits`) and order-preserving union
  (`stale_contexts`).
- Serialization: per-line sparse `"ctx": {"3": 5}` and `"stale_ctx": [2]`,
  both omitted when empty. Growth is bounded by captures × hit-lines; small
  integer keys keep it compact.

## 6. Loader and validity changes

- `CoverageStore.add_context(...) -> int` allocates the next id, resolves the
  display label (`display_name` → `board` → tier name), and appends the
  record. The reporter calls it once per (deduped) capture — and once per
  tier for the synthetic contexts (unit harvest, legacy merged-gcda,
  explicit `.info` loads) — before invoking the corresponding loader; the
  provenance-append currently inside `apply_manual_capture` is removed in
  favor of this single reporter-side registration.
- `_insert_lines` (validity.py) grows a `ctx_id: int | None` parameter: when
  set, each inserted line also credits `context_hits[ctx_id]`. Branch triples
  are unchanged (decision #4).
- `load_capture_into_store` / `load_dirty_capture_into_store` /
  `apply_manual_capture` accept the allocated ctx id and pass it through.
- `apply_manual_capture` additionally:
  - stale lines → `line.stale_contexts.append(ctx_id)` (alongside today's
    line-level stale marking). The revoked-run chip remains listed even if a
    later capture validly covers the line — that is the traceability payoff;
    line-level color precedence ("covered wins over stale") is unchanged.
  - aging captures → `ContextRecord.aging = True` (capture-level; per-line
    aging state handling is unchanged).
- The unit-tier harvest path passes its tier's synthetic ctx id (one per
  unit-kind tier, shared across that tier's `harvest_dirs`); the legacy
  merged-gcda and explicit `.info` loads pass their tier's synthetic ctx
  id. `LCOVLoader.load` grows an optional `ctx_id` parameter for this.
  All are otherwise untouched.

## 7. Renderer / UI (`renderer/`, `templates/file.html`, `report.css`)

New right-most column ("runs") in the annotated source table:

```text
… | branches | source                        | runs
… |   0.1    | if (retry_count > MAX)        |  ▾ 3
                                             ┌──────────────────────────────┐
                                             │ ● rack2-slot4 (manual)  × 5  │
                                             │ ● gateway-a (system)   × 12  │
                                             │ ● unit                 × 40  │
                                             │ ● rack2-slot4 (manual) STALE │
                                             └──────────────────────────────┘
```

- `<details class="ctx"><summary>N</summary><div class="ctx-panel">…</div></details>`
  in the cell — pure CSS, no JavaScript. The panel is absolutely positioned
  (right-anchored overlay) so expanding does not reflow the table.
- Each chip: tier-colored dot (reusing the existing `--tier-<index>` custom
  properties), the context label (host display name; tier name for
  synthetic contexts), `× count`, and a `title` tooltip carrying tier,
  ticket, note, capture date, and pin so same-host runs stay tellable
  apart. Stale chips struck through in the stale color; chips of aging
  contexts get the aging accent.
- Lines with no context data (unit-only or uncovered) render an empty cell.
- `index.html`: the Captures table becomes the run table (adds the label
  column); legend unchanged.

## 8. Testing

- **Unit — model:** `context_hits` / `stale_contexts` merge semantics;
  `add_context` id allocation and label fallback chain (`display_name` →
  `board` → tier name); `store.json` round-trip with contexts (and
  legacy-shape load → empty run table); capture round-trip with and without
  `display_name`.
- **Unit — loaders/validity:** ctx ids credited through clean, dirty-remap,
  and manual anchor-chain paths; stale line records the revoked ctx id;
  aging sets the record flag; unit harvest and legacy merged-gcda /
  explicit-`.info` loads get their per-tier synthetic contexts.
- **Unit — renderer:** runs column emitted with correct summary count, chip
  classes, tier color indices, stale/aging chip states; empty cell for
  context-free lines; Captures table renders from the run table.
- **Unit — CLI:** `--ticket`/`--note` stamped into e2e-kind captures too;
  manual-kind still requires `--ticket`; `display_name` stamped from the
  resolved host's `host.name`.
- **Unit — reporter:** identical capture folded twice dedupes to one context
  by `(tier, pin, board, captured_at)`.
- **Integration:** extend the capture→report cycle test to a manual + e2e
  two-capture scenario asserting drilldown contents end-to-end, including a
  staled manual line listing the revoked run.

## 9. Out of scope

- Per-context branch breakdown in the drilldown (decision #4; data already
  captured, UI can layer on later).
- Unifying the coverage and monitoring frontend looks (decision #9; separate
  later work item).
- Any repo-persisted run registry beyond the existing committed manual
  captures (decision #5).
- Filtering/sorting the report by context (natural follow-on: a per-ticket
  report view).
