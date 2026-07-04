# Monitor Phase 3 Plan A (Metrics) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expand otto monitor's built-in metrics — five new Unix `/proc` shell parsers with counter→rate state, SNMP counter/meta_of descriptors + enterprise net/fs OID subtrees + named OID bundles, parser-health warnings, host-pattern (regex) parser registration, and the executed `UptimeParser` extension proof with per-host scoping tests.

**Architecture:** Everything rides the two existing acquisition channels (shell `run()`, `SnmpClient.get`). Counter→rate state lives per channel (parser instance state / per-`SnmpSource` tracker) with the math shared in one new `otto/monitor/rates.py`. Presentation stays declarative: new parsers/descriptors flow into `/api/meta` through the existing `MetricView` machinery — **no wire-contract changes in this plan** (`SnmpMetric.kind`/`meta_of` are backend-internal).

**Tech Stack:** Python 3.10+, pydantic (`SnmpMetric`), pytest + pytest-asyncio, existing Registry machinery.

**Spec:** `docs/superpowers/specs/2026-07-03-monitor-metrics-phase3-design.md` (Plan A covers everything except the "Log-sourced data" section, which is Plan B).

## Global Constraints

- **No `from __future__ import annotations`** — real 3.10+ annotations, module-top imports (Sphinx `-W` nitpicky gate).
- **ruff `select=ALL` discipline**: after any `ruff format`, re-run `ruff check .` (format is not lint-neutral). Prefer idiomatic fixes over `noqa`; narrow per-site `noqa` with a reason comment is last resort.
- **`ty` runs only at `nox -s typecheck`** — budget a typecheck round after src edits (Task 15).
- **Commits**: embed the trailer in `-m` (the prepare-commit-msg hook needs /dev/tty and silently defaults): end every commit message with `Assisted-by: Claude Fable 5`.
- **Never `git add -u`** — add named files only.
- **Fresh worktree setup**: `uv sync` first (no `.venv` otherwise); `make coverage` needs the web dist (`make web`) — use `make coverage-hostless` if Node/dist is unavailable.
- **Per-task gate** = scoped pytest; **final gate** (Task 15) = `make coverage` + `nox -s lint` + `nox -s typecheck` + `make docs`.
- **Never skip on host-down** in e2e — bed-unreachable must fail with a host-named error.
- **No heavy parallel test loops on the dev VM** — single `-n auto` passes via make/nox targets only.
- Timestamps: all `datetime.now(tz=timezone.utc)`; naive datetimes never enter the store.

## File Structure

| File | Role |
| --- | --- |
| `src/otto/monitor/rates.py` (create) | `compute_rate()` + `RateTracker` — the one shared counter→rate implementation |
| `src/otto/monitor/parsers.py` (modify) | `ParseContext.ts`; 5 new parsers; `MemParser` swap; pattern registration |
| `src/otto/monitor/snmp.py` (modify) | `SnmpMetric.kind`/`meta_of`; `process_snmp_values()`; net/fs descriptors; `expand_oid_bundles()` |
| `src/otto/monitor/collector.py` (modify) | thread `ts` into `ParseContext`; SNMP dict routing; parser-health warnings; meta_of view filter |
| `src/otto/monitor/factory.py` (modify) | bundle expansion at target construction |
| `src/otto/examples/monitor.py` (create) | `UptimeParser` — the executed extension example |
| `tests/unit/monitor/test_rates.py` (create) | rate math + tracker tests |
| `tests/unit/monitor/test_parsers.py` (modify) | new parser + pattern-registration tests |
| `tests/unit/monitor/test_snmp.py` (modify) | kind/meta_of/bundle tests; `points_from_values` → `process_snmp_values` |
| `tests/unit/monitor/test_collector_warnings.py` (create) | edge-triggered failure/recovery + never-produced layers through real `_process_*` paths |
| `tests/unit/monitor/test_scoping.py` (create) | two-mock-host scoping integration test |
| `tests/e2e/monitor/test_monitor_e2e.py` (modify) | subprocess scoping e2e (two runs, env-gated init module) |
| `tests/repo1/pylib/repo1_monitor_uptime.py` (create) + `tests/repo1/.otto/settings.toml` (modify) | env-gated init module for the e2e |
| `docs/guide/monitor.md`, `docs/guide/lab-config.md` (modify) | metric tables, OID contract, bundles, warnings, pattern example |

---

### Task 1: Shared rate math — `rates.py` + `ParseContext.ts`

**Files:**
- Create: `src/otto/monitor/rates.py`
- Modify: `src/otto/monitor/parsers.py` (ParseContext), `src/otto/monitor/collector.py:277` (thread ts)
- Test: `tests/unit/monitor/test_rates.py` (create), `tests/unit/monitor/test_parsers.py` (ParseContext)

**Interfaces:**
- Consumes: nothing new.
- Produces: `compute_rate(prev_value: float, cur_value: float, dt: float) -> float | None`;
  `RateTracker` with `update(key: str, value: float, ts: datetime) -> float | None` and
  `prune(active: set[str]) -> None`; `ParseContext.ts: datetime | None = None`.
  Tasks 2, 4, 9 rely on these exact names.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/monitor/test_rates.py`:

```python
"""Unit tests for the shared counter->rate helpers."""

from datetime import datetime, timedelta, timezone

from otto.monitor.rates import RateTracker, compute_rate

T0 = datetime(2026, 7, 3, 12, 0, 0, tzinfo=timezone.utc)


class TestComputeRate:
    def test_positive_delta(self):
        assert compute_rate(100.0, 250.0, 5.0) == 30.0

    def test_zero_delta_is_zero_rate(self):
        assert compute_rate(100.0, 100.0, 5.0) == 0.0

    def test_negative_delta_returns_none(self):
        """Counter reset (reboot) or wrap: skip the tick, never a spike."""
        assert compute_rate(100.0, 50.0, 5.0) is None

    def test_zero_dt_returns_none(self):
        assert compute_rate(100.0, 200.0, 0.0) is None

    def test_negative_dt_returns_none(self):
        assert compute_rate(100.0, 200.0, -1.0) is None


class TestRateTracker:
    def test_first_sighting_returns_none(self):
        tracker = RateTracker()
        assert tracker.update("k", 100.0, T0) is None

    def test_second_sighting_returns_rate(self):
        tracker = RateTracker()
        tracker.update("k", 100.0, T0)
        assert tracker.update("k", 250.0, T0 + timedelta(seconds=5)) == 30.0

    def test_rate_uses_actual_elapsed_not_nominal(self):
        tracker = RateTracker()
        tracker.update("k", 0.0, T0)
        # 10 s elapsed, not "the interval": 100/10 = 10
        assert tracker.update("k", 100.0, T0 + timedelta(seconds=10)) == 10.0

    def test_negative_delta_rebaselines(self):
        """Reset tick returns None; the NEXT tick rates against the new baseline."""
        tracker = RateTracker()
        tracker.update("k", 1000.0, T0)
        assert tracker.update("k", 10.0, T0 + timedelta(seconds=5)) is None
        assert tracker.update("k", 60.0, T0 + timedelta(seconds=10)) == 10.0

    def test_keys_are_independent(self):
        tracker = RateTracker()
        tracker.update("a", 0.0, T0)
        tracker.update("b", 0.0, T0)
        assert tracker.update("a", 50.0, T0 + timedelta(seconds=5)) == 10.0
        assert tracker.update("b", 100.0, T0 + timedelta(seconds=5)) == 20.0

    def test_prune_drops_stale_keys(self):
        """A vanished interface's state is dropped; re-appearance re-baselines."""
        tracker = RateTracker()
        tracker.update("gone", 100.0, T0)
        tracker.update("kept", 100.0, T0)
        tracker.prune({"kept"})
        assert tracker.update("gone", 200.0, T0 + timedelta(seconds=5)) is None
        assert tracker.update("kept", 200.0, T0 + timedelta(seconds=5)) == 20.0
```

Add to `tests/unit/monitor/test_parsers.py` (next to `test_parse_context_is_frozen`):

```python
def test_parse_context_carries_optional_ts():
    from datetime import datetime, timezone

    assert ParseContext().ts is None
    ts = datetime(2026, 7, 3, tzinfo=timezone.utc)
    assert ParseContext(core_count=2, ts=ts).ts == ts
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/monitor/test_rates.py tests/unit/monitor/test_parsers.py::test_parse_context_carries_optional_ts -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'otto.monitor.rates'` and `TypeError: ... unexpected keyword argument 'ts'`.

- [ ] **Step 3: Implement**

Create `src/otto/monitor/rates.py`:

```python
"""Counter->rate conversion shared by shell parsers and the SNMP path.

Monotonic counters (network bytes, disk sectors, SNMP Counter32) become
per-second rates here. One rule for both channels: a negative delta means the
counter reset (device reboot) or wrapped — return ``None``, re-baseline, and
emit no point for that tick. Rates divide by *actual elapsed time* between
samples, never the nominal interval, so they are correct at any cadence and
across missed ticks.
"""

from datetime import datetime


def compute_rate(prev_value: float, cur_value: float, dt: float) -> float | None:
    """Per-second rate over one sample interval, or ``None`` when undefined.

    ``None`` on a non-positive ``dt`` (clock anomaly / duplicate tick) or a
    negative delta (counter reset or wrap — reboots are common on test beds,
    wraps are rare, and wrap-compensation would turn every reboot into one
    absurd spike; losing one tick is the better trade).
    """
    if dt <= 0:
        return None
    delta = cur_value - prev_value
    if delta < 0:
        return None
    return delta / dt


class RateTracker:
    """Per-key previous-sample state for counter->rate conversion.

    Shell rate parsers hold one as instance state (parser instances are
    per-target deep copies, so state never leaks across hosts); the SNMP path
    holds one per :class:`~otto.monitor.snmp.SnmpSource`.
    """

    def __init__(self) -> None:
        self._prev: dict[str, tuple[float, datetime]] = {}

    def update(self, key: str, value: float, ts: datetime) -> float | None:
        """Record ``(value, ts)`` for ``key`` and return the rate since the
        previous sample, or ``None`` on first sighting / reset (see
        :func:`compute_rate`)."""
        prev = self._prev.get(key)
        self._prev[key] = (value, ts)
        if prev is None:
            return None
        prev_value, prev_ts = prev
        return compute_rate(prev_value, value, (ts - prev_ts).total_seconds())

    def prune(self, active: set[str]) -> None:
        """Drop state for keys not in ``active`` (e.g. a vanished interface)."""
        for key in list(self._prev):
            if key not in active:
                del self._prev[key]
```

In `src/otto/monitor/parsers.py`: add `from datetime import datetime` to the module-top imports, and extend `ParseContext`:

```python
@dataclass(frozen=True)
class ParseContext:
    """Tick-local input to MetricParser.parse — extensible without signature breaks."""

    core_count: int = 1
    """Number of CPU cores on the target host for this tick. Most parsers ignore
    this; :class:`TopCpuParser` uses it to normalize per-process CPU%."""

    ts: datetime | None = None
    """Collection timestamp for this tick. Rate parsers feed it to their
    :class:`~otto.monitor.rates.RateTracker`; ``None`` (bare construction in
    tests) means the parser falls back to ``datetime.now(tz=timezone.utc)``."""
```

In `src/otto/monitor/collector.py` `_collect_bucket` (line ~277), thread the tick timestamp:

```python
                        ctx=ParseContext(core_count=target.core_count, ts=ts),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/monitor/test_rates.py tests/unit/monitor/test_parsers.py -v`
Expected: all PASS (including the pre-existing parser tests — `ParseContext` change is additive).

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/otto/monitor/rates.py src/otto/monitor/parsers.py src/otto/monitor/collector.py tests/unit/monitor/test_rates.py
git add src/otto/monitor/rates.py src/otto/monitor/parsers.py src/otto/monitor/collector.py tests/unit/monitor/test_rates.py tests/unit/monitor/test_parsers.py
git commit -m "feat(monitor): shared counter->rate helpers + ParseContext.ts

Assisted-by: Claude Fable 5"
```

---

### Task 2: `NetDevParser` — per-interface network throughput

**Files:**
- Modify: `src/otto/monitor/parsers.py` (new class + `DEFAULT_PARSERS`)
- Test: `tests/unit/monitor/test_parsers.py`

