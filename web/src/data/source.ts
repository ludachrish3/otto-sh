// The ONE owner of the shell's data-source lifecycle (#873, #958). The shell
// reads from exactly one source at a time: the source the otto server serves
// (a live run, or a .db/.json under review), or a file the user loaded in
// the browser. Every way the tab moves between sources comes through here:
//
//  - `syncFromServer` is the ONE server sync: the boot, and every reconnect.
//  - `admitSource` admits a source state that arrives on its own: an upload
//    response, a stream frame, a `source_changed` refusal.
//  - `importLocalFile` loads a file into the tab itself, which leaves the
//    server source behind; on a `.json` source it opens the switch prompt
//    instead, and `requestSourceSwitch` asks the server to serve the file.
//
// Each source change starts in one synchronous turn (`supersede`): the
// generation moves on and every piece of UI state bound to the old source
// (the undo history, the open span, the editor, the sweep, the mark popover)
// is cleared. A new server source is then PENDING until its snapshot has
// installed (`finishInstall`): until then the tab cannot write, the token
// its writes send is still the installed source's, and the pushes the
// stream delivers belong to the pending source (reviewStore.ts logs them for
// its replay and never applies them to the old document).
//
// A sync records the URL's source the moment `/api/mode` answers, before its
// snapshot is read: from then on Import is judged by that source's kind
// (`importOffered`), whether or not a snapshot ever installs. Before the
// first answer the URL's source is unresolved, and Import waits: a sync runs
// only over an open stream, so an otto server is there, and a mode or a
// snapshot that fails is retried (`retriesSync`), never the end of the
// boot. Only a stream that never opened means no server (data/bootstrap.ts).
//
// Two syncs of the same source can be in flight at once (a same-token sync
// does not move the generation): a sync is current only while it owns the
// generation AND is the newest begun (`isCurrentSync`), and an obsolete one
// acts on nothing, so a late older answer never adopts, nor replaces a newer
// snapshot.
//
// The source-changed notice belongs to the transition (`beginTransition`
// decides it, `finishInstall` shows it once the new source has installed):
// none on the first boot, none for this tab's own switch, one for every
// switch the tab did not ask for, however it learnt of it (a stream frame, a
// refused write, a reconnect's resync).
//
// The generation counter (data/sourceGeneration.ts) is how async work knows
// it has been superseded. Every piece of it (a sync, an admission, each
// event mutation in eventApi.ts) captures `sourceGeneration()` before its
// first await and applies its result only while `isCurrentSource` still
// holds, re-checked after EVERY await, in the continuation that writes
// (mutation callers do this through eventApi's sourceGuard). A source change
// moves the generation on, so everything in flight becomes a no-op the
// moment it starts, whatever order the responses arrive in.
//
// Ordering. Every source state carries the server's stamp (`observed_at`).
// `lastSourceStamp` is the newest one the tab has admitted; a state that is
// not after it is obsolete, so a late upload response or a delayed frame
// never moves the tab backwards, and never lowers newer counts.
import type { MonitorSourceState } from "../api/export.gen";
import { useToastStore } from "../ui/Toast";
import { type SwitchPrompt, useUiStore } from "../ui/uiStore";
import { observedAtMicros } from "./exportDoc";
import { isCurrentResync, type SnapshotInstall, useReviewStore } from "./reviewStore";
import { fetchMode, fetchSnapshot, type Snapshot } from "./snapshot";
import { type UploadResult, uploadSource } from "./sourceApi";
import { isCurrentSource, nextGeneration, sourceGeneration } from "./sourceGeneration";
import type { StreamControl } from "./stream";
import { useUndoHistory } from "./undoHistory";

let stream: StreamControl | null = null;
// The tab's own switch requests in flight: their source frames are the
// switch the user asked for, not a change to announce (beginTransition).
let ownSwitches = 0;
// Whether the pending transition is one to announce once it installs: set by
// `beginTransition`, spent by `finishInstall`.
let announce = false;
// Separate from the generation: two quick imports race each other's file
// reads, not the server, and a parse failure must not bump the generation.
let importTicket = 0;

/** Records the stream startStream opened: a local file closes it, a sync
 * that could not install reconnects it (`retryOnReconnect`), and an install
 * whose replay lost a fragment reports the loss to it (`finishInstall`). */
