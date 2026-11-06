import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  type BrushArmAction,
  type BrushClearAction,
  type ChartAction,
  ChartPanel,
  type SetOptionOpts,
} from "../charts/ChartPanel";
import {
  approxMeasure,
  DEFAULT_PLOT_WIDTH_PX,
  type EventMarker,
  type EventOverlay,
  GRID,
  LANE_LABEL_STEP_PX,
  laneTopPx,
  layoutWith,
  type MeasureText,
} from "../charts/options";
import { formatClockTime } from "../data/time";

/** Every live observer, so a test can `trigger(width)` a resize: jsdom has
 * neither ResizeObserver nor layout, so the trigger stamps `clientWidth` on
 * each observed element and then runs its callback. */
const observers = new Set<FakeResizeObserver>();

class FakeResizeObserver {
  targets: Element[] = [];
  readonly callback: ResizeObserverCallback;
  constructor(callback: ResizeObserverCallback) {
    this.callback = callback;
    observers.add(this);
  }
  observe(target: Element) {
    this.targets.push(target);
  }
  unobserve() {}
  disconnect() {
    observers.delete(this);
  }
}

globalThis.ResizeObserver = FakeResizeObserver as unknown as typeof ResizeObserver;

function trigger(width: number) {
  act(() => {
    for (const ro of [...observers]) {
      for (const t of ro.targets)
        Object.defineProperty(t, "clientWidth", { configurable: true, value: width });
      ro.callback([], ro as unknown as ResizeObserver);
    }
  });
}

const instances: FakeChart[] = [];

class FakeChart {
  group = "";
  disposed = false;
  options: unknown[] = [];
  /** Full (option, opts) pairs — `options` above keeps only the option arg for
   * the legacy lifecycle assertions; this also captures the setOption opts so
   * tests can pin notMerge/lazyUpdate. */
  calls: { opt: unknown; opts: SetOptionOpts | undefined }[] = [];
  handlers = new Map<string, (e: unknown) => void>();
  /** Recorded `dispatchAction` payloads — Task 12's brush arming/clearing. */
  dispatched: ChartAction[] = [];
  setOption(opt: unknown, opts?: SetOptionOpts) {
    this.options.push(opt);
    this.calls.push({ opt, opts });
  }
  on(event: string, cb: (e: unknown) => void) {
    this.handlers.set(event, cb);
  }
  dispatchAction(payload: ChartAction) {
    this.dispatched.push(payload);
  }
  /** Drives a registered handler directly, same shape as `handlers.get(...)?.(e)`
   * but named for readability at brushEnd call sites below. */
  emit(event: string, e: unknown) {
    this.handlers.get(event)?.(e);
  }
  resize() {}
  dispose() {
    this.disposed = true;
  }
}

vi.mock("../charts/echarts", () => ({
  echarts: {
    init: () => {
      const c = new FakeChart();
      instances.push(c);
      return c;
    },
    connect: vi.fn(),
  },
}));

const WINDOW = { from: 1_000_000, to: 2_000_000 };

/** `layoutWith`'s placements alone, in input order. */
function layoutPlacements(...args: Parameters<typeof layoutWith>) {
  return layoutWith(...args).map((l) => l.placement);
}

