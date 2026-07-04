# otto monitor Phase 3 — metrics expansion design

**Date:** 2026-07-03
**Status:** Approved in brainstorming session; amended during review with log-sourced
data (CSV metrics, log-event tables, large-file guidance) and host-pattern
registration.
**Parent:** `2026-07-02-monitor-revamp-roadmap-design.md` (Phase 3 — metrics). This spec
supersedes the roadmap's Phase 3 sketch where they differ (see "Scope corrections").

## Motivation

The dashboard's built-in coverage is thin on both host kinds:

- **Unix (shell) hosts** collect four parsers (top-CPU, memory %, disk-usage %, load).
  No network, no disk I/O, no per-core CPU. The user guide has promised network
  metrics since before the revamp; they still don't exist.
- **Embedded (SNMP) hosts** chart five scalars (uptime, overall CPU %, heap used/free,
  threads). No network, no filesystem/flash visibility — even though recent bed work
  (fs-mount heap leak, FAT/LFS usage) showed exactly those numbers matter.

Phases 0–2 built the machinery this phase rides on: parser API v2 with per-target
instance state, the `SnmpMetric` descriptor registry, the typed `/api/meta` contract,
and a React frontend that renders purely from that contract — so new tabs and charts
light up with **zero frontend changes** for chart-shaped data. (The log-event table
below is this phase's one deliberate frontend addition — a new tab *kind*, not a new
chart.)

## Scope corrections (vs. the roadmap sketch)

The roadmap's Phase 3 listed "SNMP parity: HOST-RESOURCES-MIB / UCD-SNMP-MIB /
ifTable so Zephyr/embedded beds render the same tabs as Unix hosts". Decisions made
in this brainstorm replace that:

- **No cross-kind parity requirement.** Each host kind is enriched through its own
  natural channel: Unix via shell `/proc` parsers, embedded via SNMP descriptors.
  They do not need to render identical tabs.
- **No standard-MIB work.** Embedded additions extend the existing otto enterprise
  OID subtree (scalars the small Zephyr agent can serve trivially); implementing
  ifTable/HOST-RESOURCES on the device is rejected as a heavy firmware lift for no
  in-house consumer. Monitoring a net-snmp Unix host over SNMP keeps working as
  today (hand-listed OIDs + fallback descriptors) but gains nothing new here.
- **PSI (`/proc/pressure/*`) dropped.** Overlaps load/per-core CPU for our use,
  needs kernel ≥ 4.20 + config, and on most beds would produce permanently empty
  charts.

## Architecture

**Reuse the existing two-layer split; add zero new acquisition machinery.**

- Unix stats → new `MetricParser` subclasses (presentation) over the existing shell
  `run()` path (acquisition).
- Embedded stats → new `SnmpMetric` descriptors (presentation) for new enterprise
  scalar OIDs (acquisition via the existing `SnmpClient.get`).

**No SNMP table-walk (GETNEXT/GETBULK).** Per-interface / per-filesystem metrics use
**enumerated scalar OIDs** (`…2.<if-index>.…`, `…3.<fs-index>.…`). Today's plain GET
handles them; multiple OIDs already share one chart (heap used/free prove the
pattern). A small otto agent has a known, fixed set of interfaces/filesystems, so a
walk buys nothing but firmware and client complexity.

**Counter→rate lives per channel (Approach A).** Both channels turn monotonic
counters into per-second rates, but the *state* stays local to each:

- Shell rate parsers hold previous-tick counters as **instance state** — safe because
  parser instances are per-target deep copies (established Phase 1 design).
- SNMP targets get a per-target **`SnmpRateState`** (dict keyed by OID) applied in
  the SNMP result-processing path for descriptors with `kind="counter"`.

The *math* is shared: one `compute_rate(prev, cur, dt) -> float | None` helper both
sides call. The alternative — a unified rate service inside the collector keyed by
(host, series) — was rejected: it forces a cross-cutting tick-loop refactor and
couples both channels to shared collector state, against the grain of the merged
Phase 1 architecture.

### Rate rule (one rule, both channels)

