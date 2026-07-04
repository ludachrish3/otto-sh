// Permanent regression guard for the zustand v5 selector crash: components
// that fall back to a fresh `[]`/`{}` literal inside a `useMonitorStore`
// selector (e.g. `s.meta?.hosts ?? []`) return a NEW reference on every
// call, which — under zustand v5's `useSyncExternalStore`-backed
// subscriptions — makes every render see a "changed" snapshot and spins
// into React error #185's infinite update loop. That crash is only
// reachable by actually mounting the component tree while `meta` is still
// null (the window between first render and the `/api/meta` fetch
// resolving) — store.test.ts's reducer-only tests never exercise it. This
// test renders the real `<App/>` against a mocked backend so a regression
// here (see Header.tsx's EMPTY_HOSTS / TabBar.tsx's EMPTY_TABS/EMPTY_METRICS)
// fails loudly instead of only showing up as a blank page in the browser.
import { cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { DataPayload } from "../api/client";
import type { MonitorDashboardApiMetaPayload } from "../api/types.gen";
import App from "../App";
import { useMonitorStore } from "../store";

const META: MonitorDashboardApiMetaPayload = {
  hosts: ["host1"],
  live: true,
  metrics: [{ label: "Overall CPU", y_title: "%", unit: "%", command: "cpu", chart: "cpu" }],
  tabs: [{ id: "system", label: "System", metrics: ["Overall CPU"] }],
  interval: null,
};

const DATA: DataPayload = { series: {}, events: [], chart_map: {}, log_events: [] };

function jsonResponse(body: unknown): Response {
  return { ok: true, json: () => Promise.resolve(body) } as Response;
}

// Minimal EventSource stub — App only ever assigns `.onopen`/`.onmessage`/
// `.onerror` and calls `.close()` on unmount (see api/sse.ts's `startSse`);
// nothing here needs to actually fire for this smoke test.
class StubEventSource {
  onopen: (() => void) | null = null;
  onmessage: ((ev: MessageEvent<string>) => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(public readonly url: string) {}

  close(): void {
    // no-op
  }
}

// The zustand store is a module-level singleton shared by every test in
// this file (unlike DOM/globals, it isn't reset by @testing-library's
// cleanup()) — each <App/> mount here calls the real applyMeta/applyData
// actions, so a prior test's successful bootstrap would otherwise leave
// stale `meta` sitting in the store for the next test's fresh render.
// Mirrors store.test.ts's resetStore() helper.
function resetStore(): void {
  useMonitorStore.setState({
    meta: null,
    series: {},
    events: [],
    chartMap: {},
    logEvents: {},
    activeTab: null,
    selectedHost: null,
    paused: false,
    connection: "connecting",
    spanStartId: null,
    editingEventId: null,
    popoverAnchor: null,
    everLive: false,
    chartsInitialized: false,
    lastMetric: null,
  });
}

beforeEach(() => {
  resetStore();
  vi.stubGlobal(
    "fetch",
    vi.fn((input: string | URL | Request) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.includes("/api/meta")) return Promise.resolve(jsonResponse(META));
      if (url.includes("/api/data")) return Promise.resolve(jsonResponse(DATA));
      return Promise.reject(new Error(`app.test.tsx: unexpected fetch ${url}`));
    }),
  );
  vi.stubGlobal("EventSource", StubEventSource);
});

afterEach(() => {
  // vitest's config doesn't set `test.globals: true`, so
  // @testing-library/react's automatic afterEach(cleanup) registration
  // (which only fires when it finds `afterEach` on globalThis) never kicks
  // in — without this, each render() below would leave its <App/> mounted
  // and a later test's `document.getElementById(...)` could resolve against
  // a PRIOR test's stale DOM node instead of its own.
  cleanup();
  vi.unstubAllGlobals();
  document.body.className = "";
});

describe("App (zustand selector-stability smoke test)", () => {
  it("renders without throwing and #status-label is reachable once /api/meta resolves", async () => {
    expect(() => {
      render(<App />);
    }).not.toThrow();

    await waitFor(() => {
      expect(document.getElementById("status-label")).not.toBeNull();
    });
  });
});

