// web/src/data/eventApi.ts
// The dashboard's ONLY mutation surface. One rule for both modes:
// every 2xx response is applied locally as a synthetic fragment through
// applyFragment's existing upsert — in live mode the SSE echo then delivers
// the same record again and the upsert-by-id makes it a no-op, so ordering
// between response and echo cannot matter and no optimistic/rollback state
// exists anywhere. A failed request applies nothing, with one exception: a
// 409 `changed` on an existing event carries the event as the server holds
// it, and `mutate` applies that before rethrowing (#992), so the tab shows the
// saved event rather than the stale copy it sent from. Callers surface the
// thrown EventApiError inline at the control that issued it.
//
// A mutation addresses the server by session ID alone, so a response that
// lands after a file import (#873; data/source.ts) would otherwise write the
// server's record into an imported session that happens to share the ID.
// Each mutation therefore captures the source (sourceGuard) before its
// request and applies nothing once it is stale, returning `{ stale: true }`
// instead. A caller that acts on the result after awaiting it captures its
// own sourceGuard before the call and passes the result through it: the
// switch may land after the mutation's check and before the caller resumes.
import type { EventCreateBody, EventRecord, EventUpdateBody } from "../api/export.gen";
import { useUiStore } from "../ui/uiStore";
import { EventApiError } from "./eventApiError";
import { EventChangedError } from "./eventChangedError";
import { storedEventStyle } from "./eventStyle";
import { storedEvent, useReviewStore } from "./reviewStore";
import { isCurrentSource, sourceGeneration } from "./source";
import { STREAM_FORMAT } from "./streamFormat";
import { type HistoryEntry, useUndoHistory } from "./undoHistory";

/** What a mutation resolves to when a source switch overtook it: the server
 * may have applied it, but the shell no longer shows that server. */
export interface StaleResult {
  stale: true;
}

export function isStale(r: unknown): r is StaleResult {
  return typeof r === "object" && r !== null && (r as { stale?: unknown }).stale === true;
}

const STALE: StaleResult = { stale: true };

/** Captures the current source for an async flow, before its first await.
 * The returned check is the flow's half of the stale contract: call it on a
 * result in the SAME continuation that acts on it. It passes the result
 * through while the captured source still holds, and turns it into a
 * StaleResult once a source switch has happened since the capture. Every
 * await is its own boundary: an import can land after a callee's own check
 * and before its caller resumes, so each layer that writes state after an
 * await re-checks with its own guard. */
export function sourceGuard(): <T>(result: T | StaleResult) => T | StaleResult {
  const generation = sourceGeneration();
  return (result) => (isCurrentSource(generation) ? result : STALE);
}

/** A rejection handler that posts "<action> failed: <message>" as a shell
 * warning, unless a source switch has happened since the handler was made.
 * Make it BEFORE starting the mutation: a failure from a server the shell no
 * longer shows is stale, not an error to paint over the imported file. */
export function reportMutationFailure(action: string): (err: unknown) => void {
  const current = sourceGuard();
  return (err) => {
    if (isStale(current(null))) return;
    const message = err instanceof Error ? err.message : String(err);
    useReviewStore.getState().actions.addWarning(`${action} failed: ${message}`);
  };
}

export type EventCreateInput = Omit<EventCreateBody, "timestamp" | "end_timestamp"> & {
  timestamp?: string;
  end_timestamp?: string;
};
/** `end_timestamp: null` (explicit) clears the end — span becomes a point. */
export type EventUpdateInput = EventUpdateBody;

async function request(path: string, init: RequestInit): Promise<Response> {
  let res: Response;
  try {
    res = await fetch(path, init);
  } catch (err) {
    throw new EventApiError(`Network error: ${String(err)}`, { cause: err });
  }
  if (!res.ok) throw await errorFor(res);
  return res;
}

interface ErrorBody {
  error?: unknown;
  detail?: unknown;
  code?: unknown;
  current?: unknown;
}

/** The error a non-2xx response stands for: its message, status, 409 code
 * and observed stamp; a well-formed `changed` 409 becomes an
 * EventChangedError. */
async function errorFor(res: Response): Promise<EventApiError> {
  const init = { status: res.status, observed: observedAt(res) };
  let body: ErrorBody = {};
  try {
    // Read via a clone: a Response's body can only be consumed once, and a
    // caller may reuse the same Response instance across multiple mocked
    // fetches (e.g. vi.fn().mockResolvedValue in tests) or otherwise still
    // want `res` intact after this helper returns.
    body = (await res.clone().json()) as ErrorBody;
  } catch {
    // fall through to the status-based message
  }
  const message = errorMessage(body, res.status);
  if (body.code === "changed") {
    // The server always sends `current`; null means the event is gone. A
    // missing key is a broken response, not a deletion, and a record goes
    // through the same check as a success's.
    if (!("current" in body)) {
      return new EventApiError("the server's conflict response has no current event", init);
    }
    try {
      const current = body.current === null ? null : storedRecord(body.current, res);
      return new EventChangedError(message, current, init);
    } catch (err) {
      return err as EventApiError;
    }
  }
  return new EventApiError(message, init);
}

