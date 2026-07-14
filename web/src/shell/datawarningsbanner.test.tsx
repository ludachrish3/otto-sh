// Plan 5b final-review Finding [1]: `state.warnings` had no render site —
// "drop and warn" was, in practice, "drop silently." These tests exercise
// the component in isolation via direct store manipulation (mirrors
// subjecthealthbanner.test.tsx's pattern); shell.test.tsx separately proves
// the wiring end-to-end through a real Import.
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { useReviewStore } from "../data/reviewStore";
import { DataWarningsBanner } from "./DataWarningsBanner";

function resetStore() {
  useReviewStore.setState({
    sessions: [],
    rawMonitorSessions: null,
    sourceName: null,
    warnings: [],
    importError: null,
    activeSessionId: null,
    range: null,
    mode: null,
    connection: "connecting",
    windowMs: 900_000,
  });
}

afterEach(() => {
  cleanup();
  resetStore();
});

describe("DataWarningsBanner", () => {
  it("renders nothing when there are no warnings", () => {
    render(<DataWarningsBanner />);
    expect(screen.queryByTestId("data-warnings-banner")).toBeNull();
  });

  it("surfaces a dropped-row warning with a count", () => {
    useReviewStore.setState({
      warnings: ["session s1: dropped 1 metric with invalid timestamp"],
    });
    render(<DataWarningsBanner />);
    const banner = screen.getByTestId("data-warnings-banner");
    expect(banner.textContent).toContain("1 data warning");
    expect(banner.textContent).toContain("dropped 1 metric with invalid timestamp");
  });

  it("accumulates: a second warning grows the count without a second banner", () => {
    useReviewStore.setState({
      warnings: ["session s1: dropped 1 metric with invalid timestamp"],
    });
    render(<DataWarningsBanner />);
    expect(screen.getByTestId("data-warnings-banner").textContent).toContain("1 data warning");

    act(() => {
      useReviewStore.setState({
        warnings: [
          "session s1: dropped 1 metric with invalid timestamp",
          "session s1: dropped 2 events with invalid timestamp",
        ],
      });
    });
    expect(screen.getAllByTestId("data-warnings-banner")).toHaveLength(1);
    expect(screen.getByTestId("data-warnings-banner").textContent).toContain("2 data warnings");
  });

  it("dismissing hides the banner", () => {
    useReviewStore.setState({
      warnings: ["session s1: dropped 1 metric with invalid timestamp"],
    });
    render(<DataWarningsBanner />);
    fireEvent.click(screen.getByTestId("data-warnings-dismiss"));
    expect(screen.queryByTestId("data-warnings-banner")).toBeNull();
  });

  it("does not re-nag when re-rendered with the SAME warnings after dismissal", () => {
    useReviewStore.setState({
      warnings: ["session s1: dropped 1 metric with invalid timestamp"],
    });
    const { rerender } = render(<DataWarningsBanner />);
    fireEvent.click(screen.getByTestId("data-warnings-dismiss"));
    expect(screen.queryByTestId("data-warnings-banner")).toBeNull();

    // A re-render with the exact same warnings array (e.g. an unrelated
    // store update, or a heartbeat fragment that dropped nothing new) must
    // not resurrect the dismissed banner.
    rerender(<DataWarningsBanner />);
    expect(screen.queryByTestId("data-warnings-banner")).toBeNull();
  });

  it("a fresh warning after dismissal shows again", () => {
    useReviewStore.setState({
      warnings: ["session s1: dropped 1 metric with invalid timestamp"],
    });
    render(<DataWarningsBanner />);
    fireEvent.click(screen.getByTestId("data-warnings-dismiss"));
    expect(screen.queryByTestId("data-warnings-banner")).toBeNull();

    // A NEW warning lands (e.g. the next live fragment drops a bad event) —
    // this must resurface the banner, scoped to only the new entry.
    act(() => {
      useReviewStore.setState({
        warnings: [
          "session s1: dropped 1 metric with invalid timestamp",
          "session s1: dropped 1 event with invalid timestamp",
        ],
      });
    });
    const banner = screen.getByTestId("data-warnings-banner");
    expect(banner.textContent).toContain("1 data warning");
    expect(banner.textContent).toContain("dropped 1 event with invalid timestamp");
    // The dismissed, previously-acknowledged entry is not repeated.
    expect(banner.textContent).not.toContain("dropped 1 metric with invalid timestamp");
  });

  it("works for warnings pushed by the live-fragment path just as it does for import (both modes read the same channel)", () => {
    // No mode-specific branching in the component: it reads reviewStore's
    // one shared `warnings` field regardless of how it was populated
    // (importMonitorSessions/resyncMonitorSessions replace it wholesale;
    // appendFragment/appendFragments append to it — see reviewStore.ts).
    useReviewStore.setState({
      mode: "live",
      warnings: ["session s1: dropped 3 metrics with invalid timestamp"],
    });
    render(<DataWarningsBanner />);
    expect(screen.getByTestId("data-warnings-banner").textContent).toContain(
      "dropped 3 metrics with invalid timestamp",
    );
  });
});
