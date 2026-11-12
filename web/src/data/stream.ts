// The SSE client: buffer, flush once per frame, reconnect with backoff, and
// report every open so the caller can resync (data/bootstrap.ts wires it to
// data/source.ts's syncFromServer).
//
// The stream carries two kinds of frame: per-session fragments (the
// `MonitorSessionFragment` deltas) and document-level source frames
// (`MonitorSourceFrame`: a source switch, or new unsaved/revision counts).
// Both keep their arrival order.
//
// Nothing published is lost across a connection. The server subscribes a
// connection before its first byte, so by the time `onOpen` fires every
// later publish reaches this client; the caller's sync then fetches the
// snapshot, and the store replays every push stamped after the snapshot's
// capture onto it (reviewStore.ts's installSnapshot). A push the snapshot
// already holds is stamped at or before the capture, and is dropped.
import type { MonitorSessionFragment, MonitorSourceState } from "../api/export.gen";
import { useReviewStore } from "./reviewStore";
import { isSourceState } from "./sourceApi";
import { MONITOR_STREAM_READ_VERSIONS } from "./streamFormat";

const FLUSH_MS = 16; // one frame
const BACKOFF_MS = [1000, 2000, 5000, 10_000, 30_000];

// The fragment's array-typed fields (metrics/events/log_events/deleted_event_ids) —
// see MonitorSessionFragment in export.gen.ts. Checked below so a fragment carrying
// e.g. `metrics: "not an array"` is dropped rather than passed through to
// applyFragment, which iterates them directly (`for (const m of metrics)`) with no
// further validation — a string there would fan out into individual pushed characters.
const ARRAY_FIELDS = ["metrics", "events", "log_events", "deleted_event_ids"] as const;

/** Structural check. A malformed fragment is dropped, never fatal — one bad frame
 * must not take down a running monitor — and is lost (`lost` in startStream). */
function isFragment(v: unknown): v is MonitorSessionFragment {
  if (typeof v !== "object" || v === null) return false;
  const rec = v as Record<string, unknown>;
  if (typeof rec["session"] !== "string") return false;
  return ARRAY_FIELDS.every((key) => rec[key] === undefined || Array.isArray(rec[key]));
}

/** A source frame's state: a frame that names no session and carries a
 * `source_state` object. Null for anything else; a malformed state is
 * dropped like a malformed fragment, and lost alike. */
function sourceFrameState(v: unknown): MonitorSourceState | null {
  if (typeof v !== "object" || v === null) return null;
  const rec = v as Record<string, unknown>;
  if (rec["session"] !== undefined) return null;
  const state = rec["source_state"];
  return isSourceState(state) ? state : null;
}

/** Whether this dashboard reads the frame's `format`: one in the declared read
 * list (streamFormat.ts), or none at all, as the Python model accepts it (its
 * `format` has a default). */
function isReadable(frame: { format?: unknown }): boolean {
  return (
    frame.format === undefined ||
    (MONITOR_STREAM_READ_VERSIONS as readonly unknown[]).includes(frame.format)
  );
}

/** One buffered frame, in arrival order. */
type Buffered = { fragment: MonitorSessionFragment } | { source: MonitorSourceState };

/** What `startStream` hands back: the stream's controls. */
export interface StreamControl {
  /** Closes the stream for good: no reconnect, no pending flush. */
  stop: () => void;
  /** Drops the open connection at once and reconnects through the same
   * backoff as a lost one, so the new connection's `onOpen` runs again. The
   * one retry of a sync that could not install (data/source.ts), and the
   * stream's own recovery from a frame it lost (`lost`): no second loop. A
   * no-op once stopped, before the first open, or while a reconnect is
   * already scheduled. */
  reconnect: () => void;
  /** The stream's ONE handling of a fragment the tab did not apply (see
   * `lost` in startStream), for a loss found outside the stream itself: a
   * snapshot install whose replay could not apply a logged fragment
   * (data/source.ts's finishInstall). */
  lost: (how: { recover: boolean }) => void;
}

/** The one warning for a connection whose fragments arrive in a format this
 * dashboard does not read, such as a tab left open across an upgrade that bumped
 * only the stream's version: its resync still reads, so it would look live while
 * every update is dropped. */
