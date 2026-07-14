// Right side-panel link inspector (spec §10): connectivity facts from the
// static snapshot + the reserved NetEm section. Marking NetEm "coming soon"
// is deliberate — the backend query/edit path is a later phase; the section
// existing NOW teaches the mental model that links are configurable objects.
//
// Non-modal by design: §10 describes this as a side-panel, not a dialog — the
// map and the review bar (range presets, sources toggle) must stay interactive
// while a link is under inspection, since the selection itself is meant to
// survive ordinary review-bar interaction. A react-aria Modal (as EventsPanel
// now uses via the vendored slideout-menu) traps focus and blocks all
// pointer/keyboard input to the rest of the page, which would make that
// intent unreachable. So this renders as a plain aside with Escape-to-close
// instead — it borrows the slideout-menu's Header/Content SLOTS (plain,
// context-free div/header wrappers, not the Dialog/Modal/ModalOverlay
// machinery) for the same visual language, deliberately stopping short of the
// modal wrapper. The events panel stays on the full Dialog/Modal/ModalOverlay
// composition — its own interaction (clicking a row) closes it, so it never
// needs background interactivity while open.
//
// IN FLOW, not overlaid: this aside is a flex SIBLING of the React Flow
// container (see TopologyPage), so it reserves its own column and the canvas
// genuinely narrows to make room. It cannot cover anything, because there is
// nothing underneath it.
//
// Two occlusion bugs got us here, and the second is why "overlay" is not an
// option. It was first `fixed inset-y-0`, which spanned the full viewport
// height and covered the review bar's Apply button at <=1280px. Anchoring it to
// the canvas (`absolute`) fixed that but only moved the problem inside: the
// layered layout puts the deepest column hard against the canvas's right edge —
// exactly where a right-anchored panel sits — and fitView fits the graph to the
// canvas's FULL width, knowing nothing about a panel that will cover 384px of
// it. So selecting a link hid the map's rightmost nodes (issue #134).
//
// Both bugs are the same mistake: an overlay's reach is a function of geometry
// somebody has to keep in their head. A panel that takes up space needs no such
// bookkeeping — not a chrome offset, not a fitView padding that has to be kept
// equal to w-96 by hand. The layout engine already knows how wide this is.
//
// Task 8's migration deliberately does NOT reach for the vendored `Table`
// (react-aria-components' Table/Row/Cell collection API) for this fact list —
// it is a handful of fixed label/value pairs, not a sortable/selectable
// dataset, so a real `<table>` would be the wrong tool same as before.
import { useEffect } from "react";

import { SlideoutMenu } from "@/components/application/slideout-menus/slideout-menu";
import type { TopoEdge } from "../data/topology";
import { endpointText, primaryLink } from "./linkText";

function Row(props: { label: string; testId: string; children: React.ReactNode }) {
  return (
    <p data-testid={props.testId} className="text-sm text-secondary">
      <span className="mr-2 inline-block w-24 text-xs text-quaternary uppercase">
        {props.label}
      </span>
      {props.children}
    </p>
  );
}

export function LinkInspector(props: { edge: TopoEdge | null; onClose: () => void }) {
  const { edge, onClose } = props;

  // Guarded on `edge`, not just mounted: without this the listener is live
  // whenever the topology page is, so Escape fires onClose with nothing
  // selected. The `return null` below cannot do this job — hooks can't be
  // conditional, so the effect must decline the work itself.
  useEffect(() => {
    if (edge === null) return;
    const onKeyDown = (evt: KeyboardEvent): void => {
      if (evt.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [edge, onClose]);

  if (edge === null) return null;
  const primary = primaryLink(edge);
  const title = primary?.name ?? primary?.id ?? edge.id;
  return (
    <aside
      data-testid="link-inspector"
      className="flex w-96 max-w-full shrink-0 flex-col overflow-y-auto border-l border-secondary
        bg-primary"
    >
      <SlideoutMenu.Header onClose={onClose}>
        <h2 className="text-sm font-semibold text-primary">{title}</h2>
      </SlideoutMenu.Header>
      <SlideoutMenu.Content className="gap-3 pb-4">
        {edge.links && edge.links.length > 1 && (
          <p data-testid="inspector-collapsed-note" className="text-xs text-tertiary">
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
          className="rounded-lg border border-dashed border-secondary p-3 text-xs text-tertiary"
        >
          <p className="mb-1 font-semibold uppercase">NetEm</p>
          <p>delay / loss / jitter / rate — Configure — coming soon</p>
        </div>
      </SlideoutMenu.Content>
    </aside>
  );
}
