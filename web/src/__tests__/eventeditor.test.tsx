// web/src/__tests__/eventeditor.test.tsx
// Fetch-mocked against the real stores, same idiom as markcontrol.test.tsx/
// eventapi.test.ts: the store IS the assertion surface for eventEditor
// open/close, and the panel's own testids are the assertion surface for
// everything else. Renders <EventEditor /> directly (not the whole <App />)
// -- opening/closing is driven straight through uiStore's actions, exactly
// how MarkControl/EventsPanel trigger it in production.
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { setInteractionModality } from "react-aria";
import { afterEach, beforeEach, describe, expect, it, onTestFinished, vi } from "vitest";
import kitchenSink from "../../fixtures/kitchen-sink.json" with { type: "json" };
import { createEvent, updateEvent } from "../data/eventApi";
import { installOpenSpanReconciler } from "../data/openSpan";
import { useReviewStore } from "../data/reviewStore";
import { undo } from "../data/undoRedo";
import { EventEditor } from "../shell/EventEditor";
import { editorGateKey, useMutationGates } from "../shell/useMutationGate";
import { calendarDateTimeToMs, msToCalendarDateTime } from "../ui/calendarTime";
import { type EventDraft, useUiStore } from "../ui/uiStore";
import { installPlotSurfaceCss } from "./_plotSurfaceCss";

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
    // event id 1 in kitchen-sink.json: "config reload", 08:20, #7c5cff (the
    // Violet preset -- picked so the selection assertion below has a real
    // swatch option to check).
    useUiStore.getState().actions.openEventEditor({ kind: "edit", sessionId: sid, eventId: 1 });
    render(<EventEditor />);
    expect((screen.getByTestId("editor-label") as HTMLInputElement).value).toBe("config reload");
    expect(screen.getByTestId("editor-color-annotation").getAttribute("aria-selected")).toBe(
      "true",
    );
    expect(screen.getByTestId("editor-color-neutral").getAttribute("aria-selected")).toBe("false");
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

  it("Save on an edit target sends a PATCH of the edited field", async () => {
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
    // Only the label was edited: the untouched dates and end are not sent.
    expect(jsonBody(fetchMock)).toEqual({ label: "config reload v2" });
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
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ id: 2, timestamp: "2026-07-18T12:00:00+00:00" }), {
        status: 200,
      }),
    );
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
    expect(dialog.textContent).toContain("You can undo this with Ctrl Z.");
    expect(dialog.textContent).not.toContain("can't be undone");
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
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ id: 2, timestamp: "2026-07-18T12:00:00+00:00" }), {
        status: 200,
      }),
    );
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

  describe("colour (#858)", () => {
    // Event id 1 re-imported with *color* as its stored colour (and any
    // other id with its colour in *others*): the import boundary normalises
    // it (a non-hex becomes the default plus legacyColor).
    function hydrateEvent1(color: string, others: Record<number, string> = {}): string {
      const doc = JSON.parse(JSON.stringify(kitchenSink)) as {
        sessions: { events: { id: number; color?: string }[] }[];
      };
      for (const [id, c] of Object.entries({ ...others, 1: color })) {
        const ev = doc.sessions[0]?.events.find((e) => e.id === Number(id));
        if (!ev) throw new Error(`fixture has no event ${id}`);
        ev.color = c;
      }
      useReviewStore.getState().actions.importMonitorSessions(JSON.stringify(doc), "test");
      useReviewStore.getState().actions.setEditable(true);
      const id = useReviewStore.getState().sessions[0]?.id;
      if (!id) throw new Error("fixture has no session");
      return id;
    }

    // Opens event 1, appends to its label, runs *pick*, saves: the PATCH body.
    async function editAndSave(
      sid: string,
      pick?: () => Promise<void> | void,
    ): Promise<Record<string, unknown>> {
      const user = userEvent.setup();
      useUiStore.getState().actions.openEventEditor({ kind: "edit", sessionId: sid, eventId: 1 });
      const fetchMock = vi.fn().mockResolvedValue(okJson(record({ id: 1 }), 200));
      vi.stubGlobal("fetch", fetchMock);
      render(<EventEditor />);
      await user.type(screen.getByTestId("editor-label"), " v2");
      if (pick) await pick();
      await user.click(screen.getByTestId("editor-save"));
      await waitFor(() => expect(fetchMock).toHaveBeenCalled());
      return jsonBody(fetchMock);
    }

    it("an existing #00abcd event: a label edit's PATCH has no color key", async () => {
      const sid = hydrateEvent1("#00abcd");
      const body = await editAndSave(sid, () => {
        expect(screen.getByTestId("editor-color-custom").getAttribute("aria-selected")).toBe(
          "true",
        );
      });
      expect(body["label"]).toBe("config reload v2");
      expect(body).not.toHaveProperty("color");
    });

    it("picking Green sends color #2ca02c", async () => {
      const user = userEvent.setup();
      const sid = hydrate();
      const body = await editAndSave(sid, () =>
        user.click(screen.getByRole("option", { name: "Green" })),
      );
      expect(body).toMatchObject({ color: "#2ca02c" });
    });

    it("label-only edit of a legacy-colour event sends no color", async () => {
      const sid = hydrateEvent1("red");
      const body = await editAndSave(sid, () => {
        expect(screen.getByTestId("editor-color-legacy").textContent).toContain("“red”");
        expect(screen.getByTestId("editor-color-name").textContent).toBe("· Grey");
      });
      expect(body).not.toHaveProperty("color");
    });

    it("a label-only edit's PATCH has no dash key; choosing a dash sends it", async () => {
      const user = userEvent.setup();
      const sid = hydrate();
      expect(await editAndSave(sid)).not.toHaveProperty("dash");
      cleanup();
      const body = await editAndSave(sid, async () => {
        await user.click(within(screen.getByTestId("editor-dash")).getByRole("button"));
        await user.click(
          within(screen.getByRole("listbox")).getByRole("option", { name: "longdash" }),
        );
      });
      expect(body).toMatchObject({ dash: "longdash" });
      expect(body).not.toHaveProperty("color");
    });

    it("a legacy dash shows as custom; choosing the default dash sends it", async () => {
      const user = userEvent.setup();
      const doc = JSON.parse(JSON.stringify(kitchenSink)) as {
        sessions: { events: { id: number; dash?: string }[] }[];
      };
      const ev1 = doc.sessions[0]?.events.find((e) => e.id === 1);
      if (!ev1) throw new Error("fixture has no event 1");
      ev1.dash = "wavy";
      useReviewStore.getState().actions.importMonitorSessions(JSON.stringify(doc), "test");
      useReviewStore.getState().actions.setEditable(true);
      const sid = useReviewStore.getState().sessions[0]?.id ?? "";
      const body = await editAndSave(sid, async () => {
        const trigger = within(screen.getByTestId("editor-dash")).getByRole("button");
        expect(trigger.textContent).toContain("Custom — wavy");
        await user.click(trigger);
        await user.click(within(screen.getByRole("listbox")).getByRole("option", { name: "dash" }));
      });
      expect(body).toMatchObject({ dash: "dash" });
    });

    it("legacy red → Use Grey → Save sends #888888", async () => {
      const user = userEvent.setup();
      const sid = hydrateEvent1("red");
      const body = await editAndSave(sid, async () => {
        await user.click(screen.getByTestId("editor-color-legacy-repair"));
        // The repair is made: the note goes.
        expect(screen.queryByTestId("editor-color-legacy")).toBeNull();
      });
      expect(body).toMatchObject({ color: "#888888" });
    });

    it("retargeting without closing drops the previous event's custom slot", () => {
      const sid = hydrateEvent1("#00abcd", { 2: "#888888" });
      useUiStore.getState().actions.openEventEditor({ kind: "edit", sessionId: sid, eventId: 1 });
      vi.stubGlobal("fetch", vi.fn());
      const { rerender } = render(<EventEditor />);
      expect(screen.getByTestId("editor-color-custom")).toBeTruthy();
      act(() => {
        useUiStore.getState().actions.openEventEditor({ kind: "edit", sessionId: sid, eventId: 2 });
      });
      rerender(<EventEditor />);
      expect(screen.getByTestId("editor-color-name").textContent).toBe("· Grey");
      expect(screen.queryByTestId("editor-color-custom")).toBeNull();
    });

    it("Escape closes only the Custom… popover, and focus returns to Custom…", async () => {
      installPlotSurfaceCss();
      const user = userEvent.setup();
      const sid = hydrate();
      useUiStore.getState().actions.openEventEditor({ kind: "draft", draft: blankDraft(sid) });
      vi.stubGlobal("fetch", vi.fn());
      render(<EventEditor />);
      const open = screen.getByTestId("editor-color-custom-open");
      await user.click(open);
      expect(screen.getByTestId("custom-color-dialog")).toBeTruthy();
      await user.keyboard("{Escape}");
      expect(screen.queryByTestId("custom-color-dialog")).toBeNull();
      expect(screen.getByTestId("event-editor")).toBeTruthy();
      expect(useUiStore.getState().eventEditor).not.toBeNull();
      // react-aria restores focus on a later frame, not synchronously.
      await waitFor(() =>
        expect(document.activeElement).toBe(screen.getByTestId("editor-color-custom-open")),
      );
    });

    // #957: react-aria's useDialog focuses a Dialog that mounts with nothing
    // focused inside it and arms a 500 ms timer that re-focuses the Dialog
    // when focus is on it or on <body>. A nested popover closed by Escape
    // leaves focus on <body> until FocusScope's restore frame, which restores
    // only from <body>: a timer firing in between took focus for good. Fake
    // timers drive the 500 ms and a held requestAnimationFrame holds the
    // restore, so the timer fires inside that window every time. Each case
    // runs per OPENING modality: under virtual (screen-reader) modality
    // react-aria's own autoFocus waits a frame, which armed the timer.
    describe.each(["keyboard", "virtual"] as const)(
      "the editor opened under %s modality focuses its first field (#957)",
      (modality) => {
        // Held frames: monotonic ids, and cancelAnimationFrame really cancels,
        // so a callback react-aria cancels on unmount never runs.
        let frames = new Map<number, FrameRequestCallback>();
        let nextFrame = 1;
        function flushFrames(): void {
          act(() => {
            while (frames.size > 0) {
              const batch = [...frames];
              frames = new Map();
              for (const [, cb] of batch) cb(0);
            }
          });
        }

        beforeEach(() => {
          frames = new Map();
          vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
          vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => {
            const id = nextFrame++;
            frames.set(id, cb);
            return id;
          });
          vi.stubGlobal("cancelAnimationFrame", (id: number) => {
            frames.delete(id);
          });
          setInteractionModality(modality);
        });

        afterEach(() => {
          vi.useRealTimers();
        });

        function openDraft(): void {
          installPlotSurfaceCss();
          const sid = hydrate();
          useUiStore.getState().actions.openEventEditor({ kind: "draft", draft: blankDraft(sid) });
          vi.stubGlobal("fetch", vi.fn());
          render(<EventEditor />);
        }

        // fireEvent, not userEvent: RTL's async wrapper waits on a real
        // setTimeout(0), which these fake timers never fire.
        function press(key: string): void {
          const target = document.activeElement as HTMLElement;
          fireEvent.keyDown(target, { key });
          fireEvent.keyUp(target, { key });
        }

        it("on open, focus is in the Label field and no refocus timer is armed", () => {
          openDraft();
          expect(document.activeElement).toBe(screen.getByTestId("editor-label"));
          // An armed timer would pull focus lost to <body> back to the Dialog,
          // synchronously once the user is on the keyboard. The frames stay
          // held: the editor's contained FocusScope would refocus the field.
          act(() => setInteractionModality("keyboard"));
          act(() => (document.activeElement as HTMLElement).blur());
          act(() => vi.advanceTimersByTime(500));
          expect(document.activeElement).toBe(document.body);
        });

        it("Escape on Custom… restores focus to it even when 500 ms pass before the restore frame", () => {
          openDraft();
          const open = screen.getByTestId("editor-color-custom-open");
          act(() => open.focus());
          press("Enter");
          flushFrames();
          expect(document.activeElement).toBe(screen.getByTestId("custom-color-hex"));
          press("Escape");
          expect(screen.queryByTestId("custom-color-dialog")).toBeNull();
          expect(document.activeElement).toBe(document.body);
          act(() => vi.advanceTimersByTime(500));
          flushFrames();
          expect(document.activeElement).toBe(screen.getByTestId("editor-color-custom-open"));
          expect(useUiStore.getState().eventEditor).not.toBeNull();
        });

        // Save → Cancel → reopen while the Save is still pending: the Label
        // is disabled, so the open focus falls back to an enabled control.
        // Without the fallback the Dialog armed its timer at the reopen, and
        // Custom… opened and closed once the Save settled lost its restore.
        it("reopened during a pending Save, Escape on Custom… still restores focus to it", async () => {
          installPlotSurfaceCss();
          const sid = hydrate();
          let release!: (r: Response) => void;
          const pending = new Promise<Response>((res) => {
            release = res;
          });
          vi.stubGlobal("fetch", vi.fn().mockReturnValueOnce(pending));
          // The editor's opener, where its close restores focus (the Events
          // panel's Edit button in the app).
          const opener = document.createElement("button");
          document.body.append(opener);
          onTestFinished(() => opener.remove());
          act(() => opener.focus());
          const openEvent2 = () =>
            act(() =>
              useUiStore
                .getState()
                .actions.openEventEditor({ kind: "edit", sessionId: sid, eventId: 2 }),
            );
          openEvent2();
          render(<EventEditor />);
          fireEvent.change(screen.getByTestId("editor-label"), { target: { value: "edited" } });
          act(() => screen.getByTestId("editor-save").focus());
          press("Enter");
          act(() => screen.getByTestId("editor-cancel").focus());
          press("Enter");
          expect(useUiStore.getState().eventEditor).toBeNull();
          // The closed editor's own frames run: its restore returns focus to
          // the opener, as in the app.
          flushFrames();
          expect(document.activeElement).toBe(opener);
          act(() => setInteractionModality(modality));
          openEvent2();
          expect(screen.getByTestId("editor-label").hasAttribute("disabled")).toBe(true);
          expect(screen.getByTestId("event-editor").contains(document.activeElement)).toBe(true);
          await act(async () => {
            release(okJson(record({ id: 2, label: "stress run" }), 200));
            await pending;
          });
          // The settled Save gives the fields back.
          for (let i = 0; i < 5; i++) await act(async () => {});
          const open = screen.getByTestId("editor-color-custom-open");
          expect(open.hasAttribute("disabled")).toBe(false);
          act(() => open.focus());
          press("Enter");
          flushFrames();
          expect(document.activeElement).toBe(screen.getByTestId("custom-color-hex"));
          press("Escape");
          expect(document.activeElement).toBe(document.body);
          act(() => vi.advanceTimersByTime(500));
          flushFrames();
          expect(document.activeElement).toBe(screen.getByTestId("editor-color-custom-open"));
        });
      },
    );

    it("a held Save disables Custom…, and Use colour in a popover already open", async () => {
      installPlotSurfaceCss();
      const user = userEvent.setup();
      const sid = hydrate();
      useUiStore.getState().actions.openEventEditor({ kind: "draft", draft: blankDraft(sid) });
      vi.stubGlobal("fetch", vi.fn());
      render(<EventEditor />);
      await user.click(screen.getByTestId("editor-color-custom-open"));
      expect(screen.getByTestId("custom-color-apply").hasAttribute("disabled")).toBe(false);
      // The popover covers Save, so a request starts here the way a held
      // one looks to the editor: ANOTHER event's gate goes pending, and
      // the one-mutation-at-a-time gate shuts this editor all the same.
      const key = editorGateKey(sid, 99);
      act(() => {
        useMutationGates.setState({ pending: { [key]: { action: "save", run: Symbol(key) } } });
      });
      try {
        expect(screen.getByTestId("custom-color-apply").hasAttribute("disabled")).toBe(true);
        await user.keyboard("{Escape}");
        expect(screen.getByTestId("editor-color-custom-open").hasAttribute("disabled")).toBe(true);
      } finally {
        act(() => useMutationGates.setState({ pending: {} }));
      }
      expect(screen.getByTestId("editor-color-custom-open").hasAttribute("disabled")).toBe(false);
    });

    it("Use colour updates the draft only; Save sends it", async () => {
      installPlotSurfaceCss();
      const user = userEvent.setup();
      const sid = hydrate();
      useUiStore.getState().actions.openEventEditor({ kind: "draft", draft: blankDraft(sid) });
      const fetchMock = vi.fn().mockResolvedValue(okJson(record(), 201));
      vi.stubGlobal("fetch", fetchMock);
      render(<EventEditor />);
      await user.click(screen.getByTestId("editor-color-custom-open"));
      const hex = screen.getByTestId("custom-color-hex");
      await user.clear(hex);
      await user.type(hex, "#00abcd");
      await user.click(screen.getByTestId("custom-color-apply"));
      expect(fetchMock).not.toHaveBeenCalled();
      expect(screen.getByTestId("editor-color-name").textContent).toBe("· Custom — #00abcd");
      await user.click(screen.getByTestId("editor-save"));
      await waitFor(() => expect(fetchMock).toHaveBeenCalled());
      expect(jsonBody(fetchMock)).toMatchObject({ color: "#00abcd" });
    });

    it("a draft always sends its colour", async () => {
      const user = userEvent.setup();
      const sid = hydrate();
      useUiStore.getState().actions.openEventEditor({ kind: "draft", draft: blankDraft(sid) });
      const fetchMock = vi.fn().mockResolvedValue(okJson(record(), 201));
      vi.stubGlobal("fetch", fetchMock);
      render(<EventEditor />);
      await user.click(screen.getByTestId("editor-save"));
      await waitFor(() => expect(fetchMock).toHaveBeenCalled());
      expect(jsonBody(fetchMock)).toMatchObject({ color: "#888888" });
    });
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
      await user.type(screen.getByTestId("editor-label"), "!");
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
    await act(() =>
      Promise.resolve(
        release(
          new Response(JSON.stringify({ id: 2, timestamp: "2026-07-18T12:00:00+00:00" }), {
            status: 200,
          }),
        ),
      ),
    );
    await waitFor(() => expect(useUiStore.getState().eventEditor).toBeNull());
    expect(fetchMock.mock.calls.length).toBe(1);
  });

  it("a held Save spins Save alone; the editor's Delete is disabled, not spinning", async () => {
    const user = userEvent.setup();
    const sid = hydrate();
    useUiStore.getState().actions.openEventEditor({ kind: "edit", sessionId: sid, eventId: 2 });
    vi.stubGlobal("fetch", held().fetchMock);
    render(<EventEditor />);
    await user.type(screen.getByTestId("editor-label"), "!");
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

  it("a held Save disables every draft field; a failed Save gives them back intact", async () => {
    const user = userEvent.setup();
    const sid = hydrate();
    useUiStore.getState().actions.openEventEditor({ kind: "edit", sessionId: sid, eventId: 1 });
    const { fetchMock, release } = held();
    vi.stubGlobal("fetch", fetchMock);
    render(<EventEditor />);
    await user.type(screen.getByTestId("editor-label"), " v2");
    await user.click(screen.getByTestId("editor-save"));
    const green = screen.getByRole("option", { name: "Green" });
    const dashButton = within(screen.getByTestId("editor-dash")).getByRole("button");
    expect(green.getAttribute("aria-disabled")).toBe("true");
    expect(dashButton.hasAttribute("disabled")).toBe(true);
    expect(screen.getByTestId("editor-label").hasAttribute("disabled")).toBe(true);
    for (const field of ["editor-start", "editor-end"]) {
      for (const el of editableSegments(field)) {
        expect(el.getAttribute("aria-disabled")).toBe("true");
      }
    }
    // A press on a disabled swatch changes nothing.
    await user.click(green);
    expect(green.getAttribute("aria-selected")).toBe("false");
    await act(() => Promise.resolve(release(okJson({ error: "archive is locked" }, 409))));
    await waitFor(() => expect(screen.getByTestId("editor-error").textContent).toBeTruthy());
    expect((screen.getByTestId("editor-label") as HTMLInputElement).value).toBe("config reload v2");
    expect(green.getAttribute("aria-disabled")).toBeNull();
    expect(dashButton.hasAttribute("disabled")).toBe(false);
    await user.click(green);
    fetchMock.mockResolvedValue(okJson(record({ id: 1 }), 200));
    await user.click(screen.getByTestId("editor-save"));
    await waitFor(() => expect(useUiStore.getState().eventEditor).toBeNull());
    expect(jsonBody(fetchMock, 1)).toMatchObject({ label: "config reload v2", color: "#2ca02c" });
  });

  it("a Save pending for one event disables another event's editor until it settles, and only the pressing button spins", async () => {
    const user = userEvent.setup();
    const sid = hydrate();
    useUiStore.getState().actions.openEventEditor({ kind: "edit", sessionId: sid, eventId: 2 });
    const { fetchMock, release } = held();
    vi.stubGlobal("fetch", fetchMock);
    render(<EventEditor />);
    await user.type(screen.getByTestId("editor-label"), "!");
    await user.click(screen.getByTestId("editor-save"));
    expect(screen.getByTestId("editor-save").hasAttribute("data-loading")).toBe(true);
    act(() => {
      useUiStore.getState().actions.openEventEditor({ kind: "edit", sessionId: sid, eventId: 3 });
    });
    await waitFor(() =>
      expect(screen.getByTestId("editor-save").hasAttribute("disabled")).toBe(true),
    );
    expect(screen.getByTestId("editor-save").hasAttribute("data-loading")).toBe(false);
    expect(screen.getByTestId("editor-delete").hasAttribute("disabled")).toBe(true);
    await act(() => Promise.resolve(release(okJson(record({ id: 2 }), 200))));
    await waitFor(() =>
      expect(screen.getByTestId("editor-save").hasAttribute("disabled")).toBe(false),
    );
  });
});

