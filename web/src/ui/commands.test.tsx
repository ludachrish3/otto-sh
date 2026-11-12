// web/src/ui/commands.test.tsx
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { installOpenSpanReconciler } from "../data/openSpan";
import { useReviewStore } from "../data/reviewStore";
import { type HistoryEntry, useUndoHistory } from "../data/undoHistory";
import { redo, undo } from "../data/undoRedo";
import { HISTORY_GATE_KEY, useMutationGates } from "../shell/useMutationGate";
import { type Command, useCommands } from "./commands";
import {
  MARK_NOW_BINDING,
  matchesBindingFor,
  REDO_ALT_BINDING,
  REDO_BINDING,
  UNDO_BINDING,
} from "./shortcuts";
import { useUiStore } from "./uiStore";

// The history's replay is undoRedo.ts's (undohistory.test.ts); here only
// which one a command runs matters.
vi.mock("../data/undoRedo", () => ({
  undo: vi.fn(() => Promise.resolve()),
  redo: vi.fn(() => Promise.resolve()),
}));

// Minimal session shape the registry reads: lab.hosts (id/board/slot),
// elements (id), plus store mode/windowMs. Matches NormalizedSession's
// fields used by OverviewPage (hosts/elements) — see data/reviewStore.
const SESSION = {
  id: "s1",
  label: null,
  note: null,
  startMs: 0,
  endMs: 60_000,
  observedMs: 60_000,
  meta: { interval: 5 },
  lab: { hosts: [{ id: "test1", element: "rack", board: "qemu-x86", slot: 1 }] },
  elements: [{ id: "rack", type: "physical", description: null, hostIds: ["test1"] }],
  elementIds: new Set(["rack"]),
  charts: [],
  events: [],
  logEvents: [],
};

function seedStore(mode: "live" | null): void {
  useReviewStore.setState({
    // biome-ignore lint/suspicious/noExplicitAny: test seeds the minimal shape the registry reads
    sessions: [SESSION as any],
    activeSessionId: "s1",
    rawMonitorSessions: {} as never,
    mode,
    windowMs: 900_000,
    range: null,
  });
}

// The app installs this once at startup; End span leaves the clearing to it.
let uninstallReconciler: () => void;
beforeEach(() => {
  uninstallReconciler = installOpenSpanReconciler();
});

afterEach(() => {
  uninstallReconciler();
  // Unmount before resetting: this hook runs before RTL's auto-cleanup, so a
  // bare setState here would land on still-mounted hooks outside act.
  cleanup();
  useReviewStore.setState({
    sessions: [],
    activeSessionId: null,
    rawMonitorSessions: null,
    mode: null,
    range: null,
    windowMs: 900_000,
    editable: false,
    warnings: [],
    source: null,
  });
  useUiStore.setState({
    paletteOpen: false,
    theme: "light",
    eventEditor: null,
    sweepArmed: false,
    openSpan: null,
    markPopover: null,
  });
  vi.unstubAllGlobals();
  window.location.hash = "";
});

