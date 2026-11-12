// Session state for both modes: the imported/hydrated document, the active
// session, and the viewed time range. The legacy Plotly-era live store
// (store.ts) was deleted rather than merged into this one — the live hookup
// grows sessions in place here via appendFragment(s) instead, so there is
// only ever this one store.
import { create } from "zustand";

import type {
  MonitorHistoricalExportDocument,
  MonitorSessionFragment,
  MonitorSourceState,
  SourceSavedBody,
} from "../api/export.gen";
import {
  ExportParseError,
  type NormalizedSession,
  observedAtMicros,
  parseExportDocument,
  type TimeRange,
} from "./exportDoc";
import { applyFragment } from "./fragment";
import { displayEndMs } from "./time";

/** Pure merge step shared by appendFragment/appendFragments: applies each
 * fragment in order to `sessions`, returning the SAME array reference if
 * nothing changed (callers use reference equality to skip a store write) or
 * a NEW array with only the touched session(s) replaced. Kept out of the
 * store body so a whole batch can be folded into exactly one `set()` call.
 * `warnings` is mutated in place with one entry per fragment that dropped a
 * bad-timestamp metric row (see fragment.ts's applyFragment) — the caller
 * folds it into the store's `warnings` channel in the SAME `set()` as the
 * session update, so a batch that drops rows costs exactly one extra
 * notification, not one per fragment.
 *
 * The ONE horizon rule: only a live source's stamps move a session's display
 * horizon (applyFragment's `advanceHorizon`); `mode` is the mode of the
 * source the fragments belong to. A review source's stamps still order its
 * event changes. A fragment for a session `sessions` does not hold is not
 * applied: it is collected in `unheld` (appendFragments decides whether
 * that is a gap). */
function mergeFragments(
  sessions: NormalizedSession[],
  frags: readonly MonitorSessionFragment[],
  warnings: string[],
  mode: ReviewState["mode"],
  unheld: MonitorSessionFragment[] = [],
): NormalizedSession[] {
  const opts = { advanceHorizon: mode === "live" };
  let next = sessions;
  let changed = false;
  for (const frag of frags) {
    const i = next.findIndex((s) => s.id === frag.session);
    // Reading the slot IS the -1 check: `next[-1]` is undefined, so one
    // lookup replaces the sentinel compare plus two re-reads of `next[i]`.
    const current = next[i];
    if (current === undefined) {
      unheld.push(frag); // a fragment for a session we do not hold: not applied
      continue;
    }
    const merged = applyFragment(current, frag, warnings, opts);
    if (merged === current) continue;
    if (!changed) {
      next = [...next];
      changed = true;
    }
    next[i] = merged;
  }
  return next;
}

/** One in-flight sync's log: the fragments received while its snapshot is in
 * flight, and whether a fragment was lost meanwhile (`withholdCoverage`).
 * The server may have captured the snapshot before that fragment, so the
 * sync's install does not prove the held sessions complete: `gap` keeps the
 * acknowledgement withheld past it, until a sync begun after the loss
 * installs. */
interface ResyncLog {
  frags: MonitorSessionFragment[];
  gap: boolean;
}

/** The log of each sync in flight, keyed by the replay token `beginResync`
 * handed out (#868). The stream stays open while
 * a snapshot is fetched (data/source.ts's syncFromServer), and the server
 * captures the snapshot some time after the request starts: a push or a
 * mutation's response received meanwhile may be newer than it or already in
 * it, so `installSnapshot` replays one only when the server ordered it after
 * the capture. Not store state: nothing renders it. */
const resyncLogs = new Map<number, ResyncLog>();
let lastResyncToken = 0;

/** Whether `token`'s sync is the current one as far as the store can tell:
 * it is the newest begun (`beginResync`) and still in flight (its log is
 * neither installed nor ended). The ONE home of that decision: data/source.ts's
 * isCurrentSync adds the generation leg to it, and appendFragments asks it
 * whether the newest log will be installed.
 *
 * The store needs no generation leg of its own. Every source change that
 * leaves a sync in flight begins a newer one (data/source.ts's
 * beginTransition takes a new token), so the older token stops being the
 * newest, or it hands the change's install to the very sync that adopted it
 * (that sync keeps its token and moves its generation along). The one source
 * change that begins no sync, leaving for a local file, stops the stream, so
 * no fragment reaches appendFragments after it. */
