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
  url: string;
  constructor(url: string) {
    this.url = url;
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

  it("reconnects with backoff and reports each open, the reconnect's included", async () => {
    const onOpen = vi.fn();
    startStream({ onOpen });
    const first = FakeEventSource.last;
    first?.onopen?.();
    expect(onOpen).toHaveBeenCalledTimes(1);
    first?.onerror?.();
    expect(first?.closed).toBe(true);
    await vi.advanceTimersByTimeAsync(1000); // first backoff
    expect(FakeEventSource.last).not.toBe(first); // resubscribed
    expect(onOpen).toHaveBeenCalledTimes(1); // not before the new connection opens
    FakeEventSource.last?.onopen?.();
    expect(onOpen).toHaveBeenCalledTimes(2);
  });

  it("an error before the first open stops the stream: no reconnect", async () => {
    const onFirstError = vi.fn();
    startStream({ onFirstError });
    const first = FakeEventSource.last;
    first?.onerror?.();
    expect(onFirstError).toHaveBeenCalledTimes(1);
    expect(first?.closed).toBe(true);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(FakeEventSource.last).toBe(first);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("a constructor that throws is a failure before the first open, not an exception", () => {
    vi.stubGlobal("EventSource", undefined);
    const onFirstError = vi.fn();
    expect(() => startStream({ onFirstError })).not.toThrow();
    expect(onFirstError).toHaveBeenCalledTimes(1);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("source frames are delivered in arrival order between fragments", () => {
    const order: string[] = [];
    const actions = useReviewStore.getState().actions;
    const append = vi.spyOn(actions, "appendFragments").mockImplementation((frags) => {
      order.push(`fragments:${frags.map((f) => f.observed_at).join(",")}`);
      return true;
    });
    const state = (token: string) => ({
      mode: "review",
      kind: "json",
      source: `${token}.json`,
      token,
      editable: true,
      unsaved: 0,
      revision: 0,
      observed_at: `2026-07-12T00:00:0${token === "a" ? 2 : 4}Z`,
    });
    startStream({ onSource: (s) => order.push(`source:${s.token}`) });
    FakeEventSource.last?.onopen?.();
    const frag = (t: string) => ({ format: 1, session: "s", observed_at: t });
    emit(frag("t1"));
    emit({ format: 1, source_state: state("a"), observed_at: state("a").observed_at });
    emit(frag("t3"));
    emit(frag("t3b"));
    emit({ format: 1, source_state: state("b"), observed_at: state("b").observed_at });
    // Neither a session-addressed frame nor a valid source state: dropped.
    emit({ format: 1, source_state: { token: "c" } });
    vi.advanceTimersByTime(20); // one frame
    expect(order).toEqual(["fragments:t1", "source:a", "fragments:t3,t3b", "source:b"]);
    append.mockRestore();
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
      const { stop } = startStream();
      FakeEventSource.last?.onopen?.();
      FakeEventSource.last?.onerror?.(); // schedules the backoff-delayed reconnect
      expect(vi.getTimerCount()).toBeGreaterThan(0); // the reconnect timer is pending
      stop();
      expect(vi.getTimerCount()).toBe(0); // no leaked timer after stop()
    });

    it("a stopped stream never reconnects, even once every backoff tier has elapsed", async () => {
      const { stop } = startStream();
      const first = FakeEventSource.last;
      first?.onopen?.();
      first?.onerror?.(); // schedules the first backoff-delayed reconnect
      stop(); // stop BEFORE the backoff elapses
      // Exhaust every backoff tier (1000+2000+5000+10000+30000ms) — if the
      // timer had survived stop(), a new EventSource would open in this window.
      await vi.advanceTimersByTimeAsync(60_000);
      expect(FakeEventSource.last).toBe(first);
    });
  });

  // The caller's retry of a sync that could not install (data/source.ts).
  describe("reconnect()", () => {
    it("drops the open connection at once, and the new connection's open runs onOpen", async () => {
      const onOpen = vi.fn();
      const { reconnect } = startStream({ onOpen });
      const first = FakeEventSource.last;
      first?.onopen?.();
      reconnect();
      expect(first?.closed).toBe(true);
      expect(useReviewStore.getState().connection).toBe("disconnected");
      reconnect(); // already scheduled: a no-op
      await vi.advanceTimersByTimeAsync(1000); // the first backoff step
      const second = FakeEventSource.last;
      expect(second).not.toBe(first);
      expect(onOpen).toHaveBeenCalledTimes(1); // not before the new connection opens
      second?.onopen?.();
      expect(onOpen).toHaveBeenCalledTimes(2);
      expect(useReviewStore.getState().connection).toBe("live");
    });

    it("backs off while the open's resync fails, and resets once one succeeds", async () => {
      // Each open's resync result, in order: the boot's, two failed retries, a success.
      const results = [true, false, true];
      const onOpen = vi.fn(() => Promise.resolve(results.shift() ?? true));
      const { reconnect } = startStream({ onOpen });
      const opened = async () => {
        FakeEventSource.last?.onopen?.();
        await vi.advanceTimersByTimeAsync(0); // settle the resync
      };
      await opened();
      reconnect(); // the boot's sync could not install
      await vi.advanceTimersByTimeAsync(1000);
      await opened(); // its resync fails
      const failing = FakeEventSource.last;
      reconnect();
      await vi.advanceTimersByTimeAsync(1999);
      expect(FakeEventSource.last).toBe(failing); // backed off to 2 s
      await vi.advanceTimersByTimeAsync(1);
      expect(FakeEventSource.last).not.toBe(failing);
      await opened(); // this resync installs
      // A later natural drop starts the backoff again from its first step.
      const healthy = FakeEventSource.last;
      healthy?.onerror?.();
      await vi.advanceTimersByTimeAsync(1000);
      expect(FakeEventSource.last).not.toBe(healthy);
    });

    // A connection that lost a frame does not reset the backoff, though its
    // resync installed: a server that keeps sending what the tab cannot
    // apply is retried at a growing delay, not at the first step forever.
    it("repeated losses back off, though each reconnect's resync installs", async () => {
      const onOpen = vi.fn(() => Promise.resolve(true));
      startStream({ onOpen });
      const opened = async () => {
        FakeEventSource.last?.onopen?.();
        await vi.advanceTimersByTimeAsync(0); // settle the resync
      };
      await opened();
      for (const delay of [1000, 2000, 5000, 10_000, 30_000]) {
        const lossy = FakeEventSource.last;
        emit({ format: 1, session: "not-held", events: [] });
        await vi.advanceTimersByTimeAsync(16); // the flush: the fragment is lost
        expect(lossy?.closed).toBe(true);
        await vi.advanceTimersByTimeAsync(delay - 1);
        expect(FakeEventSource.last).toBe(lossy);
        await vi.advanceTimersByTimeAsync(1);
        expect(FakeEventSource.last).not.toBe(lossy);
        await opened();
      }
      // A connection that ends clean starts over.
      const clean = FakeEventSource.last;
      clean?.onerror?.();
      await vi.advanceTimersByTimeAsync(1000);
      expect(FakeEventSource.last).not.toBe(clean);
    });

    // An unreadable-format frame is a loss without a reconnect of its own:
    // when its connection drops, it does not reset the backoff either.
    it("a connection that dropped an unreadable frame does not reset the backoff when it ends", async () => {
      const onOpen = vi.fn(() => Promise.resolve(true));
      const { reconnect } = startStream({ onOpen });
      const opened = async () => {
        FakeEventSource.last?.onopen?.();
        await vi.advanceTimersByTimeAsync(0); // settle the resync
      };
      await opened();
      reconnect(); // a clean connection: the first step
      await vi.advanceTimersByTimeAsync(1000);
      await opened();
      const lossy = FakeEventSource.last;
      emit({ format: 2, session: "s", events: [] });
      await vi.advanceTimersByTimeAsync(16); // the flush
      expect(lossy?.closed).toBe(false); // no reconnect for an unreadable format
      lossy?.onerror?.();
      await vi.advanceTimersByTimeAsync(1999);
      expect(FakeEventSource.last).toBe(lossy); // backed off to 2 s, not reset
      await vi.advanceTimersByTimeAsync(1);
      expect(FakeEventSource.last).not.toBe(lossy);
    });

    it("a later forced reconnect after a successful resync also starts from the first step", async () => {
      const results = [true, false, false, true];
      const onOpen = vi.fn(() => Promise.resolve(results.shift() ?? true));
      const { reconnect } = startStream({ onOpen });
      const opened = async () => {
        FakeEventSource.last?.onopen?.();
        await vi.advanceTimersByTimeAsync(0);
      };
      await opened();
      for (const delay of [1000, 2000, 5000]) {
        reconnect();
        await vi.advanceTimersByTimeAsync(delay);
        await opened();
      }
      const healthy = FakeEventSource.last;
      reconnect(); // a later admission's snapshot failed
      await vi.advanceTimersByTimeAsync(1000);
      expect(FakeEventSource.last).not.toBe(healthy);
    });
  });
});
