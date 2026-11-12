// The ONE merge rule. A fragment is a partial SessionRecord in format:1 vocabulary,
// so this appends — it does not translate. There is deliberately no mapping table
// here: if you find yourself writing one, the wire has drifted from the payload and
// the fix belongs in the model, not here.
import type { MonitorSessionFragment, TunnelRecord } from "../api/export.gen";
import { normalizeEventStyle } from "./eventStyle";
import {
  dropInvalidTimestamps,
  editableEventId,
  type NormalizedSession,
  observedAtMicros,
  parseObservedAt,
} from "./exportDoc";
import { appendToIndex } from "./seriesIndex";
import { parseTs } from "./time";

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
 *  - a malformed event/log_event row would land in
 *    `session.events`/`logEvents` unfiltered: it renders "Invalid Date" in
 *    EventsPanel, is invisible on charts (a NaN `fromMs` fails every overlap
 *    comparison in `eventMarkers`), and — since the import path
 *    (exportDoc.ts) already drops the same row — silently DISAPPEARS again
 *    on the next resync. One rule, applied at both boundaries via this same
 *    `dropInvalidTimestamps`, closes that gap: a bad row behaves identically
 *    whether it arrives via Import or SSE.
 *  - an `EventRecord` with a valid `timestamp` but a malformed
 *    `end_timestamp` (a span whose end can't be parsed) would pass this
 *    filter, then produce a NaN `toMs` wherever `end_timestamp` is read
 *    (charts/options.ts's `eventMarkers`, EventsPanel.tsx) — NaN fails every
 *    overlap comparison, so the span silently vanishes from every chart,
 *    un-warned. `dropInvalidTimestamps`'s `endTimestamp`
 *    selector below closes that gap too, on both boundaries.
 * Filtered ONCE here — the point a fragment enters the store — rather than
 * in every reader.
 *
 * `opts.advanceHorizon` (default true): whether the fragment's
 * `observed_at` may raise the display horizon (`observedMs`, `observedAt`).
 * A review source stamps its mutation responses too; that stamp orders the
 * event changes exactly as a live one does, but it is not an observation of
 * the session, so it never moves the horizon. reviewStore.ts's
 * mergeFragments is the one caller that decides it.
 */
export function applyFragment(
  session: NormalizedSession,
  frag: MonitorSessionFragment,
  warnings: string[] = [],
  opts: { advanceHorizon?: boolean } = {},
): NormalizedSession {
  if (frag.session !== session.id) return session;

  const metrics = dropInvalidTimestamps(frag.metrics ?? [], "metric", session.id, warnings);
  const logEvents = dropInvalidTimestamps(frag.log_events ?? [], "log event", session.id, warnings);
  // Style resolved here, once, as on the import path (exportDoc.ts).
  const stamped = admitEventChanges(
    session,
    observedAtMicros(frag.observed_at),
    dropInvalidTimestamps(frag.events ?? [], "event", session.id, warnings, (e) => e.end_timestamp),
    frag.deleted_event_ids ?? [],
  );
  const fragEvents = stamped.upserts.map(normalizeEventStyle);
  const deletedIds = stamped.deletedIds;
  const chartMapPatch = frag.chart_map ?? {};
  const hasMetaPatch = frag.meta !== null && frag.meta !== undefined;
  const hasChartMapPatch = Object.keys(chartMapPatch).length > 0;
  // REPLACE semantics (the meta precedent): null/undefined = no update,
  // a list — including [] — replaces the set wholesale. Last known state,
  // expressed on the wire (spec 2026-07-16 §1).
  const hasTunnels = frag.tunnels != null;
  // The server's clock (#866) only ever raises the display horizon; it never
  // touches endMs, which stays metric-only for health.
  const fragObservedMs = (opts.advanceHorizon ?? true) ? parseObservedAt(frag.observed_at) : null;
  const hasObservation = fragObservedMs !== null && fragObservedMs > session.observedMs;
  const observedMs = hasObservation ? fragObservedMs : session.observedMs;
  const observedAt = hasObservation ? (frag.observed_at ?? null) : session.observedAt;

  // A heartbeat/no-op fragment (right session, nothing else set, or only
  // event changes that admitEventChanges found overtaken) has nothing
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
    !hasChartMapPatch &&
    !hasTunnels &&
    !hasObservation
  ) {
    return session;
  }

  // Index arrays are mutated IN PLACE (O(batch)); the session object is replaced so
  // zustand re-renders. Copying the point arrays to get a new identity would make
  // every tick O(total run length) — the exact cost this design exists to remove.
  // Loops, not `push(...batch)`: V8 caps spread arguments by stack size
  // (~125k on the default stack), and a flush batch buffered behind a stalled
  // main thread can be larger than that — the spread threw RangeError inside
  // the flush, uncaught, and killed the stream (#382).
  if (metrics.length > 0) {
    for (const m of metrics) session.metrics.push(m);
    appendToIndex(session.index, metrics);
  }
  if (logEvents.length > 0) {
    for (const e of logEvents) session.logEvents.push(e);
  }

  let events = session.events;
  if (fragEvents.length > 0) {
    // id is optional by design (the collector assigns it; a row can arrive without
    // one). Upsert-by-id only applies to rows that HAVE an id — keying a Map on
    // `e.id` for id-less rows would collapse every one of them onto the same
    // `null`/`undefined` key, silently dropping all but the last. id-less rows are
    // never edits (there's nothing to address them by), so they're just appended.
    // A repeated id (a view-only row) addresses no single row either, so those
    // rows stay, every copy. An edit replaces its row where it stands and a new
    // row goes last, so the rows keep the order the server's document holds
    // them in, and an export from this tab matches one from a reloaded tab.
    const held = new Set(events.flatMap((e) => editableEventId(session, e) ?? []));
    const edits = new Map<number, (typeof fragEvents)[number]>();
    const added: (typeof fragEvents)[number][] = [];
    const addedAt = new Map<number, number>();
    for (const e of fragEvents) {
      if (e.id == null) {
        added.push(e);
      } else if (held.has(e.id)) {
        edits.set(e.id, e);
      } else {
        const at = addedAt.get(e.id);
        if (at === undefined) {
          addedAt.set(e.id, added.length);
          added.push(e);
        } else {
          added[at] = e;
        }
      }
    }
    events = [
      ...events.map((e) => {
        const id = editableEventId(session, e);
        return (id !== null ? edits.get(id) : undefined) ?? e;
      }),
      ...added,
    ];
  }
  if (deletedIds.length > 0) {
    const gone = new Set(deletedIds);
    events = events.filter((e) => {
      const id = editableEventId(session, e);
      return id === null || !gone.has(id);
    });
  }

  let endMs = session.endMs;
  for (const m of metrics) {
    const ts = parseTs(m.timestamp);
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

  const tunnels = hasTunnels ? (frag.tunnels as TunnelRecord[]) : session.tunnels;

  return {
    ...session,
    events,
    eventStamps: stamped.eventStamps,
    endMs,
    observedMs,
    observedAt,
    meta,
    chartMap,
    tunnels,
  };
}

/** The event changes in one fragment that are newer than what the session
 * already holds (#929 §4): a late echo must never roll an event back. With a
 * server `stamp`, an upsert or deletion for id X is dropped when the stamp is
 * no later than the session's snapshot floor or than X's last applied stamp;
 * what is kept records the stamp, tombstones included. Without one (a file
 * the tab loaded itself, an older server), every change applies and no
 * stamp is recorded.
 * Every change in the fragment is judged against the stamps BEFORE it, so
 * one fragment's upsert and delete of the same id both stand, as before.
 * O(changes in the fragment); the Map is copied only when a change is kept,
 * never on a metrics-only tick, and never mutated in place. Id-less rows
 * cannot be addressed, so cannot be overtaken: they always apply. */
function admitEventChanges<E extends { id?: number | null }>(
  session: NormalizedSession,
  stamp: number | null,
  upserts: E[],
  deletedIds: number[],
): { upserts: E[]; deletedIds: number[]; eventStamps: Map<number, number> } {
  if (stamp === null || (upserts.length === 0 && deletedIds.length === 0)) {
    return { upserts, deletedIds, eventStamps: session.eventStamps };
  }
  const floor = session.eventFloor;
  const held = session.eventStamps;
  const isLate = (id: number): boolean =>
    (floor !== null && stamp <= floor) || stamp <= (held.get(id) ?? Number.NEGATIVE_INFINITY);
  const keptUpserts = upserts.filter((e) => e.id == null || !isLate(e.id));
  const keptDeletes = deletedIds.filter((id) => !isLate(id));
  let eventStamps = held;
  for (const id of [...keptUpserts.map((e) => e.id), ...keptDeletes]) {
    if (id == null) continue;
    if (eventStamps === held) eventStamps = new Map(held);
    eventStamps.set(id, stamp);
  }
  return { upserts: keptUpserts, deletedIds: keptDeletes, eventStamps };
}
