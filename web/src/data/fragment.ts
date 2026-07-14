// The ONE merge rule. A fragment is a partial SessionRecord in format:1 vocabulary,
// so this appends — it does not translate. There is deliberately no mapping table
// here: if you find yourself writing one, the wire has drifted from the payload and
// the fix belongs in the model, not here.
import type { MonitorSessionFragment } from "../api/export.gen";
import { dropInvalidTimestamps, type NormalizedSession } from "./exportDoc";
import { appendToIndex } from "./seriesIndex";

/**
 * Applies one live-stream fragment to a session. `warnings`, when passed, is
 * mutated in place with a summary entry whenever a metric/event/log_event
 * row is dropped for a non-finite (start or, for a span event, end)
 * timestamp — the caller (reviewStore.ts's mergeFragments) folds those into
 * the store's `warnings` channel, the live-path twin of what
 * `parseExportDocument`/`normalizeSession` (exportDoc.ts) already do for an
 * imported document. Optional (defaults to a throwaway array) so existing
 * direct callers/tests that don't care about warnings are unaffected.
 *
 * WHY this matters: stream.ts's `isFragment` only checks STRUCTURE (is
 * `metrics` an array, etc.), never each record's own timestamp(s). Without
 * this filter:
 *  - a malformed metric row would flow straight through to `appendToIndex`
 *    below and land in the series index's `tsMs` array — which every reader
 *    (health.ts's `lastIndexAtOrBefore`, seriesIndex.ts's own `lowerBound`)
 *    assumes is ASCENDING and binary-searches. A NaN entry there doesn't
 *    crash anything; `tsMs[mid] <= t` is always false for NaN, so the search
 *    silently misbehaves and slices the wrong window from then on.
 *  - a malformed event/log_event row would land in `session.events`/
 *    `logEvents` unfiltered (Plan 5b final-review Finding [2]): it renders
 *    "Invalid Date" in EventsPanel, is invisible on charts (a NaN `fromMs`
 *    fails every overlap comparison in `eventMarkers`), and — since the
 *    import path (exportDoc.ts) already drops the same row — silently
 *    DISAPPEARS again on the next resync. One rule, applied at both
 *    boundaries via this same `dropInvalidTimestamps`, closes that gap: a
 *    bad row behaves identically whether it arrives via Import or SSE.
 *  - an `EventRecord` with a valid `timestamp` but a malformed
 *    `end_timestamp` (a span whose end can't be parsed) would pass this
 *    filter, then produce a NaN `toMs` wherever `end_timestamp` is read
 *    (charts/options.ts's `eventMarkers`, EventsPanel.tsx) — NaN fails every
 *    overlap comparison, so the span silently vanishes from every chart,
 *    un-warned (Finding [3]). `dropInvalidTimestamps`'s `endTimestamp`
 *    selector below closes that gap too, on both boundaries.
 * Filtered ONCE here — the point a fragment enters the store — rather than
 * in every reader.
 */
export function applyFragment(
  session: NormalizedSession,
  frag: MonitorSessionFragment,
  warnings: string[] = [],
): NormalizedSession {
  if (frag.session !== session.id) return session;

  const metrics = dropInvalidTimestamps(frag.metrics ?? [], "metric", session.id, warnings);
  const logEvents = dropInvalidTimestamps(frag.log_events ?? [], "log event", session.id, warnings);
  const fragEvents = dropInvalidTimestamps(
    frag.events ?? [],
    "event",
    session.id,
    warnings,
    (e) => e.end_timestamp,
  );
  const deletedIds = frag.deleted_event_ids ?? [];
  const chartMapPatch = frag.chart_map ?? {};
  const hasMetaPatch = frag.meta !== null && frag.meta !== undefined;
  const hasChartMapPatch = Object.keys(chartMapPatch).length > 0;

  // A heartbeat/no-op fragment (right session, nothing else set) has nothing
  // to mutate or replace below — return the SAME session object rather than
  // a fresh copy. Checked BEFORE any mutation (not by comparing fields
  // after) since metrics/logEvents are appended IN PLACE further down: a
  // post-hoc "did endMs/events/meta/chartMap change" check would wrongly
  // call a real metrics-only append a no-op whenever the new point's
  // timestamp doesn't exceed the session's current endMs. mergeFragments
  // (reviewStore.ts) uses this reference equality to skip both the
  // array-copy and the store write for a batch of otherwise-empty fragments
  // (e.g. the SSE client's keepalive-shaped ticks).
  if (
    metrics.length === 0 &&
    logEvents.length === 0 &&
    fragEvents.length === 0 &&
    deletedIds.length === 0 &&
    !hasMetaPatch &&
    !hasChartMapPatch
  ) {
    return session;
  }

  // Index arrays are mutated IN PLACE (O(batch)); the session object is replaced so
  // zustand re-renders. Copying the point arrays to get a new identity would make
  // every tick O(total run length) — the exact cost this design exists to remove.
  if (metrics.length > 0) {
    session.metrics.push(...metrics);
    appendToIndex(session.index, metrics);
  }
  if (logEvents.length > 0) session.logEvents.push(...logEvents);

  let events = session.events;
  if (fragEvents.length > 0) {
    // id is optional by design (the collector assigns it; a row can arrive without
    // one). Upsert-by-id only applies to rows that HAVE an id — keying a Map on
    // `e.id` for id-less rows would collapse every one of them onto the same
    // `null`/`undefined` key, silently dropping all but the last. id-less rows are
    // never edits (there's nothing to address them by), so they're just appended.
    const idless = events.filter((e) => e.id == null);
    const byId = new Map(events.filter((e) => e.id != null).map((e) => [e.id, e]));
    for (const e of fragEvents) {
      if (e.id == null) idless.push(e);
      else byId.set(e.id, e); // upsert: add AND edit
    }
    events = [...idless, ...byId.values()];
  }
  if (deletedIds.length > 0) {
    const gone = new Set(deletedIds);
    events = events.filter((e) => e.id == null || !gone.has(e.id));
  }

  let endMs = session.endMs;
  for (const m of metrics) {
    const ts = Date.parse(m.timestamp);
    if (ts > endMs) endMs = ts;
  }

  const meta = hasMetaPatch
    ? {
        interval: frag.meta?.interval ?? null,
        charts: frag.meta?.charts ?? [],
        tabs: frag.meta?.tabs ?? [],
      }
    : session.meta;

  const chartMap = hasChartMapPatch ? { ...session.chartMap, ...chartMapPatch } : session.chartMap;

  return { ...session, events, endMs, meta, chartMap };
}
