// Converts the raw Chromium CDP precise-coverage dumps written by the browser
// e2e suites (tests/_fixtures/_ts_coverage.py -> reports/ts-e2e-cov/raw/)
// into istanbul JSON under reports/ts-e2e-cov/istanbul/, resolving served
// URLs back to web/src through the HIDDEN sourcemaps that `make web` builds
// beside each dist file. A missing .map is a hard error: it means
// build.sourcemap regressed and the merged TS gate would silently lose its
// e2e leg — the gate must fail loudly instead.

import { existsSync, readdirSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { mergeScriptCovs } from "@bcoe/v8-coverage";
import { decode, encode } from "@jridgewell/sourcemap-codec";
import { CoverageReport } from "monocart-coverage-reports";

const repo = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const webRoot = resolve(repo, "web");
const rawDir = resolve(repo, "reports/ts-e2e-cov/raw");
const outDir = resolve(repo, "reports/ts-e2e-cov/istanbul");
const dists = [
  resolve(repo, "src/otto/monitor/static/dist"),
  resolve(repo, "src/otto/coverage/renderer/static/dist"),
];

// Vendored Untitled UI source + generated wire types: excluded from coverage
// the same way Biome/vitest/knip exclude them (web/README.md, vendor
// boundary). Bootstrap entrypoints (main.tsx, covreport/main.ts) are
// deliberately INCLUDED here — the e2e leg is precisely what exercises them
// (vitest excludes them for the opposite reason).
const EXCLUDED = new Set([
  "src/utils/cx.ts",
  "src/utils/is-react-component.ts",
  "src/hooks/use-breakpoint.ts",
  "src/hooks/use-resize-observer.ts",
]);
// Adaptation point (sanctioned, see task-6-brief.md Step 2 API caveat):
// monocart-coverage-reports@2.12.12 runs `sourceFilter` on whatever
// `sourcePath` just returned, not on the pre-resolution sourcemap path — the
// two callbacks share one normalized value per source, mutated in place
// before the filter sees it (lib/utils/source-path.js's
// initSourceMapSourcesPath runs before lib/converter/converter.js's
// initOriginalList). Verified by instrumenting both callbacks against a real
// dist bundle's hidden sourcemap: the raw arg `sourcePath` receives is
// `web/<repo-relative path>` (e.g. "web/src/topo/TopologyPage.tsx",
// "web/node_modules/react/index.js") — every sourcemap `sources` entry here
// is "../../../../../../web/..." and normalizePathDir() strips the leading
// ".." segments, always leaving a "web/"-rooted path regardless of process
// cwd (path.relative(cwd, path.resolve(cwd, rel)) reconstructs `rel`). Since
// `sourcePath` must return the absolute `/…/web/src/…` key vitest uses (the
// merge's load-bearing invariant — Step 4), `excluded()` below runs on that
// same absolute value and strips the `webRoot` prefix itself, rather than
// assuming a bare "src/..." arg. The exclusion SEMANTICS are unchanged from
// the brief; only this prefix handling is the adapted plumbing.
const excluded = (absSourcePath) => {
  const p = absSourcePath.startsWith(`${webRoot}/`)
    ? absSourcePath.slice(webRoot.length + 1)
    : absSourcePath;
  return (
    !p.startsWith("src/") ||
    p.includes(".test.") ||
    /^src\/(components|styles)\//.test(p) ||
    /^src\/api\/(types|export)\.gen\.ts$/.test(p) ||
    EXCLUDED.has(p)
  );
};

// Adaptation point #2 (path plumbing, not an exclusion-semantics change):
// the brief assumed a root-mounted dist (pathname === the dist-relative
// path). The actual served URLs, observed in a real raw dump, are
// `http://host:port/static/dist/assets/index-*.js` (monitor server mounts
// _STATIC_DIR at /static, static/server.py) and, for the covreport suite
// (opened via file://, no server — report_browser/conftest.py),
// `file:///tmp/.../report/static/dist/covreport.js`. Both share a `/dist/`
// path segment immediately before the dist-relative subpath, so anchor on
// that instead of assuming the URL is dist-rooted.
function distFileFor(url) {
  const pathname = new URL(url).pathname;
  const marker = "/dist/";
  const idx = pathname.lastIndexOf(marker);
  if (idx === -1) {
    throw new Error(`e2e_coverage_report: no dist file serves ${url}`);
  }
  const rel = pathname.slice(idx + marker.length);
  for (const dist of dists) {
    const candidate = resolve(dist, rel);
    if (existsSync(candidate)) return candidate;
  }
  throw new Error(`e2e_coverage_report: no dist file serves ${url}`);
}

// Prune a bundle's hidden sourcemap down to its web/src sources before monocart
// ever sees it. The monitor bundle maps to ~1300 sources, but ~1235 are
// node_modules and we only ever report on web/src (monocart's sourceFilter
// discards the rest anyway). Left whole, monocart decodes ~2.1 M mapping chars
// and builds coverage structures for all 1300 sources — that alone needs >3.5
// GB of V8 heap and OOMs. Keeping only web/src (~94 sources) drops peak RSS to
// ~0.5 GB. Dropped-source segments are collapsed to a bare [genCol] "unmapped"
// marker rather than removed, so a V8 range that lands in vendored code maps to
// nothing instead of bleeding onto the previous kept source (which deleting the
// segment outright would cause).
function pruneSourceMap(map) {
  const oldToNew = new Array(map.sources.length).fill(-1);
  const keepOld = [];
  map.sources.forEach((src, i) => {
    if (src?.includes("/web/src/")) {
      oldToNew[i] = keepOld.length;
      keepOld.push(i);
    }
  });
  // A bundle that has sources but none under /web/src/ means the emitted
  // `sources` changed shape (e.g. a Vite/Rollup bump switched to sourceRoot-
  // relative or absolute paths) — the keep filter would silently match nothing
  // and the report would go quietly empty for this bundle. Fail loud instead,
  // matching the missing-.map precedent.
  if (map.sources.length && !keepOld.length) {
    throw new Error(
      `e2e_coverage_report: sourcemap has ${map.sources.length} sources but none under /web/src/ — ` +
        "the served bundle's source paths changed shape; coverage would be silently empty",
    );
  }
  const mappings = decode(map.mappings).map((line) =>
    line.map((seg) => {
      if (seg.length <= 1) return seg;
      const ni = oldToNew[seg[1]];
      if (ni === -1) return [seg[0]];
      return seg.length === 5 ? [seg[0], ni, seg[2], seg[3], seg[4]] : [seg[0], ni, seg[2], seg[3]];
    }),
  );
  return {
    ...map,
    sources: keepOld.map((i) => map.sources[i]),
    sourcesContent: map.sourcesContent ? keepOld.map((i) => map.sourcesContent[i]) : undefined,
    mappings: encode(mappings),
  };
}

const dumps = existsSync(rawDir) ? readdirSync(rawDir).filter((f) => f.endsWith(".json")) : [];
if (!dumps.length) {
  throw new Error(
    "e2e_coverage_report: no raw dumps in reports/ts-e2e-cov/raw — run `make dashboard` (chromium) first",
  );
}

// Group every raw CDP snapshot by its served path (host:port stripped). The
// browser suites run each test against a fresh monitor server on an ephemeral
// port, so the SAME ~8.8 MB dist bundle (index-*.js) is captured once per test
// — dozens of near-identical snapshots on dozens of ports, plus the covreport
// bundle. Handing each snapshot to monocart separately made this script parse
// that bundle's 8.8 MB hidden sourcemap once PER snapshot and hold every copy
// (monocart retains each add()ed entry's sourceMap until generate()), which
// blew even a 4 GB V8 heap. So we merge each bundle's snapshots into ONE
// ScriptCov — union coverage, a statement covered by ANY test counts — with
// @bcoe/v8-coverage's mergeScriptCovs. It is offset-aware, which is required:
// V8 precise coverage only reports already-parsed functions, so the snapshots
// do NOT share a function list and a positional merge would be wrong. monocart
// then sees one entry (one parsed sourcemap) per bundle; peak RSS drops from
// >4 GB to ~330 MB.
const byBundle = new Map();
for (const f of dumps) {
  const dump = JSON.parse(readFileSync(resolve(rawDir, f), "utf8"));
  for (const entry of dump.result) {
    const key = new URL(entry.url).pathname;
    const group = byBundle.get(key);
    if (group) {
      group.push(entry);
    } else {
      byBundle.set(key, [entry]);
    }
  }
}

const entries = [];
for (const scripts of byBundle.values()) {
  // mergeScriptCovs keeps the first snapshot's (full) url, so distFileFor still
  // resolves the served bundle back to its dist file + hidden sourcemap.
  const merged = mergeScriptCovs(scripts);
  if (!merged) {
    continue;
  }
  const file = distFileFor(merged.url);
  const map = `${file}.map`;
  if (!existsSync(map)) {
    throw new Error(
      `e2e_coverage_report: missing hidden sourcemap ${map} — build.sourcemap regressed?`,
    );
  }
  entries.push({
    ...merged,
    source: readFileSync(file, "utf8"),
    sourceMap: pruneSourceMap(JSON.parse(readFileSync(map, "utf8"))),
  });
}

const report = new CoverageReport({
  name: "otto web e2e coverage",
  outputDir: outDir,
  reports: [["json", { file: "coverage-final.json" }]],
  sourceFilter: (sourcePath) => !excluded(sourcePath),
  // Key the istanbul JSON by the same absolute paths vitest's
  // coverage-final.json uses, so nyc merges them as one file set.
  // `filePath` already arrives "web/"-rooted (see the adaptation-point
  // comment above), so resolving against the repo root — not `web/` again —
  // lands on the correct absolute path.
  sourcePath: (filePath) => resolve(repo, filePath),
});
await report.add(entries);
await report.generate();
console.log(`e2e_coverage_report: wrote ${resolve(outDir, "coverage-final.json")}`);
