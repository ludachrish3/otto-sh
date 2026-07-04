# Monitor Phase 3 Plan B — ship-as-noted follow-ups

Source: final whole-branch review (three rounds) of the Plan B (log-sourced
data) branch. All items were triaged non-blocking; the final verdict was
"Ready to merge: Yes". Spec: `docs/superpowers/specs/2026-07-03-monitor-metrics-phase3-design.md`;
plan: `docs/superpowers/plans/2026-07-04-monitor-metrics-plan-b.md`.

## Behavior / robustness

- **Tab-collision error surfaces late** (`src/otto/monitor/collector.py`,
  `get_meta_model`): a chart/table tab-id collision raises `ValueError` only
  when `/api/meta` is hit — a colliding config collects but serves repeated
  500s. Validate once at collector construction (or server startup) to fail
  fast at boot.
- **Aware non-UTC timestamps aren't normalized** (`src/otto/monitor/log_sourced.py`,
  `_as_utc`): only naive values are coerced; an ISO row with `+02:00` keeps
  its offset through `ev.ts.isoformat()` into the DB, where `ORDER BY ts` is
  lexicographic (mixed-offset rows can misorder on reload) and the dashboard
  time cell shows offset-local digits. Fix: `.astimezone(timezone.utc)` on
  the aware branch.
- **Direct-constructor path shares parser instances across targets**
  (`MetricCollector(hosts=[h1, h2], parsers=[...])`): every target gets the
  same dict, so one `HighWaterMark`/`ProvisionalTail` would cross-drop rows.
  Pre-existing convention (`RateTracker` documents per-target deep copies;
  the factory path deep-copies via `get_host_parsers`), but Plan B widens the
  blast radius from slightly-wrong rates to silently-dropped rows. A
  `copy.deepcopy(parser_dict)` per target in the constructor closes it.
- **Events from a parser without `table_columns` are a silent black hole**:
  routed to the ring under a charts tab, never rendered. A one-line warning
  in `_record_log_events` (or a contract doc note) would surface the misuse.
- **DB commit-per-row in `_record_log_events`**: a first-tick `tail -n 200`
  backfill is 200 sequential commits (mirrors the pre-existing `write_point`
  shape). Batch as `executemany` + one commit per call when convenient.
- **Rotation can swallow one pending row** (`ProvisionalTail`): a file
  rotating between the read that holds line Z and the read that would confirm
  it loses Z (at most one row per rotation-straddling race; strictly better
  than the HWM-poisoning it replaced). Add one docstring sentence
  acknowledging it.
- **Host clock >2 days ahead of otto** would trip the injected-year rollover
  guard and misfile today's year-less rows a year back — extreme skew,
  documented here for completeness.

## Docs

- **The provisional-tail hold is documented only under CSV** in
  `docs/guide/monitor.md`; the log-event tables and Timestamps subsections
  should carry the one-sentence note too (newest event delays up to one poll
  interval).
- **Guide doesn't mention `otto.monitor.log_sourced` is off the eager import
  chain** (import-budget guard) — the examples model correct usage, but a
  note would preempt a confusing guard failure.
- **HighWaterMark same-second boundary rule vs. 1-second syslog resolution**:
  same-second rows split across tick reads are droppable (spec-accepted
  trade-off). When Phase 4 revisits table UX, consider a guide warning for
  coarse-resolution timestamp formats.
- Spec's torn-line degradation row now reads slightly optimistic
  ("re-emitted whole next tick" vs. the actual held→confirmed sequence);
  the guide is accurate — spec is a point-in-time record, no edit required.

## Tests (coverage-breadth nits from per-task reviews)

- `groupRowsFromData` per-key cap has no direct test (shares the one-liner
  with the tested `appendRows` path).
- Export round-trip pins `log_events` key-set, not content equality, in the
  browser-lane historical test (unit-level round-trip does check content).
- No test drives a tick emitting BOTH samples and events from one parser.
- Rollover-guard branch of `test_leap_day/rollover` tests exercises the
  fall-through (not the guard) during Dec 2–31 runs; a companion test could
  pin the guard branch year-round.

## Style (from per-task reviews; all cosmetic)

- `collector.py` `sample.ts or ts` vs explicit `is None`; quoted
  `get_log_events` return annotation; collision-message verb asymmetry
  (declare/have); `FakeCollector` docstring doesn't mention
  `extra_parsers`/`push_log_events`; quoted `"str | re.Pattern[str]"` and
  conftest `Coroutine` annotations; `zip(strict=True)` after a length check;
  `slice(-MAX_TABLE_ROWS)` duplicated in two logevents.ts helpers;
  `historical_table_dash` spins two single-use ThreadPoolExecutors;
  `_seed()`'s `db.close()` not in try/finally.
