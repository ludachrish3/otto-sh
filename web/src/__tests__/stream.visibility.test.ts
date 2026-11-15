// web/src/__tests__/stream.visibility.test.ts
// #1041: an HTTP/1.1 browser holds about six connections per host across
// every tab, so a tab hidden for HIDDEN_GRACE_MS lets its stream go
// (data/stream.ts's `park`), and takes it back at once when shown. Its own
// file: every stream listens to the one `document`, so each test here stops
// the streams it started, and no other file's streams are around to hear
// the visibility changes.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useReviewStore } from "../data/reviewStore";
import { HIDDEN_GRACE_MS, type StreamControl, startStream } from "../data/stream";

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

let visibility: DocumentVisibilityState = "visible";
let started: StreamControl[] = [];

beforeEach(() => {
  vi.stubGlobal("EventSource", FakeEventSource as unknown as typeof EventSource);
  vi.useFakeTimers();
  visibility = "visible";
  vi.spyOn(document, "visibilityState", "get").mockImplementation(() => visibility);
  useReviewStore.setState({ connection: "connecting" });
});

afterEach(() => {
  for (const control of started) control.stop();
  started = [];
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

const show = (state: DocumentVisibilityState) => {
  visibility = state;
  document.dispatchEvent(new Event("visibilitychange"));
};

const start = (opts: Parameters<typeof startStream>[0] = {}): StreamControl => {
  const control = startStream(opts);
  started.push(control);
  return control;
};

/** A stream whose first connection opened. */
const openStream = (opts: Parameters<typeof startStream>[0] = {}): StreamControl => {
  const control = start(opts);
  FakeEventSource.last?.onopen?.();
  return control;
};

describe("a hidden tab lets its stream go", () => {
  it("closes after the grace hidden, and reconnects at once when shown, staying live", async () => {
    const onOpen = vi.fn(() => undefined);
    openStream({ onOpen });
    const first = FakeEventSource.last;
    show("hidden");
    await vi.advanceTimersByTimeAsync(HIDDEN_GRACE_MS - 1);
    expect(first?.closed).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    expect(first?.closed).toBe(true);
    // Let go on purpose: nothing to show a reconnecting banner for.
    expect(useReviewStore.getState().connection).toBe("live");
    await vi.advanceTimersByTimeAsync(60_000);
    expect(FakeEventSource.last).toBe(first); // no retry while hidden

    show("visible");
    const second = FakeEventSource.last;
    expect(second).not.toBe(first);
    expect(useReviewStore.getState().connection).toBe("live");
    second?.onopen?.();
    expect(onOpen).toHaveBeenCalledTimes(2); // the resumed open resyncs
  });

  it("a tab shown again within the grace keeps its connection", async () => {
    openStream();
    const first = FakeEventSource.last;
    show("hidden");
    await vi.advanceTimersByTimeAsync(HIDDEN_GRACE_MS - 1);
    show("visible");
    await vi.advanceTimersByTimeAsync(HIDDEN_GRACE_MS);
    expect(FakeEventSource.last).toBe(first);
    expect(first?.closed).toBe(false);
  });

  it("letting go withholds the acknowledgement: what is published meanwhile is missed", async () => {
    const withhold = vi.spyOn(useReviewStore.getState().actions, "withholdCoverage");
    openStream();
    show("hidden");
    await vi.advanceTimersByTimeAsync(HIDDEN_GRACE_MS);
    expect(withhold).toHaveBeenCalledTimes(1);
  });

  it("a stream retrying when let go stops retrying, and reconnects at once when shown", async () => {
    openStream();
    show("hidden");
    // Until the grace passes the stream is held, so a drop retries as usual.
    FakeEventSource.last?.onerror?.();
    await vi.advanceTimersByTimeAsync(HIDDEN_GRACE_MS);
    const parked = FakeEventSource.last;
    expect(parked?.closed).toBe(true);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(FakeEventSource.last).toBe(parked);
    expect(vi.getTimerCount()).toBe(0);
    show("visible");
    expect(FakeEventSource.last).not.toBe(parked);
  });

  it("a reconnect asked for while let go waits for the tab to be shown", async () => {
    const control = openStream();
    show("hidden");
    await vi.advanceTimersByTimeAsync(HIDDEN_GRACE_MS);
    const parked = FakeEventSource.last;
    control.reconnect();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(FakeEventSource.last).toBe(parked);
  });

  it("a stream that has not opened is not let go until it has", async () => {
    start();
    const first = FakeEventSource.last;
    show("hidden");
    await vi.advanceTimersByTimeAsync(HIDDEN_GRACE_MS);
    expect(first?.closed).toBe(false);
    first?.onopen?.();
    await vi.advanceTimersByTimeAsync(HIDDEN_GRACE_MS);
    expect(first?.closed).toBe(true);
  });

  it("a tab opened in the background lets go once its stream has opened and the grace passed", async () => {
    visibility = "hidden";
    openStream();
    const first = FakeEventSource.last;
    await vi.advanceTimersByTimeAsync(HIDDEN_GRACE_MS);
    expect(first?.closed).toBe(true);
  });

  it("what arrived before letting go applies at once, and a frame it cannot apply asks for no reconnect", async () => {
    const append = vi
      .spyOn(useReviewStore.getState().actions, "appendFragments")
      .mockReturnValue(false); // the frame cannot apply: `lost`
    const withhold = vi.spyOn(useReviewStore.getState().actions, "withholdCoverage");
    openStream();
    show("hidden");
    await vi.advanceTimersByTimeAsync(HIDDEN_GRACE_MS - 1);
    // Buffered for the next frame's flush, one tick before the grace ends.
    FakeEventSource.last?.onmessage?.({
      data: JSON.stringify({ format: 1, session: "s", metrics: [], observed_at: "t" }),
    } as MessageEvent<string>);
    await vi.advanceTimersByTimeAsync(1);
    expect(append).toHaveBeenCalledTimes(1); // flushed by the park itself
    expect(withhold).toHaveBeenCalled();
    // Let go, not dropped: no retry scheduled, no reconnecting state.
    expect(vi.getTimerCount()).toBe(0);
    expect(useReviewStore.getState().connection).toBe("live");
  });

  it("a stream stopped within the grace leaves no timer behind", async () => {
    const control = openStream();
    show("hidden");
    await vi.advanceTimersByTimeAsync(HIDDEN_GRACE_MS / 2);
    control.stop();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("a stopped stream no longer follows the tab's visibility", async () => {
    const control = openStream();
    const first = FakeEventSource.last;
    control.stop();
    show("hidden");
    await vi.advanceTimersByTimeAsync(HIDDEN_GRACE_MS);
    show("visible");
    expect(FakeEventSource.last).toBe(first);
    expect(vi.getTimerCount()).toBe(0);
  });
});