export function isCurrentResync(token: number): boolean {
  return token === lastResyncToken && resyncLogs.has(token);
}

/** The fragment as a snapshot install may replay it: whole (metrics, log
 * events, meta and event changes alike), when it carries the server stamp
 * that orders it against the capture. A stampless fragment cannot be
 * ordered, so the snapshot wins and it is not logged. */
function replayable(frag: MonitorSessionFragment): MonitorSessionFragment | null {
  return frag.observed_at == null ? null : frag;
}

/** The logged fragments the server ordered strictly after the snapshot's
 * `captured` stamp, in the order they applied. Both stamps come from the
 * server's one clock (event_ops.utc_now, strictly increasing at microsecond
 * resolution), read in the same step as the state each describes, so the
 * comparison is the server's own order. A tie, or a missing capture stamp,
 * means the snapshot may already hold the change: it is dropped. */
function laterThanCapture(
  log: readonly MonitorSessionFragment[],
  captured: string | null,
): MonitorSessionFragment[] {
  const floor = observedAtMicros(captured);
  if (floor === null) return [];
  return log.filter((frag) => (observedAtMicros(frag.observed_at) ?? floor) > floor);
}

/** A loaded snapshot's sessions with the replay applied. A resync (not
 * `fresh`) also never lowers a kept session's horizon (`observedMs`, with
 * the `observedAt` it came from: the server's clock only moves forward). The
 * replayed events are still ordered against each session's own floor by
 * applyFragment's one rule. A replayed row that is dropped as invalid was
 * already warned about when it first applied, so the replay's own warnings
 * are discarded. `mode`: the installing source's (see mergeFragments). A
 * replayed fragment for a session the snapshot does not hold is not applied:
 * it is collected in `unheld` (installSnapshot reports it as a loss). */
function reconcileSnapshot(
  loaded: NormalizedSession[],
  held: readonly NormalizedSession[],
  later: readonly MonitorSessionFragment[],
  opts: {
    fresh: boolean;
    mode: ReviewState["mode"];
    unheld?: MonitorSessionFragment[] | undefined;
  },
): NormalizedSession[] {
  const replayed = mergeFragments(loaded, later, [], opts.mode, opts.unheld);
  if (opts.fresh) return replayed;
  return replayed.map((s) => {
    const before = held.find((h) => h.id === s.id);
    return before !== undefined && before.observedMs > s.observedMs
      ? { ...s, observedMs: before.observedMs, observedAt: before.observedAt }
      : s;
  });
}

/** The store fields that follow from the installed and the pending source:
 * written together, by the two source writers only, so none can disagree
 * with `source`. A tab installing a source (`pendingSource` set) cannot
 * write. */
function sourceFields(
  source: MonitorSourceState | null,
  pendingSource: MonitorSourceState | null,
): Pick<
  ReviewState,
  | "source"
  | "pendingSource"
  | "mode"
  | "editable"
  | "sourceToken"
  | "sourceKind"
  | "unsaved"
  | "revision"
> {
  return {
    source,
    pendingSource,
    mode: source?.mode ?? null,
    editable: source?.editable === true && pendingSource === null,
    sourceToken: source?.token ?? null,
    sourceKind: source?.kind ?? null,
    unsaved: source?.unsaved ?? 0,
    revision: source?.revision ?? 0,
  };
}

/** `last` raised to `state`'s stamp: every state written moves it forward. */
function raisedStamp(last: number | null, state: MonitorSourceState | null): number | null {
  const stamp = observedAtMicros(state?.observed_at);
  if (stamp === null) return last;
  return last === null || stamp > last ? stamp : last;
}

/** The revision of `token` the held sessions are known to hold: the
 * export's acknowledgement (`exportAcknowledgement`). */
export interface CoveredRevision {
  token: string;
  revision: number;
}

/** `covered` raised to `revision` of `token`; another token starts over. */
function raisedCover(
  covered: CoveredRevision | null,
  token: string,
  revision: number,
): CoveredRevision {
  if (covered?.token !== token) return { token, revision };
  return revision > covered.revision ? { token, revision } : covered;
}

