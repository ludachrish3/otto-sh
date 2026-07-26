// focus.tsx's contract (Task 7 brief): a single pinned run-label, state in
// the hash-embedded route query (`?ctx=<label>`, INSIDE the `#...`
// fragment — not wouter's real pre-`#` location.search) mirrored to
// `localStorage["otto-cov:<stamp>:focus"]`. Boot precedence: query wins,
// else localStorage; an unknown label at boot is treated as cleared, never
// a crash. `setFocus` toasts "Focused <label>"/"Focus cleared"; a
// dropped-by-navigation query gets re-appended so the chip survives
// internal navigation.
import {
  act,
  cleanup,
  fireEvent,
  render,
  renderHook,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { allowConsoleOutput } from "../../vitest.setup";
import {
  FocusProvider,
  parseHashQuery,
  replaceHashQuery,
  setHashQuery,
  useFocus,
  useHashLocation,
} from "./focus";
import { ToastProvider } from "./Toast";
import { makeIndex, makeRun } from "./testUtils";
import type { IndexPayload } from "./types";

function buildIndex(overrides: Partial<IndexPayload> = {}): IndexPayload {
  return makeIndex({
    stamp: "stamp-1",
    runs: [
      makeRun({ id: 1, label: "ctx-a", tier: "system" }),
      makeRun({ id: 2, label: "ctx-b", tier: "unit" }),
    ],
    ...overrides,
  });
}

function Consumer() {
  const { focus, setFocus } = useFocus();
  return (
    <div>
      <span data-testid="focus-value">{focus ?? "null"}</span>
      <button type="button" data-testid="set-a" onClick={() => setFocus("ctx-a")}>
        set a
      </button>
      <button type="button" data-testid="set-b" onClick={() => setFocus("ctx-b")}>
        set b
      </button>
      <button type="button" data-testid="clear" onClick={() => setFocus(null)}>
        clear
      </button>
    </div>
  );
}

function renderConsumer() {
  return render(
    <ToastProvider>
      <FocusProvider>
        <Consumer />
      </FocusProvider>
    </ToastProvider>,
  );
}

beforeEach(() => {
  window.location.hash = "";
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks(); // several tests spy on window.history.replaceState
  window.location.hash = "";
  localStorage.clear();
  delete (window as { __OTTO_COV__?: IndexPayload }).__OTTO_COV__;
});

describe("parseHashQuery / setHashQuery", () => {
  it("parses the ctx param out of the hash fragment (not real location.search)", () => {
    window.location.hash = "#/coverage/a.c?ctx=nightly-full";
    expect(parseHashQuery().get("ctx")).toBe("nightly-full");
  });

  it("returns an empty params object when the hash has no query", () => {
    window.location.hash = "#/coverage/a.c";
    expect(parseHashQuery().get("ctx")).toBeNull();
  });

  it("setHashQuery preserves the path and writes only the query", () => {
    window.location.hash = "#/coverage/a.c";
    setHashQuery((p) => p.set("ctx", "nightly-full"));
    expect(window.location.hash).toBe("#/coverage/a.c?ctx=nightly-full");
  });

  it("setHashQuery is a no-op (no write) when nothing actually changes", () => {
    window.location.hash = "#/coverage/a.c?ctx=x";
    setHashQuery((p) => p.set("ctx", "x"));
    expect(window.location.hash).toBe("#/coverage/a.c?ctx=x");
  });

  it("setHashQuery can delete the param, collapsing back to a bare path", () => {
    window.location.hash = "#/coverage/a.c?ctx=x";
    setHashQuery((p) => p.delete("ctx"));
    expect(window.location.hash).toBe("#/coverage/a.c");
  });
});

// Push vs. replace is the whole fix for the Back-button trap (fix report,
// task-7-report.md): setHashQuery (a real user action, e.g. setFocus
// pinning/clearing) MUST push a Back-button stop; replaceHashQuery (a
// reconciliation write nobody asked for — boot restore, navigation-
// survival re-append) must NOT, or every in-app navigation while focused
// leaves a ctx-less intermediate entry that traps Back.
describe("setHashQuery vs replaceHashQuery: push vs. replace semantics", () => {
  it("setHashQuery pushes a new session-history entry", () => {
    window.location.hash = "#/coverage/a.c";
    const before = window.history.length;
    setHashQuery((p) => p.set("ctx", "x"));
    expect(window.history.length).toBe(before + 1);
  });

  it("replaceHashQuery rewrites the current entry in place — same hash, no new entry", () => {
    window.location.hash = "#/coverage/a.c";
    const before = window.history.length;
    replaceHashQuery((p) => p.set("ctx", "x"));
    expect(window.location.hash).toBe("#/coverage/a.c?ctx=x");
    expect(window.history.length).toBe(before);
  });

  it("replaceHashQuery calls history.replaceState, not a plain (pushing) hash assignment", () => {
    window.location.hash = "#/coverage/a.c";
    const replaceSpy = vi.spyOn(window.history, "replaceState");
    replaceHashQuery((p) => p.set("ctx", "x"));
    expect(replaceSpy).toHaveBeenCalledTimes(1);
  });

  it("replaceHashQuery is a no-op — no history.replaceState call — when nothing actually changes", () => {
    window.location.hash = "#/coverage/a.c?ctx=x";
    const replaceSpy = vi.spyOn(window.history, "replaceState");
    replaceHashQuery((p) => p.set("ctx", "x"));
    expect(replaceSpy).not.toHaveBeenCalled();
  });
});

describe("useHashLocation (custom Router hook)", () => {
  it("strips a hash-embedded ctx query from the location it hands to wouter's route matcher", () => {
    window.location.hash = "#/coverage/src/foo.c?ctx=nightly-full";
    const { result } = renderHook(() => useHashLocation());
    expect(result.current[0]).toBe("/coverage/src/foo.c");
  });

  it("returns a clean path unchanged when there is no query at all", () => {
    window.location.hash = "#/coverage";
    const { result } = renderHook(() => useHashLocation());
    expect(result.current[0]).toBe("/coverage");
  });

  it("navigate() writes a plain hash path", () => {
    window.location.hash = "#/coverage";
    const { result } = renderHook(() => useHashLocation());
    result.current[1]("/coverage/other");
    expect(window.location.hash).toBe("#/coverage/other");
  });
});

describe("useFocus / FocusProvider", () => {
  it("throws when used without a FocusProvider ancestor", () => {
    allowConsoleOutput(); // React logs the render-time throw to console.error
    function Bare() {
      useFocus();
      return null;
    }
    expect(() => render(<Bare />)).toThrow(/FocusProvider/);
  });

  it("setFocus writes the hash query and the stamp-namespaced storage key", () => {
    window.__OTTO_COV__ = buildIndex();
    window.location.hash = "#/coverage";
    renderConsumer();

    fireEvent.click(screen.getByTestId("set-a"));

    expect(screen.getByTestId("focus-value").textContent).toBe("ctx-a");
    expect(window.location.hash).toBe("#/coverage?ctx=ctx-a");
    expect(localStorage.getItem("otto-cov:stamp-1:focus")).toBe("ctx-a");
  });

  it("setFocus switches directly between two contexts (menu-switcher scenario)", () => {
    window.__OTTO_COV__ = buildIndex();
    window.location.hash = "#/coverage";
    renderConsumer();

    fireEvent.click(screen.getByTestId("set-a"));
    fireEvent.click(screen.getByTestId("set-b"));

    expect(screen.getByTestId("focus-value").textContent).toBe("ctx-b");
    expect(window.location.hash).toBe("#/coverage?ctx=ctx-b");
    expect(localStorage.getItem("otto-cov:stamp-1:focus")).toBe("ctx-b");
  });

  it("setFocus(null) removes the query param and the storage key", () => {
    window.__OTTO_COV__ = buildIndex();
    window.location.hash = "#/coverage?ctx=ctx-a";
    localStorage.setItem("otto-cov:stamp-1:focus", "ctx-a");
    renderConsumer();

    fireEvent.click(screen.getByTestId("clear"));

    expect(screen.getByTestId("focus-value").textContent).toBe("null");
    expect(window.location.hash).toBe("#/coverage");
    expect(localStorage.getItem("otto-cov:stamp-1:focus")).toBeNull();
  });

  it("boot: the hash query wins over localStorage", () => {
    window.__OTTO_COV__ = buildIndex();
    localStorage.setItem("otto-cov:stamp-1:focus", "ctx-b");
    window.location.hash = "#/coverage?ctx=ctx-a";
    renderConsumer();

    expect(screen.getByTestId("focus-value").textContent).toBe("ctx-a");
  });

  it("boot: a localStorage-only focus (no query) is adopted AND written into the hash query", () => {
    window.__OTTO_COV__ = buildIndex();
    localStorage.setItem("otto-cov:stamp-1:focus", "ctx-b");
    window.location.hash = "#/coverage";
    renderConsumer();

    expect(screen.getByTestId("focus-value").textContent).toBe("ctx-b");
    expect(window.location.hash).toBe("#/coverage?ctx=ctx-b");
  });

  // Fix report (task-7-report.md, IMPORTANT 1): the boot write-back used to
  // push, growing session history by one entry on every page load with a
  // storage-only focus — nobody navigated, so nothing should be pushed.
  it("boot: the localStorage-only write-back does NOT push a new history entry (replaceState)", async () => {
    window.__OTTO_COV__ = buildIndex();
    localStorage.setItem("otto-cov:stamp-1:focus", "ctx-b");
    window.location.hash = "#/coverage";
    const before = window.history.length;
    const replaceSpy = vi.spyOn(window.history, "replaceState");

    renderConsumer();

    await waitFor(() => expect(window.location.hash).toBe("#/coverage?ctx=ctx-b"));
    expect(replaceSpy).toHaveBeenCalled();
    expect(window.history.length).toBe(before);
  });

  it("boot: an unknown label in the query is treated as cleared, without crashing, and the stale param is wiped", () => {
    window.__OTTO_COV__ = buildIndex();
    window.location.hash = "#/coverage?ctx=ghost-context";
    expect(() => renderConsumer()).not.toThrow();

    expect(screen.getByTestId("focus-value").textContent).toBe("null");
    expect(window.location.hash).toBe("#/coverage");
  });

  it("boot: an unknown label in localStorage is treated as cleared, without crashing, and the stale entry is wiped", () => {
    window.__OTTO_COV__ = buildIndex();
    localStorage.setItem("otto-cov:stamp-1:focus", "ghost-context");
    window.location.hash = "#/coverage";
    expect(() => renderConsumer()).not.toThrow();

    expect(screen.getByTestId("focus-value").textContent).toBe("null");
    expect(localStorage.getItem("otto-cov:stamp-1:focus")).toBeNull();
  });

  it("no data payload at all (getIndex() null) boots unfocused without crashing", () => {
    window.location.hash = "#/coverage?ctx=ctx-a";
    expect(() => renderConsumer()).not.toThrow();
    expect(screen.getByTestId("focus-value").textContent).toBe("null");
  });

  it("namespaces the storage key by report stamp", () => {
    window.__OTTO_COV__ = buildIndex({ stamp: "stamp-A" });
    window.location.hash = "#/coverage";
    renderConsumer();

    fireEvent.click(screen.getByTestId("set-a"));

    expect(localStorage.getItem("otto-cov:stamp-A:focus")).toBe("ctx-a");
    expect(localStorage.getItem("otto-cov:stamp-1:focus")).toBeNull();
  });

  it("re-appends the ctx query after a navigation that drops it (chip survives internal navigation)", async () => {
    window.__OTTO_COV__ = buildIndex();
    window.location.hash = "#/coverage?ctx=ctx-a";
    renderConsumer();
    expect(screen.getByTestId("focus-value").textContent).toBe("ctx-a");

    // A plain navigation (a raw `<a href="#/...">` click, or wouter's own
    // navigate()) REPLACES the whole hash fragment — simulate that losing
    // the query outright.
    window.location.hash = "#/coverage/foo";

    await waitFor(() => {
      expect(window.location.hash).toBe("#/coverage/foo?ctx=ctx-a");
    });
    // The pinned focus itself never changed.
    expect(screen.getByTestId("focus-value").textContent).toBe("ctx-a");
  });

  it("does not fight a navigation when nothing is focused", async () => {
    window.__OTTO_COV__ = buildIndex();
    window.location.hash = "#/coverage";
    renderConsumer();
    expect(screen.getByTestId("focus-value").textContent).toBe("null");

    window.location.hash = "#/coverage/foo";
    await waitFor(() => expect(window.location.hash).toBe("#/coverage/foo"));
  });

  // Fix report (task-7-report.md, IMPORTANT 1): the Back-button trap. Before
  // the fix, the re-append ALSO pushed — so one in-app navigation while
  // focused produced TWO history entries (the navigation's own push, then
  // this correction's push), and Back landed on the ctx-less intermediate
  // entry, which this same handler immediately corrected forward again.
  // Real Back/Forward keyboard/mouse behavior isn't exercised by jsdom here
  // (see Task 9's browser lane for that) — this pins the MECHANISM the trap
  // depended on: exactly one new entry per navigation, always already
  // carrying the right `?ctx=`, via replaceState not a second push.
  it("re-appending a dropped ctx after navigation uses replaceState, costing exactly ONE history entry (not two)", async () => {
    window.__OTTO_COV__ = buildIndex();
    window.location.hash = "#/coverage?ctx=ctx-a";
    renderConsumer();
    expect(screen.getByTestId("focus-value").textContent).toBe("ctx-a");

    const before = window.history.length;
    const replaceSpy = vi.spyOn(window.history, "replaceState");

    // The ONE push a real navigation performs (a raw `<a href="#/...">`
    // click, or wouter's own navigate()) — dropping the query outright.
    window.location.hash = "#/coverage/foo";
    expect(window.history.length).toBe(before + 1); // the navigation's own push

    await waitFor(() => expect(window.location.hash).toBe("#/coverage/foo?ctx=ctx-a"));
    expect(replaceSpy).toHaveBeenCalled();
    // The re-append rewrote the CURRENT (just-pushed) entry in place —
    // still exactly one new entry total, not two.
    expect(window.history.length).toBe(before + 1);
  });

  it("explicit setFocus (pin) still pushes a real history entry — a user action, unlike the reconciliation writes above", () => {
    window.__OTTO_COV__ = buildIndex();
    window.location.hash = "#/coverage";
    renderConsumer();
    const before = window.history.length;

    fireEvent.click(screen.getByTestId("set-a"));

    expect(window.history.length).toBe(before + 1);
  });

  it("explicit setFocus(null) (clear) also pushes a real history entry", () => {
    window.__OTTO_COV__ = buildIndex();
    window.location.hash = "#/coverage?ctx=ctx-a";
    renderConsumer();
    const before = window.history.length;

    fireEvent.click(screen.getByTestId("clear"));

    expect(window.history.length).toBe(before + 1);
  });

  it("toasts 'Focused <label>' on pin and 'Focus cleared' on clear", () => {
    window.__OTTO_COV__ = buildIndex();
    window.location.hash = "#/coverage";
    renderConsumer();

    fireEvent.click(screen.getByTestId("set-a"));
    expect(screen.getByTestId("toast").textContent).toBe("Focused ctx-a");

    fireEvent.click(screen.getByTestId("clear"));
    expect(screen.getByTestId("toast").textContent).toBe("Focus cleared");
  });

  it("round-trips a label containing spaces/parens through the hash query", () => {
    window.__OTTO_COV__ = buildIndex({
      runs: [makeRun({ id: 1, label: "router-a (system bed)", tier: "system" })],
    });
    window.location.hash = "#/coverage";
    renderConsumer();

    fireEvent.click(screen.getByTestId("clear")); // no-op, exercises nothing focused yet
    // Directly drive setFocus with the spacey label via a dedicated button
    // would require a new fixture button; instead simulate a deep link with
    // it and confirm it boots resolved.
    cleanup();
    window.location.hash = `#/coverage?ctx=${encodeURIComponent("router-a (system bed)")}`;
    renderConsumer();
    expect(screen.getByTestId("focus-value").textContent).toBe("router-a (system bed)");
  });
});

// Fix round 2 (task-9-report.md): the FIRST fix for "Back/Forward while
// focused" used a `popstate` + "did `history.length` grow" heuristic —
// provably wrong (found in review): a push made from MID-STACK (after at
// least one Back) truncates the forward entries it replaces, so
// `history.length` doesn't reliably grow on a push either, misreading an
// ordinary in-app click as a genuine Back/Forward and silently clearing a
// pinned focus. The real browser Back/Forward *traversal* itself can't be
// simulated in jsdom (see test_spa_runs_focus.py's browser-lane coverage
// for that), but the actual discriminator these tests pin — what
// `history.state` looks like at `hashchange` time — is plain DOM state
// jsdom supports directly: a fresh `location.hash` push always lands with
// `state: null` (browsers create every new entry that way); this module's
// own `stampCurrentEntry()` is the only thing that ever sets
// `history.state.ottoCov`, and only AFTER a hashchange has already been
// processed once for that entry — so a `hashchange` arriving with the
// stamp already present can only mean "we've been at this exact history
// position before", i.e. a genuine traversal landed here.
describe("FocusProvider: history.state stamp discriminates Back/Forward from a fresh push", () => {
  it("stamps the entry it boots on, so a later landing there reads as known", () => {
    window.__OTTO_COV__ = buildIndex();
    window.location.hash = "#/coverage?ctx=ctx-a";
    renderConsumer();

    expect(screen.getByTestId("focus-value").textContent).toBe("ctx-a");
    expect(window.history.state).toEqual({ ottoCov: true });
  });

  it("a fresh location.hash push lands with null history.state and gets REASSERTED, not adopted", async () => {
    window.__OTTO_COV__ = buildIndex();
    window.location.hash = "#/coverage?ctx=ctx-a";
    renderConsumer();
    expect(screen.getByTestId("focus-value").textContent).toBe("ctx-a");

    window.location.hash = "#/coverage/foo"; // a plain in-app push, drops ctx
    // Sanity on the discriminator's actual premise, not just the outcome:
    // jsdom's own `location.hash =` semantics create a brand-new entry
    // with no stamp on it — jsdom itself reports this as `undefined`
    // (real browsers report `null`); `?? null` normalizes both to the
    // same "nothing here" value the production code's own `?? {}` already
    // treats identically.
    expect(window.history.state ?? null).toBeNull();

    await waitFor(() => expect(window.location.hash).toBe("#/coverage/foo?ctx=ctx-a"));
    expect(screen.getByTestId("focus-value").textContent).toBe("ctx-a"); // reasserted, not cleared
  });

  it("a hashchange landing on a PREVIOUSLY-STAMPED entry ADOPTS its own ctx instead of reasserting the pin", async () => {
    window.__OTTO_COV__ = buildIndex();
    window.location.hash = "#/coverage?ctx=ctx-a";
    renderConsumer();
    expect(screen.getByTestId("focus-value").textContent).toBe("ctx-a");

    // jsdom can't fire a real popstate/history traversal — this reproduces
    // the one thing the discriminator actually reads at hashchange time
    // (history.state already carrying this module's own stamp, and a URL
    // whose ctx differs from the currently-pinned focus), the same shape
    // a real Back to an earlier, already-visited, ctx-less entry produces.
    // Wrapped in `act`: dispatching synchronously triggers FocusProvider's
    // `setFocusState` outside any Testing Library helper that would
    // otherwise wrap it.
    act(() => {
      window.history.replaceState({ ottoCov: true }, "", "#/coverage/bar");
      window.dispatchEvent(new Event("hashchange"));
    });

    expect(screen.getByTestId("focus-value").textContent).toBe("null");
    expect(localStorage.getItem("otto-cov:stamp-1:focus")).toBeNull();
  });

  it("adopting a stamped entry can switch DIRECTLY to a different known label, not just clear", () => {
    window.__OTTO_COV__ = buildIndex();
    window.location.hash = "#/coverage?ctx=ctx-a";
    renderConsumer();
    expect(screen.getByTestId("focus-value").textContent).toBe("ctx-a");

    act(() => {
      window.history.replaceState({ ottoCov: true }, "", "#/coverage/bar?ctx=ctx-b");
      window.dispatchEvent(new Event("hashchange"));
    });

    expect(screen.getByTestId("focus-value").textContent).toBe("ctx-b");
    expect(localStorage.getItem("otto-cov:stamp-1:focus")).toBe("ctx-b");
  });

  // Reviewer note: the adopt branch's `resolveLabel` degrade path was
  // untested — a stamped (genuinely-visited) entry whose ctx names a
  // label that no longer resolves (report regenerated with a different
  // run set, or a hand-edited deep link) must still degrade to cleared,
  // the same "no crash, treated as cleared" contract boot already has.
  it("adopting a stamped entry whose ctx names an unknown/stale label degrades to cleared, not a crash", () => {
    window.__OTTO_COV__ = buildIndex();
    window.location.hash = "#/coverage?ctx=ctx-a";
    renderConsumer();
    expect(screen.getByTestId("focus-value").textContent).toBe("ctx-a");

    window.history.replaceState({ ottoCov: true }, "", "#/coverage/bar?ctx=ghost-context");
    expect(() => {
      act(() => {
        window.dispatchEvent(new Event("hashchange"));
      });
    }).not.toThrow();

    expect(screen.getByTestId("focus-value").textContent).toBe("null");
    expect(localStorage.getItem("otto-cov:stamp-1:focus")).toBeNull();
  });

  it("replaceHashQuery (via replaceHash) preserves an existing stamp instead of clobbering it with null", () => {
    window.__OTTO_COV__ = buildIndex();
    window.location.hash = "#/coverage?ctx=ctx-a";
    renderConsumer();
    expect(window.history.state).toEqual({ ottoCov: true }); // boot stamps it

    // A reconciliation rewrite of the SAME (already-stamped) entry, e.g.
    // the navigation-survival re-append — must not lose the stamp. Uses an
    // actual change (ctx-a -> ctx-b) so `replaceHashQuery`'s internal
    // "no-op if nothing changed" guard doesn't skip the underlying
    // `history.replaceState` call this test needs to exercise. Wrapped in
    // `act`: rewriting an already-stamped entry's ctx also triggers
    // FocusProvider's own adopt branch (setFocusState) synchronously.
    act(() => {
      replaceHashQuery((p) => p.set("ctx", "ctx-b"));
    });

    expect(window.location.hash).toBe("#/coverage?ctx=ctx-b");
    expect(window.history.state).toEqual({ ottoCov: true });
  });

  it("the real reassert-branch sequence (replaceHashQuery then stampCurrentEntry) ends with BOTH the corrected ctx and the stamp", async () => {
    window.__OTTO_COV__ = buildIndex();
    window.location.hash = "#/coverage?ctx=ctx-a";
    renderConsumer();
    expect(window.history.state).toEqual({ ottoCov: true }); // boot stamps it

    // A plain in-app push (fresh entry, null state) that drops ctx — the
    // reassert branch fires `replaceHashQuery` (rewriting this brand-new,
    // still-unstamped entry) and then `stampCurrentEntry` (marking it):
    // the two writes must compose onto the SAME entry, not one
    // overwriting the other's work.
    window.location.hash = "#/coverage/foo";
    expect(window.history.state ?? null).toBeNull(); // sanity: fresh push, unstamped

    await waitFor(() => expect(window.location.hash).toBe("#/coverage/foo?ctx=ctx-a"));
    expect(window.history.state).toEqual({ ottoCov: true });
  });
});