export function registerStream(control: StreamControl): void {
  stream = control;
}

/** How a sync ended:
 *  - `installed`: the served source is installed;
 *  - `stale`: the sync is obsolete (`isCurrentSync`): the tab moved onto
 *    another source meanwhile, which owns it now, or a newer sync has begun,
 *    which installs instead;
 *  - `unavailable`: the mode or the snapshot could not be read
 *    (data/snapshot.ts's soft-fail contract); a source whose mode answered
 *    stays pending;
 *  - `mismatch`: the snapshot named another source than the mode, twice;
 *    or the sync went obsolete after a newer mode named another source
 *    than the one the tab now serves (`obsoleteResult`), which a retry
 *    adopts;
 *  - `invalid`: the snapshot did not parse (shown through `importError`). */
export type SyncResult = "installed" | "stale" | "unavailable" | "mismatch" | "invalid";

/** The ONE retry of a sync or an admission that could not install: drop the
 * stream's connection and reconnect it through its own backoff
 * (data/stream.ts), so the new connection's open runs `syncFromServer`
 * again. A server that is there but answered badly is retried this way
 * without waiting for the stream to drop by itself. */
function retryOnReconnect(): void {
  stream?.reconnect();
}

/** How the tab names a server source to the user. */
function sourceLabel(state: MonitorSourceState): string {
  return state.source ?? "the live session";
}

/** The toast a tab shows when it moved onto another source it did not ask
 * for (`finishInstall`). */
function sourceChangedNotice(state: MonitorSourceState): string {
  return `Source changed to ${sourceLabel(state)}`;
}

/** The ONE warning that the pending source `state`'s snapshot could not be
 * read, so the tab stays read-only until a retry installs it: an
 * admission's and a sync's adoption alike. It shows when the tab left an
 * installed source for it (on a first boot there is nothing to leave, and
 * the empty state says the tab is waiting), and once until an install
 * replaces the warnings channel, however many retries fail meanwhile. */
function warnPendingUnavailable(state: MonitorSourceState): void {
  const { actions, source, warnings } = useReviewStore.getState();
  const message = `Could not load ${sourceLabel(state)}; retrying when the connection returns.`;
  if (source !== null && !warnings.includes(message)) actions.addWarning(message);
}

/** One sync's identity: the generation it owns and its replay token. */
interface SyncId {
  gen: number;
  replayToken: number;
}

/** The ONE test of whether a sync (a `syncFromServer`, or an admission's
 * install) may still act: it owns the generation (no source change since)
 * AND it is the newest sync begun, still in flight (reviewStore.ts's
 * `isCurrentResync`, the store's leg of this one decision).
 * Checked before every side effect: an adoption, a pending-state change, an
 * install. */
function isCurrentSync(sync: SyncId): boolean {
  return isCurrentSource(sync.gen) && isCurrentResync(sync.replayToken);
}

/** How an obsolete sync ends, given the mode it read (null with none):
 * `stale`, unless that mode names another source than the one the tab now
 * serves and is newer than every state admitted. A newer sync may have read
 * the URL's source before it changed, so that evidence is not dropped:
 * `mismatch`, retried (`retriesSync`), so the retry's sync adopts it. The
 * obsolete sync itself acts on nothing. */
function obsoleteResult(seen: MonitorSourceState | null): "stale" | "mismatch" {
  const served = servedSource(useReviewStore.getState());
  if (served === null || seen === null || seen.token === served.token) return "stale";
  return isAfterLastStamp(seen) ? "mismatch" : "stale";
}

/** Whether `state` is after the newest admitted source state. */
function isAfterLastStamp(state: MonitorSourceState): boolean {
  return isAfter(state, useReviewStore.getState().lastSourceStamp);
}

/** Whether `state`'s stamp is after `last` (microseconds). An EQUAL stamp is
 * not: it is the same state, or one the server ordered no later. A state with
 * no readable stamp is after nothing but an empty tab (`last` null). */
function isAfter(state: MonitorSourceState, last: number | null): boolean {
  if (last === null) return true;
  const stamp = observedAtMicros(state.observed_at);
  return stamp !== null && stamp > last;
}

