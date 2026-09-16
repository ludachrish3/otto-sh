import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SearchTrigger } from "./SearchTrigger";

afterEach(() => {
  cleanup();
});

describe("SearchTrigger", () => {
  it("renders the input-lookalike with placeholder text and the Cmd/Ctrl K keycap", () => {
    // The trigger advertises the palette binding (jsdom is non-mac -> Ctrl K),
    // NOT the "/" that belongs to in-page search boxes.
    render(<SearchTrigger onOpen={() => {}} />);
    const trigger = screen.getByTestId("search-trigger");
    expect(trigger.textContent).toContain("Search…");
    expect(trigger.textContent).toContain("Ctrl K");
    expect(trigger.textContent).not.toContain("/");
  });

  it("calls onOpen on click — the trigger owns no store of its own", async () => {
    const user = userEvent.setup();
    const onOpen = vi.fn();
    render(<SearchTrigger onOpen={onOpen} />);
    await user.click(screen.getByTestId("search-trigger"));
    expect(onOpen).toHaveBeenCalledTimes(1);
  });
});
