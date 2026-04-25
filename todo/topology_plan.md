# Network Topology Visualization — Design Plan

## Context

Otto needs a graphical view of the lab's network topology: hosts grouped by NE, connections between NEs showing protocols on hover, and the ability to click a host node to select it for monitoring. This replaces the current host ID dropdown in the monitor dashboard with a richer, always-accessible topology sidebar.

**Decisions made:**

- **Placement:** Integrated into the existing `otto monitor` dashboard as a collapsible left sidebar
- **Library:** Cytoscape.js (~500KB, bundled locally for air-gap compatibility)
- **Layout:** Collapsible sidebar (~30% width when expanded, thin strip when collapsed)

## Data Model

The topology graph is derived from existing `RemoteHost` fields — no schema changes needed:

- **Nodes:** One per host. Properties: `id`, `name`, `ne`, `neId`, `board`, `slot`, `term`, `transfer`
- **NE groups:** Two cases:
  - **Compound NE:** When multiple hosts share the same `ne` + `neId`, they are grouped under a compound/parent node. Hosts within the group are sorted by `slot` number.
  - **Standalone NE:** When an NE has only a single host with no board/slot composition, it renders as a simple node (no compound wrapper). This represents a standalone device with no internal structure.
- **Edges:** Derived from `hop` field (host A hops through host B → bidirectional edge between A and B). Edge label = `term` protocol (ssh/telnet). Hosts with no `hop` are implicitly connected to otto (the root/management node).
- **Future:** NE-to-NE connections for netem (not in scope now, but the graph structure supports it)

## API Changes

Add a new endpoint to `MonitorServer` (`src/otto/monitor/server.py`):

```json
GET /api/topology → {
  "nodes": [{ "id": "", "name": "", "ne": "", "neId": 0, "board": "", "slot": 0, "term": "", "transfer": "" }],
  "groups": [{ "id": "ne:router:1", "ne": "", "neId": 0, "label": "" }],
  "edges": [{ "source": "", "target": "", "protocol": "" }]
}
```

Build the topology JSON from the collector's host list. The collector already holds `MonitorTarget` objects which contain `RemoteHost` references.

## Frontend Changes

### New files

- `src/otto/monitor/static/cytoscape.min.js` — Bundled Cytoscape.js library
- `src/otto/monitor/static/topology.js` — Topology panel logic
- `src/otto/monitor/static/topology.css` — Sidebar styling

### Modified files

- `src/otto/monitor/static/dashboard.html` — Add sidebar container, load new scripts
- `src/otto/monitor/static/dashboard.js` — Wire sidebar toggle, replace dropdown host selection with topology node click events
- `src/otto/monitor/static/dashboard.css` — Adjust main content area to accommodate sidebar
- `src/otto/monitor/server.py` — Add `/api/topology` endpoint

### Sidebar behavior

1. **Expanded (default on load):** ~30% viewport width (adjustable via a drag handle on the sidebar's right edge). Shows full Cytoscape graph with NE compound nodes, host nodes inside, and hop edges between them.
2. **Collapsed:** Thin vertical strip (~40px) with an expand button. Metrics area fills full width.
3. **Toggle:** Button in the sidebar header or the toolbar.
4. **Host selection:** Clicking a node highlights it and triggers the same host-selection logic as the current dropdown (updates charts, page title, etc.). The dropdown is replaced by the topology view.
5. **Hover on edges:** Tooltip showing the protocol (ssh/telnet) and any relevant connection info.
6. **Selected host indicator:** The currently-monitored host node gets a distinct style (border highlight, color change).

### Cytoscape layout

- Use `compound` nodes for NE groups
- Layout algorithm: `cose` (force-directed, built-in). Network connections are bidirectional so a force-directed layout is the natural fit. Otto acts as an implicit root/anchor for hosts with no `hop` defined, but this is a layout hint, not a directional relationship.
- Style host nodes by type/status, NE group boxes with labels

## Implementation Steps

1. **Download and bundle Cytoscape.js** into `src/otto/monitor/static/cytoscape.min.js`
2. **Add `/api/topology` endpoint** in `server.py` — build graph JSON from collector's targets
3. **Create `topology.js`** — initialize Cytoscape instance, fetch `/api/topology`, render graph, handle click/hover events, emit host-selection events
4. **Create `topology.css`** — sidebar layout, collapse/expand transitions, responsive sizing, drag handle for width adjustment
5. **Modify `dashboard.html`** — add sidebar container div, script/CSS includes, restructure main content wrapper
6. **Modify `dashboard.js`** — replace host dropdown with topology-driven selection, listen for topology selection events, handle sidebar collapse/expand state
7. **Modify `dashboard.css`** — flex layout changes for sidebar + main content coexistence

## Verification

1. **Unit tests:** Test the `/api/topology` endpoint returns correct node/edge structure for a mock lab configuration
2. **Manual testing:**
   - Launch `otto --lab <test_lab> monitor` and verify the sidebar renders with correct NE groupings
   - Click host nodes and confirm metric charts update
   - Hover over edges and confirm protocol tooltip appears
   - Collapse/expand sidebar and verify layout transitions smoothly
   - Verify the old host dropdown is removed and the topology is the sole host selector
   - Drag the sidebar resize handle and confirm width adjusts smoothly
3. **Air-gap check:** Verify no external network requests are made (browser dev tools network tab)