describe("EventEditor and a late result (#932)", () => {
  function held() {
    let release!: (r: Response) => void;
    const promise = new Promise<Response>((res) => {
      release = res;
    });
    return { fetchMock: vi.fn().mockReturnValueOnce(promise), release };
  }

  function storedLabel(sid: string, id: number): string | undefined {
    return useReviewStore
      .getState()
      .sessions.find((s) => s.id === sid)
      ?.events.find((ev) => ev.id === id)?.label;
  }

  function open(sid: string, eventId: number): void {
    act(() => {
      useUiStore.getState().actions.openEventEditor({ kind: "edit", sessionId: sid, eventId });
    });
  }

  it("event 2's late Save leaves event 3's editor and its custom colour; the store takes event 2's update", async () => {
    installPlotSurfaceCss();
    const user = userEvent.setup();
    const sid = hydrate();
    open(sid, 2);
    const { fetchMock, release } = held();
    vi.stubGlobal("fetch", fetchMock);
    render(<EventEditor />);
    await user.type(screen.getByTestId("editor-label"), "!");
    await user.click(screen.getByTestId("editor-save"));
    open(sid, 3);
    // Event 3's editor is shut while event 2's Save is pending, so its custom
    // colour can only be set once the late result has landed.
    expect(screen.getByTestId("editor-color-custom-open").hasAttribute("disabled")).toBe(true);
    await act(() => Promise.resolve(release(okJson(record({ id: 2, label: "saved" }), 200))));
    await waitFor(() => expect(storedLabel(sid, 2)).toBe("saved"));
    expect(useUiStore.getState().eventEditor).toMatchObject({ eventId: 3 });
    expect((screen.getByTestId("editor-label") as HTMLInputElement).value).toBe("log capture");
    await user.click(screen.getByTestId("editor-color-custom-open"));
    const hex = screen.getByTestId("custom-color-hex");
    await user.clear(hex);
    await user.type(hex, "#00abcd");
    await user.click(screen.getByTestId("custom-color-apply"));
    expect(screen.getByTestId("editor-color-name").textContent).toBe("· Custom — #00abcd");
    expect(useUiStore.getState().eventEditor).toMatchObject({ eventId: 3 });
  });

  it.each([
    { outcome: "success", response: () => okJson(record({ id: 2, label: "saved" }), 200) },
    { outcome: "failure", response: () => okJson({ error: "archive is locked" }, 409) },
  ])(
    "a Save's late $outcome after Cancel and reopening the same event leaves the reopened editor",
    async ({ response }) => {
      const user = userEvent.setup();
      const sid = hydrate();
      open(sid, 2);
      const { fetchMock, release } = held();
      vi.stubGlobal("fetch", fetchMock);
      render(<EventEditor />);
      await user.type(screen.getByTestId("editor-label"), "!");
      await user.click(screen.getByTestId("editor-save"));
      await user.click(screen.getByTestId("editor-cancel"));
      expect(useUiStore.getState().eventEditor).toBeNull();
      open(sid, 2);
      await act(() => Promise.resolve(release(response())));
      await waitFor(() =>
        expect(screen.getByTestId("editor-save").hasAttribute("data-loading")).toBe(false),
      );
      expect(useUiStore.getState().eventEditor).toMatchObject({ eventId: 2 });
      expect(screen.getByTestId("event-editor")).toBeTruthy();
      expect(screen.queryByTestId("editor-error")).toBeNull();
    },
  );

  it("a Save's late success reseeds the reopened editor, and its next Save sends the landed values", async () => {
    const user = userEvent.setup();
    const sid = hydrate();
    open(sid, 2);
    const { fetchMock, release } = held();
    vi.stubGlobal("fetch", fetchMock);
    render(<EventEditor />);
    const label = screen.getByTestId("editor-label") as HTMLInputElement;
    await user.clear(label);
    await user.type(label, "saved");
    await user.click(screen.getByTestId("editor-save"));
    await user.click(screen.getByTestId("editor-cancel"));
    open(sid, 2);
    // Seeded before the update landed: the stored label.
    expect((screen.getByTestId("editor-label") as HTMLInputElement).value).toBe("stress run");
    await act(() =>
      Promise.resolve(release(okJson(record({ id: 2, label: "saved", end_timestamp: null }), 200))),
    );
    await waitFor(() =>
      expect((screen.getByTestId("editor-label") as HTMLInputElement).value).toBe("saved"),
    );
    await user.click(screen.getByTestId("editor-save"));
    await waitFor(() => expect(useUiStore.getState().eventEditor).toBeNull());
    // The reseed made "saved" the stored label and reset the edit marks, so
    // this Save changes nothing: it closes without a second request.
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("a Delete's late success after Cancel and reopening the same event closes the editor", async () => {
    const user = userEvent.setup();
    const sid = hydrate();
    open(sid, 2);
    const { fetchMock, release } = held();
    vi.stubGlobal("fetch", fetchMock);
    render(<EventEditor />);
    await user.click(screen.getByTestId("editor-delete"));
    await user.click(screen.getByTestId("confirm-delete-confirm"));
    act(() => useUiStore.getState().actions.closeEventEditor());
    open(sid, 2);
    expect(screen.getByTestId("event-editor")).toBeTruthy();
    await act(() =>
      Promise.resolve(
        release(
          new Response(JSON.stringify({ id: 2, timestamp: "2026-07-18T12:00:00+00:00" }), {
            status: 200,
          }),
        ),
      ),
    );
    await waitFor(() => expect(useUiStore.getState().eventEditor).toBeNull());
    expect(storedLabel(sid, 2)).toBeUndefined();
    expect(screen.queryByTestId("event-editor")).toBeNull();
  });

  it("a Delete's late failure after Cancel and reopening the same event shows no error there", async () => {
    const user = userEvent.setup();
    const sid = hydrate();
    open(sid, 2);
    const { fetchMock, release } = held();
    vi.stubGlobal("fetch", fetchMock);
    render(<EventEditor />);
    await user.click(screen.getByTestId("editor-delete"));
    await user.click(screen.getByTestId("confirm-delete-confirm"));
    act(() => useUiStore.getState().actions.closeEventEditor());
    open(sid, 2);
    await act(() => Promise.resolve(release(okJson({ error: "archive is locked" }, 409))));
    await waitFor(() =>
      expect(screen.getByTestId("editor-delete").hasAttribute("disabled")).toBe(false),
    );
    expect(useUiStore.getState().eventEditor).toMatchObject({ eventId: 2 });
    expect(screen.queryByTestId("editor-error")).toBeNull();
    // The dialog, reopened, carries no error from the old request.
    await user.click(screen.getByTestId("editor-delete"));
    expect(within(screen.getByTestId("confirm-delete")).queryByRole("alert")).toBeNull();
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
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ id: 2, timestamp: "2026-07-18T12:00:00+00:00" }), {
          status: 200,
        }),
      ),
    );
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
    await user.type(screen.getByTestId("editor-label"), "!");
    await user.click(screen.getByTestId("editor-save"));
    await waitFor(() => expect(useUiStore.getState().eventEditor).toBeNull());
    expect(useUiStore.getState().openSpan).toBeNull();
  });
});

