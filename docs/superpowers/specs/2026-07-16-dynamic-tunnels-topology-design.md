# Dynamic tunnels in the monitor topology view

**Date:** 2026-07-16
**Status:** approved design, awaiting implementation plan
**Closes the gap:** the web renders a `dynamic` (tunnel) edge class end-to-end
(styling, casing, legend, measurement bucket), but no producer ever emits one —
`snapshot_lab()` (`src/otto/monitor/session.py:106`) is hard-wired to
`implicit + declared`, and live tunnel state (`discover_tunnels()`,
`src/otto/tunnel/discovery.py`) is only ever consumed by `otto tunnel list` and
the add-conflict check. The only `dynamic` emitter today is the fixture
generator. This is roadmap item #6's monitor seam (`todo/link.md:51`) and it
delivers `todo/monitor-topology-followups.md` item 7 (tunnels as overlays on
their underlay links) in full.

## Decisions (from brainstorming, 2026-07-16)

1. **Scope: path in schema, overlay render.** The format:1 change exports each
   tunnel's ordered hop path, and the web renders the tunnel as styling along
   the links that path traverses — item 7 in full, NOT a single
   endpoint-to-endpoint chord. Each consecutive hop-pair rides an existing
   underlay link where one exists and gets a bare segment where none does.
2. **Liveness: live layer over SSE.** Tunnel discovery runs on the collector's
   existing interval tick and streams changes as session fragments. No
   snapshot-freeze, no manual refresh.
3. **Discovery scans the whole lab**, not the monitored subset. Stats gathering
   may target a few hosts; tunnels can traverse hosts outside that set. The
   composition site passes the full lab to discovery, independent of
   `--host`-style selection.
4. **Wire shape: first-class `tunnels` field** on the session, not dynamic
   `LinkSnapshot`s. `LabSnapshot` keeps its static-only invariant.
5. **Health is visible:** ok / degraded / uncertain styling variants on the
   tunnel edge class; full detail in the link inspector.
6. **Exported data carries last known state only** — the current tunnel set,
   overwritten in place. No timeline, no per-tick history.
7. **The poller lives in the `Collector`** as a loop sibling of
   `_bucket_loop`, fed by an injected discovery callable.

## 1. Wire contract (format:1)

New model in `src/otto/models/monitor.py`:

```python
class TunnelRecord(RowModel):
    id: str                    # tun-<12hex>-<port> (make_tunnel_id)
    protocol: str              # Tunnel.protocol
    service_port: int
    hops: list[str]            # ordered host ids along Tunnel.path, len >= 2
    status: Literal["ok", "degraded", "uncertain"]
    carriers_present: int      # len(DiscoveredTunnel.present)
    carriers_expected: int     # Tunnel.expected_processes() count
    age_seconds: float | None  # oldest observed carrier age
```

- `hops[0]` is the entry end, `hops[-1]` the exit end. The overlay consumes
  consecutive `(hops[i], hops[i+1])` pairs. Hops use the same host identifiers
  as `LinkSnapshot.endpoints`, so the web resolves them through the exact
  host→node mapping links already use.
- `status` is derived from `DiscoveredTunnel`'s fields (`uncertain` →
  `"uncertain"`, else `missing` non-empty → `"degraded"`, else `"ok"`) — never
  by parsing the human `status()` string.

`SessionRecord` gains `tunnels: list[TunnelRecord] = []`.
`MonitorSessionFragment` gains `tunnels: list[TunnelRecord] | None = None` with
**replace semantics**: `None` means "no tunnel update in this fragment"; a list
(including `[]`) replaces the session's set wholesale. This follows the
`meta: SessionMeta | None` precedent rather than the append rule — replace IS
"last known state" expressed on the wire.

**`"dynamic"` leaves `LinkSnapshot.provenance`.** Its literal becomes
`Literal["implicit", "declared"]`. Fixtures were the only writer (the docstring
already guaranteed real exporters never emit it); their `tun-*` links migrate
to `tunnels` records. The runtime `Provenance.DYNAMIC` enum
(`src/otto/link/model.py`) stays — the link-conflict rules use it. End state:
`LinkSnapshot` = static config, `tunnels` = runtime state, no overlap.

Ripple (moves in one commit, as every format:1 change): jsonschema assembly
(`src/otto/models/jsonschema.py::_monitor_export_schema`),
`scripts/gen_web_types.sh` regen of `web/src/api/export.gen.ts`, the `make web`
drift diff, `scripts/gen_monitor_fixtures.py`, committed `web/fixtures/*.json`,
and the drift tests (`tests/unit/models/test_jsonschema.py`,
`tests/unit/scripts/test_monitor_fixture_files.py`).

## 2. Producer: collector tunnel loop

