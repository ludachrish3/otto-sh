// web/src/ui/commands.ts
// The command registry (spec §Registry): ONE derivation feeding three
// consumers — palette rows (CommandMenu), bound-chord handlers
// (useGlobalShortcuts), and every visible keycap hint. Navigation rows are
// deliberately chord-less: browsers reserve ⌘T/⌘N/⌘W/⌘⇧T uninterceptably
// and macOS owns ⌘H (spec decision 4), so views ride the palette + tabs.
import {
  Clock,
  Dataflow03,
  Download01,
  Flag01,
  List,
  Monitor01,
  Moon01,
  PauseCircle,
  Play,
  Scissors01,
  Sun,
  Upload01,
} from "@untitledui/icons";
import type { FC } from "react";
import { useMemo } from "react";
import { useHashLocation } from "wouter/use-hash-location";

import { useActiveSession, useIsPaused, useReviewStore } from "../data/reviewStore";
import { exportLoadedDocument, openImportPicker } from "../shell/ImportExport";
import { blankDraft, endOpenSpan } from "../shell/marking";
import {
  type Binding,
  EXPORT_BINDING,
  IMPORT_BINDING,
  MARK_NOW_BINDING,
  PAUSE_BINDING,
  THEME_BINDING,
} from "./shortcuts";
import { type EventEditorTarget, useUiStore } from "./uiStore";

export type CommandSection = "Navigation" | "Actions" | "Live window";

export interface Command {
  id: string;
  label: string;
  section: CommandSection;
  /** Secondary text (host board · slot). */
  sublabel?: string;
  icon: FC<{ className?: string }>;
  /** Absent on navigation rows (decision 4) and preset rows. */
  binding?: Binding;
  /** false renders a disabled row (Export without data) — still listed. */
  enabled: boolean;
  /** Live-window rows: the active preset. */
  checked?: boolean;
  run: () => void;
}

// The follow-window presets, moved here from AppBar (Task 7 re-imports) so
// the palette rows and the AppBar ButtonGroup share one definition.
export const LIVE_WINDOW_PRESETS = [
  { id: "5m", label: "5m", ms: 300_000 },
  { id: "15m", label: "15m", ms: 900_000 },
  { id: "1h", label: "1h", ms: 3_600_000 },
] as const;

