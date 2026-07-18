# Topology as the monitor's default view + docs topology hero

**Date:** 2026-07-17
**Status:** Approved (brainstorm session with Chris)
**Companion spec:** `2026-07-17-makefile-quality-parity-design.md` (same
session; independent change, separate implementation).

## Goal

Opening `otto monitor` lands on the topology map — it is the flagship view —
and the docs lead with a dense, "everything visible" topology screenshot
instead of the current fleet-grid hero.

## Decisions (made in the brainstorm)

1. **App + docs, not docs-only.** The `/` route becomes `TopologyPage`; the
   fleet grid moves to its own route. Docs-only would lead with a view that
   isn't what actually opens.
2. **The grid screenshot moves down, not out:** the hero in
   `docs/guide/monitor.md` becomes the new topology shot; the grid still
   appears beside the "Web dashboard" section that describes it. The charts
   screenshot stays where it is.
3. **The topology shot is fed by `isp-core.json` + a generator touch-up**
   (Chris's "telephony core" pick — no fixture by that name exists;
   `isp-core` is the densest at 25 hosts / 44 links). The touch-up adds a
   degraded tunnel and a couple of chassis `elements` so one frame shows the
   full tri-state health story (ok / degraded / uncertain `?`) plus element
   grouping. The grid + charts shots keep using `kitchen-sink.json` (it
   carries the metrics the chart stack needs).

## App routing change

- `/` → `TopologyPage` (was `OverviewPage`).
- Fleet grid moves to `/hosts` (name matches what the page shows; "overview"
  no longer describes a non-landing page). `/host/:id` subject pages are
  unchanged.
- `/topology` and `/topology/:elementId` remain valid — the drill-down URL
  scheme is unchanged and plain `/topology` stays as a working alias, which
  also keeps existing bookmarks and most e2e navigation working.
- In-app links updated: the view switcher currently living on OverviewPage
  (navigates to `/topology`) gets its mirror on the topology side pointing at
  `/hosts`; SubjectPage's "Fleet" breadcrumb (`/`) → `/hosts`; TopologyPage's
  own breadcrumbs keep their `/topology` targets (the alias).
- E2e fallout, updated in the same change: tests that import a fixture at `/`
  and expect the grid (e.g. `test_review_shell`'s host-tile waits) now either
  navigate to `#/hosts` first or assert the topology landing, whichever the
  test is actually about. The import front door itself is route-independent.

## Fixture touch-up (`scripts/gen_monitor_fixtures.py`)

- `isp-core` gains: one **degraded** tunnel (`degraded (n/m)` badge), keeps
  its **uncertain** tunnel, and gains ~2 chassis `elements` grouping a few
  hosts (element grouping + drill-down affordance visible in the shot).
- Additive only; regenerated via the script (fixtures are never hand-edited)
  and committed. Tests that pin isp-core contents
  (`test_topology_tunnels`, `test_topology_budget`, `test_review_shell`,
  `test_gen_monitor_fixtures`) get their pinned counts adjusted knowingly —
  the budget test's node/edge counts are expected to move.

## Docs media capture (`scripts/capture_docs_media.py`)

- New artifact `dashboard-topology.png` added to `ARTIFACTS`: fresh page,
  import `isp-core.json`, land on `/` (now topology), wait for React Flow to
  have *rendered edges* (RF withholds edges until nodes are measured — wait
  on an edge path element, not just node presence), screenshot.
- The grid capture navigates to `#/hosts` after its kitchen-sink import;
  otherwise unchanged. Charts capture unchanged.
- `web/fixtures/isp-core.json` joins `_STAMP_INPUTS`.
- `docs/guide/monitor.md`: hero image (line 8) → `dashboard-topology.png`
  with alt text describing the topology map; grid image moves into the
  "Web dashboard" section; "Topology view" section wording updated to say
  topology is the landing view (`/`, drill-down at `/topology/<element>`),
  and the `--help`/route mentions swept for staleness.

## Verification

- `nox -s dashboard` (full three-engine matrix — bare pytest runs Chromium
  only and must not be called green) after the route swap and fixture regen.
- `make docs` regenerates media from a rebuilt dist; confirm
  `dashboard-topology.png` is a real render (non-placeholder, nonzero
  topology nodes visible) and monitor.md references resolve (clean Sphinx
  build — incremental `-W` misses broken refs).
- Grid-move regression: `/hosts` shows the fleet grid, `/` shows topology,
  subject-page breadcrumb round-trips through `/hosts`.
- Fixture diff reviewed as generator-output-only (no hand edits); budget
  test's new pinned counts justified in its comment.
