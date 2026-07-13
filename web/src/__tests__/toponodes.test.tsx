import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { TopoNode } from "../data/topology";
import { ElementNode, HostNode, LocalNode } from "../topo/nodes";

afterEach(cleanup);

const element: TopoNode = {
  id: "chassis-a",
  kind: "element",
  depth: 2,
  label: "chassis-a",
  element: {
    id: "chassis-a",
    type: "physical",
    explicit: false,
    description: null,
    hostIds: ["lc1", "lc2", "sup"],
    singleton: false,
  },
  rollup: ["ok", "unreachable", "down"],
  enterTarget: "/topology/chassis-a",
};

describe("ElementNode", () => {
  it("renders glyph, rollup segments with statuses, and worst data-status", () => {
    render(<ElementNode data={element} />);
    const root = screen.getByTestId("topo-node-chassis-a");
    expect(root.getAttribute("data-status")).toBe("down");
    expect(root.textContent).toContain("chassis-a");
    expect(root.textContent).toContain("3 hosts");
    const segments = root.querySelectorAll("[data-status-segment]");
    expect(segments).toHaveLength(3);
    expect(segments[1].getAttribute("data-status-segment")).toBe("unreachable");
  });

  it("shows unknown, not ok, for an element with an empty rollup", () => {
    render(<ElementNode data={{ ...element, id: "spare-chassis", rollup: [] }} />);
    expect(screen.getByTestId("topo-node-spare-chassis").getAttribute("data-status")).toBe(
      "unknown",
    );
  });
});

describe("HostNode", () => {
  it("shows slot badge and dimmed unreachable treatment", () => {
    const host: TopoNode = {
      id: "rack-a_n1",
      kind: "host",
      depth: 2,
      label: "rack-a_n1",
      host: { id: "rack-a_n1", element: "rack-a", slot: 1 } as TopoNode["host"],
      effective: "unreachable",
      enterTarget: "/host/rack-a_n1",
    };
    render(<HostNode data={{ ...host, slotBadge: true }} />);
    const root = screen.getByTestId("topo-node-rack-a_n1");
    expect(root.getAttribute("data-status")).toBe("unreachable");
    expect(root.textContent).toContain("slot 1");
    expect(root.className).toContain("opacity-60");
  });

  it("omits the slot badge when not requested", () => {
    const host: TopoNode = {
      id: "h",
      kind: "host",
      depth: 1,
      label: "h",
      host: { id: "h", element: "h" } as TopoNode["host"],
      effective: "ok",
    };
    render(<HostNode data={host} />);
    expect(screen.getByTestId("topo-node-h").textContent).not.toContain("slot");
  });

  it("renders no dangling separator when there is nothing to separate", () => {
    // "unreachable · " with nothing after it: the separator is punctuation
    // BETWEEN two parts, so it must not survive when the second part is absent.
    const host: TopoNode = {
      id: "lonely",
      kind: "host",
      depth: 1,
      label: "lonely",
      host: { id: "lonely", element: "lonely" } as TopoNode["host"],
      effective: "unreachable",
    };
    render(<HostNode data={host} />);
    const root = screen.getByTestId("topo-node-lonely");
    expect(root.textContent).toContain("unreachable");
    expect(root.textContent).not.toContain("·");
  });

  it("separates two present parts with a single ·", () => {
    const host: TopoNode = {
      id: "rack-a_n1",
      kind: "host",
      depth: 2,
      label: "rack-a_n1",
      host: { id: "rack-a_n1", element: "rack-a", slot: 1 } as TopoNode["host"],
      effective: "unreachable",
    };
    render(<HostNode data={{ ...host, slotBadge: true }} />);
    expect(screen.getByTestId("topo-node-rack-a_n1").textContent).toContain("unreachable · slot 1");
  });
});

describe("LocalNode", () => {
  it("names itself without narrating that the user is here", () => {
    const local: TopoNode = { id: "local", kind: "local", depth: 0, label: "local" };
    render(<LocalNode data={local} />);
    const root = screen.getByTestId("topo-node-local");
    expect(root.textContent).toContain("local");
    expect(root.textContent).not.toContain("you are here");
  });
});
