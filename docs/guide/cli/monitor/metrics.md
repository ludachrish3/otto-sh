# Metrics & data sources

What otto samples, and where the numbers come from: the built-in metrics
every Unix host reports, values otto reads out of files the host already
writes, and SNMP.

## Built-in metrics

Every Unix host in the monitored set runs `DEFAULT_PARSERS` unless a custom
registration says otherwise —
see [Custom parsers](../../../library/custom-parsers.md#custom-parsers).
Each chart draws at most `MetricParser.max_series` series at once — 8 by
default — beyond which the dashboard shows the first `max_series` and notes
how many were hidden; a parser can raise that cap or set `max_series = None`
to opt out entirely, as the CPU chart below does:

| Command | Series | Chart | Tab | Notes |
| --- | --- | --- | --- | --- |
| `cat /proc/stat` | Overall CPU; `core <N>` (%) per CPU core | CPU | CPU | One read yields both: the aggregate `cpu` line becomes `Overall CPU`, each `cpuN` line becomes `core N`. Every core is charted, however many the host has — the CPU chart opts out of the series cap other charts respect. |
| `free -b` | Memory Usage; Swap | Memory Usage | Memory | The Swap series only appears when the host has swap configured — it is omitted, not charted as a flat 0. |
| `df -h` | one series per mounted filesystem, labelled by mount point | Disk Usage | Disk | |
| `cat /proc/loadavg` | Load (1m), Load (5m), Load (15m) | Load | CPU | |
| `cat /proc/net/dev` | `rx <iface>`, `tx <iface>` (B/s) per interface | Network I/O | Network | Loopback (`lo`) is skipped. Packet counts and error/drop rates ride along in each series' hover meta rather than charting separately. |
| `ss -s` | Established, Time-wait | Sockets | Network | A host without `ss` simply has no Sockets series — see [Parser health](../../../library/custom-parsers.md#parser-health). |
| `cat /proc/diskstats` | `read <device>`, `write <device>` (B/s) per device | Disk I/O | Disk | Whole devices only — partitions (`sda1`, `nvme0n1p2`, …) and virtual/noise devices (`loop*`, `ram*`, `dm-*`, `zram*`, `sr*`) are skipped. |
| `cat /proc/loadavg /proc/stat` | Runnable, Total procs, Blocked | Processes | CPU | |

Network I/O and Disk I/O are rate metrics: computed from monotonic counter
deltas, they need two samples before they can chart anything, so the first
tick for a given interface or device emits no point.  A host reboot resets
those counters; otto detects the resulting negative delta, skips one tick,
and re-baselines from the new counters — a reboot never shows up as a
spike.

```{note}
{doc}`otto tunnel <../tunnel/index>` discovery (`discover_tunnels`) is built as a
`(command, pure parser)` pair for exactly this reason — it maps 1:1 onto the
`MetricParser` shape below (command / parse / interval). `otto tunnel` needs
no monitor to function — `otto tunnel list` is the CLI's own live view. When
`otto monitor` *is* running, the collector also scans the whole lab for
tunnels on each collection interval and streams them into the topology view
as overlays; see [Topology view](dashboard.md#topology-view).
```

## Log-sourced data

Some systems don't expose live values through a poll-able command: a cron
job digests performance counters into a timestamped file every few
minutes, or the interesting record is a log file's event stream rather
than a number. Both ride the same shell acquisition path as every other
parser — the command *is* the reduction step (`cat`/`tail`/`awk`/`grep`/`jq`
on the host ships back only the lines otto needs) — but instead of one
untimed value per tick, each row or line carries its own timestamp. The
design assumes source data is textually reducible on the host; binary or
otherwise irreducible formats are out of scope.

### CSV metric files

{class}`~otto.monitor.log_sourced.CsvMetricParser` charts a cron-digested
CSV file. Register it like any other parser (see [Custom parsers](../../../library/custom-parsers.md#custom-parsers)):

```python
from otto.monitor.log_sourced import CsvMetricParser
from otto.monitor.parsers import register_parsers

register_parsers(
    [
        CsvMetricParser(
            "cat /var/log/perf/net.csv",
            columns=["rx_kbps", "tx_kbps"],
            chart="Cron net digest",
            tab="network",
            tab_label="Network",
            unit="kb/s",
            interval=60,
        ),
    ]
)
```

Line format: the first column is an ISO-8601 or epoch-seconds timestamp
(naive values are treated as UTC); the remaining columns are numeric
values matching `columns`, comma-separated, in file order. Header and
otherwise malformed lines are skipped outright. The final line of each
read is provisional rather than trusted immediately — a mid-write read
can torn-truncate it into something that still parses — so it only emits
once a later read shows it unchanged; worst case this delays the newest
row by one poll interval, and a torn line itself never emits (see
[Timestamps](#timestamps) below for the high-water mark this protects).

Because points carry their own data-carried timestamps rather than the
collector's tick time, a file already holding the last hour of digests
backfills the dashboard and DB with a full hour of real history the moment
monitor starts, not just whatever arrives after that.

One instance per file: the command string is the parser registry key, so
monitoring "a couple of CSV files" means two registered instances. Give a
slow-cadence file its own `interval` (seconds; see
[Per-parser collection intervals](../../../library/custom-parsers.md#per-parser-collection-intervals)) so
otto doesn't re-read an unchanged file on every tick.

A cron job maintaining such a file might look like this:

```sh
#!/bin/sh
# Example cron digest: append "epoch,val1,val2", prune to the last hour.
# Cron entry (every 5 minutes):  */5 * * * *  root  /usr/local/bin/perf_digest.sh
FILE=/var/log/perf/net.csv
printf '%s,%s,%s\n' "$(date -u +%s)" "$(cat /sys/class/net/eth0/statistics/rx_bytes)" \
    "$(cat /sys/class/net/eth0/statistics/tx_bytes)" >> "$FILE"
tail -n 12 "$FILE" > "$FILE.tmp" && mv "$FILE.tmp" "$FILE"   # 12 lines = 1 h at 5-min cadence
```

Provisioning a script like this on a bed is a manual demo step — otto's own
test suite exercises `CsvMetricParser` entirely against fixture-written
files, never a live cron job.

### Log-event tables

{class}`~otto.monitor.log_sourced.RegexLogEventParser` turns matching log
lines into table rows instead of chart points. A worked syslog example,
using the same pattern otto's own test suite registers:

```python
from otto.monitor.log_sourced import RegexLogEventParser
from otto.monitor.parsers import register_parsers

SYSLOG_PATTERN = r"^(?P<ts>\S+) (?P<loghost>\S+) (?P<proc>[^:\[]+)(?:\[\d+\])?: (?P<message>.*)$"

register_parsers(
    [
        RegexLogEventParser(
            "tail -n 200 /var/log/syslog",
            SYSLOG_PATTERN,
            tab="syslog",
            tab_label="Syslog",
        ),
    ]
)
```

Every named group in `pattern` besides the timestamp group becomes a table
column, in pattern order (`loghost`, `proc`, `message` above). A line that
doesn't match is skipped entirely — a wrong pattern therefore produces zero
rows ever, which the [Parser health](../../../library/custom-parsers.md#parser-health) silent-command
backstop surfaces by the third tick.

`ts_group` (default `"ts"`) names the group holding the timestamp;
`ts_format` (default `"iso"`) tells `parse_timestamp` how to read it:
`"iso"` for ISO-8601, `"epoch"` for Unix epoch seconds, or anything else as
a `strptime` format. Classic syslog timestamps (`Jan  2 15:04:05`, no year)
need a `strptime` format — otto injects the current UTC year before
parsing those, so they parse correctly instead of rejecting outright. If
that injected year would land the row more than 2 days in the future (a
`Dec 31` line read just after New Year rolls over to next year's `Jan 1`
under the current-year injection), otto subtracts one year — the standard
syslog-consumer rollover guard, so a New Year boundary can't wedge the
high-water mark a year ahead of every real row.

Each `RegexLogEventParser` contributes one `kind="table"` tab on the
dashboard and no chart. Rows render newest-first with a client-side,
case-insensitive substring filter; the browser keeps roughly the last 500
rows on screen even though the database keeps every row ever collected —
reload that database with `otto monitor <path>` (see [Reviewing a
capture](review.md#reviewing-a-capture)) and the full history replays as a table
too, not just as charts.

Table parsers must declare their own `tab` id: a table tab can't share an
id with a chart tab, or with another table tab (see
{class}`~otto.models.monitor.TabSpec`). Registering a colliding tab id is a
configuration error that otto raises loudly rather than silently picking a
winner.

{class}`~otto.monitor.parsers.LogEvent` rows are a deliberately separate
data path from {class}`~otto.monitor.events.MonitorEvent` markers: log
events are per-host, high-volume, columnar table data, while
`MonitorEvent`s are the global, low-volume annotations that mark moments
on the chart timeline (see
[Monitoring from test suites](../../../library/custom-parsers.md#monitoring-from-test-suites)).

### Timestamps

Every log-sourced row carries its own data-carried timestamp instead of
the collector's tick time; a naive value (no timezone) is always treated
as UTC, whether it comes from a CSV's first column or a regex's timestamp
group. A row with no parseable timestamp is dropped — log-sourced parsers
have no tick-time fallback, so an empty or unrecognized timestamp field
means that row never appears at all.

Each parser instance keeps a high-water mark: the newest row timestamp it
has emitted so far. Re-reading a rolling window (the usual `tail -n N`)
drops everything at or below the mark, so ticks that overlap the previous
read are deduplicated rather than double-counted. The mark is keyed on the
row's own timestamp, not a file offset or byte count, so log rotation and
truncation need no special handling — a rotated file's new rows are still
newer than the mark and come straight through.

### Large files

An append-only log fits at any size: a fixed `tail -n N` window bounds
what one tick reads, and the high-water mark discards whatever overlaps
the previous read, so `N` only needs to comfortably cover one poll
interval's worth of new lines, not the file's total size.

Because a parser's `command` string is a static registry key, one parser
can't vary its command per tick — reading from a byte offset that grows
over time, for example, is unsupported by design; size `tail -n N` to the
interval instead. A large *regenerated* file (a digest script that
rewrites the whole thing on every run rather than appending) fits the same
way any verbose command output does: reduce at the source with
`awk`/`jq`/a product CLI, and give the parser its own slower `interval`
(see [Per-parser collection intervals](../../../library/custom-parsers.md#per-parser-collection-intervals))
if the file itself only changes infrequently — each parser rides its own
bucket, so a slow file never blocks faster ones.

## SNMP monitoring

Some targets expose performance metrics over SNMP rather than via a shell
interface.  Otto supports SNMP v2c polling for any standards-compliant agent —
a Zephyr device running otto's test-bed agent, a Linux box running net-snmp, or
network gear — on a separate channel from command execution.

### When to use it

Use SNMP monitoring when a host either has no shell (embedded Zephyr targets) or
when you prefer to pull metrics through a dedicated management channel rather than
shell commands.  See {doc}`../host/embedded` for embedded host setup and {doc}`../../configuration/lab-config`
for the `snmp` field reference.

### How otto reads SNMP data

`SnmpClient` (in `otto.monitor.snmp`) is a thin async SNMP v2c GET wrapper.  It
issues a single GET PDU per poll tick for all configured OIDs and returns a
`{oid: float | None}` mapping.  The `pysnmp` library is imported lazily inside
`SnmpClient.get`, so the SNMP path is entirely optional — otto imports cleanly
without `pysnmp` installed, and unit tests can mock at the `get` boundary.

### Built-in metric descriptors

Otto ships descriptors for a standard OID set.  Each descriptor (an `SnmpMetric`)
carries the label, chart group, y-axis title, unit, tab, and a `scale` factor
that converts the raw integer varbind to a real value.

| OID | Label | Chart | Unit | Notes |
| --- | ----- | ----- | ---- | ----- |
| `1.3.6.1.2.1.1.3.0` | Uptime | Uptime | s | Standard `sysUpTime` (TimeTicks ÷ 100); works against any compliant agent |
| `1.3.6.1.4.1.63245.1.1.0` | Overall CPU | CPU | % | Otto enterprise OID (centi-percent ÷ 100) |
| `1.3.6.1.4.1.63245.1.2.0` | Heap Used | Memory Usage | B | Otto enterprise OID |
| `1.3.6.1.4.1.63245.1.3.0` | Heap Free | Memory Usage | B | Otto enterprise OID |
| `1.3.6.1.4.1.63245.1.4.0` | Threads | Threads | — | Otto enterprise OID |

The enterprise OIDs are served by otto's Zephyr test-bed agent.  The enterprise
base is `1.3.6.1.4.1.63245` (PEN 63245, a placeholder — a real IANA PEN has not
yet been assigned).

`kind` governs how a raw varbind becomes a chart point: `gauge` (the
default, e.g. Heap Used above) charts `raw * scale` directly; `counter`
treats the varbind as a monotonic counter and converts it to a per-second
rate — first sighting and post-reboot re-baselining emit nothing, the same
rule the Unix `Network I/O`/`Disk I/O` parsers follow (see
[Built-in metrics](#built-in-metrics)).

### Per-interface and per-filesystem OIDs

Network and filesystem metrics live in an **indexed** subtree rather than a
handful of fixed leaves: a small agent has a known, fixed set of interfaces
and filesystems, 0-indexed by the firmware, and otto polls one scalar per
value with a plain GET — no table walk.  This layout is the
**firmware/manager contract**: the agent and otto must agree on it exactly,
the same way both sides agree on the core `.1` scalars above.

| OID | Leaf | Kind | Notes |
| --- | ---- | ---- | ----- |
| `1.3.6.1.4.1.63245.2.<i>.1.0` | rx bytes | counter | Charted as `rx if<i>` (B/s) on the Network tab. |
| `1.3.6.1.4.1.63245.2.<i>.2.0` | tx bytes | counter | Charted as `tx if<i>` (B/s) on the Network tab. |
| `1.3.6.1.4.1.63245.2.<i>.3.0` | rx packets | counter | Rides the rx-bytes series' hover meta, not its own chart. |
| `1.3.6.1.4.1.63245.2.<i>.4.0` | tx packets | counter | Rides the tx-bytes series' hover meta. |
| `1.3.6.1.4.1.63245.2.<i>.5.0` | rx+tx errors (combined) | counter | Charted as `errors if<i>` on the "Net errors" chart. |
| `1.3.6.1.4.1.63245.2.<i>.6.0` | drops | counter | Charted as `drops if<i>` on the "Net errors" chart. |
| `1.3.6.1.4.1.63245.3.<i>.1.0` | filesystem used bytes | gauge | Charted as `fs<i> used` on the Storage tab. |
| `1.3.6.1.4.1.63245.3.<i>.2.0` | filesystem total bytes | gauge | Rides the used-bytes series' hover meta as a human-readable total, not its own chart. |

`<i>` is the interface or filesystem index (`0`, `1`, …).  The generated
labels above (`rx if0`, `fs1 used`, …) come from the same descriptor
registry as the core scalars, so they can be renamed per device — see
[registering custom descriptors](../../../library/custom-parsers.md#extending-registering-custom-descriptors).
Lab data never spells out these OIDs directly; the `otto-net:N` /
`otto-fs:N` bundles (see {doc}`../../configuration/lab-config`) expand them
and register their descriptors together.

An OID present in `oids` but without a registered descriptor falls back to
default styling via `resolve_snmp_metric`: the OID string is used as the label
and chart name on the generic `metrics` tab, so a host can poll a bare OID with
zero code and still get a chart.
