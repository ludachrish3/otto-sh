# Monitoring — the observation pipeline

The monitor answers "what are the hosts doing while my tests run?" — a
pipeline that observes the lab rather than commanding it, careful about two
hazards: polluting the logs it observes through, and writing a database onto
a filesystem that can't take it.

```{admonition} Frontend: React, not vanilla JS
:class: note

The monitor backend was reworked behind a stable
{class}`~otto.monitor.collector.MetricCollector` facade: the collector
decomposes into `store`/`db`/`broadcast` modules — joined by `session` (a
run's identity and lab snapshot) and `export` (the `format:1` producer) —
dashboard metadata is typed `TabSpec`/`ChartSpec` models — each chart
carrying an optional `max_series` cap (default 8 series; `None` uncaps it,
as the CPU chart does) — reshaped into each session's `SessionMeta` for the
`GET /api/monitor_sessions`/`GET /api/stream` wire (spec 2026-07-12
monitor-live-streaming), and a project-level `register_parsers()` joins the
per-host registry. The dashboard itself was ported from a single vanilla-JS
file to a React + Vite
+ TypeScript single-page app (`web/`, built to `static/dist/`) behind the
same observable surface, and later gained a real live producer: both live
and review boot through the same `/api/monitor_sessions` endpoint, and a
live tab keeps growing via `/api/stream` SSE fragments rather than
reloading. See {doc}`../../contributing` for the frontend dev workflow
(`make web-dev`) — `tests/e2e/monitor/dashboard/` pins the exact
ids/classes/behaviors that must survive any further change to either side.
```

```{graphviz}
digraph monitor {
    rankdir=LR;
    node [shape=box];

    hosts [label="lab hosts\n(Unix or SNMP-enabled)"];
    factory [label="factory\nMonitorTarget per host\nshell parsers or SNMP source\nhost.log → NEVER"];
    collector [label="collector tick loop\nconcurrent poll, one shared\ntimestamp per tick"];
    events [label="test events\nmonitor fixture · per-test marks", style=dashed];
    db [label="SQLite session archive (--db)\nWAL local / DELETE on network FS"];
    dash [label="web dashboard\nOS-assigned port"];
    replay [label="otto monitor <source>\nreview (no hosts touched)", style=dashed];

    hosts -> factory -> collector;
    events -> collector;
    collector -> db;
    collector -> dash;
    replay -> dash;
}
```

**One library.** `otto monitor`, `otto test --monitor` and the `monitor`
fixture all go through {class}`~otto.monitor.session.MonitorSession`, which
owns a run's identity, lab snapshot, collector and archive; the CLI leaf
modules only parse, call the library and translate its errors to exit codes. {func}`~otto.monitor.live.run_live`
is the lab-aware `otto monitor --live`, {func}`~otto.monitor.review.serve_review`
the review path, and {func}`~otto.monitor.tls.resolve_monitor_tls` the TLS
resolution. The package quick start is the worked example.

**Collection.** `build_monitor_collector` (`otto/monitor/factory.py`) turns a
host list into `MonitorTarget`s and picks each host's collection mode:

- **Shell mode** — the target pairs the host with a dict of
  {class}`~otto.monitor.parsers.MetricParser` objects, each keyed by the
  command whose output it parses (`/proc/stat`, `/proc/meminfo`, …). One
  command can feed more than one series: the built-in
  {class}`~otto.monitor.parsers.PerCoreCpuParser` (`cat /proc/stat`) emits
  `Overall CPU` from the aggregate line plus `core <N>` per CPU core, all
  onto one `"CPU"` chart. Per-host parser sets are pluggable via the
  `HOST_PARSERS` registry.
