// web/src/ui/commands.test.tsx
import { renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { useReviewStore } from "../data/reviewStore";
import { useCommands } from "./commands";
import { useUiStore } from "./uiStore";

// Minimal session shape the registry reads: lab.hosts (id/board/slot),
// elements (id), plus store mode/windowMs. Matches NormalizedSession's
// fields used by OverviewPage (hosts/elements) — see data/reviewStore.
const SESSION = {
  id: "s1",
  label: null,
  note: null,
  startMs: 0,
  endMs: 60_000,
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

afterEach(() => {
  useReviewStore.setState({
    sessions: [],
    activeSessionId: null,
    rawMonitorSessions: null,
    mode: null,
    range: null,
    windowMs: 900_000,
  });
  useUiStore.setState({ paletteOpen: false, theme: "light" });
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
    useUiStore.setState({ theme: "dark" });
    const { result: r2 } = renderHook(() => useCommands());
    expect(r2.current.find((c) => c.id === "action-theme")?.label).toBe("Switch to light mode");
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
    result.current.find((c) => c.id === "nav-host-test1")?.run();
    expect(window.location.hash).toBe("#/host/test1");
  });
});
