// covapp shell. App renders GuardScreen instead of the router whenever
// dataGuard() != "ok" — the classic `cov_data/index.js` script (loaded
// before the app bundle, see covapp.html) either never ran or produced a
// payload this build can't read.
//
// "/coverage" and "/coverage/*" share one dispatcher (CoverageRoute):
// `findNode` resolves the (possibly decoded, possibly empty) path segments
// against the tree, then renders DirectoryPage for a DirNode, FilePage for a
// FileNode, or NotFoundPlaceholder when nothing resolves.
import { Route, Router, Switch, useParams } from "wouter";

import { dataGuard, getIndex } from "./data";
import { FocusProvider, useHashLocation } from "./focus";
import { DirectoryPage } from "./pages/DirectoryPage";
import { FilePage } from "./pages/FilePage";
import { GuardScreen } from "./pages/GuardScreen";
import { RunsPage } from "./pages/RunsPage";
import { TicketsPage } from "./pages/TicketsPage";
import { findNode } from "./stats";
import { ToastProvider } from "./Toast";
import type { IndexPayload } from "./types";

/** wouter's OWN router already runs the whole location string through a
 * fail-safe `decodeURI` before route matching (`relativePath`/`unescape` in
 * wouter/src/paths.js's `useLocationFromRouter`) — so by the time `raw`
 * (the wildcard capture) reaches here, ordinary percent-escapes (e.g. a "%"
 * in a name, written as "%25" by `DirectoryPage`'s `encodePath`) are
 * ALREADY resolved. `decodeURI` deliberately leaves one reserved set
 * (`;/?:@&=+$,#`) still percent-encoded, since those characters are
 * significant to URI *structure*, not just content — a dir/file name
 * containing one of them (e.g. "#") arrives here as literal "%23". This
 * finishes decoding exactly that residual case. It must be fail-safe like
 * wouter's own `unescape`: re-running `decodeURIComponent` on a segment
 * wouter ALREADY fully resolved can hit a raw "%" that isn't part of any
 * escape (e.g. "100%.c") and throw `URIError` — caught here, falling back
 * to the (already-correct) segment, exactly as wouter's own decode does on
 * failure. */
function finishDecodeSegment(segment: string): string {
  try {
    return decodeURIComponent(segment);
  } catch {
    return segment;
  }
}

/** Splits a wouter wildcard capture into path segments, finishing the
 * decode wouter's own router already started (see `finishDecodeSegment`) —
 * `findNode` compares against raw `DirNode.name`/`FileNode.name`
 * strings. */
function segmentsFromWildcard(raw: string | undefined): string[] {
  return (raw ?? "").split("/").filter(Boolean).map(finishDecodeSegment);
}

function NotFoundPlaceholder() {
  return (
    <main data-testid="not-found" className="p-4 text-sm text-tertiary">
      Not found. <a href="#/coverage">Go to coverage</a>
    </main>
  );
}

/** Shared dispatcher for both the exact "/coverage" route (segments `[]`)
 * and the "/coverage/*" wildcard. */
function CoverageRoute({ index, segments }: { index: IndexPayload; segments: string[] }) {
  const node = findNode(index.tree, segments);
  if (node === null) return <NotFoundPlaceholder />;
  if ("dirs" in node) return <DirectoryPage index={index} segments={segments} />;
  return <FilePage index={index} segments={segments} node={node} />;
}

function CoverageWildcardRoute({ index }: { index: IndexPayload }) {
  const params = useParams<{ "*": string }>();
  return <CoverageRoute index={index} segments={segmentsFromWildcard(params["*"])} />;
}

function CoverageApp() {
  const guard = dataGuard();
  if (guard !== "ok") {
    return (
      <GuardScreen reason={guard === "missing" ? "missing data" : "unsupported data format"} />
    );
  }
  // dataGuard() === "ok" guarantees getIndex() is non-null; the fallback
  // below is unreachable in practice and exists only so TypeScript doesn't
  // need a non-null assertion at every call site below.
  const index = getIndex();
  if (index === null) {
    return <GuardScreen reason="missing data" />;
  }
  return (
    <Router hook={useHashLocation}>
      <Switch>
        <Route path="/coverage">
          <CoverageRoute index={index} segments={[]} />
        </Route>
        <Route path="/coverage/*">
          <CoverageWildcardRoute index={index} />
        </Route>
        <Route path="/runs">
          <RunsPage index={index} />
        </Route>
        <Route path="/tickets">
          <TicketsPage index={index} />
        </Route>
        <Route>
          <NotFoundPlaceholder />
        </Route>
      </Switch>
    </Router>
  );
}

/** Providers mounted ONCE at the true app root — `FocusProvider` calls
 * `useToast()` itself (see focus.tsx), so it must nest inside
 * `ToastProvider`, not the other way round. Wrapping unconditionally
 * (rather than only inside the `dataGuard() === "ok"` branch) keeps this a
 * single mount point regardless of guard state — harmless when
 * `GuardScreen` renders instead of the router, since nothing in that
 * branch reads focus/toast. `<Router hook={useHashLocation}>` above uses
 * focus.tsx's own hash-location hook (NOT wouter's `wouter/use-hash-
 * location`) — see focus.tsx's header comment for why that swap is load-
 * bearing once `?ctx=` lives inside the hash fragment. */
function App() {
  return (
    <ToastProvider>
      <FocusProvider>
        <CoverageApp />
      </FocusProvider>
    </ToastProvider>
  );
}

export default App;
