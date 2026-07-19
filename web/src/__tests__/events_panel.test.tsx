import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
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
import { useUiStore } from "../ui/uiStore";

const HERE = dirname(fileURLToPath(import.meta.url));
const KITCHEN = readFileSync(join(HERE, "../../fixtures/kitchen-sink.json"), "utf-8");
const MIN = 60_000;

function okJson(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status });
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  useReviewStore.setState({
    sessions: [],
    rawMonitorSessions: null,
    sourceName: null,
    warnings: [],
    importError: null,
    activeSessionId: null,
    range: null,
    mode: null,
    editable: false,
  });
  useUiStore.setState({ openSpan: null, eventEditor: null });
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

  it("a refused jump (padded range wholly outside session bounds) keeps the panel open and shows a notice", async () => {
    const { onClose, session } = load();
    // kitchen-sink's session ends at 10:00; this event sits an hour past
    // that, so its ±15m pad ([10:45, 11:15]) clamps to a DEGENERATE range
    // ([10:45, 10:00], from >= to) — the exact case setRange's inverted-
    // range guard refuses silently (reviewStore.ts).
    act(() => {
      useReviewStore.getState().actions.appendFragment({
        format: 1,
        session: session.id,
        events: [
          {
            id: 99,
            timestamp: "2026-07-01T11:00:00Z",
            label: "post-session",
            source: "manual",
          },
        ],
      });
    });
    const row = await screen.findByTestId("event-row-99");
    const rangeBefore = useReviewStore.getState().range;
    fireEvent.click(row);
    expect(useReviewStore.getState().range).toBe(rangeBefore); // unchanged
    expect(onClose).not.toHaveBeenCalled(); // the panel must not close on a refusal
    expect(await screen.findByTestId("events-panel")).toBeTruthy();
    expect(screen.getByTestId("jump-notice").textContent).toBe("Outside the session's time range");
  });

  it("a subsequent successful jump clears a lingering refused-jump notice", async () => {
    const { onClose, session } = load();
    act(() => {
      useReviewStore.getState().actions.appendFragment({
        format: 1,
        session: session.id,
        events: [
          { id: 99, timestamp: "2026-07-01T11:00:00Z", label: "post-session", source: "manual" },
        ],
      });
    });
    fireEvent.click(await screen.findByTestId("event-row-99"));
    expect(screen.getByTestId("jump-notice")).toBeTruthy();
    fireEvent.click(screen.getByTestId("event-row-2")); // stress run -- within bounds
    expect(screen.queryByTestId("jump-notice")).toBeNull();
    expect(useReviewStore.getState().range).not.toBeNull();
    expect(onClose).toHaveBeenCalled();
  });

  it("reopening the panel clears a lingering refused-jump notice", async () => {
    useReviewStore.getState().actions.importMonitorSessions(KITCHEN, "kitchen-sink.json");
    const session = useReviewStore.getState().sessions[0];
    useReviewStore.getState().actions.appendFragment({
      format: 1,
      session: session.id,
      events: [
        { id: 99, timestamp: "2026-07-01T11:00:00Z", label: "post-session", source: "manual" },
      ],
    });
    const onClose = vi.fn();
    const { rerender } = render(<EventsPanel isOpen onClose={onClose} />);
    fireEvent.click(await screen.findByTestId("event-row-99"));
    expect(screen.getByTestId("jump-notice")).toBeTruthy();
    rerender(<EventsPanel isOpen={false} onClose={onClose} />);
    rerender(<EventsPanel isOpen onClose={onClose} />);
    expect(screen.queryByTestId("jump-notice")).toBeNull();
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

describe("EventsPanel compose row", () => {
  it("renders no compose row when the session is not editable, in any mode", async () => {
    useReviewStore.setState({ mode: "live", editable: false });
    load();
    await screen.findByTestId("events-panel");
    expect(screen.queryByTestId("events-compose")).toBeNull();
    act(() => {
      useReviewStore.setState({ mode: "review" }); // still not editable
    });
    expect(screen.queryByTestId("events-compose")).toBeNull();
  });

  it("live + editable: renders the label input and Mark/Start/Stop, not Add event…", async () => {
    useReviewStore.setState({ mode: "live", editable: true });
    load();
    expect(await screen.findByTestId("events-compose")).toBeTruthy();
    expect(screen.getByTestId("events-compose-label")).toBeTruthy();
    expect(screen.getByTestId("events-compose-mark")).toBeTruthy();
    expect(screen.getByTestId("events-compose-start")).toBeTruthy();
    expect(screen.getByTestId("events-compose-stop")).toBeTruthy();
    expect(screen.queryByTestId("events-compose-add")).toBeNull();
  });

  it("review + editable: renders only the Add event… button", async () => {
    useReviewStore.setState({ mode: "review", editable: true });
    load();
    expect(await screen.findByTestId("events-compose")).toBeTruthy();
    expect(screen.getByTestId("events-compose-add")).toBeTruthy();
    expect(screen.queryByTestId("events-compose-label")).toBeNull();
    expect(screen.queryByTestId("events-compose-mark")).toBeNull();
    expect(screen.queryByTestId("events-compose-start")).toBeNull();
    expect(screen.queryByTestId("events-compose-stop")).toBeNull();
  });

  it("Stop is disabled until a span is open for this session, then enabled", async () => {
    useReviewStore.setState({ mode: "live", editable: true });
    const { session } = load();
    expect(screen.getByTestId("events-compose-stop").hasAttribute("disabled")).toBe(true);
    act(() => {
      useUiStore.getState().actions.setOpenSpan({ sessionId: session.id, eventId: 77 });
    });
    await waitFor(() =>
      expect(screen.getByTestId("events-compose-stop").hasAttribute("disabled")).toBe(false),
    );
    // A span open on a DIFFERENT session must not enable this one's Stop.
    act(() => {
      useUiStore.getState().actions.setOpenSpan({ sessionId: "some-other-session", eventId: 77 });
    });
    await waitFor(() =>
      expect(screen.getByTestId("events-compose-stop").hasAttribute("disabled")).toBe(true),
    );
  });

  it("Mark posts a point event with the typed label and the new row appears; label clears", async () => {
    const user = userEvent.setup();
    useReviewStore.setState({ mode: "live", editable: true });
    const { session } = load();
    const fetchMock = vi.fn().mockResolvedValue(
      okJson(
        {
          id: 50,
          timestamp: "2026-07-01T09:50:00Z",
          label: "checkpoint",
          source: "manual",
          color: "#888888",
          dash: "dash",
        },
        201,
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    await user.type(screen.getByTestId("events-compose-label"), "checkpoint");
    await user.click(screen.getByTestId("events-compose-mark"));
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        `/api/session/${encodeURIComponent(session.id)}/event`,
        expect.objectContaining({ method: "POST" }),
      ),
    );
    const body = JSON.parse((fetchMock.mock.calls[0][1] as RequestInit).body as string);
    expect(body).toEqual({ label: "checkpoint" });
    expect(await screen.findByTestId("event-row-50")).toBeTruthy();
    expect((screen.getByTestId("events-compose-label") as HTMLInputElement).value).toBe("");
  });

  it("Start posts a span-start event and records openSpan; Stop then ends it", async () => {
    const user = userEvent.setup();
    useReviewStore.setState({ mode: "live", editable: true });
    const { session } = load();
    const fetchMock = vi.fn().mockResolvedValueOnce(
      okJson(
        {
          id: 60,
          timestamp: "2026-07-01T09:50:00Z",
          label: "soak",
          source: "manual",
          color: "#888888",
          dash: "dash",
        },
        201,
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    await user.type(screen.getByTestId("events-compose-label"), "soak");
    await user.click(screen.getByTestId("events-compose-start"));
    await waitFor(() =>
      expect(useUiStore.getState().openSpan).toEqual({ sessionId: session.id, eventId: 60 }),
    );
    fetchMock.mockResolvedValueOnce(
      okJson({
        id: 60,
        timestamp: "2026-07-01T09:50:00Z",
        end_timestamp: "2026-07-01T09:55:00Z",
        label: "soak",
        source: "manual",
        color: "#888888",
        dash: "dash",
      }),
    );
    await user.click(screen.getByTestId("events-compose-stop"));
    await waitFor(() =>
      expect(fetchMock).toHaveBeenLastCalledWith(
        `/api/session/${encodeURIComponent(session.id)}/event/60/end`,
        expect.objectContaining({ method: "POST" }),
      ),
    );
    await waitFor(() => expect(useUiStore.getState().openSpan).toBeNull());
  });

  it("a rejected Mark shows events-compose-error, keeps the popover's label, and does not add a row", async () => {
    const user = userEvent.setup();
    useReviewStore.setState({ mode: "live", editable: true });
    load();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(okJson({ error: "archive is locked" }, 409)));
    await user.type(screen.getByTestId("events-compose-label"), "checkpoint");
    await user.click(screen.getByTestId("events-compose-mark"));
    const errorEl = await screen.findByTestId("events-compose-error");
    expect(errorEl.textContent).toContain("archive is locked");
    expect((screen.getByTestId("events-compose-label") as HTMLInputElement).value).toBe(
      "checkpoint",
    );
  });

  it("Add event… opens a blank draft editor anchored to the session end", async () => {
    const user = userEvent.setup();
    useReviewStore.setState({ mode: "review", editable: true });
    const { session } = load();
    await user.click(screen.getByTestId("events-compose-add"));
    const target = useUiStore.getState().eventEditor;
    expect(target?.kind).toBe("draft");
    if (target?.kind === "draft") {
      expect(target.draft.sessionId).toBe(session.id);
      expect(target.draft.timestampMs).toBe(session.endMs);
      expect(target.draft.label).toBe("");
    }
  });
});

describe("EventsPanel row affordances", () => {
  it("an edit button opens the event editor targeted at that event's id", async () => {
    const user = userEvent.setup();
    useReviewStore.setState({ mode: "live", editable: true });
    const { session } = load();
    await user.click(screen.getByTestId("event-edit-2")); // stress run
    expect(useUiStore.getState().eventEditor).toEqual({
      kind: "edit",
      sessionId: session.id,
      eventId: 2,
    });
  });

  it("End now appears only on live, id'd, endless (no end_timestamp) rows", async () => {
    useReviewStore.setState({ mode: "live", editable: true });
    load();
    await screen.findByTestId("events-panel");
    expect(screen.getByTestId("event-endnow-1")).toBeTruthy(); // config reload: no end
    expect(screen.getByTestId("event-endnow-4")).toBeTruthy(); // w2 lost: no end
    expect(screen.queryByTestId("event-endnow-2")).toBeNull(); // stress run: has an end
    expect(screen.queryByTestId("event-endnow-3")).toBeNull(); // log capture: has an end
  });

  it("End now never appears outside live mode, even for endless rows", async () => {
    useReviewStore.setState({ mode: "review", editable: true });
    load();
    await screen.findByTestId("events-panel");
    expect(screen.queryByTestId("event-endnow-1")).toBeNull();
    expect(screen.queryByTestId("event-endnow-4")).toBeNull();
  });

  it("clicking End now ends that event via the session/event/id/end route", async () => {
    const user = userEvent.setup();
    useReviewStore.setState({ mode: "live", editable: true });
    const { session } = load();
    const fetchMock = vi.fn().mockResolvedValue(
      okJson({
        id: 1,
        timestamp: "2026-07-01T08:20:00Z",
        end_timestamp: "2026-07-01T09:59:00Z",
        label: "config reload",
        source: "manual",
        color: "#7c5cff",
        dash: "dash",
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    await user.click(screen.getByTestId("event-endnow-1"));
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        `/api/session/${encodeURIComponent(session.id)}/event/1/end`,
        expect.objectContaining({ method: "POST" }),
      ),
    );
    // The row now has a span duration -- applyRecord's upsert landed the response.
    await waitFor(() => expect(screen.getByTestId("event-row-1").textContent).toContain("·"));
  });

  it("not editable: no edit button on id'd rows, even in review mode", async () => {
    useReviewStore.setState({ mode: "review", editable: false });
    load();
    await screen.findByTestId("events-panel");
    // kitchen-sink's events all have real ids (1-4) -- an edit affordance
    // here would save into a server that has no mutation route to accept it
    // (a plain export opened read-only, not a .db).
    expect(screen.queryByTestId("event-edit-1")).toBeNull();
    expect(screen.queryByTestId("event-edit-2")).toBeNull();
    expect(screen.queryByTestId("event-edit-3")).toBeNull();
    expect(screen.queryByTestId("event-edit-4")).toBeNull();
  });

  it("not editable: no End now on a live, id'd, endless row", async () => {
    useReviewStore.setState({ mode: "live", editable: false });
    load();
    await screen.findByTestId("events-panel");
    // ids 1 and 4 are endless (no end_timestamp) -- End now must not offer
    // to end them against a server that would just reject the POST.
    expect(screen.queryByTestId("event-endnow-1")).toBeNull();
    expect(screen.queryByTestId("event-endnow-4")).toBeNull();
  });

  it("id-less rows get no edit or End now affordances even when live+editable", async () => {
    useReviewStore.setState({ mode: "live", editable: true });
    const synthDoc = JSON.stringify({
      format: 1,
      sessions: [
        {
          id: "synth-idless",
          label: "test",
          start: "2026-07-01T08:00:00Z",
          end: "2026-07-01T09:00:00Z",
          lab: { elements: [], hosts: [{ id: "h1", element: "h1" }], links: [] },
          meta: { interval: 60.0, charts: [], tabs: [] },
          metrics: [],
          events: [{ timestamp: "2026-07-01T08:10:00Z" }],
          log_events: [],
          chart_map: {},
        },
      ],
    });
    useReviewStore.getState().actions.importMonitorSessions(synthDoc, "test-idless.json");
    render(<EventsPanel isOpen onClose={() => {}} />);
    await screen.findByTestId("events-panel");
    expect(screen.getByTestId("event-row--1")).toBeTruthy();
    expect(screen.queryByTestId("event-edit--1")).toBeNull();
    expect(screen.queryByTestId("event-endnow--1")).toBeNull();
  });
});
