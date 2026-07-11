// Review-bar behavior against the drift fixture (3 sessions, evolving lab)
// — the config-drift acceptance path: switching sessions re-renders under
// THAT session's lab.

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import App from "../App";
import { useReviewStore } from "../data/reviewStore";

// `new URL(relative, import.meta.url)` throws "The URL must be of scheme
// file" under this project's vitest/jsdom setup (see shell.test.tsx) —
// fileURLToPath+dirname+join is the pattern that works here.
const __dir = dirname(fileURLToPath(import.meta.url));
const DRIFT = readFileSync(join(__dir, "../../fixtures/drift.json"), "utf-8");
const MINIMAL = readFileSync(join(__dir, "../../fixtures/minimal.json"), "utf-8");

// jsdom (pinned here) doesn't implement `CSS.escape`
// (https://github.com/jsdom/jsdom/issues/3363), which react-aria's
// selection utilities call unconditionally when a Menu/Select autofocuses
// or scrolls a selected/focused item into view. Without this, the session
// picker Select throws on interaction. Polyfill per the CSSOM spec so real
// component behavior — not the test environment — is what's under test.
if (typeof globalThis.CSS === "undefined") {
  Object.defineProperty(globalThis, "CSS", {
    value: { escape: (value: string) => value.replace(/[^a-zA-Z0-9_-]/g, (ch) => `\\${ch}`) },
    writable: true,
  });
}

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
  // vitest's config doesn't set `test.globals: true`, so
  // @testing-library/react's automatic afterEach(cleanup) registration
  // never kicks in — without this, a popover/menu portal from one test's
  // render() lingers in the document for the next test's queries.
  cleanup();
  resetStore();
});

async function importText(text: string, name: string) {
  const file = new File([text], name, { type: "application/json" });
  fireEvent.change(screen.getByTestId("import-input"), { target: { files: [file] } });
  await waitFor(() => expect(screen.getByTestId("review-bar")).toBeTruthy());
}

describe("ReviewBar", () => {
  it("shows tag + source, hides the session picker for single-session files", async () => {
    render(<App />);
    await importText(MINIMAL, "minimal.json");
    expect(screen.getByTestId("historical-tag").textContent).toBe("HISTORICAL");
    expect(screen.getByTestId("source-name").textContent).toBe("minimal.json");
    expect(screen.queryByTestId("session-picker")).toBeNull();
  });

  it("switches sessions and re-renders that session's lab (drift)", async () => {
    render(<App />);
    await importText(DRIFT, "drift.json");
    expect(screen.getByTestId("session-picker")).toBeTruthy();
    // baseline lab: no workers_w1
    expect(screen.queryByTestId("subject-link-workers_w1")).toBeNull();
    fireEvent.click(screen.getByTestId("session-picker"));
    // react-aria-components' Select also mirrors its options into a
    // visually-hidden native <select> (for autofill/native-form support),
    // so an unscoped getByText("expanded") matches both that <option> and
    // the visible popover item — scope to the listbox the popover renders
    // (see ui.test.tsx).
    fireEvent.click(within(screen.getByRole("listbox")).getByText("expanded"));
    await waitFor(() => expect(screen.getByTestId("subject-link-workers_w1")).toBeTruthy());
    expect(screen.getByTestId("subject-link-workers_w2")).toBeTruthy();
    fireEvent.click(screen.getByTestId("session-picker"));
    fireEvent.click(within(screen.getByRole("listbox")).getByText("rewired"));
    await waitFor(() => expect(screen.queryByTestId("subject-link-workers_w2")).toBeNull());
    expect(screen.getByTestId("subject-link-edge-gw")).toBeTruthy();
  });

  it("reset restores the first session and full range", async () => {
    render(<App />);
    await importText(DRIFT, "drift.json");
    fireEvent.click(screen.getByTestId("session-picker"));
    fireEvent.click(within(screen.getByRole("listbox")).getByText("rewired"));
    fireEvent.click(screen.getByTestId("range-reset"));
    await waitFor(() =>
      expect(useReviewStore.getState().activeSessionId).toBe(
        useReviewStore.getState().sessions[0].id,
      ),
    );
    expect(useReviewStore.getState().range).toBeNull();
  });
});
