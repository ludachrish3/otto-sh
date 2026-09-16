// The pure search engine (no React, no library): what the ⌘K palette runs
// over `cov_data/search.js` and what FilePage re-runs over one file to paint
// `?q=` matches. Plain `RegExp` on purpose — the report's CSP forbids WASM
// and the covapp bundle has no room for a search library; a 1 MB C tree is a
// single synchronous pass well under a frame.
import type { FileChunk, FunctionEntry, SearchFile } from "./types";

/** One char per source line in `SearchFile.states`; mirrors
 * `SEARCH_STATE_CHARS` in spa_data.py (pinned by the covapp contract). */
export const STATE_CHARS = {
  "-": "none",
  c: "covered",
  u: "uncovered",
  x: "excluded",
  s: "stale",
  a: "aging",
} as const;

export type CompiledQuery = { re: RegExp; error?: undefined } | { re?: undefined; error: string };

const REGEX_META = /[.*+?^${}()|[\]\\]/g;
const UPPERCASE_LETTER = /\p{Lu}/u;

/** Smart-case (the rg/fzf rule): any uppercase letter makes the query
 * case-sensitive; otherwise the `i` flag applies. Both modes compile to one
 * global RegExp so the scanners below have a single code path. */
export function compileQuery(q: string, regex: boolean): CompiledQuery {
  const flags = UPPERCASE_LETTER.test(q) ? "g" : "gi";
  const source = regex ? q : q.replace(REGEX_META, "\\$&");
  try {
    return { re: new RegExp(source, flags) };
  } catch (err) {
    return { error: err instanceof Error ? err.message : String(err) };
  }
}

export interface LineMatch {
  line: number;
  text: string;
  /** `[start, end)` column pairs; empty for a zero-width match. */
  spans: [number, number][];
  /** The line's `STATE_CHARS` key. */
  state: string;
}

export interface FileMatches {
  chunk: string;
  path: string;
  lines: LineMatch[];
}

/** Scan one file's lines. `states[i]` gates line `i + 1` when `uncoveredOnly`.
 * A zero-length match advances `lastIndex` by one so `x*`-style patterns
 * terminate; the line still counts as matched, with no span to paint. `re`
 * must carry the `g` flag — this loop relies on `re.lastIndex` advancing
 * between `exec` calls, which only happens on a global RegExp; a
 * non-global one would `exec` the same match forever. */
export function matchesInLines(
  lines: string[],
  re: RegExp,
  states: string,
  uncoveredOnly: boolean,
): LineMatch[] {
  if (!re.global) throw new Error("matchesInLines needs a global RegExp (compileQuery makes one)");
  const out: LineMatch[] = [];
  for (let i = 0; i < lines.length; i++) {
    const state = states[i] ?? "-";
    if (uncoveredOnly && state !== "u") continue;
    const text = lines[i] ?? "";
    const spans: [number, number][] = [];
    let matched = false;
    re.lastIndex = 0;
    for (;;) {
      const m = re.exec(text);
      if (m === null) break;
      matched = true;
      if (m[0].length === 0) {
        re.lastIndex++;
        if (m.index >= text.length) break;
        continue;
      }
      spans.push([m.index, m.index + m[0].length]);
    }
    if (matched) out.push({ line: i + 1, text, spans, state });
  }
  return out;
}

export interface SearchOptions {
  uncoveredOnly: boolean;
  /** Maximum matching LINES listed; `total` keeps counting past it. */
  cap: number;
  /** The open file page's chunk, listed first (spec §3.3). */
  firstChunk?: string | undefined;
  /** Display paths a pinned ticket owns lines in; null/undefined = no scoping. */
  allowedPaths?: Set<string> | null | undefined;
}

export interface SearchResult {
  groups: FileMatches[];
  total: number;
  fileCount: number;
  capped: boolean;
}

export function searchFiles(files: SearchFile[], re: RegExp, opts: SearchOptions): SearchResult {
  const ordered =
    opts.firstChunk === undefined
      ? files
      : [
          ...files.filter((f) => f.chunk === opts.firstChunk),
          ...files.filter((f) => f.chunk !== opts.firstChunk),
        ];
  const groups: FileMatches[] = [];
  let total = 0;
  let fileCount = 0;
  let shown = 0;
  for (const f of ordered) {
    if (opts.allowedPaths && !opts.allowedPaths.has(f.path)) continue;
    const lines = matchesInLines(f.text.split("\n"), re, f.states, opts.uncoveredOnly);
    if (lines.length === 0) continue;
    total += lines.length;
    fileCount++;
    if (shown >= opts.cap) continue;
    const room = opts.cap - shown;
    const kept = lines.length > room ? lines.slice(0, room) : lines;
    shown += kept.length;
    groups.push({ chunk: f.chunk, path: f.path, lines: kept });
  }
  return { groups, total, fileCount, capped: total > shown };
}

export interface FunctionSearchResult {
  rows: FunctionEntry[];
  total: number;
  capped: boolean;
}

/** Substring smart-case over the function name; the chunk is already sorted
 * by name/path/line, and that order is kept. An empty query lists all. */
export function searchFunctions(
  fns: FunctionEntry[],
  q: string,
  cap: number,
): FunctionSearchResult {
  const caseSensitive = UPPERCASE_LETTER.test(q);
  const needle = caseSensitive ? q : q.toLowerCase();
  const matching = fns.filter((f) =>
    (caseSensitive ? f.name : f.name.toLowerCase()).includes(needle),
  );
  return { rows: matching.slice(0, cap), total: matching.length, capped: matching.length > cap };
}

/** FilePage's stand-in for `SearchFile.states` when only the file chunk is
 * loaded — the same rule `line_states` applies in spa_data.py. */
export function statesFromChunk(chunk: FileChunk): string {
  const excluded = new Set(chunk.excluded);
  const lineCount = chunk.source.split("\n").length;
  let out = "";
  for (let lineNo = 1; lineNo <= lineCount; lineNo++) {
    if (excluded.has(lineNo)) {
      out += "x";
      continue;
    }
    const line = chunk.lines[String(lineNo)];
    if (line === undefined) out += "-";
    else if (line.state === "stale") out += "s";
    else if (line.state === "aging") out += "a";
    else if (Object.values(line.hits).some((n) => n > 0)) out += "c";
    else out += "u";
  }
  return out;
}