// #929, spec §3: an undo is a mutation like any other, so an editor opened on
// its target while it is pending inherits it (#932): it reseeds from the
// store when the undo settles, or closes if the undo deleted the event.
describe("EventEditor and a pending undo (#929)", () => {
  function heldRevert() {
    let release!: (r: Response) => void;
    const promise = new Promise<Response>((res) => {
      release = res;
    });
    return { promise, release };
  }

  const label = () => (screen.getByTestId("editor-label") as HTMLInputElement).value;

  it("an editor opened on the target while an undo is pending reseeds from the store when it settles", async () => {
    const sid = hydrate();
    const fetchMock = vi.fn().mockResolvedValueOnce(okJson(record({ id: 2, label: "renamed" })));
    vi.stubGlobal("fetch", fetchMock);
    await updateEvent(sid, 2, { label: "renamed" });
    const { promise, release } = heldRevert();
    fetchMock.mockReturnValueOnce(promise);
    let undoing!: Promise<void>;
    act(() => {
      undoing = undo();
    });
    act(() => {
      useUiStore.getState().actions.openEventEditor({ kind: "edit", sessionId: sid, eventId: 2 });
    });
    render(<EventEditor />);
    expect(label()).toBe("renamed");
    expect(screen.getByTestId("editor-label").hasAttribute("disabled")).toBe(true);
    await act(async () => {
      release(okJson(record({ id: 2, label: "stress run" })));
      await undoing;
    });
    await waitFor(() => expect(label()).toBe("stress run"));
    expect(screen.getByTestId("editor-label").hasAttribute("disabled")).toBe(false);
  });

  it("an editor opened on the target while an undo is pending closes if the undo deleted the event", async () => {
    const sid = hydrate();
    const fetchMock = vi.fn().mockResolvedValueOnce(okJson(record({ id: 101 }), 201));
    vi.stubGlobal("fetch", fetchMock);
    await createEvent(sid, { label: "deploy" });
    const { promise, release } = heldRevert();
    fetchMock.mockReturnValueOnce(promise);
    let undoing!: Promise<void>;
    act(() => {
      undoing = undo();
    });
    act(() => {
      useUiStore.getState().actions.openEventEditor({ kind: "edit", sessionId: sid, eventId: 101 });
    });
    render(<EventEditor />);
    expect(screen.getByTestId("event-editor")).toBeTruthy();
    await act(async () => {
      release(okJson(null));
      await undoing;
    });
    await waitFor(() => expect(useUiStore.getState().eventEditor).toBeNull());
    expect(screen.queryByTestId("event-editor")).toBeNull();
  });
});