function unreadableFormatWarning(format: unknown, dropped: number): string {
  return (
    `The live stream sent ${dropped} update(s) in format ${String(format)}, but this ` +
    `dashboard reads format ${MONITOR_STREAM_READ_VERSIONS.join(", ")}. Live updates are ` +
    "being dropped; reload the page to get a dashboard that matches the server."
  );
}

/**
 * Opens the stream and returns its controls (`StreamControl`): `stop`,
 * `reconnect`, the caller's retry, and `lost`, a loss the caller found.
 *  - `onOpen` runs on every open, the first and each reconnect's, once the
 *    new connection is subscribed. It may return a promise of whether the
 *    open is healthy (the caller's resync on it installed); only a healthy
 *    connection that loses no frame starts the backoff over, when it ends
 *    (`clean`). The promise must not
 *    reject (data/source.ts's resyncOnOpen resolves false instead).
 *    Returning nothing means the open itself is enough.
 *  - `onFirstError` runs when the stream fails before it EVER opened (a
 *    static server with no `/api/stream`): the stream stops itself, with no
 *    reconnect.
 *  - `onSource` receives each source frame's state, in arrival order with
 *    the fragments around it.
 * A later error reconnects with backoff, and so does `reconnect()`. The
 * backoff grows across every reconnect until a connection that opened
 * healthy and lost no frame ends (`clean`), so a caller whose resync keeps
 * failing retries at 1, 2, 5, 10, then every 30 s, and so does a server
 * that keeps sending what the tab cannot apply; a drop of a clean connection
 * waits the first step again.
 */
