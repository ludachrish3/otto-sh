import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { parseExportDocument } from "../data/exportDoc";
import { buildSeriesTree } from "../data/seriesTree";
import { SeriesPanel } from "../pages/SeriesPanel";
import { focusSearchInput, registerSearchInput } from "../ui/searchFocus";

// jsdom (pinned here) doesn't implement `CSS.escape`
// (https://github.com/jsdom/jsdom/issues/3363), which react-aria's
// selection/focus utilities call unconditionally when focus moves into a
// collection (the chip TagGroups below) — without this, focusing a chip
// throws. Same polyfill as reviewbar.test.tsx / shell.test.tsx / rangepicker.test.tsx.
if (typeof globalThis.CSS === "undefined") {
  Object.defineProperty(globalThis, "CSS", {
    value: { escape: (value: string) => value.replace(/[^a-zA-Z0-9_-]/g, (ch) => `\\${ch}`) },
    writable: true,
  });
}

const HERE = dirname(fileURLToPath(import.meta.url));
const kitchen = parseExportDocument(
  readFileSync(join(HERE, "../../fixtures/kitchen-sink.json"), "utf-8"),
).sessions[0];

afterEach(cleanup);
afterEach(() => registerSearchInput(null));

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

  it("checkbox toggle reports the series key", async () => {
    // Untitled UI's Checkbox puts data-testid on the <label> it renders
    // (react-aria-components' Checkbox filters DOMProps onto the label, not
    // the visually-hidden <input> nested inside) — clicking the label
    // relies on native label->control forwarding, which jsdom does not
    // reliably replay for userEvent's realistic pointer-event sequence (it
    // works for a bare fireEvent.click dispatch in some cases but not here;
    // verified empirically). Query the actual checkbox role from inside the
    // testid'd wrapper and act on that, same pattern the brief prescribes
    // for a testid that lands on a wrapper rather than the interactive leaf.
    const user = userEvent.setup();
    const props = renderPanel();
    const checkbox = within(screen.getByTestId("series-node-CPU %")).getByRole("checkbox");
    await user.click(checkbox);
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

  it("Ctrl+A inside the chart-filter chips selects every chart key", async () => {
    // The chart-filter TagGroup is selectionMode="multiple", so react-aria's
    // useSelectableCollection wires Ctrl/Cmd+A to manager.selectAll(), which
    // calls onSelectionChange("all") — the string sentinel, not a Set. This
    // is a real keyboard interaction (not defensive TS narrowing) and must
    // land on every chart key, exercising SeriesPanel's `keys === "all"`
    // branch. fireEvent can't drive react-aria's press/selection handling;
    // only user-event's realistic pointer+keyboard sequencing does.
    const user = userEvent.setup();
    // Held as a local `vi.fn()` (rather than read back off `renderPanel`'s
    // return, whose type is widened to the plain callback signature by the
    // `...overrides` spread) so `.mock.calls` stays available for asserting
    // on the LAST call specifically.
    const onChips = vi.fn();
    const props = renderPanel({ onChips });

    // Click a chip first to move focus into the grid (clicking anywhere in
    // the Tag's children bubbles to its press handler — see file header).
    await user.click(screen.getByTestId("chip-cpu"));
    await user.keyboard("{Control>}a{/Control}");

    const allChartKeys = props.tree.map((c) => c.chartKey);
    expect(allChartKeys.length).toBeGreaterThan(1);
    const lastCall = onChips.mock.calls.at(-1);
    expect(lastCall?.[0]).toEqual(new Set(allChartKeys));
  });

  it("series search shows the / keycap and registers itself for the / shortcut", () => {
    renderPanel(); // the file's existing render helper — reuse it
    const input = screen.getByTestId("series-search") as HTMLInputElement;
    // The keycap is aria-hidden decoration NEXT to the input, inside the
    // same InputBase wrapper group.
    const wrapper = input.closest("div[class*='ring-1']");
    expect(wrapper?.textContent).toContain("/");
    // Registration: the / shortcut focuses this exact input.
    expect(document.activeElement).not.toBe(input);
    expect(focusSearchInput()).toBe(true);
    expect(document.activeElement).toBe(input);
  });
});
