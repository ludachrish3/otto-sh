import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

if (typeof CSS === "undefined" || !CSS.escape) {
  // Same polyfill as reviewbar.test.tsx/shell.test.tsx — react-aria portals
  // call CSS.escape.
  (globalThis as { CSS?: unknown }).CSS = {
    escape: (value: string) => value.replace(/[^a-zA-Z0-9_-]/g, (ch) => `\\${ch}`),
  };
}

import { useReviewStore } from "../data/reviewStore";
import { EventsPanel } from "../shell/EventsPanel";

const HERE = dirname(fileURLToPath(import.meta.url));
const KITCHEN = readFileSync(join(HERE, "../../fixtures/kitchen-sink.json"), "utf-8");
const MIN = 60_000;

afterEach(() => {
  cleanup();
  useReviewStore.setState({
    sessions: [],
    rawMonitorSessions: null,
    sourceName: null,
    warnings: [],
    importError: null,
    activeSessionId: null,
    range: null,
  });
});

function load() {
  useReviewStore.getState().actions.importMonitorSessions(KITCHEN, "kitchen-sink.json");
  const onClose = vi.fn();
  render(<EventsPanel isOpen onClose={onClose} />);
  return { onClose, session: useReviewStore.getState().sessions[0] };
}

describe("EventsPanel", () => {
  it("lists events newest-first with span durations", async () => {
    load();
    const panel = await screen.findByTestId("events-panel");
    const rows = panel.querySelectorAll("[data-testid^=event-row-]");
    expect(rows).toHaveLength(4);
    // Newest first: the log-capture span (start 90m) precedes stress (85m),
    // w2 lost (60m), config reload (20m).
    expect(rows[0].textContent).toContain("log capture");
    expect(rows[0].textContent).toContain("10m"); // 90->100m span
    expect(rows[3].textContent).toContain("config reload");
  });

  it("clicking a row jumps the range around the event and closes", async () => {
    const { onClose, session } = load();
    const row = await screen.findByTestId("event-row-2"); // stress run 85–95m
    fireEvent.click(row);
    const range = useReviewStore.getState().range;
    expect(range).not.toBeNull();
    expect(range?.from).toBe(session.startMs + 70 * MIN); // 85m − 15m
    expect(range?.to).toBe(session.startMs + 110 * MIN); // 95m + 15m
    expect(onClose).toHaveBeenCalled();
  });

  it("shows the empty state without events", async () => {
    useReviewStore
      .getState()
      .actions.importMonitorSessions(
        readFileSync(join(HERE, "../../fixtures/minimal.json"), "utf-8"),
        "minimal.json",
      );
    render(<EventsPanel isOpen onClose={() => {}} />);
    const panel = await screen.findByTestId("events-panel");
    expect(panel.textContent).toContain("No events in this session");
  });

  it("renders events without ids with distinct testids", async () => {
    // Synthetic document with two id-less events
    const synthDoc = JSON.stringify({
      format: 1,
      sessions: [
        {
          id: "synth",
          label: "test",
          start: "2026-07-01T08:00:00Z",
          end: "2026-07-01T09:00:00Z",
          lab: { elements: [], hosts: [{ id: "h1", element: "h1" }], links: [] },
          meta: { interval: 60.0, charts: [], tabs: [] },
          metrics: [],
          events: [{ timestamp: "2026-07-01T08:10:00Z" }, { timestamp: "2026-07-01T08:20:00Z" }],
          log_events: [],
          chart_map: {},
        },
      ],
    });
    useReviewStore.getState().actions.importMonitorSessions(synthDoc, "test.json");
    const onClose = vi.fn();
    render(<EventsPanel isOpen onClose={onClose} />);
    await screen.findByTestId("events-panel");
    // Both rows should exist with distinct negative ids (matching eventMarkers logic)
    expect(screen.getByTestId("event-row--1")).toBeTruthy();
    expect(screen.getByTestId("event-row--2")).toBeTruthy();
  });
});