describe("EventEditor sends only the fields the user changed (#993)", () => {
  // Event 2 re-imported with *patch* applied (a start/end of day 5 so one
  // Backspace clears a day segment), opened for edit; returns the fetch mock.
  function openEdit(patch: Record<string, unknown> = {}): ReturnType<typeof vi.fn> {
    const doc = JSON.parse(JSON.stringify(kitchenSink)) as {
      sessions: { events: Record<string, unknown>[] }[];
    };
    const ev = doc.sessions[0]?.events.find((e) => e["id"] === 2);
    if (!ev) throw new Error("fixture has no event 2");
    Object.assign(ev, {
      timestamp: "2026-07-05T12:00:00Z",
      end_timestamp: "2026-07-05T13:00:00Z",
      ...patch,
    });
    for (const [k, v] of Object.entries(patch)) if (v === null) delete ev[k];
    useReviewStore.getState().actions.importMonitorSessions(JSON.stringify(doc), "test");
    useReviewStore.getState().actions.setEditable(true);
    const sid = useReviewStore.getState().sessions[0]?.id;
    if (!sid) throw new Error("fixture has no session");
    useUiStore.getState().actions.openEventEditor({ kind: "edit", sessionId: sid, eventId: 2 });
    const fetchMock = vi.fn().mockResolvedValue(okJson(record({ id: 2 }), 200));
    vi.stubGlobal("fetch", fetchMock);
    render(<EventEditor />);
    return fetchMock;
  }

  async function pickGreen(user: User): Promise<void> {
    await user.click(screen.getByRole("option", { name: "Green" }));
  }

  async function save(user: User, fetchMock: ReturnType<typeof vi.fn>): Promise<void> {
    await user.click(screen.getByTestId("editor-save"));
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
  }

  it("an edit that changes only the colour sends only color", async () => {
    const user = userEvent.setup();
    const fetchMock = openEdit();
    await pickGreen(user);
    await save(user, fetchMock);
    expect(jsonBody(fetchMock)).toEqual({ color: "#2ca02c" });
  });

  it("an edit that changes only the label sends only label", async () => {
    const user = userEvent.setup();
    const fetchMock = openEdit();
    await user.type(screen.getByTestId("editor-label"), "!");
    await save(user, fetchMock);
    expect(jsonBody(fetchMock)).toEqual({ label: "stress run!" });
  });

  it("clearing every End segment sends end_timestamp null", async () => {
    const user = userEvent.setup();
    const fetchMock = openEdit();
    await clearField(user, "editor-end");
    await waitFor(() => expect(saveDisabled()).toBe(false));
    await save(user, fetchMock);
    expect(jsonBody(fetchMock)).toEqual({ end_timestamp: null });
  });

  it("clicking the Clear-End button sends end_timestamp null", async () => {
    const user = userEvent.setup();
    const fetchMock = openEdit();
    await user.click(screen.getByTestId("editor-end-clear"));
    await save(user, fetchMock);
    expect(jsonBody(fetchMock)).toEqual({ end_timestamp: null });
  });

  it("a legacy blank-label event can be recoloured", async () => {
    const user = userEvent.setup();
    const fetchMock = openEdit({ label: "" });
    expect(saveDisabled()).toBe(false);
    await pickGreen(user);
    await save(user, fetchMock);
    expect(jsonBody(fetchMock)).toEqual({ color: "#2ca02c" });
  });

  it("Save on an edit that changed nothing closes without a request", async () => {
    const user = userEvent.setup();
    const fetchMock = openEdit();
    await user.click(screen.getByTestId("editor-save"));
    await waitFor(() => expect(useUiStore.getState().eventEditor).toBeNull());
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("clearing a label blocks Save", async () => {
    const user = userEvent.setup();
    openEdit();
    await user.clear(screen.getByTestId("editor-label"));
    expect(saveDisabled()).toBe(true);
  });

  it("an untouched blank draft cannot be saved", () => {
    const sid = hydrate();
    useUiStore
      .getState()
      .actions.openEventEditor({ kind: "draft", draft: blankDraft(sid, { label: "" }) });
    render(<EventEditor />);
    expect(saveDisabled()).toBe(true);
  });

  it("a partially cleared start blocks Save on an edit", async () => {
    const user = userEvent.setup();
    const fetchMock = openEdit();
    await user.click(seg("editor-start", "day") as HTMLElement);
    await user.keyboard("{Backspace}");
    await waitFor(() => expect(saveDisabled()).toBe(true));
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("a partial end blocks Save on an edit", async () => {
    const user = userEvent.setup();
    const fetchMock = openEdit();
    await user.click(seg("editor-end", "day") as HTMLElement);
    await user.keyboard("{Backspace}");
    await waitFor(() => expect(saveDisabled()).toBe(true));
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("a point event can be recoloured", async () => {
    const user = userEvent.setup();
    const fetchMock = openEdit({ end_timestamp: null });
    expect(saveDisabled()).toBe(false);
    await pickGreen(user);
    await save(user, fetchMock);
    expect(jsonBody(fetchMock)).toEqual({ color: "#2ca02c" });
  });

  it("a colour-only save after the mount effects settle sends only color", async () => {
    const user = userEvent.setup();
    const fetchMock = openEdit();
    // Reopen the same event: both date fields remount and report their
    // completeness afresh (as they do on every mount); only then pick.
    const sid = useReviewStore.getState().sessions[0]?.id ?? "";
    act(() =>
      useUiStore.getState().actions.openEventEditor({ kind: "edit", sessionId: sid, eventId: 2 }),
    );
    await waitFor(() => expect(screen.getByTestId("editor-start")).toBeTruthy());
    expect(saveDisabled()).toBe(false);
    await pickGreen(user);
    await save(user, fetchMock);
    expect(jsonBody(fetchMock)).toEqual({ color: "#2ca02c" });
  });

  it("a partial date edit restored to complete sends the date", async () => {
    const user = userEvent.setup();
    // A point event, so the moved start has no end to fall behind.
    const fetchMock = openEdit({ end_timestamp: null });
    await user.click(seg("editor-start", "day") as HTMLElement);
    await user.keyboard("{Backspace}");
    await waitFor(() => expect(saveDisabled()).toBe(true));
    await user.keyboard("15");
    await waitFor(() => expect(saveDisabled()).toBe(false));
    await save(user, fetchMock);
    const day15 = calendarDateTimeToMs(msToCalendarDateTime(DAY5_MS).set({ day: 15 }));
    expect(jsonBody(fetchMock)).toEqual({ timestamp: new Date(day15).toISOString() });
  });

  it("a legacy backward span can be recoloured", async () => {
    const user = userEvent.setup();
    const fetchMock = openEdit({
      timestamp: "2026-07-05T13:00:00Z",
      end_timestamp: "2026-07-05T12:00:00Z",
    });
    expect(saveDisabled()).toBe(false);
    expect(screen.queryByText("End must be after Start.")).toBeNull();
    await pickGreen(user);
    await save(user, fetchMock);
    expect(jsonBody(fetchMock)).toEqual({ color: "#2ca02c" });
  });

  it("moving a legacy backward span's end keeps Save blocked", async () => {
    const user = userEvent.setup();
    openEdit({
      timestamp: "2026-07-05T13:00:00Z",
      end_timestamp: "2026-07-05T12:00:00Z",
    });
    await user.click(seg("editor-end", "minute") as HTMLElement);
    await user.keyboard("{ArrowUp}");
    await waitFor(() => expect(screen.getByText("End must be after Start.")).toBeTruthy());
    expect(saveDisabled()).toBe(true);
  });

  it("changing a date and back still sends it", async () => {
    const user = userEvent.setup();
    const fetchMock = openEdit();
    await user.click(seg("editor-start", "second") as HTMLElement);
    await user.keyboard("{ArrowUp}{ArrowDown}");
    await save(user, fetchMock);
    expect(jsonBody(fetchMock)).toEqual({ timestamp: "2026-07-05T12:00:00.000Z" });
  });
});
