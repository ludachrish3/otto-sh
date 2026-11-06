// web/src/data/eventApi.ts
// The dashboard's ONLY mutation surface. One rule for both modes:
// every 2xx response is applied locally as a synthetic fragment through
// applyFragment's existing upsert — in live mode the SSE echo then delivers
// the same record again and the upsert-by-id makes it a no-op, so ordering
// between response and echo cannot matter and no optimistic/rollback state
// exists anywhere. A failed request applies nothing; callers surface the
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
import { useReviewStore } from "./reviewStore";
import { isCurrentSource, sourceGeneration } from "./source";
import { STREAM_FORMAT } from "./streamFormat";

export class EventApiError extends Error {}

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
  if (!res.ok) throw new EventApiError(await errorMessage(res));
  return res;
}

async function errorMessage(res: Response): Promise<string> {
  const fallback = `Request failed (${res.status})`;
  try {
    // Read via a clone: a Response's body can only be consumed once, and a
    // caller may reuse the same Response instance across multiple mocked
    // fetches (e.g. vi.fn().mockResolvedValue in tests) or otherwise still
    // want `res` intact after this helper returns.
    const body = (await res.clone().json()) as { error?: unknown; detail?: unknown };
    if (typeof body.error === "string") return body.error;
    // FastAPI body-validation failures arrive as {"detail": [{msg, ...}]}.
    if (typeof body.detail === "string") return body.detail;
    if (Array.isArray(body.detail)) {
      const msg = (body.detail[0] as { msg?: unknown } | undefined)?.msg;
      if (typeof msg === "string") return msg;
    }
  } catch {
    // fall through to the status-based message
  }
  return fallback;
}

function applyRecord(sessionId: string, record: EventRecord): void {
  useReviewStore
    .getState()
    .actions.appendFragment({ format: STREAM_FORMAT, session: sessionId, events: [record] });
}

const jsonInit = (method: string, body: unknown): RequestInit => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

const base = (sessionId: string) => `/api/session/${encodeURIComponent(sessionId)}/event`;

/** The one sequence every mutation shares: the source generation is captured
 * before the request, and the response applied only while it is still
 * current. A response, or a failure, that arrives after a source switch is
 * reported as stale: it belongs to a server the shell no longer shows. */
async function mutate<T>(
  send: () => Promise<T>,
  apply: (value: T) => void,
): Promise<T | StaleResult> {
  const current = sourceGuard();
  let value: T;
  try {
    value = await send();
  } catch (err) {
    if (isStale(current(null))) return STALE;
    throw err;
  }
  const result = current(value);
  if (!isStale(result)) apply(value);
  return result;
}

/** A mutation whose response body is the affected event record. */
function mutateRecord(
  sessionId: string,
  path: string,
  init: RequestInit,
): Promise<EventRecord | StaleResult> {
  return mutate(
    async () => (await (await request(path, init)).json()) as EventRecord,
    (record) => applyRecord(sessionId, record),
  );
}

export function createEvent(
  sessionId: string,
  input: EventCreateInput,
): Promise<EventRecord | StaleResult> {
  return mutateRecord(sessionId, base(sessionId), jsonInit("POST", input));
}

/**
 * Closes an open span event. Called by EventsPanel's per-row "End now"
 * action and by marking.ts's "End span" flow. Lives here with the rest of
 * the mutation client so the module matches the wire contract in one piece.
 */
export function endEvent(sessionId: string, eventId: number): Promise<EventRecord | StaleResult> {
  return mutateRecord(sessionId, `${base(sessionId)}/${eventId}/end`, jsonInit("POST", {}));
}

export function updateEvent(
  sessionId: string,
  eventId: number,
  input: EventUpdateInput,
): Promise<EventRecord | StaleResult> {
  return mutateRecord(sessionId, `${base(sessionId)}/${eventId}`, jsonInit("PATCH", input));
}

export function deleteEvent(sessionId: string, eventId: number): Promise<undefined | StaleResult> {
  return mutate(
    () => request(`${base(sessionId)}/${eventId}`, { method: "DELETE" }).then(() => undefined),
    () =>
      useReviewStore.getState().actions.appendFragment({
        format: STREAM_FORMAT,
        session: sessionId,
        deleted_event_ids: [eventId],
      }),
  );
}
