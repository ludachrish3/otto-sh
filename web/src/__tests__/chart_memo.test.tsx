// Task 11: buildStackOption(...) used to be called inline in JSX, so every
// chart's option object was rebuilt on every SubjectPage render — including
// a live tick that only touched a DIFFERENT host's series. This pins the
// memo's key: it must move on the drawn series' own revisions (bumped only
// when that series gets new points — seriesIndex.ts), not on `session`
// identity (which changes on every non-empty append, for every chart).
// Idiom follows subjectpage.retirement.test.tsx: mock ../charts/options to
// spy on buildStackOption, mock ../charts/echarts so ChartPanel doesn't
// touch a real canvas, and drive wouter's useParams directly.
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

globalThis.ResizeObserver ??= class {
  observe() {}
  unobserve() {}
  disconnect() {}
} as unknown as typeof ResizeObserver;

// A spying fake, not a no-op stub: the bug this file guards against (window/
// markers baked into the memoized `option` but never in its deps) is
// invisible to a call-count-only assertion — buildStackOption's call count
// can look perfectly correct while ECharts itself still received a stale
// x-axis. `calls` captures exactly what each setOption() invocation handed
// ECharts, so a test can pin the actual VALUES that reached the chart.
class FakeChart {
  group = "";
  calls: { opt: Record<string, unknown>; opts?: Record<string, unknown> }[] = [];
  setOption(opt: Record<string, unknown>, opts?: Record<string, unknown>) {
    this.calls.push({ opt, opts });
  }
  on() {}
  resize() {}
  dispose() {}
}
const instances: FakeChart[] = [];
vi.mock("../charts/echarts", () => ({
  echarts: {
    init: () => {
      const c = new FakeChart();
      instances.push(c);
      return c;
    },
    connect: () => {},
  },
}));

const buildStackOption = vi.fn((_args: unknown) => ({ series: [] }));
vi.mock("../charts/options", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../charts/options")>();
  return {
    ...actual,
    buildStackOption: (args: Parameters<typeof actual.buildStackOption>[0]) =>
      buildStackOption(args),
  };
});

vi.mock("wouter", async (importOriginal) => {
  const mod = await importOriginal<typeof import("wouter")>();
  return { ...mod, useParams: () => ({ id: "h0" }) };
});

import { useReviewStore } from "../data/reviewStore";
import { SubjectPage } from "../pages/SubjectPage";
import { synthSession } from "./_synth";

// Mirrors _synth.ts's private T0 so this file can predict the exact epoch-ms
// window bounds liveRange derives, rather than asserting "some later value".
const T0 = Date.parse("2026-07-12T00:00:00Z");
const WINDOW_MS = 900_000; // reviewStore's default windowMs

/** Last setOption() call that actually carried an xAxis patch — the mocked
 * buildStackOption stub (`{ series: [] }`) never includes one, so only the
 * incremental window/marker patch (options.ts's windowPatch) can produce a
 * hit here. Throws if none was ever applied. */
function lastXAxis(chart: FakeChart): { min: number; max: number } {
  for (let i = chart.calls.length - 1; i >= 0; i--) {
    const xAxis = chart.calls[i].opt.xAxis as { min: number; max: number } | undefined;
    if (xAxis) return xAxis;
  }
  throw new Error("no setOption call carried an xAxis patch");
}

