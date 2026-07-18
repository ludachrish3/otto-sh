// `defineConfig` comes from "vitest/config" rather than "vite" so the `test`
// key below type-checks; it re-exports vite's own config type merged with
// vitest's, and is a drop-in for plain `vite build`/`vite dev` too.

import path from "node:path";

import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// otto monitor's MonitorServer serves this build's output straight off disk
// (see src/otto/monitor/server.py's dashboard() dist-preferred branch), so
// the base path must match the StaticFiles mount it already exposes at
// /static/dist/*. emptyOutDir keeps stale chunks from a previous build from
// lingering in the dist otto serves.
const OTTO_TARGET = process.env.VITE_OTTO_TARGET ?? "http://127.0.0.1:8080";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  base: "/static/dist/",
  // Vite does not read tsconfig.json's "paths" — this alias is the source of
  // truth for `@/*`, and vitest inherits it from this same defineConfig.
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  build: {
    outDir: "../src/otto/monitor/static/dist",
    emptyOutDir: true,
    // Hidden sourcemaps: emitted for the merged TS coverage gate
    // (make coverage-ts maps Chromium V8 coverage of THIS shipped bundle back
    // to web/src), never referenced from the bundle. They ride along in dist
    // and the wheel — that is the price of certifying the real artifact
    // instead of an instrumented second build.
    sourcemap: "hidden",
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
    include: ["src/**/*.test.ts", "src/**/*.test.tsx"],
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
        "src/main.tsx",
        "src/covreport/main.ts",
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
      // UNIT-TIER floor (browserless; what CI's web-quality job gates via
      // `make coverage-ts-unit`). The FULL floor lives in the merged gate
      // (`make coverage-ts`, web/package.json's coverage:merged): it folds in
      // the Playwright e2e leg, which is where TopologyPage.tsx and the
      // bootstrap entrypoints are exercised — the reason these numbers sit
      // below the merged gate's (the vitest leg alone cannot see e2e-only
      // coverage). Raise these only from measured vitest-only output.
      thresholds: {
        statements: 81,
        branches: 73,
        functions: 80,
        lines: 82,
      },
    },
  },
});
