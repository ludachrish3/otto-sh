// The live-stream reader (stream.ts) judges `format` by the declared read list
// (web/src/data/streamFormat.ts), never by a literal of its own. With the list
// swapped for [2], a version-2 fragment is applied and a version-1 fragment is
// dropped; a reader still comparing with `1` fails both. The browser's writer,
// eventApi.ts, stamps the mocked STREAM_FORMAT, not a `1`.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../data/streamFormat", () => ({
  MONITOR_STREAM_READ_VERSIONS: [2],
  MONITOR_STREAM_WRITE_VERSIONS: [2],
  STREAM_FORMAT: 2,
}));

// Imported AFTER the mock so stream.ts and eventApi.ts pick up the swapped list.
const { startStream } = await import("../data/stream");
const { createEvent, deleteEvent } = await import("../data/eventApi");
const { useReviewStore } = await import("../data/reviewStore");
const { synthSession } = await import("./_synth");

class FakeEventSource {
  static last: FakeEventSource | null = null;
  onmessage: ((e: MessageEvent<string>) => void) | null = null;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor() {
    FakeEventSource.last = this;
  }
  close() {}
}

const metric = (format: number) => ({
  format,
  session: "s",
  metrics: [
    {
      host: "h",
      label: "m",
      timestamp: "2026-07-01T08:00:05+00:00",
      value: format,
    },
  ],
});

function streamed(frags: readonly unknown[]) {
  const stop = startStream();
  for (const frag of frags)
    FakeEventSource.last?.onmessage?.({
      data: JSON.stringify(frag),
    } as MessageEvent<string>);
  vi.advanceTimersByTime(20);
  stop();
  return useReviewStore.getState().sessions[0];
}

beforeEach(() => {
  vi.stubGlobal("EventSource", FakeEventSource as unknown as typeof EventSource);
  vi.useFakeTimers();
  useReviewStore.setState({
    sessions: [
      {
        ...synthSession({ hosts: 0, seriesPerHost: 0, ticks: 0, intervalS: 5 }),
        id: "s",
      },
    ],
    connection: "connecting",
  });
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("startStream follows the declared read list", () => {
  it("applies a version the list allows and drops one it no longer holds", () => {
    expect(streamed([metric(1), metric(2)])?.metrics.map((m) => m.value)).toEqual([2]);
  });
});

describe("eventApi follows the declared write version", () => {
  it("stamps the declared write version on every fragment it builds", async () => {
    const spy = vi.spyOn(useReviewStore.getState().actions, "appendFragment");
    const created = {
      id: 7,
      timestamp: "2026-07-01T08:00:06+00:00",
      label: "deploy",
      source: "manual",
      color: "#888888",
      dash: "dash",
    };
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValueOnce(new Response(JSON.stringify(created), { status: 201 }))
        .mockResolvedValueOnce(new Response(null, { status: 204 })),
    );
    await createEvent("s", { label: "deploy" });
    await deleteEvent("s", 7);
    expect(spy.mock.calls.map(([frag]) => frag.format)).toEqual([2, 2]);
  });
});
