// The redesigned shell (plan 2026-07-11). Review-first: no backend fetch
// on boot — the Import front door hydrates the review store. Live mode
// (SSE, /api/meta) returns at the live-hookup phase; the legacy live data
// layer (store.ts/api/sse.ts) is intentionally kept, unreferenced, for it.

import { Route, Router, Switch } from "wouter";
import { useHashLocation } from "wouter/use-hash-location";

import { useReviewStore } from "./data/reviewStore";
import { OverviewPage } from "./pages/OverviewPage";
import { SubjectPage } from "./pages/SubjectPage";
import { TopologyPage } from "./pages/TopologyPage";
import { AppBar } from "./shell/AppBar";
import { EmptyState } from "./shell/EmptyState";
import { ImportProvider } from "./shell/ImportExport";
import { ReviewBar } from "./shell/ReviewBar";

function App() {
  const hasData = useReviewStore((s) => s.sessions.length > 0);
  const importError = useReviewStore((s) => s.importError);
  const clearImportError = useReviewStore((s) => s.actions.clearImportError);
  return (
    <ImportProvider>
      <AppBar />
      {hasData ? (
        <Router hook={useHashLocation}>
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
          <Switch>
            <Route path="/" component={OverviewPage} />
            <Route path="/host/:id" component={SubjectPage} />
            <Route path="/topology" component={TopologyPage} />
            <Route>
              <main data-testid="not-found" className="p-4 text-sm text-gray-500">
                Not found.
              </main>
            </Route>
          </Switch>
        </Router>
      ) : (
        <EmptyState />
      )}
    </ImportProvider>
  );
}

export default App;
