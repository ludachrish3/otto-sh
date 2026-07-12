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
      // (stmts 83.63 / branch 75.49 / funcs 82.73 / lines 84.78), mirroring the
      // Python gate's headroom (CI floor 90 vs ~93.25 actual). Catches
      // regressions without breaking on trivial refactors; raise it as
      // component test coverage grows (see the tooling follow-ups).
      // raised after the shell rebuild: stmts +16.2, branches +24.5, funcs +13.4, lines +15.8.
      // raised after the views phase (Plan 3): stmts +4.89, branches +0.57, funcs +4.10, lines +5.38.
      // lowered after the topology phase (Plan 4): stmts 85->81 (measured 83.63),
      // branches 78->73 (measured 75.49), funcs 83->80 (measured 82.73),
      // lines 86->82 (measured 84.78). Deliberate drop: TopologyPage.tsx wires
      // @xyflow/react to a live ResizeObserver/canvas and is exercised by the
      // Playwright dashboard e2e instead of jsdom RTL (3.57% stmts / 0% funcs
      // here by design), and the topo/ node+edge components carry only
      // structural RTL coverage — both pull the global average down even
      // though behavior is fully covered end-to-end.
      thresholds: {
        statements: 81,
        branches: 73,
        functions: 80,
        lines: 82,
      },
    },
  },
});
