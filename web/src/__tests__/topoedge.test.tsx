import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { cleanup } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { TopoEdge } from "../data/topology";
import {
  EDGE_STYLES,
  type EdgeClass,
  EMPHASIS_WIDTH,
  edgeClass,
  edgeStyle,
  tunnelEdgeStyle,
} from "../topo/edgeStyles";

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
  // Stroke/width/dash are still identical across the three "static"
  // provenances -- there is no functional difference in what they draw.
  // Opacity is a SEPARATE axis (checked below): declared is data-plane and
  // stays full-strength; implicit/local are management and fade.
  it("draws declared, implicit and local with the same stroke, width and dash", () => {
    const strokeOnly = (s: ReturnType<typeof edgeStyle>) => {
      const { opacity: _opacity, ...rest } = s;
      return JSON.stringify(rest);
    };
    const declared = strokeOnly(edgeStyle("declared", false));
    expect(strokeOnly(edgeStyle("implicit", false))).toBe(declared);
    expect(strokeOnly(edgeStyle("local", false))).toBe(declared);
  });

  it("maps the five provenances onto three distinct strokes", () => {
    expect(edgeStyle("declared", false).strokeDasharray).toBeUndefined();
    expect(edgeStyle("declared", false).strokeWidth).toBe(1.5);
    expect(edgeStyle("dynamic", false).strokeDasharray).toBe("7 4");
    expect(edgeStyle("reports-for", false).strokeDasharray).toBe("2 5");
    const strokesOnly = ALL_PROVENANCE.map((p) => {
      const { opacity: _opacity, ...rest } = edgeStyle(p, false);
      return JSON.stringify(rest);
    });
    expect(new Set(strokesOnly).size).toBe(3);
  });

  it("emphasized state thickens the stroke", () => {
    expect(edgeStyle("declared", true).strokeWidth).toBeGreaterThan(
      edgeStyle("declared", false).strokeWidth,
    );
  });

  // The faint-management-line treatment (design doc §4): a pale management
  // edge crossing behind an element is honest and unobtrusive, and hiding it
  // costs the user real information -- so it is dimmed, never hidden.
  describe("management fade", () => {
    it("fades implicit, local and reports-for, but not declared or dynamic", () => {
      expect(edgeStyle("declared", false).opacity).toBeUndefined();
      expect(edgeStyle("dynamic", false).opacity).toBeUndefined();
      expect(edgeStyle("implicit", false).opacity).toBeDefined();
      expect(edgeStyle("local", false).opacity).toBeDefined();
      expect(edgeStyle("reports-for", false).opacity).toBeDefined();
    });

    it("is not zero -- dimmed, not hidden", () => {
      const opacity = edgeStyle("implicit", false).opacity as number;
      expect(opacity).toBeGreaterThan(0);
      expect(opacity).toBeLessThan(1);
    });

    it("uses the SAME opacity for every management provenance", () => {
      const implicitOpacity = edgeStyle("implicit", false).opacity;
      expect(edgeStyle("local", false).opacity).toBe(implicitOpacity);
      expect(edgeStyle("reports-for", false).opacity).toBe(implicitOpacity);
    });

    it("restores full opacity when emphasized (hovered or selected)", () => {
      expect(edgeStyle("implicit", true).opacity).toBeUndefined();
      expect(edgeStyle("local", true).opacity).toBeUndefined();
      expect(edgeStyle("reports-for", true).opacity).toBeUndefined();
    });

    it("never fades a data-plane or tunnel edge, emphasized or not", () => {
      expect(edgeStyle("declared", true).opacity).toBeUndefined();
      expect(edgeStyle("dynamic", true).opacity).toBeUndefined();
    });
  });
});

describe("tunnelEdgeStyle", () => {
  it("ok keeps the shipped tunnel stroke", () => {
    const s = tunnelEdgeStyle("ok", false);
    expect(s.stroke).toBe(EDGE_STYLES.tunnel.stroke);
    expect(s.strokeDasharray).toBe("7 4");
    expect(s.opacity).toBeUndefined();
  });

  it("degraded takes the warning accent, same geometry", () => {
    const s = tunnelEdgeStyle("degraded", false);
    expect(s.stroke).toBe("var(--topo-edge-tunnel-degraded)");
    expect(s.strokeDasharray).toBe("7 4");
  });

  it("uncertain ghosts", () => {
    expect(tunnelEdgeStyle("uncertain", false).opacity).toBeLessThan(1);
  });

  it("emphasis widens and restores opacity", () => {
    const s = tunnelEdgeStyle("uncertain", true);
    expect(s.strokeWidth).toBe(EDGE_STYLES.tunnel.strokeWidth + EMPHASIS_WIDTH);
    expect(s.opacity).toBeUndefined();
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

  // Same dark-mode trap as --topo-edge-reports above, applied to the
  // degraded-tunnel accent: both theme blocks must define it, and each
  // theme's value must actually differ from that theme's plain tunnel
  // stroke (--topo-edge-static), or a degraded tunnel would be visually
  // indistinguishable from a healthy one.
  it("defines --topo-edge-tunnel-degraded in BOTH themes, distinct from the tunnel stroke", () => {
    const root = block(":root {");
    const dark = block(".dark-mode {");

    for (const [theme, css] of [
      ["light", root],
      ["dark", dark],
    ] as const) {
      expect(
        value(css, "--topo-edge-tunnel-degraded"),
        `--topo-edge-tunnel-degraded in ${theme}`,
      ).toBeDefined();
    }
    expect(value(root, "--topo-edge-tunnel-degraded")).not.toBe(value(root, "--topo-edge-static"));
    expect(value(dark, "--topo-edge-tunnel-degraded")).not.toBe(value(dark, "--topo-edge-static"));
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