`compute_rate` returns `None` on **any negative delta**; the caller re-baselines and
emits no point for that tick. This treats device reboot and counter wrap identically.
Trade-off, chosen deliberately: a genuine Counter32 wrap (4.3 GB between ticks) loses
one tick instead of producing a point, while the wrap-compensation alternative would
turn every reboot — common on test beds — into one absurd multi-GB/s spike. The first
tick after any baseline loss likewise emits no point. Rates divide by *actual elapsed
time* between samples, not the nominal interval, so they are correct at any cadence
and across missed ticks.

### Constraint recorded for implementers

Two structural facts of the parser registry shape the catalog below:

1. Parsers are keyed by their **exact command string** (`{p.command: p}`) — two
   parsers cannot share a command.
2. One parser maps to **one chart** (`ChartSpec` is built from the parser's own
   `chart`/`y_title`/`unit`/`tab` attributes; multi-series is fine, multi-chart is
   not).

Hence: swap rides `free -b` inside `MemParser` (same chart, same unit), but process
counts cannot ride `cat /proc/loadavg` (key collision with `LoadParser`, and mixing
~400-proc counts onto the Load chart would flatten its curves) — they get their own
command and chart.

## Unix catalog — new shell parsers

Every new command is cheap and instant (four `/proc` reads plus `ss -s` — nothing
like the 0.5 s-blocking `top -bn2`); the per-tick shell burden grows by five
commands. All default to the global
interval — no per-parser `interval` overrides needed, since rates use actual elapsed
time.

| Parser | Command | Series | Chart / Tab | State |
| --- | --- | --- | --- | --- |
| `NetDevParser` | `cat /proc/net/dev` | `rx <iface>`, `tx <iface>` (B/s) | "Network I/O" / **Network** (new tab, id `network`) | prev byte counters per iface |
| `SocketsParser` | `ss -s` | `Established`, `Time-wait` | "Sockets" / Network | none (gauges) |
| `DiskIoParser` | `cat /proc/diskstats` | `read <dev>`, `write <dev>` (B/s) | "Disk I/O" / Disk (existing tab) | prev sector counters per dev |
| `MemParser` (extended) | `free -b` (existing) | + `Swap` (%) | joins "Memory Usage" / Memory | none |
| `PerCoreCpuParser` | `cat /proc/stat` | `core 0` … `core N` (%) | "Per-core CPU" / CPU | prev jiffies per core |
| `ProcCountParser` | `cat /proc/loadavg /proc/stat` | `Runnable`, `Blocked`, `Total procs` | "Processes" / CPU | none (gauges) |

Details:

- **`NetDevParser`** skips `lo`. Hover meta per point carries packet/error/drop
  rates computed from the same deltas (`pkt/s`, `err/s`, `drop/s`) — errors/drops
  are meta, not series, on the Unix side. Interface churn (e.g. `ppp0` appearing/
  vanishing): per-interface baselines; a vanished interface's state is dropped, a
  new one baselines silently (first point on its second tick).
- **`SocketsParser`** parses the `TCP:` summary line of `ss -s`
  (`estab N` / `timewait N`). Absolute counts, no rate state.
- **`DiskIoParser`** converts sector deltas × 512 to B/s. Whole devices only:
  skip partitions (`sd[a-z]+N`, `nvmeXnYpZ`, `mmcblkXpY` name patterns) and
  virtual/noise devices (`loop*`, `ram*`, `dm-*`, `zram*`, `sr*`).
- **`MemParser`** additionally parses the `Swap:` line already present in `free -b`
  output → `Swap` used-% series on the existing "Memory Usage" chart. Hosts with no
  swap (total = 0) omit the series entirely — no flat-0 line.
- **`PerCoreCpuParser`** computes busy % per `cpuN` line as
  `100 × (1 − Δ(idle+iowait)/Δtotal)`. The aggregate `cpu` line is skipped —
  `TopCpuParser` already charts overall CPU.
- **`ProcCountParser`** reads `Runnable`/`Total procs` from loadavg field 4
  (`running/total`) and `Blocked` from `/proc/stat`'s `procs_blocked`. Pure `cat`,
  trivially portable; the two-file command string is what makes its registry key
  unique.

## Embedded catalog — new enterprise OIDs + descriptors

