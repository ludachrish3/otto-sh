// The drilled-in unreachable treatment (5b follow-ups design doc §A).
//
// Three groups of tests:
//  - SubjectHealthBanner in isolation (host dim / element no-dim / healthy /
//    live-vs-review "now" / the mount-identity guard below) — rendered
//    directly with a stub `children`, no SubjectPage or routing involved.
//  - The render-count guard, which MUST mount the real SubjectPage: the
//    property under test ("a tick re-renders the banner, never the chart
//    stack") lives in how SubjectPage wires SubjectHealthBanner, not in
//    the banner alone. See the mutation proof in this task's report for
//    what happens when that wiring is broken.
//  - The review-mode wiring guard, also via the real SubjectPage.
import { act, cleanup, render, screen } from "@testing-library/react";
import { useEffect } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

globalThis.ResizeObserver ??= class {
  observe() {}
  unobserve() {}
  disconnect() {}
} as unknown as typeof ResizeObserver;

/** Counts actual mount/unmount lifecycle events via an effect with an empty
 * dependency array -- NOT a render counter (like `chartPanelRenders`
 * below), which can't tell a remount from an ordinary re-render: both call
 * the function body again. Only an effect's mount/cleanup pair proves
 * whether React actually tore down and recreated the fiber, which is
 * exactly the instrument the whole-branch review used to catch Minor 2
 * (SubjectHealthBanner's healthy path returning a bare `<>{children}</>`
 * fragment while its down path returned a `<div>` wrapper, a type change
 * that unmounts/remounts everything beneath): "reviewer verified with a
 * probe child counting mounts: mounts=2, unmounts=1" after crossing the
 * down threshold. */
function MountCountingProbe(props: { onMount: () => void; onUnmount: () => void }) {
  const { onMount, onUnmount } = props;
  useEffect(() => {
    onMount();
    return () => onUnmount();
  }, [onMount, onUnmount]);
  return <div data-testid="chart-stack-stub" />;
}

// Full replacement, not a spy on echarts: SubjectPage's own ChartSection
// still runs for real (buildStackOption etc.), only the leaf that would
// touch a real canvas is swapped out. What makes the render-count assertion
// below meaningful is NOT the `useReviewStore` line inside the double (the
// REAL ChartPanel subscribes to no store at all — it is entirely
// props-driven) — it's that this double is a plain, non-memoized function
// component whose render body increments the counter on every invocation,
// so the counter tracks every time React actually re-renders it (the exact
// property four earlier Plan 5b render-count guards lacked, letting them go
// tautologically green — see data/clock.ts's clock.test.tsx TIER-2 GUARD,
// which established this idiom first). What keeps the count flat across a
// tick is SubjectHealthBanner's `children`-identity bailout (see that
// module's comment): React skips reconciling the subtree beneath a stable
// `children` reference instead of re-invoking this component and finding
// nothing changed.
let chartPanelRenders = 0;
vi.mock("../charts/ChartPanel", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../charts/ChartPanel")>();
  return {
    ...actual,
    ChartPanel: (props: Parameters<typeof actual.ChartPanel>[0]) => {
      useReviewStore((s) => s.range);
      chartPanelRenders++;
      return <div data-testid={props.testId ?? "chart-panel-stub"} />;
    },
  };
});

vi.mock("wouter", async (importOriginal) => {
  const mod = await importOriginal<typeof import("wouter")>();
  return { ...mod, useParams: () => ({ id: "h0" }) };
});

import type { HostSnapshot, MetricRecord } from "../api/export.gen";
import { useClockStore } from "../data/clock";
import { deriveElements, type NormalizedSession } from "../data/exportDoc";
import { useReviewStore } from "../data/reviewStore";
import { buildIndex } from "../data/seriesIndex";
import { SubjectPage } from "../pages/SubjectPage";
import { SubjectHealthBanner } from "./SubjectHealthBanner";

const T0 = Date.parse("2026-07-13T00:00:00Z");
const INTERVAL_S = 5; // HEALTH_K (3) x 5s = 15s down threshold