`Collector.__init__` gains one optional argument:

```python
tunnel_source: Callable[[], Awaitable[list[TunnelRecord]]] | None = None
```

The monitor package never imports `otto.tunnel`. The adapter
(`DiscoveredTunnel` → `TunnelRecord`) lives in the tunnel package (which may
import `otto.models.monitor`); `src/otto/cli/monitor.py` composes the callable
over the **full lab** (decision 3). When `tunnel_source` is `None` (suite,
plugin, tests that don't care), no loop is spawned and nothing changes.

`Collector.run()` spawns `_tunnel_loop` beside the `_bucket_loop`s, using the
same cadence primitive: `await asyncio.gather(asyncio.sleep(interval),
self._tunnel_pass())` — the period is `max(interval, scan_time)`, so passes
serialize by construction; no overlap guard needed. Interval =
`self._global_interval` (decision 2: the existing tick).

Each pass:

1. Await the source. **A failed scan publishes nothing and writes nothing** —
   last known state stands. Failure reaches the loop as an exception: the
   callable returns a list only for a scan that genuinely ran, and the adapter
   raises when discovery could reach no host at all (all-unreachable is a
   failed scan, not an empty lab). Never emit `[]` because the scan failed;
   only a *successful* scan that found no tunnels may empty the set (guard
   what you emit — the 5b lesson). The loop logs a warning on the first
   failure, a warning again on recovery, debug in between (no tick-spam);
   warnings render on stderr under the three-sink rules, a named render site.
2. Partially-unreachable hosts do not blank anything: discovery already marks
   affected tunnels `uncertain`, and they flow through as such.
3. Normalize (sort by `id`) and compare to the last *published* set by full
   record equality — a status flip publishes, a mere re-observation does not.
   The comparison baseline starts at `[]` (a live session starts tunnel-less).
4. On change: upsert the db first, then publish
   `{"format": 1, "session": self.session_id, "tunnels": [...]}` through the
   existing `_publish`/`Broadcaster` path (db-first so a crash between the two
   can only make hydrate *fresher* than the stream, never staler).

## 3. Persistence

The `sessions` table gains a `tunnels_json TEXT NOT NULL DEFAULT '[]'` column,
upserted in place on change — the exact `write_chart_map` precedent
(`src/otto/monitor/db.py:309`). **No version bump**: the repo's convention is
no-migrations-ever; `chart_map_json` was added to v2 in place, pre-release,
with a column-presence check that refuses old shapes loudly (`db.py:110`).
`tunnels_json` gets the same treatment: schema stays v2, and the
column-presence check extends to name the missing column in its refusal.

`build_live_export` / `_session_record` (`src/otto/monitor/export.py`) read
the column back into `SessionRecord.tunnels`, so live hydrate
(`/api/monitor_sessions`) and db-mode sessions both carry the last known set —
parity for free, no timeline.

## 4. Web

- `export.gen.ts` regenerates with `TunnelRecord` / `SessionRecord.tunnels` /
  fragment `tunnels`; `normalizeSession` (`web/src/data/exportDoc.ts`) carries
  `tunnels` into the store session (default `[]`).
- `applyFragment` (`web/src/data/fragment.ts`) gets one rule:
  `frag.tunnels != null` → replace `session.tunnels` wholesale, in the
  new-session spread at `fragment.ts:133`.
- `buildTopoGraph` (`web/src/data/topology.ts:222`) takes the tunnel list as a
  new input and renders each tunnel as an **overlay along its hop path**, not
  an endpoint-to-endpoint chord. For each consecutive pair
  `(hops[i], hops[i + 1])`:
  - **Riding segment** — if an underlay link joins the pair, the tunnel is a
    tunnel-styled overlay stroke drawn along that edge's exact geometry; the
    underlay stays visible and independently clickable beneath it. When
    several links join the pair, preference is **declared over implicit**,
    then stable by link id. This mapping is schematic, not measured — we do
    not know which wire a socat leg actually crosses, and the spec claims no
    more than "rides the drawn link".
  - **Bare segment** — if no underlay joins the pair, a routed
    `provenance: "dynamic"` edge is drawn between the two nodes (today's
    tunnel style). A 2-hop tunnel with no underlay degenerates to exactly the
    old endpoint chord, so kitchen-sink's `tun-demo` keeps its current look.
  - Every segment carries the full record as `edge.tunnel`. **Selection is
    whole-tunnel**: clicking any segment selects the tunnel, highlights the
    entire path, and opens the tunnel inspector — segments are not entities.
  - Multiple tunnels riding one link fan apart with small perpendicular
    offsets, reusing the existing parallel-edge indexing.
  The now-dead `link.provenance === "dynamic"` branches (`topology.ts:304`,
  `:401`) are removed. A hop host absent from the lab snapshot cannot occur
  for real data (discovery is lab-scoped) — the mapping fails loud in dev
  builds rather than dropping a segment silently.
- **Layout is untouched and untouchable by tunnels.** Layering and coordinate
  assignment key on `declared` edges only (`web/src/topo/layout.ts:28`,
  `:354`); riding segments borrow underlay geometry and bare segments are
  `dynamic`, so tunnel churn never moves a node. This property gets an
  explicit e2e assertion (node positions unchanged across a tunnel add/remove
  fragment).
- **Health styling:** three variants of the single `tunnel` edge class in
  `web/src/topo/edgeStyles.ts` — `ok` = the shipped dashed-stroke-plus-casing,
  `degraded` = warning accent on the same geometry, `uncertain` = ghosted
  opacity — applied uniformly to every segment of a tunnel: one tunnel, one
  status. Resolve the colour *values* in both themes (the dark-mode-only
  collision lesson). The legend keeps one "tunnel" entry; status appears in
  the hover card subtitle (`web/src/topo/linkText.ts`) and the inspector.
- **Inspector:** `LinkInspector.tsx` gains a tunnel block (at the NetEm
  placeholder's position, `LinkInspector.tsx:112`) rendered when the selected
  edge carries `edge.tunnel`: status, carriers n/m, protocol, service port,
  age, and the hop chain in order.
- **Accepted quirk (stated, not fixed):** a host participating only in tunnels
  stays management-faded — `deriveManagementIds` (`topology.ts:149`) keys on
  declared links. The tunnel overlay is prominent regardless of node fade;
  revisit only if a faded hop host proves confusing in practice (see Out of
  scope).

## 5. Testing & fixtures

- `scripts/gen_monitor_fixtures.py`: migrate `tun-*` dynamic links to
  `tunnels` records; ensure vectors exist for all three statuses, and for a
  ≥3-hop tunnel whose path rides real declared/implicit links (isp-core or
  sprawl — kitchen-sink has no underlay to wrap; its `tun-demo` stays as the
  bare-segment vector).
- Python: `_tunnel_loop` unit tests against a fake source — change detection
  (status flip publishes, re-observation doesn't), failed-scan-keeps-state,
  successful-empty-scan publishes `[]` exactly once, db-first ordering;
  `tunnels_json` upsert/read-back; jsonschema shape tests.
- Web (vitest): fragment replace-merge including the `[]`-vs-absent
  distinction; hop-pair → riding/bare segment mapping (declared-over-implicit
  preference, bare fallback, 2-hop degenerate case); edgeStyles status
  variants.
- Dashboard e2e: riding segments follow their underlay geometry; bare-segment
  fallback renders; clicking any segment selects the whole tunnel and opens
  the inspector tunnel block; live SSE add/remove with node positions asserted
  unchanged. The overlay's hit target must be verified clickable on WebKit
  specifically (WebKit refuses to hit-test unpainted strokes — the React Flow
  interaction-path lesson). Browser gate is `nox -s dashboard` (all three
  engines), never bare pytest. Monitor e2e stays docker-free: discovery is
  faked, no real tunnels, no real socat.
- New guards are proven able to fail: mutate the production code (drop the
  change-detection, emit `[]` on failure) and confirm the guard goes red.

## 6. Sequencing & risks

- No blocking prerequisites: the topology layout redesign (`11068ca`) and the
  5b follow-ups / Untitled UI shell (`f9e9d6e`) are both on main; this design
  builds directly on them.
- Risk: the riding overlay needs a custom React Flow edge that renders along
  another edge's computed path. That geometry-sharing seam and its hit target
  are the newest web machinery in the design — prototype first in the plan,
  and give the hit target painted-stroke hit-testing from day one (WebKit).
- All format:1 pieces move together in one commit (schema, generated types,
  generator, fixtures, drift guards), per the standing rule.
- The `#139` discovery fixes (side-effect-free probing, docker never required
  or started at endpoints) are on main (`4b01cc6`) — the collector loop
  inherits them; discovery must never boot a stack on a peer.
- Risk: discovery cost on big labs — one `ps` scan per lab host per tick,
  concurrent, serialized per pass by the cadence primitive. If a scan
  consistently overruns the interval, the loop degrades to back-to-back passes
  (period = scan time), same as `_bucket_loop`; no pile-up is possible. A
  divisor knob (scan every Nth tick) is deliberately YAGNI'd until a real lab
  hurts.

## Out of scope

- Death ghosts (a vanished tunnel lingering visibly) — rejected in
  brainstorming; the map mirrors current truth.
- Tunnel history/timeline in the archive — decision 6, last known state only.
- Flipping tunnel-only hosts out of the management fade.
