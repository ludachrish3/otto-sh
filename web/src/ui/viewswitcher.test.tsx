import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";

import { ViewSwitcher } from "./ViewSwitcher";

// jsdom (pinned here) doesn't implement `CSS.escape`
// (https://github.com/jsdom/jsdom/issues/3363), which react-aria's
// selection/focus-scroll utilities call unconditionally — without this,
// clicking a tab throws. Same polyfill as rangepicker.test.tsx / shell.test.tsx.
if (typeof globalThis.CSS === "undefined") {
  Object.defineProperty(globalThis, "CSS", {
    value: { escape: (value: string) => value.replace(/[^a-zA-Z0-9_-]/g, (ch) => `\\${ch}`) },
    writable: true,
  });
}

afterEach(() => {
  cleanup();
  window.location.hash = "";
});

describe("ViewSwitcher", () => {
  it("renders button-border tabs with the active view selected", () => {
    render(<ViewSwitcher active="topology" />);
    const tabs = screen.getAllByRole("tab");
    expect(tabs.map((t) => t.textContent)).toEqual(["Topology", "Hosts"]);
    expect(tabs[0].getAttribute("aria-selected")).toBe("true");
    expect(tabs[1].getAttribute("aria-selected")).toBe("false");
  });

  it("selecting the other tab navigates the hash route", async () => {
    const user = userEvent.setup();
    render(<ViewSwitcher active="topology" />);
    await user.click(screen.getByRole("tab", { name: "Hosts" }));
    expect(window.location.hash).toBe("#/hosts");
  });

  it("navigates to / for topology", async () => {
    const user = userEvent.setup();
    window.location.hash = "#/hosts";
    render(<ViewSwitcher active="hosts" />);
    await user.click(screen.getByRole("tab", { name: "Topology" }));
    expect(window.location.hash).toBe("#/");
  });
});
