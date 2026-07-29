// `defineConfig` comes from "vitest/config" rather than "vite" so the `test`
// key below type-checks; it re-exports vite's own config type merged with
// vitest's, and is a drop-in for plain `vite build`/`vite dev` too.

import path from "node:path";
// Explicit `node:process` import, and `import.meta.dirname` below rather than
// `__dirname`: web/package.json is `"type": "module"`, so this file IS an ES
// module and neither `process` nor `__dirname` is a real binding in it. Both
// only ever resolved because Vite pre-bundles the config before evaluating it.
// Naming the source makes the file honest on its own terms (biome
// correctness/noProcessGlobal + noGlobalDirnameFilename).
import process from "node:process";

import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// otto monitor's MonitorServer serves this build's output straight off disk
// (see src/otto/monitor/server.py's dashboard() dist-preferred branch), so
// the base path must match the StaticFiles mount it already exposes at
// /static/dist/* — outDir moves freely (src/otto/_webassets/monitor/), the
// URL space does not. emptyOutDir keeps stale chunks from a previous build
// from lingering in the dist otto serves.
const OTTO_TARGET = process.env["VITE_OTTO_TARGET"] ?? "http://127.0.0.1:8080";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  base: "/static/dist/",
  // Vite does not read tsconfig.json's "paths" — this alias is the source of
  // truth for `@/*`, and vitest inherits it from this same defineConfig.
  resolve: {
    alias: {
      "@": path.resolve(import.meta.dirname, "./src"),
    },
  },
  build: {
    outDir: "../src/otto/_webassets/monitor/dist",
    emptyOutDir: true,
    // Hidden sourcemaps: emitted for the merged TS coverage gate
    // (make coverage-ts maps Chromium V8 coverage of THIS shipped bundle back
    // to web/src), never referenced from the bundle. They ride along in dist
    // and the wheel — that is the price of certifying the real artifact
    // instead of an instrumented second build.
    sourcemap: "hidden",
    // Explicit bundle budget, not a warning mute. The default 500 kB limit
    // warns on every build: this dashboard ships ECharts + React Flow in one
    // bundle ON PURPOSE — MonitorServer serves it off disk over
    // localhost/LAN in air-gapped labs (see `base` above), so the CDN-era
    // rationale for aggressive code-splitting doesn't apply, and a single
    // artifact keeps the air-gap gate (scripts/check_airgap.sh) trivially
    // auditable. 2 300 kB = the 2026-07 bundle (~2 110 kB) plus headroom;
    // `make web` runs vite through scripts/build_web_no_warnings.sh, which
    // turns any (!) warning into a BUILD FAILURE — so growth past this
    // number stops the build, and raising it is a reviewed edit here, same
    // deal as the Python import-budget guard.
    chunkSizeWarningLimit: 2_300,
  },
  server: {
    proxy: {
      // `make web-dev` runs only the Vite dev server, not otto's own
      // MonitorServer, so /api calls from the dashboard need to be forwarded
      // to a real running instance. Default assumes `otto monitor` on its
      // usual localhost:8080; override with VITE_OTTO_TARGET=http://host:port
      // when the monitor is bound elsewhere.
      "/api": {
        target: OTTO_TARGET,
        changeOrigin: true,
      },
    },
  },
  test: {
    // jsdom (not "node") because theme.ts/components touch localStorage and
    // document.body — store-only reducer tests don't need it, but component
    // tests added by later Phase 2 tasks will, and one environment for the
    // whole web/ vitest project is simpler than per-file overrides.
    environment: "jsdom",
    // Isolation, measured free at 928/928 (2026-07-28). Without these a
    // vi.spyOn/vi.stubGlobal/vi.stubEnv in one test survives into the next, so
    // a suite can pass because of a leak rather than in spite of one. 16 of
    // the 77 test files already do this by hand in their own afterEach
    // (vi.restoreAllMocks/vi.unstubAllGlobals) — which protects exactly the
    // files that remembered to write one, and no new file thereafter.
    restoreMocks: true,
    unstubEnvs: true,
    unstubGlobals: true,
    // Fails any test that reaches its end without asserting — the analogue of
    // a pytest test whose body is all setup and no assert. Every existing test
    // already asserts. Note it counts vitest `expect()` calls ONLY: a bare
    // `await screen.findByTestId(...)` throws when the element never appears,
    // but does not count as an assertion (verified), so a test whose only
    // check is a findBy*/getBy* needs an explicit expect as well.
    expect: { requireAssertions: true },
    // Randomized order, the pytest-randomly analogue (shuffles BOTH the file
    // order and the tests within each file). Fixed order hides cross-test
    // coupling: it hid a missing Testing Library cleanup in clock.test.tsx
    // (render counters reading the previous test's still-mounted tree) and an
    // unawaited chunk load in FilePage.test.tsx (a state update applied
    // outside act() after the test body returned), both of which passed for
    // as long as the file order never changed. The seed is not pinned,
    // matching pytest-randomly: vitest prints `Running tests with seed "N"` in
    // every run's header, so replaying a red CI run is
    // `npx vitest run --sequence.shuffle --sequence.seed=N`.
    sequence: { shuffle: true },
    include: ["src/**/*.test.ts", "src/**/*.test.tsx"],
    // Console warnings are test failures (see vitest.setup.ts) — a warning
    // that only scrolls past in coverage output is a warning nobody fixes.
    setupFiles: ["./vitest.setup.ts"],
    coverage: {
      // v8 provider (matches @vitest/coverage-v8); parity with the Python
      // pytest-cov gate. Report term-missing + html like pyproject's addopts.
      provider: "v8",
      reporter: ["text", "html", "json"], // json feeds the merged gate (make coverage-ts)
      include: ["src/**"],
      exclude: [
        // Tests, generated wire types (owned by scripts/gen_web_types.sh and
        // the `make web` drift gate), type-only declarations, and the two
        // bootstrap entrypoints that only wire the app to the DOM (exercised
        // by the Playwright dashboard e2e, not unit tests).
        "src/**/*.test.{ts,tsx}",
        "src/__tests__/**",
        "src/**/*.d.ts",
        "src/api/types.gen.ts",
        "src/api/export.gen.ts",
        "src/main.tsx",
        "src/covapp/main.tsx",
        // Vendored Untitled UI source — not ours to test. web/src/ui/** (our
        // own components) stays measured. Same vendor boundary Biome's
        // files.includes excludes from format/lint; see web/README.md
        // ("Vendored source (Untitled UI)") for the full rationale and the
        // never-hand-edit rule.
        "src/components/**",
        "src/styles/**",
        "src/utils/cx.ts",
        "src/utils/is-react-component.ts",
        "src/hooks/use-breakpoint.ts",
        "src/hooks/use-resize-observer.ts",
      ],
      // UNIT-TIER floor (browserless; what CI's check-ts job gates via
      // `make coverage-ts-unit`). The FULL floor lives in the merged gate
      // (`make coverage-ts`, web/package.json's coverage:merged): it folds in
      // the Playwright e2e leg, which is where TopologyPage.tsx and the
      // bootstrap entrypoints are exercised — the reason these numbers sit
      // below the merged gate's (the vitest leg alone cannot see e2e-only
      // coverage). Raise these only from measured vitest-only output.
      //
      // PER-FILE FLOORS on the merged gate — tier 6, measured 2026-07-28 at
      // 0af2e833. `make coverage-ts` is 1:40.66 wall clock end to end (cold,
      // browser lane included), so it stays on every push; nothing about
      // Tier 6 was decided by runtime. The merged report is 87 files,
      // aggregate 92.71 / 79.69 / 94.63 / 97.08 against the 91/78/94/95
      // floors.
      //
      // A per-file floor at PERCENTAGES was measured and DECLINED:
      //
      //   - at the aggregate values it fails 36 of 87 files (77 breaches).
      //   - the sweep, uniform floor -> files failing: 91→58, 85→45, 80→32,
      //     75→18, 70→12, 60→9, 50→4, 40→2, 25→0. The highest value that
      //     passes today is 25, i.e. 53 points below the aggregate branches
      //     floor. A gate that cannot fail is not a gate.
      //   - per-metric floors at the current minima (26/25/50/38) pass with
      //     ZERO headroom: functions is pinned by topo/LinkEdge.tsx at
      //     exactly 50.00 and branches by covapp/chrome/ShortcutsDialog.tsx
      //     at exactly 25.00, so unrelated drift in either file reddens it.
      //   - exemptions cannot rescue a higher floor. nyc's --exclude removes
      //     the file from the coverage MAP, so it also raises the aggregate
      //     (measured: excluding LinkEdge.tsx alone moves the aggregate to
      //     93.01 / 80.05 / 94.74 / 97.39). Exempting requires a SECOND nyc
      //     process, and a floor of 80 would need 32 of 87 files in its
      //     allow-list.
      //
      // And the premise per-file floors exist for does not hold here. With
      // the floors as they stand the aggregate already reddens after only 11
      // uncovered functions, 74 uncovered lines, 108 statements or 115
      // branches are added ANYWHERE — against a median measured file of 23
      // lines (mean 39). A new module of median size or larger landing
      // untested already fails the aggregate; functions carries just 0.63
      // points of headroom. Percentages on a 23-line file are quantisation
      // noise anyway: shell/EmptyState.tsx "fails" branches at 50.00% because
      // it has two branches and one is untaken.
      //
      // What IS kept is the one case the aggregate genuinely misses — a file
      // SMALLER than that, landing with nothing exercising it at all. Hence
      // the second invocation in coverage:merged: `--per-file` at 1, which
      // fails only a file at literally 0%. Headroom is 24-49 points on every
      // metric (lowest live values: branches 25.00, statements 26.92, lines
      // 38.88, functions 50.00), it needs no exemption list, and istanbul
      // reports 100% for a metric whose total is 0, so the 10 branch-free and
      // 5 function-free files here cannot false-fire.
      //
      // One asymmetry to know before touching this: the merged report's file
      // set is NOT this `exclude` list's complement. src/main.tsx and
      // src/covapp/main.tsx are excluded here yet appear in the merged report
      // (the V8 e2e leg loads them), which is why a per-file percentage floor
      // would have gated two files this config deliberately does not measure.
      // The escape hatch for a genuinely unexercisable new file is still this
      // list: excluded here AND never loaded by the browser lane means absent
      // from both legs, so absent from the merged report.
      thresholds: {
        statements: 81,
        branches: 73,
        functions: 80,
        lines: 82,
      },
    },
  },
});
