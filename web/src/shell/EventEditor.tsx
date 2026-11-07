// web/src/shell/EventEditor.tsx
// The event editor slide-over (spec 2026-07-18 §UI surfaces): draft targets
// create, edit targets send a full-field PATCH. Composed from the vendored
// slideout-menu's individually-exported pieces (Dialog/Modal/ModalOverlay +
// SlideoutMenu.Header/Content/Footer), same rationale as EventsPanel.tsx's
// header comment -- the all-in-one `SlideoutMenu` wrapper puts `data-testid`
// on the outer backdrop ModalOverlay, not the visible Dialog panel.
import { X } from "@untitledui/icons";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
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
import { EVENT_DASH_STYLES, type EventDash } from "../api/eventStyle.gen";
import { createEvent, deleteEvent, type EventUpdateInput, updateEvent } from "../data/eventApi";
import { customStyleName, isEventDash } from "../data/eventStyle";
import { storedEvent, useReviewStore } from "../data/reviewStore";
import { parseTs } from "../data/time";
import { calendarDateTimeToMs, msToCalendarDateTime } from "../ui/calendarTime";
import { EventColorField } from "../ui/EventColorField";
import { TextInput } from "../ui/TextInput";
import { type EventDraft, type EventEditorTarget, useUiStore } from "../ui/uiStore";
import { useOpenFocus } from "../ui/useOpenFocus";
import { ConfirmDeleteEvent } from "./ConfirmDeleteEvent";
import { DateFieldCompleteness, type DateFill } from "./DateFieldCompleteness";
import { settleOnOpening } from "./settleOnOpening";
import { editorGateKey, useMutationGate } from "./useMutationGate";

// The swatches (ui/EventColorField.tsx) and the dash list are the
// event-style registry's (otto/models/event_style.py, mirrored in
// api/eventStyle.gen.ts).

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
    color: string;
    dash: EventDash;
  },
): Form {
  return {
    sessionId,
    timestampMs: parseTs(record.timestamp),
    endTimestampMs: record.end_timestamp != null ? parseTs(record.end_timestamp) : null,
    label: record.label ?? "",
    color: record.color,
    dash: record.dash,
  };
}

/** The style fields the user chose for this target (even the value already
 * shown). */
interface StyleTouched {
  color: boolean;
  dash: boolean;
}

const UNTOUCHED: StyleTouched = { color: false, dash: false };

/** An update PATCH's style fields: only the ones the user chose, so a
 * label-only Save leaves the stored colour and dash -- a legacy one
 * (data/eventStyle.ts) included -- untouched on the server. A create always
 * sends both. */
