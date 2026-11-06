// web/src/shell/EventEditor.tsx
// The event editor slide-over (spec 2026-07-18 §UI surfaces): draft targets
// create, edit targets send a full-field PATCH. Composed from the vendored
// slideout-menu's individually-exported pieces (Dialog/Modal/ModalOverlay +
// SlideoutMenu.Header/Content/Footer), same rationale as EventsPanel.tsx's
// header comment -- the all-in-one `SlideoutMenu` wrapper puts `data-testid`
// on the outer backdrop ModalOverlay, not the visible Dialog panel.
import { X } from "@untitledui/icons";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Dialog,
  Modal,
  ModalOverlay,
  SlideoutMenu,
} from "@/components/application/slideout-menus/slideout-menu";
import { Button } from "@/components/base/buttons/button";
import { ButtonUtility } from "@/components/base/buttons/button-utility";
import { InputDate } from "@/components/base/input/input-date";
import { Select } from "@/components/base/select/select";
import { cx } from "@/utils/cx";
import type { EventRecord } from "../api/export.gen";
import {
  createEvent,
  deleteEvent,
  isStale,
  type StaleResult,
  sourceGuard,
  updateEvent,
} from "../data/eventApi";
import { useReviewStore } from "../data/reviewStore";
import { parseTs } from "../data/time";
import { calendarDateTimeToMs, msToCalendarDateTime } from "../ui/calendarTime";
import { TextInput } from "../ui/TextInput";
import { type EventDraft, useUiStore } from "../ui/uiStore";
import { ConfirmDeleteEvent } from "./ConfirmDeleteEvent";
import { DateFieldCompleteness, type DateFill } from "./DateFieldCompleteness";
import { editorGateKey, useMutationGate } from "./useMutationGate";

// Default manual grey + the app's event accent + the auto lifecycle colors --
// mirrors AUTO_EVENT_COLORS (otto/monitor/events.py: start/pass/fail =
// #888888/#2ca02c/#d62728) plus two extra chart hues for manual marking.
// Not exported (knip's unused-exports check) -- nothing outside this module
// consumes it yet; an earlier draft marked it `export` in anticipation of a
// future consumer that never arrived, and a dead export is exactly what that
// gate is there to catch.
const EVENT_COLOR_SWATCHES = [
  "#888888",
  "#7c5cff",
  "#2ca02c",
  "#d62728",
  "#1f77b4",
  "#ff7f0e",
] as const;

// Mirrors otto/models/monitor.py's VALID_DASH_STYLES. No TS export exists
// for it (that module is Python-only) -- this list is hand-kept in sync; a
// mismatched entry just 422s loudly (server-side validation catches it),
// it does not silently corrupt data.
const DASH_STYLES = ["solid", "dot", "dash", "longdash", "dashdot", "longdashdot"] as const;

type Form = EventDraft;

// How filled-in each date field's segments are, as DateFieldCompleteness
// reports them. The form's epochs cannot say this: a partially cleared
// field never calls onChange, so its epoch still holds the old instant.
interface Fill {
  start: DateFill;
  end: DateFill;
}

function startMessage(fill: DateFill, startEmpty: boolean): string | null {
  if (fill === "partial") return "Enter a complete start time.";
  if (startEmpty || fill === "empty") return "Enter a start time.";
  return null;
}

function endMessage(form: Form, fill: Fill, startEmpty: boolean): string | null {
  if (fill.end === "partial") return "Enter a complete end time, or clear it with ✕.";
  // Only judge the range against a Start the user can see: while Start is
  // incomplete its held epoch is not what the field shows.
  const startShown = !startEmpty && fill.start === "complete";
  if (startShown && form.endTimestampMs !== null && form.endTimestampMs <= form.timestampMs) {
    return "End must be after Start.";
  }
  return null;
}

function formFromRecord(
  sessionId: string,
  record: {
    timestamp: string;
    end_timestamp?: string | null;
    label?: string;
    color?: string;
    dash?: string;
  },
): Form {
  return {
    sessionId,
    timestampMs: parseTs(record.timestamp),
    endTimestampMs: record.end_timestamp != null ? parseTs(record.end_timestamp) : null,
    label: record.label ?? "",
    color: record.color ?? "#888888",
    dash: record.dash ?? "dash",
  };
}