export function useCommands(): Command[] {
  const [, navigate] = useHashLocation();
  const session = useActiveSession();
  const mode = useReviewStore((s) => s.mode);
  const windowMs = useReviewStore((s) => s.windowMs);
  const hasData = useReviewStore((s) => s.rawMonitorSessions !== null);
  const editable = useReviewStore((s) => s.editable);
  const togglePause = useReviewStore((s) => s.actions.togglePause);
  const setWindow = useReviewStore((s) => s.actions.setWindow);
  const addWarning = useReviewStore((s) => s.actions.addWarning);
  const paused = useIsPaused();
  const theme = useUiStore((s) => s.theme);
  const toggleTheme = useUiStore((s) => s.actions.toggleTheme);
  const openSpan = useUiStore((s) => s.openSpan);
  const openEventEditor = useUiStore((s) => s.actions.openEventEditor);
  const armSweep = useUiStore((s) => s.actions.armSweep);
  const openMarkPopover = useUiStore((s) => s.actions.openMarkPopover);

  return useMemo(() => {
    const commands: Command[] = [
      {
        id: "nav-topology",
        label: "Topology View",
        section: "Navigation",
        icon: Dataflow03,
        enabled: true,
        run: () => navigate("/"),
      },
      {
        id: "nav-hosts",
        label: "List View",
        section: "Navigation",
        icon: List,
        enabled: true,
        run: () => navigate("/hosts"),
      },
    ];
    for (const host of session?.lab.hosts ?? []) {
      const pieces = [host.board, host.slot != null ? `slot ${host.slot}` : null].filter(
        (p): p is string => p != null,
      );
      commands.push({
        id: `nav-host-${host.id}`,
        label: host.id,
        section: "Navigation",
        sublabel: pieces.length > 0 ? pieces.join(" · ") : undefined,
        icon: Monitor01,
        enabled: true,
        run: () => navigate(`/host/${host.id}`),
      });
    }
    for (const el of session?.elements ?? []) {
      commands.push({
        id: `nav-element-${el.id}`,
        label: el.id,
        section: "Navigation",
        sublabel: "element",
        icon: Dataflow03,
        enabled: true,
        run: () => navigate(`/topology/${el.id}`),
      });
    }
    commands.push(
      {
        id: "action-import",
        label: "Import…",
        section: "Actions",
        icon: Upload01,
        binding: IMPORT_BINDING,
        enabled: true,
        run: openImportPicker,
      },
      {
        id: "action-export",
        label: "Export",
        section: "Actions",
        icon: Download01,
        binding: EXPORT_BINDING,
        enabled: hasData,
        run: exportLoadedDocument,
      },
      {
        id: "action-theme",
        label: theme === "dark" ? "Switch to light mode" : "Switch to dark mode",
        section: "Actions",
        icon: theme === "dark" ? Sun : Moon01,
        binding: THEME_BINDING,
        enabled: true,
        run: toggleTheme,
      },
    );
    // Plan 5c marking rows: add-event/sweep-span are available in either
    // mode (both just prep a draft/gesture); mark-now/start-span/end-span
    // are live-only (a review session has no "now" to mark and no in-flight
    // span to start/end). Every row is gated on `editable` (spec §Marking) —
    // a read-only server (or a review session opened from a plain export,
    // not a .db) must not offer affordances it cannot fulfil.
    if (editable && session) {
      commands.push(
        {
          id: "action-add-event",
          label: "Add event…",
          section: "Actions",
          icon: Flag01,
          enabled: true,
          run: () => {
            const target: EventEditorTarget = { kind: "draft", draft: blankDraft(session) };
            openEventEditor(target);
          },
        },
        {
          id: "action-sweep-span",
          label: "Sweep span on chart",
          section: "Actions",
          icon: Scissors01,
          enabled: true,
          run: armSweep,
        },
      );
      if (mode === "live") {
        commands.push(
          {
            id: "action-mark-now",
            label: "Mark now…",
            section: "Actions",
            icon: Flag01,
            binding: MARK_NOW_BINDING,
            enabled: true,
            run: () => openMarkPopover("mark"),
          },
          {
            id: "action-start-span",
            label: "Start span…",
            section: "Actions",
            icon: Flag01,
            enabled: true,
            run: () => openMarkPopover("start"),
          },
          {
            id: "action-end-span",
            label: "End span",
            section: "Actions",
            icon: Flag01,
            // Only meaningful while the open span belongs to the session
            // currently being viewed — switching sessions with a span still
            // running elsewhere must not offer to end the wrong one.
            enabled: openSpan?.sessionId === session.id,
            run: () => {
              void endOpenSpan().catch((err) =>
                addWarning(`End span failed: ${err instanceof Error ? err.message : String(err)}`),
              );
            },
          },
        );
      }
    }
    if (mode === "live") {
      commands.push({
        id: "action-pause",
        label: paused ? "Resume" : "Pause",
        section: "Actions",
        icon: paused ? Play : PauseCircle,
        binding: PAUSE_BINDING,
        enabled: true,
        run: togglePause,
      });
      for (const preset of LIVE_WINDOW_PRESETS) {
        commands.push({
          id: `window-${preset.id}`,
          label: `Follow ${preset.label}`,
          section: "Live window",
          icon: Clock,
          enabled: true,
          checked: windowMs === preset.ms,
          run: () => setWindow(preset.ms),
        });
      }
    }
    return commands;
  }, [
    navigate,
    session,
    mode,
    windowMs,
    hasData,
    paused,
    theme,
    togglePause,
    setWindow,
    toggleTheme,
    editable,
    openSpan,
    openEventEditor,
    armSweep,
    openMarkPopover,
    addWarning,
  ]);
}