describe("ChartPanel lifecycle", () => {
  beforeEach(() => {
    instances.length = 0;
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
    cleanup();
  });

  it("inits with the group, applies options, disposes on unmount", () => {
    const { rerender, unmount } = render(
      <ChartPanel option={{ a: 1 }} groupId="g" window={WINDOW} testId="chart-panel-x" />,
    );
    expect(instances).toHaveLength(1);
    expect(instances[0].group).toBe("g");
    expect(instances[0].options).toEqual([{ a: 1 }]);
    rerender(<ChartPanel option={{ a: 2 }} groupId="g" window={WINDOW} testId="chart-panel-x" />);
    expect(instances[0].options).toEqual([{ a: 1 }, { a: 2 }]);
    unmount();
    expect(instances[0].disposed).toBe(true);
  });

  it("applies the full-replace option synchronously — no lazyUpdate", () => {
    // The [option]-effect's setOption is a notMerge (whole-model rebuild) full
    // replace. It MUST NOT pass lazyUpdate: with lazyUpdate:true, ECharts
    // installs the new, still data-less GlobalModel and defers the
    // data-processing pipeline to the next zr frame — leaving a window where
    // getSeriesByIndex(i).getData() is undefined. An axis-trigger tooltip
    // mousemove landing in that window crashes in getDataParams reading
    // getRawIndex of undefined (apache/echarts#9402). A synchronous setOption
    // runs the pipeline + flush before returning, so no mousemove handler can
    // ever observe a data-less series. See ChartPanel.tsx.
    render(
      <ChartPanel
        option={{ series: [{ id: "s", data: [] }] }}
        groupId="g"
        window={WINDOW}
        testId="chart-panel-x"
      />,
    );
    const replace = instances[0].calls.find((c) => c.opts?.notMerge === true);
    expect(replace, "expected a notMerge full-replace setOption call").toBeDefined();
    expect(replace?.opts?.lazyUpdate).not.toBe(true);
  });

  it("debounces datazoom into an onZoom range", () => {
    const onZoom = vi.fn();
    render(<ChartPanel option={{}} groupId="g" window={WINDOW} onZoom={onZoom} />);
    instances[0].handlers.get("datazoom")?.({ start: 25, end: 75 });
    expect(onZoom).not.toHaveBeenCalled(); // debounced
    vi.advanceTimersByTime(250);
    expect(onZoom).toHaveBeenCalledWith({ from: 1_250_000, to: 1_750_000 });
  });

  it("suppresses sub-second no-op zooms", () => {
    const onZoom = vi.fn();
    render(<ChartPanel option={{}} groupId="g" window={WINDOW} onZoom={onZoom} />);
    instances[0].handlers.get("datazoom")?.({ start: 0, end: 100 });
    vi.advanceTimersByTime(250);
    expect(onZoom).not.toHaveBeenCalled();
  });
});