**Interfaces:**
- Consumes: `RateTracker` (Task 1).
- Produces: `NetDevParser` with `command = "cat /proc/net/dev"`, series `rx <iface>` / `tx <iface>` (B/s), chart `"Network I/O"`, tab `network`/`Network`. Joins `DEFAULT_PARSERS`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/monitor/test_parsers.py` (import `NetDevParser` in the module-top import block; add the helper near `_top_output`):

```python
def _net_dev_output(eth0: tuple, wlan0: tuple | None = None) -> str:
    """Build /proc/net/dev output. Each tuple: (rx_bytes, rx_pkts, rx_errs,
    rx_drop, tx_bytes, tx_pkts, tx_errs, tx_drop)."""

    def line(name: str, v: tuple) -> str:
        rx = f"{v[0]:>8} {v[1]:>7} {v[2]:>4} {v[3]:>4}    0     0          0         0"
        tx = f"{v[4]:>8} {v[5]:>7} {v[6]:>4} {v[7]:>4}    0     0       0          0"
        return f"{name:>6}: {rx} {tx}\n"

    out = (
        "Inter-|   Receive                                                |  Transmit\n"
        " face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed\n"
        + line("lo", (999, 9, 0, 0, 999, 9, 0, 0))
        + line("eth0", eth0)
    )
    if wlan0 is not None:
        out += line("wlan0", wlan0)
    return out


class TestNetDevParser:
    def _ctx(self, seconds: int) -> ParseContext:
        from datetime import datetime, timedelta, timezone

        t0 = datetime(2026, 7, 3, 12, 0, 0, tzinfo=timezone.utc)
        return ParseContext(ts=t0 + timedelta(seconds=seconds))

    def test_first_tick_is_baseline_empty(self):
        parser = NetDevParser()
        assert parser.parse(_net_dev_output((1000, 10, 0, 0, 2000, 20, 0, 0)), ctx=self._ctx(0)) == {}

    def test_second_tick_emits_byte_rates(self):
        parser = NetDevParser()
        parser.parse(_net_dev_output((1000, 10, 0, 0, 2000, 20, 0, 0)), ctx=self._ctx(0))
        points = parser.parse(_net_dev_output((6000, 60, 5, 10, 4000, 40, 0, 0)), ctx=self._ctx(5))
        assert points["rx eth0"].value == 1000.0  # (6000-1000)/5
        assert points["tx eth0"].value == 400.0  # (4000-2000)/5
        assert points["rx eth0"].meta == {"Packets": "10.0/s", "Errors": "1.0/s", "Drops": "2.0/s"}

    def test_loopback_is_skipped(self):
        parser = NetDevParser()
        parser.parse(_net_dev_output((0, 0, 0, 0, 0, 0, 0, 0)), ctx=self._ctx(0))
        points = parser.parse(_net_dev_output((500, 5, 0, 0, 500, 5, 0, 0)), ctx=self._ctx(5))
        assert not any("lo" == k.split()[-1] for k in points)

    def test_new_interface_baselines_silently(self):
        parser = NetDevParser()
        parser.parse(_net_dev_output((0, 0, 0, 0, 0, 0, 0, 0)), ctx=self._ctx(0))
        points = parser.parse(
            _net_dev_output((100, 1, 0, 0, 100, 1, 0, 0), wlan0=(50, 1, 0, 0, 50, 1, 0, 0)),
            ctx=self._ctx(5),
        )
        assert "rx wlan0" not in points  # first sighting = baseline
        points = parser.parse(
            _net_dev_output((200, 2, 0, 0, 200, 2, 0, 0), wlan0=(100, 2, 0, 0, 100, 2, 0, 0)),
            ctx=self._ctx(10),
        )
        assert points["rx wlan0"].value == 10.0

    def test_counter_reset_skips_tick(self):
        parser = NetDevParser()
        parser.parse(_net_dev_output((9000, 90, 0, 0, 9000, 90, 0, 0)), ctx=self._ctx(0))
        points = parser.parse(_net_dev_output((10, 1, 0, 0, 10, 1, 0, 0)), ctx=self._ctx(5))
        assert "rx eth0" not in points

    def test_garbage_output_is_empty(self):
        assert NetDevParser().parse("cat: /proc/net/dev: No such file", ctx=self._ctx(0)) == {}

    def test_in_default_parsers(self):
        assert "cat /proc/net/dev" in DEFAULT_PARSERS
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/monitor/test_parsers.py::TestNetDevParser -v`
Expected: FAIL with `ImportError: cannot import name 'NetDevParser'`.

- [ ] **Step 3: Implement**

Add to `src/otto/monitor/parsers.py` after `LoadParser` (also add `from datetime import datetime, timezone` — datetime came in Task 1, add `timezone` — and `from .rates import RateTracker`):

```python
def _rate_meta(rates: dict[str, float | None]) -> dict[str, Any] | None:
    """Format auxiliary counter rates as hover meta; None when nothing rated yet."""
    meta = {label: f"{rate:.1f}/s" for label, rate in rates.items() if rate is not None}
    return meta or None


class NetDevParser(MetricParser):
    """Per-interface network throughput from ``/proc/net/dev`` counter deltas.

    Emits ``rx <iface>`` / ``tx <iface>`` byte rates; packet/error/drop rates
    ride each series' hover meta. The loopback interface is skipped. First
    tick per interface is the rate baseline and emits nothing; a counter
    reset (reboot) skips one tick and re-baselines (see
    :mod:`otto.monitor.rates`).
    """

    y_title = "Throughput"
    unit = "B/s"
    command = "cat /proc/net/dev"
    tab = "network"
    tab_label = "Network"
    chart = "Network I/O"

    def __init__(self) -> None:
        self._rates = RateTracker()

    @override
    def parse(self, output: str, *, ctx: ParseContext) -> dict[str, MetricDataPoint]:
        ts = ctx.ts or datetime.now(tz=timezone.utc)
        result: dict[str, MetricDataPoint] = {}
        active: set[str] = set()
        for line in output.splitlines():
            if ":" not in line:
                continue  # header lines
            iface, _, rest = line.partition(":")
            iface = iface.strip()
            fields = rest.split()
            if iface == "lo" or len(fields) < 16:  # noqa: PLR2004 — /proc/net/dev rows carry 8 rx + 8 tx counters
                continue
            try:
                counters = [float(fields[i]) for i in (0, 1, 2, 3, 8, 9, 10, 11)]
            except ValueError:
                continue
            rx_bytes, rx_pkts, rx_errs, rx_drop, tx_bytes, tx_pkts, tx_errs, tx_drop = counters
            active.update(f"{iface}/{c}" for c in ("rx", "rxp", "rxe", "rxd", "tx", "txp", "txe", "txd"))
            rx_rate = self._rates.update(f"{iface}/rx", rx_bytes, ts)
            rx_aux = {
                "Packets": self._rates.update(f"{iface}/rxp", rx_pkts, ts),
                "Errors": self._rates.update(f"{iface}/rxe", rx_errs, ts),
                "Drops": self._rates.update(f"{iface}/rxd", rx_drop, ts),
            }
            tx_rate = self._rates.update(f"{iface}/tx", tx_bytes, ts)
            tx_aux = {
                "Packets": self._rates.update(f"{iface}/txp", tx_pkts, ts),
                "Errors": self._rates.update(f"{iface}/txe", tx_errs, ts),
                "Drops": self._rates.update(f"{iface}/txd", tx_drop, ts),
            }
            if rx_rate is not None:
                result[f"rx {iface}"] = MetricDataPoint(round(rx_rate, 2), meta=_rate_meta(rx_aux))
            if tx_rate is not None:
                result[f"tx {iface}"] = MetricDataPoint(round(tx_rate, 2), meta=_rate_meta(tx_aux))
        self._rates.prune(active)
        return result
