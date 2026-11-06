// web/src/__tests__/eventeditor.test.tsx
// Fetch-mocked against the real stores, same idiom as markcontrol.test.tsx/
// eventapi.test.ts: the store IS the assertion surface for eventEditor
// open/close, and the panel's own testids are the assertion surface for
// everything else. Renders <EventEditor /> directly (not the whole <App />)
// -- opening/closing is driven straight through uiStore's actions, exactly
// how MarkControl/EventsPanel trigger it in production.
import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import kitchenSink from "../../fixtures/kitchen-sink.json" with { type: "json" };
import { installOpenSpanReconciler } from "../data/openSpan";
import { useReviewStore } from "../data/reviewStore";
import { EventEditor } from "../shell/EventEditor";
import { calendarDateTimeToMs, msToCalendarDateTime } from "../ui/calendarTime";
import { type EventDraft, useUiStore } from "../ui/uiStore";

// jsdom lacks CSS.escape; react-aria menus/selects call it unconditionally
// (same polyfill as rangepicker.test.tsx/reviewbar.test.tsx/markcontrol.test.tsx).
if (typeof globalThis.CSS === "undefined") {
  Object.defineProperty(globalThis, "CSS", {
    value: { escape: (value: string) => value.replace(/[^a-zA-Z0-9_-]/gu, (ch) => `\\${ch}`) },
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

// A start instant whose LOCAL day is a single digit (5) in every timezone
// from UTC-11 to UTC+11, so one Backspace on the day segment clears it
// (react-aria drops one digit per Backspace; a lone digit clears the segment).
const DAY5_MS = new Date("2026-07-05T12:00:00Z").getTime();

type User = ReturnType<typeof userEvent.setup>;

function seg(field: string, type: string): HTMLElement | null {
  return screen.getByTestId(field).querySelector(`[data-type="${type}"]`);
}

function editableSegments(field: string): HTMLElement[] {
  return Array.from(
    screen
      .getByTestId(field)
      .querySelectorAll<HTMLElement>('[data-type]:not([data-type="literal"])'),
  );
}

function isPlaceholder(el: HTMLElement | null): boolean {
  return el?.hasAttribute("data-placeholder") ?? false;
}

// Clears every editable segment of *field* by keyboard, re-querying each
// segment by type after every keypress (the field re-renders as it goes).
async function clearField(user: User, field: string): Promise<void> {
  const types = editableSegments(field).map((el) => el.getAttribute("data-type") as string);
  for (const type of types) {
    await user.click(seg(field, type) as HTMLElement);
    for (let i = 0; i < 6 && !isPlaceholder(seg(field, type)); i++) {
      await user.keyboard("{Backspace}");
    }
  }
}

async function typeSegment(user: User, field: string, type: string, text: string): Promise<void> {
  const el = seg(field, type);
  if (el === null) return; // e.g. no dayPeriod segment under a 24-hour locale
  await user.click(el);
  await user.keyboard(text);
}

function saveDisabled(): boolean {
  return screen.getByTestId("editor-save").hasAttribute("disabled");
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
    expect(body["label"]).toBe("config reload v2");
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

  it("Delete opens a dialog naming the event, and a double-click deletes nothing", async () => {
    const user = userEvent.setup();
    const sid = hydrate();
    useUiStore.getState().actions.openEventEditor({ kind: "edit", sessionId: sid, eventId: 2 });
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);
    render(<EventEditor />);
    const deleteButton = screen.getByTestId("editor-delete");
    expect(deleteButton.textContent).toBe("Delete");
    await user.dblClick(deleteButton);
    expect(fetchMock).not.toHaveBeenCalled();
    const dialog = screen.getByTestId("confirm-delete");
    expect(dialog.getAttribute("role")).toBe("alertdialog");
    expect(within(dialog).getByText("Delete event?")).toBeTruthy();
    expect(dialog.textContent).toContain("\u201cstress run\u201d will be removed.");
    expect(dialog.textContent).toContain("This can't be undone.");
    expect(screen.getByTestId("editor-delete").textContent).toBe("Delete");
    expect(document.activeElement).toBe(screen.getByTestId("confirm-delete-cancel"));
  });

  it("the dialog names the stored label, not an unsaved edit to the label field", async () => {
    const user = userEvent.setup();
    const sid = hydrate();
    useUiStore.getState().actions.openEventEditor({ kind: "edit", sessionId: sid, eventId: 2 });
    vi.stubGlobal("fetch", vi.fn());
    render(<EventEditor />);
    const input = screen.getByTestId("editor-label");
    await user.clear(input);
    await user.type(input, "renamed");
    await user.click(screen.getByTestId("editor-delete"));
    const dialog = screen.getByTestId("confirm-delete");
    expect(dialog.textContent).toContain("“stress run” will be removed.");
    expect(dialog.textContent).not.toContain("renamed");
  });

  it("Confirm sends one DELETE and closes editor and dialog", async () => {
    const user = userEvent.setup();
    const sid = hydrate();
    useUiStore.getState().actions.openEventEditor({ kind: "edit", sessionId: sid, eventId: 2 });
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);
    render(<EventEditor />);
    await user.click(screen.getByTestId("editor-delete"));
    await user.dblClick(screen.getByTestId("confirm-delete-confirm"));
    await waitFor(() => expect(useUiStore.getState().eventEditor).toBeNull());
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledWith(
      `/api/session/${encodeURIComponent(sid)}/event/2`,
      expect.objectContaining({ method: "DELETE" }),
    );
    expect(screen.queryByTestId("confirm-delete")).toBeNull();
    expect(screen.queryByTestId("event-editor")).toBeNull();
  });

  it("Cancel and Escape close only the dialog; the editor stays and Delete has focus", async () => {
    const user = userEvent.setup();
    const sid = hydrate();
    useUiStore.getState().actions.openEventEditor({ kind: "edit", sessionId: sid, eventId: 2 });
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    render(<EventEditor />);
    await user.click(screen.getByTestId("editor-delete"));
    expect(screen.getByTestId("confirm-delete")).toBeTruthy();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByTestId("confirm-delete")).toBeNull());
    expect(screen.getByTestId("event-editor")).toBeTruthy();
    expect(useUiStore.getState().eventEditor).not.toBeNull();
    await waitFor(() => expect(document.activeElement).toBe(screen.getByTestId("editor-delete")));
    await user.click(screen.getByTestId("editor-delete"));
    await user.click(screen.getByTestId("confirm-delete-cancel"));
    await waitFor(() => expect(screen.queryByTestId("confirm-delete")).toBeNull());
    expect(screen.getByTestId("event-editor")).toBeTruthy();
    expect(fetchMock).not.toHaveBeenCalled();
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

  it("a failed delete keeps the dialog open with the error", async () => {
    const user = userEvent.setup();
    const sid = hydrate();
    useUiStore.getState().actions.openEventEditor({ kind: "edit", sessionId: sid, eventId: 2 });
    const fetchMock = vi.fn().mockResolvedValue(okJson({ error: "archive is locked" }, 409));
    vi.stubGlobal("fetch", fetchMock);
    render(<EventEditor />);
    await user.click(screen.getByTestId("editor-delete"));
    await user.click(screen.getByTestId("confirm-delete-confirm"));
    const dialog = screen.getByTestId("confirm-delete");
    await waitFor(() =>
      expect(within(dialog).getByRole("alert").textContent).toBe("archive is locked"),
    );
    expect(screen.getByTestId("confirm-delete")).toBeTruthy();
    expect(useUiStore.getState().eventEditor).not.toBeNull();
  });

  it("reopening on a new target never shows a stale abandoned edit or an open delete dialog", async () => {
    const user = userEvent.setup();
    const sid = hydrate();
    useUiStore.getState().actions.openEventEditor({ kind: "edit", sessionId: sid, eventId: 2 });
    vi.stubGlobal("fetch", vi.fn());
    const { rerender } = render(<EventEditor />);
    await user.click(screen.getByTestId("editor-delete"));
    expect(screen.getByTestId("confirm-delete")).toBeTruthy();
    await user.keyboard("{Escape}");
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
    expect(screen.queryByTestId("confirm-delete")).toBeNull();
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
    expect(body["timestamp"]).toBe(new Date(draft.timestampMs + 5000).toISOString());
  });

  describe("incomplete and invalid dates (#864)", () => {
    function openDraft(overrides: Partial<EventDraft> = {}): {
      fetchMock: ReturnType<typeof vi.fn>;
    } {
      const sid = hydrate();
      useUiStore.getState().actions.openEventEditor({
        kind: "draft",
        draft: blankDraft(sid, { timestampMs: DAY5_MS, ...overrides }),
      });
      const fetchMock = vi.fn().mockResolvedValue(okJson(record(), 201));
      vi.stubGlobal("fetch", fetchMock);
      render(<EventEditor />);
      return { fetchMock };
    }

    it("clearing one start segment disables Save and explains", async () => {
      const user = userEvent.setup();
      const { fetchMock } = openDraft();
      expect(saveDisabled()).toBe(false);
      await user.click(seg("editor-start", "day") as HTMLElement);
      await user.keyboard("{Backspace}");
      expect(saveDisabled()).toBe(true);
      expect(screen.getByText("Enter a complete start time.")).toBeTruthy();
      await user.click(screen.getByTestId("editor-save"));
      expect(fetchMock).not.toHaveBeenCalled();
    });

    it("the cleared segment stays blank after effects settle", async () => {
      const user = userEvent.setup();
      openDraft();
      await user.click(seg("editor-start", "day") as HTMLElement);
      await user.keyboard("{Backspace}");
      await waitFor(() => expect(screen.getByText("Enter a complete start time.")).toBeTruthy());
      // A completeness report that handed the field a new value identity
      // would make react-stately reset its display to the old date here.
      expect(isPlaceholder(seg("editor-start", "day"))).toBe(true);
      expect(saveDisabled()).toBe(true);
    });

    it("typing a complete start again re-enables Save", async () => {
      const user = userEvent.setup();
      const { fetchMock } = openDraft();
      await user.click(seg("editor-start", "day") as HTMLElement);
      await user.keyboard("{Backspace}");
      await waitFor(() => expect(saveDisabled()).toBe(true));
      await user.keyboard("15");
      await waitFor(() => expect(saveDisabled()).toBe(false));
      expect(screen.queryByText("Enter a complete start time.")).toBeNull();
      await user.click(screen.getByTestId("editor-save"));
      await waitFor(() => expect(fetchMock).toHaveBeenCalled());
      const expected = calendarDateTimeToMs(msToCalendarDateTime(DAY5_MS).set({ day: 15 }));
      expect(jsonBody(fetchMock)["timestamp"]).toBe(new Date(expected).toISOString());
    });

    it("clearing every Start segment blocks Save", async () => {
      const user = userEvent.setup();
      const { fetchMock } = openDraft();
      await clearField(user, "editor-start");
      await waitFor(() => expect(screen.getByText("Enter a start time.")).toBeTruthy());
      // The field calls onChange(null) and resets to its controlled value;
      // the old date must not reappear.
      for (const el of editableSegments("editor-start")) expect(isPlaceholder(el)).toBe(true);
      expect(saveDisabled()).toBe(true);
      // Typing a full date again re-enables Save.
      await typeSegment(user, "editor-start", "month", "07");
      await typeSegment(user, "editor-start", "day", "05");
      await typeSegment(user, "editor-start", "year", "2026");
      await typeSegment(user, "editor-start", "hour", "09");
      await typeSegment(user, "editor-start", "minute", "30");
      await typeSegment(user, "editor-start", "second", "00");
      await typeSegment(user, "editor-start", "dayPeriod", "A");
      await waitFor(() => expect(saveDisabled()).toBe(false));
      expect(screen.queryByText("Enter a start time.")).toBeNull();
      await user.click(screen.getByTestId("editor-save"));
      await waitFor(() => expect(fetchMock).toHaveBeenCalled());
      const expected = calendarDateTimeToMs(
        msToCalendarDateTime(DAY5_MS).set({ month: 7, day: 5, hour: 9, minute: 30, second: 0 }),
      );
      expect(jsonBody(fetchMock)["timestamp"]).toBe(new Date(expected).toISOString());
    });

    it("clearing every End segment makes a point event", async () => {
      const user = userEvent.setup();
      const sid = hydrate();
      // event id 2 has both a start and an end.
      useUiStore.getState().actions.openEventEditor({ kind: "edit", sessionId: sid, eventId: 2 });
      const fetchMock = vi.fn().mockResolvedValue(okJson(record({ id: 2 }), 200));
      vi.stubGlobal("fetch", fetchMock);
      render(<EventEditor />);
      await clearField(user, "editor-end");
      await waitFor(() => {
        for (const el of editableSegments("editor-end")) expect(isPlaceholder(el)).toBe(true);
      });
      expect(saveDisabled()).toBe(false);
      await user.click(screen.getByTestId("editor-save"));
      await waitFor(() => expect(fetchMock).toHaveBeenCalled());
      expect(jsonBody(fetchMock)).toHaveProperty("end_timestamp", null);
    });

    it("partially entering an empty End explains, ✕ clears it", async () => {
      const user = userEvent.setup();
      const { fetchMock } = openDraft({ endTimestampMs: null });
      expect(screen.getByTestId("editor-end-clear").hasAttribute("disabled")).toBe(true);
      await typeSegment(user, "editor-end", "month", "7");
      await waitFor(() =>
        expect(screen.getByText("Enter a complete end time, or clear it with ✕.")).toBeTruthy(),
      );
      expect(saveDisabled()).toBe(true);
      await user.click(screen.getByTestId("editor-end-clear"));
      await waitFor(() => expect(saveDisabled()).toBe(false));
      expect(screen.queryByText("Enter a complete end time, or clear it with ✕.")).toBeNull();
      for (const el of editableSegments("editor-end")) expect(isPlaceholder(el)).toBe(true);
      await user.click(screen.getByTestId("editor-save"));
      await waitFor(() => expect(fetchMock).toHaveBeenCalled());
      expect(jsonBody(fetchMock)).not.toHaveProperty("end_timestamp");
    });

    // A new target with the SAME instants hands the fields equal epochs, so
    // their memoised values keep their identity: only a remount drops the
    // previous target's half-typed segments.
    it.each([
      { field: "editor-start", end: null },
      { field: "editor-end", end: DAY5_MS + 3_600_000 },
    ])("a new target with equal times drops $field's partial segments", async ({ field, end }) => {
      const user = userEvent.setup();
      const { fetchMock } = openDraft({ endTimestampMs: end });
      await user.click(seg(field, "day") as HTMLElement);
      await user.keyboard("{Backspace}");
      await waitFor(() => expect(saveDisabled()).toBe(true));
      const sid = useReviewStore.getState().sessions[0]?.id ?? "";
      act(() => {
        useUiStore.getState().actions.openEventEditor({
          kind: "draft",
          draft: blankDraft(sid, { timestampMs: DAY5_MS, endTimestampMs: end, label: "second" }),
        });
      });
      await waitFor(() =>
        expect(screen.getByTestId("editor-label")).toHaveProperty("value", "second"),
      );
      for (const el of editableSegments(field)) expect(isPlaceholder(el)).toBe(false);
      expect(saveDisabled()).toBe(false);
      await user.click(screen.getByTestId("editor-save"));
      await waitFor(() => expect(fetchMock).toHaveBeenCalled());
      const body = jsonBody(fetchMock);
      expect(body["label"]).toBe("second");
      expect(body["timestamp"]).toBe(new Date(DAY5_MS).toISOString());
      expect(body["end_timestamp"]).toBe(end === null ? undefined : new Date(end).toISOString());
    });

    it("end before start explains on the End field", () => {
      openDraft({ endTimestampMs: DAY5_MS - 1000 });
      expect(saveDisabled()).toBe(true);
      expect(screen.getByText("End must be after Start.")).toBeTruthy();
    });

    it("server error is announced", async () => {
      const user = userEvent.setup();
      const sid = hydrate();
      useUiStore.getState().actions.openEventEditor({ kind: "edit", sessionId: sid, eventId: 2 });
      vi.stubGlobal(
        "fetch",
        vi.fn().mockResolvedValue(okJson({ error: "archive is locked" }, 409)),
      );
      render(<EventEditor />);
      await user.click(screen.getByTestId("editor-save"));
      await waitFor(() => expect(screen.getByRole("alert").textContent).toBe("archive is locked"));
    });
  });
});

