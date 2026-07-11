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
  handlers = new Map<string, (e: unknown) => void>();
  setOption(opt: unknown) {
    this.options.push(opt);
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
