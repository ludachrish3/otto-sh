// New-shell behavior: empty state -> import -> loaded chrome; theme menu
// toggles the html class; import errors surface without losing data.

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import App from "../App";
import { useReviewStore } from "../data/reviewStore";

// `new URL(relative, import.meta.url)` (the brief's original form) throws
// "The URL must be of scheme file" under this project's vitest/jsdom setup;
// reviewstore.test.ts's fileURLToPath+dirname+join pattern is what already
// works here, so shell.test.tsx follows it for consistency.
const __dir = dirname(fileURLToPath(import.meta.url));
const MINIMAL = readFileSync(join(__dir, "../../fixtures/minimal.json"), "utf-8");

// jsdom (pinned here) doesn't implement `CSS.escape`
// (https://github.com/jsdom/jsdom/issues/3363), which react-aria's
// selection utilities call unconditionally when a Menu autofocuses or
// scrolls a selected/focused item into view. Without this, the ⋯ menu
// (OverflowMenu, from ui.test.tsx's same fix) throws on interaction here
// too. Polyfill per the CSSOM spec so real component behavior — not the
// test environment — is what's under test.
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

afterEach(() => {
  // vitest's config doesn't set `test.globals: true`, so
  // @testing-library/react's automatic afterEach(cleanup) registration
  // never kicks in — without this, a menu portal from one test's render()
  // lingers in the document for the next test's queries.
  cleanup();
  resetStore();
  localStorage.clear();
  document.documentElement.classList.remove("dark");
});

async function importMinimal() {
  const file = new File([MINIMAL], "minimal.json", { type: "application/json" });
  fireEvent.change(screen.getByTestId("import-input"), { target: { files: [file] } });
  await waitFor(() => expect(screen.getByTestId("status-text").textContent).toBe("Historical"));
}

describe("App shell", () => {
  it("boots to the empty review state with no backend fetches", () => {
    render(<App />);
    expect(screen.getByTestId("empty-review")).toBeTruthy();
    expect(screen.getByTestId("status-text").textContent).toBe("No data");
  });

  it("imports a fixture through the hidden input", async () => {
    render(<App />);
    await importMinimal();
    expect(screen.queryByTestId("empty-review")).toBeNull();
  });

  it("surfaces an import error and keeps prior data", async () => {
    render(<App />);
    await importMinimal();
    const bad = new File(["{}"], "bad.json", { type: "application/json" });
    fireEvent.change(screen.getByTestId("import-input"), { target: { files: [bad] } });
    const banner = await screen.findByTestId("import-error");
    expect(banner.textContent).toContain(useReviewStore.getState().importError);
    expect(screen.getByTestId("status-text").textContent).toBe("Historical");
    // Prior data is still rendered underneath the banner, not replaced by it.
    expect(screen.getByTestId("overview-page")).toBeTruthy();
    expect(screen.getByTestId("subject-link-solo")).toBeTruthy();

    fireEvent.click(screen.getByTestId("import-error-dismiss"));
    expect(screen.queryByTestId("import-error")).toBeNull();
    expect(useReviewStore.getState().importError).toBeNull();
  });

  it("a subsequent successful import clears a prior import error", async () => {
    render(<App />);
    await importMinimal();
    const bad = new File(["{}"], "bad.json", { type: "application/json" });
    fireEvent.change(screen.getByTestId("import-input"), { target: { files: [bad] } });
    await screen.findByTestId("import-error");

    // Re-importing good data (no dismiss click) must clear the stale banner
    // itself — a fresh import is a stronger signal than the dismiss button.
    await importMinimal();
    expect(screen.queryByTestId("import-error")).toBeNull();
    expect(useReviewStore.getState().importError).toBeNull();
  });

  it("theme menu item toggles the html dark class and persists", async () => {
    render(<App />);
    const before = document.documentElement.classList.contains("dark");
    fireEvent.click(screen.getByTestId("overflow-menu"));
    fireEvent.click(await screen.findByTestId("menu-theme"));
    expect(document.documentElement.classList.contains("dark")).toBe(!before);
    expect(localStorage.getItem("otto-theme")).toBe(before ? "light" : "dark");
  });
});
