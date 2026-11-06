// Events slide-over (UX spec §11): reverse-chron list; a row jumps the
// charts to its time (±15 min, clamped). Marking/editing: live+editable
// sessions get an inline compose row (Mark/Start/Stop, mirroring
// MarkControl's flows); review+editable sessions get an "Add event…"
// button opening the shared EventEditor draft target.
// Per-row affordances (edit / End now) apply only to id'd (real, non-
// synthetic) events -- there is nothing on the server to address a
// synthetic negative id by.
//
// Composed from the vendored slideout-menu's individually-exported pieces
// (Dialog/Modal/ModalOverlay + the SlideoutMenu.Header/Content slots) rather
// than the all-in-one `SlideoutMenu` wrapper: that wrapper spreads unknown
// props (including `data-testid`) onto the OUTER `ModalOverlay` — the
// full-viewport backdrop `<div>` — not the visible Dialog panel. A vitest
// `getByTestId` still finds it (the Dialog nests inside the overlay's DOM
// subtree), but a pixel-hit-testing click would land on the backdrop's
// bounding box (the whole viewport), not the right-anchored panel. Manual
// composition puts `data-testid="events-panel"` directly on `Dialog`,
// matching where the pre-migration SlideOver put it.
import { Edit01 } from "@untitledui/icons";
import { useEffect, useState } from "react";

import {
  Dialog,
  Modal,
  ModalOverlay,
  SlideoutMenu,
} from "@/components/application/slideout-menus/slideout-menu";
import { Button } from "@/components/base/buttons/button";
import { ButtonUtility } from "@/components/base/buttons/button-utility";
import {
  endEvent,
  isStale,
  reportMutationFailure,
  type StaleResult,
  sourceGuard,
} from "../data/eventApi";
import { clampRange, type NormalizedSession, sessionBounds } from "../data/exportDoc";
import { useActiveSession, useReviewStore } from "../data/reviewStore";
import { formatClockTime, formatSpan, parseTs } from "../data/time";
import { TextInput } from "../ui/TextInput";
import { type EventEditorTarget, useUiStore } from "../ui/uiStore";
import { blankDraft, endOpenSpan, markNow, startSpan } from "./marking";
import { endNowGateKey, markGateKey, useMutationGate } from "./useMutationGate";

const JUMP_PAD_MS = 15 * 60_000;

/** The compose row's action names on the session's shared mark gate. */
const COMPOSE_MARK = "events-mark";
const COMPOSE_START = "events-start";
const COMPOSE_STOP = "events-stop";

/** The compose row: live+editable sessions mark/start/stop inline; review+
 * editable sessions get "Add event…" (there's no "now" to mark/start/end in
 * a review session). Rendered only when `editable` (see EventsPanel below)
 * -- a read-only server must not offer affordances it cannot fulfil. */
