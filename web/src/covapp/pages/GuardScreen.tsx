// Rendered by App.tsx instead of the router whenever dataGuard() != "ok"
// (or a lazily-loaded chunk rejects with StampMismatchError — the tree and
// file pages route that case here too). Built from the vendored empty-state
// (same component monitor's own EmptyState.tsx uses) per the
// component-sourcing policy: vendored UUI first, before anything
// covapp-local.
import { EmptyState } from "@/components/application/empty-state/empty-state";

type GuardReason = "missing data" | "unsupported data format" | "report changed on disk";

interface GuardScreenProps {
  reason: GuardReason;
}

export function GuardScreen({ reason }: GuardScreenProps) {
  return (
    <EmptyState data-testid="guard-screen" size="sm" className="py-24 text-center">
      <EmptyState.Content>
        <EmptyState.Title>This report needs to be regenerated</EmptyState.Title>
        <EmptyState.Description>
          Run <code>otto cov report</code> to rebuild it ({reason}).
        </EmptyState.Description>
      </EmptyState.Content>
    </EmptyState>
  );
}