export function startStream(
  opts: {
    url?: string;
    onOpen?: () => Promise<boolean> | undefined;
    onFirstError?: () => void;
    onSource?: (state: MonitorSourceState) => void;
  } = {},
): StreamControl {
  const url = opts.url ?? "/api/stream";
  let source: EventSource | null = null;
  let stopped = false;
  let opened = false;
  let attempt = 0;
  // The current connection's record for the backoff: whether its open was
  // healthy, and whether it lost a frame (`lost`). Only a connection that
  // was healthy and lost nothing resets the backoff, once it ends (`failed`):
  // a loss is found after the open's resync installed, so resetting at the
  // open would retry a server that keeps sending what the tab cannot apply
  // at the first step forever.
  let healthy = false;
  let lossSeen = false;
  let buffer: Buffered[] = [];
  let timer: ReturnType<typeof setTimeout> | null = null;
  // The reconnect backoff timer scheduled by onerror below. Tracked
  // separately from `timer` (the per-frame flush timer) so stop() can cancel
  // BOTH kinds of pending work — leaving this one untracked was the bug: a
  // stopped stream's already-scheduled reconnect fired anyway.
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  // Fragments dropped for an unreadable `format`: counted until, and surfaced ONCE
  // per connection in the warnings channel at, the next flush — not once per fragment.
  // Reset by connect(), because the resync on each reconnect replaces the
  // warnings channel wholesale.
  let unreadable = 0;
  let unreadableFormat: unknown;
  let warnedUnreadable = false;

  // ~90 fragments arrive per tick (one per point). Applying each separately would
  // be ~90 store updates and ~90 render passes; one flush per frame makes it one.
  // Calling the singular appendFragment 90 times in a loop would NOT achieve this —
  // each call does its own internal set(), so it'd still be 90 notifications, just
  // deferred to the same tick. appendFragments folds the whole batch into one set().
  // A source frame splits the batch: the fragments before it apply first, then
  // the frame, then the rest, so the two kinds keep their arrival order. When
  // a run holds a fragment the store could not apply, that one is lost (the
  // run's fragments for held sessions still apply), and what follows the run
  // lies after the gap: the later source frames and runs are not applied
  // (the reconnect's resync heals them).
  const flush = () => {
    timer = null;
    const batch = buffer;
    buffer = [];
    const { actions } = useReviewStore.getState();
    const applied = (frags: MonitorSessionFragment[]) =>
      frags.length === 0 || actions.appendFragments(frags);
    let run: MonitorSessionFragment[] = [];
    let gap = false;
    for (const item of batch) {
      if ("fragment" in item) {
        run.push(item.fragment);
        continue;
      }
      if (!applied(run)) {
        gap = true;
        break;
      }
      run = [];
      opts.onSource?.(item.source);
    }
    if (gap || !applied(run)) lost({ recover: true });
    if (unreadable > 0 && !warnedUnreadable) {
      warnedUnreadable = true;
      useReviewStore
        .getState()
        .actions.addWarning(unreadableFormatWarning(unreadableFormat, unreadable));
    }
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
    unreadable = 0;
    warnedUnreadable = false;
    healthy = false;
    lossSeen = false;
    try {
      source = new EventSource(url);
    } catch {
      // A constructor that throws (a refused URL; no EventSource at all) is
      // a transport failure like any other: the same path as an error event.
      failed();
      return;
    }

    const connection = source;
    source.onopen = () => {
      opened = true;
      useReviewStore.getState().actions.setConnection("live");
      const answer = opts.onOpen?.();
      if (answer === undefined) {
        markHealthy(connection);
        return;
      }
      void answer.then((ok) => {
        if (ok) markHealthy(connection);
      });
    };

    source.onmessage = (e: MessageEvent<string>) => {
      // A connection this stream has dropped delivers nothing more: what it
      // still had queued lies after the gap its successor's resync heals.
      if (source !== connection) return;
      let parsed: unknown;
      try {
        parsed = JSON.parse(e.data);
      } catch {
        lost({ recover: true }); // not JSON
        return;
      }
      const state = sourceFrameState(parsed);
      let item: Buffered;
      if (state !== null) item = { source: state };
      else if (isFragment(parsed)) item = { fragment: parsed };
      else {
        lost({ recover: true });
        return;
      }
      const frame = parsed as { format?: unknown };
      if (isReadable(frame)) {
        buffer.push(item);
      } else {
        lost({ recover: false });
        if (!warnedUnreadable) {
          // Counted only until the one warning goes out, so the count it states is
          // exactly what it counted; later unreadable fragments are dropped uncounted.
          if (unreadable === 0) unreadableFormat = frame.format;
          unreadable += 1;
        }
      }
      if (timer === null) timer = setTimeout(flush, FLUSH_MS);
    };

    source.onerror = failed;
  };

  /** A connection failed: before the stream ever opened, no stream is
   * served here (a static server), so retrying would only fail again; stop,
   * and say so once. After it opened, reconnect with backoff. Reconnect only:
   * the resync runs on the new connection's open (`onOpen`), once it is
   * subscribed, so nothing published in between is lost. */
  function failed(): void {
    source?.close();
    source = null;
    if (!opened) {
      stop();
      opts.onFirstError?.();
      return;
    }
    useReviewStore.getState().actions.setConnection("disconnected");
    if (stopped) return;
    if (clean()) attempt = 0;
    const delay = BACKOFF_MS[Math.min(attempt, BACKOFF_MS.length - 1)];
    attempt += 1;
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      connect();
    }, delay);
  }

  /** `connection` opened healthy, if it is still the stream's connection (a
   * reconnect since then keeps backing off). */
  function markHealthy(connection: EventSource): void {
    if (source === connection) healthy = true;
  }

  /** The ONE test of whether the ending connection resets the backoff: it
   * opened healthy and lost no frame. */
  function clean(): boolean {
    return healthy && !lossSeen;
  }

  function stop(): void {
    stopped = true;
    if (timer !== null) clearTimeout(timer);
    if (reconnectTimer !== null) clearTimeout(reconnectTimer);
    source?.close();
  }

  function reconnect(): void {
    if (stopped || !opened || reconnectTimer !== null) return;
    failed();
  }

  /** The ONE handling of a frame the tab did not apply: one it could not
   * read (not JSON, or neither a fragment nor a source frame), one in a
   * format this dashboard does not read, or a fragment for a session it
   * does not hold (reviewStore.ts's appendFragments). Whatever write it
   * carried is missing here, so no count after it may be acknowledged until
   * an install (reviewStore.ts's `withholdCoverage`). `recover` forces the
   * reconnect, the ONE recovery: the dropped connection delivers nothing
   * more, and the new one's open resyncs. A frame in an unreadable format
   * does not recover: a reconnect would only read the same format again
   * (its warning says to reload instead), so the acknowledgement stays
   * withheld until an install for another reason. */
  function lost(how: { recover: boolean }): void {
    lossSeen = true;
    useReviewStore.getState().actions.withholdCoverage();
    if (how.recover) reconnect();
  }

  connect();
  return { stop, reconnect, lost };
}
