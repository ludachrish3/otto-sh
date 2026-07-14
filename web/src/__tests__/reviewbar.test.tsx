// Review-bar behavior against the drift fixture (3 sessions, evolving lab)
// — the config-drift acceptance path: switching sessions re-renders under
// THAT session's lab.

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import App from "../App";
import { useReviewStore } from "../data/reviewStore";

// `new URL(relative, import.meta.url)` throws "The URL must be of scheme
// file" under this project's vitest/jsdom setup (see shell.test.tsx) —
// fileURLToPath+dirname+join is the pattern that works here.
const __dir = dirname(fileURLToPath(import.meta.url));
const DRIFT = readFileSync(join(__dir, "../../fixtures/drift.json"), "utf-8");
const MINIMAL = readFileSync(join(__dir, "../../fixtures/minimal.json"), "utf-8");
const KITCHEN_SINK = readFileSync(join(__dir, "../../fixtures/kitchen-sink.json"), "utf-8");

// jsdom (pinned here) doesn't implement `CSS.escape`
// (https://github.com/jsdom/jsdom/issues/3363), which react-aria's
// selection utilities call unconditionally when a Menu/Select autofocuses
// or scrolls a selected/focused item into view. Without this, the session
// picker Select throws on interaction. Polyfill per the CSSOM spec so real
// component behavior — not the test environment — is what's under test.
if (typeof globalThis.CSS === "undefined") {
  Object.defineProperty(globalThis, "CSS", {
    value: { escape: (value: string) => value.replace(/[^a-zA-Z0-9_-]/g, (ch) => `\\${ch}`) },
    writable: true,
  });
}

// jsdom doesn't implement `matchMedia` either — RangePicker's vendored
// RangeCalendar uses `useBreakpoint` (see ui/rangepicker.test.tsx for the
// full rationale, including why this reports every breakpoint as met).
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

function resetStore() {
  useReviewStore.setState({
    sessions: [],
    rawMonitorSessions: null,
    sourceName: null,
    warnings: [],
    importError: null,
    activeSessionId: null,
    range: null,
    mode: null,
  });
}

beforeEach(() => {
  window.location.hash = "#/";
});
afterEach(() => {
  // vitest's config doesn't set `test.globals: true`, so
  // @testing-library/react's automatic afterEach(cleanup) registration
  // never kicks in — without this, a popover/menu portal from one test's
  // render() lingers in the document for the next test's queries.
  cleanup();
  resetStore();
});

async function importText(text: string, name: string) {
  const file = new File([text], name, { type: "application/json" });
  fireEvent.change(screen.getByTestId("import-input"), { target: { files: [file] } });
  await waitFor(() => expect(screen.getByTestId("review-bar")).toBeTruthy());
}

// The vendored Untitled UI `Select` (components/base/select/select.tsx)
// spreads unknown props — including `data-testid` — onto the outer wrapper
// `AriaSelect` itself renders, not onto the pressable button nested inside
// it (that button is built internally by the vendored component and never
// receives caller props). `getByTestId("session-picker")` therefore finds
// the wrapper; the actual click target is the `role="button"` element
// within it. Real clicks (Playwright) are unaffected — it hit-tests by
// pixel, and the wrapper sits exactly on top of the button.
function sessionPickerButton() {
  return within(screen.getByTestId("session-picker")).getByRole("button");
}

// react-aria's press handling (`usePress`) is driven by pointer events, not
// the single synthetic `click` event `fireEvent.click` dispatches —
// `userEvent` synthesizes the full pointerdown/pointerup/click sequence a
// real interaction produces, which is what these components actually
// listen for.
async function openSessionPicker(user: ReturnType<typeof userEvent.setup>) {
  await user.click(sessionPickerButton());
}

