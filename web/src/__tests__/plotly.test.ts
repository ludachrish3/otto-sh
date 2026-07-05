// Pins plotly.ts's pure trace/layout/shape/annotation builders against the
// dashboard.js functions they port byte-for-byte: buildMetricTraces (name
// stripping, hovertemplates, meta text), buildShapes/buildAnnotations, and
// topMargin/legendRows (still individually exported/tested, but no longer
// fed into buildLayout's margin/height sizing — see below). buildLayout's
// height math itself was ported AS-IS from dashboard.js through Task 6/7;
// Task 10 replaces it with the fix for the height-growth bug (a proc/*
// legend/trace accumulating without bound used to grow `margin.b`/`height`
// forever) — margins and height are now hard CONSTANTS.
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import type { MonitorEvent } from "../api/client";
import type { Metric } from "../grouping";
import {
  buildAnnotations,
  buildLayout,
  buildMetricTraces,
  buildShapes,
  CONSTANT_CHART_HEIGHT_PX,
  FIXED_BOTTOM_MARGIN_PX,
  FIXED_TOP_MARGIN_PX,
  hexToRgba,
  legendRows,
  metaText,
  topMargin,
} from "../plotly";

beforeEach(() => {
  document.body.className = "";
});

afterEach(() => {
  document.body.className = "";
});

function event(overrides: Partial<MonitorEvent> = {}): MonitorEvent {
  return {
    id: 1,
    timestamp: "2026-07-02T00:00:00Z",
    label: "Reboot",
    source: "manual",
    color: "#112233",
    dash: "dash",
    end_timestamp: null,
    ...overrides,
  };
}

describe("metaText (dashboard.js metaText)", () => {
  it("joins entries with <br>", () => {
    expect(metaText({ pid: "7", cmd: "init" })).toBe("pid: 7<br>cmd: init");
  });

  it("is empty for null/undefined", () => {
    expect(metaText(null)).toBe("");
    expect(metaText(undefined)).toBe("");
  });
});

describe("buildMetricTraces (dashboard.js buildMetricTraces)", () => {
  const metrics: Metric[] = [{ label: "proc/init", chart: "cpu", y_title: "%", unit: "%" }];

  it("strips the 'proc/' prefix from the trace name only, not the series key", () => {
    const series = { "host1/proc/init": [{ ts: "t1", value: 1.5, meta: null }] };
    const [trace] = buildMetricTraces(metrics, series, "host1");
    expect(trace.name).toBe("init");
    expect(trace.x).toEqual(["t1"]);
    expect(trace.y).toEqual([1.5]);
  });

  it("falls back to the bare label (no host prefix) for historical data", () => {
    const series = { "proc/init": [{ ts: "t1", value: 1, meta: null }] };
    const [trace] = buildMetricTraces(metrics, series, null);
    expect(trace.x).toEqual(["t1"]);
  });

  it("uses the no-meta hovertemplate when no point carries meta", () => {
    const series = { "host1/proc/init": [{ ts: "t1", value: 1, meta: null }] };
    const [trace] = buildMetricTraces(metrics, series, "host1");
    expect(trace.hovertemplate).toBe("<b>init</b>: %{y:.2f}%<br>%{x}<extra></extra>");
    expect(trace.text).toBeUndefined();
  });

  it("switches to the meta hovertemplate and carries text once any point has meta", () => {
    const series = {
      "host1/proc/init": [
        { ts: "t1", value: 1, meta: null },
        { ts: "t2", value: 2, meta: { pid: "7" } },
      ],
    };
    const [trace] = buildMetricTraces(metrics, series, "host1");
    expect(trace.hovertemplate).toBe("<b>init</b>: %{y:.2f}%<br>%{text}<br>%{x}<extra></extra>");
    expect(trace.text).toEqual(["", "pid: 7"]);
  });

  it("renders an empty trace (no crash) for a metric with no data yet", () => {
    const [trace] = buildMetricTraces(metrics, {}, "host1");
    expect(trace.x).toEqual([]);
    expect(trace.y).toEqual([]);
  });

  it("always emits type scattergl / mode lines+markers (the gl2d-bundle contract)", () => {
    const [trace] = buildMetricTraces(metrics, {}, "host1");
    expect(trace.type).toBe("scattergl");
    expect(trace.mode).toBe("lines+markers");
    expect(trace.connectgaps).toBe(false);
  });
});

describe("hexToRgba (dashboard.js hexToRgba)", () => {
  it("converts a hex color + alpha to an rgba() string", () => {
    expect(hexToRgba("#112233", 0.12)).toBe("rgba(17,34,51,0.12)");
  });
});

