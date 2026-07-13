// The SSE client: buffer, flush once per frame, reconnect with backoff, resync.
import type { MonitorSessionFragment } from "../api/export.gen";
import { useReviewStore } from "./reviewStore";

const FLUSH_MS = 16; // one frame
const BACKOFF_MS = [1000, 2000, 5000, 10_000, 30_000];

// The fragment's array-typed fields (metrics/events/log_events/deleted_event_ids) —
// see MonitorSessionFragment in export.gen.ts. Checked below so a fragment carrying
// e.g. `metrics: "not an array"` is dropped rather than passed through to
// applyFragment, which spreads them directly (`push(...metrics)`) with no further
// validation — a string there would fan out into individual pushed characters.
const ARRAY_FIELDS = ["metrics", "events", "log_events", "deleted_event_ids"] as const;

/** Structural check. A malformed fragment is dropped, never fatal — one bad frame
 * must not take down a running monitor. */
function isFragment(v: unknown): v is MonitorSessionFragment {
  if (typeof v !== "object" || v === null) return false;
  const rec = v as Record<string, unknown>;
  if (typeof rec.session !== "string") return false;
  return ARRAY_FIELDS.every((key) => rec[key] === undefined || Array.isArray(rec[key]));
}

export function startStream(
  opts: { url?: string; resync?: () => Promise<unknown> } = {},
): () => void {
  const url = opts.url ?? "/api/stream";
  let source: EventSource | null = null;
  let stopped = false;
  let attempt = 0;
  let buffer: MonitorSessionFragment[] = [];
  let timer: ReturnType<typeof setTimeout> | null = null;

  // ~90 fragments arrive per tick (one per point). Applying each separately would
  // be ~90 store updates and ~90 render passes; one flush per frame makes it one.
  // Calling the singular appendFragment 90 times in a loop would NOT achieve this —
  // each call does its own internal set(), so it'd still be 90 notifications, just
  // deferred to the same tick. appendFragments folds the whole batch into one set().
  const flush = () => {
    timer = null;
    const batch = buffer;
    buffer = [];
    useReviewStore.getState().actions.appendFragments(batch);
  };

  const connect = () => {
    if (stopped) return;
    // zustand's vanilla setState notifies subscribers on every call, even when the
    // value doesn't change (it bails only on reference-equal partials, never on
    // value equality — see reviewStore.ts's setConnection). Both branches below are
    // already true by the time connect() runs (the store inits to "connecting", and
    // onerror already set "disconnected" before scheduling this retry), so setting
    // them again would fire a spurious update ahead of the very first buffered
    // fragment. Only write when the value would actually change.
    const target = attempt === 0 ? "connecting" : "disconnected";
    if (useReviewStore.getState().connection !== target) {
      useReviewStore.getState().actions.setConnection(target);
    }
    source = new EventSource(url);

    source.onopen = () => {
      attempt = 0;
      useReviewStore.getState().actions.setConnection("live");
    };

    source.onmessage = (e: MessageEvent<string>) => {
      let parsed: unknown;
      try {
        parsed = JSON.parse(e.data);
      } catch {
        return; // not JSON — drop it
      }
      if (!isFragment(parsed)) return;
      buffer.push(parsed);
      if (timer === null) timer = setTimeout(flush, FLUSH_MS);
    };

    source.onerror = () => {
      source?.close();
      source = null;
      useReviewStore.getState().actions.setConnection("disconnected");
      if (stopped) return;
      const delay = BACKOFF_MS[Math.min(attempt, BACKOFF_MS.length - 1)];
      attempt += 1;
      setTimeout(() => {
        // Resync BEFORE reopening: re-fetch the whole payload rather than trying to
        // replay what we missed. The snapshot is the truth and already contains it —
        // no sequence numbers, no replay buffer, no way to disagree about history.
        void (opts.resync?.() ?? Promise.resolve()).finally(connect);
      }, delay);
    };
  };

  connect();

  return () => {
    stopped = true;
    if (timer !== null) clearTimeout(timer);
    source?.close();
  };
}