describe("ReviewBar", () => {
  it("shows tag + source, hides the session picker for single-session files", async () => {
    render(<App />);
    await importText(MINIMAL, "minimal.json");
    expect(screen.getByTestId("historical-tag").textContent).toBe("HISTORICAL");
    expect(screen.getByTestId("source-name").textContent).toBe("minimal.json");
    expect(screen.queryByTestId("session-picker")).toBeNull();
  });

  it("switches sessions and re-renders that session's lab (drift)", async () => {
    const user = userEvent.setup();
    render(<App />);
    await importText(DRIFT, "drift.json");
    expect(screen.getByTestId("session-picker")).toBeTruthy();
    // baseline lab: no workers_w1
    expect(screen.queryByTestId("subject-link-workers_w1")).toBeNull();
    await openSessionPicker(user);
    // react-aria-components' Select also mirrors its options into a
    // visually-hidden native <select> (for autofill/native-form support),
    // so an unscoped role query would match both that <option> and the
    // visible popover item — scope to the listbox the popover renders.
    await user.click(within(screen.getByRole("listbox")).getByRole("option", { name: "expanded" }));
    await waitFor(() => expect(screen.getByTestId("subject-link-workers_w1")).toBeTruthy());
    expect(screen.getByTestId("subject-link-workers_w2")).toBeTruthy();
    await openSessionPicker(user);
    await user.click(within(screen.getByRole("listbox")).getByRole("option", { name: "rewired" }));
    await waitFor(() => expect(screen.queryByTestId("subject-link-workers_w2")).toBeNull());
    expect(screen.getByTestId("subject-link-edge-gw")).toBeTruthy();
  });

  it("carries a session's note as the picker option's supporting text", async () => {
    // Build a two-session document from MINIMAL rather than reusing the
    // shared drift/kitchen-sink fixtures — this test owns exactly the shape
    // it needs (one session with a note, one without) without risking
    // collateral changes to fixtures other test files also assert against.
    const base = JSON.parse(MINIMAL) as { format: number; sessions: Record<string, unknown>[] };
    const [first] = base.sessions;
    const second = {
      ...first,
      id: "2026-07-02T08-00-00-second",
      label: "second",
      note: "why this run",
    };
    const doc = JSON.stringify({ format: base.format, sessions: [first, second] });

    const user = userEvent.setup();
    render(<App />);
    await importText(doc, "two-session.json");
    await openSessionPicker(user);
    const listbox = screen.getByRole("listbox");
    // Untitled UI's SelectItemType has a native `supportingText` field
    // (select-shared.tsx), rendered as a second text node under the
    // label — the session's note maps onto that instead of a `title`
    // hover tooltip (select.tsx:77, select-item.tsx:115-119).
    const secondOption = within(listbox).getByRole("option", { name: "second" });
    expect(within(secondOption).getByText("why this run")).toBeTruthy();
    // The un-noted session's option renders no supporting-text node.
    const firstOption = within(listbox).getByRole("option", { name: "minimal" });
    expect(within(firstOption).queryByText("why this run")).toBeNull();
    expect(firstOption.textContent).toBe("minimal");
  });

  // Plan 5b final review, Finding C1: mode="live" must hide the whole
  // HISTORICAL bar, not just leave it dangling under AppBar's "Live ●"
  // status. This hiding was reverted (commit 7a9e849) only because
  // bootstrap.ts used to set mode="live" BEFORE a boot hydrate had
  // succeeded — that root cause is fixed now (mode is set only after a
  // successful hydrate), so the hiding in ReviewBar itself is safe again.
  it("hides entirely in live mode, independent of AppBar's own live status", async () => {
    render(<App />);
    await importText(MINIMAL, "minimal.json");
    expect(screen.getByTestId("historical-tag")).toBeTruthy();
    act(() => {
      useReviewStore.setState({ mode: "live" });
    });
    expect(screen.queryByTestId("review-bar")).toBeNull();
    expect(screen.queryByTestId("historical-tag")).toBeNull();
  });

  // RangePicker's own unit tests (ui/rangepicker.test.tsx) cover its preset
  // math, minute precision, and bounds-clamping against a mocked onChange —
  // this checks the real wiring into the store: ReviewBar hands it the
  // active session's actual bounds and `actions.setRange`, not a stand-in.
  it("wires the range picker to the store — a preset narrows `range`", async () => {
    const user = userEvent.setup();
    render(<App />);
    await importText(KITCHEN_SINK, "kitchen-sink.json");
    expect(screen.getByTestId("range-picker")).toBeTruthy();
    expect(useReviewStore.getState().range).toBeNull();
    await user.click(within(screen.getByTestId("range-picker")).getByRole("button"));
    await user.click(screen.getByRole("button", { name: "Last 15m" }));
    await user.click(screen.getByRole("button", { name: "Apply" }));
    const session = useReviewStore.getState().sessions[0];
    const range = useReviewStore.getState().range;
    expect(range).not.toBeNull();
    expect(range?.to).toBe(session.endMs);
    expect(range?.from).toBe(Math.max(session.startMs, session.endMs - 15 * 60_000));
  });
});
