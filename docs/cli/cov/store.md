(coverage-store-json)=
# The coverage store (`store.json`)

`store.json` is the canonical, versioned artifact `otto cov report`
writes for downstream consumers — external tooling, a foreign report
viewer — to read back; the in-process renderer consumes the same store
directly, in memory, before it is ever serialized. `CoverageStore.save`
(`otto.coverage.store.model`) stamps every file with a top-level `"format"`
key: the one version otto writes (`STORE_WRITE_VERSIONS` in
`otto.coverage.formats`, today `9`). `CoverageStore.load` accepts only the
versions otto declares it reads (`STORE_READ_VERSIONS`, today `9` alone): a
file whose `"format"` is missing, the wrong type, or any other version fails
loud with a `ValueError` naming the versions and telling the caller to
regenerate — a v8 store reads `coverage store format v9 required; found v8 —
regenerate with otto cov get/report` — rather than attempting to read
renamed or reshaped keys under old assumptions. There is no migration shim,
by design: `store.json` is a cheap-to-regenerate report artifact, not a
long-lived source of truth, so "delete and regenerate" beats accreted,
rarely-exercised migration code. The committed manual captures it is built
from are not affected: their format is `capture.json`'s, and the store's
labels are derived from them at report time.

This is otto's own renderer-shaped schema: a reshape bumps the version, and
dropping the old version from what otto reads and writes is a breaking
change, marked like any other. The one export built for consumers otto
does not control, `tickets.json` (`--tickets-json` / `--cov-tickets-json`),
deliberately does **not** share this version counter; see
{ref}`coverage-tickets-json`.

## Top-level keys

`CoverageStore.save` writes exactly ten:

- **`format`** — the version stamp above.
- **`tier_order`** — the tier precedence list, first entry highest. Drives
  column order and the winner-take-all row colouring.
- **`tier_colors`** — tier name → colour string, seeded from each
  `TierConfig.color`.
- **`thresholds`** — `{"high", "medium"}`, sourced from `[coverage.report]`
  (see {doc}`index`); the render cutoffs.
- **`stat_types`** — the type-extensible stats vocabulary `("line",
  "branch", "decision")`. `decision` is a declared slot with no producer,
  so a consumer should render "no decision data" rather than assume every
  declared type carries values.
- **`runs`** — the run table, one `RunRecord` per capture or synthetic
  per-tier load; a record's `id` is its index here.
- **`tickets`** — ticket id → `TicketRecord`: `id`, `url` (`None` when
  `[coverage.tickets]` configures no template, or the id doesn't match
  one), and `commits`, the shas that named it.
- **`files`** — one `FileRecord` each: `path`, `lines`,
  `excluded_lines` (the line numbers the exclusion rules deleted out of
  `lines`), `branch_excluded_lines` (lines whose branch records were
  removed while the line itself still counts), and `functions`, the
  compiler-reported functions: a list of `{name, start_line, end_line,
  hits}` sorted by start line, `hits` being tier name → call count and
  `end_line` `null` when the compiler did not report one.
- **`overrides`** — the asserted-entry table, one `OverrideRecord` per
  entry loaded from the override file ({doc}`index`):
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

## Per-run fields (`RunRecord`)

- **`id`**, **`tier`** — index into `runs`, and the tier this run credits.
- **`label`** — the run's identity: what a drilldown chip shows, what the
  report groups a context by, and what `?ctx=` pins. It is
  `<host_id>/<product>` for a run on a host (the id `otto monitor` and
  `otto test` print), `<tier>/<product>` for a unit view, and the tier alone
  for a `--tier` tracefile. otto builds it; no input names its own. `CoverageStore.load`
  rebuilds each row's label and refuses a row whose stored label differs,
  naming the run id, and refuses a row whose `source` is not one of the
  four below.
- **`source`** — the input the run was folded from: `run` (an e2e capture
  from a run directory's `cov/`), `manual` (a committed manual capture),
  `unit` (a unit view) or `tracefile` (a `--tier` tracefile).
- **`run_dir`** — the run directory an e2e (`source` `run`) capture came
  from; `""` for every other source.
- **`board`**, **`host`**, **`product`** — the capture's board id. `host` is
  the explicit host identity, which every e2e and manual run carries; it is
  `""` only for a run with no single host behind it (a unit harvest, a
  `--tier` tracefile). `product` is the `<product>` segment the capture was
  produced from, `""` for a `--tier` tracefile, which names no product. A
  newer manual capture of the same tier, host and product supersedes the
  older one ({ref}`coverage-supersede`).
- **`labs`**, **`captured_at`**, **`tester`**, **`ticket`**, **`note`**,
  **`base_commit`**, **`dirty_remap`** — capture provenance, carried
  straight through from `capture.json`.
- **`aging`** — computed at report time, not read from `capture.json`: true
  when the validity pass found a manual capture older than its tier's
  `max_age_days` ({doc}`../../architecture/subsystems/coverage/manual`).

`source` and `run_dir` are what the report's `Sources:` log line counts
({doc}`report`): per tier, its e2e captures and the distinct run directories
they came from, its manual captures, unit views and tracefiles. A superseded
or deduplicated capture and a missing tracefile leave no run, so they are
never counted.

## Per-line fields (`LineRecord`, under each file's `lines`)

- **`hits`** — tier name → execution count; **`branches`** — one entry per
  `(block, branch)` on the line, each with its own per-tier counts and
  tri-state reachability ({doc}`../../architecture/subsystems/coverage/types`).
- **`state`** — `"stale"`, `"aging"`, or `null`, set by the manual-capture
  validity pass ({doc}`../../architecture/subsystems/coverage/manual`).
- **`run`** — run id → hit count, written only for runs that actually
  executed the line; **`stale_run`** — ids of runs whose evidence for this
  line was revoked.
- **`ticket`** — the ticket id(s) named by the commit that last touched the
  line. A **list**, because a commit may name several ids and every one of
  them owns the line
  ({doc}`../../architecture/subsystems/coverage/attribution`'s "overlap"
  ruling).
- **`asserted`** — tier name → the `overrides` ids that asserted this line
  in that tier. Present **only while the tier's sole hits are
  override-sourced**: a real run covering the line clears the mark, so the
  key answers "is this tier's evidence here manual?" rather than "was an
  override ever written for it".

`run`, `stale_run`, `ticket`, and `asserted` are omitted from the
serialized line entirely when empty, rather than written as `null`, `[]`,
or `{}`.

`ticket` and the `tickets` table are populated by exactly one thing:
`[coverage.tickets]` being configured at all
({doc}`../../architecture/subsystems/coverage/attribution`). A report
without it still writes the full shape — a present-but-empty `tickets`
table and every `LineRecord` omitting `ticket` — and the coverage numbers
are identical either way, since attribution annotates lines and never
changes what counts as covered.

## Per-host breakdowns are derived, not stored

The schema carries no per-line host data at all. One capture is exactly one
host, one product, and one run, so grouping a line's `run` map (run id → hit
count) by that run's `RunRecord.host` (or `RunRecord.product`) reconstructs
per-host and per-product line counts without a persisted table for either.
The report's runs page does exactly that grouping.

Every e2e and manual run comes from one per-(host, product) capture, so
there is always a host to group by. A unit harvest or a `--tier` tracefile
names no host and drops out of the per-host view.
