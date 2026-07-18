"""Unit tests for built-in metric parsers."""

from dataclasses import FrozenInstanceError

import pytest

from otto.monitor.collector import MetricCollector
from otto.monitor.parsers import (
    DEFAULT_PARSERS,
    DiskIoParser,
    DiskParser,
    LoadParser,
    MemParser,
    MetricDataPoint,
    MetricParser,
    NetDevParser,
    ParseContext,
    PerCoreCpuParser,
    ProcCountParser,
    SocketsParser,
    TickResult,
    default_catalog,
    get_host_parsers,
    human_readable,
    register_host_parsers,
    register_parsers,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _net_dev_output(eth0: tuple, wlan0: tuple | None = None) -> str:
    """Build /proc/net/dev output. Each tuple: (rx_bytes, rx_pkts, rx_errs,
    rx_drop, tx_bytes, tx_pkts, tx_errs, tx_drop)."""

    def line(name: str, v: tuple) -> str:
        rx = f"{v[0]:>8} {v[1]:>7} {v[2]:>4} {v[3]:>4}    0     0          0         0"
        tx = f"{v[4]:>8} {v[5]:>7} {v[6]:>4} {v[7]:>4}    0     0       0          0"
        return f"{name:>6}: {rx} {tx}\n"

    header_line1 = "Inter-|   Receive                                                |  Transmit\n"
    header_line2 = (
        " face |bytes    packets errs drop fifo frame compressed multicast|"
        "bytes    packets errs drop fifo colls carrier compressed\n"
    )
    out = (
        header_line1 + header_line2 + line("lo", (999, 9, 0, 0, 999, 9, 0, 0)) + line("eth0", eth0)
    )
    if wlan0 is not None:
        out += line("wlan0", wlan0)
    return out


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


def _proc_stat_output(
    cores: list[tuple[int, int]], aggregate: tuple[int, int] | None = None
) -> str:
    """cores = [(busy_jiffies_excluding_idle, idle_plus_iowait_jiffies), ...].

    Emits the aggregate 'cpu' line plus one cpuN line per core:
    user nice system idle iowait irq softirq steal. When *aggregate* is given
    as (busy, idle) the 'cpu' line reflects it (so a two-tick call drives
    Overall CPU); otherwise a constant placeholder aggregate is emitted (zero
    delta -> no Overall CPU point).
    """
    if aggregate is None:
        agg_line = "cpu  99999 0 99999 999999 9999 0 0 0 0 0"
    else:
        busy, idle = aggregate
        user, system = busy // 2, busy - busy // 2
        idle_j, iowait = idle // 2, idle - idle // 2
        agg_line = f"cpu {user} 0 {system} {idle_j} {iowait} 0 0 0 0 0"
    lines = [agg_line]
    for n, (busy, idle) in enumerate(cores):
        user, system = busy // 2, busy - busy // 2
        idle_j, iowait = idle // 2, idle - idle // 2
        lines.append(f"cpu{n} {user} 0 {system} {idle_j} {iowait} 0 0 0 0 0")
    lines += ["intr 12345", "ctxt 6789", "procs_running 3", "procs_blocked 1"]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# human_readable
# ---------------------------------------------------------------------------


class TestHumanReadable:
    def test_zero_bytes(self):
        assert human_readable(0) == "0 B"

    def test_zero_bytes_precision_zero(self):
        assert human_readable(0, precision=0) == "0 B"

    def test_bytes(self):
        assert human_readable(512) == "512 B"

    def test_kibibytes(self):
        assert human_readable(1024) == "1 K"

    def test_mebibytes(self):
        assert human_readable(1024**2) == "1 M"

    def test_gibibytes(self):
        assert human_readable(1024**3) == "1 G"

    def test_precision_strips_trailing_zeros(self):
        # 1.5 MiB — with precision=1, no trailing zeros to strip
        assert human_readable(1.5 * 1024**2) == "1.5 M"

    def test_precision_zero(self):
        assert human_readable(64 * 1024**2, precision=0) == "64 M"


def test_parse_context_is_frozen():
    from datetime import datetime, timezone

    ctx = ParseContext(ts=datetime(2026, 7, 3, tzinfo=timezone.utc))
    with pytest.raises(FrozenInstanceError):
        ctx.ts = None  # type: ignore[misc]


def test_parse_context_carries_optional_ts():
    from datetime import datetime, timezone

    assert ParseContext().ts is None
    ts = datetime(2026, 7, 3, tzinfo=timezone.utc)
    assert ParseContext(ts=ts).ts == ts


# ---------------------------------------------------------------------------
# MemParser
# ---------------------------------------------------------------------------


class TestMemParser:
    parser = MemParser()

    def test_command_uses_bytes_flag(self):
        assert self.parser.command == "free -b"

    def test_unit(self):
        assert self.parser.unit == "%"

    def test_typical_output(self):
        # 3 GiB used out of 8 GiB total → 37.5%
        total = 8 * 1024**3
        used = 3 * 1024**3
        output = (
            "              total        used        free      shared  buff/cache   available\n"
            f"Mem:    {total}  {used}  {total - used}       0       0       0\n"
            "Swap:          0          0          0\n"
        )
        result = self.parser.parse(output, ctx=ParseContext())
        assert result == {
            "Memory Usage": MetricDataPoint(
                value=pytest.approx(37.5, abs=0.01),
                meta={"Used": "3 G", "Total": "8 G"},
            )
        }

    def test_full_memory(self):
        total = 4 * 1024**3
        output = f"Mem:    {total}  {total}  0  0  0  0\n"
        result = self.parser.parse(output, ctx=ParseContext())
        assert result["Memory Usage"].value == pytest.approx(100.0)

    def test_meta_keys_are_human_readable(self):
        total = 1024**3  # 1 GiB
        used = 512 * 1024**2  # 512 MiB
        output = f"Mem:    {total}  {used}  {total - used}  0  0  0\n"
        meta = self.parser.parse(output, ctx=ParseContext())["Memory Usage"].meta
        assert meta is not None
        assert "Used" in meta
        assert "Total" in meta

    def test_empty_output_returns_empty_dict(self):
        assert self.parser.parse("", ctx=ParseContext()) == {}

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


# ---------------------------------------------------------------------------
# DiskParser
# ---------------------------------------------------------------------------


class TestDiskParser:
    parser = DiskParser()

    def test_unit(self):
        assert self.parser.unit == "%"

    def test_typical_output_keyed_by_mount(self):
        output = (
            "Filesystem      Size  Used Avail Use% Mounted on\n"
            "/dev/sda1        20G  5.4G   14G  27% /\n"
        )
        result = self.parser.parse(output, ctx=ParseContext())
        assert "/" in result
        assert result["/"].value == pytest.approx(27.0)

    def test_meta_contains_display_fields(self):
        output = (
            "Filesystem      Size  Used Avail Use% Mounted on\n"
            "/dev/sda1        20G  5.4G   14G  27% /\n"
        )
        meta = self.parser.parse(output, ctx=ParseContext())["/"].meta
        assert meta is not None
        assert "Used" in meta
        assert "Total" in meta
        assert "Available" in meta
        assert "Mount" in meta

    def test_full_disk(self):
        output = (
            "Filesystem      Size  Used Avail Use% Mounted on\n"
            "/dev/sda1        20G   20G     0 100% /\n"
        )
        assert self.parser.parse(output, ctx=ParseContext())["/"].value == pytest.approx(100.0)

    def test_multiple_mounts(self):
        output = (
            "Filesystem      Size  Used Avail Use% Mounted on\n"
            "/dev/sda1        20G  5.4G   14G  27% /\n"
            "/dev/sdb1       100G   40G   60G  40% /data\n"
        )
        result = self.parser.parse(output, ctx=ParseContext())
        assert "/" in result
        assert "/data" in result
        assert result["/data"].value == pytest.approx(40.0)

    def test_empty_output_returns_empty_dict(self):
        assert self.parser.parse("", ctx=ParseContext()) == {}


# ---------------------------------------------------------------------------
# LoadParser
# ---------------------------------------------------------------------------


class TestLoadParser:
    parser = LoadParser()

    def test_unit(self):
        assert self.parser.unit == ""

    def test_typical_output(self):
        output = "0.52 0.58 0.59 1/432 12345\n"
        result = self.parser.parse(output, ctx=ParseContext())
        assert result["Load (1m)"].value == pytest.approx(0.52)
        assert result["Load (5m)"].value == pytest.approx(0.58)
        assert result["Load (15m)"].value == pytest.approx(0.59)

    def test_all_three_series_present(self):
        result = self.parser.parse("1.0 2.0 3.0 1/100 999\n", ctx=ParseContext())
        assert set(result.keys()) == {"Load (1m)", "Load (5m)", "Load (15m)"}

    def test_high_load(self):
        result = self.parser.parse("16.00 12.50 8.20 4/512 9999", ctx=ParseContext())
        assert result["Load (1m)"].value == pytest.approx(16.0)

    def test_empty_output_returns_empty_dict(self):
        assert self.parser.parse("", ctx=ParseContext()) == {}

    def test_non_numeric_returns_empty_dict(self):
        assert self.parser.parse("error: permission denied", ctx=ParseContext()) == {}


# ---------------------------------------------------------------------------
# NetDevParser
# ---------------------------------------------------------------------------


class TestNetDevParser:
    def _ctx(self, seconds: int) -> ParseContext:
        from datetime import datetime, timedelta, timezone

        t0 = datetime(2026, 7, 3, 12, 0, 0, tzinfo=timezone.utc)
        return ParseContext(ts=t0 + timedelta(seconds=seconds))

    def test_first_tick_is_baseline_empty(self):
        parser = NetDevParser()
        result = parser.parse(_net_dev_output((1000, 10, 0, 0, 2000, 20, 0, 0)), ctx=self._ctx(0))
        assert result == {}

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
        assert not any(k.split()[-1] == "lo" for k in points)

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


# ---------------------------------------------------------------------------
# SocketsParser
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# DiskIoParser
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# PerCoreCpuParser
# ---------------------------------------------------------------------------


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

    def test_counter_reset_rebaselines(self):
        parser = PerCoreCpuParser()
        parser.parse(_proc_stat_output([(10000, 90000)]), ctx=ParseContext())
        assert parser.parse(_proc_stat_output([(10, 90)]), ctx=ParseContext()) == {}
        points = parser.parse(_proc_stat_output([(60, 140)]), ctx=ParseContext())
        assert points["core 0"].value == 50.0

    def test_in_default_parsers(self):
        assert "cat /proc/stat" in DEFAULT_PARSERS

    def test_chart_is_cpu(self):
        assert PerCoreCpuParser().chart == "CPU"

    def test_uncapped(self):
        assert PerCoreCpuParser().max_series is None

    def test_overall_cpu_from_aggregate_deltas(self):
        parser = PerCoreCpuParser()
        parser.parse(_proc_stat_output([(100, 900)], aggregate=(100, 900)), ctx=ParseContext())
        points = parser.parse(
            _proc_stat_output([(130, 970)], aggregate=(150, 950)), ctx=ParseContext()
        )
        # aggregate delta: Δtotal=100, Δidle=50 -> 100*(1-50/100) = 50%
        assert points["Overall CPU"].value == 50.0
        assert points["core 0"].value == 30.0

    def test_overall_cpu_absent_when_aggregate_is_flat(self):
        parser = PerCoreCpuParser()
        parser.parse(_proc_stat_output([(100, 900), (100, 900)]), ctx=ParseContext())
        points = parser.parse(_proc_stat_output([(130, 970), (180, 920)]), ctx=ParseContext())
        assert "Overall CPU" not in points  # constant placeholder aggregate -> zero delta
        assert set(points) == {"core 0", "core 1"}


# ---------------------------------------------------------------------------
# ProcCountParser
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# MetricParser extensibility
# ---------------------------------------------------------------------------


class TestMetricParserExtensibility:
    """Verify that MetricParser is properly abstract and can be subclassed."""

    def test_subclass_must_implement_parse(self):
        class Incomplete(MetricParser):
            y_title = "x"
            unit = ""
            command = "echo x"
            chart = "x"

        with pytest.raises(TypeError):
            Incomplete()  # type: ignore[abstract]

    def test_custom_parser_works(self):
        class UptimeParser(MetricParser):
            y_title = "Uptime"
            unit = "days"
            command = "uptime -p"
            chart = "Uptime"

            def parse(self, output: str, *, ctx: ParseContext) -> dict[str, MetricDataPoint]:
                import re

                m = re.search(r"(\d+)\s+day", output)
                if m:
                    return {self.chart: MetricDataPoint(float(m.group(1)))}
                return {}

        p = UptimeParser()
        assert p.parse("up 3 days, 4 hours", ctx=ParseContext())["Uptime"].value == 3.0
        assert p.parse("up 5 hours", ctx=ParseContext()) == {}

    def test_default_max_series_is_the_numeric_default(self):
        from otto.models.monitor import DEFAULT_MAX_SERIES_PER_CHART

        class Plain(MetricParser):
            y_title = "x"
            unit = ""
            command = "echo x"
            chart = "x"

            def parse(self, output, *, ctx):
                return {}

        assert Plain().max_series == DEFAULT_MAX_SERIES_PER_CHART

    def test_parser_can_opt_out_of_the_cap(self):
        class Uncapped(MetricParser):
            y_title = "x"
            unit = ""
            command = "echo y"
            chart = "y"
            max_series = None

            def parse(self, output, *, ctx):
                return {}

        collector = MetricCollector(hosts=[], parsers=[Uncapped()])
        spec = next(c for c in collector.get_meta_model().metrics if c.chart == "y")
        assert spec.max_series is None


class TestHostParserRegistry:
    """register_host_parsers / get_host_parsers — the per-host HOST_PARSERS registry."""

    @pytest.fixture(autouse=True)
    def _isolate_host_parser_registry(self):
        from otto.monitor import parsers as parsers_mod

        before = set(parsers_mod.HOST_PARSERS.names())
        try:
            yield
        finally:
            for host_id in set(parsers_mod.HOST_PARSERS.names()) - before:
                parsers_mod.HOST_PARSERS.unregister(host_id)

    def test_unregistered_host_falls_back_to_default_parsers(self):
        # get_host_parsers deep-copies its result (see test below), so compare
        # shape rather than identity/equality of the MetricParser instances.
        fallback = get_host_parsers("no-such-host")
        assert set(fallback) == set(DEFAULT_PARSERS)
        assert {type(p) for p in fallback.values()} == {type(p) for p in DEFAULT_PARSERS.values()}

    def test_registered_host_returns_its_own_parsers(self):
        custom = {"free -b": MemParser()}
        register_host_parsers("gpu-01", custom)
        assert set(get_host_parsers("gpu-01")) == {"free -b"}

    def test_returned_dict_is_a_deep_copy(self):
        custom = {"free -b": MemParser()}
        register_host_parsers("gpu-02", custom)
        got = get_host_parsers("gpu-02")
        got.clear()
        assert set(get_host_parsers("gpu-02")) == {"free -b"}  # unaffected by caller mutation

    def test_reregistering_same_host_id_overwrites(self):
        # Re-registering a host_id is normal usage (e.g. an init module composing
        # {**DEFAULT_PARSERS, ...}), not a mistake — it must not raise.
        register_host_parsers("gpu-03", {"free -b": MemParser()})
        register_host_parsers("gpu-03", {"df -h": DiskParser()})
        assert set(get_host_parsers("gpu-03")) == {"df -h"}


class TestHostPatternRegistry:
    """register_host_parsers with re.Pattern: fullmatch scoping + loud ambiguity."""

    @pytest.fixture(autouse=True)
    def _isolate_host_and_pattern_parser_registries(self):
        from otto.monitor import parsers as parsers_mod

        host_before = set(parsers_mod.HOST_PARSERS.names())
        pattern_before = set(parsers_mod.HOST_PATTERN_PARSERS.names())
        try:
            yield
        finally:
            for host_id in set(parsers_mod.HOST_PARSERS.names()) - host_before:
                parsers_mod.HOST_PARSERS.unregister(host_id)
            for pattern_str in set(parsers_mod.HOST_PATTERN_PARSERS.names()) - pattern_before:
                parsers_mod.HOST_PATTERN_PARSERS.unregister(pattern_str)

    def _parsers(self) -> dict[str, MetricParser]:
        return {_UptimeParser.command: _UptimeParser()}

    def test_pattern_matches_family_of_hosts(self):
        import re

        register_host_parsers(re.compile(r"pat-family-.*"), self._parsers())
        assert _UptimeParser.command in get_host_parsers("pat-family-01")
        assert _UptimeParser.command in get_host_parsers("pat-family-02")

    def test_fullmatch_not_search(self):
        import re

        register_host_parsers(re.compile("pat-exact"), self._parsers())
        assert _UptimeParser.command in get_host_parsers("pat-exact")
        assert _UptimeParser.command not in get_host_parsers("my-pat-exact-2")

    def test_exact_registration_shadows_pattern(self):
        import re

        register_host_parsers(re.compile(r"pat-shadow-.*"), self._parsers())
        register_host_parsers("pat-shadow-1", dict(DEFAULT_PARSERS))
        assert _UptimeParser.command not in get_host_parsers("pat-shadow-1")
        assert _UptimeParser.command in get_host_parsers("pat-shadow-2")

    def test_two_matching_patterns_raise(self):
        import re

        register_host_parsers(re.compile(r"pat-ambig-.*"), self._parsers())
        register_host_parsers(re.compile(r"pat-ambig-0\d"), self._parsers())
        with pytest.raises(ValueError, match="matches multiple parser patterns"):
            get_host_parsers("pat-ambig-01")

    def test_no_match_falls_through_to_defaults(self):
        import re

        register_host_parsers(re.compile(r"pat-nomatch-.*"), self._parsers())
        assert get_host_parsers("unrelated-host").keys() == default_catalog().keys()

    def test_pattern_result_is_a_deep_copy(self):
        import re

        register_host_parsers(re.compile(r"pat-copy-.*"), self._parsers())
        a = get_host_parsers("pat-copy-1")
        b = get_host_parsers("pat-copy-2")
        assert a[_UptimeParser.command] is not b[_UptimeParser.command]


# ---------------------------------------------------------------------------
# parse_tick() contract
# ---------------------------------------------------------------------------


class TestParseTickDefaultAdapter:
    """parse_tick() default: wraps parse() as one untimed sample—existing parsers bit-identical."""

    _FREE_B = (
        "               total        used        free\n"
        "Mem:     16000000000  4000000000  12000000000\n"
        "Swap:     2000000000   500000000   1500000000\n"
    )

    def test_wraps_parse_as_one_untimed_sample(self) -> None:
        parser = MemParser()
        tick = parser.parse_tick(self._FREE_B, ctx=ParseContext())
        assert tick.events == []
        assert len(tick.samples) == 1
        assert tick.samples[0].ts is None
        assert tick.samples[0].series == parser.parse(self._FREE_B, ctx=ParseContext())

    def test_empty_parse_yields_no_samples(self) -> None:
        assert MemParser().parse_tick("garbage", ctx=ParseContext()) == TickResult(
            samples=[], events=[]
        )


# ---------------------------------------------------------------------------
# Project-level parser registry
# ---------------------------------------------------------------------------


class _UptimeParser(MetricParser):
    """Test fixture parser with a command not in DEFAULT_PARSERS."""

    y_title = "Uptime"
    unit = "days"
    command = "uptime -p"
    chart = "Uptime"

    def parse(self, output: str, *, ctx: ParseContext) -> dict[str, MetricDataPoint]:
        return {}


class TestProjectParserRegistry:
    """register_parsers / get_host_parsers — the project-level PROJECT_PARSERS registry."""

    @pytest.fixture(autouse=True)
    def _isolate_project_and_host_parser_registries(self):
        from otto.monitor import parsers as parsers_mod

        host_before = set(parsers_mod.HOST_PARSERS.names())
        project_before = set(parsers_mod.PROJECT_PARSERS.names())
        try:
            yield
        finally:
            for host_id in set(parsers_mod.HOST_PARSERS.names()) - host_before:
                parsers_mod.HOST_PARSERS.unregister(host_id)
            for command in set(parsers_mod.PROJECT_PARSERS.names()) - project_before:
                parsers_mod.PROJECT_PARSERS.unregister(command)

    def test_register_parsers_extends_defaults_for_all_hosts(self):
        register_parsers([_UptimeParser()])
        merged = get_host_parsers("any-host-without-per-host-registration")
        assert "uptime -p" in merged
        assert set(DEFAULT_PARSERS) <= set(merged)

    def test_register_parsers_overrides_default_command(self):
        class MyMem(MemParser):
            chart = "My Memory"

        register_parsers([MyMem()])
        merged = get_host_parsers("some-host")
        assert merged["free -b"].chart == "My Memory"

    def test_per_host_registration_beats_project_level(self):
        register_parsers([_UptimeParser()])
        register_host_parsers("special", dict(DEFAULT_PARSERS))
        assert "uptime -p" not in get_host_parsers("special")  # per-host dict is total

    def test_duplicate_project_registration_is_loud(self):
        register_parsers([_UptimeParser()])
        with pytest.raises(ValueError, match="uptime -p"):
            register_parsers([_UptimeParser()])