function errorMessage(body: ErrorBody | null, status: number): string {
  if (typeof body?.error === "string") return body.error;
  // FastAPI body-validation failures arrive as {"detail": [{msg, ...}]}.
  if (typeof body?.detail === "string") return body.detail;
  if (Array.isArray(body?.detail)) {
    const msg = (body.detail[0] as { msg?: unknown } | undefined)?.msg;
    if (typeof msg === "string") return msg;
  }
  return `Request failed (${status})`;
}

/** The response header carrying the server's clock; OBSERVED_AT_HEADER in
 * src/otto/monitor/server.py names it on the other side, and
 * tests/unit/models/test_browser_format_mirrors.py holds the two equal. */
const OBSERVED_AT_HEADER = "X-Otto-Observed-At";

/** The server's time a mutation's response reports, for the fragment it
 * applies: the shared apply path raises the live horizon with it, so a mark
 * on a stalled collector shows at once even with the SSE echo withheld
 * (#866). Absent (review mode, an older server) means no observation. */
function observedAt(res: Response): string | null {
  return res.headers.get(OBSERVED_AT_HEADER);
}

/** The ONE success-application path: every mutation's result (and a lost
 * response's recovery) lands as a synthetic fragment through applyFragment's
 * upsert, or its deletion when `record` is null, stamped with the response's
 * observation so it orders against the SSE echo. */
function applyEventResult(
  sessionId: string,
  eventId: number,
  record: EventRecord | null,
  observed: string | null,
): void {
  useReviewStore.getState().actions.appendFragment({
    format: STREAM_FORMAT,
    session: sessionId,
    ...(record === null ? { deleted_event_ids: [eventId] } : { events: [record] }),
    observed_at: observed,
  });
}

const jsonInit = (method: string, body: unknown): RequestInit => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

const base = (sessionId: string) => `/api/session/${encodeURIComponent(sessionId)}/event`;

/** The existing event a mutation addresses. A create has none: it has no id
 * yet, so it cannot be refused as `changed`. */
interface MutationTarget {
  sessionId: string;
  eventId: number;
}

/** The one sequence every mutation shares: the source generation is captured
 * before the request, and the response applied only while it is still
 * current. A response, or a failure, that arrives after a source switch is
 * reported as stale: it belongs to a server the shell no longer shows.
 *
 * The ONE place a 409 `changed` reaches the store: for a `target`, the
 * refusal's `current` (the event as the server holds it, null when gone) is
 * applied through the success path, with the refusal's stamp, before the
 * error is rethrown to the caller. */
async function mutate<T>(
  send: () => Promise<T>,
  apply: (value: T) => void,
  onSuccess?: (value: T) => void,
  target?: MutationTarget,
): Promise<T | StaleResult> {
  const current = sourceGuard();
  let value: T;
  try {
    value = await send();
  } catch (err) {
    if (isStale(current(null))) return STALE;
    if (target !== undefined && err instanceof EventChangedError) {
      applyEventResult(target.sessionId, target.eventId, err.current, err.observed);
    }
    throw err;
  }
  const result = current(value);
  if (!isStale(result)) {
    apply(value);
    onSuccess?.(value);
  }
  return result;
}

/** An event record as a mutation's response carries it. The generated
 * `EventRecord` is the import boundary's type, where `id` may be absent (a
 * file row the collector numbers); a response is the server's stored record,
 * which always has one. */
type StoredRecord = EventRecord & { id: number };

/** The ONE check of a response's record: one without an id is a broken
 * response, refused loudly rather than filed under a made-up id. */
function storedRecord(body: unknown, res: Response): StoredRecord {
  const id = (body as { id?: unknown } | null)?.id;
  if (typeof id !== "number") {
    throw new EventApiError("the server's response has no event id", {
      status: res.status,
      observed: observedAt(res),
    });
  }
  return body as StoredRecord;
}

/** A response's record, with the stamp it was observed at. */
interface Recorded {
  record: StoredRecord;
  observed: string | null;
}

/** The ONE recording point of the undo history (#929): the onSuccess every
 * ordinary mutation hands to `mutate`. `states` turns the response's record
 * into the entry's `after` / `before` (null: nothing to record); what it
 * needs from the tab, like `trackedSpan`, was captured at SEND time. A
 * revert never records. */
function recorder(
  verb: HistoryEntry["verb"],
  sessionId: string,
  trackedSpan: boolean,
  states: (record: EventRecord) => Pick<HistoryEntry, "after" | "before"> | null,
): (value: Recorded) => void {
  return ({ record }) => {
    const legs = states(record);
    if (legs === null) return;
    useUndoHistory.getState().actions.record({
      verb,
      sessionId,
      eventId: record.id,
      label: record.label ?? "",
      ...legs,
      trackedSpan,
    });
  };
}

/** Whether the tab is tracking `(sessionId, eventId)` as its open span. */
function tracksSpan(sessionId: string, eventId: number): boolean {
  const span = useUiStore.getState().openSpan;
  return span?.sessionId === sessionId && span.eventId === eventId;
}

