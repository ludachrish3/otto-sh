// `defineConfig` comes from "vitest/config" rather than "vite" so the `test`
// key below type-checks; it re-exports vite's own config type merged with
// vitest's, and is a drop-in for plain `vite build`/`vite dev` too.

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
  build: {
    outDir: "../src/otto/monitor/static/dist",
    emptyOutDir: true,
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
      reporter: ["text", "html"],
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
      ],
      // Ratchet floor: ~2-3% below the current measured baseline
      // (stmts 83.3 / branch 80.11 / funcs 82.03 / lines 83.53), mirroring the
      // Python gate's headroom (CI floor 92 vs ~94.7 actual). Catches
      // regressions without breaking on trivial refactors; raise it as
      // component test coverage grows (see the tooling follow-ups).
      // Re-ratcheted post monitor-ui-scaffold: the legacy shell wipe removed
      // large partially-covered files, and the new review-shell components
      // came in near-fully tested, pushing coverage up ~15-18 points across
      // the board (previous baseline: stmts 67.1 / branch 55.6 / funcs 68.6 /
      // lines 67.7).
      thresholds: {
        statements: 81,
        branches: 78,
        functions: 79,
        lines: 81,
      },
    },
  },
});
