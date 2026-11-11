/* AUTO-GENERATED from src/otto/models/monitor.py (the event-style registry) — run scripts/gen_web_types.sh; do not edit. */

export interface EventColorPreset {
  readonly id: string;
  readonly name: string;
  readonly hex: string;
}

export const EVENT_COLOR_PRESETS: readonly EventColorPreset[] = [
  { id: "neutral", name: "Grey", hex: "#888888" },
  { id: "annotation", name: "Violet", hex: "#7c5cff" },
  { id: "success", name: "Green", hex: "#2ca02c" },
  { id: "failure", name: "Red", hex: "#d62728" },
];

export const DEFAULT_EVENT_COLOR = "#888888";

export const LIFECYCLE_COLOR_IDS = {
  start: "neutral",
  pass: "success",
  fail: "failure",
} as const;

export const EVENT_DASH_STYLES = [
  "solid",
  "dot",
  "dash",
  "longdash",
  "dashdot",
  "longdashdot",
] as const;

export type EventDash = (typeof EVENT_DASH_STYLES)[number];

export const DEFAULT_EVENT_DASH: EventDash = "dash";

export const LIFECYCLE_DASHES = {
  start: "dash",
  pass: "solid",
  fail: "solid",
} as const satisfies Record<keyof typeof LIFECYCLE_COLOR_IDS, EventDash>;

/** Unanchored: apply it as a whole-string match, `^(?:…)$`. */
export const EVENT_COLOR_PATTERN = "#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})";
