import { afterEach, describe, expect, it, vi } from "vitest";

import { clearMatches, MATCH_HIGHLIGHT_NAME, paintMatches } from "./matchHighlight";

function row(line: number, html: string): HTMLElement {
  const el = document.createElement("div");
  el.setAttribute("data-testid", `code-row-${line}`);
  el.innerHTML = `<div class="cv-src">${html}</div>`;
  return el;
}

class FakeHighlight {
  ranges: Range[] = [];
  add(range: Range): void {
    this.ranges.push(range);
  }
}

afterEach(() => {
  document.body.innerHTML = "";
});

// jsdom defines no `CSS` global at all (testUtils polyfills only `escape`
// where a test needs react-aria); both cases below stub it explicitly, and
// vitest's `unstubGlobals: true` restores after each test.
describe("paintMatches", () => {
  it("returns false and paints nothing when the Custom Highlight API is absent", () => {
    vi.stubGlobal("CSS", { escape: (s: string) => s });
    const root = document.createElement("div");
    root.append(row(1, "int a;"));
    expect(paintMatches(root, new Map([[1, [[0, 3]]]]))).toBe(false);
  });

  it("builds one Range per span across token boundaries and registers it by name", () => {
    const registry = new Map<string, FakeHighlight>();
    vi.stubGlobal("Highlight", FakeHighlight);
    vi.stubGlobal("CSS", { escape: (s: string) => s, highlights: registry });
    const root = document.createElement("div");
    document.body.append(root);
    root.append(row(1, "<span>int</span><span> mutex_lock</span>"), row(2, "<span>x</span>"));
    const ok = paintMatches(
      root,
      new Map([
        [1, [[2, 9]]],
        [2, [[0, 1]]],
      ]),
    );
    expect(ok).toBe(true);
    const painted = registry.get(MATCH_HIGHLIGHT_NAME) as FakeHighlight;
    expect(painted.ranges.map((r) => r.toString())).toEqual(["t mutex", "x"]);
    clearMatches();
    expect(registry.has(MATCH_HIGHLIGHT_NAME)).toBe(false);
  });
});
