import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { LinkSnapshot } from "../api/export.gen";
import type { TopoEdge } from "../data/topology";
import { EdgeHoverCard } from "../topo/EdgeHoverCard";
import { edgeSubtitle, edgeTitle } from "../topo/linkText";

afterEach(cleanup);

const appDbLink: LinkSnapshot = {
  id: "app-db",
  name: "app-db",
  protocol: "tcp",
  provenance: "declared",
  endpoints: [
    { host: "workers_w1", interface: "eth0", ip: "10.20.2.21" },
    { host: "db-01", interface: "eth0", ip: "10.20.3.31" },
  ],
};

const declared: TopoEdge = {
  id: "app-db",
  source: "workers",
  target: "db-01",
  provenance: "declared",
  link: appDbLink,
  impair: null,
  parallelIndex: 0,
};

const hopGroup: TopoEdge = {
  id: "implicit:chassis-a~edge-gw",
  source: "edge-gw",
  target: "chassis-a",
  provenance: "implicit",
  links: [appDbLink, appDbLink, appDbLink],
  impair: null,
  parallelIndex: 0,
};

const reports: TopoEdge = {
  id: "reports:mgmt-01~chassis-a",
  source: "mgmt-01",
  target: "chassis-a",
  provenance: "reports-for",
  impair: null,
  parallelIndex: 0,
};

describe("edge text", () => {
  it("names a declared link by its name", () => {
    expect(edgeTitle(declared)).toBe("app-db");
    expect(edgeSubtitle(declared)).toBe("declared · tcp");
  });

  // The inspector renders these degenerate edges as a raw id today. The hover
  // card must not repeat that.
  it("summarises a collapsed hop group rather than showing its synthetic id", () => {
    expect(edgeTitle(hopGroup)).toBe("edge-gw ⇄ chassis-a");
    expect(edgeSubtitle(hopGroup)).toBe("hop (implicit) · 3 links");
  });

  it("describes a reports-for edge, which has no link at all", () => {
    expect(edgeTitle(reports)).toBe("mgmt-01 → chassis-a");
    expect(edgeSubtitle(reports)).toBe("reports for");
  });
});

describe("EdgeHoverCard", () => {
  it("names the link and its endpoints", () => {
    render(<EdgeHoverCard edge={declared} x={10} y={20} />);
    const card = screen.getByTestId("topo-hover-app-db");
    expect(card.textContent).toContain("app-db");
    expect(card.textContent).toContain("tcp");
    expect(card.textContent).toContain("10.20.2.21");
  });

  it("renders for an edge with no link", () => {
    render(<EdgeHoverCard edge={reports} x={0} y={0} />);
    expect(screen.getByTestId("topo-hover-reports:mgmt-01~chassis-a").textContent).toContain(
      "reports for",
    );
  });
});