function styleChanges(form: Form, touched: StyleTouched): Pick<EventUpdateInput, "color" | "dash"> {
  return {
    ...(touched.color && { color: form.color }),
    ...(touched.dash && { dash: form.dash }),
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
  // Save and Delete share one gate, keyed by the editor's target. One
  // request in flight per tab: a request pending for ANY event (or surface)
  // disables this editor, and only the pressing button spins.
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
    editor?.kind === "edit" ? (storedEvent(editor.sessionId, editor.eventId, s)?.label ?? "") : "",
  );
  // The stored record's dash when it is not a registry style
  // (data/eventStyle.ts): until the user picks a dash, the select shows it
  // as custom with NO selected key, so picking any dash -- the drawn default
  // included -- is a change and replaces it, as a colour pick does.
  const storedLegacyDash = useReviewStore((s) =>
    editor?.kind === "edit"
      ? storedEvent(editor.sessionId, editor.eventId, s)?.legacyDash
      : undefined,
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
  // Which style fields the user chose for this target: styleChanges.
  const [touched, setTouched] = useState<StyleTouched>(UNTOUCHED);
  // The edited record's non-hex stored colour (data/eventStyle.ts), if any.
  const [legacyColor, setLegacyColor] = useState<string | undefined>(undefined);

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
  //
  // It also reseeds ONCE more when a mutation that was pending at this
  // opening settles (#932): the gate is global, so that is ANY pending
  // mutation, not only one on this target. An opening that finds the gate
  // pending seeds from the store as it was, so that request's update lands
  // after the seed; re-reading the stored record when it settles is correct,
  // and harmless when it was another target's. Every field is disabled
  // meanwhile, so the form holds no edit of the user's to lose. A request
  // sent from THIS opening never reseeds: a failed Save keeps the user's
  // draft. A target whose record is gone (that request was a Delete) closes
  // the editor.
  const seeded = useRef<{ editor: EventEditorTarget | null; inherited: boolean }>({
    editor: null,
    inherited: false,
  });
  useEffect(() => {
    const fresh = seeded.current.editor !== editor;
    const inheritedSettled = seeded.current.inherited && !gate.pending;
    if (!fresh && !inheritedSettled) return;
    seeded.current = { editor, inherited: fresh && gate.pending };
    setError(null);
    setConfirmOpen(false);
    setDeleteError(null);
    if (editor === null) {
      setForm(null);
      return;
    }
    let next: Form;
    let legacy: string | undefined;
    if (editor.kind === "draft") {
      next = editor.draft;
    } else {
      const record = storedEvent(editor.sessionId, editor.eventId);
      if (!record) {
        closeEventEditor();
        return;
      }
      next = formFromRecord(editor.sessionId, record);
      legacy = record.legacyColor;
    }
    setForm(next);
    setTouched(UNTOUCHED);
    setLegacyColor(legacy);
    setTargetKey((k) => k + 1);
    setStartEmpty(false);
    setFill({ start: "complete", end: next.endTimestampMs == null ? "empty" : "complete" });
  }, [editor, gate.pending, closeEventEditor]);

  // The overlay below is mounted exactly while this holds.
  const labelFocus = useOpenFocus<HTMLInputElement>(editor !== null && form !== null);

  if (editor === null || form === null) return null;

  const startMsg = startMessage(fill.start, startEmpty);
  const endMsg = endMessage(form, fill, startEmpty);
  const invalid = form.label.trim() === "" || startMsg !== null || endMsg !== null;

  // Each request settles on the editor opening that sent it: a late result
  // never closes, or shows an error in, a newer opening (settleOnOpening).
  const save = () =>
    gate.run(
      () =>
        settleOnOpening(
          "eventEditor",
          () =>
            editor.kind === "draft"
              ? createEvent(form.sessionId, {
                  label: form.label,
                  timestamp: new Date(form.timestampMs).toISOString(),
                  ...(form.endTimestampMs !== null
                    ? { end_timestamp: new Date(form.endTimestampMs).toISOString() }
                    : {}),
                  color: form.color,
                  dash: form.dash,
                })
              : updateEvent(editor.sessionId, editor.eventId, {
                  label: form.label,
                  timestamp: new Date(form.timestampMs).toISOString(),
                  end_timestamp:
                    form.endTimestampMs !== null
                      ? new Date(form.endTimestampMs).toISOString()
                      : null,
                  ...styleChanges(form, touched),
                }),
          { done: closeEventEditor, fail: setError },
        ),
      SAVE,
    );

  // Confirm runs through the same gate as Save, so the two stay exclusive.
  const confirmDelete = () => {
    if (editor.kind !== "edit") return;
    setDeleteError(null);
    void gate.run(
      () =>
        settleOnOpening("eventEditor", () => deleteEvent(editor.sessionId, editor.eventId), {
          done: closeEventEditor,
          fail: setDeleteError,
        }),
      DELETE,
    );
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
          {labelFocus.scope}
          <SlideoutMenu.Header onClose={closeEventEditor}>
            <h2 className="text-sm font-semibold text-primary">
              {editor.kind === "edit" ? "Edit event" : "New event"}
            </h2>
          </SlideoutMenu.Header>
          <SlideoutMenu.Content className="gap-4 pb-4">
            <TextInput
              label="Label"
              testId="editor-label"
              // The editor opens with focus here, so a nested overlay
              // (Custom…, the dash Select, the delete confirmation) restores
              // focus when it closes: ui/useOpenFocus.ts (#957).
              inputRef={labelFocus.ref}
              // Every draft field shares Delete's predicate: nothing the
              // draft holds can change under an in-flight request.
              isDisabled={gate.pending}
              value={form.label}
              onChange={(value) => setForm({ ...form, label: value })}
            />
            <InputDate
              key={targetKey}
              label="Start"
              granularity="second"
              data-testid="editor-start"
              isDisabled={gate.pending}
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
                isDisabled={gate.pending}
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
                isDisabled={
                  gate.pending || (form.endTimestampMs === null && fill.end !== "partial")
                }
                onClick={() => {
                  setForm({ ...form, endTimestampMs: null });
                  setEndKey((k) => k + 1);
                }}
              />
            </div>
            <EventColorField
              // Remounts per target, like the InputDates: its custom slot
              // is per-editor-session state and must not carry over.
              key={`color:${targetKey}`}
              value={form.color}
              dash={form.dash}
              isDisabled={gate.pending}
              // Once a colour is picked the repair is made: the note goes.
              {...(!touched.color && legacyColor !== undefined && { legacyColor })}
              onChange={(color) => {
                setTouched((t) => ({ ...t, color: true }));
                setForm({ ...form, color });
              }}
            />
            <Select
              aria-label="Dash style"
              label="Dash"
              data-testid="editor-dash"
              isDisabled={gate.pending}
              items={EVENT_DASH_STYLES.map((d) => ({ id: d, label: d }))}
              {...(!touched.dash && storedLegacyDash !== undefined
                ? { selectedKey: null, placeholder: customStyleName(storedLegacyDash) }
                : { selectedKey: form.dash })}
              onSelectionChange={(key) => {
                const dash = String(key);
                if (key === null || !isEventDash(dash)) return;
                setTouched((t) => ({ ...t, dash: true }));
                setForm({ ...form, dash });
              }}
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
