// Pins events.ts's pure span state machine + popover clamp/draft logic —
// ported from dashboard.js's `EventBox` class (§Event bar) and
// `openPopover()` (§Event popover).
import { describe, expect, it } from "vitest";

import type { MonitorEvent } from "../api/client";
import {
  clampPopoverPosition,
  DASH_OPTIONS,
  initialPopoverDraft,
  popoverDraftReducer,
  spanTransition,
  spanVisual,
} from "../events";

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

describe("DASH_OPTIONS (dashboard.html's static <option> list)", () => {
  it("matches the legacy source order", () => {
    expect(DASH_OPTIONS).toEqual(["dash", "dot", "solid", "longdash", "dashdot", "longdashdot"]);
  });
});

describe("spanVisual (dashboard.js's EventBox text/class for the span button)", () => {
  it.each([
    ["idle", "Start event", false, false],
    ["starting", "Start event", false, true],
    ["open", "End event", true, false],
    ["ending", "End event", true, true],
  ] as const)("phase=%s -> text=%s active=%s disabled=%s", (phase, text, active, disabled) => {
    expect(spanVisual(phase)).toEqual({ text, active, disabled });
  });
});

describe("spanTransition (dashboard.js's span-btn onAction start/end flow)", () => {
  it("idle -> starting -> open on a successful start", () => {
    let phase = spanTransition("idle", { type: "start_requested" });
    expect(phase).toBe("starting");
    phase = spanTransition(phase, { type: "start_succeeded" });
    expect(phase).toBe("open");
  });

  it("starting -> idle when the start POST fails (`!res.ok` / a thrown fetch error)", () => {
    expect(spanTransition("starting", { type: "start_failed" })).toBe("idle");
  });

  it("open -> ending -> idle on end", () => {
    let phase = spanTransition("open", { type: "end_requested" });
    expect(phase).toBe("ending");
    phase = spanTransition(phase, { type: "end_settled" });
    expect(phase).toBe("idle");
  });

  it("abandoned always resets to idle regardless of phase (dashboard.js's src.onerror)", () => {
    for (const phase of ["idle", "starting", "open", "ending"] as const) {
      expect(spanTransition(phase, { type: "abandoned" })).toBe("idle");
    }
  });

  it("is a no-op for an action that doesn't apply to the current phase", () => {
    // start_requested only fires from idle; end_requested only from open.
    expect(spanTransition("open", { type: "start_requested" })).toBe("open");
    expect(spanTransition("idle", { type: "end_requested" })).toBe("idle");
    expect(spanTransition("starting", { type: "end_requested" })).toBe("starting");
    expect(spanTransition("open", { type: "start_succeeded" })).toBe("open");
  });

  it("start_failed is a no-op once already open (the toolbar's finally-always-fires guard)", () => {
    // Mirrors EventToolbar's start handler: dispatching start_succeeded then
    // an unconditional start_failed in `finally` must not undo the success.
    let phase = spanTransition("idle", { type: "start_requested" });
    phase = spanTransition(phase, { type: "start_succeeded" });
    phase = spanTransition(phase, { type: "start_failed" });
    expect(phase).toBe("open");
  });
});

describe("clampPopoverPosition (dashboard.js's openPopover() viewport-clamp math)", () => {
  it("places the popover past the click point with the default margin when it fits", () => {
    expect(clampPopoverPosition(100, 100, 240, 140, 1200, 800)).toEqual({ left: 108, top: 108 });
  });

  it("flips to the left of the cursor when the right edge would overflow", () => {
    // click near the right edge: 1150 + 240 + 8 > 1200 viewport width
    expect(clampPopoverPosition(1150, 100, 240, 140, 1200, 800)).toEqual({
      left: 1150 - 240 - 8,
      top: 108,
    });
  });

  it("flips above the cursor when the bottom edge would overflow", () => {
    expect(clampPopoverPosition(100, 750, 240, 140, 1200, 800)).toEqual({
      left: 108,
      top: 750 - 140 - 8,
    });
  });

  it("clamps to a 0px floor when even the flipped position would go negative (popover wider/taller than the available space on either side)", () => {
    expect(clampPopoverPosition(50, 50, 240, 200, 250, 200)).toEqual({ left: 0, top: 0 });
  });

  it("honors a custom margin", () => {
    expect(clampPopoverPosition(100, 100, 240, 140, 1200, 800, 20)).toEqual({ left: 120, top: 120 });
  });
});

describe("popoverDraftReducer (dashboard.js's popover-label/-color/-dash field seeding + onChange)", () => {
  it("seed copies label/color/dash from the clicked event", () => {
    const draft = popoverDraftReducer(initialPopoverDraft, {
      type: "seed",
      event: event({ label: "Router rebooted", color: "#ff0000", dash: "dot" }),
    });
    expect(draft).toEqual({ label: "Router rebooted", color: "#ff0000", dash: "dot" });
  });

  it("seed on a second click replaces (does not merge with) any unsaved edit — legacy has no 'already open' guard", () => {
    const edited = popoverDraftReducer(initialPopoverDraft, { type: "label", value: "unsaved draft" });
    const reseeded = popoverDraftReducer(edited, { type: "seed", event: event({ label: "fresh" }) });
    expect(reseeded.label).toBe("fresh");
  });

  it("label/color/dash each update only their own field", () => {
    const seeded = popoverDraftReducer(initialPopoverDraft, {
      type: "seed",
      event: event({ label: "L", color: "#abcdef", dash: "dot" }),
    });
    const labelChanged = popoverDraftReducer(seeded, { type: "label", value: "new label" });
    expect(labelChanged).toEqual({ label: "new label", color: "#abcdef", dash: "dot" });

    const colorChanged = popoverDraftReducer(seeded, { type: "color", value: "#000000" });
    expect(colorChanged).toEqual({ label: "L", color: "#000000", dash: "dot" });

    const dashChanged = popoverDraftReducer(seeded, { type: "dash", value: "solid" });
    expect(dashChanged).toEqual({ label: "L", color: "#abcdef", dash: "solid" });
  });
});
