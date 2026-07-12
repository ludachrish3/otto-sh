// The anchored key for the topology canvas.
//
// Bottom-left, NOT right: LinkInspector is a `fixed inset-y-0 right-0 w-96`
// aside, so a right-anchored panel would be covered the moment an edge is
// selected. `mb-28` lifts it clear of React Flow's own zoom Controls, which
// occupy the same corner.
//
// Every swatch renders from EDGE_STYLES / STATUS_DOT — the same tables the
// canvas draws from — so the key cannot drift from what it explains.
import { Panel } from "@xyflow/react";

import type { EffectiveStatus } from "../data/topology";
import { Disclosure } from "../ui/Disclosure";
import { EDGE_STYLES, type Provenance } from "./edgeStyles";
import { ImpairPill } from "./ImpairPill";
import { STATUS_DOT } from "./nodes";

// Explicit, not derived from Object.keys(EDGE_STYLES): display order is a
// deliberate design choice, not alphabetical. Exported so a test can assert
// it covers exactly EDGE_STYLES's keys — TypeScript alone doesn't force this
// hand-written array to be exhaustive, only EDGE_STYLES's own Record type is.
export const LINK_ORDER: Provenance[] = ["declared", "implicit", "dynamic", "reports-for", "local"];
const STATUS_ORDER: EffectiveStatus[] = ["ok", "down", "unreachable", "no-data", "unknown"];
const STATUS_LABEL: Record<EffectiveStatus, string> = {
  ok: "ok",
  down: "down",
  unreachable: "unreachable",
  "no-data": "no data",
  unknown: "unknown",
};

const ROW = "flex items-center gap-2 py-0.5 text-[11px] text-gray-600 dark:text-gray-300";
const HEAD = "mb-1 text-[10px] font-semibold tracking-wide text-gray-400 uppercase";

function Swatch({ provenance }: { provenance: Provenance }) {
  const spec = EDGE_STYLES[provenance];
  return (
    <svg width="30" height="10" aria-hidden="true" className="shrink-0">
      {spec.casing && (
        <path
          d="M0,5 L30,5"
          fill="none"
          strokeLinecap="round"
          stroke={spec.casing.stroke}
          strokeWidth={spec.casing.strokeWidth}
          strokeOpacity={spec.casing.opacity}
        />
      )}
      <path
        d="M0,5 L30,5"
        fill="none"
        stroke={spec.stroke}
        strokeWidth={spec.strokeWidth}
        strokeDasharray={spec.strokeDasharray}
      />
    </svg>
  );
}

export function TopoLegend() {
  return (
    <Panel position="bottom-left" className="!mb-28">
      <Disclosure title="Key" testId="topo-legend" toggleTestId="topo-legend-toggle">
        <div className="grid grid-cols-2">
          <ul className="border-r border-gray-100 p-2 dark:border-gray-800">
            <li className={HEAD}>Links</li>
            {LINK_ORDER.map((p) => (
              <li
                key={p}
                data-testid={`topo-legend-link-${p}`}
                className={ROW}
                title={EDGE_STYLES[p].hint}
              >
                <Swatch provenance={p} />
                {EDGE_STYLES[p].label}
              </li>
            ))}
            <li data-testid="topo-legend-link-impair" className={ROW}>
              <span className="flex w-[30px] shrink-0 justify-center">
                <ImpairPill />
              </span>
              middlebox
            </li>
          </ul>
          <ul className="p-2">
            <li className={HEAD}>Status</li>
            {STATUS_ORDER.map((s) => (
              <li key={s} data-testid={`topo-legend-status-${s}`} className={ROW}>
                <span aria-hidden className={`h-2 w-2 shrink-0 rounded-full ${STATUS_DOT[s]}`} />
                {STATUS_LABEL[s]}
              </li>
            ))}
          </ul>
        </div>
      </Disclosure>
    </Panel>
  );
}