/** The one start of every source change, in one synchronous turn: in-flight
 * work goes stale, and every piece of UI state bound to the old source is
 * cleared, the undo history included (nothing in it addresses the new
 * source), and every dialog bound to it closes. */
function supersede(): void {
  nextGeneration();
  useUndoHistory.getState().actions.clear();
  const ui = useUiStore.getState().actions;
  // A source change resets live UI state synchronously: a decision of its
  // own, apart from data/openSpan.ts's ended-or-vanished rule.
  ui.setOpenSpan(null);
  ui.closeEventEditor();
  ui.disarmSweep();
  ui.closeMarkPopover();
  // A prompt or a name offered for the old source is stale: the switch
  // prompt confirms the old source's counts, and the export dialog offers a
  // name built from the old source.
  ui.closeSwitchPrompt();
  ui.closeExportDialog();
}

/** The transition onto a new server source: `supersede`, then `state` is
 * pending (or the pending state of the same source, when that one is
 * newer). Returns the replay token the install will spend: `replayToken`
 * when given (a sync has ONE replay token, taken before its fetches, so a
 * push that lands after the capture survives the transition), else a new
 * one taken now.
 *
 * A transition onto the source already pending (a retry of the same
 * switch) is not a second source change: it does not `supersede` (what the
 * user opened over the pending source stays open), and it keeps that one's
 * notice decision. Any other is announced (`announce`) when a source was
 * installed (not the first boot), the tab is not uploading a switch of its
 * own, and the caller is not reporting the tab's own switch (`own`). */
function beginTransition(
  state: MonitorSourceState,
  opts: { replayToken?: number; own?: boolean } = {},
): number {
  const { actions, pendingSource, source } = useReviewStore.getState();
  if (pendingSource?.token !== state.token) {
    supersede();
    announce = opts.own !== true && ownSwitches === 0 && source !== null;
  }
  const newerPending =
    pendingSource?.token === state.token &&
    !isAfter(state, observedAtMicros(pendingSource.observed_at));
  actions.setPendingSource(newerPending ? pendingSource : state);
  return opts.replayToken ?? actions.beginResync();
}

/** The ONE finalisation of a server source: installs the snapshot (which
 * replays the pushes stamped after `captured`) together with the newest
 * state admitted for `state.token`, in one store write:
 *  - the pending state, when it has that token (a newer same-token state
 *    that arrived meanwhile has already raised its counts);
 *  - else `state`, when it is newer than every admitted state or names
 *    another source than the installed one;
 *  - else the installed state stays: a counts frame admitted during a
 *    same-token resync is newer than that resync's mode, so its counts
 *    survive and only the snapshot install takes effect.
 * Then nothing is pending, `editable` follows the installed source, and the
 * transition's notice shows (`announce`). `state` is the one read before
 * the snapshot was requested (the sync's mode, or the admitted state), so
 * the snapshot holds its revision. A body that does not parse surfaces
 * through `importError` and leaves the source pending (`invalid`).
 *
 * The caller has just checked that its sync is current (`isCurrentSync`),
 * in the same synchronous turn: a late older snapshot never replaces a newer
 * one, nor leaves the acknowledgement withheld with no sync left to resolve
 * it. */
function finishInstall(
  text: string,
  state: MonitorSourceState,
  opts: Omit<SnapshotInstall, "state" | "holds">,
): "installed" | "invalid" {
  const { actions, pendingSource, source } = useReviewStore.getState();
  let installed = source ?? state;
  if (pendingSource?.token === state.token) installed = pendingSource;
  else if (state.token !== source?.token || isAfterLastStamp(state)) installed = state;
  const install = { ...opts, state: installed, holds: state.revision ?? 0 };
  const result = actions.installSnapshot(text, state.source ?? "server", install);
  if (result === "invalid") return "invalid";
  // A logged fragment the replay could not apply is a loss like any other.
  if (result === "lost") stream?.lost({ recover: true });
  if (announce) {
    announce = false;
    useToastStore.getState().showToast(sourceChangedNotice(installed));
  }
  return "installed";
}

