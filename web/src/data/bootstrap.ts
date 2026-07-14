// The shell's ONLY boot fetch. On mount, ask a same-origin otto monitor
// server which mode it's running in (`otto monitor <source>`, the
// positional review path over a saved .json/.db export; or `otto monitor
// --live`, the live-collecting path) and hydrate the review store from
// `/api/monitor_sessions` exactly as the Import front door would — the
// live server serves a snapshot of the running session, so this is valid
// in both modes. In live mode, if that hydrate actually succeeded, also
// attach the SSE stream (data/stream.ts) so further points arrive without a
// page reload; on every reconnect the stream re-fetches the whole snapshot
// (a resync, never a delta replay — see stream.ts's header) via `resync`
// below, a sibling of `hydrate` that replaces sessions/rawMonitorSessions/
// warnings the same way but — unlike a fresh hydrate/Import — leaves
// range/activeSessionId untouched, so a transient reconnect can't silently
// discard a user's paused/pinned view (Plan 5b final review, Finding I3). A
// live server with nothing recording yet (e.g. shell_dash's bare harness,
// or the docs-capture script) 404s the hydrate — opening a stream against
// it would sit idle forever, and shell_dash's own contract is "no boot-time
// API calls" for exactly that case, so the stream stays closed too.
//
// Soft-fail contract (binding): the built dist/ is also served by dumb
// static file servers with no /api/* routes at all (the docs-capture
// script, ad-hoc demo serving), and the offline Playwright pin blocks
// every non-local request outright. Both depend on this module leaving the
// shell exactly as it behaves without it whenever a fetch can't succeed.
// So ANY transport failure — a rejected fetch, a non-200 response, or a
// body that isn't JSON/text — is swallowed and returns silently, before
// importMonitorSessions/resyncMonitorSessions is ever called. Either one
// only ever sees a 200 `/api/monitor_sessions` body; ITS validation
// failures are not swallowed — they surface through the store's existing
// `importError`, the same as a bad file chosen through Import.

import { useReviewStore } from "./reviewStore";
import { startStream } from "./stream";

interface ModePayload {
  mode: "live" | "review";
  source: string | null;
}

function isModePayload(value: unknown): value is ModePayload {
  if (typeof value !== "object" || value === null) return false;
  const rec = value as Record<string, unknown>;
  return (
    (rec.mode === "live" || rec.mode === "review") &&
    (typeof rec.source === "string" || rec.source === null)
  );
}

export async function bootstrapFromServer(): Promise<void> {
  let modeRes: Response;
  try {
    modeRes = await fetch("/api/mode");
  } catch {
    return;
  }
  if (!modeRes.ok) return;
  let modeBody: unknown;
  try {
    modeBody = await modeRes.json();
  } catch {
    return;
  }
  if (!isModePayload(modeBody)) return;

  // Same soft-fail rules as the mode fetch above: a transport failure here
  // is swallowed silently too (the Import shell stays exactly as it was);
  // only a 200-but-unparseable body reaches the store and its existing
  // importError surface. Shared by `hydrate` (the initial load) and
  // `resync` (the stream's resync-on-reconnect) below.
  const fetchDocumentText = async (): Promise<string | null> => {
    let docRes: Response;
    try {
      docRes = await fetch("/api/monitor_sessions");
    } catch {
      return null;
    }
    if (!docRes.ok) return null;
    try {
      return await docRes.text();
    } catch {
      return null;
    }
  };

  // The initial boot load. Fresh state: sessions[0] becomes active and any
  // view (range) resets, exactly like a user-chosen Import. Returns whether
  // the fetch actually reached the store (true) or bailed out early on a
  // transport failure (false) — used below to decide whether starting the
  // stream is even worthwhile.
  const hydrate = async (): Promise<boolean> => {
    const bodyText = await fetchDocumentText();
    if (bodyText === null) return false;
    useReviewStore.getState().actions.importMonitorSessions(bodyText, modeBody.source ?? "server");
    return true;
  };

  // The stream's resync-on-reconnect (data/stream.ts's onerror -> resync ->
  // reopen). Unlike `hydrate` above, this must NOT reset `range`/
  // `activeSessionId` — a transient network blip resyncing the whole
  // snapshot must not silently discard a user's paused/pinned view (Plan 5b
  // final review, Finding I3; see reviewStore.ts's resyncMonitorSessions).
  const resync = async (): Promise<boolean> => {
    const bodyText = await fetchDocumentText();
    if (bodyText === null) return false;
    useReviewStore.getState().actions.resyncMonitorSessions(bodyText, modeBody.source ?? "server");
    return true;
  };

  const hydrated = await hydrate();

  // The shell is LIVE only if it actually hydrated a live session and
  // started streaming — never before knowing that. A live server with
  // nothing recording yet (e.g. shell_dash's bare harness) 404s the
  // hydrate above; setting mode="live" regardless would make a shell that
  // never got any data claim it's live (hiding the review bar, reading
  // "Reconnecting…" instead of "No data", etc.) when it is, in truth, the
  // same Import/static state as any other empty shell — see Finding 1,
  // Plan 5b Task 9 review. Review mode carries no such ambiguity (a
  // review-mode server always already holds the document it announced),
  // so it's set unconditionally as before.
  if (modeBody.mode === "live") {
    if (hydrated) {
      useReviewStore.getState().actions.setMode("live");
      // Known, undocumented-no-longer gap (Plan 5b follow-ups #4): any point
      // the server publishes strictly between `hydrate()`'s response above
      // and this `startStream` call's `new EventSource(url)` actually
      // opening (stream.ts's `connect`) is neither in that response NOR
      // replayed by the stream — the stream only carries what's published
      // AFTER it opens, and the snapshot only carries what existed as of
      // its own request. The window is a handful of event-loop turns, and
      // `resync` (below, stream.ts's onerror -> resync -> reopen) reopens
      // through this exact same shape on every reconnect, not just at boot.
      // Small and real, not zero: the spec's "provably correct" framing
      // overstated it. Left open — closing it needs either a
      // sequence-numbered replay buffer or a shared server-side cursor
      // between the snapshot fetch and the stream open, neither of which
      // exists today.
      startStream({ resync });
    }
  } else {
    useReviewStore.getState().actions.setMode(modeBody.mode);
  }
}
