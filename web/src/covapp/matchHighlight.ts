// Paints `?q=` matches on the file page with the CSS Custom Highlight API:
// Ranges over the existing text nodes, registered under one name, styled by
// `::highlight(otto-search)` in covapp.css. No DOM mutation, so Shiki's
// token spans stay untouched. Where the API is absent the caller keeps the
// row-level `data-match` tint only.
export const MATCH_HIGHLIGHT_NAME = "otto-search";

function highlightApiAvailable(): boolean {
  return typeof CSS !== "undefined" && "highlights" in CSS && typeof Highlight === "function";
}

function rangeForColumns(cell: HTMLElement, start: number, end: number): Range | null {
  const walker = document.createTreeWalker(cell, NodeFilter.SHOW_TEXT);
  let offset = 0;
  let startNode: Text | null = null;
  let startOffset = 0;
  for (
    let node = walker.nextNode() as Text | null;
    node !== null;
    node = walker.nextNode() as Text | null
  ) {
    const length = node.data.length;
    if (startNode === null && start < offset + length) {
      startNode = node;
      startOffset = start - offset;
    }
    if (startNode !== null && end <= offset + length) {
      const range = document.createRange();
      range.setStart(startNode, startOffset);
      range.setEnd(node, end - offset);
      return range;
    }
    offset += length;
  }
  return null;
}

/** Register a Range per `[start, end)` column span of each listed line.
 * Returns false (painting nothing) when the browser has no `CSS.highlights`. */
export function paintMatches(
  root: HTMLElement,
  spansByLine: Map<number, [number, number][]>,
): boolean {
  if (!highlightApiAvailable()) return false;
  const highlight = new Highlight();
  for (const [line, spans] of spansByLine) {
    const cell = root.querySelector<HTMLElement>(`[data-testid="code-row-${line}"] .cv-src`);
    if (cell === null) continue;
    for (const [start, end] of spans) {
      const range = rangeForColumns(cell, start, end);
      if (range !== null) highlight.add(range);
    }
  }
  CSS.highlights.set(MATCH_HIGHLIGHT_NAME, highlight);
  return true;
}

export function clearMatches(): void {
  if (highlightApiAvailable()) CSS.highlights.delete(MATCH_HIGHLIGHT_NAME);
}
