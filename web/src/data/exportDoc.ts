// Client-side reader of the versioned monitor export document (spec
// 2026-07-10 §3; wire types generated in api/export.gen.ts). This is the
// Import front door's parser: it validates the format marker, normalizes
// the lenient optionals ONCE at the boundary (everything downstream sees
// dense arrays), derives elements from hosts, and surfaces non-fatal
// oddities as warnings (spec ship-and-note: duplicate ids warn, not fail).
import type {
  ChartSpecRecord,
  ElementRecord,
  HostSnapshot,
  LinkSnapshot,
  MetricRecord,
  MonitorHistoricalExportDocument,
  SessionRecord,
  TabSpecRecord,
} from "../api/export.gen";
import { parseTs } from "./time";

export class ExportParseError extends Error {}

export interface TimeRange {
  from: number;
  to: number;
}

export interface DerivedElement {
  id: string;
  type: "physical" | "logical";
  explicit: boolean;
  description: string | null;
  hostIds: string[];
  singleton: boolean;
}

/** Dense presentation meta: normalized ONCE at the boundary (follow-up #1) —
 * downstream code iterates charts/tabs unconditionally, never `?? []`. */
export interface NormalizedMeta {
  interval: number | null;
  charts: ChartSpecRecord[];
  tabs: TabSpecRecord[];
}

export interface NormalizedSession {
  id: string;
  label: string | null;
  note: string | null;
  startMs: number;
  endMs: number;
  lab: {
    hosts: HostSnapshot[];
    links: LinkSnapshot[];
    explicitElements: ElementRecord[];
  };
  meta: NormalizedMeta;
  metrics: MetricRecord[];
  events: NonNullable<SessionRecord["events"]>;
  logEvents: NonNullable<SessionRecord["log_events"]>;
  chartMap: Record<string, string>;
  elements: DerivedElement[];
  hostIds: Set<string>;
  elementIds: Set<string>;
}

export interface ParseResult {
  document: MonitorHistoricalExportDocument;
  sessions: NormalizedSession[];
  warnings: string[];
}

export function deriveElements(hosts: HostSnapshot[], explicit: ElementRecord[]): DerivedElement[] {
  const byId = new Map<string, DerivedElement>();
  for (const rec of explicit) {
    byId.set(rec.id, {
      id: rec.id,
      type: rec.type ?? "logical",
      explicit: true,
      description: rec.description ?? null,
      hostIds: [],
      singleton: false,
    });
  }
  for (const host of hosts) {
    const existing = byId.get(host.element);
    if (existing) {
      existing.hostIds.push(host.id);
    } else {
      byId.set(host.element, {
        id: host.element,
        type: "logical",
        explicit: false,
        description: null,
        hostIds: [host.id],
        singleton: false,
      });
    }
  }
  for (const el of byId.values()) {
    // Type inference only where not explicitly declared: slots => physical.
    if (!el.explicit) {
      const hostsOf = hosts.filter((h) => h.element === el.id);
      el.type = hostsOf.some((h) => h.slot !== null && h.slot !== undefined)
        ? "physical"
        : "logical";
    }
    // Singleton is ALWAYS derived from membership count (spec §2).
    el.singleton = el.hostIds.length === 1;
  }
  return [...byId.values()].sort((a, b) => a.id.localeCompare(b.id));
}

function normalizeSession(raw: SessionRecord, warnings: string[]): NormalizedSession {
  const hosts = raw.lab?.hosts ?? [];
  const links = raw.lab?.links ?? [];
  const explicitElements = raw.lab?.elements ?? [];
  const metrics = raw.metrics ?? [];
  const startMs = parseTs(raw.start);
  const lastSampleMs = metrics.length
    ? Math.max(...metrics.map((m) => parseTs(m.timestamp)))
    : null;
  const endMs = raw.end != null ? parseTs(raw.end) : (lastSampleMs ?? startMs);

  const hostIds = new Set<string>();
  for (const h of hosts) {
    if (hostIds.has(h.id)) warnings.push(`session ${raw.id}: duplicate host id ${h.id}`);
    hostIds.add(h.id);
  }
  const elements = deriveElements(hosts, explicitElements);

  return {
    id: raw.id,
    label: raw.label ?? null,
    note: raw.note ?? null,
    startMs,
    endMs,
    lab: { hosts, links, explicitElements },
    meta: {
      interval: raw.meta?.interval ?? null,
      charts: raw.meta?.charts ?? [],
      tabs: raw.meta?.tabs ?? [],
    },
    metrics,
    events: raw.events ?? [],
    logEvents: raw.log_events ?? [],
    chartMap: raw.chart_map ?? {},
    elements,
    hostIds,
    elementIds: new Set(elements.map((e) => e.id)),
  };
}

export function parseExportDocument(text: string): ParseResult {
  let doc: unknown;
  try {
    doc = JSON.parse(text);
  } catch {
    throw new ExportParseError("Not a JSON document.");
  }
  if (typeof doc !== "object" || doc === null) {
    throw new ExportParseError("Not a JSON object.");
  }
  const record = doc as Record<string, unknown>;
  if (!("format" in record)) {
    throw new ExportParseError(
      "No 'format' field — this looks like a legacy unversioned export. " +
        "Re-export from a current otto run.",
    );
  }
  if (record.format !== 1) {
    throw new ExportParseError(`Unsupported export format ${String(record.format)}.`);
  }
  if (!Array.isArray(record.sessions)) {
    throw new ExportParseError("Missing 'sessions' array.");
  }
  const typed = doc as MonitorHistoricalExportDocument;
  const warnings: string[] = [];
  const seen = new Set<string>();
  for (const s of typed.sessions) {
    if (seen.has(s.id)) warnings.push(`duplicate session id ${s.id}`);
    seen.add(s.id);
  }
  const sessions = typed.sessions.map((s) => normalizeSession(s, warnings));
  return { document: typed, sessions, warnings };
}

export function sessionBounds(session: NormalizedSession): TimeRange {
  return { from: session.startMs, to: session.endMs };
}

/** minutes=null means Full range → no filter (null). */
export function presetRange(bounds: TimeRange, minutes: number | null): TimeRange | null {
  if (minutes === null) return null;
  return { from: Math.max(bounds.from, bounds.to - minutes * 60_000), to: bounds.to };
}

export function clampRange(range: TimeRange, bounds: TimeRange): TimeRange {
  return {
    from: Math.max(range.from, bounds.from),
    to: Math.min(range.to, bounds.to),
  };
}

export function subjectKind(session: NormalizedSession, id: string): "host" | "element" | null {
  if (session.hostIds.has(id)) return "host";
  if (session.elementIds.has(id)) return "element";
  return null;
}

export function metricsForSubject(
  session: NormalizedSession,
  subjectId: string,
  range: TimeRange | null,
): MetricRecord[] {
  return session.metrics.filter((m) => {
    if (m.host !== subjectId) return false;
    if (range === null) return true;
    const ts = parseTs(m.timestamp);
    return ts >= range.from && ts <= range.to;
  });
}