```

Extend `DEFAULT_PARSERS`:

```python
DEFAULT_PARSERS: dict[str, MetricParser] = {
    p.command: p
    for p in [TopCpuParser(), MemParser(), DiskParser(), LoadParser(), NetDevParser()]
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/monitor/test_parsers.py tests/unit/monitor/ -x -q`
Expected: all PASS (the whole monitor unit package — catches anything pinning `DEFAULT_PARSERS` contents).

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/otto/monitor/parsers.py tests/unit/monitor/test_parsers.py
git add src/otto/monitor/parsers.py tests/unit/monitor/test_parsers.py
git commit -m "feat(monitor): NetDevParser — per-interface rx/tx rates on a Network tab

Assisted-by: Claude Fable 5"
```

---

### Task 3: `SocketsParser` — socket counts

**Files:**
- Modify: `src/otto/monitor/parsers.py`
- Test: `tests/unit/monitor/test_parsers.py`

**Interfaces:**
- Consumes: nothing beyond base class.
- Produces: `SocketsParser` with `command = "ss -s"`, series `Established` / `Time-wait`, chart `"Sockets"`, tab `network`. Joins `DEFAULT_PARSERS`.

- [ ] **Step 1: Write the failing tests**

```python
_SS_OUTPUT = """Total: 201
TCP:   9 (estab 2, closed 3, orphaned 0, timewait 4)

Transport Total     IP        IPv6
RAW       0         0         0
UDP       5         4         1
TCP       6         5         1
"""


class TestSocketsParser:
    def test_parses_estab_and_timewait(self):
        points = SocketsParser().parse(_SS_OUTPUT, ctx=ParseContext())
        assert points["Established"].value == 2.0
        assert points["Time-wait"].value == 4.0

    def test_missing_tool_output_is_empty(self):
        assert SocketsParser().parse("sh: ss: command not found", ctx=ParseContext()) == {}

    def test_in_default_parsers(self):
        assert "ss -s" in DEFAULT_PARSERS
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/monitor/test_parsers.py::TestSocketsParser -v`
Expected: FAIL with `ImportError: cannot import name 'SocketsParser'`.

- [ ] **Step 3: Implement**

```python
class SocketsParser(MetricParser):
    """TCP socket-state counts from the ``TCP:`` summary line of ``ss -s``.

    Hosts without ``ss`` produce a shell error the parser cannot match — the
    series simply never appears (and the collector warns once; see
    parser-health warnings). Swap in a ``netstat``-based parser per host via
    :func:`register_host_parsers` if needed.
    """

    y_title = "Sockets"
    unit = ""
    command = "ss -s"
    tab = "network"
    tab_label = "Network"
    chart = "Sockets"

    _regex = re.compile(r"^TCP:\s+\d+\s+\(estab (?P<estab>\d+),.*timewait (?P<timewait>\d+)")

    @override
    def parse(self, output: str, *, ctx: ParseContext) -> dict[str, MetricDataPoint]:
        for line in output.splitlines():
            m = self._regex.match(line.strip())
            if m:
                return {
                    "Established": MetricDataPoint(float(m["estab"])),
                    "Time-wait": MetricDataPoint(float(m["timewait"])),
                }
        return {}
```

Add `SocketsParser()` to the `DEFAULT_PARSERS` list (after `NetDevParser()`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/monitor/ -x -q`
Expected: all PASS.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/otto/monitor/parsers.py tests/unit/monitor/test_parsers.py
git add src/otto/monitor/parsers.py tests/unit/monitor/test_parsers.py
git commit -m "feat(monitor): SocketsParser — established/time-wait counts from ss -s

Assisted-by: Claude Fable 5"
```

---

### Task 4: `DiskIoParser` — per-device read/write rates

**Files:**
- Modify: `src/otto/monitor/parsers.py`
- Test: `tests/unit/monitor/test_parsers.py`

**Interfaces:**
- Consumes: `RateTracker` (Task 1).
- Produces: `DiskIoParser` with `command = "cat /proc/diskstats"`, series `read <dev>` / `write <dev>` (B/s), chart `"Disk I/O"`, tab `disk`. Joins `DEFAULT_PARSERS`.

- [ ] **Step 1: Write the failing tests**

```python
def _diskstats_output(sda_sectors: tuple[int, int], with_noise: bool = True) -> str:
    """Build /proc/diskstats output; sda_sectors = (sectors_read, sectors_written)."""
    rows = [
        f"   8       0 sda 5000 100 {sda_sectors[0]} 400 3000 200 {sda_sectors[1]} 800 0 900 1200",
        "   8       1 sda1 4000 90 90000 350 2500 150 60000 700 0 800 1000",
    ]
    if with_noise:
        rows += [
            "   7       0 loop0 10 0 80 5 0 0 0 0 0 5 5",
            " 253       0 dm-0 100 0 800 50 100 0 800 50 0 50 100",
            "  11       0 sr0 2 0 8 1 0 0 0 0 0 1 1",
        ]
    return "\n".join(rows) + "\n"


class TestDiskIoParser:
    def _ctx(self, seconds: int) -> ParseContext:
        from datetime import datetime, timedelta, timezone

        t0 = datetime(2026, 7, 3, 12, 0, 0, tzinfo=timezone.utc)
        return ParseContext(ts=t0 + timedelta(seconds=seconds))

    def test_second_tick_emits_byte_rates(self):
        parser = DiskIoParser()
        parser.parse(_diskstats_output((100000, 50000)), ctx=self._ctx(0))
        points = parser.parse(_diskstats_output((100100, 50200)), ctx=self._ctx(5))
        assert points["read sda"].value == 100 * 512 / 5  # sector delta x 512 / dt
        assert points["write sda"].value == 200 * 512 / 5

    def test_partitions_and_virtual_devices_skipped(self):
        parser = DiskIoParser()
        parser.parse(_diskstats_output((0, 0)), ctx=self._ctx(0))
        points = parser.parse(_diskstats_output((512, 512)), ctx=self._ctx(5))
        devices = {k.split()[-1] for k in points}
        assert devices == {"sda"}  # no sda1, loop0, dm-0, sr0

    def test_first_tick_is_baseline_empty(self):
        assert DiskIoParser().parse(_diskstats_output((1, 1)), ctx=self._ctx(0)) == {}

    def test_in_default_parsers(self):
        assert "cat /proc/diskstats" in DEFAULT_PARSERS
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/monitor/test_parsers.py::TestDiskIoParser -v`
Expected: FAIL with `ImportError: cannot import name 'DiskIoParser'`.

- [ ] **Step 3: Implement**

```python
_SECTOR_BYTES = 512  # /proc/diskstats counts 512-byte sectors regardless of device geometry


class DiskIoParser(MetricParser):
    """Per-device read/write throughput from ``/proc/diskstats`` sector deltas.

    Whole devices only: partitions (``sda1``, ``nvme0n1p2``, ``mmcblk0p1``)
    and virtual/noise devices (``loop*``, ``ram*``, ``dm-*``, ``zram*``,
    ``sr*``) are skipped so charts show physical disk activity once.
    """

    y_title = "Disk I/O"
    unit = "B/s"
    command = "cat /proc/diskstats"
    tab = "disk"
    tab_label = "Disk"
    chart = "Disk I/O"

    _skip = re.compile(r"^(?:loop|ram|dm-|zram|sr)")
    _partition = re.compile(r"^(?:[shv]d[a-z]+\d+|nvme\d+n\d+p\d+|mmcblk\d+p\d+)$")

    def __init__(self) -> None:
        self._rates = RateTracker()

    @override
    def parse(self, output: str, *, ctx: ParseContext) -> dict[str, MetricDataPoint]:
        ts = ctx.ts or datetime.now(tz=timezone.utc)
        result: dict[str, MetricDataPoint] = {}
        active: set[str] = set()
        for line in output.splitlines():
            fields = line.split()
            if len(fields) < 10:  # noqa: PLR2004 — device rows carry >= 10 stat fields
                continue
            name = fields[2]
            if self._skip.match(name) or self._partition.match(name):
                continue
            try:
                sectors_read, sectors_written = float(fields[5]), float(fields[9])
            except ValueError:
                continue
            active.update((f"{name}/r", f"{name}/w"))
            read_rate = self._rates.update(f"{name}/r", sectors_read * _SECTOR_BYTES, ts)
            write_rate = self._rates.update(f"{name}/w", sectors_written * _SECTOR_BYTES, ts)
            if read_rate is not None:
                result[f"read {name}"] = MetricDataPoint(round(read_rate, 2))
            if write_rate is not None:
                result[f"write {name}"] = MetricDataPoint(round(write_rate, 2))
        self._rates.prune(active)
        return result
```

Add `DiskIoParser()` to the `DEFAULT_PARSERS` list.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/monitor/ -x -q`
Expected: all PASS.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/otto/monitor/parsers.py tests/unit/monitor/test_parsers.py
git add src/otto/monitor/parsers.py tests/unit/monitor/test_parsers.py
git commit -m "feat(monitor): DiskIoParser — per-device B/s from /proc/diskstats deltas

Assisted-by: Claude Fable 5"
```

---

### Task 5: `PerCoreCpuParser` — per-core busy %

**Files:**
- Modify: `src/otto/monitor/parsers.py`
- Test: `tests/unit/monitor/test_parsers.py`

**Interfaces:**
- Consumes: base class only (jiffies ratios need no wall clock — dt cancels).
- Produces: `PerCoreCpuParser` with `command = "cat /proc/stat"`, series `core 0` … `core N` (%), chart `"Per-core CPU"`, tab `cpu`. Joins `DEFAULT_PARSERS`.

- [ ] **Step 1: Write the failing tests**

```python
def _proc_stat_output(cores: list[tuple[int, int]]) -> str:
    """cores = [(busy_jiffies_excluding_idle, idle_plus_iowait_jiffies), ...].

    Emits the aggregate 'cpu' line (skipped by the parser) plus one cpuN line
    per core: user nice system idle iowait irq softirq steal.
    """
    lines = ["cpu  99999 0 99999 999999 9999 0 0 0 0 0"]
    for n, (busy, idle) in enumerate(cores):
        user, system = busy // 2, busy - busy // 2
        idle_j, iowait = idle // 2, idle - idle // 2
        lines.append(f"cpu{n} {user} 0 {system} {idle_j} {iowait} 0 0 0 0 0")
    lines += ["intr 12345", "ctxt 6789", "procs_running 3", "procs_blocked 1"]
    return "\n".join(lines) + "\n"


class TestPerCoreCpuParser:
    def test_first_tick_is_baseline_empty(self):
        assert PerCoreCpuParser().parse(_proc_stat_output([(100, 900)]), ctx=ParseContext()) == {}

    def test_busy_percent_from_deltas(self):
        parser = PerCoreCpuParser()
        parser.parse(_proc_stat_output([(100, 900), (100, 900)]), ctx=ParseContext())
        # core0: +30 busy / +100 total = 30%; core1: +80 busy / +100 total = 80%
        points = parser.parse(_proc_stat_output([(130, 970), (180, 920)]), ctx=ParseContext())
        assert points["core 0"].value == 30.0
        assert points["core 1"].value == 80.0

    def test_aggregate_cpu_line_skipped(self):
        parser = PerCoreCpuParser()
        parser.parse(_proc_stat_output([(100, 900)]), ctx=ParseContext())
        points = parser.parse(_proc_stat_output([(150, 950)]), ctx=ParseContext())
        assert set(points) == {"core 0"}

    def test_counter_reset_rebaselines(self):
        parser = PerCoreCpuParser()
        parser.parse(_proc_stat_output([(10000, 90000)]), ctx=ParseContext())
        assert parser.parse(_proc_stat_output([(10, 90)]), ctx=ParseContext()) == {}
        points = parser.parse(_proc_stat_output([(60, 140)]), ctx=ParseContext())
        assert points["core 0"].value == 50.0

    def test_in_default_parsers(self):
        assert "cat /proc/stat" in DEFAULT_PARSERS
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/monitor/test_parsers.py::TestPerCoreCpuParser -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Implement**

```python
class PerCoreCpuParser(MetricParser):
    """Per-core busy %% from ``/proc/stat`` jiffies deltas.

    Far cheaper than a second ``top`` run: busy%% = 100 x (1 - Δ(idle+iowait)
    / Δtotal) per ``cpuN`` line. The aggregate ``cpu`` line is skipped —
    :class:`TopCpuParser` already charts overall CPU. Jiffies ratios need no
    wall clock (time cancels), so state is plain previous counters.
    """

    y_title = "Usage %"
    unit = "%"
    command = "cat /proc/stat"
    tab = "cpu"
    tab_label = "CPU"
    chart = "Per-core CPU"

    def __init__(self) -> None:
        self._prev: dict[str, tuple[float, float]] = {}  # core -> (total, idle_all)

    @override
    def parse(self, output: str, *, ctx: ParseContext) -> dict[str, MetricDataPoint]:
        result: dict[str, MetricDataPoint] = {}
        for line in output.splitlines():
            fields = line.split()
            if not fields or not re.fullmatch(r"cpu\d+", fields[0]) or len(fields) < 9:  # noqa: PLR2004 — cpuN rows carry 8 jiffies fields
                continue
            try:
                jiffies = [float(f) for f in fields[1:9]]
            except ValueError:
                continue
            total, idle_all = sum(jiffies), jiffies[3] + jiffies[4]
            core = fields[0].removeprefix("cpu")
            prev = self._prev.get(core)
            self._prev[core] = (total, idle_all)
            if prev is None:
                continue
            d_total, d_idle = total - prev[0], idle_all - prev[1]
            if d_total <= 0 or d_idle < 0:
                continue  # counter reset — re-baseline, skip the tick
            result[f"core {core}"] = MetricDataPoint(round(100.0 * (1 - d_idle / d_total), 2))
        return result
```

Add `PerCoreCpuParser()` to the `DEFAULT_PARSERS` list.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/monitor/ -x -q`
Expected: all PASS.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/otto/monitor/parsers.py tests/unit/monitor/test_parsers.py
git add src/otto/monitor/parsers.py tests/unit/monitor/test_parsers.py
git commit -m "feat(monitor): PerCoreCpuParser — busy%% per core from /proc/stat deltas

Assisted-by: Claude Fable 5"
```

---

### Task 6: `ProcCountParser` — runnable/blocked/total processes

**Files:**
- Modify: `src/otto/monitor/parsers.py`
- Test: `tests/unit/monitor/test_parsers.py`

**Interfaces:**
- Consumes: base class only.
- Produces: `ProcCountParser` with `command = "cat /proc/loadavg /proc/stat"` (two-file cat — the command string must differ from both `LoadParser`'s and `PerCoreCpuParser`'s, since command strings are registry keys), series `Runnable` / `Blocked` / `Total procs`, chart `"Processes"`, tab `cpu`. Joins `DEFAULT_PARSERS`.

- [ ] **Step 1: Write the failing tests**

```python
_LOADAVG_STAT = """0.52 0.58 0.59 3/432 12345
cpu  100 0 100 800 0 0 0 0 0 0
procs_running 3
procs_blocked 2
"""


class TestProcCountParser:
    def test_parses_all_three_series(self):
        points = ProcCountParser().parse(_LOADAVG_STAT, ctx=ParseContext())
        assert points["Runnable"].value == 3.0
        assert points["Blocked"].value == 2.0
        assert points["Total procs"].value == 432.0

    def test_garbage_is_empty(self):
        assert ProcCountParser().parse("cat: /proc/loadavg: error", ctx=ParseContext()) == {}

    def test_in_default_parsers(self):
        assert "cat /proc/loadavg /proc/stat" in DEFAULT_PARSERS
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/monitor/test_parsers.py::TestProcCountParser -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Implement**

```python
class ProcCountParser(MetricParser):
    """Process counts: runnable/total from loadavg field 4, blocked from /proc/stat.

    Cats both files in one command — the command string doubles as the parser
    registry key, so it must differ from ``LoadParser``'s ``cat /proc/loadavg``
    and ``PerCoreCpuParser``'s ``cat /proc/stat``; reading both also gets
    ``procs_blocked`` for free.
    """

    y_title = "Count"
    unit = ""
    command = "cat /proc/loadavg /proc/stat"
    tab = "cpu"
    tab_label = "CPU"
    chart = "Processes"

    _loadavg = re.compile(r"^[\d.]+ [\d.]+ [\d.]+ (?P<run>\d+)/(?P<total>\d+) \d+$")

    @override
    def parse(self, output: str, *, ctx: ParseContext) -> dict[str, MetricDataPoint]:
        result: dict[str, MetricDataPoint] = {}
        for line in output.splitlines():
            m = self._loadavg.match(line.strip())
            if m:
                result["Runnable"] = MetricDataPoint(float(m["run"]))
                result["Total procs"] = MetricDataPoint(float(m["total"]))
            elif line.startswith("procs_blocked"):
                with contextlib.suppress(ValueError, IndexError):
                    result["Blocked"] = MetricDataPoint(float(line.split()[1]))
        return result
```

(Add `import contextlib` to the module-top imports.) Add `ProcCountParser()` to the `DEFAULT_PARSERS` list.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/monitor/ -x -q`
Expected: all PASS.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/otto/monitor/parsers.py tests/unit/monitor/test_parsers.py
git add src/otto/monitor/parsers.py tests/unit/monitor/test_parsers.py
git commit -m "feat(monitor): ProcCountParser — runnable/blocked/total process counts

Assisted-by: Claude Fable 5"
```

---

### Task 7: `MemParser` swap extension

**Files:**
- Modify: `src/otto/monitor/parsers.py` (`MemParser.parse` restructure)
- Test: `tests/unit/monitor/test_parsers.py`

**Interfaces:**
- Consumes: existing `MemParser`.
- Produces: `MemParser.parse` additionally returns a `"Swap"` series (%; same `"Memory Usage"` chart) when swap total > 0. Existing `"Memory Usage"` series behavior unchanged.

- [ ] **Step 1: Write the failing tests**

Add to the existing `TestMemParser` class:

```python
    _FREE_WITH_SWAP = (
        "              total        used        free      shared  buff/cache   available\n"
        "Mem:     16000000000  4000000000  8000000000   100000000  4000000000 11000000000\n"
        "Swap:     2000000000   500000000  1500000000\n"
    )
    _FREE_NO_SWAP = (
        "              total        used        free      shared  buff/cache   available\n"
        "Mem:     16000000000  4000000000  8000000000   100000000  4000000000 11000000000\n"
        "Swap:              0           0           0\n"
    )

    def test_swap_series_present_with_swap(self):
        points = MemParser().parse(self._FREE_WITH_SWAP, ctx=ParseContext())
        assert points["Swap"].value == 25.0  # 0.5G / 2G
        assert points["Swap"].meta == {"Used": "476.8 M", "Total": "1.9 G"}
        assert points["Memory Usage"].value == 25.0  # unchanged existing series

    def test_swap_series_omitted_without_swap(self):
        points = MemParser().parse(self._FREE_NO_SWAP, ctx=ParseContext())
        assert "Swap" not in points  # no flat-0 line for swapless hosts
        assert "Memory Usage" in points
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/monitor/test_parsers.py::TestMemParser -v`
Expected: the two new tests FAIL with `KeyError: 'Swap'` / the first assert failing; existing tests PASS.

- [ ] **Step 3: Implement**

Replace `MemParser.parse` (the early-return-on-`mem:` structure becomes collect-both-lines) — keep the class attributes and docstring, update the docstring's first line to "Parse memory and swap usage % from `free -b` output.":

```python
    @override
    def parse(self, output: str, *, ctx: ParseContext) -> dict[str, MetricDataPoint]:
        result: dict[str, MetricDataPoint] = {}
        for line in output.splitlines():
            lowered = line.lower()
            if not (lowered.startswith(("mem:", "swap:"))):
                continue
            parts = line.split()
            # free -b: <label> total used free [shared buff/cache available]
            if len(parts) < 3:  # noqa: PLR2004 — need at least label, total, used
                continue
            try:
                total, used = float(parts[1]), float(parts[2])
            except ValueError:
                continue
            if total <= 0:
                continue  # swapless host: omit the series, no flat-0 line
            label = "Memory Usage" if lowered.startswith("mem:") else "Swap"
            result[label] = MetricDataPoint(
                value=round(used / total * 100.0, 2),
                meta={"Used": human_readable(used), "Total": human_readable(total)},
            )
        return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/monitor/ -x -q`
Expected: all PASS — including all pre-existing `TestMemParser` cases (behavior for the `Mem:` line is identical).

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/otto/monitor/parsers.py tests/unit/monitor/test_parsers.py
git add src/otto/monitor/parsers.py tests/unit/monitor/test_parsers.py
git commit -m "feat(monitor): Swap series rides free -b in MemParser

Assisted-by: Claude Fable 5"
```

---

### Task 8: Parser-health warnings (shell layers)

**Files:**
- Modify: `src/otto/monitor/collector.py` (`__init__` state + `_process_host_results`)
- Test: `tests/unit/monitor/test_collector_warnings.py` (create)

**Interfaces:**
- Consumes: `CommandResult.retcode` (existing), parser `chart` attribute.
- Produces: collector-internal health state (`_note_health` is reused by Task 9's SNMP layer); exact log lines:
  - ok→failed transition (fires on EVERY distinct outage, so transients stay visible):
    `Monitor: '<command>' failed on <host> (exit <retcode>): <first line of output> — <chart> metrics will be missing`
  - failed→ok transition: `Monitor: '<command>' recovered on <host> after <N> failed tick(s)`
  - never-produced backstop (warn-once): `Monitor: parser <ClassName> ('<command>') has produced no data on <host> after 3 ticks`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/monitor/test_collector_warnings.py`:

```python
"""Parser-health warning layers, driven through the real _process_host_results.

Command failures are EDGE-TRIGGERED: every ok->failed transition warns (so
transient/intermittent failures are logged whenever they happen), every
failed->ok transition warns the recovery with the outage length, and a
sustained outage logs once — not once per tick. The never-produced backstop
stays warn-once."""

from datetime import datetime, timezone

import pytest

from otto.monitor.collector import MetricCollector
from otto.monitor.parsers import MetricDataPoint, MetricParser, ParseContext
from otto.result import CommandResult
from otto.utils import Status

TS = datetime(2026, 7, 3, 12, 0, 0, tzinfo=timezone.utc)


class _NeverParses(MetricParser):
    chart = "Sockets"
    y_title = "Sockets"
    unit = ""
    command = "ss -s"

    def parse(self, output: str, *, ctx: ParseContext) -> dict[str, MetricDataPoint]:
        return {}


class _ParsesFine(MetricParser):
    chart = "Test"
    y_title = "Value"
    unit = ""
    command = "echo 42"

    def parse(self, output: str, *, ctx: ParseContext) -> dict[str, MetricDataPoint]:
        return {"Test": MetricDataPoint(42.0)}


def _failed(cmd: str, retcode: int, output: str) -> CommandResult:
    return CommandResult(Status.Error, value=output, command=cmd, retcode=retcode)


def _ok(cmd: str, output: str = "42\n") -> CommandResult:
    return CommandResult(Status.Success, value=output, command=cmd, retcode=0)


@pytest.fixture
def collector() -> MetricCollector:
    return MetricCollector(targets=[])


async def _tick(collector, parsers, results):
    await collector._process_host_results("test1", TS, results, parsers, ctx=ParseContext())


class TestCommandFailedWarning:
    @pytest.mark.asyncio
    async def test_sustained_failure_warns_once_with_details(self, collector, caplog):
        parsers = {"ss -s": _NeverParses()}
        failed = _failed("ss -s", 127, "sh: ss: command not found")
        with caplog.at_level("WARNING", logger="otto"):
            for _ in range(4):
                await _tick(collector, parsers, [failed])
        warnings = [r for r in caplog.records if "failed on test1" in r.message]
        assert len(warnings) == 1  # edge-triggered: one warning per outage, not per tick
        msg = warnings[0].message
        assert "'ss -s'" in msg
        assert "(exit 127)" in msg
        assert "sh: ss: command not found" in msg
        assert "Sockets metrics will be missing" in msg

    @pytest.mark.asyncio
    async def test_transient_failures_warn_every_time(self, collector, caplog):
        """fail -> ok -> fail is TWO outages: each transition logs. Intermittent
        issues after collection starts must never be swallowed."""
        parsers = {"ss -s": _NeverParses()}
        failed = _failed("ss -s", 1, "read: connection reset")
        with caplog.at_level("WARNING", logger="otto"):
            await _tick(collector, parsers, [failed])
            await _tick(collector, parsers, [_ok("ss -s", "unparseable")])
            await _tick(collector, parsers, [failed])
        failures = [r for r in caplog.records if "failed on test1" in r.message]
        recoveries = [r for r in caplog.records if "recovered on test1" in r.message]
        assert len(failures) == 2
        assert len(recoveries) == 1

    @pytest.mark.asyncio
    async def test_recovery_reports_outage_length(self, collector, caplog):
        parsers = {"ss -s": _NeverParses()}
        failed = _failed("ss -s", 1, "transient")
        with caplog.at_level("WARNING", logger="otto"):
            for _ in range(3):
                await _tick(collector, parsers, [failed])
            await _tick(collector, parsers, [_ok("ss -s", "unparseable")])
        recoveries = [r for r in caplog.records if "recovered on test1" in r.message]
        assert len(recoveries) == 1
        assert "after 3 failed tick(s)" in recoveries[0].message

    @pytest.mark.asyncio
    async def test_late_first_failure_still_warns(self, collector, caplog):
        """A command that worked for many ticks then breaks (network blip long
        after startup) warns on that transition."""
        parsers = {"echo 42": _ParsesFine()}
        with caplog.at_level("WARNING", logger="otto"):
            for _ in range(10):
                await _tick(collector, parsers, [_ok("echo 42")])
            await _tick(collector, parsers, [_failed("echo 42", 1, "blip")])
        assert [r for r in caplog.records if "failed on test1" in r.message]

    @pytest.mark.asyncio
    async def test_success_never_warns(self, collector, caplog):
        parsers = {"echo 42": _ParsesFine()}
        with caplog.at_level("WARNING", logger="otto"):
            await _tick(collector, parsers, [_ok("echo 42")])
        assert not [r for r in caplog.records if "failed on" in r.message]
        assert not [r for r in caplog.records if "recovered on" in r.message]


class TestSilentParserWarning:
    @pytest.mark.asyncio
    async def test_never_produced_by_tick_3_warns_once(self, collector, caplog):
        parsers = {"ss -s": _NeverParses()}
        with caplog.at_level("WARNING", logger="otto"):
            for _ in range(5):
                await _tick(collector, parsers, [_ok("ss -s", "unparseable")])
        warnings = [r for r in caplog.records if "has produced no data" in r.message]
        assert len(warnings) == 1
        assert "_NeverParses" in warnings[0].message
        assert "after 3 ticks" in warnings[0].message

    @pytest.mark.asyncio
    async def test_two_empty_ticks_do_not_warn(self, collector, caplog):
        parsers = {"ss -s": _NeverParses()}
        with caplog.at_level("WARNING", logger="otto"):
            for _ in range(2):
                await _tick(collector, parsers, [_ok("ss -s", "unparseable")])
        assert not [r for r in caplog.records if "has produced no data" in r.message]

    @pytest.mark.asyncio
    async def test_early_data_disarms_later_droughts(self, collector, caplog):
        """Rule is never-produced-by-tick-3, NOT consecutive empties: sparse
        sources legitimately go quiet between writes."""

        class _SparseParser(MetricParser):
            chart = "Sparse"
            y_title = "V"
            unit = ""
            command = "cat sparse"
            _tick = 0

            def parse(self, output, *, ctx):
                self._tick += 1
                return {"Sparse": MetricDataPoint(1.0)} if self._tick == 1 else {}

        parsers = {"cat sparse": _SparseParser()}
        with caplog.at_level("WARNING", logger="otto"):
            for _ in range(6):
                await _tick(collector, parsers, [_ok("cat sparse", "x")])
        assert not [r for r in caplog.records if "has produced no data" in r.message]

    @pytest.mark.asyncio
    async def test_state_is_per_host(self, collector, caplog):
        parsers = {"ss -s": _NeverParses()}
        with caplog.at_level("WARNING", logger="otto"):
            for host in ("test1", "test2"):
                for _ in range(3):
                    await collector._process_host_results(
                        host, TS, [_ok("ss -s", "unparseable")], parsers, ctx=ParseContext()
                    )
        warnings = [r for r in caplog.records if "has produced no data" in r.message]
        assert len(warnings) == 2  # one per host
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/monitor/test_collector_warnings.py -v`
Expected: FAIL — no warnings are emitted yet (`len(warnings) == 1` asserts fail with 0).

- [ ] **Step 3: Implement**

In `MetricCollector.__init__` (after `self._global_interval = None`):

```python
        # Parser-health state, keyed (host_name, command) — or (host_name, oid)
        # for the SNMP layer. Command failures are edge-triggered: _failing
        # counts consecutive failed ticks per key; the 0->1 transition warns,
        # the pop-on-success warns the recovery with the outage length. The
        # never-produced backstop below stays warn-once per run.
        self._failing: dict[tuple[str, str], int] = {}
        self._warned_silent: set[tuple[str, str]] = set()
        self._health_ticks: dict[tuple[str, str], int] = {}
        self._health_produced: set[tuple[str, str]] = set()
```

Add the module-level constant next to `logger`:

```python
# Ticks a parser may produce nothing before the "silent parser" warning fires.
# Deliberately "never produced by tick K", not "K consecutive empties": rate
# parsers legitimately return {} on their baseline tick and sparse log-sourced
# parsers go quiet between writes — only a source that has NEVER produced is
# suspect. K=3 clears the baseline tick with margin.
_SILENT_PARSER_TICKS = 3
```

Replace `_process_host_results`:

```python
    async def _process_host_results(
        self,
        host_name: str,
        ts: datetime,
        cmd_results: "list[CommandResult]",
        parsers: dict[str, MetricParser],
        *,
        ctx: ParseContext,
    ) -> None:
        for cmd_result in cmd_results:
            parser = parsers.get(cmd_result.command)
            if parser is None:
                continue
            key = (host_name, cmd_result.command)
            if cmd_result.retcode != 0:
                # Edge-triggered: warn on each ok->failed transition so
                # transient failures stay visible whenever they happen; a
                # sustained outage logs once (plus its recovery below).
                failed_ticks = self._failing.get(key, 0)
                if failed_ticks == 0:
                    first_line = str(cmd_result.value).strip().splitlines()[:1]
                    logger.warning(
                        "Monitor: '%s' failed on %s (exit %d): %s — %s metrics will be missing",
                        cmd_result.command,
                        host_name,
                        cmd_result.retcode,
                        first_line[0] if first_line else "",
                        parser.chart,
                    )
                self._failing[key] = failed_ticks + 1
            else:
                failed_ticks = self._failing.pop(key, 0)
                if failed_ticks:
                    logger.warning(
                        "Monitor: '%s' recovered on %s after %d failed tick(s)",
                        cmd_result.command,
                        host_name,
                        failed_ticks,
                    )
            points = parser.parse(cmd_result.value, ctx=ctx)
            self._note_health(key, produced=bool(points), what=type(parser).__name__)
            if not points:
                continue
            for label, dp in points.items():
                await self._record_point(host_name, ts, label, dp, parser)

    def _note_health(self, key: tuple[str, str], *, produced: bool, what: str) -> None:
        """Track never-produced-by-tick-K per (host, command/oid); warn once."""
        if produced:
            self._health_produced.add(key)
            return
        if key in self._health_produced or key in self._warned_silent:
            return
        ticks = self._health_ticks.get(key, 0) + 1
        self._health_ticks[key] = ticks
        if ticks >= _SILENT_PARSER_TICKS:
            self._warned_silent.add(key)
            logger.warning(
                "Monitor: parser %s ('%s') has produced no data on %s after %d ticks",
                what,
                key[1],
                key[0],
                _SILENT_PARSER_TICKS,
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/monitor/test_collector_warnings.py tests/unit/monitor/ -x -q`
Expected: all PASS.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/otto/monitor/collector.py tests/unit/monitor/test_collector_warnings.py
git add src/otto/monitor/collector.py tests/unit/monitor/test_collector_warnings.py
git commit -m "feat(monitor): parser-health warnings (edge-triggered failures + never-produced backstop)

Assisted-by: Claude Fable 5"
```

---

### Task 9: SNMP `kind`/`meta_of` + rate state + collector routing

**Files:**
- Modify: `src/otto/monitor/snmp.py` (`SnmpMetric` fields, `SnmpSource.rates`, `process_snmp_values` replacing `points_from_values`)
- Modify: `src/otto/monitor/collector.py` (`_collect_one` returns the raw values dict; `_collect_bucket` `case dict()`; `_process_snmp_results` rewires + SNMP silent-OID warning; `__init__` view filter for `meta_of`)
- Test: `tests/unit/monitor/test_snmp.py`, `tests/unit/monitor/test_collector_warnings.py`, `tests/unit/monitor/test_collector_run.py` (existing SNMP-target tests must stay green)

**Interfaces:**
- Consumes: `RateTracker` (Task 1), `_note_health` (Task 8), `human_readable` (existing).
- Produces: `SnmpMetric.kind: Literal["gauge", "counter"] = "gauge"`, `SnmpMetric.meta_of: str | None = None`;
  `SnmpSource.rates: RateTracker`;
  `process_snmp_values(values: dict[str, float | None], *, rates: RateTracker, ts: datetime) -> list[tuple[str, MetricDataPoint, SnmpMetric]]` (module function in `snmp.py`; **replaces `points_from_values`, which is deleted**). Task 10's descriptors rely on `kind`/`meta_of`.

- [ ] **Step 1: Write the failing tests**

In `tests/unit/monitor/test_snmp.py`, replace the `TestPointsFromValues` class (and the `points_from_values` import) with:

```python
from datetime import datetime, timedelta, timezone

from otto.monitor.rates import RateTracker
from otto.monitor.snmp import process_snmp_values

T0 = datetime(2026, 7, 3, 12, 0, 0, tzinfo=timezone.utc)


class TestProcessSnmpValues:
    def test_gauge_known_oid_is_scaled_and_labelled(self):
        triples = process_snmp_values({OID_SYS_UPTIME: 12345}, rates=RateTracker(), ts=T0)
        assert len(triples) == 1
        label, point, metric = triples[0]
        assert label == "Uptime"
        assert point.value == 123.45  # sysUpTime is 1/100 s
        assert metric.oid == OID_SYS_UPTIME

    def test_none_values_skipped(self):
        triples = process_snmp_values(
            {OID_SYS_UPTIME: 12345, "1.2.3.4": None}, rates=RateTracker(), ts=T0
        )
        assert [t[0] for t in triples] == ["Uptime"]

    def test_unknown_oid_gets_fallback_descriptor(self):
        triples = process_snmp_values({"1.2.3.4": 7}, rates=RateTracker(), ts=T0)
        label, point, metric = triples[0]
        assert label == "1.2.3.4"
        assert point.value == 7.0

    def test_counter_first_tick_baselines_and_emits_nothing(self):
        register_snmp_metric(
            SnmpMetric(oid="1.2.3.9.1", label="rx test", chart="Net", kind="counter", unit="B/s")
        )
        rates = RateTracker()
        assert process_snmp_values({"1.2.3.9.1": 1000}, rates=rates, ts=T0) == []
        triples = process_snmp_values({"1.2.3.9.1": 6000}, rates=rates, ts=T0 + timedelta(seconds=5))
        assert triples[0][1].value == 1000.0  # (6000-1000)/5

    def test_counter_reset_skips_tick(self):
        register_snmp_metric(
            SnmpMetric(oid="1.2.3.9.2", label="rx r", chart="Net", kind="counter")
        )
        rates = RateTracker()
        process_snmp_values({"1.2.3.9.2": 9000}, rates=rates, ts=T0)
        assert process_snmp_values({"1.2.3.9.2": 10}, rates=rates, ts=T0 + timedelta(seconds=5)) == []

    def test_meta_of_attaches_to_target_series_not_own(self):
        register_snmp_metric(SnmpMetric(oid="1.2.3.9.3", label="fs0 used", chart="Filesystem", unit="B"))
        register_snmp_metric(
            SnmpMetric(oid="1.2.3.9.4", label="Total", chart="Filesystem", unit="B", meta_of="1.2.3.9.3")
        )
        triples = process_snmp_values(
            {"1.2.3.9.3": 1048576, "1.2.3.9.4": 2097152}, rates=RateTracker(), ts=T0
        )
        assert [t[0] for t in triples] == ["fs0 used"]  # meta_of OID charts no series
        assert triples[0][1].meta == {"Total": "2 M"}  # human-readable for unit "B"

    def test_meta_of_target_absent_drops_meta_never_errors(self):
        register_snmp_metric(
            SnmpMetric(oid="1.2.3.9.5", label="Orphan", chart="X", meta_of="1.2.3.9.99")
        )
        assert process_snmp_values({"1.2.3.9.5": 5}, rates=RateTracker(), ts=T0) == []

    def test_gauge_default_kind_unchanged(self):
        assert SnmpMetric(oid="1.2.3", label="x", chart="x").kind == "gauge"
        assert SnmpMetric(oid="1.2.3", label="x", chart="x").meta_of is None
```

Add to `tests/unit/monitor/test_collector_warnings.py`:

```python
class TestSnmpSilentOidWarning:
    @pytest.mark.asyncio
    async def test_never_served_oid_warns_once_by_tick_3(self, collector, caplog):
        from unittest.mock import MagicMock

        from otto.monitor.snmp import SnmpClient, SnmpSource
        from otto.monitor.collector import MonitorTarget

        host = MagicMock()
        host.name = "zeph1"
        target = MonitorTarget(
            host=host,
            snmp=SnmpSource(client=SnmpClient(address="10.0.0.1"), oids=["1.2.3.4.0"]),
        )
        with caplog.at_level("WARNING", logger="otto"):
            for _ in range(5):
                await collector._process_snmp_results(target, TS, {"1.2.3.4.0": None})
        warnings = [r for r in caplog.records if "has produced no data" in r.message]
        assert len(warnings) == 1
        assert "1.2.3.4.0" in warnings[0].message
        assert "zeph1" in warnings[0].message
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/monitor/test_snmp.py tests/unit/monitor/test_collector_warnings.py -v`
Expected: FAIL — `ImportError: cannot import name 'process_snmp_values'`; the warnings test fails on `_process_snmp_results` signature.

- [ ] **Step 3: Implement**

In `src/otto/monitor/snmp.py`:

1. Add imports: `from datetime import datetime`, `from dataclasses import dataclass, field`, `from .parsers import MetricDataPoint, human_readable`, `from .rates import RateTracker`.
2. Extend `SnmpMetric` (after `scale: float = 1.0`):

```python
    kind: Literal["gauge", "counter"] = "gauge"
    """How the varbind is interpreted: a ``gauge`` charts its (scaled) value
    directly; a ``counter`` is monotonic and is converted to a per-second rate
    via the target's :class:`~otto.monitor.rates.RateTracker` (negative delta
    -> re-baseline, skip the tick — see :mod:`otto.monitor.rates`)."""

    meta_of: str | None = None
    """When set to another OID, this descriptor's value is not charted as its
    own series — it is attached to the hover-meta dict of the series produced
    by the ``meta_of`` OID, under this descriptor's ``label``. A ``meta_of``
    target absent this tick simply drops the meta; never an error."""
```

3. Extend `SnmpSource`:

```python
@dataclass(slots=True)
class SnmpSource:
    """A :class:`~otto.monitor.collector.MonitorTarget`'s SNMP collection mode.
    ...existing docstring...
    """

    client: SnmpClient
    oids: list[str]
    rates: RateTracker = field(default_factory=RateTracker)
    """Per-target counter->rate state for ``kind="counter"`` descriptors."""
```

4. **Delete `points_from_values`** and add in its place:

```python
def process_snmp_values(
    values: dict[str, float | None],
    *,
    rates: RateTracker,
    ts: datetime,
) -> list[tuple[str, MetricDataPoint, SnmpMetric]]:
    """Turn one GET's ``{oid: raw_value}`` into chartable ``(label, point, view)`` triples.

    Applies each descriptor's ``scale``; converts ``kind="counter"`` values to
    per-second rates via *rates* (first sighting / reset ticks emit nothing);
    routes ``meta_of`` descriptors into their target series' hover meta instead
    of their own series. OIDs with a ``None`` value are skipped.
    """
    resolved = {oid: resolve_snmp_metric(oid) for oid in values}
    scaled: dict[str, float] = {}
    for oid, raw in values.items():
        if raw is None:
            continue
        metric = resolved[oid]
        value = raw * metric.scale
        if metric.kind == "counter":
            rate = rates.update(oid, value, ts)
            if rate is None:
                continue
            value = rate
        scaled[oid] = round(value, 2)

    meta_map: dict[str, dict[str, str]] = {}
    for oid, value in scaled.items():
        metric = resolved[oid]
        if metric.meta_of is not None:
            formatted = human_readable(value) if metric.unit == "B" else f"{value} {metric.unit}".strip()
            meta_map.setdefault(metric.meta_of, {})[metric.label] = formatted

    triples: list[tuple[str, MetricDataPoint, SnmpMetric]] = []
    for oid, value in scaled.items():
        metric = resolved[oid]
        if metric.meta_of is not None:
            continue
        triples.append((metric.label, MetricDataPoint(value=value, meta=meta_map.get(oid)), metric))
    return triples
```

In `src/otto/monitor/collector.py`:

5. Update the import: `from .snmp import SnmpMetric, SnmpSource, process_snmp_values, resolve_snmp_metric`.
6. `_collect_one`'s SNMP branch returns the raw dict (update its return annotation to `"Results | dict[str, float | None] | None"` and the docstring's SNMP sentence):

```python
        if target.snmp is not None:
            return await asyncio.wait_for(
                target.snmp.client.get(target.snmp.oids),
                timeout,
            )
```

7. `_collect_bucket`'s match arm becomes:

```python
                case dict() as values:
                    await self._process_snmp_results(target, ts, values)
```

8. Rewrite `_process_snmp_results` (signature change — it needs the target for rate state and health keys):

```python
    async def _process_snmp_results(
        self,
        target: MonitorTarget,
        ts: datetime,
        values: dict[str, float | None],
    ) -> None:
        if target.snmp is None:  # routing invariant from _collect_bucket; keeps ty narrow
            return
        host_name = target.host.name
        for oid, raw in values.items():
            self._note_health((host_name, oid), produced=raw is not None, what="SNMP OID")
        triples = process_snmp_values(values, rates=target.snmp.rates, ts=ts)
        for label, dp, view in triples:
            await self._record_point(host_name, ts, label, dp, view)
```

9. In `__init__`'s view-building loop, skip meta_of descriptors (they chart no series):

```python
            if t.snmp is not None:
                for oid in t.snmp.oids:
                    if oid not in seen_oids:
                        seen_oids.add(oid)
                        view = resolve_snmp_metric(oid)
                        if view.meta_of is None:
                            snmp_views.append(view)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/monitor/ -x -q`
Expected: all PASS — including `test_collector_run.py`'s existing SNMP-target tests (they exercise the dict routing end to end). Also grep for stragglers: `grep -rn "points_from_values" src/ tests/ docs/` must return nothing.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/otto/monitor/snmp.py src/otto/monitor/collector.py tests/unit/monitor/
git add src/otto/monitor/snmp.py src/otto/monitor/collector.py tests/unit/monitor/test_snmp.py tests/unit/monitor/test_collector_warnings.py
git commit -m "feat(monitor)!: SNMP counter->rate + meta_of descriptors (process_snmp_values replaces points_from_values)

Assisted-by: Claude Fable 5"
```

---

### Task 10: Enterprise net/fs OID descriptors + named bundles + factory wiring

**Files:**
- Modify: `src/otto/monitor/snmp.py` (subtree constants, descriptor builders, `expand_oid_bundles`), `src/otto/monitor/factory.py:56`
- Test: `tests/unit/monitor/test_snmp.py`, `tests/unit/monitor/test_monitor_factory.py`

**Interfaces:**
- Consumes: `register_snmp_metric`, `SnmpMetric.kind`/`meta_of` (Task 9).
- Produces: `net_oids(index: int) -> list[str]`, `fs_oids(index: int) -> list[str]`, `CORE_OIDS: tuple[str, ...]`, `expand_oid_bundles(oids: Sequence[str]) -> list[str]`. Lab data may now list `otto-core`, `otto-net[:N]`, `otto-fs[:N]` in `snmp.oids`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/monitor/test_snmp.py`:

```python
from otto.monitor.snmp import CORE_OIDS, expand_oid_bundles, fs_oids, get_snmp_metric, net_oids


class TestOidBundles:
    def test_raw_oids_pass_through_untouched(self):
        assert expand_oid_bundles(["1.3.6.1.2.1.1.3.0"]) == ["1.3.6.1.2.1.1.3.0"]

    def test_otto_core_expands_to_existing_scalars(self):
        expanded = expand_oid_bundles(["otto-core"])
        assert expanded == list(CORE_OIDS)
        assert OID_SYS_UPTIME in expanded
        assert len(expanded) == 5  # uptime, cpu, heap used/free, threads

    def test_otto_net_default_is_one_interface(self):
        assert expand_oid_bundles(["otto-net"]) == net_oids(0)
        assert len(net_oids(0)) == 6  # rx/tx bytes+packets, errors, drops

    def test_otto_net_count_expands_indices(self):
        assert expand_oid_bundles(["otto-net:2"]) == net_oids(0) + net_oids(1)

    def test_otto_fs_expands(self):
        assert expand_oid_bundles(["otto-fs:1"]) == fs_oids(0)
        assert len(fs_oids(0)) == 2  # used, total

    def test_bundles_and_raw_mix(self):
        expanded = expand_oid_bundles(["otto-core", "1.2.3.4.0"])
        assert expanded == [*CORE_OIDS, "1.2.3.4.0"]

    def test_unknown_bundle_raises_with_known_names(self):
        with pytest.raises(ValueError, match=r"otto-typo.*otto-core.*otto-fs.*otto-net"):
            expand_oid_bundles(["otto-typo"])

    def test_expansion_registers_descriptors(self):
        expand_oid_bundles(["otto-net:1", "otto-fs:1"])
        rx_bytes = get_snmp_metric(net_oids(0)[0])
        assert rx_bytes is not None
        assert rx_bytes.kind == "counter"
        assert rx_bytes.label == "rx if0"
        assert rx_bytes.tab == "network"
        rx_packets = get_snmp_metric(net_oids(0)[2])
        assert rx_packets.meta_of == net_oids(0)[0]  # packets ride the byte series
        fs_used = get_snmp_metric(fs_oids(0)[0])
        assert fs_used.kind == "gauge"
        assert fs_used.tab == "storage"
        fs_total = get_snmp_metric(fs_oids(0)[1])
        assert fs_total.meta_of == fs_oids(0)[0]
```

Add to `tests/unit/monitor/test_monitor_factory.py` (mirror the mock-host construction used by `test_snmp_host_becomes_snmp_target` at the top of that class — same MagicMock host shape, `snmp` attribute carrying `oids`):

```python
    def test_bundle_names_expand_at_target_construction(self):
        from otto.monitor.snmp import CORE_OIDS

        host = MagicMock()
        host.snmp = MagicMock(oids=("otto-core",), port=161, community="public", version="2c", address=None)
        host.ip = "10.0.0.9"
        host.address_for = MagicMock(return_value="10.0.0.9")
        collector = build_monitor_collector([host])
        assert collector._targets[0].snmp.oids == list(CORE_OIDS)
```

(Adjust the mock-host construction to exactly match the existing tests in this file — read `test_snmp_host_becomes_snmp_target` first and copy its host setup.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/monitor/test_snmp.py::TestOidBundles tests/unit/monitor/test_monitor_factory.py -v`
Expected: FAIL with `ImportError: cannot import name 'expand_oid_bundles'`.

- [ ] **Step 3: Implement**

In `src/otto/monitor/snmp.py` (after `_register_builtin_metrics()`):

```python
# ---------------------------------------------------------------------------
# Enterprise subtrees for indexed per-interface / per-filesystem scalars
# ---------------------------------------------------------------------------
# The firmware agent serves these as plain scalars (one OID per value, indexed
# by a small integer the agent assigns, 0-based, stable per build). No table
# walk: a small agent has a known, fixed set of interfaces/filesystems, and
# plain GET keeps both sides trivial. This table IS the manager<->agent
# contract, exactly as the .1 subtree comment above is for the core scalars.

CORE_OIDS: tuple[str, ...] = (
    OID_SYS_UPTIME,
    f"{_OTTO_BASE}.1.1.0",  # overall CPU
    f"{_OTTO_BASE}.1.2.0",  # heap used
    f"{_OTTO_BASE}.1.3.0",  # heap free
    f"{_OTTO_BASE}.1.4.0",  # threads
)


def net_oids(index: int) -> list[str]:
    """The six network OIDs for interface *index*: rx/tx bytes, rx/tx packets, errors, drops."""
    return [f"{_OTTO_BASE}.2.{index}.{leaf}.0" for leaf in range(1, 7)]


def fs_oids(index: int) -> list[str]:
    """The two filesystem OIDs for filesystem *index*: bytes used, bytes total."""
    return [f"{_OTTO_BASE}.3.{index}.{leaf}.0" for leaf in (1, 2)]


def _register_net_metrics(index: int) -> None:
    """Register descriptors for interface *index* (idempotent — always overwrites)."""
    rx, tx, rx_p, tx_p, errs, drops = net_oids(index)

    def _m(oid: str, label: str, chart: str, unit: str, **kw: object) -> SnmpMetric:
        return SnmpMetric(
            oid=oid,
            label=label,
            chart=chart,
            unit=unit,
            kind="counter",
            tab="network",
            tab_label="Network",
            **kw,
        )

    for metric in (
        _m(rx, f"rx if{index}", "Network I/O", "B/s", y_title="Throughput"),
        _m(tx, f"tx if{index}", "Network I/O", "B/s", y_title="Throughput"),
        _m(rx_p, "Packets", "Network I/O", "pkt/s", meta_of=rx),
        _m(tx_p, "Packets", "Network I/O", "pkt/s", meta_of=tx),
        _m(errs, f"errors if{index}", "Net errors", "err/s", y_title="Rate"),
        _m(drops, f"drops if{index}", "Net errors", "drop/s", y_title="Rate"),
    ):
        register_snmp_metric(metric)


def _register_fs_metrics(index: int) -> None:
    """Register descriptors for filesystem *index* (idempotent — always overwrites)."""
    used, total = fs_oids(index)
    for metric in (
        SnmpMetric(
            oid=used,
            label=f"fs{index} used",
            chart="Filesystem",
            y_title="Bytes",
            unit="B",
            tab="storage",
            tab_label="Storage",
        ),
        SnmpMetric(
            oid=total,
            label="Total",
            chart="Filesystem",
            unit="B",
            tab="storage",
            tab_label="Storage",
            meta_of=used,
        ),
    ):
        register_snmp_metric(metric)


_BUNDLE_RE = re.compile(r"^(?P<name>[a-z-]+)(?::(?P<count>[1-9]\d*))?$")
_BUNDLE_NAMES = ("otto-core", "otto-fs[:N]", "otto-net[:N]")


def expand_oid_bundles(oids: "Sequence[str]") -> list[str]:
    """Expand named OID bundles in a host's ``snmp.oids`` list into raw OIDs.

    Raw OIDs (anything starting with a digit) pass through untouched, so
    existing lab data keeps working. ``otto-net:N`` / ``otto-fs:N`` expand to
    interfaces / filesystems ``0..N-1`` (``:N`` defaults to 1) and register
    the matching descriptors as a side effect — expansion is the moment the
    set of live indices is known. Unknown bundle names raise loudly.
    """
    out: list[str] = []
    for entry in oids:
        if entry[:1].isdigit():
            out.append(entry)
            continue
        m = _BUNDLE_RE.match(entry)
        name = m["name"] if m else entry
        count = int(m["count"]) if m and m["count"] else 1
        if name == "otto-core":
            out.extend(CORE_OIDS)
        elif name == "otto-net":
            for i in range(count):
                _register_net_metrics(i)
                out.extend(net_oids(i))
        elif name == "otto-fs":
            for i in range(count):
                _register_fs_metrics(i)
                out.extend(fs_oids(i))
        else:
            raise ValueError(
                f"Unknown SNMP OID bundle {entry!r}; known bundles: {', '.join(_BUNDLE_NAMES)}"
            )
    return out
```

(Add `import re` and `from collections.abc import Sequence` to snmp.py's module-top imports.)

In `src/otto/monitor/factory.py`, change the `SnmpSource` construction (line ~56):

```python
                    snmp=SnmpSource(client=client, oids=expand_oid_bundles(snmp.oids)),
```

and add `expand_oid_bundles` to the `from .snmp import ...` line.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/monitor/ -x -q`
Expected: all PASS.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/otto/monitor/snmp.py src/otto/monitor/factory.py tests/unit/monitor/
git add src/otto/monitor/snmp.py src/otto/monitor/factory.py tests/unit/monitor/test_snmp.py tests/unit/monitor/test_monitor_factory.py
git commit -m "feat(monitor): enterprise net/fs OID contract + named SNMP bundles (otto-core/net/fs)

Assisted-by: Claude Fable 5"
```

---

### Task 11: Host-pattern (regex) parser registration

**Files:**
- Modify: `src/otto/monitor/parsers.py` (`register_host_parsers` + `get_host_parsers` + new pattern registry)
- Test: `tests/unit/monitor/test_parsers.py`

**Interfaces:**
- Consumes: existing `HOST_PARSERS` / `Registry`.
- Produces: `register_host_parsers(host_id: str | re.Pattern[str], parsers: dict[str, MetricParser]) -> None` (str = exact, unchanged; Pattern = fullmatch). Resolution precedence: exact > pattern > project-level > defaults; two matching patterns raise `ValueError`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/monitor/test_parsers.py`:

```python
class TestHostPatternRegistry:
    """register_host_parsers with re.Pattern: fullmatch scoping + loud ambiguity."""

    def _parsers(self) -> dict[str, MetricParser]:
        return {_SocketParser.command: _SocketParser()}

    def test_pattern_matches_family_of_hosts(self):
        register_host_parsers(re.compile(r"pat-family-.*"), self._parsers())
        assert _SocketParser.command in get_host_parsers("pat-family-01")
        assert _SocketParser.command in get_host_parsers("pat-family-02")

    def test_fullmatch_not_search(self):
        register_host_parsers(re.compile("pat-exact"), self._parsers())
        assert _SocketParser.command in get_host_parsers("pat-exact")
        assert _SocketParser.command not in get_host_parsers("my-pat-exact-2")

    def test_exact_registration_shadows_pattern(self):
        register_host_parsers(re.compile(r"pat-shadow-.*"), self._parsers())
        register_host_parsers("pat-shadow-1", dict(DEFAULT_PARSERS))
        assert _SocketParser.command not in get_host_parsers("pat-shadow-1")
        assert _SocketParser.command in get_host_parsers("pat-shadow-2")

    def test_two_matching_patterns_raise(self):
        register_host_parsers(re.compile(r"pat-ambig-.*"), self._parsers())
        register_host_parsers(re.compile(r"pat-ambig-0\d"), self._parsers())
        with pytest.raises(ValueError, match="matches multiple parser patterns"):
            get_host_parsers("pat-ambig-01")

    def test_no_match_falls_through_to_defaults(self):
        register_host_parsers(re.compile(r"pat-nomatch-.*"), self._parsers())
        assert get_host_parsers("unrelated-host").keys() == default_catalog().keys()

    def test_pattern_result_is_a_deep_copy(self):
        register_host_parsers(re.compile(r"pat-copy-.*"), self._parsers())
        a = get_host_parsers("pat-copy-1")
        b = get_host_parsers("pat-copy-2")
        assert a[_SocketParser.command] is not b[_SocketParser.command]
```

(Add `import re` and the `default_catalog` import to the test module if not present. Use pattern strings unique to this class — the registries are process-global.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/monitor/test_parsers.py::TestHostPatternRegistry -v`
Expected: FAIL — `register_host_parsers` rejects/ignores the Pattern (Registry key type error or exact-lookup miss).

- [ ] **Step 3: Implement**

In `src/otto/monitor/parsers.py` (add `import re` at module top; it is already imported — verify):

```python
# Pattern-scoped parser sets, keyed by pattern.pattern. Unlike HOST_PARSERS
# (re-registering a host_id is normal usage), registering the same pattern
# string twice is a config bug and raises loudly.
HOST_PATTERN_PARSERS: Registry[tuple["re.Pattern[str]", dict[str, "MetricParser"]]] = Registry(
    "host-pattern parser set", register_hint="otto.monitor.parsers.register_host_parsers()"
)


def register_host_parsers(
    host_id: "str | re.Pattern[str]", parsers: dict[str, "MetricParser"]
) -> None:
    """Associate a custom parser dict with a host ID or a host-ID pattern.

    A plain string is an exact host ID (the key in ``lab.hosts``) — this is a
    total replacement for that host and may be re-registered freely. A compiled
    ``re.Pattern`` scopes the dict to every host whose id ``fullmatch``es —
    one registration covers a family of hosts (e.g.
    ``re.compile(r"busybox-.*")``). Precedence: exact id > pattern >
    project-level > defaults; two patterns matching the same host raise at
    resolution time. Call from an init module listed in ``.otto/settings.toml``.

    Hosts with no registered parsers automatically fall back to DEFAULT_PARSERS.
    """
    if isinstance(host_id, re.Pattern):
        HOST_PATTERN_PARSERS.register(host_id.pattern, (host_id, parsers), origin=caller_module())
        return
    HOST_PARSERS.register(host_id, parsers, overwrite=True, origin=caller_module())


def get_host_parsers(host_id: str) -> dict[str, "MetricParser"]:
    """Return the parser dict for *host_id*: exact > pattern > project-level > defaults.

    An exact registration wins outright for its host_id — it is a total
    replacement, not merged with anything else, and shadows any pattern.
    Otherwise a single ``fullmatch``ing pattern registration wins the same
    way; two or more matching patterns raise (no import-order-dependent
    silent winner). With neither, project-level parsers (see
    :func:`register_parsers`) are merged over DEFAULT_PARSERS. Non-raising
    for unregistered hosts by design: no registration at all is normal.
    """
    if host_id in HOST_PARSERS:
        return copy.deepcopy(HOST_PARSERS.get(host_id))
    matches = [
        (key, parsers)
        for key, (pattern, parsers) in HOST_PATTERN_PARSERS.items()
        if pattern.fullmatch(host_id)
    ]
    if len(matches) > 1:
        patterns = ", ".join(repr(key) for key, _ in matches)
        raise ValueError(
            f"Host {host_id!r} matches multiple parser patterns ({patterns}); "
            "register an exact host id to disambiguate"
        )
    if matches:
        return copy.deepcopy(matches[0][1])
    return copy.deepcopy(default_catalog())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/monitor/ -x -q`
Expected: all PASS (existing `TestHostParserRegistry` exact-string behavior unchanged).

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/otto/monitor/parsers.py tests/unit/monitor/test_parsers.py
git add src/otto/monitor/parsers.py tests/unit/monitor/test_parsers.py
git commit -m "feat(monitor): regex host patterns for register_host_parsers (fullmatch, loud ambiguity)

Assisted-by: Claude Fable 5"
```

---

### Task 12: `UptimeParser` example + two-host scoping integration test

**Files:**
- Create: `src/otto/examples/monitor.py`
- Modify: `src/otto/examples/__init__.py` (docstring module list)
- Test: `tests/unit/monitor/test_scoping.py` (create)

**Interfaces:**
- Consumes: `MetricParser`, `register_host_parsers`, `build_monitor_collector`.
- Produces: `otto.examples.monitor.UptimeParser` with `command = "cat /proc/uptime"`, single `"Uptime"` series (seconds). Task 13's init module imports it.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/monitor/test_scoping.py`:

```python
"""Per-host parser scoping through the full registry -> factory -> collector path.

Two mock shell hosts; UptimeParser registered for host A only. Proves the
executed third-party extension path (registration -> get_host_parsers ->
build_monitor_collector -> series -> /api/meta) AND its scoping: the host
that did NOT register keeps exactly the defaults.
"""

import asyncio
import contextlib
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from otto.examples.monitor import UptimeParser
from otto.logger.mode import LogMode
from otto.monitor.factory import build_monitor_collector
from otto.monitor.parsers import DEFAULT_PARSERS, register_host_parsers
from otto.result import CommandResult, Results
from otto.utils import Status

_CANNED = {
    "cat /proc/uptime": "12345.67 23456.78\n",
    "free -b": (
        "              total        used        free\n"
        "Mem:     16000000000  4000000000  8000000000\n"
        "Swap:              0           0           0\n"
    ),
}


def _make_host(name: str, host_id: str) -> MagicMock:
    host = MagicMock()
    host.name = name
    host.id = host_id
    host.snmp = None
    host.log = LogMode.QUIET

    async def _run(cmds, timeout=None):
        return Results.collect(
            [
                CommandResult(Status.Success, value=_CANNED.get(cmd, ""), command=cmd, retcode=0)
                for cmd in cmds
            ]
        )

    host.run = AsyncMock(side_effect=_run)
    return host


class TestPerHostScoping:
    def test_uptime_parser_parses(self):
        from otto.monitor.parsers import ParseContext

        points = UptimeParser().parse("12345.67 23456.78\n", ctx=ParseContext())
        assert set(points) == {"Uptime"}
        assert points["Uptime"].value == 12345.67

    @pytest.mark.asyncio
    async def test_registered_host_gets_uptime_unregistered_does_not(self):
        register_host_parsers(
            "scoping-host-a",
            {**DEFAULT_PARSERS, UptimeParser().command: UptimeParser()},
        )
        host_a = _make_host("scope_a", "scoping-host-a")
        host_b = _make_host("scope_b", "scoping-host-b")
        collector = build_monitor_collector([host_a, host_b])
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(
                collector.run(interval=timedelta(seconds=0.05), duration=timedelta(seconds=0.2)),
                timeout=5,
            )
        series = set(collector._store.series)
        assert "scope_a/Uptime" in series          # registered host gets the custom metric
        assert "scope_b/Uptime" not in series      # unregistered host does NOT
        assert "scope_b/Memory Usage" in series    # ...and keeps the untouched defaults
        meta = collector.get_meta_model()
        assert any(m.chart == "Uptime" for m in meta.metrics)  # /api/meta grew the chart
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/monitor/test_scoping.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'otto.examples.monitor'`.

- [ ] **Step 3: Implement**

Create `src/otto/examples/monitor.py`:

```python
"""A minimal custom metric parser — the template for writing your own.

To chart a metric otto doesn't ship, subclass
:class:`~otto.monitor.parsers.MetricParser`: set the presentation attributes,
set ``command`` to the exact shell command to run each tick, and implement
``parse()`` to turn that command's output into labelled numeric points.
Then register it from an init module listed in ``.otto/settings.toml`` —
per host (exact id or ``re.compile`` pattern) or project-wide::

    from otto.examples.monitor import UptimeParser
    from otto.monitor.parsers import DEFAULT_PARSERS, register_host_parsers

    register_host_parsers(
        "router1",
        {**DEFAULT_PARSERS, UptimeParser().command: UptimeParser()},
    )

otto's own test suite registers this parser exactly that way (see
``tests/repo1/pylib/repo1_monitor_uptime.py``), so the example is executed,
not just documented.
"""

from typing_extensions import override

from ..monitor.parsers import MetricDataPoint, MetricParser, ParseContext


class UptimeParser(MetricParser):
    """Chart host uptime in seconds from ``cat /proc/uptime``.

    ``/proc/uptime`` holds two floats — seconds since boot and aggregate idle
    seconds; the first is the metric. Returns an empty dict when the output
    doesn't parse (the series simply doesn't appear that tick).
    """

    y_title = "Uptime"
    unit = "s"
    command = "cat /proc/uptime"
    chart = "Uptime"

    @override
    def parse(self, output: str, *, ctx: ParseContext) -> dict[str, MetricDataPoint]:
        try:
            return {"Uptime": MetricDataPoint(round(float(output.split()[0]), 2))}
        except (IndexError, ValueError):
            return {}
```

In `src/otto/examples/__init__.py`, add to the docstring's module list:

```
- :mod:`otto.examples.monitor` — a custom metric parser for ``otto monitor``.
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/monitor/test_scoping.py tests/unit/monitor/ -x -q`
Expected: all PASS.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/otto/examples/monitor.py tests/unit/monitor/test_scoping.py
git add src/otto/examples/monitor.py src/otto/examples/__init__.py tests/unit/monitor/test_scoping.py
git commit -m "feat(examples): UptimeParser — executed custom-parser template + scoping integration test

Assisted-by: Claude Fable 5"
```

---

### Task 13: Subprocess e2e — init-module registration + scoping

**Files:**
- Create: `tests/repo1/pylib/repo1_monitor_uptime.py`
- Modify: `tests/repo1/.otto/settings.toml` (init list), `tests/e2e/monitor/test_monitor_e2e.py`

**Interfaces:**
- Consumes: `UptimeParser` (Task 12), the e2e module's existing `_start_monitor` / `_has_metric_rows` / `monitor_host` helpers (`tests/e2e/monitor/test_monitor_e2e.py:61-139`).
- Produces: env-gated init module `repo1_monitor_uptime` (no-op for every other repo1 consumer when `OTTO_E2E_UPTIME_HOST` is unset).

- [ ] **Step 1: Create the init module**

Create `tests/repo1/pylib/repo1_monitor_uptime.py`:

```python
"""Init module proving per-host monitor-parser scoping end to end.

Registers :class:`otto.examples.monitor.UptimeParser` for the host id named
by ``OTTO_E2E_UPTIME_HOST``. The default id matches no lab host, making this
module a deliberate no-op for every other test that bootstraps repo1 — the
registration sits in HOST_PARSERS but is never looked up.
"""

import os

from otto.examples.monitor import UptimeParser
from otto.monitor.parsers import DEFAULT_PARSERS, register_host_parsers

register_host_parsers(
    os.environ.get("OTTO_E2E_UPTIME_HOST", "e2e-uptime-unregistered"),
    {**DEFAULT_PARSERS, UptimeParser().command: UptimeParser()},
)
```

In `tests/repo1/.otto/settings.toml`, extend the init list:

```toml
init = [
    'repo1_instructions',
    'custom_hosts',
    'repo1_monitor_uptime',
]
```

- [ ] **Step 2: Extend `_start_monitor` with env passthrough**

Read `tests/e2e/monitor/test_monitor_e2e.py:61-99`. Add a keyword-only parameter `extra_env: "dict[str, str] | None" = None` to `_start_monitor`, and immediately after the function's existing subprocess-environment construction, apply:

```python
    if extra_env:
        env.update(extra_env)
```

(If the helper currently passes `env=...` inline to `Popen`, hoist it to a local `env` dict first.)

- [ ] **Step 3: Write the failing e2e test**

Add to `tests/e2e/monitor/test_monitor_e2e.py`, following `test_monitor_collects_and_persists`'s structure exactly (same start/SIGINT/wait choreography and DB location conventions; reuse its row-reading approach — copy the `SELECT` used by `_has_metric_rows` and add the `label`/`host` filters, matching that helper's actual table/column names):

```python
def _uptime_rows(db_path: Path, host: str) -> int:
    """Count persisted Uptime points for *host* (schema per _has_metric_rows)."""
    with contextlib.closing(sqlite3.connect(db_path)) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM metrics WHERE host = ? AND label = 'Uptime'", (host,)
        ).fetchone()[0]


def test_per_host_parser_scoping_via_init_module(monitor_host: str, tmp_path: Path) -> None:
    """UptimeParser registered (via repo1's init module) for the leased host id
    produces Uptime rows; registered for a non-existent id, the same host gets
    only defaults. Two runs = both halves of the scoping guarantee through the
    real subprocess path (settings.toml init -> registry -> collector -> DB).
    """
    # Run 1: registration targets the leased host -> Uptime present.
    db_registered = tmp_path / "registered.db"
    _run_monitor_briefly(  # same Popen->ticks->SIGINT choreography as the existing test
        monitor_host, db_registered, extra_env={"OTTO_E2E_UPTIME_HOST": monitor_host}
    )
    assert _uptime_rows(db_registered, monitor_host) > 0, (
        f"host {monitor_host} registered UptimeParser but produced no Uptime rows"
    )

    # Run 2: registration targets a host id that matches nothing -> no Uptime,
    # defaults intact.
    db_unregistered = tmp_path / "unregistered.db"
    _run_monitor_briefly(monitor_host, db_unregistered, extra_env=None)
    assert _uptime_rows(db_unregistered, monitor_host) == 0, (
        "unregistered host must NOT get the custom parser"
    )
    assert _has_metric_rows(db_unregistered), "default parsers must still produce rows"
```

`_run_monitor_briefly` is a small extraction of the existing test's start→wait-for-ticks→SIGINT→wait body parameterized by `(host, db_path, extra_env)` — factor it out of `test_monitor_collects_and_persists` so both tests share it (keep that test's assertions where they are; only the process choreography moves).

- [ ] **Step 4: Run the e2e (requires the veggies bed)**

Run: `uv run pytest tests/e2e/monitor/test_monitor_e2e.py -v`
Expected: both tests PASS against a leased bed host. If the bed is unreachable the test must FAIL with the host-named lease error — never skip. Do not kill a slow live-bed run at a tight timeout; let it finish.

- [ ] **Step 5: Verify repo1 ripple + lint + commit**

Run: `uv run pytest tests/unit -q -x -k "repo1 or settings or init_module"` and `uv run pytest tests/integration -q -x -k repo1` — the init-list change must not break other repo1 consumers (the module is env-gated to a no-op).

```bash
uv run ruff check tests/repo1/pylib/repo1_monitor_uptime.py tests/e2e/monitor/test_monitor_e2e.py
git add tests/repo1/pylib/repo1_monitor_uptime.py tests/repo1/.otto/settings.toml tests/e2e/monitor/test_monitor_e2e.py
git commit -m "test(monitor): e2e per-host parser scoping via repo1 init module

Assisted-by: Claude Fable 5"
```

---

### Task 14: Documentation

**Files:**
- Modify: `docs/guide/monitor.md`, `docs/guide/lab-config.md`

**Interfaces:**
- Consumes: everything shipped in Tasks 1–13 (names/commands/OIDs must match the code exactly).
- Produces: user-facing docs; the guide's long-standing "network metrics" promise becomes true.

- [ ] **Step 1: Read both pages first**

Read `docs/guide/monitor.md` and `docs/guide/lab-config.md` end to end; match their existing heading levels, table style, and cross-reference syntax (MyST). Grep for any now-stale claims: `grep -n "network" docs/guide/monitor.md`.

- [ ] **Step 2: Update `docs/guide/monitor.md`**

Content requirements (adapt wording to the page's voice):

1. **Built-in Unix metrics table** — replace/extend the existing parser table with all nine parsers (command, series, chart, tab): top-CPU, memory+swap (`free -b`), disk usage, load, network I/O (`cat /proc/net/dev`), sockets (`ss -s`), disk I/O (`cat /proc/diskstats`), per-core CPU (`cat /proc/stat`), processes (`cat /proc/loadavg /proc/stat`). Note that rate metrics emit nothing on their first tick (baseline) and re-baseline after a host reboot.
2. **Embedded/SNMP metrics section** — the OID contract table from the spec (`.2.<i>.{1..6}.0` network, `.3.<i>.{1..2}.0` filesystem, plus the existing `.1` scalars), explicitly marked as the firmware-facing contract; document `kind` (gauge/counter) semantics and that indexed labels (`rx if0`) can be renamed via `register_snmp_metric`.
3. **Parser-health warnings** — what `Monitor: 'ss -s' failed on test1 (exit 127) ...`, `Monitor: 'ss -s' recovered on test1 after N failed tick(s)`, and `... has produced no data ... after 3 ticks` mean; that failure/recovery warnings are edge-triggered (each transient outage logs its start and end, a sustained outage logs once); and that a missing tool means the series is absent, not an error.
4. **Extension examples** — the executed `UptimeParser` snippet (import from `otto.examples.monitor`, per-host registration), and the worked "these hosts have no `ss`" swap using a pattern:

```python
import re

from otto.monitor.parsers import DEFAULT_PARSERS, register_host_parsers
from my_repo.parsers import NetstatSocketsParser  # your own ss-free implementation

parsers = {k: v for k, v in DEFAULT_PARSERS.items() if k != "ss -s"}
parsers[NetstatSocketsParser.command] = NetstatSocketsParser()
register_host_parsers(re.compile(r"busybox-.*"), parsers)
```

- [ ] **Step 3: Update `docs/guide/lab-config.md`**

In the host `snmp` block reference, document that `oids` accepts named bundles mixed with raw OIDs:

```toml
[hosts.zephyr.snmp]
oids = ["otto-core", "otto-net:2", "otto-fs:1", "1.3.6.1.2.1.1.3.0"]
```

with one line per bundle (`otto-core` = the five core scalars; `otto-net:N` / `otto-fs:N` = interfaces/filesystems `0..N-1`, `:N` defaults to 1; unknown names fail fast at monitor startup).

- [ ] **Step 4: Build docs**

Run: `make docs`
Expected: exit 0, **zero warnings** (Sphinx `-W`). Fix any unresolved xrefs (common: `:class:` targets — use full paths like `~otto.monitor.parsers.MetricParser`). Check the exit code directly — never pipe through `tail`.

- [ ] **Step 5: Commit**

```bash
git add docs/guide/monitor.md docs/guide/lab-config.md
git commit -m "docs(monitor): Phase 3 metrics — Unix parser tables, OID contract, bundles, warnings

Assisted-by: Claude Fable 5"
```

---

### Task 15: Full gate + dashboard/browser ripple check

**Files:**
- Possibly modify: browser-suite pins under `tests/e2e/monitor/dashboard/`, `tests/unit/import_budget/` snapshots.

- [ ] **Step 1: Grep for count/tab pins that the new catalog may break**

```bash
grep -rn "DEFAULT_PARSERS\|tab\b.*cpu\|Network\|len(.*parsers" tests/e2e/monitor/dashboard/ tests/unit/monitor/test_meta_models.py tests/unit/monitor/test_fake_collector.py | head -30
```

Any test pinning the number of tabs/charts or the exact tab list now sees `network` (and, only when SNMP fs bundles are polled, `storage`). Update such pins deliberately — the new expected values are part of this feature, not collateral.

- [ ] **Step 2: Browser suite**

Run: `make dashboard`
Expected: PASS. The React frontend renders from `/api/meta`, so new tabs appear without frontend changes; only pinned expectations should need updating.

- [ ] **Step 3: Full coverage gate**

Run: `make coverage` (needs the web dist — run `make web` first in a fresh worktree; fall back to `make coverage-hostless` only if Node is unavailable, and say so in the report).
Expected: exit 0, coverage ≥ the gate. Triage failures via `uv run python scripts/junit_failures.py` — do not hand-roll JUnit parsing. Watch for the known ambient-env/xdist flakes noted in the repo memories before assuming a regression.

- [ ] **Step 4: Lint + typecheck + import budget**

```bash
uv run nox -s lint
uv run nox -s typecheck
uv run pytest tests/unit/import_budget -q
```

Expected: all green. `ruff format` fallout: re-run `ruff check .` after any formatting. If the import-budget snapshot moved (parsers.py now imports `datetime`/`rates`), regenerate deliberately via `make import-snapshot` and include the change in the commit with a note.

- [ ] **Step 5: Docs gate re-run + final commit**

Run: `make docs` (again, post-fixups).

```bash
git add -A -- tests/ src/ docs/  # ONLY after reviewing git status; never git add -u blind
git status --short   # review: nothing unintended
git commit -m "test(monitor): gate fixups for Phase 3 Plan A (pins, snapshots)

Assisted-by: Claude Fable 5"
```

If nothing needed fixing, skip the commit. Report the final gate table (coverage %, test count, lint, ty, docs) with real numbers — evidence before assertions.

---

## Self-Review Notes (already applied)

- **Spec coverage check**: Unix parsers (Tasks 2–7) ✔; compute_rate rule (Task 1) ✔; warnings incl. never-produced rule + SNMP layer (Tasks 8–9) ✔; SNMP kind/meta_of/rate state (Task 9) ✔; OID contract + bundles + factory (Task 10) ✔; pattern registration (Task 11) ✔; UptimeParser + scoping integration + e2e (Tasks 12–13) ✔; docs (Task 14) ✔. **Not in Plan A** (deliberate): everything under the spec's "Log-sourced data" section (`parse_tick`, CSV, event tables) — that is Plan B; the `meta_of` view-filter is included here (Task 9 step 9) because the descriptors land here.
- **Type consistency**: `RateTracker.update(key, value, ts)` used identically in Tasks 2, 4, 9; `process_snmp_values(values, *, rates, ts)` defined in Task 9, consumed in Task 9's collector rewiring; `_note_health(key, *, produced, what)` defined Task 8, reused Task 9.
- **Known judgment calls an executor must not "fix" silently**: `points_from_values` is deleted (Task 9), not deprecated — it is internal; `DEFAULT_PARSERS` grows to 9 entries — browser/meta pins update deliberately in Task 15.
