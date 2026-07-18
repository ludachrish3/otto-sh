import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { act } from "react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { useReviewStore } from "../data/reviewStore";
import { AppBar } from "../shell/AppBar";

beforeEach(() => {
  useReviewStore.setState({ mode: "live", connection: "live", range: null, windowMs: 900_000 });
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
  // The Live/Reconnecting distinction moved off `status-text` (deleted here,
  // Task 7, spec decision 9 — the status cluster is gone entirely, with no
  // replacement in THIS task) onto a dedicated Reconnecting banner (Task 9's
  // ReconnectingBanner.tsx, `reconnecting-banner` testid), which becomes the
  // `connection` state's one render site — see that task's
  // reconnectingbanner.test.tsx for the equivalent live/reconnecting
  // coverage this test used to provide.

  it("pause pins the view and resume returns to following", () => {
    render(<AppBar />);
    // `paused` is derived (mode === "live" && range !== null — see
    // reviewStore.ts's useIsPaused), not a stored field, so this asserts
    // through the toggle's own label/range effect rather than a `.paused`
    // property that no longer exists. Task 7 turned this into an icon-only
    // ButtonUtility glyph — the label now lives in `aria-label` (also the
    // tooltip text), not `.textContent` (there is no text node anymore, just
    // the icon).
    expect(screen.getByTestId("pause-toggle").getAttribute("aria-label")).toBe("Pause");
    fireEvent.click(screen.getByTestId("pause-toggle"));
    expect(useReviewStore.getState().range).not.toBeNull(); // frozen at an absolute window
    expect(screen.getByTestId("pause-toggle").getAttribute("aria-label")).toBe("Resume");
    fireEvent.click(screen.getByTestId("pause-toggle"));
    expect(useReviewStore.getState().range).toBeNull(); // following again
    expect(screen.getByTestId("pause-toggle").getAttribute("aria-label")).toBe("Pause");
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
    expect(screen.getByTestId("pause-toggle").getAttribute("aria-label")).toBe("Resume");
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

// The live-window ButtonGroup moved to SubjectPage's title row (Task 7,
// spec decision 10 — the presets only affect that page's chart windows) —
// its coverage moved with it, see subjectpage.test.tsx's
// "live window control" describe block.
