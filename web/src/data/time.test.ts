// formatOutage's copy exists specifically because the down threshold is
// HEALTH_K x cadence (3s at a 1s interval) — sub-minute outages are
// reachable, and formatSpan alone would print "0m" for them.
import { describe, expect, it } from "vitest";
import { formatOutage } from "./time";

describe("formatOutage", () => {
  it("shows seconds under a minute", () => {
    expect(formatOutage(45_000)).toBe("45s");
  });

  it("shows minutes at and above a minute", () => {
    expect(formatOutage(120_000)).toBe("2m");
  });

  it("shows fractional hours the same way formatSpan does", () => {
    expect(formatOutage(5_400_000)).toBe("1.5h");
  });

  it("boundary: exactly 60s rounds to 1m, not 60s", () => {
    expect(formatOutage(60_000)).toBe("1m");
  });
});
