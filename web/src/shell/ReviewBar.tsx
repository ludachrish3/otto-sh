// The historical review bar (UX spec §12): HISTORICAL tag + source ·
// session picker (only >1) · range presets + custom from-to · Reset.
import { useEffect, useState } from "react";

import { clampRange, presetRange, sessionBounds } from "../data/exportDoc";
import { useActiveSession, useReviewStore } from "../data/reviewStore";
import { localInputToMs, msToLocalInput } from "../data/time";
import { Badge } from "../ui/Badge";
import { Button } from "../ui/Button";
import { Select } from "../ui/Select";
import { TextInput } from "../ui/TextInput";
import { ToggleGroup } from "../ui/ToggleGroup";

const PRESETS = [
  { id: "full", label: "Full", minutes: null },
  { id: "15m", label: "Last 15m", minutes: 15 },
  { id: "1h", label: "Last 1h", minutes: 60 },
] as const;

export function ReviewBar() {
  const sessions = useReviewStore((s) => s.sessions);
  const sourceName = useReviewStore((s) => s.sourceName);
  const activeSessionId = useReviewStore((s) => s.activeSessionId);
  const range = useReviewStore((s) => s.range);
  const mode = useReviewStore((s) => s.mode);
  const { selectSession, setRange, resetView } = useReviewStore((s) => s.actions);
  const session = useActiveSession();

  const bounds = session ? sessionBounds(session) : null;
  // sessionBounds returns a fresh object every render, so the effect below
  // depends on its primitive edges (not the object) — biome's
  // useExhaustiveDependencies otherwise flags the dependency as "more
  // specific than its capture" (bounds.from/.to) or, if the whole object is
  // listed instead, re-runs every render since the reference never settles.
  const boundsFrom = bounds?.from;
  const boundsTo = bounds?.to;
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  useEffect(() => {
    if (boundsFrom === undefined || boundsTo === undefined) return;
    setFrom(msToLocalInput(range?.from ?? boundsFrom));
    setTo(msToLocalInput(range?.to ?? boundsTo));
  }, [range?.from, range?.to, boundsFrom, boundsTo]);

  // UX spec §12: "Review bar (per-view context row, historical only)". Live
  // mode gets its own follow/pause chrome in AppBar instead (Task 9) — the
  // HISTORICAL tag + range presets/Reset here would otherwise contradict the
  // "Live"/pause-toggle chrome rendering at the same time. This hiding was
  // reverted (commit 7a9e849) only because bootstrap.ts set mode="live"
  // before a boot hydrate had actually succeeded, so an empty live server
  // claimed to be live and broke the dashboard Playwright suite; that root
  // cause is now fixed (mode is set only after a successful hydrate — see
  // bootstrap.ts), so hiding is safe to restore (Plan 5b final review, C1).
  if (!session || !bounds || mode === "live") return null;

  const activePreset =
    range === null
      ? "full"
      : (PRESETS.find(
          (p) =>
            p.minutes !== null &&
            presetRange(bounds, p.minutes)?.from === range.from &&
            presetRange(bounds, p.minutes)?.to === range.to,
        )?.id ?? "custom");

  const applyCustom = () => {
    const fromMs = localInputToMs(from);
    const toMs = localInputToMs(to);
    if (fromMs !== null && toMs !== null && fromMs < toMs) {
      setRange(clampRange({ from: fromMs, to: toMs }, bounds));
    }
  };

  return (
    <div
      data-testid="review-bar"
      className="flex flex-wrap items-center gap-3 border-b border-gray-200 px-4 py-2
        dark:border-gray-800"
    >
      <Badge tone="historical" testId="historical-tag">
        HISTORICAL
      </Badge>
      <span data-testid="source-name" className="text-sm text-gray-500 dark:text-gray-400">
        {sourceName}
      </span>
      {sessions.length > 1 && (
        <Select
          label="Session"
          items={sessions.map((s) => ({
            id: s.id,
            label: s.label ?? s.id,
            title: s.note ?? undefined,
          }))}
          selectedKey={activeSessionId ?? ""}
          onSelectionChange={selectSession}
          testId="session-picker"
        />
      )}
      <ToggleGroup
        label="Range"
        options={PRESETS.map((p) => ({ id: p.id, label: p.label }))}
        selectedId={activePreset}
        onSelect={(id) => {
          const preset = PRESETS.find((p) => p.id === id);
          if (preset) setRange(presetRange(bounds, preset.minutes));
        }}
        testId="range-presets"
      />
      <TextInput
        label="From"
        type="datetime-local"
        value={from}
        onChange={setFrom}
        testId="range-from"
      />
      <TextInput label="To" type="datetime-local" value={to} onChange={setTo} testId="range-to" />
      <Button onPress={applyCustom} testId="range-apply">
        Apply
      </Button>
      <Button variant="ghost" onPress={resetView} testId="range-reset">
        Reset
      </Button>
    </div>
  );
}
