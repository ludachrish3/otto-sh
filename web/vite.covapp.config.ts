// `import.meta.dirname`, not `__dirname`: web/package.json is
// `"type": "module"`, so this file is an ES module and `__dirname` is not a
// binding in it — it only resolved because Vite pre-bundles the config
// (biome correctness/noGlobalDirnameFilename). Same change in vite.config.ts.
import { resolve } from "node:path";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig, type Plugin } from "vite";

/** file:// cannot load ES modules; Jenkins CSP forbids inline scripts. Emit one classic
 *  IIFE script + strip module attributes from the HTML Vite generates. */
function classicScript(): Plugin {
  return {
    name: "otto-classic-script",
    enforce: "post",
    generateBundle(_opts, bundle) {
      const html = bundle["covapp.html"];
      if (html && html.type === "asset") {
        html.fileName = "index.html";
        html.source = String(html.source)
          .replaceAll(' type="module"', " defer")
          .replaceAll(" crossorigin", "");
      }
    },
  };
}

export default defineConfig({
  plugins: [react(), tailwindcss(), classicScript()],
  base: "./",
  resolve: { alias: { "@": resolve(import.meta.dirname, "./src") } },
  build: {
    outDir: "../src/otto/_webassets/covapp",
    emptyOutDir: true,
    // Vite's default (cssCodeSplit: true) is fine for "es"/"cjs" output, but
    // for "iife"/"umd" formats it injects CSS via a `document.createElement
    // ('style')` call baked into the JS bundle instead of emitting a
    // stylesheet — there is no separate file for check_brand_tokens.sh to
    // grep, and it defeats scripts/check_airgap.sh's CSS/HTML file scan.
    // false forces ONE real dist/covapp.css, referenced by a plain
    // `<link rel="stylesheet">` Vite injects into the HTML — no JS required
    // to see first paint, which also matters for file:// (styles apply
    // before dist/covapp.js finishes evaluating).
    cssCodeSplit: false,
    sourcemap: "hidden",
    chunkSizeWarningLimit: 2_000, // bundle-size ceiling (spec §10) — warnings are build failures
    rollupOptions: {
      input: resolve(import.meta.dirname, "covapp.html"),
      output: {
        format: "iife",
        // NOT inlineDynamicImports: true — Vite 8 ships Rolldown, which
        // already forces single-chunk output for "iife" (codeSplitting:
        // false is implicit in the format) and warns that the option is a
        // no-op. Any dynamic import() a later task adds still lands in this
        // one file; nothing here relies on the option actually doing
        // anything, so it stays out rather than shipping a build warning
        // build_web_no_warnings.sh's "(!)" grep doesn't happen to catch.
        entryFileNames: "dist/covapp.js",
        assetFileNames: "dist/covapp.[ext]",
      },
    },
  },
});
