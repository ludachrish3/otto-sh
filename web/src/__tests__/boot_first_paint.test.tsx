// What the page shows before the boot has concluded anything: the first
// commit, which React paints before App's effect starts the boot. Import is
// withheld there, because a file handed over then is dropped once it is read
// (data/source.ts's importLocalFile re-reads importOffered), and a page that
// showed the button would invite exactly that.

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import { useReviewStore } from "../data/reviewStore";
import { importOffered } from "../data/source";

// A boot that never starts stands for the time before App's effect runs: the
// store is then exactly as the first commit reads it.
vi.mock("../data/bootstrap", () => ({ bootstrapFromServer: vi.fn() }));

afterEach(cleanup);

describe("before the boot concludes", () => {
  it("the store's first state withholds Import", () => {
    expect(importOffered(useReviewStore.getInitialState())).toBe(false);
  });

  it("the first commit shows no Import button", () => {
    useReviewStore.setState(useReviewStore.getInitialState(), true);
    render(<App />);
    expect(screen.getByTestId("empty-review-text").textContent).toBe(
      "Waiting for the server's data…",
    );
    expect(screen.queryByTestId("empty-import-btn")).toBeNull();
  });
});
