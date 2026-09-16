// The pure search engine behind the ⌘K palette and the file page's ?q=
// painting: substring/regex compilation with smart-case, per-line scanning
// over the search chunk, the uncovered-only gate, the result cap, the
// current-file-first ordering, ticket scoping, and the function list.
import { describe, expect, it } from "vitest";

import {
  compileQuery,
  matchesInLines,
  STATE_CHARS,
  searchFiles,
  searchFunctions,
  statesFromChunk,
} from "./search";
import type { FileChunk, FunctionEntry, SearchFile } from "./types";

function file(chunk: string, text: string, states?: string): SearchFile {
  const lineCount = text.split("\n").length;
  return { chunk, path: chunk.replace(/_/g, "/"), text, states: states ?? "-".repeat(lineCount) };
}

describe("compileQuery", () => {
  it("substring mode escapes regex metacharacters", () => {
    const { re } = compileQuery("a.b(", false);
    expect(re?.test("axb(")).toBe(false);
    expect(re?.test("a.b(")).toBe(true);
  });

  it("smart-case: no uppercase is case-insensitive, any uppercase is case-sensitive", () => {
    expect(compileQuery("mutex", false).re?.test("MUTEX_lock")).toBe(true);
    expect(compileQuery("Mutex", false).re?.test("mutex_lock")).toBe(false);
    expect(compileQuery("Mutex", false).re?.test("Mutex_lock")).toBe(true);
  });

  it("regex mode compiles the pattern and applies smart-case as the i flag", () => {
    expect(compileQuery("mu.ex_\\w+", true).re?.test("MUTEX_lock")).toBe(true);
    expect(compileQuery("Mu.ex", true).re?.flags).toBe("g");
    expect(compileQuery("mu.ex", true).re?.flags).toBe("gi");
  });

  it("an invalid regex reports the SyntaxError message instead of throwing", () => {
    const result = compileQuery("(", true);
    expect(result.re).toBeUndefined();
    expect(result.error).toMatch(/Invalid regular expression|Unterminated/);
  });
});

describe("matchesInLines", () => {
  it("reports every span on a line as one row, with column offsets", () => {
    const { re } = compileQuery("ab", false);
    const rows = matchesInLines(["xx ab yy AB", "none"], re as RegExp, "--", false);
    expect(rows).toEqual([
      {
        line: 1,
        text: "xx ab yy AB",
        spans: [
          [3, 5],
          [9, 11],
        ],
        state: "-",
      },
    ]);
  });

  it("terminates on a zero-length pattern and matches the line without spans", () => {
    const { re } = compileQuery("x*", true);
    const rows = matchesInLines(["", "abc"], re as RegExp, "--", false);
    expect(rows.map((r) => r.line)).toEqual([1, 2]);
    expect(rows[0].spans).toEqual([]);
  });

  it("uncoveredOnly keeps only lines whose state char is u", () => {
    const { re } = compileQuery("v", false);
    const rows = matchesInLines(["v1", "v2", "v3"], re as RegExp, "cu-", true);
    expect(rows.map((r) => r.line)).toEqual([2]);
    expect(rows[0].state).toBe("u");
  });

  it("refuses a non-global RegExp instead of looping", () => {
    expect(() => matchesInLines(["a"], /a/, "-", false)).toThrow(/global/);
  });
});

