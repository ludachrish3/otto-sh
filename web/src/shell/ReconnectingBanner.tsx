// The `connection` state's ONE render site (spec §Reconnecting banner).
// The AppBar status text/dot it replaces were deleted in the same spec —
// deleting them without this banner would have left stream.ts's
// connecting/disconnected states with no reader (the render-site rule:
// guard what you emit). Same pattern as App.tsx's import-error banner.
import { AlertTriangle } from "@untitledui/icons";

import { useReviewStore } from "../data/reviewStore";

export function ReconnectingBanner() {
  const mode = useReviewStore((s) => s.mode);
  const connection = useReviewStore((s) => s.connection);
  if (mode !== "live" || connection === "live") return null;
  return (
    <div
      data-testid="reconnecting-banner"
      className="flex items-center gap-2 border-b border-status-warn/30 bg-status-warn/10 px-4
        py-2 text-sm font-medium text-status-warn dark:bg-status-warn/15"
    >
      <AlertTriangle aria-hidden className="size-4 shrink-0" />
      Reconnecting…
    </div>
  );
}
