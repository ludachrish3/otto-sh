# Web dashboard

In both modes, `otto monitor` serves a web server: it binds an OS-assigned
free port and logs the dashboard URL at startup (`Server running at
http://<ip>:<port>`, one URL per non-loopback interface).

On load, the dashboard shell asks that same server one question — `GET
/api/mode` — then, regardless of the answer, follows up with `GET
/api/monitor_sessions` and renders the result, exactly as if you'd used
Import yourself: no click needed. Live and review servers hydrate through
that *same* endpoint and the *same* `format:1` shape — a live monitor
session is simply one whose `end` is still open, exactly like a crashed
session found on disk — so the topology map populates immediately either
way (as do the fleet grid and charts once you switch to Hosts), not just
in review mode. In live mode, once that initial
hydrate succeeds the shell also opens `GET /api/stream` (Server-Sent
Events) and *grows* the loaded session in place by appending each fragment
as it arrives — the wire fragments carry the same field names as the
payload they append to, so there is no separate live shape to reconcile.
The same boot fetch is also why the dashboard still works when served by a
bare static file server with no `/api/*` routes at all (used for the
screenshots on this page, and for ad-hoc demos): any failure — connection
refused, a non-JSON body, whatever a dumb server hands back — is swallowed
and falls back to the same empty Import screen, never a broken page.

Feed it a monitor export document yourself at any time — drag a file onto
the window, or use the **⋯** overflow menu's *Import* — and it renders
that document entirely in the browser, exactly like a boot-fetched monitor
session:

- **Fleet grid.** Element-grouped host tiles, each with a status dot, an
  element-level health-rollup bar, and a labeled headline metric; a down
  tile shows its outage duration instead.

  ![The review dashboard's fleet grid: element-grouped host tiles with a
  status dot, a health-rollup bar per element, and a labeled headline
  metric](../../../_static/generated/dashboard-review.png)

- **Health, scoped to the viewed range.** Every status, rollup, and
  headline reflects whichever time window the review bar is currently
  showing — narrow the range and a host that's healthy across the full
  session can show down (or vice versa) if that's what the narrower
  window actually contains.
