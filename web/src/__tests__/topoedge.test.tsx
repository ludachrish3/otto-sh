import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { cleanup } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { TopoEdge } from "../data/topology";
import { EDGE_STYLES, type EdgeClass, edgeClass, edgeStyle } from "../topo/edgeStyles";

afterEach(cleanup);

const HERE = dirname(fileURLToPath(import.meta.url));
const APP_CSS = readFileSync(join(HERE, "../app.css"), "utf-8");

const ALL_PROVENANCE: TopoEdge["provenance"][] = [
  "implicit",
  "declared",
  "dynamic",
  "local",
  "reports-for",
];
const ALL_CLASSES: EdgeClass[] = ["static", "tunnel", "reports-for"];

// Shared app.css slicing/reading helpers — reused by every test below that
// needs to check what a CSS custom property actually resolves to, rather
// than trust that the TS and CSS sides agree by construction.
const block = (selector: string): string => {
  const start = APP_CSS.indexOf(selector);
  expect(start, `${selector} block not found in app.css`).toBeGreaterThan(-1);
  return APP_CSS.slice(start, APP_CSS.indexOf("}", start));
};
const value = (css: string, name: string): string | undefined =>
  new RegExp(`${name}:\\s*(#[0-9a-fA-F]{3,8})`).exec(css)?.[1]?.toLowerCase();

describe("edgeClass", () => {
  it("collapses declared, implicit and local into one static class", () => {
    // declared, implicit and local are all derived from lab config. The canvas
    // must not imply a difference that does not exist.
    expect(edgeClass("declared")).toBe("static");
    expect(edgeClass("implicit")).toBe("static");
    expect(edgeClass("local")).toBe("static");
  });

  it("keeps a tunnel and a reports-for relation in their own classes", () => {
    expect(edgeClass("dynamic")).toBe("tunnel");
    expect(edgeClass("reports-for")).toBe("reports-for");
  });
});

describe("edgeStyle", () => {
  it("draws declared, implicit and local identically", () => {
    const declared = JSON.stringify(edgeStyle("declared", false));
    expect(JSON.stringify(edgeStyle("implicit", false))).toBe(declared);
    expect(JSON.stringify(edgeStyle("local", false))).toBe(declared);
  });

  it("maps the five provenances onto three distinct strokes", () => {
    expect(edgeStyle("declared", false).strokeDasharray).toBeUndefined();
    expect(edgeStyle("declared", false).strokeWidth).toBe(1.5);
    expect(edgeStyle("dynamic", false).strokeDasharray).toBe("7 4");
    expect(edgeStyle("reports-for", false).strokeDasharray).toBe("2 5");
    const styles = ALL_PROVENANCE.map((p) => JSON.stringify(edgeStyle(p, false)));
    expect(new Set(styles).size).toBe(3);
  });

  it("emphasized state thickens the stroke", () => {
    expect(edgeStyle("declared", true).strokeWidth).toBeGreaterThan(
      edgeStyle("declared", false).strokeWidth,
    );
  });
});

describe("EDGE_STYLES", () => {
  // The legend renders from this table. A class without a row would ship a line
  // style that the key silently fails to explain.
  it("has a labelled row for every class", () => {
    for (const c of ALL_CLASSES) {
      expect(EDGE_STYLES[c].label.length).toBeGreaterThan(0);
      expect(EDGE_STYLES[c].hint.length).toBeGreaterThan(0);
    }
    expect(Object.keys(EDGE_STYLES).sort()).toEqual([...ALL_CLASSES].sort());
  });

  it("gives tunnels — and only tunnels — a casing", () => {
    expect(EDGE_STYLES.tunnel.casing).toBeDefined();
    expect(EDGE_STYLES.static.casing).toBeUndefined();
    expect(EDGE_STYLES["reports-for"].casing).toBeUndefined();
  });

  // THE DARK-MODE TRAP. static and reports-for now share a 1.5px width, so
  // colour and dash are all that separate a network link from a
  // metrics-attribution arrow. --topo-edge-static resolves to #9ca3af in dark;
  // if --topo-edge-reports has no dark alternate it resolves to #9ca3af too,
  // and the two become identical but for the dash. Asserting the two specs use
  // *different variables* would NOT catch this — distinct variables can still
  // resolve to the same grey — so this reads the resolved values out of the
  // stylesheet itself.
  it("keeps reports-for dimmer than static in BOTH themes", () => {
    const root = block(":root {");
    const dark = block(".dark-mode {");

    // Both themes define both variables...
    for (const [theme, css] of [
      ["light", root],
      ["dark", dark],
    ] as const) {
      expect(value(css, "--topo-edge-static"), `--topo-edge-static in ${theme}`).toBeDefined();
      expect(value(css, "--topo-edge-reports"), `--topo-edge-reports in ${theme}`).toBeDefined();
    }
    // ...and they never collide.
    expect(value(root, "--topo-edge-reports")).not.toBe(value(root, "--topo-edge-static"));
    expect(value(dark, "--topo-edge-reports")).not.toBe(value(dark, "--topo-edge-static"));
  });

  it("has retired the per-provenance stroke variables", () => {
    expect(APP_CSS).not.toContain("--topo-edge-implicit");
    expect(APP_CSS).not.toContain("--topo-edge-local");
    expect(APP_CSS).not.toContain("--topo-edge-declared");
  });

  // Nothing else binds the `var(--…)` names EDGE_STYLES references to the
  // custom properties app.css defines. tsc, Biome and the CSS-parsing test
  // above all stay green if one side renames a variable and the other
  // doesn't — the stroke then resolves to an invalid value, SVG falls back
  // to its initial `stroke: none`, and the edge silently vanishes from the
  // map (this is the #131/#132 failure mode: a green suite certifying a
  // broken artifact). Variable names are pulled out of EDGE_STYLES itself,
  // not re-typed here, so this can't drift into a second hand-written list.
  it("defines every CSS variable EDGE_STYLES references in :root", () => {
    const root = block(":root {");
    const varName = (ref: string): string => {
      const match = /^var\((--[\w-]+)\)$/.exec(ref);
      expect(match, `"${ref}" is not a var(--name) reference`).not.toBeNull();
      return (match as RegExpExecArray)[1];
    };

    const referenced = new Set<string>();
    for (const spec of Object.values(EDGE_STYLES)) {
      referenced.add(varName(spec.stroke));
      if (spec.casing) referenced.add(varName(spec.casing.stroke));
    }
    expect(referenced.size).toBeGreaterThan(0);

    for (const name of referenced) {
      expect(value(root, name), `${name} is referenced but not defined in :root`).toBeDefined();
    }
  });
});
