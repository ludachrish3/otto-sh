// Meta-test: every testid a test references must exist somewhere in shipped
// source (gate G14, todo/test-infra-remediation-plan-2026-08-06.md).
//
// The defect class: `expect(queryByTestId("status-text")).toBeNull()` where NO
// production code has ever rendered `status-text` — the assertion is green
// against every possible product, including one that regressed the exact
// behavior the test names. Absence assertions are only meaningful against ids
// the product could render; presence assertions with a typo'd id fail loudly,
// but absence assertions with a phantom id never do. This suite closes that
// hole structurally: a testid referenced anywhere in a vitest file must appear
// in non-test source, either verbatim (`data-testid="x"`, or a forwarded prop
// named `testId` or camelCase-ending in `TestId` — ui/Disclosure takes both
// `testId` and `toggleTestId`, each landing in a `data-testid={...}`) or as a
// template prefix (`data-testid={`code-row-${...}`}` legitimizes
// `code-row-1`). A test file's OWN renders also count, for its own references
// only: harness probes (`<div data-testid="my-header">` in CodeView.test.tsx)
// are legitimate, but one file's harness must not be able to legitimize
// another file's phantom. Extraction runs on comment-stripped source: a
// `data-testid="x"` spelled inside a comment must not enter the acceptance
// sets, or a removal note written the natural way would re-legitimize the
// exact phantom this gate exists to catch.
//
// A test that wants to pin "this id was deliberately removed" (the shape that
// motivated this gate — spec decision 9's status-text/status-dot) should pin
// the REPLACEMENT's behavior instead — e.g. AppShell.test.tsx's menu test
// asserts no row TEXT names a ticket inside the real open menu, which catches
// a regrown list under any future testid.
//
// Accepted blind spots, stated so the enumeration doesn't shrink below the
// gate: (1) the Playwright dashboard lane (tests/dashboard/, Python
// `get_by_test_id`) is a separate namespace with its own runner; (2)
// `el.getAttribute("data-testid")` comparisons — a value-position literal
// with no extractable call shape (three live sites today, all presence-side,
// where a typo fails loudly); (3) comment stripping is line-based — a
// same-line trailing comment after code is not stripped, and a template
// literal whose own LINE starts with `/*` would enter block mode and hide
// later references (false-negative direction; zero corpus hits); (4)
// `[data-testid^="prefix"]` PREFIX selectors are not extracted (four live
// sites, prefixes rendered today). Exact querySelector attribute selectors
// ARE covered — `[data-testid="x"]` in a test file is a reference, and in
// shipped source it is a lookup, not a render site.
import { readdirSync, readFileSync } from "node:fs";
import { basename, dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const SRC_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const SELF = resolve(fileURLToPath(import.meta.url));

function walk(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) out.push(...walk(path));
    else if (/\.tsx?$/.test(entry.name)) out.push(path);
  }
  return out;
}

/** Blanks comment lines while preserving line numbers: `//`-only lines, JSX
 * `{/*` lines, and block-comment bodies (`/*` opener through the line holding
 * the closer). Trailing same-line comments after code are left alone — see
 * the header's accepted-blind-spots note. */
function stripCommentLines(source: string): string {
  const lines = source.split("\n");
  let inBlock = false;
  return lines
    .map((line) => {
      const trimmed = line.trim();
      if (inBlock) {
        if (trimmed.includes("*/")) inBlock = false;
        return "";
      }
      if (trimmed.startsWith("//")) return "";
      if (trimmed.startsWith("/*") || trimmed.startsWith("{/*")) {
        if (!trimmed.includes("*/")) inBlock = true;
        return "";
      }
      return line;
    })
    .join("\n");
}

const ALL_FILES = walk(SRC_ROOT);
// A test helper is neither a reference source (not a suite) nor shipped
// source (its renders must stay file-local to the suites importing it —
// letting covapp/testUtils.tsx or __tests__/_synth.ts into the GLOBAL
// acceptance set would be the cross-file harness leak through a side door).
function isTestFile(f: string): boolean {
  return /\.test\.tsx?$/.test(f);
}
function isTestHelper(f: string): boolean {
  return (
    basename(f).startsWith("testUtils") || relative(SRC_ROOT, f).split("/").includes("__tests__")
  );
}
// The meta-test excludes ITSELF from reference extraction: its own controls
// (below) embed phantom-shaped calls in strings, which the extractor would
// otherwise report as real references.
const TEST_FILES = ALL_FILES.filter((f) => isTestFile(f) && resolve(f) !== SELF);
const SOURCE_FILES = ALL_FILES.filter((f) => !isTestFile(f) && !isTestHelper(f));