/** The ONE server sync: the boot runs it `fresh`, every reconnect not.
 * The stream is already subscribed when it starts, so nothing published
 * after the snapshot's capture is lost: it is logged under this sync's one
 * replay token and replayed onto the snapshot. In order:
 *  1. take the replay token;
 *  2. fetch the mode. One that names another source than the installed one
 *     starts the transition onto it at once, with this sync's replay token:
 *     the URL's source is then known (pending), whatever the snapshot does;
 *  3. fetch the snapshot. One whose token is not the mode's (a switch
 *     landed between the two) is refused, and both are fetched again ONCE
 *     (step 2 again for the new mode); a second mismatch installs nothing,
 *     as a failed snapshot does;
 *  4. finish the install: `fresh` as asked, and always fresh onto a new
 *     token (another source resets the view, as an import does).
 * A sync on the SAME token still re-installs the snapshot, keeping the view:
 * that refresh is how a write missed across a reconnect reaches the tab.
 * Every answer is applied only while the sync is current (`isCurrentSync`:
 * it owns the generation and is the newest sync begun), re-checked after
 * EVERY await, before the continuation acts on it (a source admitted, or a
 * newer sync begun, between a fetch's own check and this continuation owns
 * the tab). An obsolete sync adopts, supersedes and installs nothing
 * (`obsoleteResult`). Every transport failure is silent (data/snapshot.ts's
 * soft-fail contract) but one: a source this sync adopted that it could not
 * install, for any reason a retry can fix, is warned about as an
 * admission's is (`warnPendingUnavailable`). The result says how the sync
 * ended, and the caller decides whether to retry. */
export async function syncFromServer(opts: { fresh: boolean }): Promise<SyncResult> {
  const review = useReviewStore.getState().actions;
  const sync: SyncId = { gen: sourceGeneration(), replayToken: review.beginResync() };
  let adopted: MonitorSourceState | null = null;
  /** Step 2: `mode` names the URL's source; a new token is pending now. */
  const adopt = (mode: MonitorSourceState): void => {
    if (mode.token === useReviewStore.getState().source?.token) return;
    beginTransition(mode, { replayToken: sync.replayToken });
    sync.gen = sourceGeneration();
    adopted = mode;
  };
  /** Steps 2 and 3, once: the mode and the snapshot read for it. */
  const read = async (): Promise<SyncResult | { mode: MonitorSourceState; snapshot: Snapshot }> => {
    const mode = await fetchMode(sync.gen);
    if (!isCurrentSync(sync)) return obsoleteResult(mode);
    if (mode === null) return "unavailable";
    adopt(mode);
    const snapshot = await fetchSnapshot(sync.gen);
    if (!isCurrentSync(sync)) return obsoleteResult(mode);
    if (snapshot === null) return "unavailable";
    return { mode, snapshot };
  };
  /** Steps 2 to 4. */
  const run = async (): Promise<SyncResult> => {
    let answer = await read();
    if (typeof answer !== "string" && answer.snapshot.token !== answer.mode.token) {
      if (!isCurrentSync(sync)) return obsoleteResult(answer.mode);
      answer = await read();
    }
    if (typeof answer === "string") return answer;
    const { mode, snapshot } = answer;
    if (!isCurrentSync(sync)) return obsoleteResult(mode);
    if (snapshot.token !== mode.token) return "mismatch";
    return finishInstall(snapshot.text, mode, {
      replayToken: sync.replayToken,
      captured: snapshot.captured,
      fresh: opts.fresh || adopted !== null,
    });
  };
  try {
    const result = await run();
    // A source this sync adopted that it could not install leaves the tab
    // read-only until a retry installs it: warned about as an admission's is.
    if (adopted !== null && retriesSync(result) && isCurrentSync(sync)) {
      warnPendingUnavailable(adopted);
    }
    return result;
  } finally {
    review.endResync(sync.replayToken);
  }
}

/** Whether a sync that ended `result` is retried through the stream's
 * reconnect (`retryOnReconnect`): a fetch that failed, or a switch that
 * kept landing under it. An unparsable snapshot is not: a retry would read
 * the same body; the stream stays open, and the next reconnect or source
 * frame recovers. */
export function retriesSync(result: SyncResult): boolean {
  return result === "unavailable" || result === "mismatch";
}

/** The stream's every later open (a reconnect): resync, not fresh, and
 * retry through the next reconnect when nothing installed for a reason a
 * retry can fix. Resolves whether the open was healthy (no retry): the
 * stream resets its backoff when a healthy connection that lost no frame
 * ends (data/stream.ts's `clean`). It never
 * rejects: a sync that throws is not healthy, and is not retried (a throw is
 * a defect, not a server that answered badly). */
