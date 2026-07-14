// Render site for reviewStore's `warnings` channel (Plan 5b final-review
// Finding [1]). Before this component existed, "drop and warn" was, in
// practice, "drop silently": exportDoc.ts and reviewStore.ts both WRITE
// `state.warnings` (a session's own duplicate ids, a dropped bad-timestamp
// metric/event/log_event row — see data/exportDoc.ts's
// dropInvalidTimestamps), but nothing ever READ it. A live run dropping 500
// malformed metric rows left a chart with holes and zero signal anywhere;
// the only trace was a store field nothing displayed.
//
// Built from FeaturedIcon + Untitled UI's semantic warning tokens, the same
// way SubjectHealthBanner.tsx does — no dark: variant needed (the tokens
// resolve per-theme via the `dark-mode` class already) and no hand-rolled
// imitation of a component Untitled UI ships (there is no free-tier alert
// component to reach for).
//
// Dismiss/re-nag contract: `dismissed.count` is how many of the store's
// `warnings` entries the user has already acknowledged, scoped to the
// document they were dismissed against (`dismissed.raw`, reviewStore's
// `rawMonitorSessions`). Pending = the entries past that count. Dismissing
// sets count to the CURRENT length, so a fragment/import that adds no new
// warning entries does not resurface the banner (no re-nag on every tick),
// while a later entry — a fresh warning after dismissal, live or import —
// grows `warnings` past the dismissed count again and the banner reappears
// with the new count.
//
// Scoping the dismissal to `rawMonitorSessions`'s object identity (not just
// comparing lengths) matters: `warnings` only ever GROWS while fragments
// accumulate onto the same document (appendFragment/appendFragments push
// onto it — reviewStore.ts's mergeFragments), but a fresh
// importMonitorSessions/resyncMonitorSessions REPLACES it wholesale with
// whatever the newly (re-)parsed document produced — which can easily be
// SHORTER than what was already dismissed (e.g. dismiss 2 warnings, then
// import a totally different document with only 1). A plain
// `warnings.length - dismissedCount` clamp would silently swallow that
// fresh warning (length 1 <= previously-dismissed count of 2). Resetting
// whenever `rawMonitorSessions`'s reference changes — which happens on
// every fresh import/resync and NEVER on a live fragment append, since
// mergeFragments touches only `sessions`/`warnings` — is the one reliable
// "this is a new document" signal. Adjusted during render (the documented
// "adjusting state when a prop changes" pattern), not in a useEffect, so
// there is no stale extra frame where a leftover dismissal count is shown
// against the new document before an effect catches up.
import { AlertTriangle } from "@untitledui/icons";
import { useState } from "react";

import { FeaturedIcon } from "@/components/foundations/featured-icon/featured-icon";
import type { MonitorHistoricalExportDocument } from "../api/export.gen";
import { useReviewStore } from "../data/reviewStore";

interface Dismissed {
  raw: MonitorHistoricalExportDocument | null;
  count: number;
}

export function DataWarningsBanner() {
  const warnings = useReviewStore((s) => s.warnings);
  const rawMonitorSessions = useReviewStore((s) => s.rawMonitorSessions);
  const [dismissed, setDismissed] = useState<Dismissed>({ raw: rawMonitorSessions, count: 0 });

  if (dismissed.raw !== rawMonitorSessions) {
    setDismissed({ raw: rawMonitorSessions, count: 0 });
  }
  const dismissedCount = dismissed.raw === rawMonitorSessions ? dismissed.count : 0;
  const seen = Math.min(dismissedCount, warnings.length);
  const pending = warnings.slice(seen);

  if (pending.length === 0) return null;

  return (
    <div
      data-testid="data-warnings-banner"
      className="flex items-start gap-3 rounded-lg bg-warning-primary px-4 py-3"
    >
      <FeaturedIcon color="warning" icon={AlertTriangle} size="sm" />
      <p className="min-w-0 grow text-sm font-medium text-warning-primary">
        {pending.length} data warning{pending.length === 1 ? "" : "s"} — {pending.join(" · ")}
      </p>
      <button
        type="button"
        data-testid="data-warnings-dismiss"
        onClick={() => setDismissed({ raw: rawMonitorSessions, count: warnings.length })}
        className="cursor-pointer rounded-md px-2 py-0.5 text-sm font-medium text-warning-primary
          underline-offset-2 hover:underline"
      >
        Dismiss
      </button>
    </div>
  );
}
