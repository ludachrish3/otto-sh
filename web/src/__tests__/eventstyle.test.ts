// The event-style registry's TS side (#857): the authored helpers over the
// generated mirror (api/eventStyle.gen.ts), and a guard that no event
// consumer restates a preset hex instead of reading the registry. The
// generated file's parity with the Python registry is pinned on the Python
// side (tests/unit/models/test_event_style.py::test_generated_ts_matches_registry).
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import {
  DEFAULT_EVENT_COLOR,
  DEFAULT_EVENT_DASH,
  EVENT_COLOR_PRESETS,
} from "../api/eventStyle.gen";
import {
  eventColorName,
  normalizeEventStyle,
  normalizeHexInput,
  presetFor,
} from "../data/eventStyle";
import { documentFromSessions, parseExportDocument } from "../data/exportDoc";

describe("presetFor", () => {
  it("presetFor matches case-insensitively", () => {
    expect(presetFor("#7C5CFF")?.name).toBe("Violet");
    expect(presetFor("#888888")?.id).toBe("neutral");
    expect(presetFor("#1f77b4")).toBeUndefined();
  });
});

describe("eventColorName", () => {
  it("eventColorName gives Grey or Custom — #00abcd", () => {
    expect(eventColorName("#888888")).toBe("Grey");
    expect(eventColorName("#D62728")).toBe("Red");
    expect(eventColorName("#00ABCD")).toBe("Custom — #00abcd");
  });
});

describe("normalizeHexInput", () => {
  it("normalizeHexInput accepts 00ABCD and #00abcd, rejects #00ab, #00abcdff and red", () => {
    expect(normalizeHexInput("00ABCD")).toBe("#00abcd");
    expect(normalizeHexInput("#00abcd")).toBe("#00abcd");
    for (const bad of ["#00ab", "#00abcdff", "red", "", "#00abcd\n", "##00abcd"]) {
      expect(normalizeHexInput(bad)).toBeNull();
    }
  });
});

describe("normalizeEventStyle", () => {
  it("legacy colour is preserved and drawn as the default", () => {
    expect(normalizeEventStyle({ color: "red" })).toEqual({
      color: DEFAULT_EVENT_COLOR,
      dash: DEFAULT_EVENT_DASH,
      legacyColor: "red",
    });
  });

  it("defaults a missing or null colour and dash, with no legacyColor", () => {
    expect(normalizeEventStyle({})).toEqual({ color: "#888888", dash: "dash" });
    expect(normalizeEventStyle({ color: null, dash: null })).toEqual({
      color: "#888888",
      dash: "dash",
    });
  });

  it("normalises hex to lowercase #rrggbb", () => {
    expect(normalizeEventStyle({ color: "#ABC" }).color).toBe("#aabbcc");
    expect(normalizeEventStyle({ color: "#7C5CFF" }).color).toBe("#7c5cff");
  });

  it("rejects a hex with a trailing newline as legacy", () => {
    expect(normalizeEventStyle({ color: "#aabbcc\n" })).toMatchObject({
      color: "#888888",
      legacyColor: "#aabbcc\n",
    });
  });

  it("an unknown dash is kept as legacyDash and drawn as the default; a known one is kept", () => {
    expect(normalizeEventStyle({ dash: "wavy" })).toEqual({
      color: DEFAULT_EVENT_COLOR,
      dash: DEFAULT_EVENT_DASH,
      legacyDash: "wavy",
    });
    expect(normalizeEventStyle({ dash: "dot" })).toEqual({
      color: DEFAULT_EVENT_COLOR,
      dash: "dot",
    });
  });

  it("is idempotent: a normalised legacy record keeps its legacyColor and legacyDash", () => {
    const once = normalizeEventStyle({ color: "red", dash: "wavy" });
    expect(normalizeEventStyle(once)).toEqual(once);
  });

  it("keeps every other field", () => {
    expect(normalizeEventStyle({ id: 3, label: "x", color: "#888888" })).toEqual({
      id: 3,
      label: "x",
      color: "#888888",
      dash: "dash",
    });
  });
});

describe("export round-trips a legacy style", () => {
  function roundTrip(style: { color?: string; dash?: string }) {
    const { sessions } = parseExportDocument(
      JSON.stringify({
        format: 1,
        sessions: [
          {
            id: "old",
            start: "2026-01-01T00:00:00Z",
            events: [{ id: 1, timestamp: "2026-01-01T00:00:01Z", label: "x", ...style }],
          },
        ],
      }),
    );
    const [drawn] = sessions[0]?.events ?? [];
    const [exported] = documentFromSessions(sessions).sessions[0]?.events ?? [];
    return { drawn, exported };
  }

  it("writes legacyColor back as the colour, never the drawn default", () => {
    const { exported } = roundTrip({ color: "red" });
    expect(exported?.color).toBe("red");
    expect(exported).not.toHaveProperty("legacyColor");
  });

  it("writes legacyDash back as the dash, never the drawn default", () => {
    const { drawn, exported } = roundTrip({ color: "#2ca02c", dash: "wavy" });
    expect(drawn?.dash).toBe(DEFAULT_EVENT_DASH);
    expect(exported).toMatchObject({ color: "#2ca02c", dash: "wavy" });
    expect(exported).not.toHaveProperty("legacyDash");
  });
});

describe("event consumers import the registry", () => {
  const SRC = join(dirname(fileURLToPath(import.meta.url)), "..");
  const CONSUMERS = [
    "charts/options.ts",
    "data/exportDoc.ts",
    "data/fragment.ts",
    "shell/EventsPanel.tsx",
    "shell/EventEditor.tsx",
    "shell/marking.ts",
    "pages/SubjectPage.tsx",
  ];
  const PRESET_HEXES = EVENT_COLOR_PRESETS.map((p) => p.hex);

  it.each(CONSUMERS)("%s restates no preset hex", (file) => {
    const offending = readFileSync(join(SRC, file), "utf-8")
      .split("\n")
      // The brush in options.ts is a brand colour, not an event style.
      .filter((line) => !line.includes("brushStyle"))
      .filter((line) => PRESET_HEXES.some((hex) => line.toLowerCase().includes(hex)));
    expect(offending).toEqual([]);
  });
});