describe("searchFiles", () => {
  const files = [
    file("a_one.c", "alpha\nbeta\nalpha beta\n"),
    file("b_two.c", "gamma\nalpha\n"),
    file("c_three.c", "nothing\n"),
  ];

  it("groups by file in the given order and counts total lines and files", () => {
    const { re } = compileQuery("alpha", false);
    const result = searchFiles(files, re as RegExp, { uncoveredOnly: false, cap: 500 });
    expect(result.groups.map((g) => g.chunk)).toEqual(["a_one.c", "b_two.c"]);
    expect(result.total).toBe(3);
    expect(result.fileCount).toBe(2);
    expect(result.capped).toBe(false);
  });

  it("firstChunk moves that file's group to the front without reordering the rest", () => {
    const { re } = compileQuery("alpha", false);
    const result = searchFiles(files, re as RegExp, {
      uncoveredOnly: false,
      cap: 500,
      firstChunk: "b_two.c",
    });
    expect(result.groups.map((g) => g.chunk)).toEqual(["b_two.c", "a_one.c"]);
  });

  it("the cap limits shown lines while total keeps counting", () => {
    const { re } = compileQuery("alpha", false);
    const result = searchFiles(files, re as RegExp, { uncoveredOnly: false, cap: 2 });
    expect(result.groups.flatMap((g) => g.lines).length).toBe(2);
    expect(result.total).toBe(3);
    expect(result.capped).toBe(true);
  });

  it("allowedPaths restricts the file set (ticket scoping)", () => {
    const { re } = compileQuery("alpha", false);
    const result = searchFiles(files, re as RegExp, {
      uncoveredOnly: false,
      cap: 500,
      allowedPaths: new Set(["b/two.c"]),
    });
    expect(result.groups.map((g) => g.chunk)).toEqual(["b_two.c"]);
    expect(result.total).toBe(1);
  });

  it("scans a 1 MB corpus in one pass well under the runaway ceiling", () => {
    const line = "static int mutex_lock(struct mutex *m) { return m->owner == 0; }\n";
    const big = Array.from({ length: 200 }, (_, i) => file(`f${i}.c`, line.repeat(80)));
    const { re } = compileQuery("owner ==", false);
    const started = performance.now();
    const result = searchFiles(big, re as RegExp, { uncoveredOnly: false, cap: 500 });
    const elapsed = performance.now() - started;
    expect(result.total).toBe(16_000);
    // A runaway guard, not a discriminator: a quadratic regression costs
    // seconds here; a loaded laptop costs tens of milliseconds.
    expect(elapsed).toBeLessThan(2_000);
  });
});

describe("searchFunctions", () => {
  const fns: FunctionEntry[] = [
    { name: "checked_add", chunk: "m.c", path: "m.c", line: 3, end: 8, hits: { unit: 1 } },
    { name: "main", chunk: "m.c", path: "m.c", line: 10, end: 13, hits: {} },
    { name: "Main_init", chunk: "u.c", path: "u.c", line: 1, end: null, hits: {} },
  ];

  it("substring smart-case over the name, keeping the chunk's sort order", () => {
    expect(searchFunctions(fns, "main", 200).rows.map((f) => f.name)).toEqual([
      "main",
      "Main_init",
    ]);
    expect(searchFunctions(fns, "Main", 200).rows.map((f) => f.name)).toEqual(["Main_init"]);
  });

  it("an empty query lists everything; the cap limits rows and total keeps counting", () => {
    expect(searchFunctions(fns, "", 200).total).toBe(3);
    const capped = searchFunctions(fns, "", 2);
    expect(capped.rows.length).toBe(2);
    expect(capped.total).toBe(3);
    expect(capped.capped).toBe(true);
  });
});

describe("statesFromChunk", () => {
  it("mirrors the Python line_states rule for every state", () => {
    const chunk: FileChunk = {
      stamp: "s",
      chunk: "c",
      path: "c.c",
      source: "l1\nl2\nl3\nl4\nl5\nl6\nl7",
      lines: {
        "2": { hits: { system: 1 }, branches: [], state: null },
        "3": { hits: {}, branches: [], state: null },
        "4": { hits: {}, branches: [], state: "stale" },
        "5": { hits: {}, branches: [], state: "aging" },
        "6": { hits: { system: 9 }, branches: [], state: null },
        "40": { hits: {}, branches: [], state: null },
      },
      excluded: [6],
    };
    expect(statesFromChunk(chunk)).toBe("-cusax-");
  });

  it("exports the state vocabulary the contract pins", () => {
    expect(Object.keys(STATE_CHARS).sort()).toEqual(["-", "a", "c", "s", "u", "x"]);
  });
});
