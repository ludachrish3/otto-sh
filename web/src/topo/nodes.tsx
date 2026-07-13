// Custom React Flow nodes — plain Tailwind DOM, so testids, tokens and the
// Playwright contract all survive (the reason React Flow beat canvas
// renderers, spec §Decisions). data-status carries semantic state for tests;
// classes are presentation only.
import { Handle, Position, useNodeId } from "@xyflow/react";

import type { EffectiveStatus, TopoNode } from "../data/topology";

export const STATUS_DOT: Record<EffectiveStatus, string> = {
  ok: "bg-status-ok",
  down: "bg-status-error",
  unreachable: "border border-status-error/60 bg-transparent",
  "no-data": "bg-gray-300 dark:bg-gray-600",
  unknown: "bg-gray-200 dark:bg-gray-700",
};

export const STATUS_SEGMENT: Record<EffectiveStatus, string> = {
  ok: "bg-status-ok",
  down: "bg-status-error",
  unreachable: "bg-status-error/25",
  "no-data": "bg-gray-300 dark:bg-gray-600",
  unknown: "bg-gray-200 dark:bg-gray-700",
};

const SEVERITY: EffectiveStatus[] = ["down", "unreachable", "no-data", "unknown", "ok"];

function worst(statuses: EffectiveStatus[]): EffectiveStatus {
  // An empty rollup means the element has no hosts to report on — that's a
  // structural fact, not a clean bill of health (same "no claim" philosophy
  // as the health module: absence of data proves nothing either way).
  if (statuses.length === 0) return "unknown";
  for (const s of SEVERITY) if (statuses.includes(s)) return s;
  return "ok";
}

function Ports() {
  const nodeId = useNodeId();
  if (nodeId === null) return null;
  return (
    <>
      <Handle type="target" position={Position.Left} className="!h-1 !w-1 !opacity-0" />
      <Handle type="source" position={Position.Right} className="!h-1 !w-1 !opacity-0" />
    </>
  );
}

export function LocalNode({ data: _data }: { data: TopoNode }) {
  return (
    <div
      data-testid="topo-node-local"
      data-status="local"
      className="rounded-lg border-2 border-brand-500 bg-white px-3 py-2 text-sm font-semibold
        dark:bg-gray-950"
    >
      ◉ local
      <Ports />
    </div>
  );
}

export function ElementNode({ data }: { data: TopoNode }) {
  const rollup = data.rollup ?? [];
  return (
    <div
      data-testid={`topo-node-${data.id}`}
      data-status={worst(rollup)}
      className="w-52 cursor-pointer rounded-lg border border-gray-200 bg-white px-3 py-2
        hover:border-brand-500 dark:border-gray-800 dark:bg-gray-950 dark:hover:border-brand-500"
    >
      <p className="flex items-center gap-2 text-sm font-medium">
        <span aria-hidden>{data.element?.type === "physical" ? "▦" : "▤"}</span>
        <span className="truncate">{data.label}</span>
        <span aria-hidden className="ml-auto text-gray-400">
          ⤢
        </span>
      </p>
      {rollup.length > 0 && (
        <div className="mt-1.5 flex h-1.5 w-full gap-px overflow-hidden rounded">
          {rollup.map((status, i) => (
            <span
              // biome-ignore lint/suspicious/noArrayIndexKey: segments are positional by design
              key={i}
              data-status-segment={status}
              className={`min-w-1 flex-1 ${STATUS_SEGMENT[status]}`}
            />
          ))}
        </div>
      )}
      <p className="mt-1 text-xs text-gray-400">
        {data.element?.hostIds.length ?? 0} host
        {(data.element?.hostIds.length ?? 0) === 1 ? "" : "s"}
      </p>
      <Ports />
    </div>
  );
}

export function HostNode({ data }: { data: TopoNode & { slotBadge?: boolean } }) {
  const slotBadge = data.slotBadge ?? false;
  const status = data.effective ?? "unknown";
  // Parts, then join — not string concatenation with a baked-in separator. The
  // old form emitted "unreachable · " with nothing after it when a host had
  // neither a slot badge nor a board, and an empty <p> (still carrying its
  // margin) when it had neither and was reachable.
  const detail = [
    status === "unreachable" ? "unreachable" : null,
    slotBadge && data.host?.slot != null ? `slot ${data.host.slot}` : (data.host?.board ?? null),
  ]
    .filter((part) => part !== null && part !== "")
    .join(" · ");
  return (
    <div
      data-testid={`topo-node-${data.id}`}
      data-status={status}
      className={`w-44 cursor-pointer rounded-lg border border-gray-200 bg-white px-3 py-2
        hover:border-brand-500 dark:border-gray-800 dark:bg-gray-950 dark:hover:border-brand-500
        ${status === "unreachable" ? "opacity-60" : ""}`}
    >
      <p className="flex items-center gap-2 text-sm font-medium">
        <span aria-hidden className={`h-2 w-2 shrink-0 rounded-full ${STATUS_DOT[status]}`} />
        <span className="truncate">{data.label}</span>
      </p>
      {detail !== "" && <p className="mt-0.5 text-xs text-gray-400">{detail}</p>}
      <Ports />
    </div>
  );
}
