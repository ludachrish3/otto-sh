# Monitor: collapse CPU tracking into a single chart

**Date:** 2026-07-17
**Status:** Design — approved, pending spec review

## Summary

Remove per-PID (per-process) CPU tracking from the monitor entirely and collapse
CPU into a **single `"CPU"` chart** carrying `Overall CPU` plus one line per core,
all sourced from a single `/proc/stat` read. Today CPU is split across two charts
fed by two commands:

- `"CPU"` — `Overall CPU` + top-5 `proc/<pid>` traces, from `top -d 0.5 -bn2`
  (`TopCpuParser`).
- `"Per-core CPU"` — `core <N>`, from `cat /proc/stat` (`PerCoreCpuParser`).

After this change there is one CPU parser (`PerCoreCpuParser`), one command
(`cat /proc/stat`), and one chart.

As a second, orthogonal improvement requested during design, the previously
hard-coded per-chart series cap becomes a **first-class, per-chart API knob**
(`MetricParser.max_series`) so any registered parser — built-in or third-party —
can set or lift its own cap, rather than the CPU chart getting a hard-coded
exemption.

## Motivation

- The per-PID CPU chart is too cluttered to be worth reading: process churn
  means an unbounded set of short-lived `proc/<pid>` traces, which is exactly
  why the frontend carries a whole tick-based *retirement* subsystem to keep it
  bounded (`web/src/data/retirement.ts`).
- With per-PID gone, `TopCpuParser`'s only remaining output is `Overall CPU`,
  which `/proc/stat` already provides — so `top -bn2` (two `top` iterations over
  0.5 s, plus a per-run core-count probe to normalize per-process %CPU) becomes
  pure overhead. Consolidating onto `/proc/stat` removes a command, a per-run
  SSH probe, and the retirement subsystem.
- Splitting overall and per-core CPU across two charts is unnecessary; one CPU
  chart is easier to read.

## Decisions (resolved during brainstorming)

1. **Source of overall CPU:** consolidate onto `/proc/stat`. Delete
   `TopCpuParser`; `PerCoreCpuParser` emits both `Overall CPU` and `core <N>`.
2. **High core counts:** the combined CPU chart is **uncapped** — it shows
   `Overall CPU` + every core regardless of count.
3. **Coloring beyond the palette:** hybrid. At ≤ 8 series each gets its own
   distinct palette color; above the 8-color palette size, `Overall CPU` stays a
   distinct bold line and all `core <N>` lines collapse into one muted style.
   The hybrid is CPU-specific (it encodes "aggregate vs. members").
4. **Cap is a general API, not a CPU special-case:** promote the per-chart cap
   to `MetricParser.max_series`, carried over the wire so the frontend applies it
   per chart. Only the *coloring* hybrid remains CPU-specific.
5. **Proc awareness is fully removed** (no legacy drop-filter). There is no
   captured historical data containing `proc/*`; test fixtures that emit it are
   updated. No back-compat shim is needed.

## Design

### 1. Backend — `src/otto/monitor/parsers.py`

**Delete `TopCpuParser`** (currently `parsers.py:233-312`) in full: the
`top -d {delay} -bn2` command, the `top_n`/`delay` constructor args, the
per-process row loop, and its hover `meta`.

**Extend `PerCoreCpuParser`** (`parsers.py:571`) to own overall CPU:

- Broaden the per-line match from `cpu\d+` to `cpu\d*` so it also matches the
  aggregate `cpu` line in `/proc/stat`.
- Branch on the suffix from `fields[0].removeprefix("cpu")`: `""` (the aggregate)
  → label `"Overall CPU"`; a numeric suffix `N` → label `"core N"`. Both use the
  identical jiffies-delta math already present (`100·(1 − Δ(idle+iowait)/Δtotal)`)
  and share the `_prev` baseline dict — the aggregate keys under `""`, so it
  needs the same one-tick baseline before it can emit (first tick establishes it,
  as cores already do).