/** The editor's action names on its target's gate. */
const SAVE = "save";
const DELETE = "delete";

export function EventEditor() {
  const editor = useUiStore((s) => s.eventEditor);
  const { closeEventEditor } = useUiStore((s) => s.actions);
  const [form, setForm] = useState<Form | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Save and Delete share one gate, keyed by the editor's target: one
  // request in flight per event, and a request still pending for one event
  // never makes another event's editor look busy.
  const gate = useMutationGate(
    editor === null
      ? ""
      : editor.kind === "edit"
        ? editorGateKey(editor.sessionId, editor.eventId)
        : editorGateKey(editor.draft.sessionId, null),
  );
  // The delete dialog names the STORED record, not the unsaved form: the
  // delete removes what is stored, whatever the label field holds now.
  const storedLabel = useReviewStore((s) =>
    editor?.kind === "edit"
      ? (s.sessions
          .find((sess) => sess.id === editor.sessionId)
          ?.events.find((ev) => ev.id === editor.eventId)?.label ?? "")
      : "",
  );
  // The delete confirmation dialog, and the failure it shows (the dialog
  // owns its own error so it stays open to retry).
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [fill, setFill] = useState<Fill>({ start: "complete", end: "empty" });
  // Start was fully cleared (its field called onChange(null)). form.timestampMs
  // keeps the last valid epoch only so the user can retype; it is never saved
  // while this is set, and Start's controlled value is null meanwhile.
  const [startEmpty, setStartEmpty] = useState(false);
  // Bumped by the clear-end button: re-keying End's InputDate drops any
  // partially typed segments along with the value.
  const [endKey, setEndKey] = useState(0);
  // Bumped on every reseed, so both InputDates remount per editor target.
  // A new target whose instants equal the old one's hands the fields
  // equal epochs, so their memoised values keep their identity and
  // react-stately would keep the previous target's half-typed segments.
  // A counter, not the target's ids: two drafts have no ids to tell apart.
  const [targetKey, setTargetKey] = useState(0);

  // Each field's controlled value is memoised on its epoch: react-stately
  // resets a field's displayed segments whenever the value's IDENTITY
  // changes, so a fresh object per render would erase a half-typed edit on
  // the very re-render a completeness report causes.
  const timestampMs = form?.timestampMs;
  const endTimestampMs = form?.endTimestampMs;
  const startValue = useMemo(
    () => (timestampMs === undefined ? null : msToCalendarDateTime(timestampMs)),
    [timestampMs],
  );
  const endValue = useMemo(
    () =>
      endTimestampMs === undefined || endTimestampMs === null
        ? null
        : msToCalendarDateTime(endTimestampMs),
    [endTimestampMs],
  );
  const onStartFill = useCallback(
    (f: DateFill) => setFill((prev) => (prev.start === f ? prev : { ...prev, start: f })),
    [],
  );
  const onEndFill = useCallback(
    (f: DateFill) => setFill((prev) => (prev.end === f ? prev : { ...prev, end: f })),
    [],
  );

  // Reseeds on the TARGET's identity, not on the session -- keying this off
  // a reactive `session` value instead would re-run every time the active
  // session's data changes for ANY reason (live-streamed samples included),
  // wiping an in-progress edit out from under the user. `useReviewStore` is
  // a stable module-level import; `.getState()` is a plain, one-shot pull,
  // exactly like marking.ts's requireActiveSessionId -- not a reactive
  // dependency Biome's exhaustive-deps rule needs to see in the array.
  useEffect(() => {
    setError(null);
    setConfirmOpen(false);
    setDeleteError(null);
    if (editor === null) {
      setForm(null);
      return;
    }
    let next: Form | null;
    if (editor.kind === "draft") {
      next = editor.draft;
    } else {
      const session = useReviewStore.getState().sessions.find((s) => s.id === editor.sessionId);
      const record = session?.events.find((ev) => ev.id === editor.eventId);
      next = record ? formFromRecord(editor.sessionId, record) : null;
    }
    setForm(next);
    setTargetKey((k) => k + 1);
    setStartEmpty(false);
    setFill({ start: "complete", end: next?.endTimestampMs == null ? "empty" : "complete" });
  }, [editor]);

  if (editor === null || form === null) return null;

  const startMsg = startMessage(fill.start, startEmpty);
  const endMsg = endMessage(form, fill, startEmpty);
  const invalid = form.label.trim() === "" || startMsg !== null || endMsg !== null;

  const save = () =>
    gate.run(async () => {
      try {
        // A stale result: a file import overtook the request and already
        // closed the editor (data/source.ts), so there is nothing left to do.
        const current = sourceGuard();
        let result: EventRecord | StaleResult;
        if (editor.kind === "draft") {
          result = await createEvent(form.sessionId, {
            label: form.label,
            timestamp: new Date(form.timestampMs).toISOString(),
            ...(form.endTimestampMs !== null
              ? { end_timestamp: new Date(form.endTimestampMs).toISOString() }
              : {}),
            color: form.color,
            dash: form.dash,
          });
        } else {
          result = await updateEvent(editor.sessionId, editor.eventId, {
            label: form.label,
            timestamp: new Date(form.timestampMs).toISOString(),
            end_timestamp:
              form.endTimestampMs !== null ? new Date(form.endTimestampMs).toISOString() : null,
            color: form.color,
            dash: form.dash,
          });
        }
        if (isStale(current(result))) return;
        closeEventEditor();
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      }
    }, SAVE);

  // Confirm runs through the same gate as Save, so the two stay exclusive.
  const confirmDelete = () => {
    if (editor.kind !== "edit") return;
    setDeleteError(null);
    void gate.run(async () => {
      try {
        const current = sourceGuard();
        if (isStale(current(await deleteEvent(editor.sessionId, editor.eventId)))) return;
        closeEventEditor();
      } catch (err) {
        setDeleteError(err instanceof Error ? err.message : String(err));
      }
    }, DELETE);
  };

  return (
    <ModalOverlay
      isOpen
      onOpenChange={(open) => {
        if (!open) closeEventEditor();
      }}
      isDismissable
      // Stacks the scrim above the live-window Tabs' hoisted z-10 pill; same
      // rationale (and the same reason it lives on the call site rather than in
      // the vendored slideout-menu.tsx) as EventsPanel.tsx's ModalOverlay.
      className="z-50"
    >
      <Modal>
        <Dialog
          data-testid="event-editor"
          aria-label={editor.kind === "edit" ? "Edit event" : "New event"}
        >
          <SlideoutMenu.Header onClose={closeEventEditor}>
            <h2 className="text-sm font-semibold text-primary">
              {editor.kind === "edit" ? "Edit event" : "New event"}
            </h2>
          </SlideoutMenu.Header>
          <SlideoutMenu.Content className="gap-4 pb-4">
            <TextInput
              label="Label"
              testId="editor-label"
              value={form.label}
              onChange={(value) => setForm({ ...form, label: value })}
            />
            <InputDate
              key={targetKey}
              label="Start"
              granularity="second"
              data-testid="editor-start"
              value={startEmpty ? null : startValue}
              isInvalid={startMsg !== null}
              hint={<DateFieldCompleteness onFillChange={onStartFill} message={startMsg} />}
              onChange={(v) => {
                // A fully cleared field reports null, then redisplays its
                // controlled value -- so Start goes null rather than letting
                // the old date reappear.
                if (v === null) {
                  setStartEmpty(true);
                  return;
                }
                setStartEmpty(false);
                setForm({ ...form, timestampMs: calendarDateTimeToMs(v) });
              }}
            />
            {/* A label / input / hint grid: End's field spans its three rows
                as a subgrid, so the ✕ in the input row stays level with the
                input whether or not a message shows under it. */}
            <div className="grid grid-cols-[1fr_auto] grid-rows-[auto_auto_auto] gap-x-2 gap-y-1.5">
              <InputDate
                key={`${targetKey}:${endKey}`}
                label="End"
                granularity="second"
                data-testid="editor-end"
                className="col-start-1 row-span-3 row-start-1 grid grid-cols-1 grid-rows-subgrid"
                value={endValue}
                isInvalid={endMsg !== null}
                hint={<DateFieldCompleteness onFillChange={onEndFill} message={endMsg} />}
                // A fully cleared End is a point event, not an error.
                onChange={(v) =>
                  setForm({
                    ...form,
                    endTimestampMs: v === null ? null : calendarDateTimeToMs(v),
                  })
                }
              />
              <ButtonUtility
                className="col-start-2 row-start-2 self-center"
                aria-label="Clear end (point event)"
                tooltip="Clear end (point event)"
                icon={X}
                size="sm"
                data-testid="editor-end-clear"
                isDisabled={form.endTimestampMs === null && fill.end !== "partial"}
                onClick={() => {
                  setForm({ ...form, endTimestampMs: null });
                  setEndKey((k) => k + 1);
                }}
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <span className="text-sm font-medium text-secondary">Color</span>
              <div className="flex gap-2">
                {EVENT_COLOR_SWATCHES.map((hex) => (
                  <button
                    key={hex}
                    type="button"
                    aria-label={`Color ${hex}`}
                    aria-pressed={form.color === hex}
                    data-testid={`editor-color-${hex}`}
                    onClick={() => setForm({ ...form, color: hex })}
                    className={cx(
                      "size-6 shrink-0 cursor-pointer rounded-full ring-2 ring-offset-2 ring-offset-primary transition",
                      form.color === hex ? "ring-brand" : "ring-transparent hover:ring-secondary",
                    )}
                    style={{ backgroundColor: hex }}
                  />
                ))}
              </div>
            </div>
            <Select
              aria-label="Dash style"
              label="Dash"
              data-testid="editor-dash"
              items={DASH_STYLES.map((d) => ({ id: d, label: d }))}
              selectedKey={form.dash}
              onSelectionChange={(key) => key !== null && setForm({ ...form, dash: String(key) })}
            >
              {/* Conditional spread, not `label={item.label}`: the vendored
                  `SelectItemProps` declares `label?: string`, and the render
                  callback types `item.label` as `string | undefined`. */}
              {(item) => (
                <Select.Item
                  id={item.id}
                  {...(item.label !== undefined && { label: item.label })}
                />
              )}
            </Select>
            {error !== null && (
              <p data-testid="editor-error" role="alert" className="text-xs text-error-primary">
                {error}
              </p>
            )}
          </SlideoutMenu.Content>
          <SlideoutMenu.Footer className="flex items-center justify-between gap-2">
            <div>
              {editor.kind === "edit" && (
                <Button
                  size="sm"
                  color="secondary-destructive"
                  data-testid="editor-delete"
                  isDisabled={gate.pending}
                  onPress={() => setConfirmOpen(true)}
                >
                  Delete
                </Button>
              )}
            </div>
            <div className="flex gap-2">
              <Button
                size="sm"
                color="secondary"
                data-testid="editor-cancel"
                onPress={closeEventEditor}
              >
                Cancel
              </Button>
              <Button
                size="sm"
                color="primary"
                isDisabled={invalid || (gate.pending && gate.pendingAction !== SAVE)}
                data-testid="editor-save"
                isLoading={gate.pendingAction === SAVE}
                showTextWhileLoading
                onPress={() => void save()}
              >
                Save
              </Button>
            </div>
          </SlideoutMenu.Footer>
          <ConfirmDeleteEvent
            isOpen={confirmOpen}
            eventLabel={storedLabel}
            pending={gate.pendingAction === DELETE}
            error={deleteError}
            onCancel={() => {
              setConfirmOpen(false);
              setDeleteError(null);
            }}
            onConfirm={confirmDelete}
          />
        </Dialog>
      </Modal>
    </ModalOverlay>
  );
}