// Hover-scoped y crosshair (TODO item 4): connected charts broadcast the
// axisPointer to the whole group, which used to mirror the hovered chart's y
// crosshair + label onto every chart at a meaningless height. The built option
// parks yAxis.axisPointer.show at false (chartoptions.test.ts pins that);
// ChartPanel flips it to "auto" for the ONE instance under the cursor. The
// receiving side is strict by construction — ECharts' collectAxesInfo drops a
// show:false axis from axesInfo before any trigger/broadcast logic — but that
// half lives inside real ECharts, so a browser check stands in for it; these
// tests pin the toggling contract this component owns.
describe("ChartPanel hover-scoped y axisPointer", () => {
  beforeEach(() => {
    instances.length = 0;
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
    cleanup();
  });

  function yPointerPatches(instance: FakeChart) {
    return instance.calls.filter(
      (c) => (c.opt as { yAxis?: { axisPointer?: unknown } }).yAxis?.axisPointer !== undefined,
    );
  }

  it("enables the y pointer on mouseenter and parks it again on mouseleave", () => {
    const { container } = render(
      <ChartPanel option={{ a: 1 }} groupId="g" window={WINDOW} testId="chart-panel-x" />,
    );
    const el = container.querySelector('[data-testid="chart-panel-x"]');
    if (!(el instanceof HTMLElement)) throw new Error("chart-panel-x not found");
    expect(yPointerPatches(instances[0])).toHaveLength(0); // rest: option's own show:false rules
    fireEvent.mouseEnter(el);
    const afterEnter = yPointerPatches(instances[0]);
    expect(afterEnter).toHaveLength(1);
    expect(afterEnter[0].opt).toEqual({ yAxis: { axisPointer: { show: "auto" } } });
    // MERGE, never notMerge — a whole-model replace here would wipe the chart.
    expect(afterEnter[0].opts?.notMerge).not.toBe(true);
    fireEvent.mouseLeave(el);
    const afterLeave = yPointerPatches(instances[0]);
    expect(afterLeave).toHaveLength(2);
    expect(afterLeave[1].opt).toEqual({ yAxis: { axisPointer: { show: false } } });
  });

  // MUTATION-PROOF (same instance-state-vs-option-model trap as the brush
  // re-arm): a notMerge rebuild installs a fresh model carrying the built
  // option's resting show:false, silently un-hovering a chart the cursor is
  // still inside (live charts rebuild while hovered all the time). The re-arm
  // in the option effect must re-assert the hovered state AFTER the rebuild.
  it("re-asserts the hovered state after a notMerge option rebuild", () => {
    const { container, rerender } = render(
      <ChartPanel option={{ a: 1 }} groupId="g" window={WINDOW} testId="chart-panel-x" />,
    );
    const el = container.querySelector('[data-testid="chart-panel-x"]');
    if (!(el instanceof HTMLElement)) throw new Error("chart-panel-x not found");
    fireEvent.mouseEnter(el);
    rerender(<ChartPanel option={{ a: 2 }} groupId="g" window={WINDOW} testId="chart-panel-x" />);
    const calls = instances[0].calls;
    const lastRebuild = calls.map((c) => c.opts?.notMerge === true).lastIndexOf(true);
    expect(lastRebuild).toBeGreaterThan(-1);
    const reassert = calls
      .slice(lastRebuild + 1)
      .filter(
        (c) =>
          (c.opt as { yAxis?: { axisPointer?: { show?: unknown } } }).yAxis?.axisPointer?.show ===
          "auto",
      );
    expect(reassert).toHaveLength(1);
  });

  it("does NOT re-assert after a rebuild when the cursor is outside", () => {
    const { container, rerender } = render(
      <ChartPanel option={{ a: 1 }} groupId="g" window={WINDOW} testId="chart-panel-x" />,
    );
    const el = container.querySelector('[data-testid="chart-panel-x"]');
    if (!(el instanceof HTMLElement)) throw new Error("chart-panel-x not found");
    fireEvent.mouseEnter(el);
    fireEvent.mouseLeave(el);
    const before = instances[0].calls.length;
    rerender(<ChartPanel option={{ a: 2 }} groupId="g" window={WINDOW} testId="chart-panel-x" />);
    const after = instances[0].calls.slice(before);
    // Exactly the notMerge rebuild — no stray y-pointer patch (the built
    // option's own show:false already IS the resting state).
    expect(after.map((c) => c.opts?.notMerge)).toEqual([true]);
  });
});

