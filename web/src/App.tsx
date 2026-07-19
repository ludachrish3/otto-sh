// The redesigned shell (plan 2026-07-11). Review-first: the Import front
// door hydrates the review store, same as always. The one addition (Plan
// 5a Task 6) is a single soft-failing boot fetch — see
// data/bootstrap.ts's header for the full contract — that hydrates from a
// same-origin otto monitor server already sitting on a review document (or
// a running live session — both modes hydrate through the same
// /api/monitor_sessions endpoint and format:1 shape), so an `otto monitor
// <source>` review server, or an `otto monitor --live` server, opens
// straight into the dashboard. In live mode the shell then attaches
// data/stream.ts's SSE client (/api/stream) to grow that same session in
// place (Plan 5b) — a new streaming layer built against the format:1
// session shape, not a revival of the legacy Plotly-era live data layer
// (the zustand store, SSE client, and REST client) deleted in Task 12.

import { useEffect } from "react";
import { Route, Router, Switch } from "wouter";
import { useHashLocation } from "wouter/use-hash-location";

import { bootstrapFromServer } from "./data/bootstrap";
import { useReviewStore } from "./data/reviewStore";
import { OverviewPage } from "./pages/OverviewPage";
import { SubjectPage } from "./pages/SubjectPage";
import { AppBar } from "./shell/AppBar";
import { DataWarningsBanner } from "./shell/DataWarningsBanner";
import { EmptyState } from "./shell/EmptyState";
import { EventEditor } from "./shell/EventEditor";
import { ImportProvider } from "./shell/ImportExport";
import { ReconnectingBanner } from "./shell/ReconnectingBanner";
import { ReviewBar } from "./shell/ReviewBar";
import { TopologyPage } from "./topo/TopologyPage";
import { CommandLayer } from "./ui/CommandLayer";

function App() {
  const hasData = useReviewStore((s) => s.sessions.length > 0);
  const importError = useReviewStore((s) => s.importError);
  const clearImportError = useReviewStore((s) => s.actions.clearImportError);
  // One-shot, fire-and-forget: bootstrapFromServer never throws (see its
  // header) and Import remains available whether or not it finds anything.
  useEffect(() => {
    void bootstrapFromServer();
  }, []);
  return (
    <ImportProvider>
      <div className="flex min-h-screen flex-col">
        <AppBar />
        <ReconnectingBanner />
        {hasData ? (
          <Router hook={useHashLocation}>
            <CommandLayer />
            <EventEditor />
            <ReviewBar />
            {importError !== null && (
              <div
                data-testid="import-error"
                className="flex items-center justify-between gap-3 border-b border-status-warn/30
                  bg-status-warn/10 px-4 py-2 text-sm text-status-warn dark:bg-status-warn/15"
              >
                <span>{importError}</span>
                <button
                  type="button"
                  data-testid="import-error-dismiss"
                  onClick={clearImportError}
                  className="cursor-pointer rounded-md px-2 py-0.5 font-medium underline-offset-2
                    hover:underline"
                >
                  Dismiss
                </button>
              </div>
            )}
            <DataWarningsBanner />
            <Switch>
              <Route path="/" component={TopologyPage} />
              <Route path="/hosts" component={OverviewPage} />
              <Route path="/host/:id" component={SubjectPage} />
              <Route path="/topology" component={TopologyPage} />
              <Route path="/topology/:elementId" component={TopologyPage} />
              <Route>
                <main data-testid="not-found" className="p-4 text-sm text-tertiary">
                  Not found.
                </main>
              </Route>
            </Switch>
          </Router>
        ) : (
          <EmptyState />
        )}
      </div>
    </ImportProvider>
  );
}

export default App;