describe("buildShapes (dashboard.js buildShapes)", () => {
  it("draws a single vertical line for an instantaneous event", () => {
    const shapes = buildShapes([event({ end_timestamp: null })]);
    expect(shapes).toHaveLength(1);
    expect(shapes[0]).toMatchObject({
      type: "line",
      x0: "2026-07-02T00:00:00Z",
      x1: "2026-07-02T00:00:00Z",
    });
  });

  it("draws a borderless fill rect + two edge lines for a span event", () => {
    const shapes = buildShapes([event({ end_timestamp: "2026-07-02T01:00:00Z" })]);
    expect(shapes).toHaveLength(3);
    expect(shapes[0]).toMatchObject({ type: "rect", fillcolor: "rgba(17,34,51,0.12)" });
    expect(shapes[1]).toMatchObject({ type: "line", x0: "2026-07-02T00:00:00Z" });
    expect(shapes[2]).toMatchObject({ type: "line", x0: "2026-07-02T01:00:00Z" });
  });
});

describe("buildAnnotations (dashboard.js buildAnnotations)", () => {
  it("one annotation per event, rotated -45deg, colored to match", () => {
    const [ann] = buildAnnotations([event({ label: "Reboot", color: "#abcdef" })]);
    expect(ann).toMatchObject({ text: "Reboot", textangle: -45, showarrow: false });
    expect((ann.font as { color: string }).color).toBe("#abcdef");
  });
});

describe("topMargin (dashboard.js topMargin)", () => {
  it("defaults to 36 with no events", () => {
    expect(topMargin([])).toBe(36);
  });

  it("grows with the longest event label, floored at 36", () => {
    expect(topMargin([event({ label: "x" })])).toBe(Math.max(36, Math.round(40 + 1 * 4)));
    expect(topMargin([event({ label: "a very long maintenance window label" })])).toBe(
      Math.round(40 + "a very long maintenance window label".length * 4),
    );
  });
});

describe("legendRows (dashboard.js legendRows)", () => {
  it("is 0 for a single named trace (no legend rendered)", () => {
    expect(legendRows([{ name: "a" }])).toBe(0);
  });

  it("is 0 with zero named traces", () => {
    expect(legendRows([{ name: "" }, { showlegend: false, name: "a" }])).toBe(0);
  });

  it("is 1 row for 2..ITEMS_PER_ROW named traces, ceil-dividing beyond that", () => {
    expect(legendRows([{ name: "a" }, { name: "b" }])).toBe(1);
    const seven = Array.from({ length: 7 }, (_, i) => ({ name: `t${i}` }));
    expect(legendRows(seven)).toBe(2);
  });
});

describe("buildLayout (dashboard.js buildLayout) — Task 10: height is now a hard CONSTANT", () => {
  it("margins and height are fixed regardless of trace/legend-row count (the height-growth fix)", () => {
    const single = buildLayout([{ name: "Overall CPU" }], { yaxisTitle: "%" }, []);
    expect(single.showlegend).toBe(false);
    expect(single.legend).toBeUndefined();
    expect((single.margin as { t: number; b: number }).t).toBe(FIXED_TOP_MARGIN_PX);
    expect((single.margin as { t: number; b: number }).b).toBe(FIXED_BOTTOM_MARGIN_PX);
    expect(single.height).toBe(CONSTANT_CHART_HEIGHT_PX);

    // A chart with a full 2-row legend's worth of named traces (12) — under
    // the legacy math this would have driven a taller bottom margin/height
    // than the single-trace case above; now it's byte-identical.
    const many = Array.from({ length: 12 }, (_, i) => ({ name: `t${i}` }));
    const legendFull = buildLayout(many, { yaxisTitle: "%" }, []);
    expect(legendFull.showlegend).toBe(true);
    expect((legendFull.margin as { b: number }).b).toBe(FIXED_BOTTOM_MARGIN_PX);
    expect(legendFull.height).toBe(CONSTANT_CHART_HEIGHT_PX);
    expect(legendFull.height).toBe(single.height);
  });

  it("top margin no longer grows with event label length (the height-growth fix)", () => {
    const noEvents = buildLayout([{ name: "a" }], { yaxisTitle: "%" }, []);
    const longLabelEvent = buildLayout([{ name: "a" }], { yaxisTitle: "%" }, [
      event({ label: "a very long maintenance window label indeed" }),
    ]);
    expect((longLabelEvent.margin as { t: number }).t).toBe(FIXED_TOP_MARGIN_PX);
    expect(longLabelEvent.height).toBe(CONSTANT_CHART_HEIGHT_PX);
    expect(longLabelEvent.height).toBe(noEvents.height);
  });

  it("still toggles showlegend/legend off the (already-capped) trace list's legend rows", () => {
    const layout = buildLayout([{ name: "a" }, { name: "b" }], { yaxisTitle: "%" }, []);
    expect(layout.showlegend).toBe(true);
    expect(layout.legend).toBeDefined();
  });

  it("uses the dark palette by default and the light palette with body.light", () => {
    const dark = buildLayout([], { yaxisTitle: "" }, []);
    expect(dark.paper_bgcolor).toBe("#0f1117");
    document.body.classList.add("light");
    const light = buildLayout([], { yaxisTitle: "" }, []);
    expect(light.paper_bgcolor).toBe("#f5f6fa");
  });
});