describe("chart option memoization", () => {
  beforeEach(() => {
    buildStackOption.mockClear();
    instances.length = 0;
    const session = synthSession({ hosts: 2, seriesPerHost: 1, ticks: 3, intervalS: 5 });
    useReviewStore.setState({
      sessions: [session],
      activeSessionId: session.id,
      range: null,
      mode: "live",
    });
  });

  afterEach(() => {
    cleanup();
    useReviewStore.setState({
      sessions: [],
      rawMonitorSessions: null,
      sourceName: null,
      warnings: [],
      importError: null,
      activeSessionId: null,
      range: null,
      mode: null,
    });
  });

  it("does NOT rebuild a chart's options when an unrelated host appends", () => {
    render(<SubjectPage />);
    const callsAfterMount = buildStackOption.mock.calls.length;
    expect(callsAfterMount).toBeGreaterThan(0);

    // A fragment touching ONLY h1. h0's charts must not re-memo: their series
    // revisions did not move. Keying the memo on `session` identity (which DOES
    // change every tick — applyFragment returns a new session object even for
    // a metrics-only fragment) is the trap this asserts against.
    act(() => {
      useReviewStore.getState().actions.appendFragment({
        format: 1,
        session: "synth",
        metrics: [{ host: "h1", label: "m0", timestamp: "2026-07-12T00:01:00Z", value: 99 }],
        events: [],
        log_events: [],
        deleted_event_ids: [],
        chart_map: {},
        meta: null,
      } as never);
    });

    expect(buildStackOption.mock.calls.length).toBe(callsAfterMount);
  });

  it("DOES rebuild when the subject's own series appends", () => {
    render(<SubjectPage />);
    const before = buildStackOption.mock.calls.length;
    act(() => {
      useReviewStore.getState().actions.appendFragment({
        format: 1,
        session: "synth",
        metrics: [{ host: "h0", label: "m0", timestamp: "2026-07-12T00:01:00Z", value: 99 }],
        events: [],
        log_events: [],
        deleted_event_ids: [],
        chart_map: {},
        meta: null,
      } as never);
    });
    expect(buildStackOption.mock.calls.length).toBeGreaterThan(before);
  });

  // The regression this task fixes: `window_`/`markers` are consumed INSIDE
  // buildStackOption but were never in the memo's dep list. In live-follow
  // mode `window_` is derived from session.endMs, which is GLOBAL — any
  // host's fragment advances it (fragment.ts) — so a chart whose own series
  // never ticks must still slide its x-axis when some OTHER host ticks.
  // Call-count assertions alone can't catch this (the previous two tests
  // pass even when the bug is present), so this pins the actual VALUES
  // ECharts receives, not just how many times buildStackOption ran.
  it("advances the ECharts x-axis window for a quiet host's chart when only another host ticks, without rebuilding its series", () => {
    render(<SubjectPage />);
    const callsAfterMount = buildStackOption.mock.calls.length;
    expect(callsAfterMount).toBeGreaterThan(0);
    expect(instances).toHaveLength(1);

    // Initial window: liveRange(endMs, windowMs) off the synth session's
    // starting endMs (T0 + (ticks-1)*intervalS = T0 + 10_000).
    expect(lastXAxis(instances[0])).toEqual({ min: T0 + 10_000 - WINDOW_MS, max: T0 + 10_000 });
    const callsBeforeTick = instances[0].calls.length;

    // A fragment touching ONLY h1 (h0 is this page's subject and stays
    // quiet) — but at a LATER timestamp, so session.endMs still advances.
    act(() => {
      useReviewStore.getState().actions.appendFragment({
        format: 1,
        session: "synth",
        metrics: [{ host: "h1", label: "m0", timestamp: "2026-07-12T00:01:00Z", value: 99 }],
        events: [],
        log_events: [],
        deleted_event_ids: [],
        chart_map: {},
        meta: null,
      } as never);
    });

    // Expensive half: h0's series revision never moved, so the full option
    // rebuild must NOT have happened.
    expect(buildStackOption.mock.calls.length).toBe(callsAfterMount);

    // Cheap half: ECharts must still have received an advanced x-axis,
    // pinned to the exact bounds liveRange derives from the new endMs
    // (T0 + 60_000, from h1's fragment timestamp).
    expect(lastXAxis(instances[0])).toEqual({ min: T0 + 60_000 - WINDOW_MS, max: T0 + 60_000 });

    // And it must have arrived as a lightweight MERGE patch (not a full,
    // notMerge:true option replace) — confirms this went through the cheap
    // path, not some other route to the same numbers.
    const patchCalls = instances[0].calls.slice(callsBeforeTick);
    const mergeCall = patchCalls.find(
      (c) => c.opt.xAxis !== undefined && c.opts?.notMerge === false,
    );
    expect(mergeCall).toBeDefined();
    expect(mergeCall?.opt.grid).toBeUndefined();
  });

  // Plan 5b Task 13 (the replay soak) caught this live, end-to-end: open a
  // host's page BEFORE it has emitted a single sample, then let data stream
  // in. `checked` used to be seeded once from `tree` when `treeKey`
  // (session+subject identity) first took its value — which for THIS page
  // is at mount, while `tree` was still empty — and never revisited, since
  // treeKey never changes again for as long the page stays open. The
  // checkbox would eventually appear (tree keeps growing every render) but
  // stay permanently unchecked, so no chart would ever draw it.
  it("auto-selects a series that starts reporting after the page is already open", () => {
    const emptySession = synthSession({ hosts: 2, seriesPerHost: 1, ticks: 0, intervalS: 5 });
    useReviewStore.setState({
      sessions: [emptySession],
      activeSessionId: emptySession.id,
      range: null,
      mode: "live",
    });
    render(<SubjectPage />);
    // h0 has no data yet: no series checkbox exists at all.
    expect(screen.queryByTestId("series-node-m0")).toBeNull();

    act(() => {
      useReviewStore.getState().actions.appendFragment({
        format: 1,
        session: "synth",
        metrics: [{ host: "h0", label: "m0", timestamp: "2026-07-12T00:00:00Z", value: 1 }],
        events: [],
        log_events: [],
        deleted_event_ids: [],
        chart_map: {},
        meta: null,
      } as never);
    });

    // The checkbox now exists (the tree grew) AND is auto-checked -- not
    // left behind unchecked the way a `treeKey`-only reset used to.
    const box = screen.getByTestId("series-node-m0") as HTMLInputElement;
    expect(box.checked).toBe(true);
    // ...and the chart actually asked to draw it, not just the checkbox UI.
    const lastCall = buildStackOption.mock.calls.at(-1)?.[0] as
      | { series: { key: string }[] }
      | undefined;
    expect(lastCall?.series.some((s) => s.key === "m0")).toBe(true);
  });

  // A series a user has DELIBERATELY unchecked must stay unchecked when a
  // DIFFERENT, brand-new series appears -- the growth-triggered auto-select
  // above must not clobber existing choices, only add ones that never had a
  // choice made about them yet.
  it("a later-appearing series does not resurrect an earlier manual uncheck", () => {
    render(<SubjectPage />); // beforeEach's session: h0/m0, h1/m0, 3 ticks each
    fireEvent.click(screen.getByTestId("series-node-m0"));
    expect((screen.getByTestId("series-node-m0") as HTMLInputElement).checked).toBe(false);

    // A brand-new label for h0 -- the tree grows, which is what drives the
    // auto-select effect at all.
    act(() => {
      useReviewStore.getState().actions.appendFragment({
        format: 1,
        session: "synth",
        metrics: [{ host: "h0", label: "m1", timestamp: "2026-07-12T00:01:00Z", value: 1 }],
        events: [],
        log_events: [],
        deleted_event_ids: [],
        chart_map: {},
        meta: null,
      } as never);
    });

    expect((screen.getByTestId("series-node-m0") as HTMLInputElement).checked).toBe(false);
    expect((screen.getByTestId("series-node-m1") as HTMLInputElement).checked).toBe(true);
  });
});
