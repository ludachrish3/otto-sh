import { describe, expect, it } from "vitest";
import { formatClockTime } from "../data/time";

describe("formatClockTime", () => {
  /** Epoch ms of a LOCAL wall-clock time, so the test holds in any zone. */
  const local = (h: number, m: number, s: number) => new Date(2026, 10, 6, h, m, s).getTime();

  it("is a 24-hour, zero-padded HH:MM:SS in local time", () => {
    expect(formatClockTime(local(17, 42, 36))).toBe("17:42:36");
    expect(formatClockTime(local(5, 4, 3))).toBe("05:04:03");
  });

  it("renders midnight as 00, never 24", () => {
    expect(formatClockTime(local(0, 0, 0))).toBe("00:00:00");
    expect(formatClockTime(local(0, 59, 59))).toBe("00:59:59");
  });
});
