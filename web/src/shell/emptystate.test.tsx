import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { sourceState } from "../__tests__/_sourceState";
import { useReviewStore } from "../data/reviewStore";
import { EmptyState } from "./EmptyState";

afterEach(() => {
  cleanup();
  useReviewStore.setState({ source: null, pendingSource: null, sourceKindUnresolved: false });
});

const text = () => screen.getByTestId("empty-review-text").textContent;

// The empty page invites an Import only where one is offered
// (data/source.ts's importOffered); elsewhere it says the tab is waiting.
describe("EmptyState", () => {
  it("with no server behind the page, invites an Import", () => {
    render(<EmptyState />);
    expect(text()).toBe("No data loaded — import a collection to review.");
    expect(screen.getByTestId("empty-import-btn")).toBeTruthy();
  });

  it("over a live source with nothing installed yet, waits for the server", () => {
    useReviewStore.setState({ pendingSource: sourceState() });
    render(<EmptyState />);
    expect(text()).toBe("Waiting for the server's data…");
    expect(screen.queryByTestId("empty-import-btn")).toBeNull();
  });

  it("while the URL's source is unresolved, waits for the server", () => {
    useReviewStore.setState({ sourceKindUnresolved: true });
    render(<EmptyState />);
    expect(text()).toBe("Waiting for the server's data…");
    expect(screen.queryByTestId("empty-import-btn")).toBeNull();
  });
});