export async function resyncOnOpen(): Promise<boolean> {
  let result: SyncResult;
  try {
    result = await syncFromServer({ fresh: false });
  } catch {
    return false;
  }
  const retry = retriesSync(result);
  if (retry) retryOnReconnect();
  return !retry;
}

/** Admits a source state that arrived on its own (an upload response, a
 * stream frame, a `source_changed` refusal). It never refreshes the
 * snapshot of the source already installed; that is `syncFromServer`.
 *
 * The admission rule: a state whose stamp is not after `lastSourceStamp` is
 * ignored, an EQUAL stamp included, so an obsolete or duplicate state never
 * starts an adoption and never lowers newer counts. Otherwise:
 *  - the installed source's state, with nothing pending, only updates its
 *    counts;
 *  - the pending source's state replaces the pending one; its counts are
 *    applied when the install finishes;
 *  - any other token starts the transition (`beginTransition`, which
 *    decides its notice; `own` marks the tab's own switch): the tab cannot
 *    write until the new snapshot has installed (`finishInstall`). A
 *    snapshot read from yet another source falls back to one
 *    `syncFromServer({ fresh: true })`. A failed snapshot leaves the source
 *    pending with a warning (`warnPendingUnavailable`) and forces the stream
 *    to reconnect (`retryOnReconnect`): the new connection's open runs
 *    `syncFromServer`, which retries. */
export async function admitSource(
  state: MonitorSourceState,
  opts: { own?: boolean } = {},
): Promise<void> {
  if (!isAfterLastStamp(state)) return;
  const { actions, pendingSource, source } = useReviewStore.getState();
  if (pendingSource === null && state.token === source?.token) {
    actions.setSourceState(state);
    return;
  }
  if (pendingSource?.token === state.token) {
    actions.setPendingSource(state);
    return;
  }
  const replayToken = beginTransition(state, opts);
  const sync: SyncId = { gen: sourceGeneration(), replayToken };
  try {
    const snapshot = await fetchSnapshot(sync.gen);
    if (!isCurrentSync(sync)) return;
    if (snapshot === null) {
      warnPendingUnavailable(state);
      retryOnReconnect();
      return;
    }
    if (snapshot.token === state.token) {
      finishInstall(snapshot.text, state, {
        replayToken,
        captured: snapshot.captured,
        fresh: true,
      });
    } else {
      const result = await syncFromServer({ fresh: true });
      if (retriesSync(result)) retryOnReconnect();
    }
  } finally {
    actions.endResync(replayToken);
  }
}

/** Admits a stream's source frame, the one source state that raises what an
 * export acknowledges (reviewStore.ts's `covered`). A frame that moves the
 * tab onto another source carries the source-changed notice unless it is
 * this tab's own switch (`beginTransition`): while its upload is in flight
 * the server publishes the new source's frame before it answers, and that
 * frame is the switch the user asked for. The notice is deliberately
 * suppressed for EVERY frame during this tab's own upload, another tab's
 * switch included: that switch makes the upload's confirmation stale, and
 * the `source_changed` re-prompt that follows names the other source
 * instead (`requestSourceSwitch`). */
export function admitSourceFrame(state: MonitorSourceState): void {
  useReviewStore.getState().actions.noteStreamedState(state);
  void admitSource(state);
}

/** The URL's server source, as far as the tab knows it: the pending one
 * when a transition is under way (it is what the server serves now), else
 * the installed one; null with none (no server, or a file the tab loaded
 * itself). What an Import is judged against, and whether the tab is
 * streaming one (shell/ReconnectingBanner.tsx). */
export function servedSource(s: {
  source: MonitorSourceState | null;
  pendingSource: MonitorSourceState | null;
}): MonitorSourceState | null {
  return s.pendingSource ?? s.source;
}