/** An edit's `states`: `after` is the response, `before` the same record
 * with only the SENT fields put back to their stored values at send time
 * (a field changed elsewhere meanwhile stays as the response has it), in
 * stored form so a legacy colour stays as stored. */
function editStates(
  sessionId: string,
  eventId: number,
  sent: readonly (keyof EventUpdateBody)[],
): (record: EventRecord) => Pick<HistoryEntry, "after" | "before"> | null {
  const held = storedEvent(sessionId, eventId);
  // Deliberate: with no stored row there is no `before` we can vouch for, and
  // an unrecorded edit is better than an undo that writes a guessed state.
  if (held === undefined) return () => null;
  const stored = storedEventStyle(held);
  const prior = Object.fromEntries(sent.map((k) => [k, stored[k] ?? null]));
  return (record) => ({ after: record, before: { ...record, ...prior } });
}

/** A mutation whose response body is the affected event record; `eventId`
 * names the existing event it addresses (none for a create). */
async function mutateRecord(
  sessionId: string,
  path: string,
  init: RequestInit,
  onSuccess: (value: Recorded) => void,
  eventId?: number,
): Promise<EventRecord | StaleResult> {
  const result = await mutate(
    async (): Promise<Recorded> => {
      const res = await request(path, init);
      return { record: storedRecord(await res.json(), res), observed: observedAt(res) };
    },
    ({ record, observed }) => applyEventResult(sessionId, record.id, record, observed),
    onSuccess,
    eventId === undefined ? undefined : { sessionId, eventId },
  );
  return isStale(result) ? result : result.record;
}

/** `opts.trackSpan`: the new event becomes the tab's tracked span (Start
 * span), which its history entry remembers. */
export function createEvent(
  sessionId: string,
  input: EventCreateInput,
  opts: { trackSpan?: boolean } = {},
): Promise<EventRecord | StaleResult> {
  return mutateRecord(
    sessionId,
    base(sessionId),
    jsonInit("POST", input),
    recorder("add", sessionId, opts.trackSpan ?? false, (record) => ({
      after: record,
      before: null,
    })),
  );
}

/**
 * Closes an open span event. Called by EventsPanel's per-row "End now"
 * action and by marking.ts's "End span" flow. Lives here with the rest of
 * the mutation client so the module matches the wire contract in one piece.
 */
export function endEvent(sessionId: string, eventId: number): Promise<EventRecord | StaleResult> {
  return mutateRecord(
    sessionId,
    `${base(sessionId)}/${eventId}/end`,
    jsonInit("POST", {}),
    recorder(
      "end",
      sessionId,
      tracksSpan(sessionId, eventId),
      editStates(sessionId, eventId, ["end_timestamp"]),
    ),
    eventId,
  );
}

export function updateEvent(
  sessionId: string,
  eventId: number,
  input: EventUpdateInput,
): Promise<EventRecord | StaleResult> {
  const sent = Object.keys(input) as (keyof EventUpdateBody)[];
  return mutateRecord(
    sessionId,
    `${base(sessionId)}/${eventId}`,
    jsonInit("PATCH", input),
    recorder(
      "edit",
      sessionId,
      tracksSpan(sessionId, eventId),
      editStates(sessionId, eventId, sent),
    ),
    eventId,
  );
}

/** Deletes the event and resolves to the record the server deleted, which
 * is also the history entry's `before`: the server's, not the tab's copy. */
export async function deleteEvent(
  sessionId: string,
  eventId: number,
): Promise<EventRecord | StaleResult> {
  const result = await mutate(
    async (): Promise<Recorded> => {
      const res = await request(`${base(sessionId)}/${eventId}`, { method: "DELETE" });
      return { record: storedRecord(await res.json(), res), observed: observedAt(res) };
    },
    ({ observed }) => applyEventResult(sessionId, eventId, null, observed),
    recorder("delete", sessionId, tracksSpan(sessionId, eventId), (record) => ({
      after: null,
      before: record,
    })),
    { sessionId, eventId },
  );
  return isStale(result) ? result : result.record;
}

/** Puts the event from `expect` to `write` in one server step, refused with
 * an EventChangedError (its `current` already applied) when it is no longer
 * `expect`. Both null-or-record:
 * (record, null) deletes, (null, record) restores. Resolves to the resulting
 * record, or null after a deletion. */
export async function revertEvent(
  sessionId: string,
  eventId: number,
  expect: EventRecord | null,
  write: EventRecord | null,
): Promise<EventRecord | null | StaleResult> {
  const result = await mutate(
    async () => {
      const res = await request(
        `${base(sessionId)}/${eventId}/revert`,
        jsonInit("POST", { expect, write }),
      );
      const body: unknown = await res.json();
      // null: the delete leg's answer, no record to check.
      const record = body === null ? null : storedRecord(body, res);
      return { record, observed: observedAt(res) };
    },
    ({ record, observed }) => applyEventResult(sessionId, eventId, record, observed),
    undefined,
    { sessionId, eventId },
  );
  return isStale(result) ? result : result.record;
}