- **Per-subject charts.** Drilling into a host (or an element) stacks its
  metrics as synced chart panels — panning or zooming one follows the
  rest of the stack, so a spike is easy to correlate across series. See
  [Chart gestures](#chart-gestures) below for how to drive them.

  ![A subject page's synced chart stack: one panel per metric group and a
  kernel log table, all sharing one time axis with event
  markers](../../../_static/generated/dashboard-review-charts.png)

- **Series and source filtering.** A per-subject series tree toggles
  individual metrics on and off; chip filters narrow by metric group or
  by data source (a series' own host vs. an external management host —
  externally-sourced series carry a provenance badge).
- **Events.** A reverse-chronological slide-over lists every event in the
  loaded document; clicking a row re-scopes the review bar's range to
  that event's span (padded ±15 minutes) — or, if that padded range falls
  entirely outside the session, shows a notice and stays open rather than
  closing on a silent no-op. An editable session (see [Marking
  events](dashboard.md#marking-events)) adds marking controls to this same
  panel.
- **Multiple sessions.** A document spanning more than one session (a
  config change captured mid-run, or a `--db` archive several `--live --db`
  runs appended into, for example) exposes a session picker; each entry's
  tooltip is that session's `--label`/`--note`, and each session renders
  under the lab configuration it was captured under, so drift between
  sessions never bleeds into the wrong one's view.
- **Export.** The **⋯** menu re-downloads whatever document is currently
  loaded, unchanged.

Loading a session — automatically at boot in either mode, growing live via
SSE, or by hand via Import — is covered by the browser e2e suite
(`tests/e2e/monitor/dashboard/`, see the [behavior-spec
contract](../../../contributing.md#monitor-frontend-development)).

## Topology view

The topology map is the dashboard's landing view (`/`) — `/topology` remains
a working alias, so existing bookmarks and links keep resolving — with an
intra-element drill-down at `/topology/<element>`. It lays the lab out by its
data-plane structure rather than the management hop chain — see
{doc}`../../../architecture/subsystems/network` for the underlay/overlay model it
draws from. The inter-element map aggregates each element into one node;
opening an element expands it into its individual hosts, alongside the
`local` node for otto's own management path. The fleet grid — the other
view, reachable from the same switcher — lives at `/hosts`; see [Web
dashboard](dashboard.md#web-dashboard).

The bottom-left **Key** panel documents the canvas's two axes — link class
and health status — from the same style tables the canvas itself draws from,
so the legend can never drift from what's on screen. There are three link
classes:

- **static** — from the lab config: a declared link, a hop-derived one, or
  the `local` management star.
- **tunnel** — a live `otto tunnel`, drawn dashed with a wide casing sleeve
  (the only class drawn with one) so it reads as wrapped around a path
  rather than as a peer of the static links.
- **reports for** — metrics sourced from a management host rather than the
  subject itself.

**Tunnels are a live overlay**, not a snapshot. Each tunnel is drawn along
the links its hop path actually traverses: a consecutive pair of hops rides
its underlay link's exact geometry where one joins that pair, and gets a
plain routed segment between the two nodes where none does. Status styles
the whole tunnel uniformly, never per-segment — **ok** is the shipped
dashed-plus-casing stroke, **degraded** is a warning-accent variant of the
same geometry, and **uncertain** ghosts it down to a faint opacity. Clicking
any segment of a tunnel — riding or bare — selects the whole tunnel,
highlights every other segment of its path, and opens the tunnel block in
the link inspector: status, carriers (`present/expected`), protocol,
service port, age, and the ordered hop path.

Tunnel discovery runs on the collector's own collection interval and scans
the *whole lab*, independent of which hosts are actually monitored — a
tunnel between two otherwise-unpolled hosts still appears, on the same
cadence as every other metric tick.

## Chart gestures

Every per-subject chart (described under *Per-subject charts* above)
shares one gesture set, synced across the whole stack the same way panning
and zooming already are:

- **Drag** across a chart's plot area to zoom-select that range — release
  and the whole stack re-windows to exactly what you dragged.
- **Ctrl-drag** pans the current window instead of zooming, keeping its
  width fixed. This is **Ctrl** on every platform, not the app's usual
  per-platform modifier (⌘ on Mac, elsewhere Ctrl — see [Marking
  events](dashboard.md#marking-events)) — ECharts' own drag-modifier vocabulary
  has no meta key, so Ctrl is the one pan gesture available on every
  platform, and the guide follows the same choice rather than disagreeing
  with itself across platforms.
- **`+`/`-` buttons**, one pair per chart, step the zoom in or out around
  the window's current center — the same path a drag-select feeds, so a
  button click and a drag can never disagree about what "the current
  window" means.
- The **mouse wheel scrolls the page**, not the chart — it is never
  hijacked for zooming.

"Sweep span on chart" (see [Marking events](dashboard.md#marking-events)) reuses
the same drag gesture for a different purpose: while armed, the next drag
opens the event editor pre-filled with the dragged range instead of
zooming.

## Marking events

Any **editable** session — a live run, or a review opened from a `.db`
session archive (see [Reviewing a capture](review.md#reviewing-a-capture)) —
can mark events directly from the dashboard; a `.json` export has no
marking controls at all.

**From the app bar.** The **Mark now…** button (**⌘E** / **Ctrl E**) opens
a small popover for a label and stamps a point event at the current time.
Its dropdown adds:

- **Start span…** / **End span** — opens the same label popover to open a
  span, then closes it (server clock) once you choose **End span**; only
  one span can be open at a time.
- **Sweep span on chart** — arms a one-shot drag on the *next* chart you
  drag across: instead of zoom-selecting, that drag becomes the span's
  start and end and opens the full event editor pre-filled with it. Esc
  cancels the armed gesture before you drag.
- **Add event…** — opens the same editor with a blank draft, for typing
  exact times instead of dragging or marking "now".

The **Events** slide-over carries the same flows inline, so you never have
to leave it: a live session's compose row has **Mark**/**Start**/**Stop**
buttons; a review session (there's no "now" to stamp against) gets a
single **Add event…** button instead. Each row also grows **Edit** and
**End now** controls once the session is editable — **End now** stamps an
open span's end without opening the editor. Both disappear entirely (not
just disabled) on a read-only source, since there's nothing on the server
for them to persist to.

**The event editor**, opened from any of the flows above, has a **Label**,
second-granularity **Start**/**End** date-time fields (clearing **End**
turns a span back into a point event), a row of color swatches, and a
**Dash** style select (`solid`, `dot`, `dash`, `longdash`, `dashdot`,
`longdashdot`) applied to the event's chart overlay — a point event's marker
line and the border drawn around a span. Editing or
deleting an existing event reuses the same panel; deleting requires
pressing **Delete** twice ("Really delete?") before it takes effect.

**Live during a test run.** When a test uses `start_monitor()` (see
[Monitoring from test
suites](../../../library/custom-parsers.md#monitoring-from-test-suites)), both
the automatic per-test start/pass/fail marks and any `add_monitor_event`
call appear on that run's open dashboard the moment they're recorded —
the same live `/api/stream` feed the metrics ride — so there's no reload
needed to watch a test's marks land while it runs.

## Live status, pause, and reconnect

While `--live`, a healthy session shows no connection chrome at all — the
app bar stays quiet as long as the stream is open and receiving fragments.
If the SSE connection drops, a slim amber **Reconnecting…** banner appears
directly under the app bar for as long as the retry-with-backoff loop is
unresolved, and disappears the moment the stream reconnects. **Pause/Resume**
is the icon button in the app bar's right-hand cluster (its `aria-label`
reads "Pause" or "Resume" to match); review/historical context — including a
client-side Import with no backing server — is carried entirely by the
review bar's **HISTORICAL** badge, never by the app bar.

**Pause is a view control, not a data control.** Clicking **Pause** freezes
the visible time window; it does not stop ingestion — fragments keep
applying to the loaded session in the background, so clicking **Resume**
catches up immediately with no gap to backfill. "Paused" is *derived*
rather than a separately stored flag: it is exactly "live mode with a
pinned range," so pausing and manually picking a custom range (a chart
drag-zoom, for example) are the same state and can never disagree with each
other — toggling pause from either one resumes following the tail.

**Reconnect re-fetches; it never replays.** When the SSE connection drops,
the client backs off and retries, and immediately before reopening the
stream it re-fetches the whole `/api/monitor_sessions` payload rather than
trying to replay whatever fragments it missed while disconnected — the
fresh snapshot is already the truth, so there's no sequence-number
bookkeeping and no way for client and server to disagree about history.

**A silent host dims.** Health (see [Health, scoped to the viewed
range](dashboard.md#web-dashboard)) is derived from the gap since a host's last
sample: a host goes **down** once that gap exceeds `HEALTH_K` (3) times its
collection cadence. In live mode that evaluation runs against a moving
"now" rather than a fixed range boundary, driven by a clock that ticks at
the collection interval — polling the health check faster than the
collector itself couldn't learn anything sooner anyway.

