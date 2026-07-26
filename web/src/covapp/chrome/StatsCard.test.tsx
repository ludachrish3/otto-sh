// StatsCard's contract (task-3 brief): tier x Line/Branch/Decision matrix,
// with the "all" row emphasized and a null decision rendering muted "no
// data" rather than a bogus 0%.
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { TierStatRow } from "./StatsCard";
import { StatsCard } from "./StatsCard";

afterEach(() => {
  cleanup();
});

const thresholds = { high: 80, medium: 70 };

function rows(): TierStatRow[] {
  return [
    {
      key: "system",
      label: "System (e2e)",
      dotColor: "green",
      line: [289, 402],
      branch: [51, 92],
      decision: [22, 44],
    },
    {
      key: "unit",
      label: "Unit",
      dotColor: "blue",
      line: [120, 402],
      branch: [36, 92],
      decision: null,
    },
    {
      key: "all",
      label: "All tiers",
      line: [318, 402],
      branch: [66, 92],
      decision: [30, 44],
    },
  ];
}

describe("StatsCard", () => {
  it("renders the card and one row per tier, keyed by tier", () => {
    render(
      <StatsCard
        scope="acme-fw"
        title="Coverage — this file"
        rows={rows()}
        thresholds={thresholds}
      />,
    );
    expect(screen.getByTestId("stats-card")).toBeTruthy();
    expect(screen.getByTestId("stats-row-system").textContent).toContain("System (e2e)");
    expect(screen.getByTestId("stats-row-unit").textContent).toContain("Unit");
  });

  it("renders the scope and title", () => {
    render(
      <StatsCard
        scope="src/net/tcp.c"
        title="Coverage — this file"
        rows={rows()}
        thresholds={thresholds}
      />,
    );
    const card = screen.getByTestId("stats-card");
    expect(card.textContent).toContain("src/net/tcp.c");
    expect(card.textContent).toContain("Coverage — this file");
  });

  it("emphasizes the last 'all' row distinctly from tier rows", () => {
    render(<StatsCard scope="acme-fw" title="t" rows={rows()} thresholds={thresholds} />);
    const allRow = screen.getByTestId("stats-row-all");
    expect(allRow.textContent).toContain("All tiers");
    const tierRow = screen.getByTestId("stats-row-system");
    expect(allRow.className).not.toBe(tierRow.className);
  });

  it("shows hit/total fractions and formatted percentages for each stat type", () => {
    render(<StatsCard scope="acme-fw" title="t" rows={rows()} thresholds={thresholds} />);
    const row = screen.getByTestId("stats-row-system");
    expect(row.textContent).toContain("289/402");
    expect(row.textContent).toContain("71.9%"); // 289/402 * 100 = 71.89...
  });

  it("renders muted 'no data' for a null decision cell instead of a percentage", () => {
    render(<StatsCard scope="acme-fw" title="t" rows={rows()} thresholds={thresholds} />);
    const row = screen.getByTestId("stats-row-unit");
    expect(row.textContent).toContain("no data");
  });

  it("renders a tier dot swatch when dotColor is provided", () => {
    render(<StatsCard scope="acme-fw" title="t" rows={rows()} thresholds={thresholds} />);
    const row = screen.getByTestId("stats-row-system");
    const dot = row.querySelector("[data-testid='tier-dot']");
    expect(dot).toBeTruthy();
  });

  it("renders without crashing when rows is empty (data-less store)", () => {
    render(<StatsCard scope="acme-fw" title="t" rows={[]} thresholds={thresholds} />);
    expect(screen.getByTestId("stats-card")).toBeTruthy();
  });

  it("renders muted 'no data' for a null branch cell instead of a bogus 0% (Task 7 focused row)", () => {
    const focusedRows: TierStatRow[] = [
      {
        key: "ctx",
        label: "nightly-full",
        dotColor: "green",
        line: [289, 402],
        branch: null,
        decision: null,
      },
    ];
    render(
      <StatsCard
        scope="focused: nightly-full"
        title="t"
        rows={focusedRows}
        thresholds={thresholds}
      />,
    );
    const row = screen.getByTestId("stats-row-ctx");
    // Both nullable cells render the same muted text — exactly two, one per
    // column — never a "0.0%" placeholder.
    expect(row.textContent?.match(/no data/g)?.length).toBe(2);
  });

  it("defaults the first column header to 'Tier'", () => {
    render(<StatsCard scope="acme-fw" title="t" rows={rows()} thresholds={thresholds} />);
    expect(screen.getByText("Tier")).toBeTruthy();
  });

  it("uses keyColumnLabel to relabel the first column header (Task 7 focused variant)", () => {
    render(
      <StatsCard
        scope="focused: nightly-full"
        title="t"
        rows={rows()}
        thresholds={thresholds}
        keyColumnLabel="Context"
      />,
    );
    expect(screen.getByText("Context")).toBeTruthy();
    expect(screen.queryByText("Tier")).toBeNull();
  });
});
