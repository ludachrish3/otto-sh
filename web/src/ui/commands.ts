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
  Grid01,
  Monitor01,
  Moon01,
  PauseCircle,
  Play,
  Sun,
  Upload01,
} from "@untitledui/icons";
import type { FC } from "react";
import { useMemo } from "react";
import { useHashLocation } from "wouter/use-hash-location";

import { useActiveSession, useIsPaused, useReviewStore } from "../data/reviewStore";
import { exportLoadedDocument, openImportPicker } from "../shell/ImportExport";
import {
  type Binding,
  EXPORT_BINDING,
  IMPORT_BINDING,
  PAUSE_BINDING,
  THEME_BINDING,
} from "./shortcuts";
import { useUiStore } from "./uiStore";

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
  const togglePause = useReviewStore((s) => s.actions.togglePause);
  const setWindow = useReviewStore((s) => s.actions.setWindow);
  const paused = useIsPaused();
  const theme = useUiStore((s) => s.theme);
  const toggleTheme = useUiStore((s) => s.actions.toggleTheme);

  return useMemo(() => {
    const commands: Command[] = [
      {
        id: "nav-topology",
        label: "Topology",
        section: "Navigation",
        icon: Dataflow03,
        enabled: true,
        run: () => navigate("/"),
      },
      {
        id: "nav-hosts",
        label: "Hosts",
        section: "Navigation",
        icon: Grid01,
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
  ]);
}
