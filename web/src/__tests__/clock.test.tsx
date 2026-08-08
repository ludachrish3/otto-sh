import { act, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useNow } from "../data/clock";
import { useReviewStore } from "../data/reviewStore";

let healthRenders = 0;
let chartRenders = 0;
let nullRenders = 0;

function HealthTile() {
  useNow(5000); // subscribes to the clock
  healthRenders++;
  return null;
}
function NullTile() {
  useNow(null); // subscribes, but an unknown interval must never schedule
  nullRenders++;
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
    nullRenders = 0;
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
    // Drives the null branch DIRECTLY — the old form rendered a 5000ms tile
    // (the null arm never ran) and asserted only not.toThrow(), which
    // advancing timers cannot do. If useNow(null) ever schedules — e.g. the
    // guard is dropped and setInterval(fn, null) fires at ~0ms cadence —
    // 60s of fake time produces a flood of ticks and re-renders.
    render(<NullTile />);
    expect(nullRenders).toBe(1);
    act(() => void vi.advanceTimersByTime(60_000));
    expect(nullRenders).toBe(1); // still exactly the mount render
  });
});
