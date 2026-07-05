// The event bar: mark/span event controls + "Clear Events" — mirrors
// dashboard.js's §Event bar (the `EventBox` class + its two instances'
// `onAction` handlers, and the `clear-events-btn` click handler). IDs/
// classes match dashboard.html's markup exactly (the DOM-parity contract).
import { useEffect, useReducer, useRef, useState } from "react";

import type { MonitorEvent } from "../api/client";
import { DASH_OPTIONS, spanTransition, spanVisual } from "../events";
import { useMonitorActions, useMonitorStore } from "../store";

async function postEvent(label: string, color: string, dash: string): Promise<MonitorEvent | null> {
  const res = await fetch("/api/event", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ label, color, dash }),
  });
  if (!res.ok) return null;
  return (await res.json()) as MonitorEvent;
}

function EventToolbar() {
  const connection = useMonitorStore((s) => s.connection);
  const everLive = useMonitorStore((s) => s.everLive);
  const chartsInitialized = useMonitorStore((s) => s.chartsInitialized);
  const spanStartId = useMonitorStore((s) => s.spanStartId);
  const { setSpanStart } = useMonitorActions();

  // ── Mark event ─────────────────────────────────────────────────────────
  const [markLabel, setMarkLabel] = useState("");
  const [markColor, setMarkColor] = useState("#888888");
  const [markDash, setMarkDash] = useState<string>(DASH_OPTIONS[0]);
  const [markBusy, setMarkBusy] = useState(false);
  // dashboard.js: `markEventBox.setEnabled(true)` fires once (in
  // `src.onopen`, gated on `state.meta.live`) and is never re-disabled on
  // disconnect (only `spanEventBox` is, in `src.onerror`) — only the
  // in-flight POST toggles this one locally.
  const markDisabled = !everLive || markBusy;

  async function handleMark(): Promise<void> {
    const label = markLabel.trim();
    if (!label) return;
    setMarkBusy(true);
    try {
      await postEvent(label, markColor, markDash);
      setMarkLabel("");
    } finally {
      setMarkBusy(false);
    }
  }

  // ── Span event ─────────────────────────────────────────────────────────
  const [spanLabel, setSpanLabel] = useState("");
  const [spanColor, setSpanColor] = useState("#888888");
  const [spanDash, setSpanDash] = useState<string>(DASH_OPTIONS[0]);
  const [phase, dispatchSpan] = useReducer(spanTransition, "idle" as const);
  const visual = spanVisual(phase);
  // dashboard.js's `spanEventBox.setEnabled(false)` in `src.onerror`: tied
  // to the live connection (unlike the mark button above), so it
  // re-disables on disconnect.
  const spanDisabled = visual.disabled || connection !== "live";

  const prevSpanStartId = useRef(spanStartId);
  useEffect(() => {
    // External abandonment (dashboard.js's `src.onerror`): the SSE layer
    // nulled `spanStartId` out from under us while we still think a span is
    // starting/open — snap back to idle, same as `setButtonText('Start
    // event')` + `removeButtonClass('active')` (legacy does NOT clear the
    // span input on abandon, only the button chrome, so neither do we).
    // Excludes `phase === "ending"`: that's OUR OWN end-flow nulling
    // `spanStartId` on purpose, not an external abandonment.
    if (
      prevSpanStartId.current !== null &&
      spanStartId === null &&
      phase !== "idle" &&
      phase !== "ending"
    ) {
      dispatchSpan({ type: "abandoned" });
    }
    prevSpanStartId.current = spanStartId;
  }, [spanStartId, phase]);

  async function handleSpan(): Promise<void> {
    if (spanStartId === null) {
      // ── Start the span: POST a normal event, then switch to "End event" ──
      const label = spanLabel.trim();
      if (!label) return;
      dispatchSpan({ type: "start_requested" });
      try {
        const ev = await postEvent(label, spanColor, spanDash);
        if (!ev) return;
        setSpanStart(ev.id);
        dispatchSpan({ type: "start_succeeded" });
        // The event already arrived via SSE and shows as a vertical line.
      } finally {
        // No-ops once `start_succeeded` already moved the phase to "open"
        // (the guard in `spanTransition` only fires from "starting").
        dispatchSpan({ type: "start_failed" });
      }
    } else {
      // ── End the span: POST to /end — server records datetime.now() ──────
      const id = spanStartId;
      setSpanStart(null);
      dispatchSpan({ type: "end_requested" });
      try {
        await fetch(`/api/event/${id}/end`, { method: "POST" });
        // SSE event_updated will update the store's events and refresh plots.
        setSpanLabel("");
      } finally {
        dispatchSpan({ type: "end_settled" });
      }
    }
  }

  // dashboard.js's clear-events-btn handler: snapshot ids, confirm(), then
  // fan out DELETEs. Reads `getState()` (not a hooked value) so the
  // snapshot is taken fresh at click time, matching legacy's synchronous
  // `state.events.map(...)` read before any SSE callback can mutate it.
  async function handleClear(): Promise<void> {
    if (
      !window.confirm(
        "Are you sure you want to clear all event markers?\nThis action cannot be undone.",
      )
    )
      return;
    const ids = useMonitorStore.getState().events.map((e) => e.id);
    await Promise.all(ids.map((id) => fetch(`/api/event/${id}`, { method: "DELETE" })));
  }

  return (
    <div id="event-bar">
      <button
        type="button"
        id="clear-events-btn"
        disabled={!chartsInitialized}
        onClick={() => void handleClear()}
      >
        Clear Events
      </button>
      <div className="event-box">
        <label htmlFor="event-label">Mark event:</label>
        <input
          id="event-label"
          type="text"
          placeholder="e.g. Router rebooted"
          maxLength={120}
          value={markLabel}
          onChange={(e) => {
            setMarkLabel(e.target.value);
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !markDisabled) void handleMark();
          }}
        />
        <input
          id="event-color"
          type="color"
          title="Marker color"
          value={markColor}
          onChange={(e) => {
            setMarkColor(e.target.value);
          }}
        />
        <select
          id="event-dash"
          title="Line style"
          value={markDash}
          onChange={(e) => {
            setMarkDash(e.target.value);
          }}
        >
          {DASH_OPTIONS.map((d) => (
            <option key={d} value={d}>
              {d}
            </option>
          ))}
        </select>
        <button
          type="button"
          id="event-btn"
          disabled={markDisabled}
          onClick={() => void handleMark()}
        >
          Mark Event
        </button>
      </div>
      <div className="event-box">
        <label htmlFor="span-label">Span event:</label>
        <input
          id="span-label"
          type="text"
          placeholder="e.g. Maintenance window"
          maxLength={120}
          value={spanLabel}
          onChange={(e) => {
            setSpanLabel(e.target.value);
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !spanDisabled) void handleSpan();
          }}
        />
        <input
          id="span-color"
          type="color"
          title="Span color"
          value={spanColor}
          onChange={(e) => {
            setSpanColor(e.target.value);
          }}
        />
        <select
          id="span-dash"
          title="Line style"
          value={spanDash}
          onChange={(e) => {
            setSpanDash(e.target.value);
          }}
        >
          {DASH_OPTIONS.map((d) => (
            <option key={d} value={d}>
              {d}
            </option>
          ))}
        </select>
        <button
          type="button"
          id="span-btn"
          className={visual.active ? "active" : undefined}
          disabled={spanDisabled}
          onClick={() => void handleSpan()}
        >
          {visual.text}
        </button>
      </div>
    </div>
  );
}

export default EventToolbar;
