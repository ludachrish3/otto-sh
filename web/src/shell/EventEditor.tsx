// web/src/shell/EventEditor.tsx
// The event editor slide-over (spec 2026-07-18 §UI surfaces, Plan 5c Task
// 9): draft targets create, edit targets send a full-field PATCH. Composed
// from the vendored slideout-menu's individually-exported pieces (Dialog/
// Modal/ModalOverlay + SlideoutMenu.Header/Content/Footer), same rationale
// as EventsPanel.tsx's header comment -- the all-in-one `SlideoutMenu`
// wrapper puts `data-testid` on the outer backdrop ModalOverlay, not the
// visible Dialog panel.
import { X } from "@untitledui/icons";
import { useEffect, useState } from "react";
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
import { createEvent, deleteEvent, updateEvent } from "../data/eventApi";
import { useReviewStore } from "../data/reviewStore";
import { parseTs } from "../data/time";
import { calendarDateTimeToMs, msToCalendarDateTime } from "../ui/calendarTime";
import { TextInput } from "../ui/TextInput";
import { type EventDraft, useUiStore } from "../ui/uiStore";

// Default manual grey + the app's event accent + the auto lifecycle colors --
// mirrors AUTO_EVENT_COLORS (otto/monitor/events.py: start/pass/fail =
// #888888/#2ca02c/#d62728) plus two extra chart hues for manual marking.
// Not exported (knip's unused-exports check) -- nothing outside this module
// consumes it yet; the brief's snippet marked it `export` in anticipation of
// a future consumer, but Task 9 has none, and a dead export is exactly what
// that gate is there to catch.
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

export function EventEditor() {
  const editor = useUiStore((s) => s.eventEditor);
  const { closeEventEditor } = useUiStore((s) => s.actions);
  const [form, setForm] = useState<Form | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [deleteArmed, setDeleteArmed] = useState(false);

  // Reseeds on the TARGET's identity, not on the session -- keying this off
  // a reactive `session` value instead would re-run every time the active
  // session's data changes for ANY reason (live-streamed samples included),
  // wiping an in-progress edit out from under the user. `useReviewStore` is
  // a stable module-level import; `.getState()` is a plain, one-shot pull,
  // exactly like marking.ts's requireActiveSessionId -- not a reactive
  // dependency Biome's exhaustive-deps rule needs to see in the array.
  useEffect(() => {
    setError(null);
    setDeleteArmed(false);
    if (editor === null) {
      setForm(null);
      return;
    }
    if (editor.kind === "draft") {
      setForm(editor.draft);
      return;
    }
    const session = useReviewStore.getState().sessions.find((s) => s.id === editor.sessionId);
    const record = session?.events.find((ev) => ev.id === editor.eventId);
    setForm(record ? formFromRecord(editor.sessionId, record) : null);
  }, [editor]);

  if (editor === null || form === null) return null;

  const invalid =
    form.label.trim() === "" ||
    (form.endTimestampMs !== null && form.endTimestampMs <= form.timestampMs);

  const save = async () => {
    try {
      if (editor.kind === "draft") {
        await createEvent(form.sessionId, {
          label: form.label,
          timestamp: new Date(form.timestampMs).toISOString(),
          ...(form.endTimestampMs !== null
            ? { end_timestamp: new Date(form.endTimestampMs).toISOString() }
            : {}),
          color: form.color,
          dash: form.dash,
        });
      } else {
        await updateEvent(editor.sessionId, editor.eventId, {
          label: form.label,
          timestamp: new Date(form.timestampMs).toISOString(),
          end_timestamp:
            form.endTimestampMs !== null ? new Date(form.endTimestampMs).toISOString() : null,
          color: form.color,
          dash: form.dash,
        });
      }
      closeEventEditor();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const onDelete = async () => {
    if (editor.kind !== "edit") return;
    if (!deleteArmed) {
      setDeleteArmed(true);
      return;
    }
    try {
      await deleteEvent(editor.sessionId, editor.eventId);
      closeEventEditor();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  return (
    <ModalOverlay
      isOpen
      onOpenChange={(open) => {
        if (!open) closeEventEditor();
      }}
      isDismissable
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
              label="Start"
              granularity="second"
              data-testid="editor-start"
              value={msToCalendarDateTime(form.timestampMs)}
              onChange={(v) => v && setForm({ ...form, timestampMs: calendarDateTimeToMs(v) })}
            />
            <div className="flex items-end gap-2">
              <div className="grow">
                <InputDate
                  label="End"
                  granularity="second"
                  data-testid="editor-end"
                  value={
                    form.endTimestampMs === null ? null : msToCalendarDateTime(form.endTimestampMs)
                  }
                  onChange={(v) =>
                    v && setForm({ ...form, endTimestampMs: calendarDateTimeToMs(v) })
                  }
                />
              </div>
              <ButtonUtility
                aria-label="Clear end (point event)"
                tooltip="Clear end (point event)"
                icon={X}
                size="sm"
                data-testid="editor-end-clear"
                isDisabled={form.endTimestampMs === null}
                onClick={() => setForm({ ...form, endTimestampMs: null })}
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
              {(item) => <Select.Item id={item.id} label={item.label} />}
            </Select>
            {error !== null && (
              <p data-testid="editor-error" className="text-xs text-error-primary">
                {error}
              </p>
            )}
          </SlideoutMenu.Content>
          <SlideoutMenu.Footer className="flex items-center justify-between gap-2">
            <div>
              {editor.kind === "edit" && (
                <Button
                  size="sm"
                  color={deleteArmed ? "primary-destructive" : "secondary-destructive"}
                  data-testid="editor-delete"
                  onPress={() => void onDelete()}
                >
                  {deleteArmed ? "Really delete?" : "Delete"}
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
                isDisabled={invalid}
                data-testid="editor-save"
                onPress={() => void save()}
              >
                Save
              </Button>
            </div>
          </SlideoutMenu.Footer>
        </Dialog>
      </Modal>
    </ModalOverlay>
  );
}
