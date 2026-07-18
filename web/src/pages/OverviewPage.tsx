// Fleet grid (UX spec §8): element sections with health-rollup bars and
// host status tiles — status dot · name · board·slot · labeled headline
// metric; down tiles show the outage duration instead. All health is
// derived, range-scoped (data/health.ts) — nothing here is stored state.
import { Link } from "wouter";

import { useNow } from "../data/clock";
import { elementRollup, headlineFor, healthForHosts, type SubjectHealth } from "../data/health";
import { useActiveSession, useReviewStore } from "../data/reviewStore";
import { formatOutage } from "../data/time";
import { ViewSwitcher } from "../ui/ViewSwitcher";

const DOT_CLASS: Record<SubjectHealth["status"], string> = {
  ok: "bg-status-ok",
  down: "bg-status-error",
  "no-data": "bg-fg-quaternary",
  unknown: "bg-quaternary",
};

const SEGMENT_CLASS: Record<SubjectHealth["status"], string> = {
  ok: "bg-status-ok",
  down: "bg-status-error",
  "no-data": "bg-fg-quaternary",
  unknown: "bg-quaternary",
};

export function OverviewPage() {
  const session = useActiveSession();
  const range = useReviewStore((s) => s.range);
  const mode = useReviewStore((s) => s.mode);
  // Unreachable dimming needs a clock, not events: a silent host emits no
  // SSE message, so nothing would ever re-render it without a tick. Ticks
  // at the session's own collection interval (design: the down threshold
  // IS HEALTH_K x cadence, so a faster clock can't learn anything sooner).
  // Only live mode ticks at all — an archive's "now" is its own endMs, no
  // wall clock involved. This clock lives in its own store (data/clock.ts)
  // specifically so a tick re-renders only this page, never chart pages.
  const tickMs =
    mode === "live" && session?.meta.interval != null ? session.meta.interval * 1000 : null;
  const now = useNow(tickMs);
  if (!session) return null;

  // Liveness keeps ticking while paused (spec): nowMs comes from the wall
  // clock whenever live, independent of `range` — a paused/frozen VIEW must
  // not freeze the fleet's actual down/ok verdicts.
  const healths = healthForHosts(session, range, mode === "live" ? now : undefined);
  const hostById = new Map(session.lab.hosts.map((h) => [h.id, h]));

  return (
    <main data-testid="overview-page" className="flex flex-col gap-6 p-4">
      <div className="flex items-center gap-3">
        <ViewSwitcher active="hosts" />
      </div>
      {session.elements.map((el) => {
        const rollup = elementRollup(el, healths, session);
        const memberIds = [...el.hostIds].sort((a, b) => {
          const slotA = hostById.get(a)?.slot ?? Number.POSITIVE_INFINITY;
          const slotB = hostById.get(b)?.slot ?? Number.POSITIVE_INFINITY;
          return slotA - slotB || a.localeCompare(b);
        });
        return (
          <section key={el.id} data-testid={`element-section-${el.id}`}>
            <h2 className="mb-1 flex items-center gap-2 text-sm font-semibold">
              <span aria-hidden>{el.type === "physical" ? "▦" : "▤"}</span>
              {el.id}
              <span className="font-normal text-quaternary">
                {el.hostIds.length} host{el.hostIds.length === 1 ? "" : "s"}
                {el.description ? ` · ${el.description}` : ""}
              </span>
            </h2>
            {rollup.length > 0 && (
              <div
                data-testid={`health-rollup-${el.id}`}
                className="mb-2 flex h-1.5 w-full max-w-md gap-px overflow-hidden rounded"
                title={rollupTitle(rollup)}
              >
                {rollup.map((h, i) => (
                  <span
                    // biome-ignore lint/suspicious/noArrayIndexKey: segments are positional by design
                    key={i}
                    className={`min-w-1 flex-1 ${SEGMENT_CLASS[h.status]}`}
                  />
                ))}
              </div>
            )}
            <ul className="flex flex-wrap gap-2">
              {memberIds.map((hostId) => {
                const host = hostById.get(hostId);
                const health = healths.get(hostId) ?? {
                  status: "unknown" as const,
                  lastSeenMs: null,
                  outageMs: 0,
                };
                const headline =
                  health.status === "ok" ? headlineFor(session, hostId, range) : null;
                return (
                  <li key={hostId}>
                    <Link
                      href={`/host/${hostId}`}
                      data-testid={`subject-link-${hostId}`}
                      className="block rounded-lg border border-secondary px-3 py-2 text-sm
                        hover:border-brand-500"
                    >
                      <article
                        data-testid={`host-tile-${hostId}`}
                        data-health={health.status}
                        className="flex min-w-36 flex-col gap-1"
                      >
                        <span className="flex items-center gap-2 font-medium">
                          <span
                            aria-hidden
                            title={health.status}
                            className={`h-2 w-2 rounded-full ${DOT_CLASS[health.status]}`}
                          />
                          {hostId}
                        </span>
                        <span className="text-xs text-quaternary">
                          {host?.board ?? "—"}
                          {host?.slot != null ? ` · slot ${host.slot}` : ""}
                        </span>
                        {health.status === "down" ? (
                          <span className="text-xs font-medium text-status-error">
                            {/* formatOutage (Task 5), not formatSpan: the down
                                threshold (HEALTH_K x cadence) is reachable in
                                seconds, and formatSpan alone prints "0m" for
                                any outage under a minute (Minor 5, 5b
                                follow-ups review). */}
                            down · {formatOutage(health.outageMs)}
                          </span>
                        ) : health.status === "ok" && headline ? (
                          <span
                            data-testid={`headline-${hostId}`}
                            className="text-xs text-secondary"
                          >
                            {headline.text}
                          </span>
                        ) : (
                          <span className="text-xs text-quaternary">
                            {health.status === "no-data" ? "no data" : "—"}
                          </span>
                        )}
                      </article>
                    </Link>
                  </li>
                );
              })}
              {el.hostIds.length === 0 && (
                <li className="text-sm text-quaternary">empty — no hosts fitted</li>
              )}
            </ul>
          </section>
        );
      })}
    </main>
  );
}

function rollupTitle(rollup: SubjectHealth[]): string {
  const counts = new Map<string, number>();
  for (const h of rollup) counts.set(h.status, (counts.get(h.status) ?? 0) + 1);
  return [...counts.entries()].map(([status, n]) => `${n} ${status}`).join(" · ");
}