/** The options of one snapshot install (see `installSnapshot`). */
export interface SnapshotInstall {
  /** The token `beginResync` returned when the snapshot's sync began. */
  replayToken: number;
  /** The snapshot's capture stamp (`X-Otto-Observed-At`), or null. */
  captured: string | null;
  /** Reset the range and the active session, as an Import does. */
  fresh: boolean;
  /** The source state installed with the document, in the same store
   * write: nothing is pending after it, and its mode decides whether the
   * replay moves the horizon (mergeFragments). */
  state: MonitorSourceState;
  /** A revision of `state`'s source the snapshot is known to hold: that of
   * a state of the source read before the snapshot was requested (the
   * server captures the snapshot after it, on the same clock). */
  holds: number;
}

interface ReviewActions {
  /** Parse + load a document the user chose (a local file): a fresh load
   * with nothing to replay. Returns false (and sets importError, keeping any
   * previously loaded data) on failure. */
  importMonitorSessions: (text: string, sourceName: string) => boolean;
  /** The ONE snapshot load for a server source: the boot, every resync and
   * every source transition (data/source.ts's finishInstall). Replaces
   * sessions/rawMonitorSessions/warnings from `text`, then replays every
   * fragment logged under `replayToken` that the server stamped strictly
   * after `captured` (metrics, log events, meta and events alike) and drops
   * the rest, and installs `state` as the source (nothing pending), raising
   * `covered` to `holds`, all in one store write: the document and the
   * source it belongs to never disagree. The token is spent, and the
   * install resolves `resyncUnresolved` unless a fragment was lost while
   * its sync was in flight (`withholdCoverage`). Only the newest sync
   * installs (data/source.ts's isCurrentSync).
   *
   * `fresh` resets `range` and `activeSessionId` as an Import does. A resync
   * (not fresh) keeps both when `activeSessionId` still resolves against the
   * new snapshot: a transient network blip must not silently discard a
   * user's paused/pinned view (pause — togglePause — exists specifically to
   * protect that state). When it no longer resolves (the server restarted
   * and handed back another session set), it falls back to `sessions[0]` and
   * `range: null`, or the shell would sit in its empty state until a reload.
   * A resync also never lowers a kept session's `observedMs`.
   *
   * Returns `invalid` (and sets importError, keeping any previously loaded
   * data) when the body does not parse; `lost` when it installed but the
   * replay could not apply a logged fragment (one for a session the
   * snapshot does not hold): the caller handles it as any other loss
   * (data/stream.ts's `lost`, through data/source.ts's finishInstall), which
   * withholds the acknowledgement and starts recovery; else `installed`. */
  installSnapshot: (
    text: string,
    sourceName: string,
    opts: SnapshotInstall,
  ) => "installed" | "lost" | "invalid";
  /** Start logging the fragments `appendFragments` receives, for the sync
   * whose snapshot request starts after this; returns its replay token. The
   * sync is unresolved (`resyncUnresolved`) until it installs. */
  beginResync: () => number;
  /** Stop logging for `token`. Idempotent; `installSnapshot` spends it too,
   * so a caller ends it unconditionally once the attempt is over. */
  endResync: (token: number) => void;
  /** Append a live-stream fragment to the session it addresses. A no-op if
   * that session isn't currently held (e.g. it hasn't loaded yet). While a
   * source is pending, a fragment belongs to that source, not to the held
   * document: it is only logged, for the pending source's replay. */
  appendFragment: (frag: MonitorSessionFragment) => void;
  /** Batched sibling of appendFragment: applies a whole batch of fragments
   * (the SSE client's per-frame flush, ~90 at the live bed's shape) in ONE
   * store write — the property that keeps a tick to a single zustand
   * notification / render pass instead of one per fragment. Returns false
   * when a fragment was lost: one for a session the tab does not hold that
   * the current sync (the newest begun, still in flight) did not log for
   * its replay. The stream treats that as any
   * other frame it could not apply (data/stream.ts's `lost`); a mutation's
   * own fragment (eventApi.ts) needs nothing, as its push follows on the
   * stream. */
  appendFragments: (frags: readonly MonitorSessionFragment[]) => boolean;
  /** The ONE mark that a fragment was lost (data/stream.ts's `lost`: one
   * the tab could not read, in a format it does not read, or for a session
   * it does not hold): what the tab holds is no longer known complete, so
   * the acknowledgement is withheld (`resyncUnresolved`) until a sync begun
   * after the loss installs. A sync in flight now may have captured its
   * snapshot before the loss, so its install keeps it withheld. */
  withholdCoverage: () => void;
  selectSession: (id: string) => void;
  setRange: (range: TimeRange | null) => void;
  /** The one writer of `importError` outside the parse itself: surfaces an
   * import failure that never reached the parser (a file the browser could
   * not read — see data/source.ts's importLocalFile) on the same banner a
   * parse failure uses, and clears the banner when given null. */
  setImportError: (message: string | null) => void;
  /** The ONE writer of `source`, the installed server source, and of the
   * fields that follow from it (`mode`, `editable`, `sourceToken`,
   * `sourceKind`, `unsaved`, `revision`). Null means the tab holds no server
   * source (a file it loaded itself). Raises `lastSourceStamp` to the
   * state's stamp. data/source.ts decides what is admitted. */
  setSourceState: (state: MonitorSourceState | null) => void;
  /** Sets `pendingSource`, the source being admitted (null when none is):
   * while one is pending the tab cannot write. Raises `lastSourceStamp` to
   * the state's stamp. data/source.ts decides what is admitted. */
  setPendingSource: (state: MonitorSourceState | null) => void;
  /** Records a source state the stream delivered (a source frame): it
   * raises `covered` (data/source.ts's admitSourceFrame). A state from
   * anywhere else (an HTTP answer) may count writes whose pushes have not
   * arrived yet, and never comes through here. */
  noteStreamedState: (state: MonitorSourceState) => void;
  /** Sets `sourceKindUnresolved` (data/bootstrap.ts: at the boot's start,
   * and once its stream fails before opening). */
  setSourceKindUnresolved: (unresolved: boolean) => void;
  /** Append one message to the warnings channel (rendered by
   * DataWarningsBanner) — the surface for mutation failures issued from
   * chrome with no inline error slot of its own (palette commands). */
  addWarning: (message: string) => void;
  /** SSE connection lifecycle (see data/stream.ts); irrelevant in review mode. */
  setConnection: (connection: "connecting" | "live" | "disconnected") => void;
  /** Live-only view control. Pause and "the user picked a custom range" are
   * ONE state, not two — there is no stored `paused` flag to disagree with
   * `range` (see `useIsPaused`). Resuming clears `range` back to null —
   * following again. Pausing snapshots the currently-derived follow window
   * into an absolute `range` (see `liveRange`). Does NOT touch ingestion:
   * fragments keep applying while paused, so resume catches up with no gap
   * (spec: "pause is a view control, not a data control"). Any OTHER way of
   * pinning the view in live mode (e.g. a chart drag-zoom calling `setRange`
   * directly) is therefore *already* "paused" — `togglePause` in that state
   * resumes following rather than freezing a second, redundant window. */
  togglePause: () => void;
  /** Live-only view control: resize the follow window. `paused` is derived
   * from `range` (see `useIsPaused`/`togglePause`), never stored, so this
   * has exactly two cases, keyed on that same `range`:
   *  - Following (`range === null`): set `windowMs` only. `range` STAYS
   *    null — the widened/narrowed window is picked up on the next render
   *    via `liveRange(displayEndMs(session), windowMs)` (SubjectPage.tsx). Pinning
   *    `range` here would silently turn "pick a window" into "pause", which
   *    is not what choosing a preset means while following.
   *  - Paused (`range !== null`): re-pin `range` to the new width, ending at
   *    the SAME `range.to` — the frozen instant the user is looking at.
   *    Choosing "1h" while paused zooms around the current view rather than
   *    silently resuming (dropping the pause) or doing nothing until the
   *    user resumes first. */
  setWindow: (windowMs: number) => void;
}

