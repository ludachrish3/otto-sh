// web/src/__tests__/undohistory.test.ts
// The undo history (#929, spec §2): every ordinary mutation records one
// entry in its onSuccess; undo / redo replay it through the revert route,
// one at a time across the tab, and settle by the route's outcome.
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import minimal from "../../fixtures/minimal.json" with { type: "json" };
import type { EventRecord } from "../api/export.gen";
import { createEvent, deleteEvent, endEvent, updateEvent } from "../data/eventApi";
import { installOpenSpanReconciler } from "../data/openSpan";
import { useReviewStore } from "../data/reviewStore";
import { importLocalFile } from "../data/source";
import {
  HISTORY_DEPTH,
  type HistoryEntry,
  truncateLabel,
  useUndoHistory,
} from "../data/undoHistory";
import { redo, undo } from "../data/undoRedo";
import { endOpenSpan, startSpan } from "../shell/marking";
import { editorGateKey, runGate } from "../shell/useMutationGate";
import { useCommands } from "../ui/commands";
import { useToastStore } from "../ui/Toast";
import { useUiStore } from "../ui/uiStore";
import { useGlobalShortcuts } from "../ui/useGlobalShortcuts";

// A seam between revertEvent resolving and undo's continuation: a hook the
// import test sets to land a file there. Every other test leaves it null and
// gets the real revertEvent unchanged.
const seam = vi.hoisted(() => ({ afterRevert: null as (() => Promise<void>) | null }));
vi.mock("../data/eventApi", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../data/eventApi")>();
  return {
    ...actual,
    revertEvent: async (...args: Parameters<typeof actual.revertEvent>) => {
      const result = await actual.revertEvent(...args);
      await seam.afterRevert?.();
      return result;
    },
  };
});

const OBSERVED = "X-Otto-Observed-At";

function hydrate(): string {
  useReviewStore.getState().actions.importMonitorSessions(JSON.stringify(minimal), "test");
  const id = useReviewStore.getState().sessions[0]?.id;
  if (!id) throw new Error("fixture has no session");
  return id;
}

const record = (id: number, label: string, extra: Partial<EventRecord> = {}): EventRecord => ({
  id,
  timestamp: "2026-07-18T12:01:00+00:00",
  label,
  source: "manual",
  color: "#888888",
  dash: "dash",
  ...extra,
});

/** Puts `r` in the store as if the server had already shown it. */
function seed(sid: string, r: EventRecord, observedAt?: string): void {
  useReviewStore.getState().actions.appendFragment({
    format: 1,
    session: sid,
    events: [r],
    ...(observedAt !== undefined && { observed_at: observedAt }),
  });
}

function reply(body: unknown, status = 200, observed?: string): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: observed === undefined ? {} : { [OBSERVED]: observed },
  });
}

const changed = (current: EventRecord | null, observed?: string) =>
  reply({ error: "changed", code: "changed", current }, 409, observed);

/** The `{expect, write}` body of the last request. */
function lastBody(): unknown {
  const [, init] = vi.mocked(fetch).mock.calls.at(-1) as [string, RequestInit];
  return JSON.parse(init.body as string);
}

const stored = (id: number) =>
  useReviewStore.getState().sessions[0]?.events.find((e) => e.id === id);
const history = () => useUndoHistory.getState();
const toast = () => useToastStore.getState().toast;

function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

