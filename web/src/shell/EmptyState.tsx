// Empty review state (UX spec §13): the import CTA is the whole page.
//
// Composed from the vendored application/empty-state's Root + Content/Footer
// slots (EmptyState.Root forwards arbitrary props — including data-testid —
// straight onto its plain <div>, unlike react-aria-components wrappers that
// filter DOMProps onto an inner element). No Header/Illustration/FeaturedIcon
// slot: this state has never shown one and the brief doesn't ask for one, so
// adding it would be new UI, not a migration.
import { EmptyState as EmptyStateRoot } from "@/components/application/empty-state/empty-state";
import { Button } from "@/components/base/buttons/button";
import { useReviewStore } from "../data/reviewStore";
import { openImportPicker } from "./ImportExport";

export function EmptyState() {
  const importError = useReviewStore((s) => s.importError);
  return (
    <EmptyStateRoot data-testid="empty-review" size="sm" className="py-24 text-center">
      <EmptyStateRoot.Content>
        <EmptyStateRoot.Description>
          No data loaded — import a collection to review.
        </EmptyStateRoot.Description>
        {importError !== null && (
          <p data-testid="import-error" className="max-w-lg text-sm text-status-warn">
            {importError}
          </p>
        )}
      </EmptyStateRoot.Content>
      <EmptyStateRoot.Footer>
        <Button color="primary" onPress={openImportPicker} data-testid="empty-import-btn">
          Import…
        </Button>
      </EmptyStateRoot.Footer>
    </EmptyStateRoot>
  );
}
