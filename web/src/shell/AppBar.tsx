// Global chrome (UX spec §7): brand fixed left · pause (live mode only) ·
// far right = status text · status dot · ⋯ menu (Import/Export/theme — the
// infrequent actions). The status dot never moves; only its color and the
// text beside it change with mode/connection.

import { DotsHorizontal } from "@untitledui/icons";
import { useState } from "react";

import { ButtonGroup, ButtonGroupItem } from "@/components/base/button-group/button-group";
import { ButtonUtility } from "@/components/base/buttons/button-utility";
import { Dropdown } from "@/components/base/dropdown/dropdown";
import { useActiveSession, useIsPaused, useReviewStore } from "../data/reviewStore";
import { loadTheme, saveTheme, type Theme } from "../theme";
import { EventsPanel } from "./EventsPanel";
import { ExportButton, exportLoadedDocument, openImportPicker } from "./ImportExport";

// The live-window presets (Task 6, Plan 5b follow-ups): follow-window width
// while live, `5m · 15m · 1h`, default 15m (windowMs's own store default).
// `id` is what the ButtonGroup's selection keys off; the SELECTED item is
// derived from `windowMs` (see `selectedWindowId` below), never stored
// separately — same lesson as `useIsPaused`, a stored copy of a derived
// value drifts.
const LIVE_WINDOW_PRESETS = [
  { id: "5m", label: "5m", ms: 300_000 },
  { id: "15m", label: "15m", ms: 900_000 },
  { id: "1h", label: "1h", ms: 3_600_000 },
] as const;

function selectedWindowId(windowMs: number): string {
  return LIVE_WINDOW_PRESETS.find((p) => p.ms === windowMs)?.id ?? "15m";
}

export function AppBar() {
  const hasData = useReviewStore((s) => s.sessions.length > 0);
  const mode = useReviewStore((s) => s.mode);
  const connection = useReviewStore((s) => s.connection);
  const paused = useIsPaused();
  const togglePause = useReviewStore((s) => s.actions.togglePause);
  const windowMs = useReviewStore((s) => s.windowMs);
  const setWindow = useReviewStore((s) => s.actions.setWindow);
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
          : { text: "No data", dot: "bg-fg-quaternary" };

  return (
    <header
      data-testid="app-bar"
      className="flex h-12 items-center justify-between border-b border-secondary px-4"
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
            className="cursor-pointer rounded-md px-2 py-1 text-sm text-tertiary
              hover:bg-primary_hover"
          >
            {paused ? "Resume" : "Pause"}
          </button>
        )}
        {mode === "live" && (
          <ButtonGroup
            aria-label="Live window"
            data-testid="live-window"
            size="sm"
            selectedKeys={new Set([selectedWindowId(windowMs)])}
            disallowEmptySelection
            onSelectionChange={(keys) => {
              const id = [...keys][0];
              const preset = LIVE_WINDOW_PRESETS.find((p) => p.id === id);
              if (preset) setWindow(preset.ms);
            }}
          >
            {LIVE_WINDOW_PRESETS.map((p) => (
              <ButtonGroupItem key={p.id} id={p.id} data-testid={`live-window-${p.id}`}>
                {p.label}
              </ButtonGroupItem>
            ))}
          </ButtonGroup>
        )}
      </div>
      <div className="flex items-center gap-3">
        {mode === "live" && <ExportButton />}
        {session && session.events.length > 0 && (
          <button
            type="button"
            data-testid="events-button"
            onClick={() => setEventsOpen(true)}
            className="cursor-pointer rounded-md px-2 py-1 text-sm text-tertiary
              hover:bg-primary_hover"
          >
            Events{" "}
            <span data-testid="events-count" className="rounded-full bg-tertiary px-1.5 text-xs">
              {session.events.length}
            </span>
          </button>
        )}
        <span data-testid="status-text" className="text-sm text-tertiary">
          {status.text}
        </span>
        <span data-testid="status-dot" className={`h-2.5 w-2.5 rounded-full ${status.dot}`} />
        {/* The chrome's "⋯" overflow menu (UX spec §7): infrequent actions
            live here. */}
        <Dropdown.Root>
          <ButtonUtility
            aria-label="More actions"
            data-testid="overflow-menu"
            icon={DotsHorizontal}
            color="tertiary"
          />
          <Dropdown.Popover>
            <Dropdown.Menu>
              <Dropdown.Item
                id="import"
                label="Import…"
                onAction={openImportPicker}
                data-testid="menu-import"
              />
              {
                // Live mode already has a directly-clickable <ExportButton />
                // above (rendered when mode === "live") — the overflow entry
                // would just be a second click path to the identical
                // exportLoadedDocument() call, so it's omitted there instead
                // of shipping two visible controls for the same action.
                mode !== "live" && (
                  <Dropdown.Item
                    id="export"
                    label="Export"
                    onAction={exportLoadedDocument}
                    isDisabled={!hasData}
                    data-testid="menu-export"
                  />
                )
              }
              <Dropdown.Item
                id="theme"
                label={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
                onAction={toggleTheme}
                data-testid="menu-theme"
              />
            </Dropdown.Menu>
          </Dropdown.Popover>
        </Dropdown.Root>
        <EventsPanel isOpen={eventsOpen} onClose={() => setEventsOpen(false)} />
      </div>
    </header>
  );
}
