// Behavior smoke tests for the ui/ primitives: they render accessible
// roles, forward test ids, and fire their callbacks. Styling is not a
// contract and is deliberately unasserted.
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Badge } from "../ui/Badge";
import { Button } from "../ui/Button";
import { OverflowMenu } from "../ui/Menu";
import { Select } from "../ui/Select";
import { SlideOver } from "../ui/SlideOver";
import { TextInput } from "../ui/TextInput";
import { ToggleGroup } from "../ui/ToggleGroup";

// jsdom (pinned here) doesn't implement `CSS.escape`
// (https://github.com/jsdom/jsdom/issues/3363), which react-aria's
// selection utilities call unconditionally when a Menu/Select autofocuses
// or scrolls a selected/focused item into view. Without this, every
// portal-based primitive below throws on interaction. Polyfill per the
// CSSOM spec so real component behavior — not the test environment — is
// what's under test.
if (typeof globalThis.CSS === "undefined") {
  Object.defineProperty(globalThis, "CSS", {
    value: { escape: (value: string) => value.replace(/[^a-zA-Z0-9_-]/g, (ch) => `\\${ch}`) },
    writable: true,
  });
}

// vitest's config doesn't set `test.globals: true` (see app.test.tsx),
// so @testing-library/react's automatic afterEach(cleanup) registration
// never kicks in; every other test file in this project registers it
// explicitly, so this one follows suit — without it, popovers/menus from
// one test's render() linger in the document for the next test's queries.
afterEach(cleanup);

describe("Button", () => {
  it("renders a button role and fires onPress", () => {
    const onPress = vi.fn();
    render(
      <Button onPress={onPress} testId="btn">
        Go
      </Button>,
    );
    fireEvent.click(screen.getByTestId("btn"));
    expect(onPress).toHaveBeenCalledOnce();
    expect(screen.getByRole("button", { name: "Go" })).toBeTruthy();
  });
});

describe("OverflowMenu", () => {
  it("opens on trigger click and fires the item action", () => {
    const onAction = vi.fn();
    render(
      <OverflowMenu
        items={[{ id: "import", label: "Import…", onAction, testId: "menu-import" }]}
      />,
    );
    fireEvent.click(screen.getByTestId("overflow-menu"));
    fireEvent.click(screen.getByTestId("menu-import"));
    expect(onAction).toHaveBeenCalledOnce();
  });

  it("renders disabled items as disabled", () => {
    render(
      <OverflowMenu
        items={[{ id: "x", label: "X", onAction: () => {}, isDisabled: true, testId: "menu-x" }]}
      />,
    );
    fireEvent.click(screen.getByTestId("overflow-menu"));
    expect(screen.getByTestId("menu-x").getAttribute("aria-disabled")).toBe("true");
  });
});

describe("Select", () => {
  it("shows items and reports selection", () => {
    const onSelectionChange = vi.fn();
    render(
      <Select
        label="Session"
        items={[
          { id: "a", label: "baseline" },
          { id: "b", label: "rewired" },
        ]}
        selectedKey="a"
        onSelectionChange={onSelectionChange}
        testId="session-picker"
      />,
    );
    fireEvent.click(screen.getByTestId("session-picker"));
    // react-aria-components' Select also mirrors its options into a
    // visually-hidden native <select> (for autofill/native-form
    // support), so an unscoped getByText("rewired") matches both that
    // <option> and the visible popover item — scope to the listbox that
    // the popover renders.
    fireEvent.click(within(screen.getByRole("listbox")).getByText("rewired"));
    expect(onSelectionChange).toHaveBeenCalledWith("b");
  });
});

describe("ToggleGroup", () => {
  it("marks the selected option and reports clicks", () => {
    const onSelect = vi.fn();
    render(
      <ToggleGroup
        options={[
          { id: "full", label: "Full" },
          { id: "15m", label: "15m" },
        ]}
        selectedId="full"
        onSelect={onSelect}
        testId="range-presets"
      />,
    );
    // react-aria-components' RadioGroup renders each option as a native
    // `<input type="radio">` under a visually-hidden wrapper: selection is
    // exposed via the native `checked` property (per useRadio in
    // react-aria 1.19.0), not an `aria-checked` attribute — there isn't
    // one to read via getAttribute.
    const full = screen.getByRole("radio", { name: "Full" }) as HTMLInputElement;
    expect(full.checked).toBe(true);
    fireEvent.click(screen.getByRole("radio", { name: "15m" }));
    expect(onSelect).toHaveBeenCalledWith("15m");
  });
});

describe("Badge / TextInput", () => {
  it("render content and forward test ids", () => {
    const onChange = vi.fn();
    render(
      <>
        <Badge tone="historical" testId="tag">
          HISTORICAL
        </Badge>
        <TextInput label="From" value="2026-07-01T08:00" onChange={onChange} testId="from" />
      </>,
    );
    expect(screen.getByTestId("tag").textContent).toBe("HISTORICAL");
    fireEvent.change(screen.getByTestId("from"), { target: { value: "2026-07-01T09:00" } });
    expect(onChange).toHaveBeenCalledWith("2026-07-01T09:00");
  });
});

describe("SlideOver", () => {
  it("renders children when open and calls onClose on dismiss", async () => {
    const onClose = vi.fn();
    render(
      <SlideOver isOpen onClose={onClose} title="Events" testId="events-panel">
        <p>content</p>
      </SlideOver>,
    );
    const panel = await screen.findByTestId("events-panel");
    expect(panel.textContent).toContain("content");
    fireEvent.click(screen.getByLabelText("Close"));
    expect(onClose).toHaveBeenCalled();
  });

  it("renders nothing when closed", () => {
    render(
      <SlideOver isOpen={false} onClose={() => {}} title="Events">
        <p>content</p>
      </SlideOver>,
    );
    expect(screen.queryByText("content")).toBeNull();
  });
});
