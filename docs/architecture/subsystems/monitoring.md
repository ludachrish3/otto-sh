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
reloading. See {doc}`../../guide/monitor` for the frontend dev workflow
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
    events [label="suite events\nstart_monitor / add_monitor_event", style=dashed];
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

**Events.** Suites stamp markers onto the same timeline
(`start_monitor` / `add_monitor_event` from {class}`~otto.suite.suite.OttoSuite`),
so "CPU spiked" and "test_load started" correlate.

**Serving and persistence.** A live dashboard
({class}`~otto.monitor.server.MonitorServer`) binds an OS-assigned port and
serves the collector's buffer to the built React frontend: an initial
`GET /api/monitor_sessions` snapshot (the same `format:1` shape review mode
loads) plus a live `GET /api/stream` SSE feed of `format:1`-shaped fragments
that grows an already-open tab in real time. With `--db`, each run's
samples persist as one session in a SQLite archive — WAL journaling on
local disks, DELETE on network filesystems
({doc}`../subsystems/data-boundary`); running against the same `--db` path
again appends another session rather than overwriting the archive. The
positional `otto monitor <source>` form instead replays a saved `.json`
export or `.db` archive without touching any host. See
{doc}`../../guide/monitor` for the flag-level workflow — `--live`, `--db`,
`--label`/`--note`, and the review-mode `<SOURCE>` argument.

**Gating.** `otto monitor` gates itself per branch rather than in the
preamble: live collection (`--live`) runs the reservation gate; reviewing a
saved `<source>` reads a local file and is gate-exempt by design
({doc}`../lifecycle`).

## Where the code lives

- {mod}`otto.monitor.factory` — `build_monitor_collector`, `MonitorTarget`,
  and the shell-vs-SNMP mode selection
- {mod}`otto.monitor.collector` — `MetricCollector` and the concurrent tick
  loop
- {mod}`otto.monitor.store`, {mod}`otto.monitor.db`, {mod}`otto.monitor.broadcast`
  — the collector's in-memory buffer, SQLite persistence, and live fan-out
- {mod}`otto.monitor.session` — a run's identity and lab snapshot
- {mod}`otto.monitor.export` — the `format:1` producer shared by review and
  live hydration
- {mod}`otto.monitor.server` — `MonitorServer`, the dashboard's HTTP/SSE
  surface
- {mod}`otto.monitor.parsers` — `MetricParser`, `DEFAULT_PARSERS`, and the
  `HOST_PARSERS` registry
