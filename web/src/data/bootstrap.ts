// The shell's boot against a same-origin otto monitor server, in every
// streamed mode (`otto monitor --live`, or `otto monitor <source>` over a
// saved .json/.db export). In order:
//
//  1. Open the SSE stream (data/stream.ts) FIRST, and wait for its first
//     open: the server subscribes a connection before its first byte, so
//     from then on every push reaches the tab.
//  2. Run the one server sync (data/source.ts's syncFromServer, fresh):
//     `/api/mode`, then `/api/monitor_sessions`, installed with every push
//     stamped after the snapshot's capture replayed onto it. Nothing
//     published between the snapshot and the stream is lost, because there
//     is no such gap: the stream was open before the snapshot was read.
//  3. Every later open (a reconnect) runs the same sync, not fresh, so the
//     user's paused/pinned view survives it (data/source.ts's resyncOnOpen,
//     which also forces the next reconnect when that sync could not
//     install); a source frame is admitted through admitSourceFrame.
//
// Until the boot learns what serves the URL, the URL's source is unresolved
// (the store's `sourceKindUnresolved`) and Import is withheld: the first
// `/api/mode` answer names the source (it is pending from then on, whatever
// its snapshot does), and a stream that fails before it ever opened means
// no otto server serves the URL: no server source, where Import loads a
// file in the tab.
//
// Soft-fail contract (binding): the built dist/ is also served by dumb
// static file servers with no /api/* routes at all (ad-hoc demo serving,
// the e2e suite's no-server shell, the docs-capture script), and the offline
// Playwright pin blocks every non-local request outright. Both depend on
// this module leaving the shell exactly as it behaves without it whenever a
// fetch can't succeed. So a stream that fails before it ever opens (no
// /api/stream) stops, with no reconnect, nothing is fetched, and the empty
// Import shell stays. That is the ONE "no server" outcome.
//
// A stream that opened is an otto server, so nothing it does after that
// closes the stream: a `/api/mode` or a snapshot that could not be read (a
// live server with nothing recording yet answers the snapshot with a 404)
// or a snapshot that named another source twice is retried through the
// stream's reconnect, whose open syncs again, and `mode` stays null
// meanwhile: a shell that never got any data must not claim it is live. A
// 200 snapshot whose body fails validation is not a transport failure: it
// surfaces through the store's `importError`, like a bad file chosen
// through Import, and the stream stays open for the next reconnect or
// source frame to recover.
//
// A source frame admitted during the boot sync supersedes it (see
// data/source.ts): it bumps the source generation, so every step here in
// flight gives up silently; the admission installs, and the boot leaves the
// stream open for it.

import { useReviewStore } from "./reviewStore";
import {
  admitSourceFrame,
  registerStream,
  resyncOnOpen,
  retriesSync,
  syncFromServer,
} from "./source";
import { isCurrentSource, sourceGeneration } from "./sourceGeneration";
import { startStream } from "./stream";

export async function bootstrapFromServer(): Promise<void> {
  const review = useReviewStore.getState().actions;
  review.setSourceKindUnresolved(true);
  const generation = sourceGeneration();
  let opened = false;
  let settleFirstOpen!: (opened: boolean) => void;
  const firstOpen = new Promise<boolean>((resolve) => {
    settleFirstOpen = resolve;
  });
  // The first open's sync is the boot's own, below; the open itself is
  // healthy.
  const firstOpened = (): undefined => {
    opened = true;
    settleFirstOpen(true);
  };
  const control = startStream({
    // A reconnect's open is healthy once its resync installs (the stream
    // resets its backoff when such a connection ends having lost no frame).
    onOpen: () => (opened ? resyncOnOpen() : firstOpened()),
    // No otto server serves the URL: the one way the boot concludes that.
    onFirstError: () => {
      review.setSourceKindUnresolved(false);
      settleFirstOpen(false);
    },
    onSource: admitSourceFrame,
  });
  // A stream closed before its first open ends the boot too.
  const stop = () => {
    control.stop();
    settleFirstOpen(false);
  };
  registerStream({ stop, reconnect: control.reconnect, lost: control.lost });

  if (!(await firstOpen) || !isCurrentSource(generation)) return;
  const result = await syncFromServer({ fresh: true });
  // `stale`: the tab moved on meanwhile (a source frame's admission, which
  // now owns the install). Any failure is a server that is there: retry
  // through the stream's reconnect, whose open resyncs, or (`invalid`) keep
  // the stream open.
  if (retriesSync(result)) control.reconnect();
}