interface Ref {
  id: string;
  at: string; // "relative/path.tsx:line"
}

/** String literals passed to get/query/find(All)ByTestId, plus
 * `[data-testid="x"]` attribute selectors (the querySelector form — in a test
 * file that is a reference just like a ByTestId call). Dynamic arguments
 * (template literals with interpolation, identifiers) are deliberately not
 * extracted — their ids are legitimized by the template-prefix acceptance
 * below, at their render site. */
function extractReferencedIds(rawSource: string, label: string): Ref[] {
  const source = stripCommentLines(rawSource);
  const refs: Ref[] = [];
  const patterns = [
    /(?:get|query|find)(?:All)?ByTestId\(\s*(["'`])((?:(?!\1)[^$])+)\1/g,
    /\[data-testid=(["'])((?:(?!\1).)+)\1\]/g,
  ];
  for (const pattern of patterns) {
    for (const m of source.matchAll(pattern)) {
      const id = m[2];
      if (id === undefined) continue;
      const line = source.slice(0, m.index).split("\n").length;
      refs.push({ id, at: `${label}:${line}` });
    }
  }
  return refs;
}

/** Acceptance sets from a source: verbatim ids (the `data-testid` attribute
 * or a forwarded prop named `testId`/camelCase-`*TestId`, plain string or
 * brace-wrapped or interpolation-free template), and non-empty template
 * prefixes up to the first interpolation. The lookbehind rejects (a) longer
 * identifiers merely ENDING in testId (`latestId=`) and (b) `[data-testid=`
 * attribute SELECTORS — a querySelector in shipped source is a lookup of an
 * id rendered elsewhere, not a render site. Comment lines are stripped first
 * (see stripCommentLines). */
function extractRenderedIds(rawSource: string): { statics: string[]; prefixes: string[] } {
  const source = stripCommentLines(rawSource);
  const statics: string[] = [];
  const prefixes: string[] = [];
  const attr = /(?<![A-Za-z[-])(?:data-testid|testId|[A-Za-z]+TestId)=/.source;
  for (const m of source.matchAll(
    new RegExp(`${attr}\\{?\\s*(["'])((?:(?!\\1).)+)\\1\\s*\\}?`, "g"),
  )) {
    if (m[2] !== undefined) statics.push(m[2]);
  }
  for (const m of source.matchAll(new RegExp(`${attr}\\{\\s*\`([^\`$]+)\`\\s*\\}`, "g"))) {
    if (m[1] !== undefined) statics.push(m[1]);
  }
  for (const m of source.matchAll(new RegExp(`${attr}\\{\\s*\`([^\`$]+)\\$\\{`, "g"))) {
    if (m[1] !== undefined) prefixes.push(m[1]);
  }
  return { statics, prefixes };
}

function accepted(id: string, statics: Set<string>, prefixes: string[]): boolean {
  return statics.has(id) || prefixes.some((p) => id.startsWith(p));
}

interface TestFileScan {
  refs: Ref[];
  localStatics: Set<string>;
  localPrefixes: string[];
}

const TEST_SCANS: TestFileScan[] = TEST_FILES.map((f) => {
  const source = readFileSync(f, "utf-8");
  const local = extractRenderedIds(source);
  return {
    refs: extractReferencedIds(source, relative(SRC_ROOT, f)),
    localStatics: new Set(local.statics),
    localPrefixes: local.prefixes,
  };
});
const REFS: Ref[] = TEST_SCANS.flatMap((s) => s.refs);
const RENDERED = SOURCE_FILES.map((f) => extractRenderedIds(readFileSync(f, "utf-8")));
const STATIC_IDS = new Set(RENDERED.flatMap((r) => r.statics));
const PREFIXES = [...new Set(RENDERED.flatMap((r) => r.prefixes))];

describe("testid integrity", () => {
  // Anti-vacuity: a refactor that breaks the walk or the extractors must not
  // demote this suite to a green no-op over an empty corpus. Floors sit at
  // roughly HALF of measured actuals (2026-08: 77 test files, ~900 raw refs,
  // ~160 statics, ~40 prefixes) — high enough that losing any whole lane
  // (covapp/ui/shell/topo vs __tests__) trips them, low enough that ordinary
  // test-suite shrinkage does not.
  it("scans a real corpus", () => {
    expect(TEST_FILES.length).toBeGreaterThan(38);
    expect(REFS.length).toBeGreaterThan(450);
    expect(STATIC_IDS.size).toBeGreaterThan(80);
    expect(PREFIXES.length).toBeGreaterThan(20);
  });

  // Positive control: the checker must flag a phantom. Runs the REAL
  // acceptance sets against synthetic references, proving both halves —
  // extraction sees each reference shape, and acceptance does not
  // blanket-accept.
  it("control: a phantom id is detected, in both reference shapes", () => {
    const synthetic = [
      'expect(screen.queryByTestId("definitely-not-rendered")).toBeNull();',
      "expect(container.querySelector('[data-testid=\"also-not-rendered\"]')).toBeNull();",
    ].join("\n");
    const refs = extractReferencedIds(synthetic, "synthetic.test.tsx");
    expect(refs).toEqual([
      { id: "definitely-not-rendered", at: "synthetic.test.tsx:1" },
      { id: "also-not-rendered", at: "synthetic.test.tsx:2" },
    ]);
    expect(accepted("definitely-not-rendered", STATIC_IDS, PREFIXES)).toBe(false);
    expect(accepted("also-not-rendered", STATIC_IDS, PREFIXES)).toBe(false);
  });

  // Negative controls: occurrences that must NOT create acceptance.
  it("control: comments, selectors, and lookalike props do not legitimize ids", () => {
    // A removal note spelling the dead id in attribute form — the exact trap
    // the gate would otherwise re-open (opus review, Wave 15).
    const commented = [
      "export function X() {",
      '  // the status dot (data-testid="commented-out-id") was removed here',
      '  {/* <div data-testid="jsx-commented-id" /> */}',
      "  /* block comment:",
      '     <div data-testid="block-commented-id" /> */',
      "  return null;",
      "}",
    ].join("\n");
    const fromComments = extractRenderedIds(commented);
    expect(fromComments.statics).toEqual([]);
    // querySelector in SHIPPED source is a lookup, not a render site.
    const selector = "document.querySelector('[data-testid=\"selector-only-id\"]');";
    expect(extractRenderedIds(selector).statics).toEqual([]);
    // A prop that merely ENDS in testId is not the forwarding convention.
    const lookalike = '<Feed latestId="lookalike-id" />';
    expect(extractRenderedIds(lookalike).statics).toEqual([]);
    // And the real corpus must not have picked any of these up elsewhere.
    for (const id of ["commented-out-id", "jsx-commented-id", "block-commented-id"]) {
      expect(STATIC_IDS.has(id)).toBe(false);
    }
  });

  // Calibration controls: each acceptance path proven against a known render
  // site, so a regex regression cannot silently blind one path.
  it("control: each acceptance path sees its known render site", () => {
    // Attribute form — App shell's hidden import input.
    expect(STATIC_IDS.has("import-input")).toBe(true);
    // Forwarded-prop form — DirectoryPage's <Disclosure testId="runs-disclosure">.
    expect(STATIC_IDS.has("runs-disclosure")).toBe(true);
    // camelCase-*TestId prop form — TopoLegend's toggleTestId.
    expect(STATIC_IDS.has("topo-legend-toggle")).toBe(true);
    // Template-prefix form — ui/CodeView's `code-row-${line.number}`.
    expect(accepted("code-row-1", STATIC_IDS, PREFIXES)).toBe(true);
    expect(STATIC_IDS.has("code-row-1")).toBe(false);
    // Harness-local form — CodeView.test.tsx's own header probe is accepted
    // for that file, but must NOT leak into the global acceptance set.
    expect(STATIC_IDS.has("my-header")).toBe(false);
  });

  it("every testid referenced by a test is rendered in shipped source or by that test itself", () => {
    const violations = new Map<string, string>();
    for (const scan of TEST_SCANS) {
      for (const ref of scan.refs) {
        if (
          !accepted(ref.id, STATIC_IDS, PREFIXES) &&
          !accepted(ref.id, scan.localStatics, scan.localPrefixes) &&
          !violations.has(ref.id)
        ) {
          violations.set(ref.id, `${ref.id} — first referenced at ${ref.at}`);
        }
      }
    }
    expect([...violations.values()].sort()).toEqual([]);
  });
});
