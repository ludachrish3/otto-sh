// The event edit popover: opens on a chart annotation click (`ChartGrid`
// wires Plotly's `plotly_clickannotation` through the store's
// `editingEventId`/`popoverAnchor`, set by `ChartPanel`'s handler), and
// offers save/delete/cancel + outside-click-to-close. Mirrors dashboard.js's
// §Event popover (`openPopover`/`hidePopover` + the `popover-save`/
// `popover-delete`/`popover-cancel` click handlers + the top-level
// outside-click listener). IDs match dashboard.html's markup exactly.
import { useEffect, useReducer, useRef } from "react";

import { clampPopoverPosition, DASH_OPTIONS, initialPopoverDraft, popoverDraftReducer } from "../events";
import { useMonitorActions, useMonitorStore } from "../store";

function EventPopover() {
  const editingEventId = useMonitorStore((s) => s.editingEventId);
  const popoverAnchor = useMonitorStore((s) => s.popoverAnchor);
  const events = useMonitorStore((s) => s.events);
  const { closePopover } = useMonitorActions();

  const [draft, dispatch] = useReducer(popoverDraftReducer, initialPopoverDraft);
  const popRef = useRef<HTMLDivElement | null>(null);

  // dashboard.js's `openPopover(ev, mouseEvent)`: seed the form from the
  // clicked event, position near the click (clamped to viewport), and focus
  // the label field. Re-runs on every open — even a second click on the
  // SAME annotation — because legacy has no "already open for this id"
  // guard, so an in-progress unsaved edit is clobbered by a second click;
  // this ports that quirk rather than "fixing" it.
  useEffect(() => {
    if (editingEventId === null || !popoverAnchor) return;
    const ev = events.find((e) => e.id === editingEventId);
    if (!ev) return;
    dispatch({ type: "seed", event: ev });

    const pop = popRef.current;
    if (pop) {
      const pw = pop.offsetWidth || 240;
      const ph = pop.offsetHeight || 140;
      const { left, top } = clampPopoverPosition(
        popoverAnchor.x,
        popoverAnchor.y,
        pw,
        ph,
        window.innerWidth,
        window.innerHeight,
      );
      pop.style.left = `${left}px`;
      pop.style.top = `${top}px`;
    }
    document.getElementById("popover-label")?.focus();
    // `events` is intentionally excluded below: only a new
    // `editingEventId`/`popoverAnchor` (i.e. a fresh click) should re-seed
    // the draft, not unrelated SSE churn to the events array in between.
  }, [editingEventId, popoverAnchor]);

  // dashboard.js's top-level outside-click listener
  // (`if (pop.classList.contains('visible') && !pop.contains(e.target))`).
  useEffect(() => {
    function onDocClick(e: MouseEvent): void {
      if (editingEventId === null) return;
      if (popRef.current && !popRef.current.contains(e.target as Node)) closePopover();
    }
    document.addEventListener("click", onDocClick);
    return () => {
      document.removeEventListener("click", onDocClick);
    };
  }, [editingEventId, closePopover]);

  async function handleSave(): Promise<void> {
    const id = editingEventId;
    if (id === null) return;
    const { label, color, dash } = draft;
    closePopover();
    await fetch(`/api/event/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ label: label.trim(), color, dash }),
    });
  }

  async function handleDelete(): Promise<void> {
    const id = editingEventId;
    if (id === null) return;
    closePopover();
    await fetch(`/api/event/${id}`, { method: "DELETE" });
  }

  return (
    <div id="event-popover" ref={popRef} className={editingEventId !== null ? "visible" : undefined}>
      <label htmlFor="popover-label">Label</label>
      <input
        id="popover-label"
        type="text"
        maxLength={120}
        value={draft.label}
        onChange={(e) => {
          dispatch({ type: "label", value: e.target.value });
        }}
      />
      <div className="popover-row">
        <input
          id="popover-color"
          type="color"
          title="Marker color"
          value={draft.color}
          onChange={(e) => {
            dispatch({ type: "color", value: e.target.value });
          }}
        />
        <select
          id="popover-dash"
          title="Line style"
          value={draft.dash}
          onChange={(e) => {
            dispatch({ type: "dash", value: e.target.value });
          }}
        >
          {DASH_OPTIONS.map((d) => (
            <option key={d} value={d}>
              {d}
            </option>
          ))}
        </select>
      </div>
      <div className="popover-btns">
        <button type="button" id="popover-save" onClick={() => void handleSave()}>
          Save
        </button>
        <button type="button" id="popover-delete" onClick={() => void handleDelete()}>
          Delete
        </button>
        <button type="button" id="popover-cancel" onClick={closePopover}>
          Cancel
        </button>
      </div>
    </div>
  );
}

export default EventPopover;
