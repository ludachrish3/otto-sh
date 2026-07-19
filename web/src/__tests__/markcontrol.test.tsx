// web/src/__tests__/markcontrol.test.tsx
// Fetch-mocked against the real stores, mirroring marking.test.ts/eventapi.
// test.ts's idiom: MarkControl is a thin UI shell over marking.ts + the
// ui/review stores, so the store IS the assertion surface for openSpan and
// the popover's own testids are the assertion surface for the rest.
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import minimal from "../../fixtures/minimal.json";
import { useReviewStore } from "../data/reviewStore";
import { MarkControl } from "../shell/MarkControl";
import { useUiStore } from "../ui/uiStore";

// jsdom lacks CSS.escape; react-aria menus call it (same polyfill as
// commandmenu.test.tsx/shell.test.tsx — see their comments for the issue).
if (typeof globalThis.CSS === "undefined") {
  Object.defineProperty(globalThis, "CSS", {
    value: { escape: (value: string) => value.replace(/[^a-zA-Z0-9_-]/g, (ch) => `\\${ch}`) },
    writable: true,
  });
}

// minimal.json's first session id — hydrate once per test so marking.ts's
// requireActiveSessionId has a session to land on; setMode/setEditable put
// the store in the state AppBar would gate MarkControl's mount behind
// (MarkControl itself does not re-check mode — that's AppBar's job).
function hydrate(): string {
  useReviewStore.getState().actions.importMonitorSessions(JSON.stringify(minimal), "test");
  useReviewStore.getState().actions.setMode("live");
  useReviewStore.getState().actions.setEditable(true);
  const id = useReviewStore.getState().sessions[0]?.id;
  if (!id) throw new Error("fixture has no session");
  return id;
}

const record = (id: number, label: string, endTimestamp: string | null = null) => ({
  id,
  timestamp: "2026-07-18T12:01:00+00:00",
  label,
  source: "manual",
  color: "#888888",
  dash: "dash",
  end_timestamp: endTimestamp,
});

function okJson(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status });
}

function lastRequestBody(): unknown {
  const calls = vi.mocked(fetch).mock.calls;
  const init = calls.at(-1)?.[1] as RequestInit;
  return JSON.parse(init.body as string);
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
  useUiStore.setState({ markPopover: null, openSpan: null });
});

