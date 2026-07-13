// Global chrome (UX spec §7): brand fixed left · pause (live mode only) ·
// far right = status text · status dot · ⋯ menu (Import/Export/theme — the
// infrequent actions). The status dot never moves; only its color and the
// text beside it change with mode/connection.
import { useState } from "react";

import { useActiveSession, useIsPaused, useReviewStore } from "../data/reviewStore";
import { loadTheme, saveTheme, type Theme } from "../theme";
import { OverflowMenu } from "../ui/Menu";
import { EventsPanel } from "./EventsPanel";
import { ExportButton, exportLoadedDocument, openImportPicker } from "./ImportExport";

export function AppBar() {
  const hasData = useReviewStore((s) => s.sessions.length > 0);
  const mode = useReviewStore((s) => s.mode);
  const connection = useReviewStore((s) => s.connection);
  const paused = useIsPaused();
  const togglePause = useReviewStore((s) => s.actions.togglePause);
  const session = useActiveSession();
  const [theme, setTheme] = useState<Theme>(loadTheme);
  const [eventsOpen, setEventsOpen] = useState(false);

  const toggleTheme = () => {
    const next: Theme = theme === "dark" ? "light" : "dark";
    saveTheme(next);
    setTheme(next);
  };

  // mode === "review" is the server-booted review path (Task 8's /api/mode);
  // a client-side Import with no backing server leaves mode === null, which
  // keeps today's "Historical"/"No data" wording — the pre-existing,
  // already-tested behavior — untouched.
  const status =
    mode === "live"
      ? connection === "live"
        ? { text: "Live", dot: "bg-status-live" }
        : { text: "Reconnecting…", dot: "bg-status-warn" }
      : mode === "review"
        ? { text: "Reviewing", dot: "bg-status-historical" }
        : hasData
          ? { text: "Historical", dot: "bg-status-historical" }
          : { text: "No data", dot: "bg-gray-300 dark:bg-gray-600" };

  return (
    <header
      data-testid="app-bar"
      className="flex h-12 items-center justify-between border-b border-gray-200 px-4
        dark:border-gray-800"
    >
      <div className="flex items-center gap-3">
        <div data-testid="brand" className="flex items-center gap-2 text-sm font-semibold">
          <span aria-hidden className="text-brand-500">
            ⬡
          </span>
          otto monitor
        </div>
        {mode === "live" && (
          <button
            type="button"
            data-testid="pause-toggle"
            onClick={togglePause}
            className="cursor-pointer rounded-md px-2 py-1 text-sm text-gray-500
              hover:bg-gray-100 dark:text-gray-400 dark:hover:bg-gray-900"
          >
            {paused ? "Resume" : "Pause"}
          </button>
        )}
      </div>
      <div className="flex items-center gap-3">
        {mode === "live" && <ExportButton />}
        {session && session.events.length > 0 && (
          <button
            type="button"
            data-testid="events-button"
            onClick={() => setEventsOpen(true)}
            className="cursor-pointer rounded-md px-2 py-1 text-sm text-gray-500
              hover:bg-gray-100 dark:text-gray-400 dark:hover:bg-gray-900"
          >
            Events{" "}
            <span
              data-testid="events-count"
              className="rounded-full bg-gray-100 px-1.5 text-xs dark:bg-gray-800"
            >
              {session.events.length}
            </span>
          </button>
        )}
        <span data-testid="status-text" className="text-sm text-gray-500 dark:text-gray-400">
          {status.text}
        </span>
        <span data-testid="status-dot" className={`h-2.5 w-2.5 rounded-full ${status.dot}`} />
        <OverflowMenu
          items={[
            { id: "import", label: "Import…", onAction: openImportPicker, testId: "menu-import" },
            // Live mode already has a directly-clickable <ExportButton />
            // above (rendered when mode === "live") — the overflow entry
            // would just be a second click path to the identical
            // exportLoadedDocument() call, so it's omitted there instead of
            // shipping two visible controls for the same action.
            ...(mode === "live"
              ? []
              : [
                  {
                    id: "export",
                    label: "Export",
                    onAction: exportLoadedDocument,
                    isDisabled: !hasData,
                    testId: "menu-export",
                  },
                ]),
            {
              id: "theme",
              label: theme === "dark" ? "Switch to light mode" : "Switch to dark mode",
              onAction: toggleTheme,
              testId: "menu-theme",
            },
          ]}
        />
        <EventsPanel isOpen={eventsOpen} onClose={() => setEventsOpen(false)} />
      </div>
    </header>
  );
}
