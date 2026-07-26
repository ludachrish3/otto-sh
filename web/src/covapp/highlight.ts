// Shiki-based syntax highlighting for the file page's source view (Task 5
// brief). ONLY the fine-grained entry points are imported — never the root
// "shiki" package, whose default engine pulls in oniguruma/WASM. The
// covapp CSP (Global Constraints) is `script-src 'self'` with no
// `'wasm-unsafe-eval'`/`'unsafe-eval'`, and this build's bundle ceiling
// (`chunkSizeWarningLimit: 2_000` in vite.covapp.config.ts) treats any Vite
// warning as a hard build failure — the WASM binary alone blows past both.
// `createJavaScriptRegexEngine` is a pure-JS re-implementation of the same
// TextMate grammar engine, so it works identically under jsdom in vitest,
// with no browser/WASM runtime and no test shims needed.
import { createHighlighterCore, type HighlighterCore } from "shiki/core";
import { createJavaScriptRegexEngine } from "shiki/engine/javascript";
import c from "shiki/langs/c.mjs";
import cpp from "shiki/langs/cpp.mjs";
import githubDark from "shiki/themes/github-dark.mjs";
import githubLight from "shiki/themes/github-light.mjs";

export type CodeLang = "c" | "cpp" | "text";

/** `.c`/`.h` -> "c"; `.cpp`/`.cc`/`.cxx`/`.hpp`/`.hh` -> "cpp"; anything else
 * (including no extension at all, e.g. "Makefile") -> "text", which skips
 * Shiki entirely (see `highlightLines`). */
export function langForPath(path: string): CodeLang {
  const dot = path.lastIndexOf(".");
  const ext = dot === -1 ? "" : path.slice(dot + 1).toLowerCase();
  if (ext === "c" || ext === "h") return "c";
  if (ext === "cpp" || ext === "cc" || ext === "cxx" || ext === "hpp" || ext === "hh") return "cpp";
  return "text";
}

// Singleton: constructing a highlighter compiles its grammars/themes, which
// is the expensive part — every caller (FilePage across navigations, every
// test in this file) shares one instance via one shared promise, built
// once on first use rather than per file/per test.
let highlighterPromise: Promise<HighlighterCore> | null = null;

function getHighlighter(): Promise<HighlighterCore> {
  if (!highlighterPromise) {
    highlighterPromise = createHighlighterCore({
      langs: [c, cpp],
      themes: [githubLight, githubDark],
      engine: createJavaScriptRegexEngine(),
    });
  }
  return highlighterPromise;
}

function escapeHtml(text: string): string {
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

/** Splits exactly like Shiki's own internal line-splitter (`splitLines` in
 * `@shikijs/core`, which every `codeTo*` call tokenizes against) — verified
 * to match `source.split("\n")` element-for-element, including a trailing
 * empty final element when `source` ends with "\n". `highlightLines` keeps
 * that same indexing for the "text" (no-highlight) fallback path, so
 * FilePage can zip either path's output against `source.split("\n")`
 * without a special case. */
function plainLines(source: string): string[] {
  return source.split("\n").map((line) => escapeHtml(line));
}

/** One highlighted HTML fragment per line of `source`, indexed the same as
 * `source.split("\n")` (see `plainLines`) — safe to drop straight into a
 * `dangerouslySetInnerHTML` source cell (`ui/CodeView.tsx`): Shiki
 * HTML-escapes token content itself (`escapeHtml` above matches its
 * escaping for the "text" fallback), so untrusted file source never
 * produces unescaped markup either way.
 *
 * `lang: "text"` never touches Shiki — there's no grammar to tokenize with,
 * and loading one for a pass-through no-op would be pure overhead.
 *
 * Highlights the WHOLE source in one call, then slices the result back
 * into per-line fragments, rather than highlighting line-by-line: C/C++
 * source routinely has constructs that span lines (block comments,
 * multi-line strings) whose correct color depends on grammar state carried
 * over from a previous line — per-line calls would restart that state at
 * every line and mis-highlight the second and later lines of any such
 * construct. */
export async function highlightLines(source: string, lang: CodeLang): Promise<string[]> {
  if (lang === "text") return plainLines(source);

  const highlighter = await getHighlighter();
  const html = highlighter.codeToHtml(source, {
    lang,
    themes: { light: "github-light", dark: "github-dark" },
    defaultColor: "light",
    cssVariablePrefix: "--shiki-",
  });

  // Shiki's output shape is
  //   <pre ...><code><span class="line">TOKENS</span>
  //   <span class="line">TOKENS</span>...</code></pre>
  // — one "line" span per source line, in order, joined by literal "\n"
  // characters between spans (never nested inside one another; only
  // per-token spans nest inside a line span). Splitting on the literal
  // opening tag is therefore exact, not a fragile regex over nested markup.
  const codeStart = html.indexOf("<code>") + "<code>".length;
  const codeEnd = html.lastIndexOf("</code>");
  const inner = html.slice(codeStart, codeEnd);
  const parts = inner.split('<span class="line">');
  parts.shift(); // drop the (empty) text before the first line span
  return parts.map((part) => part.replace(/\n$/, "").replace(/<\/span>$/, ""));
}