export interface ReviewState {
  sessions: NormalizedSession[];
  rawMonitorSessions: MonitorHistoricalExportDocument | null;
  sourceName: string | null;
  warnings: string[];
  importError: string | null;
  activeSessionId: string | null;
  range: TimeRange | null;
  /** The installed server source's state (`/api/mode`, a stream frame, an
   * upload response). Null until one is installed, and once the tab loads a
   * file of its own. */
  source: MonitorSourceState | null;
  /** The source being admitted: its state is known, its snapshot is not
   * installed yet. */
  pendingSource: MonitorSourceState | null;
  /** The newest stamp of any source state admitted, in microseconds
   * (`observedAtMicros`). A state not after it is obsolete. */
  lastSourceStamp: number | null;
  /** Whether the boot has yet to conclude that no otto server serves this
   * URL: true from the boot's start until its stream fails before it ever
   * opened (data/bootstrap.ts). Read only while no server source is known:
   * once a `/api/mode` answers, its source (pending, then installed) decides
   * Import instead. While it holds with no source known, Import is withheld
   * (data/source.ts's importOffered). */
  sourceKindUnresolved: boolean;
  /** Whether the acknowledgement is withheld until an install: a sync has
   * begun (`beginResync`) since the last install, or a fragment was lost
   * (`withholdCoverage`) since the last install of a sync begun after it.
   * Only such an install proves the held sessions hold every write counted
   * so far: a reconnect's sync may yet deliver a write the tab missed while
   * disconnected, while the new connection already counts the writes after
   * it, and a count after a lost fragment counts a write the tab lacks. */
  resyncUnresolved: boolean;
  /** The revision of a source the held sessions are known to hold, once no
   * sync is unresolved: a snapshot's `holds`, raised by every state the
   * stream delivered for that source (`noteStreamedState`). The server
   * publishes each write's push before the source frame that counts it, a
   * connection delivers its frames in order and without a gap (a lapse ends
   * it), and a sync's stream is subscribed before its snapshot is captured:
   * so a write the stream counted is in the snapshot, or reached the tab as
   * a push and was applied or replayed. Kept apart from `revision`, the
   * newest count received from anywhere. */
  covered: CoveredRevision | null;
  /** The installed source's mode; null with no server source. */
  mode: "live" | "review" | null;
  /** Whether the tab may write: the installed source accepts event
   * mutations and no other source is pending. Gates every marking
   * affordance. */
  editable: boolean;
  /** The installed source's token, sent on every event write. */
  sourceToken: string | null;
  /** The installed source's kind. */
  sourceKind: MonitorSourceState["kind"] | null;
  /** The installed source's writes since its last export. */
  unsaved: number;
  /** The installed source's writes since it was loaded. */
  revision: number;
  connection: "connecting" | "live" | "disconnected";
  /** The follow-the-tail window width in live mode, applied whenever
   * `range === null` (see `liveRange` in data/time.ts). Default 15 min. */
  windowMs: number;
  actions: ReviewActions;
}