function rackSession(lastSeen: Record<"tech1" | "tech2" | "tech3", number>): NormalizedSession {
  const hosts: HostSnapshot[] = [
    { id: "tech1", element: "rack1", slot: 1 },
    { id: "tech2", element: "rack1", slot: 2 },
    { id: "tech3", element: "rack1", slot: 3 },
  ];
  const metrics: MetricRecord[] = (
    Object.entries(lastSeen) as [keyof typeof lastSeen, number][]
  ).map(([host, ts]) => ({
    host,
    label: "cpu",
    timestamp: new Date(ts).toISOString(),
    value: 1,
  }));
  const elements = deriveElements(hosts, []);
  return {
    id: "s",
    label: null,
    note: null,
    startMs: T0 - 3_600_000,
    endMs: T0,
    lab: { hosts, links: [], explicitElements: [] },
    meta: {
      interval: INTERVAL_S,
      charts: [{ label: "cpu", y_title: "%", unit: "%", command: "c", chart: "CPU" }],
      tabs: [],
    },
    metrics,
    events: [],
    logEvents: [],
    index: buildIndex(metrics),
    chartMap: { cpu: "CPU" },
    elements,
    hostIds: new Set(hosts.map((h) => h.id)),
    elementIds: new Set(elements.map((e) => e.id)),
  } satisfies NormalizedSession;
}

/** Single-host live session for the render-count guard: one chart, one
 * sample already old enough to be down at mount, so ticking only grows the
 * outage rather than needing to flip status first. */
function liveHostSession(): NormalizedSession {
  const hosts: HostSnapshot[] = [{ id: "h0", element: "h0" }];
  const metrics: MetricRecord[] = [
    { host: "h0", label: "cpu", timestamp: new Date(T0 - 20_000).toISOString(), value: 42 },
  ];
  const elements = deriveElements(hosts, []);
  return {
    id: "s",
    label: null,
    note: null,
    startMs: T0 - 3_600_000,
    endMs: T0,
    lab: { hosts, links: [], explicitElements: [] },
    meta: {
      interval: INTERVAL_S,
      charts: [{ label: "cpu", y_title: "%", unit: "%", command: "c", chart: "CPU" }],
      tabs: [],
    },
    metrics,
    events: [],
    logEvents: [],
    index: buildIndex(metrics),
    chartMap: { cpu: "CPU" },
    elements,
    hostIds: new Set(["h0"]),
    elementIds: new Set(elements.map((e) => e.id)),
  } satisfies NormalizedSession;
}

function resetStore() {
  useReviewStore.setState({
    sessions: [],
    rawMonitorSessions: null,
    sourceName: null,
    warnings: [],
    importError: null,
    activeSessionId: null,
    range: null,
    mode: null,
    connection: "connecting",
    windowMs: 900_000,
  });
}

afterEach(() => {
  cleanup();
  resetStore();
  vi.useRealTimers();
});

