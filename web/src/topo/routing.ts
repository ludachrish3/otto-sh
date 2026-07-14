// Pure edge geometry: which face a line leaves from, and what shape it takes
// to get there. No React, no React Flow — so the occlusion invariant is a
// plain unit test (see __tests__/toporouting.test.ts).
//
// THE CONSTRAINT: React Flow renders edges BENEATH nodes. An edge that
// overlaps a node box does not cross it — it disappears behind it. Every
// decision below follows from that.
import { COL_W, ROW_H } from "./layout";

export interface Rect {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface EdgeGeometry {
  path: string;
  /** Apex of the curve — where a pill or hover card should sit. */
  labelX: number;
  labelY: number;
}

/** How far in from the box corner a bowed same-column edge anchors. Leaving
 * from the bottom *centre* instead would mean travelling half the box width
 * sideways before clearing it, and the gap to the next row is only 38px — the
 * curve physically cannot turn that fast. Leaning the anchor is what makes the
 * bow fit the EXISTING 112px gutter (COL_W minus the 208px element node —
 * see layout.ts's own COL_W comment), with no layout change. */
const LEAN_INSET = 20;

/** Cap on how far down (in px) the control points sit below their anchors.
 * This is what actually governs escape: with UNCAPPED control points at
 * dy/3 and 2*dy/3, the curve's initial direction is (bow, dy/3) — so the
 * further apart the two nodes are, the more the curve leaves the anchor
 * heading DOWN the span (toward the next box) rather than sideways (into
 * the gutter), and the fixed 38px inter-row gap can't absorb that. Capping
 * the control points' vertical offset makes the curve turn out sideways
 * immediately, independent of how many rows it spans. */
const CTRL_Y_MAX = 28;

/** Bow, in px — deliberately NOT scaled by node width. The anchor is always
 * leaned to a fixed LEAN_INSET from the box corner, so the horizontal
 * distance still left to clear is LEAN_INSET + margin regardless of how
 * wide the box is; a wider box just means the anchor itself sits further
 * along the shared face. Clamped per-edge to what the gutter can hold, see
 * `maxBulge` in routeSameColumn.
 *
 * 112 is the measured minimum that clears every intervening box for row spans
 * up to 20, plus a small margin. */
const BOW_BASE = 112;

/** Extra bow per parallel index. OUTWARD ONLY: a centred fan pushes the inner
 * sibling back under the very box the bow exists to clear.
 *
 * This is a CEILING, not the step actually used — routeSameColumn spreads the
 * group across whatever bow range the gutter leaves, so a big group fans
 * tighter rather than colliding. The floor that matters is INTERACTION_WIDTH/2
 * (below): fall under it and the inner edge loses its pointer target entirely. */
const FAN_STEP = 28;

/** React Flow gives every edge a 20px-wide invisible `react-flow__edge-interaction`
 * path — that, not the 2px visible stroke, is what the pointer actually hits.
 * Two parallel edges whose centrelines are closer than half of it share one hit
 * target: the one painted last wins, and the other becomes unclickable, its
 * inspector and hover card unreachable. This is not a cosmetic threshold, and
 * `toporouting.test.ts` pins it. */
export const INTERACTION_WIDTH = 20;

/** How much of the gutter to leave unused on the far side when clamping the
 * bow — keeps the curve's apex from touching the next column's boxes. */
const GUTTER_MARGIN = 8;

/** Perpendicular fan for cross-column parallel edges — unchanged behaviour
 * (the old `curvature * FAN_SCALE`, i.e. 0.35 * 70). */
const CROSS_FAN = 24.5;

/** Horizontal separation between parallel adjacent-row links (`rowSpan <= 1`
 * in `routeSameColumn`), comfortably above `INTERACTION_WIDTH / 2` so each
 * keeps its own pointer target. This is the SAME invariant #131 fixed for
 * the bowed multi-row case (`FAN_STEP`) — it just went unneeded here until a
 * layout could plant two heavily-linked peers in adjacent rows of one
 * column instead of far-apart ones. Without it, every parallel link in the
 * group draws the identical face-centre line, and only the last one painted
 * is ever clickable. */
const ADJACENT_FAN = 28;

/** A cubic with both interior control points offset by k bulges only 0.75k. */
const BULGE = 0.75;

const centerX = (r: Rect): number => r.x + r.width / 2;
const centerY = (r: Rect): number => r.y + r.height / 2;

function cubic(
  sx: number,
  sy: number,
  c1x: number,
  c1y: number,
  c2x: number,
  c2y: number,
  tx: number,
  ty: number,
): string {
  return `M${sx},${sy} C${c1x},${c1y} ${c2x},${c2y} ${tx},${ty}`;
}

/** Same column: the peers stack vertically, so anchor bottom-face -> top-face
 * — the side of each box nearest the other. */
function routeSameColumn(
  source: Rect,
  target: Rect,
  parallelIndex: number,
  groupSize: number,
): EdgeGeometry {
  const [upper, lower] = centerY(source) <= centerY(target) ? [source, target] : [target, source];
  const sy = upper.y + upper.height;
  const ty = lower.y;
  const rowSpan = Math.max(1, Math.round((lower.y - upper.y) / ROW_H));

  if (rowSpan <= 1) {
    // Adjacent rows: nothing sits between them, so a straight face-centre line
    // is both the shortest path and the natural one. A lone edge (groupSize 1)
    // takes the exact centre (offset 0, unchanged from before); two or more
    // parallel links between the same adjacent pair fan out sideways,
    // symmetric around the centre, so they don't all draw the same line.
    const offset = (parallelIndex - (groupSize - 1) / 2) * ADJACENT_FAN;
    const sx = centerX(upper) + offset;
    const tx = centerX(lower) + offset;
    return { path: `M${sx},${sy} L${tx},${ty}`, labelX: (sx + tx) / 2, labelY: (sy + ty) / 2 };
  }

  // Two or more rows apart: a straight line would run under every box between,
  // and be swallowed. Lean each anchor along its own face toward the gutter
  // (each rect uses its own width — a column can mix element and host nodes),
  // then bow out into that gutter.
  const sx = upper.x + upper.width - LEAN_INSET;
  const tx = lower.x + lower.width - LEAN_INSET;
  // The bow must not spill past the next column: the curve's bulge is
  // BULGE * bow, so clamp bow by how much gutter is left after the lean and
  // a safety margin, on top of the usual per-parallel-index fan.
  const width = Math.max(upper.width, lower.width);
  const maxBulge = LEAN_INSET + (COL_W - width) - GUTTER_MARGIN;
  // Distribute the fan across the room actually available, instead of
  // stepping by a fixed FAN_STEP and clamping afterward — clamping a constant
  // step collapses indices 2+ onto the same bow once the cap is reached
  // (e.g. three-plus parallel links between same-column elements). Solving
  // for a per-group step keeps every index distinct, and stays OUTWARD-only
  // (bow never decreases with index).
  const maxBow = maxBulge / BULGE;
  const step = Math.min(FAN_STEP, (maxBow - BOW_BASE) / Math.max(1, groupSize - 1));
  const bow = BOW_BASE + parallelIndex * step;
  // Always +x. The -x side of a column is where local's edges run, and the
  // deepest column always has a free right gutter.
  const anchorX = (sx + tx) / 2;
  const cx = anchorX + bow;
  const dy = ty - sy;
  // Cap the control points' vertical offset so the curve exits sideways
  // right away instead of aiming down the span — see CTRL_Y_MAX above.
  const ctrlY = Math.min(dy / 3, CTRL_Y_MAX);
  return {
    path: cubic(sx, sy, cx, sy + ctrlY, cx, ty - ctrlY, tx, ty),
    labelX: anchorX + BULGE * bow,
    labelY: sy + dy / 2,
  };
}

/** Different columns: the left node's right face -> the right node's left
 * face, with a perpendicular fan separating parallel links. */
function routeCrossColumn(
  source: Rect,
  target: Rect,
  parallelIndex: number,
  groupSize: number,
): EdgeGeometry {
  const [left, right] = centerX(source) <= centerX(target) ? [source, target] : [target, source];
  const sx = left.x + left.width;
  const sy = centerY(left);
  const tx = right.x;
  const ty = centerY(right);
  const offset = (parallelIndex - (groupSize - 1) / 2) * CROSS_FAN;
  const dx = tx - sx;
  const dy = ty - sy;
  const len = Math.hypot(dx, dy) || 1;
  const nx = (-dy / len) * offset;
  const ny = (dx / len) * offset;
  return {
    path: cubic(
      sx,
      sy,
      sx + dx / 3 + nx,
      sy + dy / 3 + ny,
      sx + (2 * dx) / 3 + nx,
      sy + (2 * dy) / 3 + ny,
      tx,
      ty,
    ),
    labelX: sx + dx / 2 + BULGE * nx,
    labelY: sy + dy / 2 + BULGE * ny,
  };
}

/** Route one edge between two node rects. Symmetric in `source`/`target`:
 * geometry, not graph direction, decides which faces are used. */
export function routeEdge(
  source: Rect,
  target: Rect,
  parallelIndex: number,
  groupSize: number,
): EdgeGeometry {
  const sameColumn = Math.abs(source.x - target.x) < 1;
  return sameColumn
    ? routeSameColumn(source, target, parallelIndex, groupSize)
    : routeCrossColumn(source, target, parallelIndex, groupSize);
}