function EventsComposeRow(props: {
  session: NormalizedSession;
  mode: "live" | "review" | null;
  openEventEditor: (target: EventEditorTarget) => void;
}) {
  const { session, mode, openEventEditor } = props;
  const openSpan = useUiStore((s) => s.openSpan);
  const [label, setLabel] = useState("");
  const [error, setError] = useState<string | null>(null);
  // One request in flight for this session's marking across Mark, Start and
  // Stop, shared with the AppBar's MarkControl and the palette; held outside
  // this row, so it survives the panel closing (see useMutationGate.ts).
  const gate = useMutationGate(markGateKey(session.id));
  // The pressed button spins; its siblings are disabled meanwhile.
  const busy = (name: string) => ({
    isLoading: gate.pendingAction === name,
    isDisabled: gate.pending && gate.pendingAction !== name,
  });

  if (mode !== "live") {
    return (
      <div data-testid="events-compose">
        <Button
          size="sm"
          color="secondary"
          data-testid="events-compose-add"
          onPress={() => openEventEditor({ kind: "draft", draft: blankDraft(session) })}
        >
          Add event…
        </Button>
      </div>
    );
  }

  const spanOpen = openSpan?.sessionId === session.id;

  // Same catch shape as MarkControl's submit: the caller (this
  // control) owns the error surface, one inline slot shared by both flows.
  // A stale result means a file import overtook the request: this row is
  // already gone, so nothing is reset.
  const run = (action: (text: string) => Promise<StaleResult | undefined>, actionName: string) => {
    if (!label.trim()) return Promise.resolve();
    return gate.run(async () => {
      try {
        const current = sourceGuard();
        if (isStale(current(await action(label)))) return;
        setLabel("");
        setError(null);
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      }
    }, actionName);
  };

  const stop = () =>
    gate.run(async () => {
      try {
        const current = sourceGuard();
        if (isStale(current(await endOpenSpan()))) return;
        setError(null);
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      }
    }, COMPOSE_STOP);

  const stopBusy = busy(COMPOSE_STOP);
  return (
    <div data-testid="events-compose" className="flex flex-wrap items-end gap-2 pb-1">
      <TextInput label="Label" testId="events-compose-label" value={label} onChange={setLabel} />
      <Button
        size="sm"
        color="secondary"
        data-testid="events-compose-mark"
        {...busy(COMPOSE_MARK)}
        showTextWhileLoading
        onPress={() => void run(markNow, COMPOSE_MARK)}
      >
        Mark
      </Button>
      <Button
        size="sm"
        color="secondary"
        data-testid="events-compose-start"
        {...busy(COMPOSE_START)}
        showTextWhileLoading
        onPress={() => void run(startSpan, COMPOSE_START)}
      >
        Start
      </Button>
      <Button
        size="sm"
        color="secondary"
        data-testid="events-compose-stop"
        isLoading={stopBusy.isLoading}
        showTextWhileLoading
        isDisabled={!spanOpen || stopBusy.isDisabled}
        onPress={() => void stop()}
      >
        Stop
      </Button>
      {error !== null && (
        <p data-testid="events-compose-error" className="w-full text-xs text-error-primary">
          {error}
        </p>
      )}
    </div>
  );
}

/** One row's End now: each row has its own gate key, so ending one event
 * never blocks another, and a repeat press on the same row is ignored, even
 * across the panel closing and reopening. */
function EndNowButton(props: { sessionId: string; eventId: number }) {
  const { sessionId, eventId } = props;
  const gate = useMutationGate(endNowGateKey(sessionId, eventId));
  const onPress = () =>
    gate.run(async () => {
      const onFailure = reportMutationFailure("End now");
      await endEvent(sessionId, eventId).then(() => undefined, onFailure);
    });
  return (
    <Button
      size="sm"
      color="secondary"
      data-testid={`event-endnow-${eventId}`}
      isLoading={gate.pending}
      showTextWhileLoading
      onPress={() => void onPress()}
    >
      End now
    </Button>
  );
}