describe("SubjectHealthBanner", () => {
  it("dims a host subject and names its outage", () => {
    const session = rackSession({ tech1: T0 - 120_000, tech2: T0, tech3: T0 });
    useReviewStore.setState({
      sessions: [session],
      activeSessionId: session.id,
      mode: "review",
      range: null,
    });

    render(
      <SubjectHealthBanner subjectId="tech1">
        <div data-testid="chart-stack-stub" />
      </SubjectHealthBanner>,
    );

    expect(screen.getByTestId("unreachable-banner").textContent).toBe(
      "Unreachable for 2m — showing last-known data",
    );
    expect(screen.getByTestId("subject-health-stack").className).toContain("opacity-60");
  });

  it("names unreachable members on an element subject with each member's own outage duration", () => {
    // tech2 down much longer than tech3 -- the copy must carry EACH
    // member's own duration (Minor 3, 5b follow-ups review), not group them
    // under the longest one: a shared max would say tech3 has been down 2m
    // when it's only been 20s. tech1 stays healthy — its chart must NOT be
    // dimmed alongside the down members.
    const session = rackSession({ tech1: T0, tech2: T0 - 120_000, tech3: T0 - 20_000 });
    useReviewStore.setState({
      sessions: [session],
      activeSessionId: session.id,
      mode: "review",
      range: null,
    });

    render(
      <SubjectHealthBanner subjectId="rack1">
        <div data-testid="chart-stack-stub" />
      </SubjectHealthBanner>,
    );

    expect(screen.getByTestId("unreachable-banner").textContent).toBe(
      "tech2 (2m), tech3 (20s) unreachable — showing last-known data",
    );
    expect(screen.getByTestId("subject-health-stack").className).not.toContain("opacity-60");
  });

  it("renders no banner (but keeps the stable stack wrapper) when the subject is healthy", () => {
    const session = rackSession({ tech1: T0, tech2: T0, tech3: T0 });
    useReviewStore.setState({
      sessions: [session],
      activeSessionId: session.id,
      mode: "review",
      range: null,
    });

    render(
      <SubjectHealthBanner subjectId="tech1">
        <div data-testid="chart-stack-stub" />
      </SubjectHealthBanner>,
    );

    expect(screen.queryByTestId("unreachable-banner")).toBeNull();
    // `subject-health-stack` is now ALWAYS present, healthy or not (Minor
    // 2, 5b follow-ups review) -- it's the one stable wrapper `children`
    // lives under in both states, so its identity survives a healthy<->down
    // transition instead of the whole subtree unmounting/remounting. Not
    // dimmed while healthy.
    expect(screen.getByTestId("subject-health-stack").className).not.toContain("opacity-60");
    // getByTestId throws if absent — this project doesn't depend on
    // @testing-library/jest-dom (see livechrome.test.tsx), so presence is
    // asserted by successfully finding it rather than toBeInTheDocument.
    expect(screen.getByTestId("chart-stack-stub").textContent).toBe("");
  });

  it("no-data (a host with a series but nothing in range) does not produce a banner", () => {
    // tech1 has no metric at all in `lastSeen` below session.startMs..endMs —
    // buildIndex still needs SOME series for the host to exist at all, so
    // give it one sample far outside the evaluated range instead of none.
    const session = rackSession({
      tech1: T0 - 10 * 3_600_000, // before session.startMs (T0 - 1h) -> no-data in range
      tech2: T0,
      tech3: T0,
    });
    useReviewStore.setState({
      sessions: [session],
      activeSessionId: session.id,
      mode: "review",
      range: { from: T0 - 3_600_000, to: T0 }, // narrower than tech1's only sample
    });

    render(
      <SubjectHealthBanner subjectId="tech1">
        <div data-testid="chart-stack-stub" />
      </SubjectHealthBanner>,
    );

    expect(screen.queryByTestId("unreachable-banner")).toBeNull();
  });

  it("uses the ticking clock as 'now' in live mode, not the session's frozen end", () => {
    vi.useFakeTimers();
    vi.setSystemTime(T0);
    // data/clock.ts's store initializes `now: Date.now()` at MODULE LOAD
    // time, long before this test's vi.setSystemTime — reset explicitly
    // (same fix topology.livehealth.test.tsx needed for the same reason).
    useClockStore.setState({ now: T0 });

    const session = rackSession({ tech1: T0, tech2: T0, tech3: T0 });
    useReviewStore.setState({
      sessions: [session],
      activeSessionId: session.id,
      mode: "live",
      range: null,
    });

    render(
      <SubjectHealthBanner subjectId="tech1">
        <div data-testid="chart-stack-stub" />
      </SubjectHealthBanner>,
    );
    // At mount, tech1's sample is fresh relative to the live clock too.
    expect(screen.queryByTestId("unreachable-banner")).toBeNull();

    // Session.endMs never advances (no fragment arrives) — only the wall
    // clock reveals the outage, past HEALTH_K(3) x 5s = 15s.
    act(() => {
      vi.advanceTimersByTime(20_000);
    });

    expect(screen.getByTestId("unreachable-banner").textContent).toBe(
      "Unreachable for 20s — showing last-known data",
    );
  });

  it("does not remount the chart stack when crossing from healthy to down (Minor 2, 5b follow-ups review)", () => {
    // Rendered directly (isolation), not via the real SubjectPage: the
    // defect lives entirely in SubjectHealthBanner's OWN conditional
    // render structure (Fragment when healthy vs a `<div>` wrapper when
    // down), not in how SubjectPage wires it -- unlike the render-count
    // guard below, which tests SubjectPage's tick wiring and must go
    // through the real page. This guard, unlike every other test in this
    // file, STARTS HEALTHY and ticks the clock past the down threshold --
    // the one transition ("mounts=2, unmounts=1" per the review's own
    // probe) the pre-existing render-count guard structurally cannot see,
    // because its fixture starts already-down.
    vi.useFakeTimers();
    vi.setSystemTime(T0);
    useClockStore.setState({ now: T0 });

    const session = rackSession({ tech1: T0, tech2: T0, tech3: T0 });
    useReviewStore.setState({
      sessions: [session],
      activeSessionId: session.id,
      mode: "live",
      range: null,
    });

    let mounts = 0;
    let unmounts = 0;
    const probe = (
      <MountCountingProbe
        onMount={() => {
          mounts++;
        }}
        onUnmount={() => {
          unmounts++;
        }}
      />
    );

    render(<SubjectHealthBanner subjectId="tech1">{probe}</SubjectHealthBanner>);
    expect(screen.queryByTestId("unreachable-banner")).toBeNull(); // healthy at mount
    expect(mounts).toBe(1);
    expect(unmounts).toBe(0);

    act(() => {
      vi.advanceTimersByTime(20_000);
    });

    expect(screen.getByTestId("unreachable-banner").textContent).toBe(
      "Unreachable for 20s — showing last-known data",
    );
    // The whole point: the chart stack survived the healthy -> down
    // transition without being torn down and rebuilt.
    expect(mounts).toBe(1);
    expect(unmounts).toBe(0);
  });
});