describe("useCommands — review/import mode", () => {
  it("has navigation rows for views, hosts, and elements — all chord-less", () => {
    seedStore(null);
    const { result } = renderHook(() => useCommands());
    const nav = result.current.filter((c) => c.section === "Navigation");
    const ids = nav.map((c) => c.id);
    expect(ids).toContain("nav-topology");
    expect(ids).toContain("nav-hosts");
    expect(ids).toContain("nav-host-test1");
    expect(ids).toContain("nav-element-rack");
    for (const c of nav) expect(c.binding).toBeUndefined();
    const host = nav.find((c) => c.id === "nav-host-test1");
    expect(host?.sublabel).toBe("qemu-x86 · slot 1");
  });

  it("omits live-only rows outside live mode but keeps Export (enabled with data)", () => {
    seedStore(null);
    const { result } = renderHook(() => useCommands());
    const ids = result.current.map((c) => c.id);
    expect(ids).not.toContain("action-pause");
    expect(ids.filter((id) => id.startsWith("window-"))).toEqual([]);
    const exp = result.current.find((c) => c.id === "action-export");
    expect(exp?.enabled).toBe(true);
    expect(exp?.binding).toEqual({ key: "s", mod: true });
  });

  it("labels the theme toggle from the current theme", () => {
    seedStore(null);
    useUiStore.setState({ theme: "light" });
    const { result } = renderHook(() => useCommands());
    expect(result.current.find((c) => c.id === "action-theme")?.label).toBe("Switch to dark mode");
    act(() => {
      useUiStore.setState({ theme: "dark" });
    });
    // The SAME rendered hook must relabel — a second renderHook would mount
    // fresh and stay green even if useCommands memoized over a stale theme
    // (e.g. `theme` dropped from its dependency list).
    expect(result.current.find((c) => c.id === "action-theme")?.label).toBe("Switch to light mode");
  });

  it("binds no keyboard shortcut to the theme toggle (Cmd+L removed)", () => {
    // ⌘L is captured by macOS (focus-address-bar) so the toggle can't be
    // triggered by keyboard there; the row stays in the palette/overflow menu
    // as a click-only action, like the chord-less Navigation rows.
    seedStore(null);
    const { result } = renderHook(() => useCommands());
    expect(result.current.find((c) => c.id === "action-theme")?.binding).toBeUndefined();
  });

  it("Import is not offered on live or db sources", () => {
    seedStore(null);
    const { result } = renderHook(() => useCommands());
    expect(result.current.map((c) => c.id)).toContain("action-import");
    for (const kind of ["live", "db"] as const) {
      act(() => {
        useReviewStore.setState({
          source: {
            mode: kind === "live" ? "live" : "review",
            kind,
            source: kind === "db" ? "a.db" : null,
            token: "A",
            editable: true,
            unsaved: 0,
            revision: 0,
            observed_at: "2026-07-18T12:00:00+00:00",
          },
        });
      });
      expect(result.current.map((c) => c.id)).not.toContain("action-import");
    }
    act(() => {
      useReviewStore.setState({
        source: {
          mode: "review",
          kind: "json",
          source: "a.json",
          token: "A",
          editable: true,
          unsaved: 0,
          revision: 0,
          observed_at: "2026-07-18T12:00:00+00:00",
        },
      });
    });
    expect(result.current.map((c) => c.id)).toContain("action-import");
  });

  it("disables Export with no data loaded", () => {
    useReviewStore.setState({ rawMonitorSessions: null });
    const { result } = renderHook(() => useCommands());
    expect(result.current.find((c) => c.id === "action-export")?.enabled).toBe(false);
  });
});

describe("useCommands — live mode", () => {
  it("adds Pause and check-marks the active window preset", () => {
    seedStore("live");
    const { result } = renderHook(() => useCommands());
    const pause = result.current.find((c) => c.id === "action-pause");
    expect(pause?.binding).toEqual({ key: ".", mod: true });
    const windows = result.current.filter((c) => c.section === "Live window");
    expect(windows.map((c) => c.id)).toEqual(["window-5m", "window-15m", "window-1h"]);
    expect(windows.find((c) => c.id === "window-15m")?.checked).toBe(true);
    expect(windows.find((c) => c.id === "window-5m")?.checked).toBe(false);
  });

  it("running a navigation command changes the hash route", () => {
    seedStore(null);
    const { result } = renderHook(() => useCommands());
    act(() => {
      result.current.find((c) => c.id === "nav-host-test1")?.run();
    });
    expect(window.location.hash).toBe("#/host/test1");
  });
});

const MARKING_IDS = [
  "action-add-event",
  "action-sweep-span",
  "action-mark-now",
  "action-start-span",
  "action-end-span",
];

