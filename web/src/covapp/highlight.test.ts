// highlight.ts's contract (Task 5 brief): langForPath's extension table, and
// highlightLines' per-line HTML output. createJavaScriptRegexEngine is a
// pure-JS grammar engine (no WASM/oniguruma), so real Shiki highlighting
// runs under vitest/jsdom with no shims — these tests exercise the actual
// highlighter, not a mock.
import { describe, expect, it } from "vitest";

import { highlightLines, langForPath } from "./highlight";

describe("langForPath", () => {
  it.each([
    ["src/net/tcp.c", "c"],
    ["include/tcp.h", "c"],
    ["src/net/tcp.cpp", "cpp"],
    ["src/net/tcp.cc", "cpp"],
    ["src/net/tcp.cxx", "cpp"],
    ["include/tcp.hpp", "cpp"],
    ["include/tcp.hh", "cpp"],
    ["README.md", "text"],
    ["Makefile", "text"],
    ["src/net/tcp", "text"],
  ] as const)("%s -> %s", (path, expected) => {
    expect(langForPath(path)).toBe(expected);
  });
});

describe("highlightLines", () => {
  it('returns one HTML fragment per source line, matching source.split("\\n") length', async () => {
    const source = "int main() {\n    return 0;\n}\n";
    const html = await highlightLines(source, "c");
    expect(html).toHaveLength(source.split("\n").length);
    expect(html).toHaveLength(4); // 3 content lines + trailing empty line
    expect(html[3]).toBe(""); // the trailing "\n" yields one empty final line
  });

  it('preserves line count/emptiness parity with source.split("\\n") for a no-trailing-newline source', async () => {
    const source = "int x;\nint y;";
    const html = await highlightLines(source, "c");
    expect(html).toHaveLength(source.split("\n").length);
    expect(html).toHaveLength(2);
  });

  it("produces syntax-highlighted token spans for C code (keyword gets its own span)", async () => {
    const source = "int main(void) { return 0; }\n";
    const html = await highlightLines(source, "c");
    // "return" is a C keyword — Shiki wraps it in its own <span style="...">.
    expect(html[0]).toMatch(/<span[^>]*>return<\/span>/);
    // No leftover Shiki line/pre wrapper — only the inner token spans.
    expect(html[0]).not.toContain('class="line"');
    expect(html[0]).not.toContain("<pre");
    expect(html[0]).not.toContain("<code");
  });

  it("tokenizes cpp the same way (cpp grammar loaded)", async () => {
    const source = "class Foo {};\n";
    const html = await highlightLines(source, "cpp");
    expect(html[0]).toMatch(/<span[^>]*>class<\/span>/);
  });

  it("keeps a multi-line comment's tokenization consistent (single whole-source highlight call)", async () => {
    const source = "/* a\n   b */\nint x;\n";
    const html = await highlightLines(source, "c");
    // Both comment lines should carry the same "this is a comment" style —
    // proof the highlighter tokenized the source as one unit (preserving
    // grammar state across the line boundary), not line-by-line in
    // isolation (which would lose the open-comment state on line 2).
    const colorOf = (frag: string) => frag.match(/color:([^;"]+)/)?.[1];
    expect(colorOf(html[0])).toBeTruthy();
    expect(colorOf(html[0])).toBe(colorOf(html[1]));
  });

  it('lang "text" never calls Shiki: plain HTML-escaped lines, no token spans', async () => {
    const source = '<hello> & "world"\nline two\n';
    const html = await highlightLines(source, "text");
    expect(html).toHaveLength(3);
    expect(html[0]).toBe('&lt;hello&gt; &amp; "world"');
    expect(html[1]).toBe("line two");
    expect(html[0]).not.toContain("<span");
  });

  it("an empty source yields exactly one (empty) line", async () => {
    const html = await highlightLines("", "c");
    expect(html).toEqual([""]);
  });
});
