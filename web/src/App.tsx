// Bootstraps the dashboard: mirrors dashboard.js's init() — fetch /api/meta
// + /api/data, hydrate the store, then open the SSE stream. Renders the
// chrome (Header/TabBar) and the chart grid (ChartGrid), which gates its own
// chart creation on host selection in live mode (mirrors dashboard.js's
// populateHostSelect change handler).
import { useEffect, useState } from "react";

import { fetchData, fetchMeta } from "./api/client";
import { startSse } from "./api/sse";
import ChartGrid from "./components/ChartGrid";
import EventPopover from "./components/EventPopover";
import EventToolbar from "./components/EventToolbar";
import Header from "./components/Header";
import TabBar from "./components/TabBar";
import { useMonitorActions } from "./store";

function App() {
  const { applyData, applyMeta } = useMonitorActions();
  // dashboard.js's `init().catch(err => { ... })` — set once the bootstrap
  // fetch/parse fails, and rendered into `#tab-bar` in place of `<TabBar/>`
  // below (legacy overwrites `#tab-bar`'s `textContent` directly, since it
  // has no component tree to branch on).
  const [bootstrapError, setBootstrapError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    let sse: EventSource | undefined;

    async function bootstrap() {
      try {
        // Both requests fire concurrently (as `Promise.all` did), but each
        // is awaited separately so `document.body.classList` gets the
        // `historical` toggle the instant /api/meta resolves — mirroring
        // dashboard.js's `init()`, which awaits `metaRes.json()` and adds
        // the class BEFORE awaiting `dataRes.json()`, rather than gating it
        // on whichever of the two is slower. This write is imperative DOM
        // (not React state) specifically so it can happen independently of
        // `applyMeta`/`applyData` below, which — unlike this class toggle —
        // MUST land in the same tick: ChartGrid's one-shot chart-build
        // effect keys off `meta` alone, so if `applyMeta` committed a
        // render before `applyData` populated `series`/`chartMap`, it would
        // build historical chart groups against empty data and never
        // retry (see grouping.ts's `initSeriesFromData` / ChartGrid's
        // `builtRef`). React 18+ auto-batches same-tick `set()` calls with
        // no `await` between them, so keeping these two adjacent (as the
        // original `Promise.all`-then-apply-both code did) preserves that
        // atomicity.
        const metaPromise = fetchMeta();
        const dataPromise = fetchData();
        // If `metaPromise` rejects, the catch below fires and `await
        // dataPromise` is never reached — when the backend is fully down
        // (BOTH fetches rejecting, the common case), that would leave
        // dataPromise's rejection UNHANDLED. Legacy's `Promise.all` attached
        // rejection handling to both promises atomically; this no-op handler
        // restores that guarantee. It marks the promise handled without
        // swallowing anything: the success path's `await dataPromise` below
        // awaits the ORIGINAL promise, so a data-only failure still throws
        // into the same catch.
        void dataPromise.catch(() => {
          // no-op — the real error surfaces via `await dataPromise` below
        });
        const meta = await metaPromise;
        if (cancelled) return;
        document.body.classList.toggle("historical", !meta.live);
        const data = await dataPromise;
        if (cancelled) return;
        applyMeta(meta);
        applyData(data);
        sse = startSse();
      } catch (err) {
        if (!cancelled) setBootstrapError(String(err));
      }
    }

    void bootstrap();

    return () => {
      cancelled = true;
      sse?.close();
    };
  }, [applyData, applyMeta]);

  return (
    <>
      <Header />
      {bootstrapError !== null ? (
        <nav id="tab-bar">{`Error loading dashboard: ${bootstrapError}`}</nav>
      ) : (
        <TabBar />
      )}
      <EventToolbar />
      <EventPopover />
      <ChartGrid />
    </>
  );
}

export default App;