describe("undo history", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
    useUiStore.setState({ openSpan: null });
    seam.afterRevert = null;
  });
  afterEach(() => vi.unstubAllGlobals());

  describe("each verb's undo and redo send the right {expect, write}", () => {
    it("add", async () => {
      const sid = hydrate();
      const added = record(7, "deploy");
      vi.mocked(fetch).mockResolvedValueOnce(reply(added, 201));
      await createEvent(sid, { label: "deploy" });
      vi.mocked(fetch).mockResolvedValueOnce(reply(null));
      await undo();
      expect(lastBody()).toEqual({ expect: added, write: null });
      expect(stored(7)).toBeUndefined();
      vi.mocked(fetch).mockResolvedValueOnce(reply(added));
      await redo();
      expect(lastBody()).toEqual({ expect: null, write: added });
      expect(stored(7)?.label).toBe("deploy");
    });

    it("edit", async () => {
      const sid = hydrate();
      seed(sid, record(7, "a"));
      const after = record(7, "b");
      vi.mocked(fetch).mockResolvedValueOnce(reply(after));
      await updateEvent(sid, 7, { label: "b" });
      vi.mocked(fetch).mockResolvedValueOnce(reply(record(7, "a")));
      await undo();
      expect(lastBody()).toEqual({ expect: after, write: record(7, "a") });
      vi.mocked(fetch).mockResolvedValueOnce(reply(after));
      await redo();
      expect(lastBody()).toEqual({ expect: record(7, "a"), write: after });
    });

    it("end", async () => {
      const sid = hydrate();
      seed(sid, record(7, "span"));
      const ended = record(7, "span", { end_timestamp: "2026-07-18T12:02:00+00:00" });
      vi.mocked(fetch).mockResolvedValueOnce(reply(ended));
      await endEvent(sid, 7);
      const open = { ...ended, end_timestamp: null };
      vi.mocked(fetch).mockResolvedValueOnce(reply(open));
      await undo();
      expect(lastBody()).toEqual({ expect: ended, write: open });
      vi.mocked(fetch).mockResolvedValueOnce(reply(ended));
      await redo();
      expect(lastBody()).toEqual({ expect: open, write: ended });
    });

    it("delete", async () => {
      const sid = hydrate();
      const gone = record(7, "deploy");
      seed(sid, gone);
      vi.mocked(fetch).mockResolvedValueOnce(reply(gone));
      await deleteEvent(sid, 7);
      vi.mocked(fetch).mockResolvedValueOnce(reply(gone));
      await undo();
      expect(lastBody()).toEqual({ expect: null, write: gone });
      expect(stored(7)?.label).toBe("deploy");
      vi.mocked(fetch).mockResolvedValueOnce(reply(null));
      await redo();
      expect(lastBody()).toEqual({ expect: gone, write: null });
      expect(stored(7)).toBeUndefined();
    });
  });

  it("a success moves the entry and toasts with the inverse action", async () => {
    const sid = hydrate();
    vi.mocked(fetch).mockResolvedValueOnce(reply(record(7, "deploy"), 201));
    await createEvent(sid, { label: "deploy" });
    vi.mocked(fetch).mockResolvedValueOnce(reply(null));
    await undo();
    expect(history().undo).toHaveLength(0);
    expect(history().redo).toHaveLength(1);
    expect(toast()?.message).toBe("Undid: add “deploy”");
    expect(toast()?.action?.label).toBe("Redo");
    vi.mocked(fetch).mockResolvedValueOnce(reply(record(7, "deploy")));
    await redo();
    expect(history().undo).toHaveLength(1);
    expect(history().redo).toHaveLength(0);
    expect(toast()?.message).toBe("Redid: add “deploy”");
    expect(toast()?.action?.label).toBe("Undo");
  });

  it("an 11th record evicts the oldest", () => {
    const { record: push } = useUndoHistory.getState().actions;
    for (let i = 1; i <= HISTORY_DEPTH + 1; i++) {
      push(entry({ eventId: i, label: `e${i}`, after: record(i, `e${i}`) }));
    }
    expect(history().undo).toHaveLength(HISTORY_DEPTH);
    expect(history().undo[0]?.label).toBe("e2");
    expect(history().undo.at(-1)?.label).toBe(`e${HISTORY_DEPTH + 1}`);
  });

  it("a new record clears redo", async () => {
    const sid = hydrate();
    vi.mocked(fetch).mockResolvedValueOnce(reply(record(7, "a"), 201));
    await createEvent(sid, { label: "a" });
    vi.mocked(fetch).mockResolvedValueOnce(reply(null));
    await undo();
    expect(history().redo).toHaveLength(1);
    vi.mocked(fetch).mockResolvedValueOnce(reply(record(8, "b"), 201));
    await createEvent(sid, { label: "b" });
    expect(history().redo).toHaveLength(0);
    expect(history().undo.map((e) => e.label)).toEqual(["b"]);
  });

  it("changed with a different current drops the entry and toasts", async () => {
    const sid = hydrate();
    vi.mocked(fetch).mockResolvedValueOnce(reply(record(7, "deploy"), 201));
    await createEvent(sid, { label: "deploy" });
    vi.mocked(fetch).mockResolvedValueOnce(changed(record(7, "renamed elsewhere")));
    await undo();
    expect(history().undo).toHaveLength(0);
    expect(history().redo).toHaveLength(0);
    expect(toast()?.message).toBe("Can't undo: “deploy” changed elsewhere");
    expect(stored(7)?.label).toBe("deploy");
  });

  it("changed whose current equals the target moves the entry and updates the store", async () => {
    const sid = hydrate();
    seed(sid, record(7, "a"));
    vi.mocked(fetch).mockResolvedValueOnce(reply(record(7, "b")));
    await updateEvent(sid, 7, { label: "b" });
    // The first undo committed, its response was lost; the retry is told the
    // event is already what it wanted.
    vi.mocked(fetch).mockResolvedValueOnce(changed(record(7, "a")));
    await undo();
    expect(history().undo).toHaveLength(0);
    expect(history().redo).toHaveLength(1);
    expect(stored(7)?.label).toBe("a");
    expect(toast()?.message).toBe("Undid: edit “b”");
  });

  it("a recovered deletion is applied through the success path", async () => {
    const sid = hydrate();
    vi.mocked(fetch).mockResolvedValueOnce(reply(record(7, "deploy"), 201));
    await createEvent(sid, { label: "deploy" });
    vi.mocked(fetch).mockResolvedValueOnce(changed(null));
    await undo();
    expect(stored(7)).toBeUndefined();
    expect(history().redo).toHaveLength(1);
  });

  it("a recovery whose stamp is older than a newer remote change leaves the newer change", async () => {
    const sid = hydrate();
    seed(sid, record(7, "a"), "2026-07-18T12:00:00+00:00");
    vi.mocked(fetch).mockResolvedValueOnce(reply(record(7, "b"), 200, "2026-07-18T12:01:00+00:00"));
    await updateEvent(sid, 7, { label: "b" });
    seed(sid, record(7, "newer"), "2026-07-18T12:03:00+00:00");
    vi.mocked(fetch).mockResolvedValueOnce(changed(record(7, "a"), "2026-07-18T12:02:00+00:00"));
    await undo();
    expect(stored(7)?.label).toBe("newer");
    expect(history().redo).toHaveLength(1);
  });

  it("busy keeps the entry", async () => {
    const sid = hydrate();
    vi.mocked(fetch).mockResolvedValueOnce(reply(record(7, "deploy"), 201));
    await createEvent(sid, { label: "deploy" });
    vi.mocked(fetch).mockResolvedValueOnce(reply({ error: "locked", code: "busy" }, 409));
    await undo();
    expect(history().undo).toHaveLength(1);
    expect(history().redo).toHaveLength(0);
    expect(toast()?.message).toBe("Couldn't undo — try again");
  });

  it("a network error keeps the entry", async () => {
    const sid = hydrate();
    vi.mocked(fetch).mockResolvedValueOnce(reply(record(7, "deploy"), 201));
    await createEvent(sid, { label: "deploy" });
    vi.mocked(fetch).mockResolvedValueOnce(reply(null));
    await undo();
    vi.mocked(fetch).mockRejectedValueOnce(new TypeError("down"));
    await redo();
    expect(history().redo).toHaveLength(1);
    expect(toast()?.message).toBe("Couldn't redo — try again");
  });

  it("two undo() calls in the same tick send ONE request", async () => {
    const sid = hydrate();
    vi.mocked(fetch).mockResolvedValueOnce(reply(record(7, "a"), 201));
    await createEvent(sid, { label: "a" });
    vi.mocked(fetch).mockResolvedValueOnce(reply(record(8, "b"), 201));
    await createEvent(sid, { label: "b" });
    vi.mocked(fetch).mockClear();
    vi.mocked(fetch).mockResolvedValue(reply(null));
    await Promise.all([undo(), undo()]);
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(history().undo.map((e) => e.label)).toEqual(["a"]);
  });

  it("undo() while an editor Save is pending is refused with the wait toast", async () => {
    const sid = hydrate();
    vi.mocked(fetch).mockResolvedValueOnce(reply(record(7, "a"), 201));
    await createEvent(sid, { label: "a" });
    vi.mocked(fetch).mockClear();
    const save = deferred();
    const saving = runGate(editorGateKey(sid, 7), "save", () => save.promise);
    await undo();
    expect(fetch).not.toHaveBeenCalled();
    expect(toast()?.message).toBe("Wait for the current change to finish");
    expect(history().undo).toHaveLength(1);
    save.resolve();
    await saving;
  });

  it("an update's before covers only the sent fields", async () => {
    const sid = hydrate();
    seed(sid, record(7, "a", { color: "#111111" }));
    // The colour changed elsewhere meanwhile; the response carries it.
    vi.mocked(fetch).mockResolvedValueOnce(reply(record(7, "b", { color: "#222222" })));
    await updateEvent(sid, 7, { label: "b" });
    expect(history().undo[0]?.before).toEqual(record(7, "a", { color: "#222222" }));
  });

  it("a legacy colour in before stays as stored", async () => {
    const sid = hydrate();
    seed(sid, record(7, "a", { color: "red" }));
    vi.mocked(fetch).mockResolvedValueOnce(reply(record(7, "a", { color: "#ff0000" })));
    await updateEvent(sid, 7, { color: "#ff0000" });
    expect(history().undo[0]?.before?.color).toBe("red");
  });

  it("a delete's before is the DELETE response's record, not the tab's copy", async () => {
    const sid = hydrate();
    seed(sid, record(7, "tab copy"));
    vi.mocked(fetch).mockResolvedValueOnce(reply(record(7, "server copy")));
    await deleteEvent(sid, 7);
    expect(history().undo[0]).toMatchObject({
      verb: "delete",
      after: null,
      before: record(7, "server copy"),
      label: "server copy",
    });
  });

  it("trackedSpan is captured at send time", async () => {
    const sid = hydrate();
    seed(sid, record(7, "span"));
    useUiStore.setState({ openSpan: { sessionId: sid, eventId: 7 } });
    vi.mocked(fetch).mockResolvedValueOnce(reply(record(7, "renamed")));
    await updateEvent(sid, 7, { label: "renamed" });
    vi.mocked(fetch).mockResolvedValueOnce(reply(record(8, "other"), 201));
    await createEvent(sid, { label: "other" });
    vi.mocked(fetch).mockResolvedValueOnce(reply(record(9, "started"), 201));
    await createEvent(sid, { label: "started" }, { trackSpan: true });
    expect(history().undo.map((e) => e.trackedSpan)).toEqual([true, false, true]);
  });

  it("a revert never records", async () => {
    const sid = hydrate();
    vi.mocked(fetch).mockResolvedValueOnce(reply(record(7, "a"), 201));
    await createEvent(sid, { label: "a" });
    vi.mocked(fetch).mockResolvedValueOnce(reply(null));
    await undo();
    expect(history().undo).toHaveLength(0);
    expect(history().redo).toHaveLength(1);
  });

  it("a file import between revertEvent resolving and the continuation does nothing", async () => {
    const sid = hydrate();
    vi.mocked(fetch).mockResolvedValueOnce(reply(record(7, "deploy"), 201));
    await createEvent(sid, { label: "deploy" });
    useToastStore.setState({ toast: null });
    const file = new File([JSON.stringify(minimal)], "imported.json");
    seam.afterRevert = () => importLocalFile(file);
    vi.mocked(fetch).mockResolvedValueOnce(reply(null));
    await undo();
    expect(history().redo).toHaveLength(0);
    expect(toast()).toBeNull();
    expect(useReviewStore.getState().sessions[0]?.events).toEqual([]);
  });

  it("truncateLabel keeps short labels and ellipsises long ones", () => {
    expect(truncateLabel("short")).toBe("short");
    const long = "x".repeat(50);
    expect(truncateLabel(long)).toHaveLength(40);
    expect(truncateLabel(long).endsWith("…")).toBe(true);
    expect(truncateLabel("abcdef", 4)).toBe("abc…");
  });
});