describe("EventEditor pending gate (#867)", () => {
  function held() {
    let release!: (r: Response) => void;
    const promise = new Promise<Response>((res) => {
      release = res;
    });
    return { fetchMock: vi.fn().mockReturnValue(promise), release };
  }

  it("two Save presses while the POST is held send one POST", async () => {
    const user = userEvent.setup();
    const sid = hydrate();
    useUiStore.getState().actions.openEventEditor({
      kind: "draft",
      draft: blankDraft(sid, { label: "canary" }),
    });
    const { fetchMock, release } = held();
    vi.stubGlobal("fetch", fetchMock);
    render(<EventEditor />);
    await user.click(screen.getByTestId("editor-save"));
    await user.click(screen.getByTestId("editor-save"));
    expect(fetchMock.mock.calls.length).toBe(1);
    expect(screen.getByTestId("editor-save").hasAttribute("data-loading")).toBe(true);
    await act(() => Promise.resolve(release(okJson(record({ id: 101, label: "canary" }), 201))));
    await waitFor(() => expect(useUiStore.getState().eventEditor).toBeNull());
    expect(fetchMock.mock.calls.length).toBe(1);
  });

  it("three Confirm presses while the DELETE is held send one DELETE", async () => {
    const user = userEvent.setup();
    const sid = hydrate();
    useUiStore.getState().actions.openEventEditor({ kind: "edit", sessionId: sid, eventId: 2 });
    const { fetchMock, release } = held();
    vi.stubGlobal("fetch", fetchMock);
    render(<EventEditor />);
    await user.click(screen.getByTestId("editor-delete"));
    await user.click(screen.getByTestId("confirm-delete-confirm"));
    await user.click(screen.getByTestId("confirm-delete-confirm"));
    await user.click(screen.getByTestId("confirm-delete-confirm"));
    expect(fetchMock.mock.calls.length).toBe(1);
    expect(screen.getByTestId("confirm-delete-confirm").hasAttribute("data-loading")).toBe(true);
    await act(() => Promise.resolve(release(new Response(null, { status: 204 }))));
    await waitFor(() => expect(useUiStore.getState().eventEditor).toBeNull());
    expect(fetchMock.mock.calls.length).toBe(1);
  });

  it("a held Save spins Save alone; the editor's Delete is disabled, not spinning", async () => {
    const user = userEvent.setup();
    const sid = hydrate();
    useUiStore.getState().actions.openEventEditor({ kind: "edit", sessionId: sid, eventId: 2 });
    vi.stubGlobal("fetch", held().fetchMock);
    render(<EventEditor />);
    await user.click(screen.getByTestId("editor-save"));
    expect(screen.getByTestId("editor-save").hasAttribute("data-loading")).toBe(true);
    const del = screen.getByTestId("editor-delete");
    expect(del.hasAttribute("data-loading")).toBe(false);
    expect(del.hasAttribute("disabled")).toBe(true);
  });

  it("a held Confirm spins the dialog's Delete alone, not the editor's Save or Delete", async () => {
    const user = userEvent.setup();
    const sid = hydrate();
    useUiStore.getState().actions.openEventEditor({ kind: "edit", sessionId: sid, eventId: 2 });
    vi.stubGlobal("fetch", held().fetchMock);
    render(<EventEditor />);
    await user.click(screen.getByTestId("editor-delete"));
    await user.click(screen.getByTestId("confirm-delete-confirm"));
    expect(screen.getByTestId("confirm-delete-confirm").hasAttribute("data-loading")).toBe(true);
    expect(screen.getByTestId("editor-save").hasAttribute("data-loading")).toBe(false);
    expect(screen.getByTestId("editor-delete").hasAttribute("data-loading")).toBe(false);
  });

  it("a Save still pending for one event does not make another event's editor busy (#932)", async () => {
    const user = userEvent.setup();
    const sid = hydrate();
    useUiStore.getState().actions.openEventEditor({ kind: "edit", sessionId: sid, eventId: 2 });
    vi.stubGlobal("fetch", held().fetchMock);
    render(<EventEditor />);
    await user.click(screen.getByTestId("editor-save"));
    act(() => {
      useUiStore.getState().actions.openEventEditor({ kind: "edit", sessionId: sid, eventId: 3 });
    });
    await waitFor(() =>
      expect(screen.getByTestId("editor-save").hasAttribute("data-loading")).toBe(false),
    );
    expect(screen.getByTestId("editor-save").hasAttribute("disabled")).toBe(false);
  });
});