// Task 12 (Monitor Plan 5c): brush-based drag zoom-select + sweep-to-mark.
// A `lineX` brush is armed via takeGlobalCursor so a chart-area drag always
// draws a selection rectangle; brushEnd then routes the selected range to
// either onSweep (marking) or onZoom (zoom-select), depending on whether the
// "sweep span" gesture is currently armed (uiStore's sweepArmed).
describe("ChartPanel brush select (Task 12)", () => {
  beforeEach(() => {
    instances.length = 0;
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
    cleanup();
  });

  function arms(instance: FakeChart): BrushArmAction[] {
    return instance.dispatched.filter((d): d is BrushArmAction => d.type === "takeGlobalCursor");
  }

  function clears(instance: FakeChart): BrushClearAction[] {
    return instance.dispatched.filter((d): d is BrushClearAction => d.type === "brush");
  }

  // (a) Mount runs BOTH the init effect and the option effect (dep
  // `[option]` fires on its own initial run too), so a fresh mount dispatches
  // arming from each — this test only pins presence + shape, not an exact
  // count (that's what (b) below isolates via a delta across a rerender).
  it("arms the lineX brush select on mount", () => {
    render(<ChartPanel option={{}} groupId="g" window={WINDOW} />);
    expect(arms(instances[0]).length).toBeGreaterThanOrEqual(1);
    expect(arms(instances[0])[0].brushOption).toEqual({ brushType: "lineX", brushMode: "single" });
  });

  // (b) MUTATION-PROOF: guards no-op risk #1 (a notMerge whole-model rebuild
  // silently drops instance-level brush arming). A rerender with a new
  // `option` re-runs only the option effect (groupId is unchanged, so the
  // init effect does not re-fire) — asserting the arm COUNT strictly grows
  // across that rerender isolates the option effect's own re-arm call.
  // Removing ChartPanel's re-arm dispatchAction line from the option effect
  // must fail this test (the count would stay flat instead of growing) —
  // see task-12-report.md for the mutation-check evidence.
  it("re-arms the brush after a notMerge option rebuild", () => {
    const { rerender } = render(
      <ChartPanel option={{ a: 1 }} groupId="g" window={WINDOW} testId="chart-panel-x" />,
    );
    const before = arms(instances[0]).length;
    rerender(<ChartPanel option={{ a: 2 }} groupId="g" window={WINDOW} testId="chart-panel-x" />);
    const after = arms(instances[0]).length;
    expect(after).toBeGreaterThan(before);
  });

  // (c)
  it("brushEnd with sweepArmed false calls onZoom with the rounded range, and clears the ghost", () => {
    const onZoom = vi.fn();
    render(
      <ChartPanel option={{}} groupId="g" window={WINDOW} onZoom={onZoom} sweepArmed={false} />,
    );
    instances[0].emit("brushEnd", { areas: [{ coordRange: [1_200_000, 1_800_000] }] });
    expect(onZoom).toHaveBeenCalledWith({ from: 1_200_000, to: 1_800_000 });
    const cleared = clears(instances[0]);
    expect(cleared).toHaveLength(1);
    expect(cleared[0].areas).toEqual([]);
  });

  // (d)
  it("brushEnd with sweepArmed true calls onSweep, not onZoom", () => {
    const onZoom = vi.fn();
    const onSweep = vi.fn();
    render(
      <ChartPanel
        option={{}}
        groupId="g"
        window={WINDOW}
        onZoom={onZoom}
        onSweep={onSweep}
        sweepArmed={true}
      />,
    );
    instances[0].emit("brushEnd", { areas: [{ coordRange: [1_200_000, 1_800_000] }] });
    expect(onSweep).toHaveBeenCalledWith({ from: 1_200_000, to: 1_800_000 });
    expect(onZoom).not.toHaveBeenCalled();
  });

  // (e)
  it("ignores a sub-1000ms sweep (neither callback fires) but still clears the ghost", () => {
    const onZoom = vi.fn();
    const onSweep = vi.fn();
    render(
      <ChartPanel
        option={{}}
        groupId="g"
        window={WINDOW}
        onZoom={onZoom}
        onSweep={onSweep}
        sweepArmed={true}
      />,
    );
    instances[0].emit("brushEnd", { areas: [{ coordRange: [1_200_000, 1_200_400] }] });
    expect(onSweep).not.toHaveBeenCalled();
    expect(onZoom).not.toHaveBeenCalled();
    expect(clears(instances[0])).toHaveLength(1);
  });
});