// Task 8 (T5-review known gap): dashboard.js's bootstrap is
// `init().catch(err => { document.getElementById('tab-bar').textContent =
// Error loading dashboard: ${err} }); ` — a failed /api/meta or /api/data
// fetch (or a JSON parse failure) must surface as visible text in #tab-bar,
// not a silently blank/broken page.
describe("App bootstrap error path (dashboard.js's init().catch parity)", () => {
  it("renders 'Error loading dashboard: <err>' into #tab-bar when /api/meta fails", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: string | URL | Request) => {
        const url = typeof input === "string" ? input : input.toString();
        if (url.includes("/api/meta")) return Promise.reject(new Error("boom"));
        if (url.includes("/api/data")) return Promise.resolve(jsonResponse(DATA));
        return Promise.reject(new Error(`app.test.tsx: unexpected fetch ${url}`));
      }),
    );

    render(<App />);

    await waitFor(() => {
      expect(document.getElementById("tab-bar")?.textContent).toBe("Error loading dashboard: Error: boom");
    });
    // The error state replaces <TabBar/> entirely — no tab buttons render.
    expect(document.querySelectorAll(".tab-btn")).toHaveLength(0);
  });

  it("renders the error text when /api/data fails even though /api/meta succeeded", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: string | URL | Request) => {
        const url = typeof input === "string" ? input : input.toString();
        if (url.includes("/api/meta")) return Promise.resolve(jsonResponse(META));
        if (url.includes("/api/data")) return Promise.reject(new Error("data unavailable"));
        return Promise.reject(new Error(`app.test.tsx: unexpected fetch ${url}`));
      }),
    );

    render(<App />);

    await waitFor(() => {
      expect(document.getElementById("tab-bar")?.textContent).toBe(
        "Error loading dashboard: Error: data unavailable",
      );
    });
  });

  // The backend-fully-down combination: BOTH fetches reject (the common real
  // failure — two independent requests to one dead server). When
  // `metaPromise` rejects, bootstrap()'s control flow jumps straight to its
  // catch and never reaches `await dataPromise` — so unless a rejection
  // handler was attached to `dataPromise` at creation time (see App.tsx's
  // no-op `.catch`), its own rejection, arriving a tick later, fires as a
  // genuine unhandled promise rejection. Legacy dashboard.js used
  // `Promise.all([...])`, which attaches handling to both promises
  // atomically, so it never had this hole. Node emits 'unhandledRejection'
  // once the microtask queue drains with a rejection still unhandled; the
  // process listener + setTimeout(0) hops below catch that
  // deterministically.
  it("leaves no unhandled rejection when BOTH /api/meta and /api/data reject", async () => {
    const unhandled: unknown[] = [];
    const onUnhandled = (reason: unknown): void => {
      unhandled.push(reason);
    };
    process.on("unhandledRejection", onUnhandled);
    try {
      vi.stubGlobal(
        "fetch",
        vi.fn((input: string | URL | Request) => {
          const url = typeof input === "string" ? input : input.toString();
          if (url.includes("/api/meta")) return Promise.reject(new Error("meta down"));
          if (url.includes("/api/data")) return Promise.reject(new Error("data down"));
          return Promise.reject(new Error(`app.test.tsx: unexpected fetch ${url}`));
        }),
      );

      render(<App />);

      // The user-visible error is whichever await threw first — metaPromise
      // is awaited first, so its rejection wins.
      await waitFor(() => {
        expect(document.getElementById("tab-bar")?.textContent).toBe(
          "Error loading dashboard: Error: meta down",
        );
      });

      // Two macrotask hops: the first lets the microtask queue fully drain
      // (dataPromise's rejection settles, handler or no handler), the second
      // gives Node's process-level 'unhandledRejection' emission — scheduled
      // after that drain — time to reach the listener.
      await new Promise((resolve) => setTimeout(resolve, 0));
      await new Promise((resolve) => setTimeout(resolve, 0));

      expect(unhandled).toHaveLength(0);
    } finally {
      process.off("unhandledRejection", onUnhandled);
    }
  });
});
