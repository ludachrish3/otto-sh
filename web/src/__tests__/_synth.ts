// A synthetic NormalizedSession at a chosen scale, for the tier-1 budget guards.

import type { HostSnapshot, MetricRecord } from "../api/export.gen";
import type { NormalizedSession } from "../data/exportDoc";
import { buildIndex } from "../data/seriesIndex";

const T0 = Date.parse("2026-07-12T00:00:00Z");

export function synthSession(args: {
  hosts: number;
  seriesPerHost: number;
  ticks: number;
  intervalS: number;
}): NormalizedSession {
  const { hosts, seriesPerHost, ticks, intervalS } = args;
  const metrics: MetricRecord[] = [];
  for (let t = 0; t < ticks; t++) {
    const iso = new Date(T0 + t * intervalS * 1000).toISOString();
    for (let h = 0; h < hosts; h++) {
      for (let s = 0; s < seriesPerHost; s++) {
        metrics.push({
          host: `h${h}`,
          label: `m${s}`,
          timestamp: iso,
          value: t + s,
        } as MetricRecord);
      }
    }
  }
  // Real hosts, one per `h{n}` used as the metrics' `host` field above — a
  // synth with `lab.hosts: []` never enters healthForHosts's per-host loop
  // body at all, so a budget test against it can't see per-host cost.
  const labHosts: HostSnapshot[] = Array.from({ length: hosts }, (_, h) => ({
    id: `h${h}`,
    element: `h${h}`,
  }));
  return {
    id: "synth",
    label: null,
    note: null,
    startMs: T0,
    endMs: T0 + (ticks - 1) * intervalS * 1000,
    lab: { hosts: labHosts, links: [], explicitElements: [] },
    meta: { interval: intervalS, charts: [], tabs: [] },
    metrics,
    events: [],
    logEvents: [],
    index: buildIndex(metrics),
    chartMap: {},
    tunnels: [],
    elements: [],
    hostIds: new Set(labHosts.map((h) => h.id)),
    elementIds: new Set(),
  } satisfies NormalizedSession;
}