// Task 13 review fix: the manual Ctrl-drag pan (bypasses both the brush and
// ECharts' own dataZoom — see ChartPanel.tsx's init-effect comment) is plain
// DOM mouse listeners on the panel's own container, not anything ECharts
// dispatches — so these drive real `MouseEvent`s at that element directly,
// unlike the brush-select tests above (which drive the FakeChart's own
// `emit`).
describe("ChartPanel manual Ctrl-drag pan (Task 13 review fix)", () => {
  beforeEach(() => {
    instances.length = 0;
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
    cleanup();
  });

  function renderPanel(onZoom: (r: unknown) => void) {
    const { container } = render(
      <ChartPanel option={{}} groupId="g" window={WINDOW} onZoom={onZoom} testId="chart-panel-x" />,
    );
    const el = container.querySelector('[data-testid="chart-panel-x"]');
    if (!(el instanceof HTMLElement)) throw new Error("chart-panel-x not found");
    return el;
  }

  it("pans on a genuine Ctrl-held left-button drag", () => {
    const onZoom = vi.fn();
    const el = renderPanel(onZoom);
    fireEvent.mouseDown(el, { ctrlKey: true, button: 0, buttons: 1, clientX: 100 });
    fireEvent.mouseMove(el, { ctrlKey: true, buttons: 1, clientX: 50 });
    vi.advanceTimersByTime(250);
    expect(onZoom).toHaveBeenCalledTimes(1);
  });

  it("a Ctrl+right-button-down does not arm the pan", () => {
    const onZoom = vi.fn();
    const el = renderPanel(onZoom);
    fireEvent.mouseDown(el, { ctrlKey: true, button: 2, buttons: 2, clientX: 100 });
    fireEvent.mouseMove(el, { ctrlKey: true, buttons: 2, clientX: 50 });
    vi.advanceTimersByTime(250);
    expect(onZoom).not.toHaveBeenCalled();
  });

  // MUTATION-PROOF regression for the stuck-pan-state bug a review found:
  // all three listeners live on this one container, and the ORIGINAL
  // onPanMove gated only on `panFrom !== null && e.ctrlKey` — never on
  // whether the button was actually still down. Sequence: Ctrl-drag, move
  // the pointer off the chart, release the button OUTSIDE it (this
  // container's own mouseup never fires for that release), move back over
  // with Ctrl still held. The first mousemove this element sees after that
  // is exactly what's fired below: ctrlKey true, buttons 0 (no button held)
  // -- the ORIGINAL code would still treat that as a live drag and call
  // onZoom against the stale drag-start snapshot; only a real button-state
  // check (`e.buttons`) can tell "still dragging" from "Ctrl is held but
  // nothing is pressed" apart. Removing ChartPanel's `(e.buttons & 1) === 0`
  // check must fail this test (see task-13-report.md for the mutation-check
  // evidence: reverting just that check turns this failure back on).
  it("does not resume panning after the button is released off-chart", () => {
    const onZoom = vi.fn();
    const el = renderPanel(onZoom);

    // A genuine drag first, so `panFrom` is populated the same way the
    // real bug required.
    fireEvent.mouseDown(el, { ctrlKey: true, button: 0, buttons: 1, clientX: 100 });
    fireEvent.mouseMove(el, { ctrlKey: true, buttons: 1, clientX: 90 });
    vi.advanceTimersByTime(250);
    expect(onZoom).toHaveBeenCalledTimes(1);
    onZoom.mockClear();

    // Released OFF this element -- its own mouseup never fires. The next
    // event it sees is a mousemove with the button no longer held.
    fireEvent.mouseMove(el, { ctrlKey: true, buttons: 0, clientX: 50 });
    vi.advanceTimersByTime(250);
    expect(onZoom).not.toHaveBeenCalled();

    // Move back over the chart, Ctrl still held, still no button down --
    // must not silently resume against the stale snapshot either.
    fireEvent.mouseMove(el, { ctrlKey: true, buttons: 0, clientX: 10 });
    vi.advanceTimersByTime(250);
    expect(onZoom).not.toHaveBeenCalled();
  });
});