Existing subtree (`1.3.6.1.4.1.63245`, PEN placeholder — see existing TODO):
`.1.1.0` CPU %, `.1.2.0` heap used, `.1.3.0` heap free, `.1.4.0` threads, plus
standard `sysUpTime`.

New subtrees. `<i>` is a small integer index the agent assigns (0-based, stable for
a given build):

| OID | Metric | Kind | Chart / Tab |
| --- | --- | --- | --- |
| `.2.<i>.1.0` | net if *i* rx bytes | counter → B/s | "Network I/O" / **Network** |
| `.2.<i>.2.0` | net if *i* tx bytes | counter → B/s | "Network I/O" / Network |
| `.2.<i>.3.0` | net if *i* rx packets | counter → pkt/s | hover meta on the rx series |
| `.2.<i>.4.0` | net if *i* tx packets | counter → pkt/s | hover meta on the tx series |
| `.2.<i>.5.0` | net if *i* rx+tx errors | counter → err/s | "Net errors" / Network |
| `.2.<i>.6.0` | net if *i* drops | counter → drop/s | "Net errors" / Network |
| `.3.<i>.1.0` | fs *i* bytes used | gauge | "Filesystem" / **Storage** (new tab, id `storage`) |
| `.3.<i>.2.0` | fs *i* bytes total | gauge | hover meta (`Total`, human-readable) on the used series |

**The firmware agent is out of otto-sh scope.** This table *is* the contract, exactly
as the existing PEN comment block is for the current scalars: otto-sh ships the
descriptors plus a fake-agent test fixture; charts light up when the agent grows the
OIDs. Old firmware + new descriptors degrades gracefully (see Warnings/Degradation).

### `SnmpMetric` contract additions

1. **`kind: Literal["gauge", "counter"] = "gauge"`** — `counter` descriptors are
   rate-converted via the per-target `SnmpRateState` (rule above). `scale` applies
   to the raw varbind first, then the rate is computed over scaled values; `unit`
   states the rate unit (e.g. `B/s`). Existing descriptors are untouched (`gauge`
   default).
2. **`meta_of: str | None = None`** — when set to another OID, this descriptor's
   value (scaled, and rate-converted if `kind="counter"`) is not charted as its own
   series; it is attached to the hover-meta dict of the series produced by the
   `meta_of` OID, under this descriptor's `label`. This is how packet rates ride the
   byte-rate series and fs `Total` rides the fs used series. A `meta_of` target that
   isn't polled (or yields no point this tick) simply drops the meta — never an
   error.
3. **Indexed fallback labels** — built-in labels are the indexed forms (`rx if0`,
   `fs0 used`). A scalar GET cannot ask the device for interface names, so friendly
   names come from descriptor registration: a bed's init module calls
   `register_snmp_metric(SnmpMetric(oid=..., label="rx (ppp0)", ...))` to override —
   the existing overwrite-by-design path, no new mechanism.

### Named OID bundles in lab data

Today lab data hand-lists every OID. The `snmp.oids` tuple additionally accepts
bundle names, freely mixed with raw OIDs:

```toml
[hosts.zephyr.snmp]
oids = ["otto-core", "otto-net:2", "otto-fs:1", "1.3.6.1.2.1.1.3.0"]
```

- `otto-core` → the five existing scalars (uptime, CPU, heap used/free, threads).
- `otto-net:N` → the six network OIDs for interfaces `0..N-1` (`:N` defaults to 1).
- `otto-fs:N` → the two filesystem OIDs for filesystems `0..N-1` (`:N` defaults to 1).

Expansion happens in the monitor factory via a new `expand_oid_bundles()` in
`otto.monitor.snmp`; an unknown bundle name raises loudly with the known-bundle list.
Raw OIDs pass through untouched, so existing lab data keeps working unchanged.

## Parser-health warnings

Silent-missing metrics become visible. Two detection layers on the shell path, both
**warn-once per (host, command) per run** (a 5 s tick must not repeat the same line
forever):

1. **Command failed** (primary, immediate): in `_process_host_results`, first
   occurrence of `cmd_result.retcode != 0` warns —
   `Monitor: 'ss -s' failed on test1 (exit 127): sh: ss: command not found — Sockets metrics will be missing`.
   Names the host, the command, and what the user loses. Exit 127/126 is exactly the
   missing-tool case, but any non-zero retcode warns.
