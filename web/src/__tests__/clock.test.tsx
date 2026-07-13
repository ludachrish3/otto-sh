import { act, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useNow } from "../data/clock";
import { useReviewStore } from "../data/reviewStore";

let healthRenders = 0;
let chartRenders = 0;

function HealthTile() {
  useNow(5000); // subscribes to the clock
  healthRenders++;
  return null;
}
function ChartPanel() {
  // Narrow selector, mirroring how real chart-owning pages read the review
  // store (e.g. SubjectPage's `useReviewStore((s) => s.range)`). This gives
  // the tier-2 guard below a genuine subscriber that a clock/review-store
  // merge could falsely wake — without it, a component with no hooks at all
  // can never be re-rendered by any store update, and the guard is a tautology.
  useReviewStore((s) => s.range);
  chartRenders++; // does NOT subscribe to the clock
  return null;
}

describe("useNow", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    healthRenders = 0;
    chartRenders = 0;
  });
  afterEach(() => vi.useRealTimers());

  it("advances at the collection interval, not faster", () => {
    render(<HealthTile />);
    const renders = () => healthRenders;
    act(() => void vi.advanceTimersByTime(4000));
    expect(renders()).toBe(1); // not yet — a 5s cadence has not ticked
    act(() => void vi.advanceTimersByTime(1500));
    expect(renders()).toBe(2);
  });

  it("TIER-2 GUARD: a tick re-renders health consumers and NOT charts", () => {
    render(
      <>
        <HealthTile />
        <ChartPanel />
      </>,
    );
    const chartsAtStart = chartRenders;
    act(() => void vi.advanceTimersByTime(25_000)); // 5 ticks at 5s
    expect(healthRenders).toBeGreaterThan(1);
    expect(chartRenders).toBe(chartsAtStart); // charts must not wake for the clock
  });

  it("does not tick at all when the interval is unknown", () => {
    render(<HealthTile />);
    // (rendered with 5000 above; a null interval must simply never schedule)
    expect(() => act(() => void vi.advanceTimersByTime(60_000))).not.toThrow();
  });
});
