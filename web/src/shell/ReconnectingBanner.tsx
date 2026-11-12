// The `connection` state's ONE render site (spec §Reconnecting banner).
// The AppBar status text/dot it replaces were deleted in the same spec —
// deleting them without this banner would have left stream.ts's
// connecting/disconnected states with no reader (the render-site rule:
// guard what you emit). Same pattern as App.tsx's import-error banner.
import { AlertTriangle } from "@untitledui/icons";

import { useReviewStore } from "../data/reviewStore";
import { servedSource } from "../data/source";

export function ReconnectingBanner() {
  const streaming = useReviewStore((s) => servedSource(s) !== null);
  const connection = useReviewStore((s) => s.connection);
  // One rule for every streamed mode (#958): review streams too, so a lost
  // connection there stops other tabs' changes reaching this one. A server
  // source the tab knows counts whether it is installed or only pending (a
  // live run with nothing recorded yet retries through the reconnect: this
  // is how the tab shows it is retrying). With none (the static shell, a
  // file loaded in the tab, a boot no `/api/mode` has answered yet) nothing
  // is known to be streaming, so nothing is reconnecting.
  if (!streaming || connection === "live") return null;
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
