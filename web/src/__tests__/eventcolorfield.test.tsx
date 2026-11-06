// web/src/__tests__/eventcolorfield.test.tsx
// EventColorField (#858): the event editor's named colour swatches. The
// assertion surface is the accessibility tree -- option roles and names,
// aria-selected, the tooltip role -- plus the persistent name's testid.
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { EventColorField } from "../ui/EventColorField";

// jsdom lacks CSS.escape; react-aria's listbox focus utilities call it
// unconditionally (same polyfill as eventeditor.test.tsx).
if (typeof globalThis.CSS === "undefined") {
  Object.defineProperty(globalThis, "CSS", {
    value: { escape: (value: string) => value.replace(/[^a-zA-Z0-9_-]/gu, (ch) => `\\${ch}`) },
    writable: true,
  });
}

afterEach(() => {
  cleanup();
});

/** A controlled host, the way EventEditor drives the field. */
function Host({ initial, onChange }: { initial: string; onChange?: (hex: string) => void }) {
  const [value, setValue] = useState(initial);
  return (
    <EventColorField
      value={value}
      dash="dash"
      onChange={(hex) => {
        onChange?.(hex);
        setValue(hex);
      }}
    />
  );
}

function selected(name: string): string | null {
  return screen.getByRole("option", { name }).getAttribute("aria-selected");
}

describe("EventColorField", () => {
  it("each preset is an option named Grey, Violet, Green, Red", () => {
    render(<Host initial="#888888" />);
    const options = screen.getAllByRole("option");
    expect(options.map((o) => o.getAttribute("aria-label"))).toEqual([
      "Grey",
      "Violet",
      "Green",
      "Red",
    ]);
    for (const name of ["Grey", "Violet", "Green", "Red"]) {
      expect(screen.getByRole("option", { name })).toBeTruthy();
    }
    // The listbox is labelled by the fieldset's legend.
    expect(screen.getByRole("listbox", { name: "Color" })).toBeTruthy();
    expect(screen.getByTestId("editor-color-neutral")).toBe(options[0]);
  });

  it("value #7C5CFF selects Violet", () => {
    render(<Host initial="#7C5CFF" />);
    expect(selected("Violet")).toBe("true");
    expect(selected("Grey")).toBe("false");
    expect(screen.getByTestId("editor-color-name").textContent).toBe("· Violet");
    expect(screen.queryByTestId("editor-color-custom")).toBeNull();
  });

  it("value #00abcd adds a selected Custom — #00abcd slot", () => {
    render(<Host initial="#00abcd" />);
    expect(screen.getAllByRole("option")).toHaveLength(5);
    const slot = screen.getByRole("option", { name: "Custom — #00abcd" });
    expect(slot).toBe(screen.getByTestId("editor-color-custom"));
    expect(slot.getAttribute("aria-selected")).toBe("true");
    expect(screen.getByTestId("editor-color-name").textContent).toBe("· Custom — #00abcd");
  });

  it("arrow keys move between swatches and select", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Host initial="#888888" onChange={onChange} />);
    await user.tab();
    expect(document.activeElement).toBe(screen.getByRole("option", { name: "Grey" }));
    await user.keyboard("{ArrowRight}");
    expect(document.activeElement).toBe(screen.getByRole("option", { name: "Violet" }));
    await user.keyboard("{ArrowRight}");
    await user.keyboard(" ");
    expect(onChange).toHaveBeenLastCalledWith("#2ca02c");
    expect(selected("Green")).toBe("true");
    expect(screen.getByTestId("editor-color-name").textContent).toBe("· Green");
    // One tab stop for the whole row: the next Tab leaves it.
    await user.tab();
    expect(document.activeElement?.getAttribute("role")).not.toBe("option");
  });

  it("focusing a swatch shows its name tooltip", async () => {
    const user = userEvent.setup();
    render(<Host initial="#888888" />);
    expect(screen.queryByRole("tooltip")).toBeNull();
    await user.tab();
    expect(screen.getByRole("tooltip").textContent).toBe("Grey");
    await user.keyboard("{ArrowRight}");
    expect(screen.getByRole("tooltip").textContent).toBe("Violet");
    await user.tab();
    expect(screen.queryByRole("tooltip")).toBeNull();
  });

  it("hovering a swatch shows its name tooltip", async () => {
    const user = userEvent.setup();
    render(<Host initial="#888888" />);
    await user.hover(screen.getByRole("option", { name: "Red" }));
    expect(screen.getByRole("tooltip").textContent).toBe("Red");
    await user.unhover(screen.getByRole("option", { name: "Red" }));
    expect(screen.queryByRole("tooltip")).toBeNull();
  });

  it("switching to a preset keeps the custom slot available", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Host initial="#00abcd" onChange={onChange} />);
    await user.click(screen.getByRole("option", { name: "Green" }));
    expect(onChange).toHaveBeenLastCalledWith("#2ca02c");
    expect(selected("Green")).toBe("true");
    const slot = screen.getByRole("option", { name: "Custom — #00abcd" });
    expect(slot.getAttribute("aria-selected")).toBe("false");
    await user.click(slot);
    expect(onChange).toHaveBeenLastCalledWith("#00abcd");
    expect(screen.getByTestId("editor-color-name").textContent).toBe("· Custom — #00abcd");
  });

  it("a legacy colour shows a repair note whose Use Grey picks the default", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<EventColorField value="#888888" dash="dash" legacyColor="red" onChange={onChange} />);
    expect(screen.getByTestId("editor-color-legacy").textContent).toContain(
      "This event's colour “red” isn't a hex colour; it is drawn as Grey. Pick a colour to replace it.",
    );
    await user.click(screen.getByTestId("editor-color-legacy-repair"));
    expect(onChange).toHaveBeenCalledWith("#888888");
  });

  it("no repair note without a legacy colour", () => {
    render(<Host initial="#888888" />);
    expect(screen.queryByTestId("editor-color-legacy")).toBeNull();
  });
});
