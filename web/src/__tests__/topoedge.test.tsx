import { cleanup } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { edgeStyle } from "../topo/LinkEdge";

afterEach(cleanup);

describe("edgeStyle", () => {
  it("maps provenance to distinct strokes", () => {
    expect(edgeStyle("implicit", false).strokeDasharray).toBeUndefined();
    expect(edgeStyle("dynamic", false).strokeDasharray).toBe("7 4");
    expect(edgeStyle("reports-for", false).strokeDasharray).toBe("2 5");
    expect(edgeStyle("declared", false).strokeWidth).toBe(2);
    const styles = ["implicit", "declared", "dynamic", "local", "reports-for"].map((p) =>
      JSON.stringify(edgeStyle(p as Parameters<typeof edgeStyle>[0], false)),
    );
    expect(new Set(styles).size).toBe(5);
  });

  it("selected state thickens the stroke", () => {
    expect(edgeStyle("declared", true).strokeWidth).toBeGreaterThan(
      edgeStyle("declared", false).strokeWidth as number,
    );
  });
});
