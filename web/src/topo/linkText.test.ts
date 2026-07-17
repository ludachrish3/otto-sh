import { describe, expect, it } from "vitest";

import { tunnelPathText } from "./linkText";

describe("tunnelPathText", () => {
  it("joins hops in order", () => {
    expect(tunnelPathText(["gw", "mid", "db"])).toBe("gw → mid → db");
  });
});