describe("SubjectHealthBanner render-count guard (mounted via the real SubjectPage)", () => {
  beforeEach(() => {
    chartPanelRenders = 0;
    vi.useFakeTimers();
    vi.setSystemTime(T0);
    useClockStore.setState({ now: T0 });
  });

  it("a tick grows the banner's outage text while ChartPanel re-renders zero additional times", () => {
    const session = liveHostSession();
    useReviewStore.setState({
      sessions: [session],
      activeSessionId: session.id,
      mode: "live",
      range: null,
      windowMs: 900_000,
    });

    render(<SubjectPage />);

    expect(screen.getByTestId("unreachable-banner").textContent).toBe(
      "Unreachable for 20s — showing last-known data",
    );
    const rendersAfterMount = chartPanelRenders;
    // Sanity check that the double is actually reachable in the tree at all
    // — a mock wired to the wrong path would leave this at 0 regardless of
    // what SubjectPage does, which would make the assertion below pass for
    // the wrong reason.
    expect(rendersAfterMount).toBeGreaterThan(0);

    // 3 ticks at the 5s collection interval, no new fragment ever arrives.
    act(() => {
      vi.advanceTimersByTime(15_000);
    });

    expect(screen.getByTestId("unreachable-banner").textContent).toBe(
      "Unreachable for 35s — showing last-known data",
    );
    expect(chartPanelRenders).toBe(rendersAfterMount);
  });
});

describe("SubjectHealthBanner wiring in review mode (mounted via the real SubjectPage)", () => {
  // Important 2, 5b follow-ups review: every other test that mounts the
  // REAL SubjectPage (the render-count guard above) does so in live mode —
  // nothing exercised the banner's wiring in review mode at all. A
  // mode-gated regression (e.g. wrapping SubjectHealthBanner in `{mode ===
  // "live" && ...}`) would slip through every other gate in this file,
  // since the unit tests above render SubjectHealthBanner directly and
  // never touch SubjectPage's own JSX. This workstream has already shipped
  // exactly this class of bug once (a deleted wiring call left every test
  // green) — see this task's mutation proof.
  it("renders the banner for an archived session whose host died before the session ended", () => {
    const session = liveHostSession();
    useReviewStore.setState({
      sessions: [session],
      activeSessionId: session.id,
      mode: "review",
      range: null,
      windowMs: 900_000,
    });

    render(<SubjectPage />);

    // healthForHost defaults `nowMs` to the session's own end when not live
    // (data/health.ts) — h0's one sample is already 20s before session.endMs
    // (liveHostSession's fixture), past HEALTH_K(3) x 5s = 15s, so the
    // archive itself, with no clock involved, is enough to read "down."
    expect(screen.getByTestId("unreachable-banner").textContent).toBe(
      "Unreachable for 20s — showing last-known data",
    );
  });
});