describe("useCommands — marking rows (Plan 5c)", () => {
  it("live + editable: all five marking rows exist with the stated ids/labels/enabled states", () => {
    seedStore("live");
    useReviewStore.setState({ editable: true });
    const { result } = renderHook(() => useCommands());
    const byId = (id: string) => result.current.find((c) => c.id === id);
    expect(byId("action-add-event")).toMatchObject({ label: "Add event…", enabled: true });
    expect(byId("action-sweep-span")).toMatchObject({
      label: "Sweep span on chart",
      enabled: true,
    });
    expect(byId("action-mark-now")).toMatchObject({
      label: "Mark now…",
      enabled: true,
      binding: MARK_NOW_BINDING,
    });
    expect(byId("action-start-span")).toMatchObject({ label: "Start span…", enabled: true });
    // No open span yet -> End span is listed but disabled.
    expect(byId("action-end-span")).toMatchObject({ label: "End span", enabled: false });
  });

  it("action-end-span is enabled only while openSpan matches the active session", () => {
    seedStore("live");
    useReviewStore.setState({ editable: true });
    useUiStore.setState({ openSpan: { sessionId: "s1", eventId: 3 } });
    const { result } = renderHook(() => useCommands());
    expect(result.current.find((c) => c.id === "action-end-span")?.enabled).toBe(true);

    act(() => {
      useUiStore.setState({ openSpan: { sessionId: "other-session", eventId: 3 } });
    });
    // Same rendered hook, not a remount — see the theme-label test above.
    expect(result.current.find((c) => c.id === "action-end-span")?.enabled).toBe(false);
  });

  it("editable + review mode: only action-add-event/action-sweep-span appear", () => {
    seedStore(null);
    useReviewStore.setState({ editable: true, mode: "review" });
    const { result } = renderHook(() => useCommands());
    const ids = result.current.map((c) => c.id);
    expect(ids).toContain("action-add-event");
    expect(ids).toContain("action-sweep-span");
    expect(ids).not.toContain("action-mark-now");
    expect(ids).not.toContain("action-start-span");
    expect(ids).not.toContain("action-end-span");
  });

  it("not editable: none of the marking rows appear, in any mode", () => {
    seedStore("live");
    useReviewStore.setState({ editable: false });
    const { result } = renderHook(() => useCommands());
    const ids = result.current.map((c) => c.id);
    for (const id of MARKING_IDS) expect(ids).not.toContain(id);
  });

  it("action-add-event opens the event editor with a blank draft anchored to the session", () => {
    seedStore("live");
    useReviewStore.setState({ editable: true });
    const { result } = renderHook(() => useCommands());
    result.current.find((c) => c.id === "action-add-event")?.run();
    expect(useUiStore.getState().eventEditor).toEqual({
      kind: "draft",
      draft: {
        sessionId: "s1",
        timestampMs: 60_000,
        endTimestampMs: null,
        label: "",
        color: "#888888",
        dash: "dash",
      },
    });
  });

  it("action-sweep-span arms the sweep", () => {
    seedStore("live");
    useReviewStore.setState({ editable: true });
    const { result } = renderHook(() => useCommands());
    result.current.find((c) => c.id === "action-sweep-span")?.run();
    expect(useUiStore.getState().sweepArmed).toBe(true);
  });

  it("action-mark-now and action-start-span each open the mark popover in their own mode", () => {
    seedStore("live");
    useReviewStore.setState({ editable: true });
    const { result } = renderHook(() => useCommands());
    result.current.find((c) => c.id === "action-mark-now")?.run();
    expect(useUiStore.getState().markPopover).toBe("mark");
    result.current.find((c) => c.id === "action-start-span")?.run();
    expect(useUiStore.getState().markPopover).toBe("start");
  });

  it("action-end-span ends the open span through the API and clears it on success", async () => {
    seedStore("live");
    useReviewStore.setState({ editable: true });
    useUiStore.setState({ openSpan: { sessionId: "s1", eventId: 3 } });
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            id: 3,
            timestamp: "2026-07-18T12:00:00+00:00",
            label: "span",
            source: "manual",
            color: "#888888",
            dash: "dash",
            end_timestamp: "2026-07-18T12:05:00+00:00",
          }),
          { status: 200 },
        ),
      ),
    );
    const { result } = renderHook(() => useCommands());
    await act(() => Promise.resolve(result.current.find((c) => c.id === "action-end-span")?.run()));
    await waitFor(() => expect(useUiStore.getState().openSpan).toBeNull());
    expect(useReviewStore.getState().warnings).toEqual([]);
  });

  it("action-end-span ignores a repeat while pending, and is disabled meanwhile (#867)", async () => {
    seedStore("live");
    useReviewStore.setState({ editable: true });
    useUiStore.setState({ openSpan: { sessionId: "s1", eventId: 3 } });
    let release!: (r: Response) => void;
    const fetchMock = vi.fn().mockReturnValue(
      new Promise<Response>((res) => {
        release = res;
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const { result } = renderHook(() => useCommands());
    await act(() => {
      const run = result.current.find((c) => c.id === "action-end-span")?.run;
      run?.();
      run?.();
      return Promise.resolve();
    });
    expect(fetchMock.mock.calls.length).toBe(1);
    expect(result.current.find((c) => c.id === "action-end-span")?.enabled).toBe(false);
    await act(() =>
      Promise.resolve(
        release(
          new Response(
            JSON.stringify({
              id: 3,
              timestamp: "2026-07-18T12:00:00+00:00",
              label: "span",
              source: "manual",
              color: "#888888",
              dash: "dash",
              end_timestamp: "2026-07-18T12:05:00+00:00",
            }),
            { status: 200 },
          ),
        ),
      ),
    );
    await waitFor(() => expect(useUiStore.getState().openSpan).toBeNull());
    expect(fetchMock.mock.calls.length).toBe(1);
  });

  it("a failed action-end-span surfaces a warning via addWarning rather than throwing", async () => {
    seedStore("live");
    useReviewStore.setState({ editable: true });
    useUiStore.setState({ openSpan: { sessionId: "s1", eventId: 3 } });
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          new Response(JSON.stringify({ error: "archive is locked" }), { status: 409 }),
        ),
    );
    const { result } = renderHook(() => useCommands());
    await act(() => Promise.resolve(result.current.find((c) => c.id === "action-end-span")?.run()));
    await waitFor(() => expect(useReviewStore.getState().warnings.length).toBe(1));
    expect(useReviewStore.getState().warnings[0]).toContain("End span failed");
    expect(useReviewStore.getState().warnings[0]).toContain("archive is locked");
    // The failure does not clear the still-open span.
    expect(useUiStore.getState().openSpan).toEqual({ sessionId: "s1", eventId: 3 });
  });
});