export function EventsPanel(props: { isOpen: boolean; onClose: () => void }) {
  const { isOpen, onClose } = props;
  const session = useActiveSession();
  const setRange = useReviewStore((s) => s.actions.setRange);
  const mode = useReviewStore((s) => s.mode);
  const editable = useReviewStore((s) => s.editable);
  const openEventEditor = useUiStore((s) => s.actions.openEventEditor);
  const [jumpNotice, setJumpNotice] = useState<string | null>(null);

  // Cleared on reopen: a refusal from the panel's last time open must not
  // linger into the next one.
  useEffect(() => {
    if (isOpen) setJumpNotice(null);
  }, [isOpen]);

  if (!session) return null;

  const rows = session.events
    .map((ev, i) => ({
      // Wire ids are non-negative; negative synthetics can't collide (matches eventMarkers).
      id: ev.id ?? -1 - i,
      wireId: ev.id ?? null,
      label: ev.label ?? "",
      color: ev.color ?? "#7c5cff",
      source: ev.source ?? "manual",
      fromMs: parseTs(ev.timestamp),
      toMs: ev.end_timestamp != null ? parseTs(ev.end_timestamp) : null,
    }))
    .sort((a, b) => b.fromMs - a.fromMs);

  const jump = (fromMs: number, toMs: number | null) => {
    const clamped = clampRange(
      { from: fromMs - JUMP_PAD_MS, to: (toMs ?? fromMs) + JUMP_PAD_MS },
      sessionBounds(session),
    );
    if (clamped.from >= clamped.to) {
      // setRange would refuse this silently (its inverted-range guard) and
      // the panel would close on a no-op — the recorded follow-up. Name it
      // instead and stay open.
      setJumpNotice("Outside the session's time range");
      return;
    }
    setJumpNotice(null);
    setRange(clamped);
    onClose();
  };

  return (
    <ModalOverlay
      isOpen={isOpen}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
      isDismissable
      // z-50 matches the command-palette scrim (CommandMenu.tsx) and must stay
      // >= app-content z-index: the subject page's live-window Tabs paint their
      // pill at a z-10 flex-item stacking context that hoists to the document
      // root, so the vendored scrim's auto/absent z-index let them bleed on top
      // of the open panel. Keep it AT 50, not higher: the panel's own react-aria
      // overlays must still render above it — tooltips are explicitly z-50, and
      // popovers portal later in the DOM (react-aria's own overlay z), so
      // equal-or-later wins.
      //
      // It rides here rather than on the vendored slideout-menu.tsx because that
      // file is copy-in vendored source that must stay byte-identical to what
      // the untitledui CLI emits (web/README.md's never-hand-edit rule) — a
      // hand-edit there reads as upstream drift forever
      // (scripts/check_untitledui_drift.sh). The vendored ModalOverlay feeds
      // props.className through cx (tailwind-merge) last, so this lands on the
      // same backdrop <div> and wins the z-index group.
      className="z-50"
    >
      <Modal>
        <Dialog data-testid="events-panel" aria-label="Events">
          <SlideoutMenu.Header onClose={onClose}>
            <h2 className="text-sm font-semibold text-primary">Events</h2>
          </SlideoutMenu.Header>
          <SlideoutMenu.Content className="gap-1 pb-4">
            {editable && (
              <EventsComposeRow session={session} mode={mode} openEventEditor={openEventEditor} />
            )}
            {jumpNotice !== null && (
              <p data-testid="jump-notice" className="text-xs text-error-primary">
                {jumpNotice}
              </p>
            )}
            {rows.length === 0 && (
              <p className="text-sm text-tertiary">No events in this session.</p>
            )}
            <ul className="flex flex-col gap-1">
              {rows.map((ev) => {
                const wireId = ev.wireId;
                return (
                  <li key={ev.id} className="flex items-center gap-1">
                    <button
                      type="button"
                      data-testid={`event-row-${ev.id}`}
                      onClick={() => jump(ev.fromMs, ev.toMs)}
                      className="flex min-w-0 grow cursor-pointer items-center gap-2 rounded-lg
                        px-2 py-1.5 text-left text-sm hover:bg-primary_hover"
                    >
                      <span
                        aria-hidden
                        className="h-3 w-3 shrink-0 rounded-sm"
                        style={{ backgroundColor: ev.color }}
                      />
                      <span className="min-w-0 grow truncate">{ev.label}</span>
                      <span className="shrink-0 text-xs text-tertiary">
                        {formatClockTime(ev.fromMs)}
                        {ev.toMs !== null ? ` · ${formatSpan(ev.fromMs, ev.toMs)}` : ""}
                      </span>
                    </button>
                    {/* Every mutation affordance is gated on `editable` (spec
                        §Marking, same rule commands.ts applies to its own
                        rows) -- a read-only server, or a review session
                        opened from a plain export (not a .db), must not
                        offer an edit/End-now control it cannot fulfil. */}
                    {editable && wireId !== null && (
                      <ButtonUtility
                        aria-label="Edit event"
                        tooltip="Edit"
                        icon={Edit01}
                        color="tertiary"
                        size="sm"
                        data-testid={`event-edit-${wireId}`}
                        onClick={() =>
                          openEventEditor({ kind: "edit", sessionId: session.id, eventId: wireId })
                        }
                      />
                    )}
                    {editable && mode === "live" && wireId !== null && ev.toMs === null && (
                      <EndNowButton sessionId={session.id} eventId={wireId} />
                    )}
                  </li>
                );
              })}
            </ul>
          </SlideoutMenu.Content>
        </Dialog>
      </Modal>
    </ModalOverlay>
  );
}
