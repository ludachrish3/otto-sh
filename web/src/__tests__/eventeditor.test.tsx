// web/src/__tests__/eventeditor.test.tsx
// Fetch-mocked against the real stores, same idiom as markcontrol.test.tsx/
// eventapi.test.ts: the store IS the assertion surface for eventEditor
// open/close, and the panel's own testids are the assertion surface for
// everything else. Renders <EventEditor /> directly (not the whole <App />)
// -- opening/closing is driven straight through uiStore's actions, exactly
// how MarkControl/EventsPanel trigger it in production.
import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import kitchenSink from "../../fixtures/kitchen-sink.json";
import { useReviewStore } from "../data/reviewStore";
import { EventEditor } from "../shell/EventEditor";
import { type EventDraft, useUiStore } from "../ui/uiStore";

// jsdom lacks CSS.escape; react-aria menus/selects call it unconditionally
// (same polyfill as rangepicker.test.tsx/reviewbar.test.tsx/markcontrol.test.tsx).
if (typeof globalThis.CSS === "undefined") {
  Object.defineProperty(globalThis, "CSS", {
    value: { escape: (value: string) => value.replace(/[^a-zA-Z0-9_-]/g, (ch) => `\\${ch}`) },
    writable: true,
  });
}

// jsdom doesn't implement matchMedia; the vendored Select/date-picker
// pieces probe it via useBreakpoint (same polyfill as rangepicker.test.tsx).
if (typeof window.matchMedia !== "function") {
  window.matchMedia = ((query: string) => ({
    matches: true,
    media: query,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    onchange: null,
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia;
}

afterEach(() => {
  // No `test.globals: true` in vitest.config -- RTL's automatic
  // afterEach(cleanup) never kicks in (same rationale as rangepicker.test.tsx).
  cleanup();
  useUiStore.getState().actions.closeEventEditor();
  vi.unstubAllGlobals();
});

// kitchen-sink.json's first session id -- hydrate once per test so
// edit-mode has real events (ids 1-4) to resolve by id.
function hydrate(): string {
  useReviewStore.getState().actions.importMonitorSessions(JSON.stringify(kitchenSink), "test");
  useReviewStore.getState().actions.setEditable(true);
  const id = useReviewStore.getState().sessions[0]?.id;
  if (!id) throw new Error("fixture has no session");
  return id;
}

function okJson(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status });
}

function record(overrides: Record<string, unknown> = {}) {
  return {
    id: 99,
    timestamp: "2026-07-01T09:10:00Z",
    label: "deploy",
    source: "manual",
    color: "#888888",
    dash: "dash",
    ...overrides,
  };
}

function blankDraft(sessionId: string, overrides: Partial<EventDraft> = {}): EventDraft {
  return {
    sessionId,
    timestampMs: new Date("2026-07-01T09:10:00Z").getTime(),
    endTimestampMs: null,
    label: "deploy",
    color: "#888888",
    dash: "dash",
    ...overrides,
  };
}

function jsonBody(fetchMock: ReturnType<typeof vi.fn>, callIndex = 0): Record<string, unknown> {
  const call = fetchMock.mock.calls[callIndex] as [string, RequestInit] | undefined;
  if (!call) throw new Error(`fetch was not called (call #${callIndex})`);
  return JSON.parse(String(call[1]?.body)) as Record<string, unknown>;
}

describe("EventEditor", () => {
  it("renders nothing while the editor is closed", () => {
    hydrate();
    const { container } = render(<EventEditor />);
    expect(container.firstChild).toBeNull();
  });

  it("a draft target opens the panel with prefilled fields", () => {
    const sid = hydrate();
    useUiStore
      .getState()
      .actions.openEventEditor({ kind: "draft", draft: blankDraft(sid, { label: "canary" }) });
    render(<EventEditor />);
    expect(screen.getByTestId("event-editor")).toBeTruthy();
    expect((screen.getByTestId("editor-label") as HTMLInputElement).value).toBe("canary");
    // Point event (no end): the clear-end control still renders, but there's
    // nothing to clear -- exercised for real by the end-clearing test below.
    expect(screen.getByTestId("editor-start")).toBeTruthy();
    expect(screen.getByTestId("editor-end")).toBeTruthy();
  });

  it("an edit target resolves the record from session.events by id and prefills from it", () => {
    const sid = hydrate();
    // event id 1 in kitchen-sink.json: "config reload", 08:20, #7c5cff (one
    // of EVENT_COLOR_SWATCHES -- picked so the ring assertion below has a
    // real swatch button to check).
    useUiStore.getState().actions.openEventEditor({ kind: "edit", sessionId: sid, eventId: 1 });
    render(<EventEditor />);
    expect((screen.getByTestId("editor-label") as HTMLInputElement).value).toBe("config reload");
    expect(screen.getByTestId("editor-color-#7c5cff").getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByTestId("editor-color-#888888").getAttribute("aria-pressed")).toBe("false");
  });

  it("Delete is edit-mode only -- absent for a draft target", () => {
    const sid = hydrate();
    useUiStore.getState().actions.openEventEditor({ kind: "draft", draft: blankDraft(sid) });
    render(<EventEditor />);
    expect(screen.queryByTestId("editor-delete")).toBeNull();
  });

  it("Save on a draft POSTs create with the draft's exact ms as ISO strings, then closes", async () => {
    // MUTATION CHECK (per task-9-brief.md Step 3/4): this is the assertion
    // that must fail if Save is mutated to send `timestamp:
    // new Date().toISOString()` unconditionally instead of the draft's ms.
    // draft.timestampMs/endTimestampMs are fixed 2026-07-01 instants, nowhere
    // near "now" at test-run time, so a wall-clock substitution cannot
    // accidentally match. Verified by hand -- see task-9-report.md.
    const user = userEvent.setup();
    const sid = hydrate();
    const draft = blankDraft(sid, {
      timestampMs: new Date("2026-07-01T09:10:00Z").getTime(),
      endTimestampMs: new Date("2026-07-01T09:20:00Z").getTime(),
      label: "canary",
      color: "#7c5cff",
      dash: "dot",
    });
    useUiStore.getState().actions.openEventEditor({ kind: "draft", draft });
    const fetchMock = vi.fn().mockResolvedValue(okJson(record({ id: 101, label: "canary" }), 201));
    vi.stubGlobal("fetch", fetchMock);
    render(<EventEditor />);
    await user.click(screen.getByTestId("editor-save"));
    await waitFor(() => expect(useUiStore.getState().eventEditor).toBeNull());
    expect(fetchMock).toHaveBeenCalledWith(
      `/api/session/${encodeURIComponent(sid)}/event`,
      expect.objectContaining({ method: "POST" }),
    );
    expect(jsonBody(fetchMock)).toMatchObject({
      label: "canary",
      timestamp: new Date(draft.timestampMs).toISOString(),
      end_timestamp: new Date(draft.endTimestampMs as number).toISOString(),
      color: "#7c5cff",
      dash: "dot",
    });
  });

  it("Save on an edit target sends a full-field PATCH", async () => {
    const user = userEvent.setup();
    const sid = hydrate();
    useUiStore.getState().actions.openEventEditor({ kind: "edit", sessionId: sid, eventId: 1 });
    const fetchMock = vi
      .fn()
      .mockResolvedValue(okJson(record({ id: 1, label: "config reload v2" }), 200));
    vi.stubGlobal("fetch", fetchMock);
    render(<EventEditor />);
    const label = screen.getByTestId("editor-label") as HTMLInputElement;
    await user.clear(label);
    await user.type(label, "config reload v2");
    await user.click(screen.getByTestId("editor-save"));
    await waitFor(() => expect(useUiStore.getState().eventEditor).toBeNull());
    expect(fetchMock).toHaveBeenCalledWith(
      `/api/session/${encodeURIComponent(sid)}/event/1`,
      expect.objectContaining({ method: "PATCH" }),
    );
    const body = jsonBody(fetchMock);
    expect(body.label).toBe("config reload v2");
    // event id 1 has no end_timestamp in the fixture -- an untouched edit
    // must still explicitly clear it (full-field PATCH), not omit the key.
    expect(body).toHaveProperty("end_timestamp", null);
  });

  it("clearing the end field on an edit explicitly PATCHes end_timestamp: null", async () => {
    const user = userEvent.setup();
    const sid = hydrate();
    // event id 2 has BOTH a start and an end -- start from a real span.
    useUiStore.getState().actions.openEventEditor({ kind: "edit", sessionId: sid, eventId: 2 });
    const fetchMock = vi
      .fn()
      .mockResolvedValue(okJson(record({ id: 2, label: "stress run" }), 200));
    vi.stubGlobal("fetch", fetchMock);
    render(<EventEditor />);
    await user.click(screen.getByTestId("editor-end-clear"));
    await user.click(screen.getByTestId("editor-save"));
    await waitFor(() => expect(useUiStore.getState().eventEditor).toBeNull());
    const body = jsonBody(fetchMock);
    expect(body).toHaveProperty("end_timestamp", null);
  });

  it("Save is disabled while the label is blank", () => {
    const sid = hydrate();
    useUiStore
      .getState()
      .actions.openEventEditor({ kind: "draft", draft: blankDraft(sid, { label: "" }) });
    render(<EventEditor />);
    expect(screen.getByTestId("editor-save").hasAttribute("disabled")).toBe(true);
  });

  it("Save is disabled while endTimestampMs <= timestampMs (mirrors the server's 422)", () => {
    const sid = hydrate();
    const now = new Date("2026-07-01T09:10:00Z").getTime();
    useUiStore.getState().actions.openEventEditor({
      kind: "draft",
      draft: blankDraft(sid, { timestampMs: now, endTimestampMs: now - 1000 }),
    });
    render(<EventEditor />);
    expect(screen.getByTestId("editor-save").hasAttribute("disabled")).toBe(true);
  });

  it("Delete requires a second press (relabels 'Really delete?') before it DELETEs", async () => {
    const user = userEvent.setup();
    const sid = hydrate();
    useUiStore.getState().actions.openEventEditor({ kind: "edit", sessionId: sid, eventId: 2 });
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);
    render(<EventEditor />);
    const deleteButton = screen.getByTestId("editor-delete");
    expect(deleteButton.textContent).toBe("Delete");
    await user.click(deleteButton);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(screen.getByTestId("editor-delete").textContent).toBe("Really delete?");
    await user.click(screen.getByTestId("editor-delete"));
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        `/api/session/${encodeURIComponent(sid)}/event/2`,
        expect.objectContaining({ method: "DELETE" }),
      ),
    );
    await waitFor(() => expect(useUiStore.getState().eventEditor).toBeNull());
  });

  it("a 422 on Save keeps the editor open and shows editor-error", async () => {
    const user = userEvent.setup();
    const sid = hydrate();
    useUiStore
      .getState()
      .actions.openEventEditor({ kind: "draft", draft: blankDraft(sid, { dash: "dash" }) });
    const fetchMock = vi.fn().mockResolvedValue(
      okJson(
        {
          detail: [
            { loc: ["body", "dash"], msg: "dash must be one of [...]", type: "value_error" },
          ],
        },
        422,
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    render(<EventEditor />);
    await user.click(screen.getByTestId("editor-save"));
    await waitFor(() =>
      expect(screen.getByTestId("editor-error").textContent).toBe("dash must be one of [...]"),
    );
    expect(useUiStore.getState().eventEditor).not.toBeNull();
    expect(screen.getByTestId("event-editor")).toBeTruthy();
  });

  it("a failed Delete surfaces editor-error and does not close the editor", async () => {
    const user = userEvent.setup();
    const sid = hydrate();
    useUiStore.getState().actions.openEventEditor({ kind: "edit", sessionId: sid, eventId: 2 });
    const fetchMock = vi.fn().mockResolvedValue(okJson({ error: "archive is locked" }, 409));
    vi.stubGlobal("fetch", fetchMock);
    render(<EventEditor />);
    await user.click(screen.getByTestId("editor-delete"));
    await user.click(screen.getByTestId("editor-delete"));
    await waitFor(() =>
      expect(screen.getByTestId("editor-error").textContent).toBe("archive is locked"),
    );
    expect(useUiStore.getState().eventEditor).not.toBeNull();
  });

  it("reopening on a new target never shows a stale abandoned edit or an armed delete", async () => {
    const user = userEvent.setup();
    const sid = hydrate();
    useUiStore.getState().actions.openEventEditor({ kind: "edit", sessionId: sid, eventId: 2 });
    vi.stubGlobal("fetch", vi.fn());
    const { rerender } = render(<EventEditor />);
    await user.click(screen.getByTestId("editor-delete"));
    expect(screen.getByTestId("editor-delete").textContent).toBe("Really delete?");
    const label = screen.getByTestId("editor-label") as HTMLInputElement;
    await user.clear(label);
    await user.type(label, "abandoned edit");
    // Reopen on a DIFFERENT event -- a fresh target identity.
    act(() => {
      useUiStore.getState().actions.openEventEditor({ kind: "edit", sessionId: sid, eventId: 3 });
    });
    rerender(<EventEditor />);
    expect((screen.getByTestId("editor-label") as HTMLInputElement).value).toBe("log capture");
    expect(screen.getByTestId("editor-delete").textContent).toBe("Delete");
  });

  it("choosing a color swatch updates the selection ring and is sent on Save", async () => {
    const user = userEvent.setup();
    const sid = hydrate();
    useUiStore.getState().actions.openEventEditor({ kind: "draft", draft: blankDraft(sid) });
    const fetchMock = vi.fn().mockResolvedValue(okJson(record(), 201));
    vi.stubGlobal("fetch", fetchMock);
    render(<EventEditor />);
    await user.click(screen.getByTestId("editor-color-#2ca02c"));
    expect(screen.getByTestId("editor-color-#2ca02c").getAttribute("aria-pressed")).toBe("true");
    await user.click(screen.getByTestId("editor-save"));
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(jsonBody(fetchMock)).toMatchObject({ color: "#2ca02c" });
  });

  it("choosing a dash style via the vendored Select is sent on Save", async () => {
    const user = userEvent.setup();
    const sid = hydrate();
    useUiStore.getState().actions.openEventEditor({ kind: "draft", draft: blankDraft(sid) });
    const fetchMock = vi.fn().mockResolvedValue(okJson(record(), 201));
    vi.stubGlobal("fetch", fetchMock);
    render(<EventEditor />);
    // The vendored Select spreads data-testid onto its outer wrapper, not
    // the pressable button nested inside (same "wrapper, not leaf" gap
    // reviewbar.test.tsx documents for the session picker) -- scope in and
    // query the role.
    await user.click(within(screen.getByTestId("editor-dash")).getByRole("button"));
    await user.click(within(screen.getByRole("listbox")).getByRole("option", { name: "longdash" }));
    await user.click(screen.getByTestId("editor-save"));
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(jsonBody(fetchMock)).toMatchObject({ dash: "longdash" });
  });

  it("driving the start field's second segment by keyboard changes the saved timestamp", async () => {
    // react-aria date-field segments need real interaction (pointer + key
    // events), same idiom as rangepicker.test.tsx's minute-segment tests --
    // this proves the field is wired both ways (not just prefilled) and
    // exercises calendarTime.ts's round-trip through a real user gesture.
    const user = userEvent.setup();
    const sid = hydrate();
    const draft = blankDraft(sid, {
      timestampMs: new Date("2026-07-01T09:10:00Z").getTime(),
      endTimestampMs: null,
    });
    useUiStore.getState().actions.openEventEditor({ kind: "draft", draft });
    const fetchMock = vi.fn().mockResolvedValue(okJson(record(), 201));
    vi.stubGlobal("fetch", fetchMock);
    render(<EventEditor />);
    const secondSegment = screen
      .getByTestId("editor-start")
      .querySelector('[data-type="second"]') as HTMLElement;
    expect(secondSegment).toBeTruthy();
    await user.click(secondSegment);
    for (let i = 0; i < 5; i++) {
      await user.keyboard("{ArrowUp}");
    }
    await user.click(screen.getByTestId("editor-save"));
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const body = jsonBody(fetchMock);
    expect(body.timestamp).toBe(new Date(draft.timestampMs + 5000).toISOString());
  });
});