- Change `chart` from `"Per-core CPU"` to **`"CPU"`**.
- Set **`max_series = None`** (uncapped — see §2).
- Update the docstring: it now owns overall CPU (drop the "TopCpuParser already
  charts overall CPU" note).

**`DEFAULT_PARSERS`** (`parsers.py:650`): remove the `TopCpuParser()` entry.

**`ParseContext.core_count`** (`parsers.py:77`): remove — `TopCpuParser` was its
only consumer.

### 2. Per-chart series cap — first-class API

Today the cap is a frontend constant, `MAX_SERIES_PER_CHART = 8`
(`web/src/charts/palette.ts:34`), applied uniformly at
`SubjectPage.tsx:235`. Promote it to a per-chart property declared by the parser.

- **New constant** `DEFAULT_MAX_SERIES_PER_CHART = 8` defined in
  `src/otto/models/monitor.py` (shared, so `parsers.py` imports it rather than
  `models` importing from `monitor` — avoids a layering inversion).
- **New base attribute** `MetricParser.max_series: int | None = DEFAULT_MAX_SERIES_PER_CHART`.
  A positive int caps the chart at that many series; **`None` means uncapped**.
  Any parser — including third-party parsers registered via `register_parsers` /
  `register_host_parsers` — may override it.
- **`PerCoreCpuParser.max_series = None`.**
- **`ChartSpec`** (`src/otto/models/monitor.py:84`): add
  `max_series: int | None = DEFAULT_MAX_SERIES_PER_CHART`. The default is the
  *numeric cap*, not `None`, so a read-back record missing the field is
  default-capped rather than silently uncapped; `None` is reserved to mean
  explicitly uncapped. `ChartSpecRecord` subclasses `ChartSpec` and is built via
  `ChartSpecRecord(**spec.model_dump())` (`export.py:121`), so the field flows to
  the `format:1` wire model automatically — no separate addition.
- **`get_meta_model`** (`collector.py:827-839`): the `ChartSpec(...)` construction
  already reads view attributes via `getattr`; add
  `max_series=getattr(v, "max_series", DEFAULT_MAX_SERIES_PER_CHART)`. SNMP
  descriptors that don't define the attribute fall back to the default cap (8),
  not `None` — so they stay capped, only parsers that explicitly set
  `max_series = None` are uncapped.
- **Multiple views on one chart:** the meta emits one `ChartSpec` per view, and
  the frontend already takes the first spec matching a chart label
  (`seriesTree.ts:95`). This is unchanged pre-existing behavior. For the default
  parser set the CPU chart has a single contributing shell parser
  (`PerCoreCpuParser`), so its `max_series = None` wins. (Merging duplicate
  specs by a resolution rule is explicitly out of scope; see Non-goals.)
- **Frontend:** `buildSeriesTree` (`seriesTree.ts`) reads `spec?.max_series` onto
  a new `ChartNode.maxSeries` field. `SubjectPage.tsx` replaces the hard-coded
  `active.slice(0, MAX_SERIES_PER_CHART)` with a per-chart cap:
  `const cap = chart.maxSeries; const shown = cap == null ? active : active.slice(0, cap);`
  and only sets `overflowCount` when `cap != null && active.length > cap`.
  `MAX_SERIES_PER_CHART = 8` stays as the defensive fallback for a chart with no
  spec, so `chartoptions.test.ts`'s `MAX_SERIES_PER_CHART === 8` assertion is
  unchanged.

### 3. Frontend — chart merge, coloring, proc removal

**Chart merge is automatic.** Once `PerCoreCpuParser.chart = "CPU"`, `chart_map`
and `meta.charts` regenerate and `buildSeriesTree` groups `Overall CPU` + every
`core <N>` onto one `"CPU"` chart; the `"Per-core CPU"` chart/chip disappears. No
frontend code references the old chart name, so nothing else breaks.

**Hybrid coloring (CPU-specific).** Series color is resolved from `slot` at
`options.ts:258` (`theme.series[s.slot % theme.series.length]`).

- Add `muted?: boolean` to `SeriesInput` (`options.ts:41`).
- In `SubjectPage.tsx`, when building the CPU chart's series inputs
  (`chart.chartKey === "CPU"`) **and** the number of series to render exceeds the
  palette size (`theme.series.length`, i.e. 8): give `Overall CPU` a distinct
  prominent slot (slot 0, normal weight) and set `muted: true` on every `core *`
  series. At ≤ 8 series, nobody is muted (existing distinct-slot behavior).
- At `options.ts:258`, a `muted` series renders in one fixed low-emphasis style —
  a muted grey (`MUTED_SERIES_LIGHT` / `MUTED_SERIES_DARK` constants in
  `palette.ts`) with reduced `lineWidth`/`opacity` — instead of `theme.series[slot]`.
- `chart.chartKey === "CPU"` is the single CPU-specific string in the frontend
  and is deliberately localized so it can be revisited later.
- A generic third-party **uncapped** chart with > 8 series is *not* muted; it
  cycles palette colors via the existing `slot % length` — an accepted,
  documented consequence of opting out of the cap.

**Full removal of proc awareness.**

- Delete `web/src/data/retirement.ts` entirely (`isProcMetric`,
  `RETIRE_AFTER_TICKS`, `lastKDistinct`, `isProcSeries`, `retireStaleSeries`).
- Remove the `retireStaleSeries(...)` call in `SubjectPage.tsx:223-233` with no
  replacement filter.
- Update the stale `proc/<pid>` comment at `exportDoc.ts:252`.

### 4. Backend — `collector.py` core-count probe

`TopCpuParser` was the only consumer of the per-run core count. Remove:

- `MonitorTarget.core_count` field (`collector.py:122`).
- The `grep -c ^processor /proc/cpuinfo` probe (`collector.py:~404-419`).
- The `core_count=` argument passed into `ParseContext` (`collector.py:355`),
  leaving `ParseContext(ts=ts)`.

Net effect: one fewer SSH probe per run.

### 5. Back-compat / schema

- **No schema-version or wire-format bump.** SQLite stays v2 (no column changes);
  export `format:1` is unchanged. `ChartSpec.max_series` is a new optional field
  with a default, and `*Record` read-back is `extra="ignore"`, so older otto
  builds read newer exports and vice-versa.
- **No historical `proc/*` data exists**, so full proc removal has no data-side
  consequence; only fixtures emit it and they are updated (§6).

## Testing

Following TDD — update/add tests alongside each change.

**Backend (`tests/unit/monitor/`):**

- `test_parsers.py`: delete the `TopCpuParser` test class and the
  `ParseContext.core_count` normalization/isolation tests. Rewrite the
  `PerCoreCpuParser` tests: a first tick yields nothing (baseline), a second tick
  yields `Overall CPU` (from the aggregate `cpu` line) + `core <N>`, on chart
  `"CPU"`, with `max_series is None`.
- Add a test that `get_meta_model` emits `max_series` on each `ChartSpec`:
  `None` for the CPU chart, `DEFAULT_MAX_SERIES_PER_CHART` for a default-capped
  chart, and a custom value for a parser that overrides it.
- Sweep `test_collector_db.py`, `test_export_producer.py`, `test_scoping.py`,
  `test_fake_collector.py` for stale `TopCpu` / `proc/` / `core_count` /
  `"Per-core CPU"` references and update.
- `tests/unit/models/test_jsonschema.py`: regenerate/refresh the JSON-schema
  snapshot for the new `ChartSpec.max_series` field.

**Fixtures:**

- `tests/e2e/monitor/dashboard/conftest.py:184-186`: drop the two `proc/*`
  pushes (and the `_PROC_META` constant); push `core 0` / `core 1` alongside
  `Overall CPU`, mapped to chart `"CPU"`.
- `tests/_fixtures/_fake_collector.py`: ensure any CPU emission matches the new
  single-chart shape (no `proc/*`).

**Frontend (`web/src/__tests__/`):**

- Delete `retirement.data.test.ts`, `subjectpage.retirement.test.tsx`, and the
  retirement perf test in `perf_budget.test.ts:105-144`.
- Neutralize the `proc/999` label in `importexport.livefragment.test.ts` to a
  generic non-proc example (the test exercises generic `chart_map` roundtrip, not
  per-PID semantics).
- Add tests for: (a) an uncapped chart renders **all** series (no slice to 8) and
  shows no overflow note; (b) a default-capped chart still slices at 8 with the
  note; (c) hybrid coloring — CPU chart with > 8 series marks `core *` muted and
  keeps `Overall CPU` distinct, and with ≤ 8 series marks nobody muted.
- Regenerate the TS wire types (`web/src/api/export.gen`) for the new
  `max_series` field via the existing codegen step.

## Docs

- `docs/guide/monitor.md`: drop the per-process CPU description; describe the
  single CPU chart (overall + per-core) and mention `max_series` if per-chart
  caps are documented.
- `docs/architecture/subsystems/monitoring.md`: update the parser/data-flow
  description — remove `TopCpuParser` and per-PID; describe the consolidated
  `PerCoreCpuParser` and the `max_series` chart property.
- Generated `docs/_build/**` regenerates via `make docs`; not edited by hand.

## Non-goals / out of scope

- Merging duplicate `ChartSpec`s for the same chart label with a cap-resolution
  rule. The frontend's existing first-spec-wins behavior is retained.
- A `settings.toml`-level cap override. The cap is exposed on the parser/chart
  registration API only; a config-file override can be a later addition.
- Any change to the SQLite schema version or the `format:1` wire version.
- Preserving or migrating historical `proc/*` data (none exists).

## Touched files (summary)

Backend:

- `src/otto/monitor/parsers.py` — delete `TopCpuParser`; extend
  `PerCoreCpuParser`; `DEFAULT_PARSERS`; `DEFAULT_MAX_SERIES_PER_CHART` +
  `MetricParser.max_series`; remove `ParseContext.core_count`.
- `src/otto/monitor/collector.py` — `get_meta_model` `max_series`; remove
  core-count probe/field/arg.
- `src/otto/models/monitor.py` — `ChartSpec.max_series`.

Frontend:

- `web/src/pages/SubjectPage.tsx` — per-chart cap; hybrid coloring; remove
  retirement call.
- `web/src/data/seriesTree.ts` — `ChartNode.maxSeries` from spec.
- `web/src/charts/options.ts` — `SeriesInput.muted`; muted styling.
- `web/src/charts/palette.ts` — muted-series color constants.
- `web/src/data/retirement.ts` — **deleted**.
- `web/src/data/exportDoc.ts` — stale comment.
- `web/src/api/export.gen` — regenerated.

Tests & fixtures, docs — per the sections above.
