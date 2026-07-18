import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { useReviewStore } from "../data/reviewStore";
import { ReconnectingBanner } from "./ReconnectingBanner";

afterEach(() => {
  cleanup();
  useReviewStore.setState({ mode: null, connection: "connecting" });
});

describe("ReconnectingBanner", () => {
  it("renders only in live mode with a non-live connection", () => {
    useReviewStore.setState({ mode: "live", connection: "disconnected" });
    render(<ReconnectingBanner />);
    expect(screen.getByTestId("reconnecting-banner").textContent).toContain("Reconnecting…");
  });

  it("disappears when the connection recovers", () => {
    useReviewStore.setState({ mode: "live", connection: "live" });
    render(<ReconnectingBanner />);
    expect(screen.queryByTestId("reconnecting-banner")).toBeNull();
  });

  it("never renders outside live mode, whatever the connection says", () => {
    useReviewStore.setState({ mode: null, connection: "disconnected" });
    const { rerender } = render(<ReconnectingBanner />);
    expect(screen.queryByTestId("reconnecting-banner")).toBeNull();
    useReviewStore.setState({ mode: "review", connection: "connecting" });
    rerender(<ReconnectingBanner />);
    expect(screen.queryByTestId("reconnecting-banner")).toBeNull();
  });
});
