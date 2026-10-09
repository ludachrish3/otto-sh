// Dump spec §13.5 for the live stream (`monitor-live-stream`), a format of its
// own and not the export document's. Every declared read version has a frozen,
// populated sample (tests/_fixtures/formats/monitor-live-stream/, shared with
// the Python model test) that the browser's one reader, startStream, must apply
// for meaning, one server-sent event per fragment. Every declared write version
// is what the browser's own fragments (eventApi.ts) are stamped with. The
// versions come from streamFormat.ts, which
// tests/unit/models/test_browser_format_mirrors.py holds equal to the Python lists.
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { MonitorSessionFragment } from "../api/export.gen";
import { createEvent, deleteEvent } from "../data/eventApi";
import type { NormalizedSession } from "../data/exportDoc";
import { useReviewStore } from "../data/reviewStore";
import { startStream } from "../data/stream";
import { MONITOR_STREAM_READ_VERSIONS, MONITOR_STREAM_WRITE_VERSIONS } from "../data/streamFormat";
import { synthSession } from "./_synth";

const SAMPLES = join(
  dirname(fileURLToPath(import.meta.url)),
  "../../../tests/_fixtures/formats/monitor-live-stream",
);
const sample = (version: number): Record<string, unknown>[] =>
  JSON.parse(readFileSync(join(SAMPLES, `${version}.json`), "utf-8")) as Record<string, unknown>[];
const SESSION = "2026-07-01T08-00-00-live";
const OPENED_MS = Date.parse("2026-07-01T08:00:00Z");

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

const emit = (payload: unknown) =>
  FakeEventSource.last?.onmessage?.({
    data: JSON.stringify(payload),
  } as MessageEvent<string>);

/** Stream *frags* through the real reader into an empty held session; return it. */
function streamed(frags: readonly unknown[]): NormalizedSession {
  const stop = startStream();
  for (const frag of frags) emit(frag);
  vi.advanceTimersByTime(20); // one flush
  stop();
  return useReviewStore.getState().sessions[0] as NormalizedSession;
}

const MEANING: Record<number, (s: NormalizedSession) => void> = {
  1: (s) => {
    expect(s.metrics.map((m) => [m.host, m.label, m.value])).toEqual([
      ["rack1-a", "cpu", 12.5],
      ["rack1-a", "cpu", 25],
    ]);
    expect(s.endMs).toBe(Date.parse("2026-07-01T08:00:10Z"));
    expect(s.chartMap).toEqual({ cpu: "CPU" });
    expect(s.meta.interval).toBe(5);
    expect(s.meta.charts.map((c) => c.chart)).toEqual(["CPU"]);
    // event 1 was added then edited (upsert by id); event 2 was added then deleted
    expect(s.events.map((e) => [e.id, e.label, e.end_timestamp])).toEqual([
      [1, "deploy v2", "2026-07-01T08:00:09+00:00"],
    ]);
    expect(s.logEvents.map((e) => e.fields)).toEqual([{ level: "error", message: "link flap" }]);
    expect(s.tunnels.map((t) => [t.id, t.hops])).toEqual([["t1", ["rack1-a", "rack1-b"]]]);
  },
};

beforeEach(() => {
  vi.stubGlobal("EventSource", FakeEventSource as unknown as typeof EventSource);
  vi.useFakeTimers();
  useReviewStore.setState({
    sessions: [
      {
        ...synthSession({ hosts: 0, seriesPerHost: 0, ticks: 0, intervalS: 5 }),
        id: SESSION,
        // the live session opened just before the sample's first fragment
        startMs: OPENED_MS,
        endMs: OPENED_MS,
      },
    ],
    connection: "connecting",
    warnings: [],
  });
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("monitor-live-stream versions (browser)", () => {
  it.each([...MONITOR_STREAM_READ_VERSIONS])("reads the frozen v%s sample", (version) => {
    const check = MEANING[version];
    if (check === undefined) throw new Error(`no expectation for read version ${version}`);
    check(streamed(sample(version)));
  });

  it("drops, counts and warns once about fragments outside the declared reads", () => {
    const newest = Math.max(...MONITOR_STREAM_READ_VERSIONS);
    const next = newest + 1;
    const [first, second, ...rest] = sample(newest);
    const stop = startStream();
    // three unreadable fragments in one frame, then two more in the next
    for (const frag of [first, second, ...rest].slice(0, 3)) emit({ ...frag, format: next });
    vi.advanceTimersByTime(20);
    for (const frag of rest.slice(1, 3)) emit({ ...frag, format: next });
    vi.advanceTimersByTime(20);
    let state = useReviewStore.getState();
    expect(state.sessions[0]?.metrics).toEqual([]);
    expect(state.sessions[0]?.events).toEqual([]);
    expect(state.warnings).toHaveLength(1);
    expect(state.warnings[0]).toContain(`sent 3 update(s) in format ${next}`);
    expect(state.warnings[0]).toContain(`reads format ${MONITOR_STREAM_READ_VERSIONS.join(", ")}`);
    const warned = state.warnings[0];
    // the stream is not crashed: a readable fragment still applies, and a later
    // unreadable one is still dropped, with no second warning and the first unchanged
    emit(second);
    emit({ ...rest[0], format: next }); // the "deploy" event add
    vi.advanceTimersByTime(20);
    stop();
    state = useReviewStore.getState();
    expect(state.sessions[0]?.metrics.map((m) => m.value)).toEqual([25]);
    expect(state.sessions[0]?.events).toEqual([]);
    expect(state.warnings).toEqual([warned]);
  });

  it("reads a fragment with no format, as the Python model does", () => {
    const [first] = sample(Math.max(...MONITOR_STREAM_READ_VERSIONS));
    const { format: _dropped, ...unversioned } = first as Record<string, unknown>;
    expect(streamed([unversioned]).metrics).toHaveLength(1);
  });

  it.each([...MONITOR_STREAM_WRITE_VERSIONS])(
    "stamps every browser-built fragment with write version %s",
    async (version) => {
      const spy = vi.spyOn(useReviewStore.getState().actions, "appendFragment");
      vi.stubGlobal(
        "fetch",
        vi.fn(
          async () =>
            new Response(
              JSON.stringify({
                id: 7,
                timestamp: "2026-07-01T08:00:06+00:00",
                label: "deploy",
                source: "manual",
                color: "#888888",
                dash: "dash",
              }),
              { status: 201 },
            ),
        ),
      );
      await createEvent(SESSION, { label: "deploy" });
      await deleteEvent(SESSION, 7);
      const stamped = spy.mock.calls.map(([frag]) => (frag as MonitorSessionFragment).format);
      expect(stamped).toEqual([version, version]);
    },
  );
});
