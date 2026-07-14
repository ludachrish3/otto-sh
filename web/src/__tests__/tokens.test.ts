import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

// A token collision is invisible to lint, typecheck, and the DOM-asserting
// browser suite: Untitled UI's theme.css defines a FULL brand ramp whose 500 is
// #9E77ED (purple), and ours is violet. Which one resolves depends purely on
// @import order in app.css. charts/palette.ts reads this token.
//
// otto: uses the repo's established fileURLToPath(import.meta.url) + join
// idiom (see health.test.ts, topology.test.ts, ...) rather than
// `new URL("../app.css", import.meta.url)` directly — Vite statically
// special-cases that exact `new URL(literal, import.meta.url)` syntax as an
// asset-URL reference and rewrites it to a dev-server http: URL, which
// node:fs then rejects ("The URL must be of scheme file").
const HERE = dirname(fileURLToPath(import.meta.url));

describe("design tokens", () => {
  it("keeps otto's brand violet, not Untitled UI's purple", () => {
    const css = readFileSync(join(HERE, "../app.css"), "utf8");
    const themeImport = css.indexOf('@import "./styles/theme.css"');
    const ourBrand = css.indexOf("--color-brand-500: #7c5cff");
    expect(themeImport).toBeGreaterThan(-1);
    expect(ourBrand).toBeGreaterThan(themeImport);
  });
});
