// Pure state-machine + UI-math logic for the event bar & popover, extracted
// the same way grouping.ts/plotly.ts are so the tricky bits are vitest'able
// without mounting React or touching the DOM. Ported from dashboard.js's
// §Event bar (the `EventBox` class + its two `onAction` instances) and
// §Event popover (`openPopover`'s viewport-clamp math + its field-seeding).
import type { MonitorEvent } from "./api/client";

/** The `<select>` options shared by `#event-dash`/`#span-dash`/`#popover-dash` — dashboard.html's static `<option>` list, in source order. */
export const DASH_OPTIONS = ["dash", "dot", "solid", "longdash", "dashdot", "longdashdot"] as const;

// ── Span button state machine ───────────────────────────────────────────────
// dashboard.js's spanEventBox has two *steady* states (no span open / a span
// open, keyed off `state.spanStartId`) plus two *transient* ones the source
// passes through while awaiting its POST calls. Critically, during those
// transient states the button is disabled but has NOT yet flipped its
// text/active class — that only happens once the awaited call settles (see
// the start branch: `state.spanStartId = ev.id` and `setButtonText('End
// event')` land in the same tick, but the end branch nulls `spanStartId`
// *before* awaiting, and only resets text/class in its `finally`). Modeling
// all four phases explicitly (rather than deriving purely from the store's
// `spanStartId`) is what lets "End event"/active stay visible-but-disabled
// for the length of the POST /end call, exactly like legacy.
export type SpanPhase = "idle" | "starting" | "open" | "ending";

export interface SpanVisual {
  text: "Start event" | "End event";
  active: boolean;
  disabled: boolean;
}

/** Button text/class/local-disable for a given phase — the connection-live gate is layered on top by the caller (`EventToolbar`). */
export function spanVisual(phase: SpanPhase): SpanVisual {
  switch (phase) {
    case "idle":
      return { text: "Start event", active: false, disabled: false };
    case "starting":
      return { text: "Start event", active: false, disabled: true };
    case "open":
      return { text: "End event", active: true, disabled: false };
    case "ending":
      return { text: "End event", active: true, disabled: true };
  }
}

export type SpanAction =
  | { type: "start_requested" }
  | { type: "start_succeeded" }
  | { type: "start_failed" }
  | { type: "end_requested" }
  | { type: "end_settled" }
  /** dashboard.js's `src.onerror`: the SSE layer abandoned an in-progress span out from under the button. */
  | { type: "abandoned" };

/** Transitions no-op (return `phase` unchanged) for an action that doesn't apply to the current phase — mirrors legacy's implicit guards (e.g. the end branch only ever runs from a click, which is only wired while a span is open). */
export function spanTransition(phase: SpanPhase, action: SpanAction): SpanPhase {
  switch (action.type) {
    case "start_requested":
      return phase === "idle" ? "starting" : phase;
    case "start_succeeded":
      return phase === "starting" ? "open" : phase;
    case "start_failed":
      return phase === "starting" ? "idle" : phase;
    case "end_requested":
      return phase === "open" ? "ending" : phase;
    case "end_settled":
      return phase === "ending" ? "idle" : phase;
    case "abandoned":
      return "idle";
  }
}

// ── Popover ──────────────────────────────────────────────────────────────

export interface PopoverPosition {
  left: number;
  top: number;
}

/**
 * dashboard.js's `openPopover()` viewport-clamp math: place the popover just
 * past the click point, flipping to the opposite side of the cursor on
 * whichever axis would overflow the viewport, then clamping to a 0px floor.
 */
export function clampPopoverPosition(
  clientX: number,
  clientY: number,
  popoverWidth: number,
  popoverHeight: number,
  viewportWidth: number,
  viewportHeight: number,
  margin = 8,
): PopoverPosition {
  let x = clientX + margin;
  let y = clientY + margin;
  if (x + popoverWidth > viewportWidth) x = clientX - popoverWidth - margin;
  if (y + popoverHeight > viewportHeight) y = clientY - popoverHeight - margin;
  return { left: Math.max(0, x), top: Math.max(0, y) };
}

export interface PopoverDraft {
  label: string;
  color: string;
  dash: string;
}

export const initialPopoverDraft: PopoverDraft = { label: "", color: "#888888", dash: DASH_OPTIONS[0] };

export type PopoverDraftAction =
  | { type: "seed"; event: MonitorEvent }
  | { type: "label"; value: string }
  | { type: "color"; value: string }
  | { type: "dash"; value: string };

/**
 * dashboard.js's `openPopover()` field-seeding (`popover-label/-color/-dash
 * .value = ev.*`) plus the three inputs' implicit onChange behavior as plain
 * form fields. `"seed"` re-copies from the clicked event every time — legacy
 * has no "already open for this id" special case, so a second click on the
 * same annotation clobbers any unsaved edit (see `EventPopover`'s comment).
 */
export function popoverDraftReducer(state: PopoverDraft, action: PopoverDraftAction): PopoverDraft {
  switch (action.type) {
    case "seed":
      return { label: action.event.label, color: action.event.color, dash: action.event.dash };
    case "label":
      return { ...state, label: action.value };
    case "color":
      return { ...state, color: action.value };
    case "dash":
      return { ...state, dash: action.value };
  }
}