2. **Parser silent** (backstop): the command succeeds but the parser has produced
   **nothing at all — no samples and no events — by its 3rd tick** → warn once —
   `Monitor: parser NetDevParser ('cat /proc/net/dev') has produced no data on test1 after 3 ticks`.
   The rule is "never produced by tick 3", deliberately *not* "3 consecutive empty
   ticks": sparse log-sourced parsers legitimately return empty between cron writes,
   and rate parsers return `{}` on their baseline tick. This layer catches format
   drift (busybox `top`, locale weirdness, wrong file path with a tolerant command)
   where the command "works" but parsing never does. Once a parser has produced data
   even once, later droughts never warn.

**SNMP symmetry:** an OID that has yielded only `None` (noSuchInstance / not served)
through its first 3 ticks warns once per (host, oid) — old firmware + new
descriptors says so instead of silently showing nothing. Transport-level SNMP
failures already warn per batch in `SnmpClient.get`; unchanged.

Warnings go to the `otto` logger at WARNING. Per the three-sink logging design,
warnings are never gated by `LogMode` — so they reach the console even though the
monitor sets hosts to `LogMode.NEVER` (which silences command I/O only). No new
logging machinery.

Not doing: recovery notifications ("parser started working again"), per-tick repeat
warnings, automatic fallback to alternative commands.

## Host-pattern parser registration

`register_host_parsers()` grows regex support so one registration can cover a family
of hosts (e.g. every busybox bed) without listing IDs:

```python
register_host_parsers(re.compile(r"busybox-.*"), {**DEFAULT_PARSERS, ...})
```

- **Type-driven API:** the first parameter becomes `str | re.Pattern`. A plain
  string is an exact host ID — today's behavior, unchanged. A compiled pattern opts
  into matching; there is no flag and no guessing whether a string was "meant" as a
  regex.
- **`fullmatch` semantics** — `re.compile("test")` matches host `test` only, never
  `my-test-2`.
- **Precedence:** exact ID > pattern > project-level > defaults. An exact
  registration shadows any pattern for that host.
- **Ambiguity is loud:** if two or more patterns match a host (and no exact
  registration shadows them), `get_host_parsers()` raises, naming the patterns and
  the host — the Registry tradition; no import-order-dependent silent winner.
- Patterns live in their own registry keyed by `pattern.pattern` (duplicate pattern
  strings stay loud via the existing dupe machinery); `get_host_parsers()` checks
  the exact registry first, then scans patterns. Host counts are small; a linear
  scan is fine.

Regex applies to `HOST_PARSERS` only. SNMP descriptors are keyed by OID, not host,
and project-level parsers already apply everywhere — neither needs it.

## Log-sourced data

Some systems don't expose live values — a cron job digests performance numbers into
timestamped CSV files every N minutes, or the interesting record is a log file's
event stream. Both ride the existing shell acquisition path; what they need is a
contract that honors **data-carried timestamps**, **multiple points per read**, and
**non-numeric event rows**.

### Contract: `parse_tick()` (additive, no parser API v3)

One new overridable method on `MetricParser`; today's `parse()` signature is
untouched, so third-party parsers keep working:

```python
class TimedSample(NamedTuple):
    ts: datetime | None                    # None → stamped with tick time
    series: dict[str, MetricDataPoint]

class LogEvent(NamedTuple):
    ts: datetime
    fields: dict[str, str]                 # column → value

class TickResult(NamedTuple):
    samples: list[TimedSample]
    events: list[LogEvent]

def parse_tick(self, output: str, *, ctx: ParseContext) -> TickResult:
    """Default: wraps parse() as one untimed sample."""
```

