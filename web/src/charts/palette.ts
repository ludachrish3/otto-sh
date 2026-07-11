// Categorical series palette — dataviz-skill reference instance, VALIDATED
// 2026-07-11 with validate_palette.js against otto's real surfaces:
//   light #ffffff: PASS (contrast WARN slots 2/3/7 -> relief = the labeled
//   series tree + tooltips, always present in the subject view)
//   dark  #030712: PASS, all >= 3:1 (CVD floor-band -> same relief)
// Slot ORDER is the CVD-safety mechanism (maximizes worst adjacent-pair
// ΔE) — NEVER reorder, NEVER cycle. A 9th series is never a generated
// color: charts render at most MAX_SERIES_PER_CHART series plus an
// overflow notice. Brand violet is deliberately absent (UI accent + the
// default event color; series must not impersonate either).

export const SERIES_LIGHT = [
  "#2a78d6", // 1 blue
  "#1baf7a", // 2 aqua
  "#eda100", // 3 yellow
  "#008300", // 4 green
  "#4a3aa7", // 5 violet-deep
  "#e34948", // 6 red
  "#e87ba4", // 7 magenta
  "#eb6834", // 8 orange
] as const;

export const SERIES_DARK = [
  "#3987e5",
  "#199e70",
  "#c98500",
  "#008300",
  "#9085e9",
  "#e66767",
  "#d55181",
  "#d95926",
] as const;

export const MAX_SERIES_PER_CHART = 8;