export const useReviewStore = create<ReviewState>()((set, get) => {
  /** Parse `text` and install it in one store write, with `later` replayed
   * onto it in `opts.mode` and the fields `along` (see installSnapshot's doc
   * comment). */
  const loadDocument = (
    text: string,
    sourceName: string,
    later: readonly MonitorSessionFragment[],
    opts: Pick<SnapshotInstall, "fresh" | "captured"> & {
      mode: ReviewState["mode"];
      unheld?: MonitorSessionFragment[];
    },
    along: Partial<ReviewState> = {},
  ): boolean => {
    let result: ReturnType<typeof parseExportDocument>;
    try {
      result = parseExportDocument(text, opts.captured);
    } catch (err) {
      set({
        importError:
          err instanceof ExportParseError ? err.message : `Import failed: ${String(err)}`,
      });
      return false;
    }
    const { activeSessionId, sessions: held } = get();
    // The ONE condition that decides preserve-vs-fallback for a resync: does
    // the CURRENT activeSessionId still resolve against the new snapshot?
    const keepView =
      !opts.fresh &&
      activeSessionId !== null &&
      result.sessions.some((s) => s.id === activeSessionId);
    set({
      sessions: reconcileSnapshot(result.sessions, held, later, {
        fresh: opts.fresh,
        mode: opts.mode,
        unheld: opts.unheld,
      }),
      rawMonitorSessions: result.document,
      sourceName,
      warnings: result.warnings,
      importError: null,
      ...(keepView ? {} : { activeSessionId: result.sessions[0]?.id ?? null, range: null }),
      ...along,
    });
    return true;
  };
  return {
    sessions: [],
    rawMonitorSessions: null,
    sourceName: null,
    warnings: [],
    importError: null,
    activeSessionId: null,
    range: null,
    lastSourceStamp: null,
    sourceKindUnresolved: false,
    resyncUnresolved: false,
    covered: null,
    ...sourceFields(null, null),
    connection: "connecting",
    windowMs: 900_000,
    actions: {
      importMonitorSessions: (text, sourceName) =>
        // A file the tab loaded itself: no server source, so no mode.
        loadDocument(text, sourceName, [], { fresh: true, captured: null, mode: null }),
      installSnapshot: (text, sourceName, opts) => {
        const log = resyncLogs.get(opts.replayToken);
        resyncLogs.delete(opts.replayToken);
        const { state } = opts;
        const { covered, lastSourceStamp } = get();
        const unheld: MonitorSessionFragment[] = [];
        // The document and the source it belongs to, in one write.
        const loaded = loadDocument(
          text,
          sourceName,
          laterThanCapture(log?.frags ?? [], opts.captured),
          { ...opts, mode: state.mode, unheld },
          {
            ...sourceFields(state, null),
            lastSourceStamp: raisedStamp(lastSourceStamp, state),
            resyncUnresolved: log?.gap === true,
            covered: raisedCover(covered, state.token, opts.holds),
          },
        );
        if (!loaded) return "invalid";
        return unheld.length > 0 ? "lost" : "installed";
      },
      // One store write and one notification, whether the door is HTTP or SSE.
      appendFragment: (frag) => get().actions.appendFragments([frag]),
      beginResync: () => {
        lastResyncToken += 1;
        resyncLogs.set(lastResyncToken, { frags: [], gap: false });
        set({ resyncUnresolved: true });
        return lastResyncToken;
      },
      endResync: (token) => {
        resyncLogs.delete(token);
      },
      appendFragments: (frags) => {
        if (resyncLogs.size > 0) {
          const kept = frags.flatMap((f) => replayable(f) ?? []);
          for (const log of resyncLogs.values()) for (const f of kept) log.frags.push(f);
        }
        const { sessions, warnings, mode, pendingSource } = get();
        if (pendingSource !== null) return true;
        const dropped: string[] = [];
        const unheld: MonitorSessionFragment[] = [];
        const next = mergeFragments(sessions, frags, dropped, mode, unheld);
        if (next !== sessions || dropped.length > 0) {
          set({
            sessions: next,
            ...(dropped.length > 0 ? { warnings: [...warnings, ...dropped] } : {}),
          });
        }
        // A fragment for a session not held yet is kept when the CURRENT
        // sync logged it (`isCurrentResync`): its install replays it, or
        // holds it already, and reports any it could not apply. An obsolete
        // sync's log is never installed.
        const replaying = isCurrentResync(lastResyncToken);
        return unheld.every((f) => replaying && replayable(f) !== null);
      },
      withholdCoverage: () => {
        for (const log of resyncLogs.values()) log.gap = true;
        if (!get().resyncUnresolved) set({ resyncUnresolved: true });
      },
      selectSession: (id) => set({ activeSessionId: id, range: null }),
      // The ONE door every range enters through — every caller (RangePicker's
      // Apply, the chart drag-zoom's clamp, the events-panel jump) funnels
      // here, so this is the one place to refuse a range that can never be
      // valid: `null` always passes (it means "follow the tail" / "the whole
      // session"), but a non-null `from >= to` is silently ignored, leaving
      // the existing range untouched. Downstream (metricsForSubject/
      // sliceSeries, every chart window) reads an inverted or empty range as
      // "0 samples in range" for EVERY series — no error, no signal, just a
      // silently blank dashboard. That state has more than one door: a caller
      // can pass an already-inverted/empty range directly (a malformed manual
      // edit), or an ORDERED range that `clampRange` (data/exportDoc.ts)
      // itself inverts when both endpoints sit outside `bounds` on the same
      // side (seen both in RangePicker's Apply and in the live follow
      // window's pre-session lead-in via a chart drag-zoom). Guarding here
      // once, rather than at each call site, is what makes the invariant
      // unconditional — the same "one rule, one place" shape as
      // `healthForHost` and `useIsPaused` below (paused is DERIVED from
      // `range`, never stored, precisely so it cannot disagree with it; this
      // guard is the same idea applied to `range` itself).
      //
      // The `>=` comparison above is a NaN blind spot: `NaN >= NaN` (and
      // every other NaN comparison) is `false`, so an inverted/empty check
      // alone lets a NaN range walk straight through. A NaN reaches here for
      // real — `parseTs` (data/time.ts) returns NaN, as `Date.parse` does,
      // for a malformed/non-ISO timestamp, and the events-panel jump builds
      // its range from a parsed event timestamp. `Number.isFinite` on both
      // edges closes that gap: it rejects NaN and +/-Infinity alike, which
      // are exactly as useless as a time range as an inverted one, and for
      // the same reason — this is the one boundary every range enters
      // through, so this is the one place that must catch them all.
      setRange: (range) => {
        if (range !== null) {
          if (!Number.isFinite(range.from) || !Number.isFinite(range.to)) return;
          if (range.from >= range.to) return;
        }
        set({ range });
      },
      setImportError: (message) => set({ importError: message }),
      setSourceState: (state) =>
        set({
          ...sourceFields(state, get().pendingSource),
          lastSourceStamp: raisedStamp(get().lastSourceStamp, state),
        }),
      setPendingSource: (state) =>
        set({
          ...sourceFields(get().source, state),
          lastSourceStamp: raisedStamp(get().lastSourceStamp, state),
        }),
      noteStreamedState: (state) =>
        set({ covered: raisedCover(get().covered, state.token, state.revision ?? 0) }),
      setSourceKindUnresolved: (unresolved) => set({ sourceKindUnresolved: unresolved }),
      addWarning: (message) => set({ warnings: [...get().warnings, message] }),
      setConnection: (connection) => set({ connection }),
      togglePause: () => {
        const { mode, range, sessions, activeSessionId, windowMs } = get();
        // `paused` is derived (see useIsPaused), never stored — so "currently
        // paused" IS "live mode with a pinned range", by construction. That
        // covers a chart drag-zoom's direct setRange(...) exactly the same as
        // a Pause click: either way, toggling resumes following instead of
        // freezing a second, redundant window on top of the user's own.
        if (mode === "live" && range !== null) {
          set({ range: null }); // resume -> follow the tail again
          return;
        }
        const session = sessions.find((s) => s.id === activeSessionId);
        // Freeze the CURRENTLY DERIVED window into an absolute range. Pause and "user
        // picked a custom range" are then the same state, so they cannot disagree.
        const to = session ? displayEndMs(session) : Date.now();
        set({ range: { from: to - windowMs, to } });
      },
      setWindow: (windowMs) => {
        const { range } = get();
        // Following: just resize the derived window (liveRange reads windowMs
        // on the next render — see the interface doc comment above). Paused:
        // `range` IS the pause, so keep its `to` and re-pin at the new width.
        set(
          range === null
            ? { windowMs }
            : { windowMs, range: { from: range.to - windowMs, to: range.to } },
        );
      },
    },
  };
});

