import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

globalThis.ResizeObserver ??= class {
  observe() {}
  unobserve() {}
  disconnect() {}
} as unknown as typeof ResizeObserver;

const instances: FakeChart[] = [];

class FakeChart {
  group = "";
  disposed = false;
  options: unknown[] = [];
  /** Full (option, opts) pairs — `options` above keeps only the option arg for
   * the legacy lifecycle assertions; this also captures the setOption opts so
   * tests can pin notMerge/lazyUpdate. */
  calls: { opt: unknown; opts: Record<string, unknown> | undefined }[] = [];
  handlers = new Map<string, (e: unknown) => void>();
  /** Recorded `dispatchAction` payloads — Task 12's brush arming/clearing. */
  dispatched: Record<string, unknown>[] = [];
  setOption(opt: unknown, opts?: Record<string, unknown>) {
    this.options.push(opt);
    this.calls.push({ opt, opts });
  }
  on(event: string, cb: (e: unknown) => void) {
    this.handlers.set(event, cb);
  }
  dispatchAction(payload: Record<string, unknown>) {
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

import { ChartPanel } from "../charts/ChartPanel";

const WINDOW = { from: 1_000_000, to: 2_000_000 };

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

  function arms(instance: FakeChart) {
    return instance.dispatched.filter((d) => d.type === "takeGlobalCursor" && d.key === "brush");
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
    const clears = instances[0].dispatched.filter((d) => d.type === "brush");
    expect(clears).toHaveLength(1);
    expect(clears[0].areas).toEqual([]);
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
    const clears = instances[0].dispatched.filter((d) => d.type === "brush");
    expect(clears).toHaveLength(1);
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
