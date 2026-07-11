// Empty review state (UX spec §13): the import CTA is the whole page.
import { useReviewStore } from "../data/reviewStore";
import { Button } from "../ui/Button";
import { openImportPicker } from "./ImportExport";

export function EmptyState() {
  const importError = useReviewStore((s) => s.importError);
  return (
    <div
      data-testid="empty-review"
      className="flex flex-col items-center justify-center gap-4 py-24 text-center"
    >
      <p className="text-gray-500 dark:text-gray-400">
        No data loaded — import a collection to review.
      </p>
      {importError !== null && (
        <p data-testid="import-error" className="max-w-lg text-sm text-status-warn">
          {importError}
        </p>
      )}
      <Button variant="primary" onPress={openImportPicker} testId="empty-import-btn">
        Import…
      </Button>
    </div>
  );
}
