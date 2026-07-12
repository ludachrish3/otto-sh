// Right side-panel link inspector (spec §10): connectivity facts from the
// static snapshot + the reserved NetEm section. Marking NetEm "coming soon"
// is deliberate — the backend query/edit path is a later phase; the section
// existing NOW teaches the mental model that links are configurable objects.
//
// Non-modal by design: §10 describes this as a side-panel, not a dialog — the
// map and the review bar (range presets, sources toggle) must stay
// interactive while a link is under inspection, since the selection itself is
// meant to survive ordinary review-bar interaction. A react-aria Modal (as
// used by SlideOver) traps focus and blocks all pointer/keyboard input to
// the rest of the page, which would make that intent unreachable. So this
// renders as a plain fixed aside with Escape-to-close instead. The events
// panel stays on SlideOver — its own interaction (clicking a row) closes it,
// so it never needs background interactivity while open.
import { useEffect } from "react";

import type { TopoEdge } from "../data/topology";
import { endpointText } from "./linkText";

function Row(props: { label: string; testId: string; children: React.ReactNode }) {
  return (
    <p data-testid={props.testId} className="text-sm">
      <span className="mr-2 inline-block w-24 text-xs text-gray-400 uppercase">{props.label}</span>
      {props.children}
    </p>
  );
}

export function LinkInspector(props: { edge: TopoEdge | null; onClose: () => void }) {
  const { edge, onClose } = props;

  useEffect(() => {
    const onKeyDown = (evt: KeyboardEvent): void => {
      if (evt.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  if (edge === null) return null;
  const primary = edge.link ?? edge.links?.[0] ?? null;
  const title = primary?.name ?? primary?.id ?? edge.id;
  return (
    <aside
      data-testid="link-inspector"
      className="fixed inset-y-0 right-0 z-30 flex w-96 max-w-full flex-col gap-3 overflow-y-auto
        border-l border-gray-200 bg-white p-4 shadow-lg dark:border-gray-800 dark:bg-gray-950"
    >
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold">{title}</h2>
        <button
          type="button"
          aria-label="Close"
          onClick={onClose}
          className="cursor-pointer rounded px-2 text-gray-400 hover:text-gray-600
            dark:hover:text-gray-200"
        >
          ✕
        </button>
      </div>
      {edge.links && edge.links.length > 1 && (
        <p data-testid="inspector-collapsed-note" className="text-xs text-gray-400">
          {edge.links.length} hop links — open the element to inspect individually.
        </p>
      )}
      {primary && (
        <div className="flex flex-col gap-2">
          <Row label="Protocol" testId="inspector-protocol">
            {primary.protocol ?? "—"}
          </Row>
          <Row label="Provenance" testId="inspector-provenance">
            {primary.provenance ?? "declared"}
          </Row>
          <Row label="Endpoints" testId="inspector-endpoints">
            {endpointText(primary)}
          </Row>
          {edge.impair !== null && (
            <Row label="Impair" testId="inspector-impair">
              in-path middlebox: {edge.impair}
            </Row>
          )}
        </div>
      )}
      <div
        data-testid="inspector-netem"
        className="mt-2 rounded-lg border border-dashed border-gray-200 p-3 text-xs text-gray-400
          dark:border-gray-800"
      >
        <p className="mb-1 font-semibold uppercase">NetEm</p>
        <p>delay / loss / jitter / rate — Configure — coming soon</p>
      </div>
    </aside>
  );
}