describe("useCommands — undo and redo (#929)", () => {
  const deleted: HistoryEntry = {
    verb: "delete",
    sessionId: "s1",
    eventId: 3,
    label: "x",
    after: null,
    before: null,
    trackedSpan: false,
  };
  const byId = (cs: Command[], id: string) => cs.find((c) => c.id === id);

  it('the palette lists "Undo delete “x”" and a disabled "Undo" when empty', () => {
    seedStore("live");
    useReviewStore.setState({ editable: true });
    useUndoHistory.getState().actions.record(deleted);
    const { result } = renderHook(() => useCommands());
    expect(byId(result.current, "edit-undo")).toMatchObject({
      label: "Undo delete “x”",
      enabled: true,
      binding: UNDO_BINDING,
      yieldsToEditing: true,
    });
    expect(byId(result.current, "edit-redo")).toMatchObject({
      label: "Redo",
      enabled: false,
      binding: REDO_BINDING,
      extraBindings: [REDO_ALT_BINDING],
      yieldsToEditing: true,
    });
    act(() => useUndoHistory.getState().actions.clear());
    expect(byId(result.current, "edit-undo")).toMatchObject({ label: "Undo", enabled: false });
  });

  it("labels Redo from the redo stack's top and runs undo / redo", () => {
    seedStore(null);
    useReviewStore.setState({ editable: true, mode: "review" });
    useUndoHistory.getState().actions.record(deleted);
    useUndoHistory.getState().actions.shiftUndoToRedo();
    const { result } = renderHook(() => useCommands());
    expect(byId(result.current, "edit-redo")).toMatchObject({
      label: "Redo delete “x”",
      enabled: true,
    });
    byId(result.current, "edit-redo")?.run();
    expect(redo).toHaveBeenCalledOnce();
    byId(result.current, "edit-undo")?.run();
    expect(undo).toHaveBeenCalledOnce();
  });

  it('read-only or a pending mutation with a non-empty stack: a disabled plain "Undo"', () => {
    seedStore("live");
    useReviewStore.setState({ editable: false });
    useUndoHistory.setState({ undo: [deleted], redo: [] });
    const { result } = renderHook(() => useCommands());
    expect(byId(result.current, "edit-undo")).toMatchObject({ label: "Undo", enabled: false });
    act(() => useReviewStore.setState({ editable: true }));
    expect(byId(result.current, "edit-undo")).toMatchObject({
      label: "Undo delete “x”",
      enabled: true,
    });
    act(() =>
      useMutationGates.setState({
        pending: { [HISTORY_GATE_KEY]: { action: "undo", run: Symbol("t") } },
      }),
    );
    // A pending mutation with a non-empty stack: a disabled, plain "Undo".
    expect(byId(result.current, "edit-undo")).toMatchObject({ label: "Undo", enabled: false });
    act(() => useMutationGates.setState({ pending: {} }));
  });

  it("⌘⇧Z redoes on mac", () => {
    seedStore("live");
    useReviewStore.setState({ editable: true });
    useUndoHistory.setState({ undo: [deleted], redo: [deleted] });
    const { result } = renderHook(() => useCommands());
    const e = new KeyboardEvent("keydown", { key: "Z", metaKey: true, shiftKey: true });
    const hit = result.current.filter((c) =>
      [c.binding, ...(c.extraBindings ?? [])].some(
        (b) => b !== undefined && matchesBindingFor(e, b, true),
      ),
    );
    expect(hit.map((c) => c.id)).toEqual(["edit-redo"]);
  });
});