/** What an export of the held sessions acknowledges: the installed source's
 * token and the revision its sessions are known to hold (`covered`), never
 * the newest count received. Null withholds the acknowledgement (the
 * download still happens): with no server source, while another source is
 * pending (the sessions held are not the ones it will hold), and while it
 * is withheld until an install (`resyncUnresolved`: a sync unresolved, or a
 * fragment lost). The ONE decision of what an
 * export saves: shell/ImportExport.tsx's downloadExport reads it in the same
 * turn as the sessions it serialises. */
export function exportAcknowledgement(
  s: Pick<ReviewState, "source" | "pendingSource" | "resyncUnresolved" | "covered">,
): SourceSavedBody | null {
  const { source, covered } = s;
  if (source === null || s.pendingSource !== null || s.resyncUnresolved) return null;
  if (covered?.token !== source.token) return null;
  return { token: source.token, revision: covered.revision };
}

export function useActiveSession(): NormalizedSession | null {
  return useReviewStore((s) => s.sessions.find((sess) => sess.id === s.activeSessionId) ?? null);
}

/** Derived, never stored: "paused" and "the user picked a custom range" are
 * ONE concept (see `togglePause`'s doc comment) — a zustand selector renders
 * exactly as stably as a stored field, and deriving it makes the two
 * structurally unable to disagree (unlike a separate boolean, which a
 * shipped bug proved CAN drift out of sync with `range`, e.g. via a chart
 * drag-zoom's direct `setRange` call). */
export function useIsPaused(): boolean {
  return useReviewStore((s) => s.mode === "live" && s.range !== null);
}

/** The ONE lookup of an event as the store holds it (normalised), by its
 * session and id; undefined when either is gone. `state` defaults to the
 * store now; a selector passes its own snapshot. */
export function storedEvent(
  sessionId: string,
  eventId: number,
  state: Pick<ReviewState, "sessions"> = useReviewStore.getState(),
): NormalizedSession["events"][number] | undefined {
  const session = state.sessions.find((s) => s.id === sessionId);
  return session?.events.find((e) => e.id === eventId);
}
