// Empty review state (UX spec §13): the import CTA is the whole page.
//
// Composed from the vendored application/empty-state's Root + Content/Footer
// slots (EmptyState.Root forwards arbitrary props — including data-testid —
// straight onto its plain <div>, unlike react-aria-components wrappers that
// filter DOMProps onto an inner element). No Header/Illustration/FeaturedIcon
// slot: this state has never shown one and the brief doesn't ask for one, so
// adding it would be new UI, not a migration.
//
// Where Import is not offered (data/source.ts's importOffered: a live or .db
// source whose data has not arrived, or a URL whose source is unresolved),
// the page says the tab is waiting for the server instead of inviting an
// Import it cannot do.
import { EmptyState as EmptyStateRoot } from "@/components/application/empty-state/empty-state";
import { Button } from "@/components/base/buttons/button";
import { useReviewStore } from "../data/reviewStore";
import { importOffered } from "../data/source";
import { openImportPicker } from "./ImportExport";

export function EmptyState() {
  const importError = useReviewStore((s) => s.importError);
  const canImport = useReviewStore(importOffered);
  return (
    <EmptyStateRoot data-testid="empty-review" size="sm" className="py-24 text-center">
      <EmptyStateRoot.Content>
        <EmptyStateRoot.Description data-testid="empty-review-text">
          {canImport
            ? "No data loaded — import a collection to review."
            : "Waiting for the server's data…"}
        </EmptyStateRoot.Description>
        {importError !== null && (
          <p data-testid="import-error" className="max-w-lg text-sm text-status-warn">
            {importError}
          </p>
        )}
      </EmptyStateRoot.Content>
      {canImport && (
        <EmptyStateRoot.Footer>
          <Button color="primary" onPress={openImportPicker} data-testid="empty-import-btn">
            Import…
          </Button>
        </EmptyStateRoot.Footer>
      )}
    </EmptyStateRoot>
  );
}
