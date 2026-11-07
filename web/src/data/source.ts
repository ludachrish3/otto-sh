// The ONE owner of the shell's data-source lifecycle (#873). The shell reads
// from exactly one source at a time: the otto server it booted against
// (bootstrap.ts: the /api/mode + /api/monitor_sessions fetches, then the SSE
// stream in live mode), or a file the user imported. A file import is a
// source SWITCH, not a data refresh: once a file has loaded, nothing the
// previous server source started may touch the store again.
//
// The generation counter is how async server work knows it has been
// superseded. Every piece of it — bootstrapFromServer, the stream's resync,
// each event mutation in eventApi.ts — captures `sourceGeneration()` before
// its first await and applies its result only while `isCurrentSource` still
// holds, re-checked after EVERY await, in the continuation that writes
// (mutation callers do this through eventApi's sourceGuard).
// `detachServerSource` bumps the generation, so everything in flight
// becomes a no-op the moment the file lands, whatever order the responses
// arrive in.
import { useUiStore } from "../ui/uiStore";
import { useReviewStore } from "./reviewStore";

let generation = 0;
let stopStream: (() => void) | null = null;
// Separate from `generation`: two quick imports race each other's file
// reads, not the server, and a parse failure must not bump the generation.
let importTicket = 0;

export function sourceGeneration(): number {
  return generation;
}

export function isCurrentSource(gen: number): boolean {
  return gen === generation;
}

/** Records the stopper startStream returned, so a source switch can close it. */
export function registerStreamStop(stop: () => void): void {
  stopStream = stop;
}

/** Cuts the server source off, in one synchronous turn: in-flight server
 * work goes stale, the stream closes, the shell drops to read-only review,
 * and every piece of UI state bound to the server (the open span, the
 * editor, the sweep, the mark popover) is cleared; setEditable(false) also
 * clears the undo history. */
function detachServerSource(): void {
  generation += 1;
  const stop = stopStream;
  stopStream = null;
  stop?.();
  const review = useReviewStore.getState().actions;
  review.setMode("review");
  review.setEditable(false);
  const ui = useUiStore.getState().actions;
  // A source switch resets live UI state synchronously: a decision of its
  // own, apart from data/openSpan.ts's ended-or-vanished rule.
  ui.setOpenSpan(null);
  ui.closeEventEditor();
  ui.disarmSweep();
  ui.closeMarkPopover();
}

/** Every UI import path (the file picker, drag and drop) comes through here.
 * The newest import wins; a read failure surfaces through `importError`; a
 * file that fails to parse leaves the current source untouched. The server
 * hydrate does not come through here: it calls importMonitorSessions itself. */
export async function importLocalFile(file: File): Promise<void> {
  importTicket += 1;
  const ticket = importTicket;
  let text: string;
  try {
    text = await file.text();
  } catch (err) {
    if (ticket !== importTicket) return;
    useReviewStore
      .getState()
      .actions.setImportError(`Import failed: could not read ${file.name}: ${String(err)}`);
    return;
  }
  if (ticket !== importTicket) return;
  if (useReviewStore.getState().actions.importMonitorSessions(text, file.name)) {
    detachServerSource();
  }
}
