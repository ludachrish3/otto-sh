// formatOutage's copy exists specifically because the down threshold is
// HEALTH_K x cadence (3s at a 1s interval) — sub-minute outages are
// reachable, and formatSpan alone would print "0m" for them.
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { synthSession } from "../__tests__/_synth";
import { observedAtMicros } from "./exportDoc";
import { displayEndMs, formatClockTime, formatOutage, parseTs } from "./time";

// #866: the live window's horizon is the later of the last metric and the
// server's last observation. endMs stays metric-only (health reads it).
describe("displayEndMs", () => {
  const session = () => synthSession({ hosts: 1, seriesPerHost: 1, ticks: 2, intervalS: 5 });

  it("follows the server's observation past a stalled last metric", () => {
    const s = session();
    expect(displayEndMs({ ...s, observedMs: s.endMs + 600_000 })).toBe(s.endMs + 600_000);
  });

  it("never falls behind the last metric", () => {
    const s = session();
    expect(displayEndMs({ ...s, observedMs: s.endMs - 600_000 })).toBe(s.endMs);
  });
});

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

// otto's convention: a timestamp with no offset means UTC, on the server
// (otto.utils.as_utc) and here alike, so a legacy capture stored naive reads
// the same instant whatever the browser's zone. parseTs is the ONE place the
// browser decides it; the zone is stubbed off UTC so a local-time read fails.
describe("parseTs: a naive timestamp means UTC", () => {
  beforeEach(() => {
    vi.stubEnv("TZ", "America/Chicago");
  });

  it("runs under a zone that is not UTC", () => {
    expect(new Date(2026, 6, 18, 12).getTime()).not.toBe(Date.UTC(2026, 6, 18, 12));
  });

  it("reads an offset-less stamp as UTC", () => {
    expect(parseTs("2026-07-18T12:00:00")).toBe(Date.UTC(2026, 6, 18, 12));
    expect(parseTs("2026-07-18T12:00:00.250")).toBe(Date.UTC(2026, 6, 18, 12, 0, 0, 250));
    expect(parseTs("2026-07-18T12:00")).toBe(Date.UTC(2026, 6, 18, 12));
  });

  it("keeps an existing offset or Z", () => {
    expect(parseTs("2026-07-18T12:00:00Z")).toBe(Date.UTC(2026, 6, 18, 12));
    expect(parseTs("2026-07-18T12:00:00+02:00")).toBe(Date.UTC(2026, 6, 18, 10));
    expect(parseTs("2026-07-18T12:00:00.123456-05:00")).toBe(Date.UTC(2026, 6, 18, 17, 0, 0, 123));
  });

  it("is NaN for a malformed stamp, as before", () => {
    expect(Number.isNaN(parseTs("not a time"))).toBe(true);
  });

  it("is what observedAtMicros and formatClockTime read a stamp with", () => {
    expect(observedAtMicros("2026-07-18T12:00:00.000007")).toBe(
      Date.UTC(2026, 6, 18, 12) * 1000 + 7,
    );
    expect(formatClockTime("2026-07-18T12:00:00")).toBe(formatClockTime(Date.UTC(2026, 6, 18, 12)));
  });
});
