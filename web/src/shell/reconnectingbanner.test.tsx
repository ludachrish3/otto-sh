import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { sourceState } from "../__tests__/_sourceState";
import { useReviewStore } from "../data/reviewStore";
import { ReconnectingBanner } from "./ReconnectingBanner";

const live = sourceState();
const review = sourceState({ mode: "review", kind: "json", source: "run.json" });

afterEach(() => {
  cleanup();
  useReviewStore.setState({ source: null, pendingSource: null, connection: "connecting" });
});

describe("ReconnectingBanner", () => {
  it("renders in live mode with a non-live connection", () => {
    useReviewStore.setState({ source: live, connection: "disconnected" });
    render(<ReconnectingBanner />);
    expect(screen.getByTestId("reconnecting-banner").textContent).toContain("Reconnecting…");
  });

  it("also renders while live is still CONNECTING — the store-default and reconnect-in-progress state", () => {
    // The banner's condition is "a server source and not connection ===
    // 'live'"; without this arm, a regression to "only when disconnected"
    // (hiding the banner during the initial connect and every reconnect
    // attempt) passes the other tests.
    useReviewStore.setState({ source: live, connection: "connecting" });
    render(<ReconnectingBanner />);
    expect(screen.getByTestId("reconnecting-banner").textContent).toContain("Reconnecting…");
  });

  it("disappears when the connection recovers", () => {
    useReviewStore.setState({ source: live, connection: "live" });
    render(<ReconnectingBanner />);
    expect(screen.queryByTestId("reconnecting-banner")).toBeNull();
  });

  // Review mode streams too (#958): a lost connection there means pushes
  // from other tabs stop arriving, the same as live.
  it("review mode shows the banner while disconnected", () => {
    useReviewStore.setState({ source: review, connection: "disconnected" });
    const { rerender } = render(<ReconnectingBanner />);
    expect(screen.getByTestId("reconnecting-banner").textContent).toContain("Reconnecting…");
    act(() => {
      useReviewStore.setState({ connection: "live" });
    });
    rerender(<ReconnectingBanner />);
    expect(screen.queryByTestId("reconnecting-banner")).toBeNull();
  });

  // A live run with nothing recorded yet: its mode answered (the source is
  // pending), its snapshot did not, and the tab retries through reconnects.
  it("renders for a pending source with nothing installed: the tab is retrying", () => {
    useReviewStore.setState({ pendingSource: live, connection: "disconnected" });
    render(<ReconnectingBanner />);
    expect(screen.getByTestId("reconnecting-banner").textContent).toContain("Reconnecting…");
  });

  it("never renders with no server source, whatever the connection says", () => {
    useReviewStore.setState({ connection: "disconnected" });
    render(<ReconnectingBanner />);
    expect(screen.queryByTestId("reconnecting-banner")).toBeNull();
  });
});
