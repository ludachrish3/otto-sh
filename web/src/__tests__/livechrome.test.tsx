import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { act } from "react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { AppBar } from "../shell/AppBar";
import { useReviewStore } from "../data/reviewStore";

beforeEach(() => {
  useReviewStore.setState({ mode: "live", connection: "live", range: null });
});

// vitest's config doesn't set `test.globals: true`, so
// @testing-library/react's automatic afterEach(cleanup) registration never
// kicks in — without this, a previous test's <AppBar /> stays mounted and
// screen.getByTestId throws "found multiple elements" on the next render()
// (same fix as shell.test.tsx / reviewbar.test.tsx).
afterEach(() => {
  cleanup();
});

describe("live chrome", () => {
  it("shows Live when connected and Reconnecting when not", () => {
    render(<AppBar />);
    // `toHaveTextContent` is a jest-dom matcher; this project doesn't depend
    // on @testing-library/jest-dom (every other test file matches raw
    // `.textContent` instead — see shell.test.tsx, overview.test.tsx), so
    // this follows the same established pattern rather than adding a new
    // dependency for two assertions.
    expect(screen.getByTestId("status-text").textContent).toMatch(/live/i);
    act(() => {
      useReviewStore.setState({ connection: "disconnected" });
    });
    expect(screen.getByTestId("status-text").textContent).toMatch(/reconnect/i);
  });

  it("pause pins the view and resume returns to following", () => {
    render(<AppBar />);
    // `paused` is derived (mode === "live" && range !== null — see
    // reviewStore.ts's useIsPaused), not a stored field, so this asserts
    // through the toggle's own label/range effect rather than a `.paused`
    // property that no longer exists.
    expect(screen.getByTestId("pause-toggle").textContent).toBe("Pause");
    fireEvent.click(screen.getByTestId("pause-toggle"));
    expect(useReviewStore.getState().range).not.toBeNull(); // frozen at an absolute window
    expect(screen.getByTestId("pause-toggle").textContent).toBe("Resume");
    fireEvent.click(screen.getByTestId("pause-toggle"));
    expect(useReviewStore.getState().range).toBeNull(); // following again
    expect(screen.getByTestId("pause-toggle").textContent).toBe("Pause");
  });

  // Finding 2 (Plan 5b Task 9 review): pause/resume and "the user picked a
  // custom range" must be structurally the SAME state, or a chart drag-zoom
  // (SubjectPage's onZoom calls setRange(...) directly, bypassing
  // togglePause entirely) can leave a stored `paused` boolean disagreeing
  // with `range` — the toggle would then read "Pause" over an already-
  // pinned view, and clicking it would silently discard the user's zoomed
  // range in favor of a freshly computed window. Deriving `paused` from
  // `range` makes that impossible: any live-mode setRange, from ANY caller,
  // reads as paused immediately.
  it("a live setRange (drag-zoom) reads as paused; toggling resumes without inventing a new window", () => {
    render(<AppBar />);
    act(() => {
      useReviewStore.getState().actions.setRange({ from: 1_000, to: 2_000 });
    });
    expect(screen.getByTestId("pause-toggle").textContent).toBe("Resume");
    fireEvent.click(screen.getByTestId("pause-toggle"));
    // The resume branch only ever does `set({ range: null })` — if toggling
    // instead froze a NEW window (the pause branch), `range` would come back
    // non-null with different from/to, not null.
    expect(useReviewStore.getState().range).toBeNull();
  });

  it("hides pause in review mode", () => {
    useReviewStore.setState({ mode: "review" });
    render(<AppBar />);
    expect(screen.queryByTestId("pause-toggle")).toBeNull();
  });
});
