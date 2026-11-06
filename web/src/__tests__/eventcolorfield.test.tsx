// web/src/__tests__/eventcolorfield.test.tsx
// EventColorField (#858): the event editor's named colour swatches. The
// assertion surface is the accessibility tree -- option roles and names,
// aria-selected, the tooltip role -- plus the persistent name's testid.
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { MARKER_LINE_WIDTH } from "../charts/markerStyle";
import { EventColorField } from "../ui/EventColorField";
import { installPlotSurfaceCss } from "./_plotSurfaceCss";

// jsdom lacks CSS.escape; react-aria's listbox focus utilities call it
// unconditionally (same polyfill as eventeditor.test.tsx).
if (typeof globalThis.CSS === "undefined") {
  Object.defineProperty(globalThis, "CSS", {
    value: { escape: (value: string) => value.replace(/[^a-zA-Z0-9_-]/gu, (ch) => `\\${ch}`) },
    writable: true,
  });
}

beforeAll(() => {
  installPlotSurfaceCss();
});

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

describe("Custom… colour popover (#859)", () => {
  type User = ReturnType<typeof userEvent.setup>;
  const HEX_ERROR = "Enter six hexadecimal digits, for example #00abcd";

  async function openCustom(user: User): Promise<HTMLElement> {
    await user.click(screen.getByTestId("editor-color-custom-open"));
    return screen.getByTestId("custom-color-dialog");
  }

  function hexInput(): HTMLInputElement {
    return screen.getByTestId("custom-color-hex") as HTMLInputElement;
  }

  function apply(): HTMLButtonElement {
    return screen.getByTestId("custom-color-apply") as HTMLButtonElement;
  }

  /** The wheel's hue input, or the area's saturation input (RAC names both
   * area inputs "Color picker"; the horizontal one is x, saturation). */
  function slider(dialog: HTMLElement, channel: "hue" | "saturation"): HTMLInputElement {
    if (channel === "hue") {
      return within(dialog).getByRole("slider", { name: "Hue" }) as HTMLInputElement;
    }
    const area = within(dialog).getAllByRole("slider", { name: "Color picker" });
    const x = area.find((el) => el.getAttribute("aria-orientation") === "horizontal");
    if (!x) throw new Error("no saturation input");
    return x as HTMLInputElement;
  }

  async function typeHex(user: User, text: string): Promise<void> {
    await user.clear(hexInput());
    await user.type(hexInput(), text);
  }

  it("Custom… is the row's last item and a second tab stop", async () => {
    const user = userEvent.setup();
    render(<Host initial="#888888" />);
    const open = screen.getByTestId("editor-color-custom-open");
    expect(open.textContent).toBe("Custom…");
    const listbox = screen.getByRole("listbox", { name: "Color" });
    expect(listbox.parentElement).toBe(open.parentElement);
    expect(open.parentElement?.lastElementChild).toBe(open);
    await user.tab();
    expect(document.activeElement?.getAttribute("role")).toBe("option");
    await user.tab();
    expect(document.activeElement).toBe(open);
  });

  it("opens with the hex field focused and holding the current colour", async () => {
    const user = userEvent.setup();
    render(<Host initial="#00abcd" />);
    const dialog = await openCustom(user);
    expect(dialog.getAttribute("aria-label")).toBe("Custom event colour");
    expect(document.activeElement).toBe(hexInput());
    expect(hexInput().value.toLowerCase()).toBe("#00abcd");
    expect(within(dialog).getByText("Light chart")).toBeTruthy();
    expect(within(dialog).getByText("Dark chart")).toBeTruthy();
  });

  it("Use colour sends the typed hex lowercased", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Host initial="#888888" onChange={onChange} />);
    await openCustom(user);
    await typeHex(user, "00ABCD");
    // Valid typing keeps what was typed; it is not reformatted mid-entry.
    expect(hexInput().value).toBe("00ABCD");
    expect(apply().disabled).toBe(false);
    await user.click(apply());
    expect(onChange).toHaveBeenCalledExactlyOnceWith("#00abcd");
    expect(screen.queryByTestId("custom-color-dialog")).toBeNull();
    expect(screen.getByTestId("editor-color-name").textContent).toBe("· Custom — #00abcd");
  });

  it("incomplete hex disables Use colour and shows the message", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Host initial="#888888" onChange={onChange} />);
    await openCustom(user);
    await typeHex(user, "#00ab");
    expect(apply().disabled).toBe(true);
    const message = screen.getByText(HEX_ERROR);
    expect(hexInput().getAttribute("aria-describedby")?.split(" ")).toContain(message.id);
    expect(onChange).not.toHaveBeenCalled();
  });

  it("an invalid attempt survives blur and Enter", async () => {
    const user = userEvent.setup();
    render(<Host initial="#888888" />);
    await openCustom(user);
    await typeHex(user, "#00ab");
    await user.keyboard("{Enter}");
    // RAC's commit would revert the unparsable text; the field keeps what
    // the user typed beside the refusal instead.
    expect(screen.getByTestId("custom-color-dialog")).toBeTruthy();
    expect(hexInput().value).toBe("#00ab");
    expect(apply().disabled).toBe(true);
    expect(screen.getByText(HEX_ERROR)).toBeTruthy();
    await typeHex(user, "#00ab");
    await user.tab();
    expect(hexInput().value).toBe("#00ab");
    expect(apply().disabled).toBe(true);
    expect(screen.getByText(HEX_ERROR)).toBeTruthy();
  });

  it("valid typing clears the refusal", async () => {
    const user = userEvent.setup();
    render(<Host initial="#888888" />);
    await openCustom(user);
    await typeHex(user, "#00ab");
    expect(apply().disabled).toBe(true);
    await user.type(hexInput(), "cd");
    expect(apply().disabled).toBe(false);
    expect(screen.queryByText(HEX_ERROR)).toBeNull();
  });

  it("Cancel discards", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Host initial="#888888" onChange={onChange} />);
    await openCustom(user);
    await typeHex(user, "00abcd");
    await user.click(screen.getByTestId("custom-color-cancel"));
    expect(screen.queryByTestId("custom-color-dialog")).toBeNull();
    expect(onChange).not.toHaveBeenCalled();
    await openCustom(user);
    expect(hexInput().value.toLowerCase()).toBe("#888888");
  });

  it("Cancel clears a refusal: reopening starts clean", async () => {
    const user = userEvent.setup();
    render(<Host initial="#888888" />);
    await openCustom(user);
    await typeHex(user, "#00ab");
    await user.click(screen.getByTestId("custom-color-cancel"));
    await openCustom(user);
    expect(apply().disabled).toBe(false);
    expect(screen.queryByText(HEX_ERROR)).toBeNull();
  });

  it("hue survives passing through grey", async () => {
    const user = userEvent.setup();
    render(<Host initial="#00abcd" />);
    const dialog = await openCustom(user);
    const hueBefore = Number(slider(dialog, "hue").value);
    const satBefore = slider(dialog, "saturation").value;
    expect(hueBefore).toBeGreaterThan(0);
    expect(Number(satBefore)).toBeGreaterThan(0);
    fireEvent.change(slider(dialog, "saturation"), { target: { value: "0" } });
    expect(Number(slider(dialog, "saturation").value)).toBe(0);
    expect(Number(slider(dialog, "hue").value)).toBe(hueBefore);
    fireEvent.change(slider(dialog, "saturation"), { target: { value: satBefore } });
    expect(slider(dialog, "saturation").value).toBe(satBefore);
    expect(Number(slider(dialog, "hue").value)).toBe(hueBefore);
  });

  it("wheel-only change then Apply sends the wheel colour", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Host initial="#ff0000" onChange={onChange} />);
    const dialog = await openCustom(user);
    fireEvent.change(slider(dialog, "hue"), { target: { value: "120" } });
    expect(hexInput().value.toLowerCase()).toBe("#00ff00");
    await user.click(apply());
    expect(onChange).toHaveBeenCalledExactlyOnceWith("#00ff00");
  });

  it("typed hex, then a wheel change, then Apply sends the wheel colour", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Host initial="#888888" onChange={onChange} />);
    const dialog = await openCustom(user);
    await typeHex(user, "ff0000");
    fireEvent.change(slider(dialog, "hue"), { target: { value: "240" } });
    await user.click(apply());
    expect(onChange).toHaveBeenCalledExactlyOnceWith("#0000ff");
  });

  it("a wheel change after an invalid attempt clears it", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Host initial="#ff0000" onChange={onChange} />);
    const dialog = await openCustom(user);
    await typeHex(user, "#00ab");
    expect(apply().disabled).toBe(true);
    fireEvent.change(slider(dialog, "hue"), { target: { value: "120" } });
    expect(apply().disabled).toBe(false);
    expect(screen.queryByText(HEX_ERROR)).toBeNull();
    // The wheel's colour replaces the refused text.
    expect(hexInput().value.toLowerCase()).toBe("#00ff00");
    await user.click(apply());
    expect(onChange).toHaveBeenCalledExactlyOnceWith("#00ff00");
  });

  it("applying a preset hex selects the preset", async () => {
    const user = userEvent.setup();
    render(<Host initial="#888888" />);
    await openCustom(user);
    await typeHex(user, "#7C5CFF");
    await user.click(apply());
    expect(selected("Violet")).toBe("true");
    expect(screen.queryByTestId("editor-color-custom")).toBeNull();
    expect(screen.getByTestId("editor-color-name").textContent).toBe("· Violet");
  });

  it("both previews draw the marker in the draft colour and dash", async () => {
    const user = userEvent.setup();
    render(<EventColorField value="#00abcd" dash="dot" onChange={() => {}} />);
    await openCustom(user);
    for (const id of ["custom-color-preview-light", "custom-color-preview-dark"]) {
      const svg = screen.getByTestId(id);
      const line = svg.querySelector("[data-marker='point']");
      expect(line?.getAttribute("stroke")).toBe("#00abcd");
      expect(line?.getAttribute("stroke-dasharray")).toBe("1");
      const span = svg.querySelector("[data-marker='span']");
      expect(span?.getAttribute("fill")).toBe("#00abcd1f");
      expect(span?.getAttribute("stroke-dasharray")).toBe("1");
      for (const el of [line, span]) {
        expect(el?.getAttribute("stroke-width")).toBe(String(MARKER_LINE_WIDTH));
      }
    }
  });
});
