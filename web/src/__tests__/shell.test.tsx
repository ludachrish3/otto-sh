// New-shell behavior: empty state -> import -> loaded chrome; theme menu
// toggles the html class; import errors surface without losing data.

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

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
// (AppBar's vendored Dropdown, same polyfill reviewbar.test.tsx and
// events_panel.test.tsx need) throws on interaction here too. Polyfill per
// the CSSOM spec so real component behavior — not the test environment —
// is what's under test.
if (typeof globalThis.CSS === "undefined") {
  Object.defineProperty(globalThis, "CSS", {
    value: { escape: (value: string) => value.replace(/[^a-zA-Z0-9_-]/g, (ch) => `\\${ch}`) },
    writable: true,
  });
}

function resetStore() {
  useReviewStore.setState({
    sessions: [],
    rawMonitorSessions: null,
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
  document.documentElement.classList.remove("dark", "dark-mode");
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

  // Plan 5b final-review Finding [1]: `state.warnings` (dropped bad-timestamp
  // rows — see data/exportDoc.ts's dropInvalidTimestamps) had no render
  // site — "drop and warn" was, in practice, "drop silently." This proves
  // the real end-to-end wiring: a document with one dropped row, imported
  // through the real Import front door (not a direct store poke), surfaces
  // the warning in the shell; dismissing it hides it; a fresh warning after
  // dismissal shows again.
  it("a document with a dropped row surfaces the data-warnings banner; dismiss hides it; a fresh warning re-shows it", async () => {
    const withBadRow = JSON.stringify({
      format: 1,
      sessions: [
        {
          id: "warn-fixture",
          start: "2026-07-01T08:00:00Z",
          lab: { hosts: [{ id: "solo", element: "solo" }] },
          metrics: [
            { timestamp: "2026-07-01T08:00:00Z", host: "solo", label: "CPU %", value: 1 },
            { timestamp: "not-a-timestamp", host: "solo", label: "CPU %", value: 2 },
          ],
        },
      ],
    });
    render(<App />);
    const file = new File([withBadRow], "warn-fixture.json", { type: "application/json" });
    fireEvent.change(screen.getByTestId("import-input"), { target: { files: [file] } });
    await waitFor(() => expect(screen.getByTestId("status-text").textContent).toBe("Historical"));

    const banner = await screen.findByTestId("data-warnings-banner");
    expect(banner.textContent).toContain("dropped 1 metric with invalid timestamp");

    fireEvent.click(screen.getByTestId("data-warnings-dismiss"));
    expect(screen.queryByTestId("data-warnings-banner")).toBeNull();

    // A fresh warning (a second bad-row import) re-shows the banner rather
    // than staying suppressed by the earlier dismissal.
    const secondBadRow = JSON.stringify({
      format: 1,
      sessions: [
        {
          id: "warn-fixture-2",
          start: "2026-07-01T09:00:00Z",
          lab: { hosts: [{ id: "solo", element: "solo" }] },
          metrics: [{ timestamp: "still-not-a-timestamp", host: "solo", label: "CPU %", value: 3 }],
        },
      ],
    });
    const file2 = new File([secondBadRow], "warn-fixture-2.json", { type: "application/json" });
    fireEvent.change(screen.getByTestId("import-input"), { target: { files: [file2] } });
    const reshown = await screen.findByTestId("data-warnings-banner");
    expect(reshown.textContent).toContain("dropped 1 metric with invalid timestamp");
  });

  it("theme menu item toggles the html dark-mode class and persists", async () => {
    render(<App />);
    const before = document.documentElement.classList.contains("dark-mode");
    fireEvent.click(screen.getByTestId("overflow-menu"));
    fireEvent.click(await screen.findByTestId("menu-theme"));
    expect(document.documentElement.classList.contains("dark-mode")).toBe(!before);
    expect(localStorage.getItem("otto-theme")).toBe(before ? "light" : "dark");
  });

  // react-aria's usePress listens for the pointer-event sequence a real
  // interaction produces, not the single synthetic `click` event
  // fireEvent.click dispatches (see reviewbar.test.tsx's openSessionPicker
  // comment) — userEvent synthesizes that sequence.
  it("Import overflow item opens the file picker (menu-import -> openImportPicker)", async () => {
    const user = userEvent.setup();
    render(<App />);
    // ImportProvider mounts this hidden input and wires openImportPicker's
    // module-level `picker` callback to its .click(); spying on the real
    // instance (rather than the prototype) pins the assertion to this
    // exact input, not just "some input somewhere got clicked".
    const input = screen.getByTestId("import-input") as HTMLInputElement;
    const clickSpy = vi.spyOn(input, "click");
    await user.click(screen.getByTestId("overflow-menu"));
    await user.click(await screen.findByTestId("menu-import"));
    expect(clickSpy).toHaveBeenCalledOnce();
  });

  // AppBar's `Dropdown.Item` for export sets `isDisabled={!hasData}` — no
  // data has been imported yet at boot, so the item must render disabled.
  // (Live mode omits this item entirely in favor of ExportButton — see
  // AppBar.tsx — so this is a review/import-mode-only assertion, exercised
  // here at boot before any import.)
  it("Export overflow item is disabled with no data loaded (menu-export -> isDisabled)", async () => {
    const user = userEvent.setup();
    render(<App />);
    await user.click(screen.getByTestId("overflow-menu"));
    const exportItem = await screen.findByTestId("menu-export");
    expect(exportItem.getAttribute("aria-disabled")).toBe("true");
  });
});