// #863: the overlay's label layout is measured — plot width from the
// ResizeObserver, label widths from `measure` — and re-applied after every
// full option replacement, so a rebuild never leaves the default-width layout
// buildStackOption baked in.
describe("ChartPanel measured label layout (#863)", () => {
  const MARKERS: EventMarker[] = [
    // At 90% of the window: centred at 800 px, but pushed in from the right
    // edge at 296 px, so the offset differs between the two widths.
    {
      id: 1,
      label: "deploy",
      color: "#7c5cff",
      fromMs: 1_900_000,
      toMs: null,
      dash: "dash" as const,
    },
  ];
  const THEME = { muted: "#999", ink: "#111", surface: "#fff" };

  beforeEach(() => {
    instances.length = 0;
  });
  afterEach(() => {
    cleanup();
  });

  function panel(option: Record<string, unknown>, theme = THEME) {
    return (
      <ChartPanel
        option={option}
        groupId="g"
        window={WINDOW}
        markers={MARKERS}
        theme={theme}
        anchorSeriesId="s0"
        measure={approxMeasure}
        testId="chart-panel-x"
      />
    );
  }

  /** `xs[i]`, failing the test (rather than the type check) when absent. */
  function nth<T>(xs: readonly T[], i = 0): T {
    const x = xs[i];
    if (x === undefined) throw new Error(`no element ${i} in a list of ${xs.length}`);
    return x;
  }
  function overlayCalls(instance: FakeChart) {
    return instance.calls.filter((c) => (c.opt as { series?: unknown }).series !== undefined);
  }
  /** The label x-offset of the last overlay patch's only point marker. */
  function lastOffset(instance: FakeChart): number {
    const last = overlayCalls(instance).at(-1);
    if (last === undefined) throw new Error("no overlay patch applied");
    const series = (last.opt as { series: EventOverlay[] }).series;
    const datum = nth(nth(series).markLine.data as { label: { offset: [number, number] } }[]);
    return datum.label.offset[0];
  }
  function expectedOffset(plotWidthPx: number): number {
    const p = nth(layoutPlacements(MARKERS, WINDOW, plotWidthPx, approxMeasure));
    return p.leftPx - 0.9 * plotWidthPx;
  }

  it("resize re-lays out labels", () => {
    const { container } = render(panel({ series: [] }));
    expect(lastOffset(nth(instances))).toBeCloseTo(expectedOffset(DEFAULT_PLOT_WIDTH_PX));
    trigger(400); // plot = 400 - GRID.left - GRID.right = 296
    expect(400 - GRID.left - GRID.right).toBe(296);
    expect(lastOffset(nth(instances))).toBeCloseTo(expectedOffset(296));
    expect(expectedOffset(296)).not.toBeCloseTo(expectedOffset(DEFAULT_PLOT_WIDTH_PX));
    const el = container.querySelector('[data-testid="chart-panel-x"]') as HTMLElement;
    const p = nth(layoutPlacements(MARKERS, WINDOW, 296, approxMeasure));
    expect(JSON.parse(el.dataset["echartsLabelRects"] ?? "null")).toEqual([
      [p.lane, p.leftPx, p.widthPx],
    ]);
    expect(el.dataset["echartsPlotWidth"]).toBe("296");
  });

  it("fonts-ready re-lays out labels", async () => {
    let resolve: () => void = () => {};
    const ready = new Promise<void>((r) => {
      resolve = r;
    });
    Object.defineProperty(document, "fonts", { configurable: true, value: { ready } });
    try {
      render(panel({ series: [] }));
      const before = overlayCalls(nth(instances)).length;
      await act(async () => {
        resolve();
        await ready;
      });
      expect(overlayCalls(nth(instances)).length).toBeGreaterThan(before);
    } finally {
      delete (document as { fonts?: unknown }).fonts;
    }
  });

  it("mounts without document.fonts", () => {
    expect("fonts" in document).toBe(false);
    expect(() => render(panel({ series: [] }))).not.toThrow();
  });

  it("an option-only change re-applies the measured layout", () => {
    const { rerender } = render(panel({ series: [], rev: 1 }));
    trigger(400);
    rerender(panel({ series: [], rev: 2 }));
    const calls = nth(instances).calls;
    const lastReplace = calls.map((c) => c.opts?.notMerge === true).lastIndexOf(true);
    expect((nth(calls, lastReplace).opt as { rev: number }).rev).toBe(2);
    const after = calls.slice(lastReplace + 1);
    expect(after.some((c) => (c.opt as { series?: unknown }).series !== undefined)).toBe(true);
    expect(lastOffset(nth(instances))).toBeCloseTo(expectedOffset(296));
  });

  it("a theme surface change re-applies the overlay with the new halo", () => {
    const option = { series: [] }; // same identity across renders: only the theme moves
    const { rerender } = render(panel(option));
    const before = overlayCalls(nth(instances)).length;
    rerender(panel(option, { ...THEME, surface: "#030712" }));
    const calls = overlayCalls(nth(instances));
    expect(calls.length).toBeGreaterThan(before);
    const series = (nth(calls, calls.length - 1).opt as { series: EventOverlay[] }).series;
    const datum = nth(nth(series).markLine.data as { label: { textBorderColor: string } }[]);
    expect(datum.label.textBorderColor).toBe("#030712");
  });
});

