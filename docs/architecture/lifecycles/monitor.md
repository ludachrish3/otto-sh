# otto monitor — the observation pipeline

The monitor answers "what are the hosts doing while my tests run?" — a
pipeline that observes the lab rather than commanding it, careful about two
hazards: polluting the logs it observes through, and writing a database onto
a filesystem that can't take it.

```{admonition} Revamp in flight — treat details below as provisional
:class: warning

The monitor backend is being reworked (see the monitor-revamp roadmap in
`docs/superpowers/`). What this page says is true of the code **today**, but
the roadmap moves pieces around: the collector decomposes into store /
database / broadcast / history modules (the WAL-vs-DELETE choice moves into
a dedicated `MetricDB`), per-parser polling intervals replace the single
"one gather per tick" loop, dashboard metadata becomes typed
`TabSpec`/`ChartSpec` models served at `/api/meta`, and a project-level
`register_parsers()` joins the per-host registry. Update this page as those
land; the *shape* — targets, parsers-or-SNMP, silenced polling, dashboard,
replay — is expected to survive.
```

```{graphviz}
digraph monitor {
    rankdir=LR;
    node [shape=box];

    hosts [label="lab hosts\n(Unix or SNMP-enabled)"];
    factory [label="factory\nMonitorTarget per host\nshell parsers or SNMP source\nhost.log → NEVER"];
    collector [label="collector tick loop\nconcurrent poll, one shared\ntimestamp per tick"];
    events [label="suite events\nstart_monitor / add_monitor_event", style=dashed];
    db [label="SQLite (--db)\nWAL local / DELETE on network FS"];
    dash [label="web dashboard\nOS-assigned port"];
    replay [label="--file replay\n(no hosts touched)", style=dashed];

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
  command whose output it parses (`/proc/stat`, `/proc/meminfo`, …).
  Per-host parser sets are pluggable via the `HOST_PARSERS` registry.
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
serves the collector's buffer. With `--db`, samples persist to SQLite — WAL
journaling on local disks, DELETE on network filesystems
({doc}`../subsystems/data-boundary`) — and `--file` replays a saved run
without touching any host.

**Gating.** `otto monitor` gates itself per branch rather than in the
preamble: live collection runs the reservation gate; `--file` replay reads a
local file and is gate-exempt by design ({doc}`index`).
