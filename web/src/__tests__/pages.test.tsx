import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import App from "../App";
import { presetRange, sessionBounds } from "../data/exportDoc";
import { useReviewStore } from "../data/reviewStore";

const __dir = dirname(fileURLToPath(import.meta.url));
const KITCHEN = readFileSync(join(__dir, "../../fixtures/kitchen-sink.json"), "utf-8");

function resetStore() {
  useReviewStore.setState({
    sessions: [],
    rawDocument: null,
    sourceName: null,
    warnings: [],
    importError: null,
    activeSessionId: null,
    range: null,
  });
}

beforeEach(() => {
  window.location.hash = "#/";
});
afterEach(() => {
  // Same rationale as reviewbar.test.tsx: cleanup() is needed because
  // vitest's config doesn't set `test.globals: true`.
  cleanup();
  resetStore();
});

async function importKitchen() {
  const file = new File([KITCHEN], "kitchen-sink.json", { type: "application/json" });
  fireEvent.change(screen.getByTestId("import-input"), { target: { files: [file] } });
  await waitFor(() => expect(screen.getByTestId("overview-page")).toBeTruthy());
}

describe("Overview page", () => {
  it("renders element sections incl. the empty chassis", async () => {
    render(<App />);
    await importKitchen();
    expect(screen.getByTestId("element-section-chassis-a")).toBeTruthy();
    expect(screen.getByTestId("element-section-spare-chassis")).toBeTruthy();
    expect(screen.getByTestId("subject-link-chassis-a_lc1")).toBeTruthy();
  });
});

describe("Subject page", () => {
  it("navigates by hash and shows range-scoped series counts", async () => {
    render(<App />);
    await importKitchen();
    fireEvent.click(screen.getByTestId("subject-link-workers_w2"));
    await waitFor(() => expect(screen.getByTestId("subject-page")).toBeTruthy());
    expect(window.location.hash).toBe("#/host/workers_w2");
    expect(screen.getByTestId("subject-title").textContent).toContain("workers_w2");
    const fullText = screen.getByTestId("series-summary").textContent ?? "";

    const session = useReviewStore.getState().sessions[0];
    useReviewStore.getState().actions.setRange(presetRange(sessionBounds(session), 15));
    await waitFor(() => {
      expect(screen.getByTestId("series-summary").textContent).not.toBe(fullText);
    });
  });

  it("unknown subjects render not-found, empty store renders empty state", async () => {
    render(<App />);
    await importKitchen();
    window.location.hash = "#/host/nope";
    await waitFor(() => expect(screen.getByTestId("not-found")).toBeTruthy());
  });
});
