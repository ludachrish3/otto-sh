# Measurement pipelines: monitor and coverage

Two subsystems observe the lab rather than command it. Both are pipelines
with pluggable stages, and both are careful about the same two hazards:
polluting the logs they observe through, and writing databases onto
filesystems that can't take them.

## The monitor

The monitor answers "what are the hosts doing while my tests run?".

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
({doc}`results-and-logging`).

**Events.** Suites stamp markers onto the same timeline
(`start_monitor` / `add_monitor_event` from {class}`~otto.suite.suite.OttoSuite`),
so "CPU spiked" and "test_load started" correlate.

**Serving and persistence.** A live dashboard
({class}`~otto.monitor.server.MonitorServer`) binds an OS-assigned port and
serves the collector's buffer. With `--db`, samples persist to SQLite — WAL
journaling on local disks, DELETE on network filesystems
({doc}`data-boundary`) — and `--file` replays a saved run without touching
any host.

## The coverage pipeline

The problem: embedded and cross-compiled products execute where no coverage
tooling runs. gcov counters (`.gcda`) accumulate on the target — in memory or
on an on-device filesystem — while the compile-time graph (`.gcno`) and
sources live in the build tree on the runner. Neither side alone can make a
report.

Coverage is organized into **tiers** — `system` (e2e), `unit`, `manual`, or
any other name — declared in `.otto/settings.toml` under `[coverage.tiers]`
with a `kind` (`e2e` / `unit` / `manual`) that selects how otto collects that
tier's data. Only the **manual** tier's data is pinned and committed into the
repo; e2e data lives in each test run's output directory and unit data is
harvested fresh from the build tree at report time.

The pipeline has two entry points, both under `otto.coverage`:

1. **Get** (`otto cov get`, also invoked by `otto test --cov`) — pulls
   `.gcda` counters from each covered host (`fetcher`: file transfer for
   Unix hosts, console extraction for embedded targets — which hosts are
   covered is *repo-declared* via the `[coverage].hosts` regex, never
   inferred), correlates them against the build tree's `.gcno` graph
   (`correlator`), and writes one pinned `capture.json` per board
   (`capture.produce`) under `<output_dir>/cov/<board_id>/` — alongside the
   retrieved `.gcda` and the toolchain's `.gcov`/`.info` intermediates, kept
   as debug artifacts. A capture records line/branch hits in
   **committed-code coordinates**: the commit (`pin`) and, per file, the git
   blob SHA the numbering means. Selecting a manual-kind tier additionally
   copies the capture into the repo's committed store at
   `.otto/coverage/manual/` — proof of a manual test session that travels
   with the code and is PR-reviewable.
2. **Report** (`otto cov report`) — assembles a store from three sources per
   tier `kind`: e2e captures from the given output directories (behind a
   strict pin guard — the capture's `pin` must equal HEAD, mirroring the
   `.gcno`/`.gcda` stamp-mismatch check below), unit `.gcda` harvested fresh
   from each unit tier's configured `harvest_dirs`, and every manual capture
   committed under the repo's `.otto/coverage/manual/` store (loaded
   automatically, no path needed). A report-time **validity pass**
   (`validity`) anchors each manual capture's lines against the current tree
   by git blob SHA — unchanged lines stay **valid**, changed/deleted lines go
   **stale**, and valid-but-old lines (past the tier's `max_age`) are flagged
   **aging** without losing coverage credit. The renderer then produces the
   HTML report plus summary tiers, colored per tier and state.

The correlator's core invariant is *build/counter identity*: `.gcda` files
are only meaningful against the exact `.gcno` graph the binary was compiled
with. When they disagree — a stale or partially rebuilt product tree — the
pipeline stops with a diagnostic error that names the mismatch and the
rebuild that fixes it, rather than a gcov stack trace or a silently wrong
report. That fail-with-instructions posture is a house rule
({doc}`principles`).