describe("EventEditor and the tracked span (#868)", () => {
  // kitchen-sink's event 1 has no end: track it as the open span.
  function trackEvent1(): string {
    const sid = hydrate();
    useUiStore.getState().actions.setOpenSpan({ sessionId: sid, eventId: 1 });
    return sid;
  }

  let uninstall: () => void;
  beforeEach(() => {
    uninstall = installOpenSpanReconciler();
  });
  afterEach(() => {
    uninstall();
    useUiStore.setState({ openSpan: null });
  });

  it("deleting the tracked span from the editor clears it", async () => {
    const user = userEvent.setup();
    const sid = trackEvent1();
    useUiStore.getState().actions.openEventEditor({ kind: "edit", sessionId: sid, eventId: 1 });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status: 204 })));
    render(<EventEditor />);
    await user.click(screen.getByTestId("editor-delete"));
    await user.click(screen.getByTestId("confirm-delete-confirm"));
    await waitFor(() => expect(useUiStore.getState().eventEditor).toBeNull());
    expect(useUiStore.getState().openSpan).toBeNull();
  });

  it("saving the tracked span with an end clears it", async () => {
    const user = userEvent.setup();
    const sid = trackEvent1();
    useUiStore.getState().actions.openEventEditor({ kind: "edit", sessionId: sid, eventId: 1 });
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(okJson(record({ id: 1, end_timestamp: "2026-07-01T09:20:00Z" }), 200)),
    );
    render(<EventEditor />);
    await user.click(screen.getByTestId("editor-save"));
    await waitFor(() => expect(useUiStore.getState().eventEditor).toBeNull());
    expect(useUiStore.getState().openSpan).toBeNull();
  });
});
