import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";

import { SearchTrigger } from "./SearchTrigger";
import { useUiStore } from "./uiStore";

afterEach(() => {
  cleanup();
  useUiStore.setState({ paletteOpen: false, theme: "light" });
});

describe("SearchTrigger", () => {
  it("renders the input-lookalike with placeholder text and the / keycap", () => {
    render(<SearchTrigger />);
    const trigger = screen.getByTestId("search-trigger");
    expect(trigger.textContent).toContain("Search…");
    expect(trigger.textContent).toContain("/");
  });

  it("opens the palette on click", async () => {
    const user = userEvent.setup();
    render(<SearchTrigger />);
    await user.click(screen.getByTestId("search-trigger"));
    expect(useUiStore.getState().paletteOpen).toBe(true);
  });
});
