import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { LinkSnapshot } from "../api/export.gen";
import type { TopoEdge } from "../data/topology";
import { LinkInspector } from "../topo/LinkInspector";

afterEach(cleanup);

const link: LinkSnapshot = {
  id: "lnk-1",
  endpoints: [
    { host: "workers_w3", interface: "eth0", ip: "10.20.2.23" },
    { host: "db-01", interface: "eth0", ip: "10.20.3.31" },
  ],
  protocol: "udp",
  provenance: "declared",
  name: "metrics-udp",
  impair: "edge-gw",
};

function edgeWith(overrides: Partial<TopoEdge>): TopoEdge {
  return {
    id: "lnk-1",
    source: "workers",
    target: "db-01",
    provenance: "declared",
    link,
    impair: "edge-gw",
    parallelIndex: 0,
    ...overrides,
  };
}

describe("LinkInspector", () => {
  it("renders link facts, impair, and the reserved NetEm section", async () => {
    render(<LinkInspector edge={edgeWith({})} onClose={vi.fn()} />);
    const panel = await screen.findByTestId("link-inspector");
    expect(panel.textContent).toContain("metrics-udp");
    expect(screen.getByTestId("inspector-protocol").textContent).toContain("udp");
    expect(screen.getByTestId("inspector-provenance").textContent).toContain("declared");
    expect(screen.getByTestId("inspector-endpoints").textContent).toContain("workers_w3");
    expect(screen.getByTestId("inspector-endpoints").textContent).toContain("10.20.3.31");
    expect(screen.getByTestId("inspector-impair").textContent).toContain("edge-gw");
    expect(screen.getByTestId("inspector-netem").textContent).toContain("Configure — coming soon");
    // Non-modal: no react-aria ModalOverlay backdrop (SlideOver's own
    // "fixed inset-0" overlay div) should exist behind the panel.
    expect(document.querySelector(".fixed.inset-0")).toBeNull();
    // Bounded by the canvas, not the viewport: a `fixed` aside spans the full
    // viewport height and covers the review bar's Apply button at <=1280px.
    expect(panel.className).toContain("absolute");
    expect(panel.className).not.toContain("fixed");
  });

  it("renders nothing when no edge is selected", () => {
    render(<LinkInspector edge={null} onClose={vi.fn()} />);
    expect(screen.queryByTestId("link-inspector")).toBeNull();
  });

  it("summarizes collapsed implicit bundles", async () => {
    const bundle = edgeWith({
      id: "implicit:chassis-a~edge-gw",
      provenance: "implicit",
      link: undefined,
      links: [link, { ...link, id: "lnk-2" }, { ...link, id: "lnk-3" }],
      impair: null,
    });
    render(<LinkInspector edge={bundle} onClose={vi.fn()} />);
    await screen.findByTestId("link-inspector");
    expect(screen.getByTestId("inspector-collapsed-note").textContent).toMatch(/3 hop links/);
  });

  it("registers no key listener while nothing is selected", () => {
    // The effect used to run on every mount of the topology page, so Escape
    // fired onClose with nothing to close.
    const add = vi.spyOn(document, "addEventListener");
    render(<LinkInspector edge={null} onClose={vi.fn()} />);
    expect(add.mock.calls.filter(([type]) => type === "keydown")).toHaveLength(0);
    add.mockRestore();
  });

  it("closes on Escape while an edge is selected", () => {
    const onClose = vi.fn();
    render(<LinkInspector edge={edgeWith({})} onClose={onClose} />);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledOnce();
  });
});