/** Whether the tab offers Import, judged by the URL's source: on a `.json`
 * source (where it switches the URL's source, after a prompt), and with no
 * server source once the boot has concluded there is none (the static
 * shell, or a file the tab loaded itself: Import loads the file in the
 * tab). A live or `.db` source, installed or only pending (its mode
 * answered, its snapshot not installed), never offers it, and nor does a
 * URL whose source is still unresolved (`sourceKindUnresolved`: no
 * `/api/mode` has answered, and the stream has not failed before opening).
 * Every Import entry point (the
 * command, the overflow item, a drop) reads this, and `importLocalFile`
 * re-reads it once the file is read: a picker opened before a live source
 * arrived does not load the file after it. */
export function importOffered(s: {
  source: MonitorSourceState | null;
  pendingSource: MonitorSourceState | null;
  sourceKindUnresolved: boolean;
}): boolean {
  const target = servedSource(s);
  if (target !== null) return target.kind === "json";
  return !s.sourceKindUnresolved;
}

/** The switch prompt for opening `name` (read as `text`) over `source`. */
function switchPromptFor(name: string, text: string, source: MonitorSourceState): SwitchPrompt {
  return {
    name,
    text,
    shown: { token: source.token, unsaved: source.unsaved ?? 0, source: sourceLabel(source) },
  };
}

/** Resolves once no server source is pending. */
function nothingPending(): Promise<void> {
  if (useReviewStore.getState().pendingSource === null) return Promise.resolve();
  return new Promise((resolve) => {
    const unsubscribe = useReviewStore.subscribe((s) => {
      if (s.pendingSource !== null) return;
      unsubscribe();
      resolve();
    });
  });
}

/** Confirms the open switch prompt: uploads its file, confirming the source
 * as the prompt showed it, and closes the prompt. Then:
 *  - accepted: the new source is admitted (a state older than one already
 *    admitted is dropped there);
 *  - refused as `source_changed`: the source the server serves is
 *    admitted, and once nothing is pending the prompt opens again over the
 *    installed source, with its fresh counts, unless another import has
 *    started meanwhile;
 *  - any other refusal: shown through `importError`; the source is
 *    untouched. */
export async function requestSourceSwitch(): Promise<void> {
  const prompt = useUiStore.getState().switchPrompt;
  if (prompt === null) return;
  // An import started after this one supersedes its re-prompt.
  const ticket = importTicket;
  useUiStore.getState().actions.closeSwitchPrompt();
  ownSwitches += 1;
  let result: UploadResult;
  try {
    result = await uploadSource(prompt.text, prompt.name, prompt.shown);
  } finally {
    ownSwitches -= 1;
  }
  if (result.kind === "error") {
    useReviewStore
      .getState()
      .actions.setImportError(`Import failed: could not open ${prompt.name}: ${result.message}`);
    return;
  }
  await admitSource(result.state, { own: true });
  if (result.kind === "ok") return;
  await nothingPending();
  const { source } = useReviewStore.getState();
  const ui = useUiStore.getState();
  if (source?.kind !== "json" || ui.switchPrompt !== null || ticket !== importTicket) return;
  ui.actions.openSwitchPrompt(switchPromptFor(prompt.name, prompt.text, source));
}

/** Leaves the server source for a file the tab loaded itself, in one
 * synchronous turn: `supersede`, the stream closes, and the tab holds no
 * server source (no token, no mode, not editable). */
function detachServerSource(): void {
  supersede();
  announce = false;
  const control = stream;
  stream = null;
  control?.stop();
  const review = useReviewStore.getState().actions;
  review.setPendingSource(null);
  review.setSourceState(null);
}

/** Every UI import path (the file picker, drag and drop) comes through here.
 * The newest import wins; a read failure surfaces through `importError`.
 * Where Import is not offered once the file is read (`importOffered`: a live
 * or `.db` source), the file is ignored. On a `.json` server source the
 * file is not loaded here: the switch prompt
 * opens for it (`requestSourceSwitch` confirms it). Otherwise the tab loads
 * the file itself; one that fails to parse leaves the current source
 * untouched. The server's snapshots do not come through here: they install
 * through `syncFromServer` and `admitSource`. */
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
  const review = useReviewStore.getState();
  if (!importOffered(review)) return;
  const target = servedSource(review);
  if (target !== null) {
    useUiStore.getState().actions.openSwitchPrompt(switchPromptFor(file.name, text, target));
    return;
  }
  if (review.actions.importMonitorSessions(text, file.name)) {
    detachServerSource();
  }
}
