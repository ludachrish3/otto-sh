import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { parseExportDocument } from "../data/exportDoc";
import { buildSeriesTree } from "../data/seriesTree";
import { SeriesPanel } from "../pages/SeriesPanel";

const HERE = dirname(fileURLToPath(import.meta.url));
const kitchen = parseExportDocument(
  readFileSync(join(HERE, "../../fixtures/kitchen-sink.json"), "utf-8"),
).sessions[0];

afterEach(cleanup);

function renderPanel(overrides: Partial<Parameters<typeof SeriesPanel>[0]> = {}) {
  const tree = buildSeriesTree(kitchen, "chassis-a_lc1");
  const props = {
    tree,
    checked: new Set(tree.flatMap((c) => c.series.map((s) => s.key))),
    onToggle: vi.fn(),
    search: "",
    onSearch: vi.fn(),
    chips: null as Set<string> | null,
    onChips: vi.fn(),
    source: null as string | null,
    onSource: vi.fn(),
    ...overrides,
  };
  render(<SeriesPanel {...props} />);
  return props;
}

describe("SeriesPanel", () => {
  it("renders a chip per chart and a source chip", () => {
    renderPanel();
    expect(screen.getByTestId("chip-cpu")).toBeTruthy();
    expect(screen.getByTestId("chip-source-mgmt-01")).toBeTruthy();
  });

  it("shows the series label for host-subject series", () => {
    renderPanel();
    const cpuRow = screen.getByTestId("series-node-CPU %").closest("li");
    expect(cpuRow?.textContent).toContain("CPU %");
    expect(cpuRow?.textContent).not.toContain("chassis-a_lc1");
  });

  it("checkbox toggle reports the series key", () => {
    const props = renderPanel();
    fireEvent.click(screen.getByTestId("series-node-CPU %"));
    expect(props.onToggle).toHaveBeenCalledWith("CPU %");
  });

  it("search box reports input", () => {
    const props = renderPanel();
    fireEvent.change(screen.getByTestId("series-search") as HTMLInputElement, {
      target: { value: "psu" },
    });
    expect(props.onSearch).toHaveBeenCalledWith("psu");
  });

  it("shows a source badge on externally-sourced series", () => {
    renderPanel();
    const node = screen.getByTestId("series-node-PSU Temp °C").closest("li");
    expect(node?.textContent).toContain("mgmt-01");
  });

  it("hides the source chip row when no external sources exist", () => {
    const tree = buildSeriesTree(kitchen, "db-01");
    renderPanel({ tree, checked: new Set() });
    expect(screen.queryByTestId("chip-source-mgmt-01")).toBeNull();
  });
});
