import { defineConfig } from "vite";

// The coverage report is Jinja-generated static HTML (see
// src/otto/coverage/renderer/templates/index.html): its <script> tag
// references the FIXED path static/dist/covreport.js, so this build must
// emit exactly that filename — lib mode with a fileName override, no content
// hashing. IIFE so it runs as a classic deferred script, including off
// file:// URLs (reports are opened straight from disk, no server).
export default defineConfig({
  build: {
    outDir: "../src/otto/coverage/renderer/static/dist",
    emptyOutDir: true,
    lib: {
      entry: "src/covreport/main.ts",
      name: "ottoCovReport",
      formats: ["iife"],
      fileName: () => "covreport.js",
    },
    // Hidden sourcemaps: emitted for the merged TS coverage gate
    // (make coverage-ts maps Chromium V8 coverage of THIS shipped bundle back
    // to web/src), never referenced from the bundle. They ride along in dist
    // and the wheel — that is the price of certifying the real artifact
    // instead of an instrumented second build.
    sourcemap: "hidden",
  },
});