The collector always calls `parse_tick`, routes samples to the metric store
(honoring each sample's own `ts`; `None` means tick time) and events to the new
log-event store. Existing parsers hit the default adapter and behave exactly as
today. One extension point covers both new source kinds — no `isinstance` routing,
and the parser-health "silent" rule counts samples *or* events as produced data.

Log-sourced parsers implement the abstract `parse()` as a trivial `return {}` —
only their `parse_tick` override matters; the base-class docstring says so.

Sample timestamps are emitted in ascending order per parser (sorted at parse time),
and a shared **high-water-mark helper** (per-target instance state: last emitted
timestamp) makes re-reads of rolling files idempotent. Pleasant consequence: on
monitor start, a file holding the last hour backfills the dashboard and the DB with
an hour of real history.

### `CsvMetricParser` — cron-digested numeric files

Shipped, configurable, subclass-friendly:

- **Line format:** first column ISO-8601 or epoch-seconds timestamp (naive = UTC,
  documented), remaining columns numeric. Column names (= series labels) are a
  constructor argument; header/malformed/partial lines are skipped (a mid-write
  read self-heals next tick — the high-water mark hasn't passed the torn line).
- **Constructor:** command (e.g. `cat /var/log/perf/net.csv`), column names, chart /
  tab / y_title / unit, `interval` (poll cadence — e.g. 60 s for a 5-minute file).
- One instance per file; "a couple of CSV files" = two registered instances
  (distinct commands → distinct registry keys), composed via the project-level /
  per-host / pattern mechanisms above.

### Log-event tables

Time-based *events* from a host's log, displayed as a table — a genuinely different
data format from every numeric family above, and deliberately **separate from
`MonitorEvent`**: that type stays the user/lifecycle chart-marker annotation system
(global, low-volume, color/dash presentation); log events are per-host, high-volume,
columnar *data*. Overlaying selected log events as chart markers is future work.

- **Record + storage:** parsers emit `LogEvent(ts, fields)`; the collector attaches
  the host (exactly as it does for metric points) and stores rows keyed by
  `(host, ts, fields)` — new SQLite table (persisted → historical `--db`/`--file`
  mode renders tables too, and JSON export/import carries them), an in-memory ring
  per host (last ~1000 rows; the DB keeps everything), and a new batched SSE
  message kind (`log_event`).
- **Parser:** shipped `RegexLogEventParser` — command (`tail -n 200 /var/log/app.log`),
  a line regex whose **named groups become table columns**, one group designated the
  timestamp (epoch or `strptime` format; naive = UTC). Columns are declared on the
  parser, so the table schema is static. A parser that yields only events
  contributes a table `TabSpec` instead of a `ChartSpec`. The worked example parses
  **syslog** — the existing Unix facet to model on; nothing to install on beds.
- **Wire contract:** `TabSpec` gains `kind: Literal["charts", "table"] = "charts"`
  and `columns: list[str] | None = None`. Backward-compatible defaults, but it is a
  wire-schema bump: `monitor-meta.schema.json` and the generated TS types regenerate
  through the existing Phase 2 drift gate.
- **Frontend (minimal v1):** an `EventTable` React component for `kind="table"`
  tabs — newest-first rows for the selected host, client-side substring filter,
  display capped at the last ~500 rows, live append from the SSE feed. No sorting,
  pagination, or virtualization in v1.

### Large files — where the method holds and where it stops

- **Append-only logs (any size):** fits unchanged — `tail -n N` bounds every read to
  new data, the high-water mark discards overlap; file size is irrelevant. Note:
  command strings are static registry keys, so per-tick *varying* commands
  (byte-offset `tail -c +OFFSET`) are out — the fixed `tail -n N` window + client
  dedup is the supported incremental read.
- **Large regenerated structured files:** fits by principle — **the command is the
  reduction step**. Shell acquisition means remote pre-filtering (`awk`, `grep`,
  `jq`, or a product-specific CLI) ships back only the lines otto needs; a slow
  reduction declares its own `interval` and rides its own bucket, so it cannot
  stall fast metrics, and the per-tick timeout scales with the bucket.

**Stated design assumption:** source data is always textually reducible on the
host — some tool can render it to lines for the command to filter. Binary or
otherwise irreducible formats are not a case this design handles or works around.

### Bed demo (optional, not gating)

A documented example cron script (timestamp + a few random values, file pruned to
the last hour) ships with the docs; provisioning it on one bed VM is a manual demo
step taken only with explicit go-ahead. All automated tests use fixture-written
files — no wall-clock cadence dependency in any gate. The event-table demo needs no
provisioning at all: it points `RegexLogEventParser` at the VM's real syslog.

## Degradation matrix (all silent-by-design, now warned)

| Situation | Behavior |
| --- | --- |
| Host lacks `ss` (or any parser command) | retcode ≠ 0 → warn-once; series never appears; no empty chart (charts materialize from data, not catalog) |
| Parser output format drift | parse returns `{}` → K=3 warn-once; series absent |
| Host without swap | `Swap` series omitted (total = 0 guard), no warning — absence is correct, and `free -b` succeeded |
| First tick of any rate parser / counter OID | no point (baseline); covered by K = 3 margin |
| Interface/device appears or vanishes | new key baselines silently; vanished key's state dropped |
| Device reboot or counter wrap | negative delta → skip tick, re-baseline |
| Agent doesn't serve a new OID | noSuchInstance → `None` → skipped + K=3 warn-once |
| Stuck SNMP relay / unreachable agent | existing per-tick timeout + batch warning; unchanged |
| Log/CSV file absent | command exits non-zero (`cat`/`tail`: no such file) → retcode warn-once |
| No new rows between cron writes | legitimately empty tick — never warns once data has flowed |
| Torn/partial line (read mid-write) | line skipped; high-water mark hasn't passed it → re-emitted whole next tick |
| Log rotation / file truncated | high-water mark keyed on row timestamps, not offsets → new rows still newer, emitted normally |
| Fully unparsable file (wrong regex/format) | zero rows ever → "silent parser" warn by tick 3 |

## Proof of the extension mechanism (repo-defined stat)

Make the docstring example real, end to end — and prove **scoping**:

- **Ship `UptimeParser` in `otto.examples`** (the conformance-sample home): parses
  `cat /proc/uptime` → single `Uptime` series (seconds, unit `s`). Deterministic,
  universally available, and mirrors the embedded `sysUpTime` chart. Its docstring is
  written as the "write your own parser" template.
- **Register it per-host** in an e2e test repo's init module (listed in that repo's
  `.otto/settings.toml`):
  `register_host_parsers("host-a", {**DEFAULT_PARSERS, UptimeParser().command: UptimeParser()})`.
- **The e2e test asserts scoping**, over two hosts in the repo's lab:
  1. host A's collected series include `Uptime`, and `/api/meta` grows the chart —
     proving init module → registry → collector → contract, the full third-party path;
  2. host B (no registration) does **not** get `Uptime`;
  3. host B still collects the untouched defaults (fallback intact).

Per-host registration (not project-level) is what makes the scoping assertion
meaningful — `register_parsers()` applies everywhere by definition, and its
everywhere-behavior already has Phase 1 test coverage. The `ss`→`netstat` per-host
*swap* stays as the documented worked example in the guide (below); the uptime parser
is the *executed* one. SNMP descriptor registration needs no separate proof —
built-ins already flow through the public `register_snmp_metric()` path by design.

## Testing

Existing tiers, no new machinery:

- **Unit** — canned-output fixtures per parser: happy path, missing command
  (`not found` + retcode), truncated/garbage output, interface churn, no-swap host,
  first-tick `{}`. `compute_rate`: normal, negative delta, zero/near-zero dt.
  `SnmpRateState`: counter sequence → rates, reset mid-stream, gauge descriptors
  bypass untouched. Bundle expansion: each bundle, `:N` counts, mixed raw OIDs,
  unknown-bundle error. Warning layers: retcode warn-once (no repeat on tick 2),
  K=3 silent-parser warn, counter reset on data, SNMP `None`-OID warn. Pattern
  registration: exact-beats-pattern shadowing, `fullmatch` (no substring hits),
  two-patterns-match ambiguity error, plain-string behavior unchanged.
- **Integration (collector-level)** — fake SNMP client scripted with counter
  sequences → rate series land in the store with correct values; shell target with
  scripted `Results` across 3+ ticks → network/disk/per-core rates correct;
  warning emission through the real `_process_*` paths.
- **Log-sourced units** — `parse_tick` default adapter (existing parsers
  bit-identical); `CsvMetricParser`: epoch + ISO timestamps, high-water dedup across
  re-reads, restart backfill, torn/malformed/header lines, column mismatch;
  `RegexLogEventParser`: named-group extraction, timestamp formats, rotation
  behavior; backdated-sample routing through the collector honors sample `ts`.
- **Contract** — new parsers/descriptors surface correctly in `/api/meta`
  (tab/chart/unit); `SnmpMetric.kind`/`meta_of` are backend-internal, but
  `TabSpec.kind`/`columns` **is a wire-schema bump** — `monitor-meta.schema.json`
  and generated TS types regenerate, and the existing drift gate pins the result
  (table tab present with its columns; chart tabs default `kind="charts"`).
- **Dashboard e2e** — chart-shaped additions need no new Playwright machinery (the
  frontend renders from the contract; one FakeCollector scripted-series addition
  covers a Network-tab render). The **event table** does: pins for table render from
  scripted events, live SSE append, substring filter, the ~500-row display cap, and
  historical (`--db`) table render. vitest covers the table store logic
  (append/filter/cap) and `log_event` SSE handling.
- **Extension e2e** — the two-host scoping test above.

## Documentation

- `guide/monitor.md`: new metric tables for both host kinds — the guide's
  long-standing network promise finally becomes true; the OID contract table marked
  explicitly as the firmware-facing contract; parser-health warnings explained
  (what a `failed on <host>` line means and how to fix or swap the parser).
- `guide/lab-config.md`: bundle syntax in the `snmp` block reference.
- Extension docs: a worked "these hosts have no `ss`" example swapping in a
  `netstat`-based parser via `register_host_parsers` with a host pattern
  (`re.compile(r"busybox-.*")`) — showing both the swap recipe and the pattern
  mechanism (documented, not shipped) — next to the executed `UptimeParser` example.
- Log-sourced guide section: configuring `CsvMetricParser` and
  `RegexLogEventParser` (with the syslog worked example), the example cron digest
  script, timestamp conventions (naive = UTC), and the large-file guidance —
  tail windows and reduce-at-the-source commands.

## Out of scope / future work

- **Firmware agent changes** — contract only; the agent grows the OIDs separately.
- **CLI metric toggles** (`otto monitor --disable network`, per-host lab-data
  toggles) — Phase 4 UX pass.
- **Shipped `NetstatSocketsParser` / tool auto-detection** — documented recipe now;
  shipping a fallback (and auto-picking per host) is future work.
- **PSI parsers** — dropped (rationale above).
- **Standard-MIB (ifTable/HOST-RESOURCES) support** — rejected for the agent;
  a net-snmp host can still be polled via raw OIDs + fallback descriptors as today.
- **Per-series chart override** (one parser → several charts) — not needed by this
  catalog once `ProcCountParser` got its own command; revisit only if a real parser
  needs it.
- **Friendly interface/fs names served by the device** — labels come from descriptor
  registration for now.
- **Considered and not chosen** (from the brainstorm's embedded menu): memory detail
  (fragmentation, stack high-water marks) and CPU/scheduler detail (per-thread CPU,
  context switches) — deferred, not rejected; natural next OID subtrees (`.4`, `.5`)
  if wanted later.
- **Log events as chart markers** (overlaying selected `LogEvent` rows on charts via
  the existing `MonitorEvent` marker machinery) — natural follow-on once tables
  exist.
- **Per-tick dynamic commands** (byte-offset incremental reads) — command strings
  stay static registry keys; `tail -n N` windows are the supported incremental read.
- **Table UX beyond v1** (sorting, pagination, virtualization, severity coloring) —
  Phase 4 UX territory.

## Phasing note

This spec is deliberately larger than one implementation plan; expect **two plans**:

- **Plan A — metrics:** (1) shared `compute_rate` + Unix rate parsers, (2) remaining
  Unix parsers + parser-health warnings, (3) SNMP `kind`/`meta_of` +
  `SnmpRateState` + descriptors + bundles, (4) host-pattern registration,
  (5) `UptimeParser` example + scoping e2e.
- **Plan B — log-sourced data:** (6) `parse_tick` contract + high-water helper +
  `CsvMetricParser`, (7) log-event backend (record, DB table, ring, SSE,
  `RegexLogEventParser`), (8) `TabSpec` bump + `EventTable` frontend + Playwright
  pins.

Docs land per-plan with their features. Plan B depends on Plan A only at the margins
(warning rule, registration mechanisms) — they could interleave, but A-then-B keeps
each review cohesive.