// #920: a crowded cluster's " (+N)" label carries a transparent button that
// lists the hidden events. Its geometry is the canvas layout's own.
describe("ChartPanel cluster overflow (#920)", () => {
  /** Five coincident points at `atMs`, ids from `firstId`: three visible,
   * the last two counted on the lane-2 label (id `firstId + 2`). */
  const cluster = (firstId: number, atMs: number): EventMarker[] =>
    ["test start", "collect logs", "test pass", "test fail", "teardown"].map((label, i) => ({
      id: firstId + i,
      label,
      color: "#888888",
      fromMs: atMs + i,
      toMs: null,
      dash: "dash" as const,
    }));
  const THEME = { muted: "#999", ink: "#111", surface: "#fff" };

  beforeEach(() => {
    instances.length = 0;
  });
  afterEach(() => {
    cleanup();
  });

  /** `xs[i]`, failing the test (rather than the type check) when absent. */
  function nth<T>(xs: readonly T[], i = 0): T {
    const x = xs[i];
    if (x === undefined) throw new Error(`no element ${i} in a list of ${xs.length}`);
    return x;
  }
  function panel(
    markers: EventMarker[],
    extra: { onEventOpen?: (id: number) => void; measure?: MeasureText } = {},
  ) {
    return (
      <ChartPanel
        option={{ series: [] }}
        groupId="g"
        window={WINDOW}
        markers={markers}
        theme={THEME}
        anchorSeriesId="s0"
        measure={extra.measure ?? approxMeasure}
        onEventOpen={extra.onEventOpen}
        testId="chart-panel-x"
      />
    );
  }
  function button(id: number): HTMLElement {
    return screen.getByTestId(`cluster-overflow-${id}`);
  }
  /** The placement `layoutWith` gives marker `id`, at plot width `width`. */
  function placementOf(markers: EventMarker[], id: number, width: number, measure: MeasureText) {
    return nth(layoutWith(markers, WINDOW, width, measure).filter((l) => l.placement.id === id))
      .placement;
  }

  it("renders one overflow button per host, named by its hidden labels", () => {
    render(panel([...cluster(1, 1_200_000), ...cluster(11, 1_700_000)]));
    const buttons = screen.getAllByTestId(/^cluster-overflow-\d+$/u);
    expect(buttons.map((b) => b.dataset["testid"])).toEqual([
      "cluster-overflow-3",
      "cluster-overflow-13",
    ]);
    // The name starts with the label's visible text (WCAG 2.5.3).
    expect(button(3).getAttribute("aria-label")).toBe(
      "test pass (+2): 2 more events: test fail, teardown",
    );
    const p = placementOf(cluster(1, 1_200_000), 3, DEFAULT_PLOT_WIDTH_PX, approxMeasure);
    expect(button(3).style.left).toBe(`${GRID.left + p.leftPx}px`);
    expect(button(3).style.top).toBe(`${GRID.top + laneTopPx(p.lane)}px`);
    expect(button(3).style.width).toBe(`${p.widthPx}px`);
    expect(button(3).style.height).toBe(`${LANE_LABEL_STEP_PX}px`);
  });

  it("names one hidden event in the singular, and at most ten by label", () => {
    const one = cluster(1, 1_200_000).slice(0, 4);
    const { unmount } = render(panel(one));
    expect(button(3).getAttribute("aria-label")).toBe("test pass (+1): 1 more event: test fail");
    unmount();
    // Three visible, eleven hidden: ten are named, the eleventh is counted.
    const many: EventMarker[] = Array.from({ length: 14 }, (_, i) => ({
      id: i + 1,
      label: `e${i + 1}`,
      color: "#888888",
      fromMs: 1_200_000 + i,
      toMs: null,
      dash: "dash" as const,
    }));
    render(panel(many));
    expect(button(3).getAttribute("aria-label")).toBe(
      "e3 (+11): 11 more events: e4, e5, e6, e7, e8, e9, e10, e11, e12, e13, and 1 more",
    );
  });

  it("the popover's items open the editor only when onEventOpen is given", async () => {
    const user = userEvent.setup();
    const onEventOpen = vi.fn();
    // One id-less (synthetic, negative id) marker among the hidden ones.
    const markers = cluster(1, 1_200_000).map((m) => (m.id === 5 ? { ...m, id: -1 } : m));
    const { rerender } = render(panel(markers));
    await user.click(button(3));
    const list = await screen.findByTestId("cluster-overflow-list");
    expect(list.textContent).toContain("test fail");
    expect(list.textContent).toContain("teardown");
    // Each row leads with its 24-hour clock time.
    expect(list.textContent).toContain(`${formatClockTime(1_200_003)}test fail`);
    // Read-only: every item is plain text.
    expect(list.querySelectorAll("button")).toHaveLength(0);
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByTestId("cluster-overflow-list")).toBeNull());

    rerender(panel(markers, { onEventOpen }));
    await user.click(button(3));
    await screen.findByTestId("cluster-overflow-list");
    // The id-less marker is listed but never offered as editable.
    expect(screen.getByTestId("cluster-overflow-item--1").tagName).toBe("SPAN");
    const item = screen.getByTestId("cluster-overflow-item-4");
    expect(item.tagName).toBe("BUTTON");
    await user.click(item);
    expect(onEventOpen).toHaveBeenCalledWith(4);
    await waitFor(() => expect(screen.queryByTestId("cluster-overflow-list")).toBeNull());
  });

  it("overflow buttons move with the labels on resize and font load", async () => {
    // A measurer whose widths change once "fonts" load, as a web font's do.
    let scale = 6;
    const measure: MeasureText = (t) => t.length * scale;
    let resolve: () => void = () => {};
    const ready = new Promise<void>((r) => {
      resolve = r;
    });
    Object.defineProperty(document, "fonts", { configurable: true, value: { ready } });
    try {
      const markers = cluster(1, 1_500_000);
      render(panel(markers, { measure }));
      const leftAt = (width: number) =>
        `${GRID.left + placementOf(markers, 3, width, measure).leftPx}px`;
      expect(button(3).style.left).toBe(leftAt(DEFAULT_PLOT_WIDTH_PX));
      trigger(400); // plot = 400 - GRID.left - GRID.right = 296 px
      expect(button(3).style.left).toBe(leftAt(296));
      const before = button(3).style.left;
      expect(before).not.toBe(leftAt(DEFAULT_PLOT_WIDTH_PX));
      scale = 8;
      await act(async () => {
        resolve();
        await ready;
      });
      expect(button(3).style.left).toBe(leftAt(296));
      expect(button(3).style.left).not.toBe(before);
    } finally {
      delete (document as { fonts?: unknown }).fonts;
    }
  });

  it("a drag on the plot outside a button still brushes", () => {
    const onZoom = vi.fn();
    render(
      <ChartPanel
        option={{ series: [] }}
        groupId="g"
        window={WINDOW}
        markers={cluster(1, 1_500_000)}
        theme={THEME}
        anchorSeriesId="s0"
        measure={approxMeasure}
        onZoom={onZoom}
        testId="chart-panel-x"
      />,
    );
    // The overlay layer takes no pointer events; only its buttons do. jsdom
    // has no hit testing, so the classes are the contract here (the browser
    // lane checks the real hit with elementFromPoint).
    const overlay = button(3).parentElement;
    expect(overlay?.className).toContain("pointer-events-none");
    // Exactly one element per panel carries the panel's testid prefix.
    expect(document.querySelectorAll('[data-testid^="chart-panel-"]')).toHaveLength(1);
    expect(button(3).className).toContain("pointer-events-auto");
    // The ECharts element, not the wrapper, still carries the stamps, and a
    // brush over the plot still routes to onZoom.
    expect(screen.getByTestId("chart-panel-x").dataset["echartsMarkerCount"]).toBe("5");
    nth(instances).emit("brushEnd", { areas: [{ coordRange: [1_200_000, 1_800_000] }] });
    expect(onZoom).toHaveBeenCalledWith({ from: 1_200_000, to: 1_800_000 });
  });
});