/** The URL of the last request. */
function lastUrl(): string {
  const [url] = vi.mocked(fetch).mock.calls.at(-1) as [string, RequestInit];
  return url;
}

// #929, spec §7: an undo or redo that leaves a tracked span's event open
// re-adopts it, through adoptOpenSpan, so End span targets it again; it
// never replaces a newer tracked span, nor adopts one a newer remote change
// ended.
describe("the tracked span across undo and redo", () => {
  const stamp = (s: number) => `2026-07-18T12:00:${String(s).padStart(2, "0")}+00:00`;
  const END = "2026-07-18T12:05:00+00:00";
  let uninstall: () => void;
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
    useUiStore.setState({ openSpan: null });
    seam.afterRevert = null;
    uninstall = installOpenSpanReconciler();
  });
  afterEach(() => {
    uninstall();
    vi.unstubAllGlobals();
    useUiStore.setState({ openSpan: null });
  });

  const span = useUiStore.getState;

  it("Start span → Undo → Redo → End span ends the restored event", async () => {
    const sid = hydrate();
    vi.mocked(fetch).mockResolvedValueOnce(reply(record(7, "span"), 201));
    await startSpan("span");
    expect(history().undo[0]?.trackedSpan).toBe(true);
    vi.mocked(fetch).mockResolvedValueOnce(reply(null));
    await undo();
    expect(span().openSpan).toBeNull();
    vi.mocked(fetch).mockResolvedValueOnce(reply(record(7, "span")));
    await redo();
    expect(span().openSpan).toEqual({ sessionId: sid, eventId: 7 });
    vi.mocked(fetch).mockResolvedValueOnce(reply(record(7, "span", { end_timestamp: END })));
    await endOpenSpan();
    expect(lastUrl()).toBe(`/api/session/${encodeURIComponent(sid)}/event/7/end`);
  });

  it("Start span, editor Save sets an end, Undo → End span targets it again", async () => {
    const sid = hydrate();
    vi.mocked(fetch).mockResolvedValueOnce(reply(record(7, "span"), 201));
    await startSpan("span");
    const ended = record(7, "span", { end_timestamp: END });
    vi.mocked(fetch).mockResolvedValueOnce(reply(ended));
    await updateEvent(sid, 7, { end_timestamp: END });
    expect(span().openSpan).toBeNull();
    vi.mocked(fetch).mockResolvedValueOnce(reply({ ...ended, end_timestamp: null }));
    await undo();
    expect(span().openSpan).toEqual({ sessionId: sid, eventId: 7 });
    vi.mocked(fetch).mockResolvedValueOnce(reply(ended));
    await endOpenSpan();
    expect(lastUrl()).toBe(`/api/session/${encodeURIComponent(sid)}/event/7/end`);
  });

  /** Start span 7, then End span it; returns the session and the open record. */
  async function startThenEnd(): Promise<{ sid: string; open: EventRecord }> {
    const sid = hydrate();
    const open = record(7, "span");
    vi.mocked(fetch).mockResolvedValueOnce(reply(open, 201, stamp(1)));
    await startSpan("span");
    vi.mocked(fetch).mockResolvedValueOnce(reply({ ...open, end_timestamp: END }, 200, stamp(2)));
    await endOpenSpan();
    expect(span().openSpan).toBeNull();
    return { sid, open };
  }

  it("undo-End re-adopts", async () => {
    const { sid, open } = await startThenEnd();
    vi.mocked(fetch).mockResolvedValueOnce(reply(open, 200, stamp(3)));
    await undo();
    expect(span().openSpan).toEqual({ sessionId: sid, eventId: 7 });
  });

  it("undo-End: a newer remote end before the response → not adopted", async () => {
    const { sid, open } = await startThenEnd();
    vi.mocked(fetch).mockImplementationOnce(() => {
      seed(sid, { ...open, end_timestamp: "2026-07-18T12:09:00+00:00" }, stamp(5));
      return Promise.resolve(reply(open, 200, stamp(4)));
    });
    await undo();
    expect(stored(7)?.end_timestamp).toBe("2026-07-18T12:09:00+00:00");
    expect(span().openSpan).toBeNull();
    expect(history().redo).toHaveLength(1);
  });

  it("re-adoption never replaces a newer tracked span", async () => {
    const { sid, open } = await startThenEnd();
    seed(sid, record(8, "newer"));
    useUiStore.getState().actions.setOpenSpan({ sessionId: sid, eventId: 8 });
    vi.mocked(fetch).mockResolvedValueOnce(reply(open, 200, stamp(3)));
    await undo();
    expect(stored(7)).toBeDefined();
    expect(stored(7)?.end_timestamp ?? null).toBeNull();
    expect(span().openSpan).toEqual({ sessionId: sid, eventId: 8 });
  });
});

