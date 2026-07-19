// web/src/data/eventApi.ts
// The dashboard's ONLY mutation surface (Plan 5c). One rule for both modes:
// every 2xx response is applied locally as a synthetic fragment through
// applyFragment's existing upsert — in live mode the SSE echo then delivers
// the same record again and the upsert-by-id makes it a no-op, so ordering
// between response and echo cannot matter and no optimistic/rollback state
// exists anywhere. A failed request applies nothing; callers surface the
// thrown EventApiError inline at the control that issued it.
import type { EventCreateBody, EventRecord, EventUpdateBody } from "../api/export.gen";
import { useReviewStore } from "./reviewStore";

export class EventApiError extends Error {}

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
    throw new EventApiError(`Network error: ${String(err)}`);
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
    .actions.appendFragment({ format: 1, session: sessionId, events: [record] });
}

const jsonInit = (method: string, body: unknown): RequestInit => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

const base = (sessionId: string) => `/api/session/${encodeURIComponent(sessionId)}/event`;

export async function createEvent(
  sessionId: string,
  input: EventCreateInput,
): Promise<EventRecord> {
  const res = await request(base(sessionId), jsonInit("POST", input));
  const record = (await res.json()) as EventRecord;
  applyRecord(sessionId, record);
  return record;
}

/**
 * Not yet wired to any control in this task — Task 10 (EventsPanel's
 * "End now" button) is its first caller. Part of the mutation client's
 * full surface per Task 6's spec; kept here rather than split across
 * tasks so the module matches the wire contract in one piece.
 */
export async function endEvent(sessionId: string, eventId: number): Promise<EventRecord> {
  const res = await request(`${base(sessionId)}/${eventId}/end`, jsonInit("POST", {}));
  const record = (await res.json()) as EventRecord;
  applyRecord(sessionId, record);
  return record;
}

export async function updateEvent(
  sessionId: string,
  eventId: number,
  input: EventUpdateInput,
): Promise<EventRecord> {
  const res = await request(`${base(sessionId)}/${eventId}`, jsonInit("PATCH", input));
  const record = (await res.json()) as EventRecord;
  applyRecord(sessionId, record);
  return record;
}

export async function deleteEvent(sessionId: string, eventId: number): Promise<void> {
  await request(`${base(sessionId)}/${eventId}`, { method: "DELETE" });
  useReviewStore
    .getState()
    .actions.appendFragment({ format: 1, session: sessionId, deleted_event_ids: [eventId] });
}
