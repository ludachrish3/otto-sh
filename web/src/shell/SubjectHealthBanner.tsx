// The drilled-in unreachable treatment (5b follow-ups design doc §A): a host
// subject shows "Unreachable for 2m — showing last-known data" and dims the
// chart stack beneath it; an element subject instead names its unreachable
// members and does NOT dim — its healthy members' charts are still live and
// correct, and dimming them would lie. Both read health.ts's healthForHost,
// never a parallel copy of the down rule (gap > HEALTH_K x cadence) — that
// function is its sole owner.
//
// This component owns the clock subscription (data/clock.ts's useNow) so
// SubjectPage doesn't have to. `children` is the chart stack SubjectPage
// already built for THIS render; because SubjectPage itself never calls
// useNow, a tick re-renders only this component, and `children` is the
// exact same element reference across those re-renders — React bails out of
// the subtree beneath it instead of re-invoking ChartPanel. Moving useNow up
// into SubjectPage breaks that (see subjecthealthbanner.test.tsx's
// render-count guard and its mutation proof).
import { AlertTriangle } from "@untitledui/icons";
import type { ReactNode } from "react";

import { FeaturedIcon } from "@/components/foundations/featured-icon/featured-icon";
import { useNow } from "../data/clock";
import { type NormalizedSession, subjectKind } from "../data/exportDoc";
import { healthForHost } from "../data/health";
import { useActiveSession, useReviewStore } from "../data/reviewStore";
import { formatOutage } from "../data/time";

/** Down members of an element, in the same slot-then-id order elementRollup
 * (data/health.ts) uses. Duplicated rather than shared because elementRollup
 * returns bare `SubjectHealth[]` — the banner needs the ids themselves to
 * name members in its copy. OverviewPage's member listing duplicates the
 * same ordering locally for the same reason (it isn't the down RULE, just
 * presentation order, so this isn't the fork health.ts's module comment
 * warns against). */
function sortedMemberIds(session: NormalizedSession, elementId: string): string[] {
  const element = session.elements.find((e) => e.id === elementId);
  if (!element) return [];
  const slotOf = (id: string) =>
    session.lab.hosts.find((h) => h.id === id)?.slot ?? Number.POSITIVE_INFINITY;
  return [...element.hostIds].sort((a, b) => slotOf(a) - slotOf(b) || a.localeCompare(b));
}

export function SubjectHealthBanner(props: { subjectId: string; children: ReactNode }) {
  const { subjectId, children } = props;
  const session = useActiveSession();
  const range = useReviewStore((s) => s.range);
  const mode = useReviewStore((s) => s.mode);
  // Only live mode ticks; review's "now" is the session's own end (health.ts's
  // own nowMs default) — same pattern OverviewPage established for the fleet
  // grid's dimming.
  const tickMs =
    mode === "live" && session?.meta.interval != null ? session.meta.interval * 1000 : null;
  const now = useNow(tickMs);

  if (!session) return <>{children}</>;
  const kind = subjectKind(session, subjectId);
  if (kind === null) return <>{children}</>;

  const nowMs = mode === "live" ? now : undefined;
  const memberIds = kind === "host" ? [subjectId] : sortedMemberIds(session, subjectId);
  const down = memberIds
    .map((id) => ({ id, health: healthForHost(session, id, range, nowMs) }))
    .filter((m) => m.health.status === "down");

  // Each member carries its OWN outage duration, not a shared max — "tech2,
  // tech3 unreachable for 2m" would be true of tech2 and false of tech3 if
  // tech3 had only been down 20s (Minor 3, 5b follow-ups review). A host
  // subject has exactly one down member (memberIds above is `[subjectId]`),
  // so `down[0]` is that member's own reading, not an aggregate.
  const text =
    down.length === 0
      ? null
      : kind === "host"
        ? `Unreachable for ${formatOutage(down[0].health.outageMs)} — showing last-known data`
        : `${down
            .map((m) => `${m.id} (${formatOutage(m.health.outageMs)})`)
            .join(", ")} unreachable — showing last-known data`;

  // ONE stable wrapper across BOTH the healthy (`text === null`) and down
  // states — the banner and the dim class toggle INSIDE it instead of
  // switching the wrapper's own element type (Minor 2, 5b follow-ups
  // review). Previously the healthy branch returned a bare `<>{children}</>`
  // fragment while the down branch returned this `<div>`: React treats
  // Fragment -> div as a type change and unmounts/remounts everything
  // beneath, so every ECharts instance in the chart stack got disposed and
  // re-initialized at the exact moment a host went unreachable (and again
  // on recovery) — precisely when the banner claims "showing last-known
  // data". Keeping `children` at the same position under the same element
  // type across that transition preserves its identity, so React bails out
  // of reconciling the subtree instead of re-invoking it (see this
  // module's top-of-file comment on the `children`-identity bailout).
  return (
    <div className="flex min-w-0 grow flex-col gap-3">
      {text !== null && (
        <div
          data-testid="unreachable-banner"
          className="flex items-center gap-3 rounded-lg bg-warning-primary px-4 py-3"
        >
          <FeaturedIcon color="warning" icon={AlertTriangle} size="sm" />
          <p className="text-sm font-medium text-warning-primary">{text}</p>
        </div>
      )}
      {/* Only a host subject dims — an element's healthy members' charts are
          still live and correct, and dimming them alongside the down ones
          would lie about the data actually being current. */}
      <div
        data-testid="subject-health-stack"
        className={text !== null && kind === "host" ? "opacity-60" : undefined}
      >
        {children}
      </div>
    </div>
  );
}