// #929, spec §6: the history lives as long as the source and the tab's
// editability; it is per entry session, not per selected session.
describe("the history's lifetime", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
    useUiStore.setState({ openSpan: null });
    seam.afterRevert = null;
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    useReviewStore.setState({ editable: false, mode: null });
  });

  /** One entry on each stack. */
  async function bothStacks(sid: string): Promise<void> {
    vi.mocked(fetch).mockResolvedValueOnce(reply(record(7, "a"), 201));
    await createEvent(sid, { label: "a" });
    vi.mocked(fetch).mockResolvedValueOnce(reply(record(8, "b"), 201));
    await createEvent(sid, { label: "b" });
    vi.mocked(fetch).mockResolvedValueOnce(reply(null));
    await undo();
    expect(history().undo).toHaveLength(1);
    expect(history().redo).toHaveLength(1);
  }

  it("a file import clears both stacks, and a following Ctrl+Z sends nothing", async () => {
    const sid = hydrate();
    useReviewStore.getState().actions.setEditable(true);
    await bothStacks(sid);
    renderHook(() => useGlobalShortcuts(useCommands()));
    await act(() => importLocalFile(new File([JSON.stringify(minimal)], "imported.json")));
    expect(history().undo).toHaveLength(0);
    expect(history().redo).toHaveLength(0);
    vi.mocked(fetch).mockClear();
    const e = new KeyboardEvent("keydown", { key: "z", ctrlKey: true, cancelable: true });
    act(() => {
      document.dispatchEvent(e);
    });
    await Promise.resolve();
    expect(fetch).not.toHaveBeenCalled();
    expect(e.defaultPrevented).toBe(false);
  });

  it("the tab dropping to read-only clears both stacks", async () => {
    const sid = hydrate();
    useReviewStore.getState().actions.setEditable(true);
    await bothStacks(sid);
    useReviewStore.getState().actions.setEditable(false);
    expect(history().undo).toHaveLength(0);
    expect(history().redo).toHaveLength(0);
  });

  it("selecting another session keeps the history and the undo addresses the entry's session", async () => {
    const doc = JSON.parse(JSON.stringify(minimal));
    doc.sessions.push({ ...doc.sessions[0], id: "another-session" });
    useReviewStore.getState().actions.importMonitorSessions(JSON.stringify(doc), "test");
    const sid = useReviewStore.getState().sessions[0]?.id as string;
    useReviewStore.getState().actions.selectSession(sid);
    vi.mocked(fetch).mockResolvedValueOnce(reply(record(7, "a"), 201));
    await createEvent(sid, { label: "a" });
    useReviewStore.getState().actions.selectSession("another-session");
    expect(history().undo).toHaveLength(1);
    vi.mocked(fetch).mockResolvedValueOnce(reply(null));
    await undo();
    expect(lastUrl()).toBe(`/api/session/${encodeURIComponent(sid)}/event/7/revert`);
    expect(history().redo).toHaveLength(1);
  });
});

function entry(over: Partial<HistoryEntry>): HistoryEntry {
  return {
    verb: "add",
    sessionId: "s",
    eventId: 1,
    label: "x",
    after: null,
    before: null,
    trackedSpan: false,
    ...over,
  };
}
