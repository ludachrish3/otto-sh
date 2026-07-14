import { beforeEach, describe, expect, it, vi } from "vitest";
import { useReviewStore } from "../data/reviewStore";
import { startStream } from "../data/stream";
import { synthSession } from "./_synth";

class FakeEventSource {
  static last: FakeEventSource | null = null;
  onmessage: ((e: MessageEvent<string>) => void) | null = null;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;
  constructor(public url: string) {
    FakeEventSource.last = this;
  }
  close() {
    this.closed = true;
  }
}

beforeEach(() => {
  vi.stubGlobal("EventSource", FakeEventSource as unknown as typeof EventSource);
  vi.useFakeTimers();
  // A real session with id "s" — the id every emitted fragment below targets —
  // not `sessions: []`. appendFragment/appendFragments are no-ops for a session
  // that isn't held (see reviewStore.ts), so with an empty sessions array every
  // fragment in the coalescing test below would be silently dropped and the
  // "ONE store update per flush" assertion would pass vacuously no matter how
  // many times the store was actually written to.
  useReviewStore.setState({
    sessions: [
      { ...synthSession({ hosts: 1, seriesPerHost: 1, ticks: 1, intervalS: 5 }), id: "s" },
    ],
    connection: "connecting",
  });
});

const emit = (payload: unknown) =>
  FakeEventSource.last?.onmessage?.({ data: JSON.stringify(payload) } as MessageEvent<string>);

describe("startStream", () => {
  it("coalesces many fragments into ONE store update per frame", () => {
    // Plan 5b final review, Finding C2: every field-empty fragment is
    // heartbeat-shaped, and applyFragment's no-op fast path (fragment.ts)
    // returns the SAME session object for one of those regardless of
    // whether stream.ts buffers or applies synchronously — so
    // mergeFragments never writes the store either way, and both
    // assertions below passed vacuously under a stream.ts with buffering
    // deleted entirely. Each of the 90 fragments now carries a REAL metric
    // (host h0/label m0 — the series `sessions[0]` above already holds one
    // sample for, so this is a genuine append, not a new series) with an
    // advancing timestamp, so every fragment is a real store write if
    // applied eagerly.
    const spy = vi.fn();
    const unsub = useReviewStore.subscribe(spy);
    const before = useReviewStore.getState().sessions[0].metrics.length;
    startStream();
    const baseMs = Date.parse("2026-07-12T00:00:05Z"); // one tick past synthSession's only sample
    for (let i = 0; i < 90; i++)
      emit({
        format: 1,
        session: "s",
        metrics: [
          {
            host: "h0",
            label: "m0",
            timestamp: new Date(baseMs + i * 1000).toISOString(),
            value: i,
          },
        ],
        events: [],
        log_events: [],
        deleted_event_ids: [],
        chart_map: {},
      });
    expect(spy).not.toHaveBeenCalled(); // still buffered
    expect(useReviewStore.getState().sessions[0].metrics.length).toBe(before); // not yet applied
    vi.advanceTimersByTime(20); // one frame
    expect(spy.mock.calls.length).toBeLessThanOrEqual(2); // one flush, not 90
    expect(useReviewStore.getState().sessions[0].metrics.length).toBe(before + 90); // all applied
    unsub();
  });

  it("marks the connection live on open and disconnected on error", () => {
    startStream();
    FakeEventSource.last?.onopen?.();
    expect(useReviewStore.getState().connection).toBe("live");
    FakeEventSource.last?.onerror?.();
    expect(useReviewStore.getState().connection).toBe("disconnected");
  });

  it("resyncs on reconnect instead of replaying missed deltas", async () => {
    const resync = vi.fn().mockResolvedValue(undefined);
    startStream({ resync });
    FakeEventSource.last?.onerror?.();
    await vi.advanceTimersByTimeAsync(1000); // first backoff
    expect(resync).toHaveBeenCalledTimes(1);
  });

  it("drops an invalid fragment without killing the stream", () => {
    startStream();
    expect(() => emit({ nonsense: true })).not.toThrow();
    FakeEventSource.last?.onopen?.();
    expect(useReviewStore.getState().connection).toBe("live");
  });

  it("drops a fragment whose metrics field isn't an array, instead of corrupting the session", () => {
    startStream();
    const before = useReviewStore.getState().sessions[0].metrics.length;
    // A backend bug shape: `metrics` present but not an array. Passing this
    // structural check through would have applyFragment do
    // `session.metrics.push(..."not an array")`, which spreads a STRING —
    // pushing individual characters as if they were MetricRecords.
    expect(() =>
      emit({
        format: 1,
        session: "s",
        metrics: "not an array",
        events: [],
        log_events: [],
        deleted_event_ids: [],
        chart_map: {},
      }),
    ).not.toThrow();
    vi.advanceTimersByTime(20); // one frame — flush would run if buffered
    expect(useReviewStore.getState().sessions[0].metrics.length).toBe(before);
  });

  // Plan 5b follow-up #6: stop() left the reconnect backoff timer (scheduled
  // by onerror below) untracked, so it fired anyway after stop() — a mode
  // switch or HMR teardown leaked both a live EventSource-reconnect cycle
  // AND a hydrate that could overwrite the store after the caller believed
  // the stream was dead. Latent in production today (nothing stops the
  // stream yet), but real for HMR and any future mode-switch teardown.
  describe("stop()", () => {
    it("cancels a pending reconnect timer — leaves no timer scheduled", () => {
      const stop = startStream();
      FakeEventSource.last?.onerror?.(); // schedules the backoff-delayed reconnect
      expect(vi.getTimerCount()).toBeGreaterThan(0); // the reconnect timer is pending
      stop();
      expect(vi.getTimerCount()).toBe(0); // no leaked timer after stop()
    });

    it("a stopped stream performs no hydrate, even once every backoff tier has elapsed", async () => {
      const resync = vi.fn().mockResolvedValue(undefined);
      const stop = startStream({ resync });
      FakeEventSource.last?.onerror?.(); // schedules the first backoff-delayed reconnect
      stop(); // stop BEFORE the backoff elapses
      // Exhaust every backoff tier (1000+2000+5000+10000+30000ms) — if the
      // timer had survived stop(), or the reconnect path hydrated before
      // checking `stopped`, resync would fire somewhere in this window.
      await vi.advanceTimersByTimeAsync(60_000);
      expect(resync).not.toHaveBeenCalled();
    });
  });
});
