// EventSource client for /api/stream, dispatching to the zustand store.
// Mirrors dashboard.js's startSSE(): one connection, dispatch-by-`type` on
// message, and an onerror handler that transitions to disconnected/historical
// and closes the source (no auto-retry — same as the legacy dashboard).

import { type LogEventMessage, type MetricMessage, useMonitorStore } from "../store";
import type { MonitorEvent } from "./client";

interface EventDeletedMessage {
  type: "event_deleted";
  id: number;
}

type StreamMessage =
  | MetricMessage
  | LogEventMessage
  | (MonitorEvent & { type: "event" })
  | (MonitorEvent & { type: "event_updated" })
  | EventDeletedMessage;

export function startSse(url = "/api/stream"): EventSource {
  const src = new EventSource(url);
  const { actions } = useMonitorStore.getState();

  src.onopen = () => {
    actions.sseOpened();
  };

  src.onmessage = (e: MessageEvent<string>) => {
    const msg = JSON.parse(e.data) as StreamMessage;
    switch (msg.type) {
      case "metric":
        actions.metricMsg(msg);
        break;
      case "log_event":
        actions.logEventMsg(msg);
        break;
      case "event":
        actions.eventMsg(msg);
        break;
      case "event_updated":
        actions.eventUpdated(msg);
        break;
      case "event_deleted":
        actions.eventDeleted(msg.id);
        break;
    }
  };

  src.onerror = () => {
    actions.sseErrored();
    // dashboard.js closes the source on error rather than letting the
    // browser's default EventSource auto-retry kick in.
    src.close();
  };

  return src;
}
