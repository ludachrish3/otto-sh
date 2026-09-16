import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { coverageSegmentsOf, hashPathOf, hashQueryOf, useHash } from "./hashState";

afterEach(() => {
  window.location.hash = "";
});

describe("hash helpers", () => {
  it("split path and query, with and without a query", () => {
    expect(hashPathOf("#/coverage/a/b.c?lines=4&q=x")).toBe("/coverage/a/b.c");
    expect(hashPathOf("#/runs")).toBe("/runs");
    expect(hashQueryOf("#/coverage/a/b.c?lines=4&q=x").get("q")).toBe("x");
    expect([...hashQueryOf("#/runs").keys()]).toEqual([]);
  });

  it("coverageSegmentsOf decodes segments and is null off the coverage route", () => {
    expect(coverageSegmentsOf("/coverage/product/main%20x.c")).toEqual(["product", "main x.c"]);
    expect(coverageSegmentsOf("/coverage")).toEqual([]);
    expect(coverageSegmentsOf("/runs")).toBeNull();
    expect(coverageSegmentsOf("/coverage/%E0%A4%A")).toBeNull();
  });
});

describe("useHash", () => {
  it("returns the current hash and re-renders on hashchange", () => {
    window.location.hash = "#/coverage?lines=1";
    const { result } = renderHook(() => useHash());
    expect(result.current).toBe("#/coverage?lines=1");
    act(() => {
      window.location.hash = "#/coverage?lines=2";
      window.dispatchEvent(new HashChangeEvent("hashchange"));
    });
    expect(result.current).toBe("#/coverage?lines=2");
  });
});
