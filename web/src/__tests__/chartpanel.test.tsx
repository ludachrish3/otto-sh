import { cleanup, render } from "@testing-library/react";
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
  setOption(opt: unknown, opts?: Record<string, unknown>) {
    this.options.push(opt);
    this.calls.push({ opt, opts });
  }
  on(event: string, cb: (e: unknown) => void) {
    this.handlers.set(event, cb);
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