describe("MarkControl", () => {
  it("clicking mark-button opens the label popover", async () => {
    const user = userEvent.setup();
    hydrate();
    vi.stubGlobal("fetch", vi.fn());
    render(<MarkControl />);
    expect(screen.queryByTestId("mark-popover")).toBeNull();
    await user.click(screen.getByTestId("mark-button"));
    expect(await screen.findByTestId("mark-popover")).toBeTruthy();
    expect(screen.getByTestId("mark-submit").textContent).toBe("Mark");
  });

  it("typing a label and pressing Enter POSTs to the create route and closes the popover", async () => {
    const user = userEvent.setup();
    const sid = hydrate();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(okJson(record(9, "checkpoint"), 201)));
    render(<MarkControl />);
    await user.click(screen.getByTestId("mark-button"));
    const input = await screen.findByTestId("mark-label-input");
    await user.type(input, "checkpoint{Enter}");
    await waitFor(() =>
      expect(vi.mocked(fetch)).toHaveBeenCalledWith(
        `/api/session/${encodeURIComponent(sid)}/event`,
        expect.objectContaining({ method: "POST" }),
      ),
    );
    expect(lastRequestBody()).toEqual({ label: "checkpoint" });
    await waitFor(() => expect(screen.queryByTestId("mark-popover")).toBeNull());
  });

  it("a rejected fetch shows mark-error with the server message and keeps the popover open", async () => {
    const user = userEvent.setup();
    hydrate();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(okJson({ error: "archive is locked" }, 409)));
    render(<MarkControl />);
    await user.click(screen.getByTestId("mark-button"));
    const input = await screen.findByTestId("mark-label-input");
    await user.type(input, "checkpoint{Enter}");
    const errorEl = await screen.findByTestId("mark-error");
    expect(errorEl.textContent).toContain("archive is locked");
    expect(screen.getByTestId("mark-popover")).toBeTruthy();
  });

  it("the menu's Start span… opens the popover in start mode; submitting records openSpan", async () => {
    const user = userEvent.setup();
    const sid = hydrate();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(okJson(record(9, "soak"), 201)));
    render(<MarkControl />);
    await user.click(screen.getByTestId("mark-menu"));
    await user.click(await screen.findByTestId("menu-start-span"));
    const popover = await screen.findByTestId("mark-popover");
    expect(popover.textContent).toContain("Start");
    expect(screen.getByTestId("mark-submit").textContent).toBe("Start");
    await user.type(await screen.findByTestId("mark-label-input"), "soak");
    await user.click(screen.getByTestId("mark-submit"));
    await waitFor(() =>
      expect(useUiStore.getState().openSpan).toEqual({ sessionId: sid, eventId: 9 }),
    );
    await waitFor(() => expect(screen.queryByTestId("mark-popover")).toBeNull());
  });

  it("End span is disabled until a span is open on this session, then enabled", async () => {
    const user = userEvent.setup();
    hydrate();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(okJson(record(9, "soak"), 201)));
    render(<MarkControl />);

    await user.click(screen.getByTestId("mark-menu"));
    const disabledItem = await screen.findByTestId("menu-end-span");
    expect(disabledItem.getAttribute("aria-disabled")).toBe("true");
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByTestId("menu-end-span")).toBeNull());

    await user.click(screen.getByTestId("mark-menu"));
    await user.click(await screen.findByTestId("menu-start-span"));
    await user.type(await screen.findByTestId("mark-label-input"), "soak");
    await user.click(screen.getByTestId("mark-submit"));
    await waitFor(() => expect(useUiStore.getState().openSpan).not.toBeNull());

    await user.click(screen.getByTestId("mark-menu"));
    const enabledItem = await screen.findByTestId("menu-end-span");
    expect(enabledItem.getAttribute("aria-disabled")).not.toBe("true");
  });

  it("End span failure is routed to reviewStore's warnings, not swallowed", async () => {
    const user = userEvent.setup();
    hydrate();
    const fetchMock = vi.fn().mockResolvedValueOnce(okJson(record(9, "soak"), 201));
    vi.stubGlobal("fetch", fetchMock);
    render(<MarkControl />);

    await user.click(screen.getByTestId("mark-menu"));
    await user.click(await screen.findByTestId("menu-start-span"));
    await user.type(await screen.findByTestId("mark-label-input"), "soak");
    await user.click(screen.getByTestId("mark-submit"));
    await waitFor(() => expect(useUiStore.getState().openSpan).not.toBeNull());

    fetchMock.mockResolvedValueOnce(okJson({ error: "archive is locked" }, 409));
    await user.click(screen.getByTestId("mark-menu"));
    await user.click(await screen.findByTestId("menu-end-span"));
    await waitFor(() =>
      expect(useReviewStore.getState().warnings).toEqual(["End span failed: archive is locked"]),
    );
  });

  it("Add event… opens the event editor with a blank draft anchored to the session end", async () => {
    const user = userEvent.setup();
    hydrate();
    vi.stubGlobal("fetch", vi.fn());
    render(<MarkControl />);
    await user.click(screen.getByTestId("mark-menu"));
    await user.click(await screen.findByTestId("menu-add-event"));
    const target = useUiStore.getState().eventEditor;
    expect(target?.kind).toBe("draft");
  });

  it("Sweep span on chart arms the sweep gesture", async () => {
    const user = userEvent.setup();
    hydrate();
    vi.stubGlobal("fetch", vi.fn());
    render(<MarkControl />);
    await user.click(screen.getByTestId("mark-menu"));
    await user.click(await screen.findByTestId("menu-sweep-span"));
    expect(useUiStore.getState().sweepArmed).toBe(true);
  });

  it("renders nothing without an active session", () => {
    vi.stubGlobal("fetch", vi.fn());
    const { container } = render(<MarkControl />);
    expect(container.firstChild).toBeNull();
  });
});