- **SNMP mode** — hosts with an `snmp` table in lab data are polled by OID
  instead; SNMP metric descriptors have their own registry. This is how
  non-shell targets (and embedded targets that can't afford console polling)
  stay observable.

Only Unix hosts and SNMP-enabled hosts are monitored — there is no generic
way to poll an arbitrary console target without disturbing it.

**The tick loop.** {class}`~otto.monitor.collector.MetricCollector` polls
every target concurrently each tick (one `asyncio.gather` per tick, one
shared timestamp per tick so cross-host data lines up). The factory sets each
polled host's log disposition to `NEVER` — a monitor sampling ten hosts every
two seconds would otherwise bury the transcript; because LogMode gates
command I/O only, real errors from those hosts still surface
({doc}`../utilities/logging`).

**The interval floor.** Every human-facing interval rejects values below one
second ({doc}`../../cli/monitor/live`): a host needs time to answer every
query in the interval without being taxed by the polling itself.
`MetricCollector` is deliberately exempt — it is the mechanism, not a knob a
human sets, and otto's own tests drive it as fast as 0.01 s against fake
hosts.

**Events.** Tests stamp markers onto the same timeline, so "CPU spiked" and
"test_load started" correlate. Under `otto test --monitor`, every test, class
or plain function, gets a start and an end mark automatically; a test marks
its own moments with `monitor.event(...)` through the `monitor` fixture
({class}`~otto.suite.monitor_fixture.MonitorHandle`), which can also start a
collector of the test's own. Neither serves a dashboard; the events are read
back on review.

**Serving and persistence.** A live dashboard
({class}`~otto.monitor.server.MonitorServer`) binds an OS-assigned port and
serves the collector's buffer to the built React frontend: an initial `GET
/api/monitor_sessions` snapshot (the same `format:1` shape review mode loads)
plus a live `GET /api/stream` SSE feed of fragments that grows an
already-open tab in real time. A fragment is a partial session record in the
export's vocabulary, but the stream is a versioned format of its own,
`monitor-live-stream` in `api/public.toml`, numbered apart from the export
document's `format`. The server stamps its own clock as `observed_at` on every
fragment, on each session of the live snapshot and, as the
`X-Otto-Observed-At` header, on every live event-mutation response (review mode,
an archive with no live window, sends none). One clock feeds them all:
`otto.monitor.event_ops.utc_now`, which also stamps a Mark-now timestamp and a
Stop; `otto.monitor.collector.observed_now` is its ISO string. The dashboard's live
window ends at the later of that time and the last metric, so a new mark shows
at once even while the collector is stalled; health still reads the last
metric alone. Because the dashboard boots from that
snapshot fetch in live and review mode alike, it also runs from a bare static
file server with no `/api/*` routes (the docs screenshots and ad-hoc demos):
any failure falls back to the empty Import screen. With `--db`, each run's
samples persist as one session in a SQLite archive — WAL journaling on local
disks, DELETE on network filesystems ({doc}`data-boundary`); running against
the same `--db` path again appends another session rather than overwriting the
archive. The positional `otto monitor <source>` form instead replays a saved
`.json` export or `.db` archive without touching any host. See
{doc}`../../cli/monitor/index` for the flag-level workflow — `--live`, `--db`,
`--label`/`--note`, and the review-mode `<SOURCE>` argument.

**Gating.** `otto monitor` gates itself per branch rather than in the
preamble: live collection (`--live`) runs the reservation gate; reviewing a
saved `<source>` reads a local file and is gate-exempt by design
({doc}`../lifecycle`).

**Opening the archive, then collecting.** `MonitorSession.open()` (which
`async with session:` calls) awaits `init_db()`, and `spawn()` refuses until
it has, so the task that collects never exists before the archive does.
{meth}`~otto.monitor.collector.MetricCollector.run` **refuses** an unopened DB
too — it raises a loud precondition rather than opening lazily in-task. That
refusal is the fix for a five-issue flake wave (#136/#137/#142-#144): an
in-task open can be cancelled mid-schema, leaving a partial archive that
`finalize()` then silently no-ops on, and its failures die inside a task whose
supervising `gather(return_exceptions=True)` swallows them. The ordering used
to be a comment repeated at every call site; now one class owns it with the
precondition behind it, so a caller that bypasses it fails at once instead of
racing. Under `otto test --monitor` the session opens once, when the test
session starts, and a class-scoped fixture restarts `run()` for each test
class.

**Bounded live buffers.** Both live-side buffers have ceilings, because a
multi-day run previously grew without one. Each SSE subscriber queue is
bounded (`SUBSCRIBER_QUEUE_MAX`, 1024 payloads), so a tab that stops draining
— frozen renderer, half-dead connection — can no longer grow memory at the
live bed's ~90 fragments per tick; each
{class}`~otto.monitor.store.MetricStore` series is a `deque` capped at
`_SERIES_POINTS_MAX` (50,000 points, roughly 27 hours at two-second ticks).
What happens at the queue's bound is a **lapse, not a drop**: the queue is
emptied and handed a single {data}`~otto.monitor.broadcast.LAPSED` sentinel,
the `/api/stream` route ends that subscriber's response when it reads it,
and the browser's `EventSource` reports the close as `onerror` — which runs
the client's existing resync-then-reopen path (`web/src/data/stream.ts`),
re-hydrating from `GET /api/monitor_sessions`. The first cut dropped the
oldest frame instead, on the argument that the reconnect resync would
recover the gap; an eviction never causes a reconnect, so nothing ever
resynced, and a tab that lost the once-emitted `chart_map` frame drew
ungrouped charts until reload. A gap is now announced, then recovered by
resync, never replayed. The cost is deliberate: a tab that is alive but
sustainedly slower than the stream lapses, resyncs and lapses again rather
than silently showing stale data. Only the live view and live exports age
out — the DB keeps everything, so a review opened from a `.db` archive is
always complete.

**Event changes are ordered by the server's stamp.** An event can reach a
tab by three routes that race: the HTTP response to the tab's own mutation,
the SSE echo of that mutation or of another actor's, and a bootstrap or
resync snapshot. Every one of them lands through the one merge path,
`applyFragment` (`web/src/data/fragment.ts`), and its `admitEventChanges`
decides, per event id, whether the change is newer than what the session
holds. The order is the server's own: every SSE fragment, every live
mutation response (the `X-Otto-Observed-At` header) and every snapshot
carries an `observed_at` from `event_ops.utc_now`, which is strictly
increasing at microsecond resolution, and each stamp is read in the same
synchronous step as the state it describes (the collector writes an event to
the database first, then changes memory and publishes). The session keeps
the stamp of the last change it applied to each id, deletions included, so
a deleted id keeps a tombstone; a snapshot seeds every id it carries and
sets the session's floor from its own `observed_at`. An upsert or a deletion
applies only when its stamp is strictly later than both the floor and that
id's stamp: a late echo never rolls an event back, in either direction, and
a delayed response never resurrects an event a later snapshot showed
deleted. A mutation's response stamp is read after its publish stamp, so the
tab's own echo is always older than the response it already applied and is
dropped; its content is identical. Review mode has no stamps, so every change
applies there. A resync is one caller of this rule: its snapshot is captured
somewhere between request and response, so the review store logs the event
changes applied meanwhile (`beginResync` in `web/src/data/reviewStore.ts`)
and replays them onto the snapshot through `applyFragment`, which keeps only
those ordered after the capture. A session's server-observed horizon never
moves back. Undo leans on this ordering (spec
`docs/superpowers/specs/2026-11-07-event-undo-design.md`, section 4); what
undo does for a user is in [the dashboard guide](../../cli/monitor/dashboard.md#undo-and-redo).

**Streams end when the server drains.** An open `/api/stream` response
never finishes on its own, and sse_starlette notices a shutdown only through
uvicorn's signal handlers, which the server removes so that otto's lifecycle
owns Ctrl-C. So the server ends its own streams: the moment its graceful
drain begins (`stop()`, or the first Ctrl-C cancelling `serve()`), it refuses
new stream requests with `503` and hands every open subscriber queue the
`otto.monitor.broadcast.ENDED` sentinel, through the same queue-terminal
helper the lapse uses. The responses end, uvicorn closes the connections, and
one Ctrl-C with a dashboard tab open exits at once instead of waiting out the
teardown deadline (#784). A second Ctrl-C still abandons whatever is in
flight.

## The dashboard

**Dashboard view state.** "Paused" is *derived* rather than a separately
stored flag — it is exactly "live mode with a pinned range" — so pausing and
picking a custom range are one state and cannot disagree. The live health
clock ticks at the collection interval, since polling health faster than the
collector could not learn anything sooner. The topology Key panel draws from the
same style tables as the canvas, so the legend cannot drift from what is on
screen.

**Chart gestures.** Pan is Ctrl-drag on every platform because ECharts'
drag-modifier vocabulary has no meta key; the `+`/`-` buttons feed the same
path a drag-select does, so the two cannot disagree about the current window.

## Where the code lives

- {mod}`otto.monitor.factory` — `build_monitor_collector`, `MonitorTarget`,
  and the shell-vs-SNMP mode selection
- {mod}`otto.monitor.collector` — `MetricCollector` and the concurrent tick
  loop
- {mod}`otto.monitor.store`, {mod}`otto.monitor.db`, {mod}`otto.monitor.broadcast`
  — the collector's in-memory buffer, SQLite persistence, and live fan-out
- {mod}`otto.monitor.session` — `MonitorSession`, a run's identity, lab
  snapshot, collector and archive
- {mod}`otto.monitor.live`, {mod}`otto.monitor.review`,
  {mod}`otto.monitor.tls` — the lab-aware live run, the review server and the
  dashboard's TLS resolution, shared by every producer
- {mod}`otto.monitor.errors` — the library's error taxonomy
- {mod}`otto.monitor.export` — the `format:1` producer shared by review and
  live hydration
- {mod}`otto.monitor.server` — `MonitorServer`, the dashboard's HTTP/SSE
  surface
- {mod}`otto.monitor.parsers` — `MetricParser`, `DEFAULT_PARSERS`, and the
  `HOST_PARSERS` registry
