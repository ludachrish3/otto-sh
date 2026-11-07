// The one owner of how an event marker is styled: the dash pattern and the
// span's faint fill. The chart (options.ts) and the editor's colour previews
// (ui/EventColorField.tsx) both draw from here, so a preview can never show
// a dash or a fill the chart does not.
import type { EventDash } from "../api/eventStyle.gen";

type MarkerDash = "solid" | number[];

/** Every event marker's line width, in px: a point's line and a span's border. */
export const MARKER_LINE_WIDTH = 1;

/** Maps each registry dash style (api/eventStyle.gen.ts EVENT_DASH_STYLES)
 * onto a dash pattern, in px at MARKER_LINE_WIDTH. ECharts' lineStyle.type /
 * itemStyle.borderType takes it as is (zrender passes an array through
 * unscaled), and an SVG preview joins it into stroke-dasharray -- so this
 * one table decides the dash on both surfaces. `dot` and `dash` are what
 * ECharts' "dotted"/"dashed" names draw on a 1px line. Typed on EventDash,
 * so a style added to the registry fails the type check here until mapped. */
const DASH_PATTERNS: Record<EventDash, MarkerDash> = {
  solid: "solid",
  dot: [1],
  dash: [4, 2],
  longdash: [12, 6],
  dashdot: [8, 4, 2, 4],
  longdashdot: [14, 4, 2, 4],
};

/** The ECharts lineStyle.type / itemStyle.borderType for `dash`. */
export function dashType(dash: EventDash): MarkerDash {
  return DASH_PATTERNS[dash];
}

/** The SVG `stroke-dasharray` that draws `dash` the way the chart does;
 * `undefined` for a solid line. */
export function svgDashArray(dash: EventDash): string | undefined {
  const pattern = DASH_PATTERNS[dash];
  return pattern === "solid" ? undefined : pattern.join(" ");
}

/** A span's fill opacity: faint, so the series under it stays readable. */
export const SPAN_FILL_ALPHA = 0.12;

/** Exactly `#rrggbb` — the only shape `withAlpha` can append a byte to. */
const SIX_DIGIT_HEX = /^#[0-9a-fA-F]{6}$/u;

/** A #rrggbb hex with an alpha byte appended (#rrggbbaa). Used for a span's
 * faint fill so its dashed border can stay full-strength — itemStyle.opacity
 * would fade the border too, leaving the dash unreadable. Every event colour
 * is already `#rrggbb` (normalised where it enters the store,
 * data/eventStyle.ts, and the Custom… preview's is a serialised hex), so the
 * guard only lets any other string pass through unchanged rather than
 * appending a byte to it. */
export function withAlpha(color: string, alpha: number): string {
  if (!SIX_DIGIT_HEX.test(color)) return color;
  const byte = Math.round(alpha * 255)
    .toString(16)
    .padStart(2, "0");
  return `${color}${byte}`;
}
