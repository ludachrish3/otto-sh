// formatOutage's copy exists specifically because the down threshold is
// HEALTH_K x cadence (3s at a 1s interval) — sub-minute outages are
// reachable, and formatSpan alone would print "0m" for them.
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
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

// Issue #161: tests/e2e's banner spec computes expected text via a PYTHON
// mirror of formatOutage, and a mirror has no compiler tying it to this
// module. Both sides pin to one shared case table instead —
// tests/_fixtures/format_outage_cases.json holds the expected strings, this
// describe asserts the real formatOutage against them, and
// tests/unit/test_format_outage_mirror.py asserts the mirror against the
// SAME file. If either implementation drifts, exactly one suite fails,
// naming the divergent side.
describe("formatOutage fixture parity (shared with the Python mirror)", () => {
  const here = dirname(fileURLToPath(import.meta.url));
  const cases = JSON.parse(
    readFileSync(join(here, "../../../tests/_fixtures/format_outage_cases.json"), "utf-8"),
  ) as { ms: number; text: string }[];

  it.each(cases)("formatOutage($ms) === $text", ({ ms, text }) => {
    expect(formatOutage(ms)).toBe(text);
  });
});
