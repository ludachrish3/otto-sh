"""Unit tests for built-in metric parsers."""

import pytest

from otto.monitor.parsers import (
    DiskParser,
    LoadParser,
    MemParser,
    MetricDataPoint,
    MetricParser,
    TopCpuParser,
    human_readable,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _top_output(
    idle1: float,
    idle2: float,
    procs1: list[tuple],
    procs2: list[tuple],
) -> str:
    """Build a two-block top -bn2 output string.

    Each proc tuple: (pid, user, res_kib, stat, cpu, mem, time_plus, command)
    cpu values are raw (per-core scale); the parser divides by parser.core_count.
    """
    header = (
        'top - 12:00:00 up 1 day,  2:00,  2 users,  load average: 0.5, 0.4, 0.3\n'
        'Tasks: 200 total,   1 running, 199 sleeping,   0 stopped,   0 zombie\n'
        '%Cpu(s):  5.0 us,  2.0 sy,  0.0 ni, {idle:.1f} id,  0.3 wa,  0.0 hi,  0.1 si\n'
        'MiB Mem :  16000.0 total,   8000.0 free,   4000.0 used,   4000.0 buff/cache\n'
        'MiB Swap:   2048.0 total,   2048.0 free,      0.0 used.   8000.0 avail Mem\n'
        '\n'
        '    PID USER      PR  NI    VIRT    RES    SHR S  %CPU  %MEM     TIME+ COMMAND\n'
    )
    proc_fmt = '{pid:>7} {user:<9}  20   0  123456 {res:>6}   4096 {stat}  {cpu:>5.1f}  {mem:>5.1f}  {time} {cmd}\n'

    def block(idle: float, procs: list[tuple]) -> str:
        rows = ''.join(
            proc_fmt.format(pid=p[0], user=p[1], res=p[2], stat=p[3],
                            cpu=p[4], mem=p[5], time=p[6], cmd=p[7])
            for p in procs
        )
        return header.format(idle=idle) + rows

    return block(idle1, procs1) + block(idle2, procs2)


# ---------------------------------------------------------------------------
# human_readable
# ---------------------------------------------------------------------------

class TestHumanReadable:
    def test_zero_bytes(self):
        assert human_readable(0) == '0 B'

    def test_zero_bytes_precision_zero(self):
        assert human_readable(0, precision=0) == '0 B'

    def test_bytes(self):
        assert human_readable(512) == '512 B'

    def test_kibibytes(self):
        assert human_readable(1024) == '1 K'

    def test_mebibytes(self):
        assert human_readable(1024 ** 2) == '1 M'

    def test_gibibytes(self):
        assert human_readable(1024 ** 3) == '1 G'

    def test_precision_strips_trailing_zeros(self):
        # 1.5 MiB — with precision=1, no trailing zeros to strip
        assert human_readable(1.5 * 1024 ** 2) == '1.5 M'

    def test_precision_zero(self):
        assert human_readable(64 * 1024 ** 2, precision=0) == '64 M'


# ---------------------------------------------------------------------------
# TopCpuParser
# ---------------------------------------------------------------------------

class TestTopCpuParser:
    parser = TopCpuParser(top_n=3)

    def test_command_includes_bn2_and_delay(self):
        assert '-bn2' in self.parser.command
        assert '-d' in self.parser.command
        assert '-1' not in self.parser.command

    def test_delay_is_configurable(self):
        p = TopCpuParser(delay=1.0)
        assert '1.0' in p.command

    def test_chart(self):
        assert self.parser.chart == 'CPU'

    def test_unit(self):
        assert self.parser.unit == '%'

    def test_typical_output_overall_cpu(self):
        output = _top_output(
            idle1=90.0,
            idle2=85.0,  # second block: 100 - 85 = 15%
            procs1=[(1, 'root', 4096, 'S', 99.0, 0.1, '0:01.00', 'fake')],
            procs2=[(1234, 'root', 65536, 'S', 8.0, 0.8, '1:23.45', 'python3')],
        )
        result = self.parser.parse(output)
        assert 'Overall CPU' in result
        assert result['Overall CPU'].value == pytest.approx(15.0)

    def test_second_block_is_used_not_first(self):
        # First block has idle=90 (cpu=10), second has idle=80 (cpu=20).
        # Parser must return 20, not 10.
        output = _top_output(
            idle1=90.0, idle2=80.0,
            procs1=[], procs2=[],
        )
        result = self.parser.parse(output)
        assert result['Overall CPU'].value == pytest.approx(20.0)

    def test_per_process_entries(self):
        procs = [
            (1234, 'root',    65536, 'S', 8.0, 0.8, '1:23.45', 'python3'),
            (5678, 'www-data', 32768, 'S', 2.0, 0.4, '0:45.67', 'nginx'),
        ]
        output = _top_output(idle1=90.0, idle2=88.0, procs1=procs, procs2=procs)
        p = TopCpuParser(top_n=3)
        p.core_count = 2
        result = p.parse(output)
        assert 'proc/1234' in result
        assert 'proc/5678' in result
        assert result['proc/1234'].value == pytest.approx(4.0)   # 8.0 / 2 cores
        assert result['proc/5678'].value == pytest.approx(1.0)   # 2.0 / 2 cores

    def test_per_process_meta_fields(self):
        procs = [(1234, 'root', 65536, 'S', 8.0, 0.8, '1:23.45', 'python3')]
        output = _top_output(idle1=90.0, idle2=88.0, procs1=procs, procs2=procs)
        meta = self.parser.parse(output)['proc/1234'].meta
        assert meta is not None
        assert meta['Command']  == 'python3'
        assert meta['User']     == 'root'
        assert meta['Stat']     == 'S'
        assert meta['CPU Time'] == '1:23.45'
        assert 'RSS' in meta
        assert 'Mem' in meta

    def test_per_process_rss_is_human_readable(self):
        # 65536 KiB * 1024 = 67108864 B = 64 MiB
        procs = [(1234, 'root', 65536, 'S', 8.0, 0.8, '1:23.45', 'python3')]
        output = _top_output(idle1=90.0, idle2=88.0, procs1=procs, procs2=procs)
        meta = self.parser.parse(output)['proc/1234'].meta
        assert meta is not None
        assert meta['RSS'] == '64 M'

    def test_per_process_normalized_by_core_count(self):
        # Raw %CPU from top is per-core; with 2 cores, 8.0 raw → 4.0 normalized
        procs = [(1234, 'root', 65536, 'S', 8.0, 0.8, '1:23.45', 'python3')]
        output = _top_output(idle1=90.0, idle2=88.0, procs1=procs, procs2=procs)
        p = TopCpuParser(top_n=3)
        p.core_count = 2
        assert p.parse(output)['proc/1234'].value == pytest.approx(4.0)

    def test_core_count_isolation_between_hosts(self):
        # Two parser instances simulate two hosts with different core counts.
        # The same raw top output (8.0% per-core) should normalize differently.
        procs = [(1234, 'root', 65536, 'S', 8.0, 0.8, '1:23.45', 'python3')]
        output = _top_output(idle1=90.0, idle2=88.0, procs1=procs, procs2=procs)

        p2 = TopCpuParser(top_n=3)
        p2.core_count = 2
        p4 = TopCpuParser(top_n=3)
        p4.core_count = 4

        assert p2.parse(output)['proc/1234'].value == pytest.approx(4.0)  # 8.0 / 2
        assert p4.parse(output)['proc/1234'].value == pytest.approx(2.0)  # 8.0 / 4

    def test_top_n_limits_processes(self):
        procs = [
            (i, 'root', 1024, 'S', float(10 - i), 0.1, '0:00.01', f'proc{i}')
            for i in range(1, 6)
        ]
        output = _top_output(idle1=90.0, idle2=88.0, procs1=procs, procs2=procs)
        result = self.parser.parse(output)
        proc_keys = [k for k in result if k.startswith('proc/')]
        assert len(proc_keys) == 3  # top_n=3

    def test_empty_output_returns_empty_dict(self):
        assert self.parser.parse('') == {}

    def test_missing_cpu_line_returns_empty_dict(self):
        assert self.parser.parse('no cpu info here\n') == {}

    def test_overall_cpu_absent_when_only_one_block(self):
        # Single-block output: only one "Tasks:" line, so block never reaches 2
        output = (
            'Tasks: 200 total,   1 running, 199 sleeping\n'
            '%Cpu(s):  5.0 us,  2.0 sy,  0.0 ni, 90.0 id,  0.3 wa\n'
            '    PID USER      PR  NI    VIRT    RES    SHR S  %CPU  %MEM     TIME+ COMMAND\n'
        )
        assert self.parser.parse(output) == {}


# ---------------------------------------------------------------------------
# MemParser
# ---------------------------------------------------------------------------

class TestMemParser:
    parser = MemParser()

    def test_command_uses_bytes_flag(self):
        assert self.parser.command == 'free -b'

    def test_unit(self):
        assert self.parser.unit == '%'

    def test_typical_output(self):
        # 3 GiB used out of 8 GiB total → 37.5%
        total = 8 * 1024 ** 3
        used  = 3 * 1024 ** 3
        output = (
            '              total        used        free      shared  buff/cache   available\n'
            f'Mem:    {total}  {used}  {total - used}       0       0       0\n'
            'Swap:          0          0          0\n'
        )
        result = self.parser.parse(output)
        assert result == {'Memory Usage': MetricDataPoint(
            value=pytest.approx(37.5, abs=0.01),
            meta={'Used': '3 G', 'Total': '8 G'},
        )}

    def test_full_memory(self):
        total = 4 * 1024 ** 3
        output = f'Mem:    {total}  {total}  0  0  0  0\n'
        result = self.parser.parse(output)
        assert result['Memory Usage'].value == pytest.approx(100.0)

    def test_meta_keys_are_human_readable(self):
        total = 1024 ** 3      # 1 GiB
        used  = 512 * 1024 ** 2  # 512 MiB
        output = f'Mem:    {total}  {used}  {total - used}  0  0  0\n'
        meta = self.parser.parse(output)['Memory Usage'].meta
        assert meta is not None
        assert 'Used'  in meta
        assert 'Total' in meta

    def test_empty_output_returns_empty_dict(self):
        assert self.parser.parse('') == {}


# ---------------------------------------------------------------------------
# DiskParser
# ---------------------------------------------------------------------------

class TestDiskParser:
    parser = DiskParser()

    def test_unit(self):
        assert self.parser.unit == '%'

    def test_typical_output_keyed_by_mount(self):
        output = (
            'Filesystem      Size  Used Avail Use% Mounted on\n'
            '/dev/sda1        20G  5.4G   14G  27% /\n'
        )
        result = self.parser.parse(output)
        assert '/' in result
        assert result['/'].value == pytest.approx(27.0)

    def test_meta_contains_display_fields(self):
        output = (
            'Filesystem      Size  Used Avail Use% Mounted on\n'
            '/dev/sda1        20G  5.4G   14G  27% /\n'
        )
        meta = self.parser.parse(output)['/'].meta
        assert meta is not None
        assert 'Used'      in meta
        assert 'Total'     in meta
        assert 'Available' in meta
        assert 'Mount'     in meta

    def test_full_disk(self):
        output = (
            'Filesystem      Size  Used Avail Use% Mounted on\n'
            '/dev/sda1        20G   20G     0 100% /\n'
        )
        assert self.parser.parse(output)['/'].value == pytest.approx(100.0)

    def test_multiple_mounts(self):
        output = (
            'Filesystem      Size  Used Avail Use% Mounted on\n'
            '/dev/sda1        20G  5.4G   14G  27% /\n'
            '/dev/sdb1       100G   40G   60G  40% /data\n'
        )
        result = self.parser.parse(output)
        assert '/' in result
        assert '/data' in result
        assert result['/data'].value == pytest.approx(40.0)

    def test_empty_output_returns_empty_dict(self):
        assert self.parser.parse('') == {}


# ---------------------------------------------------------------------------
# LoadParser
# ---------------------------------------------------------------------------

class TestLoadParser:
    parser = LoadParser()

    def test_unit(self):
        assert self.parser.unit == ''

    def test_typical_output(self):
        output = '0.52 0.58 0.59 1/432 12345\n'
        result = self.parser.parse(output)
        assert result['Load (1m)'].value  == pytest.approx(0.52)
        assert result['Load (5m)'].value  == pytest.approx(0.58)
        assert result['Load (15m)'].value == pytest.approx(0.59)

    def test_all_three_series_present(self):
        result = self.parser.parse('1.0 2.0 3.0 1/100 999\n')
        assert set(result.keys()) == {'Load (1m)', 'Load (5m)', 'Load (15m)'}

    def test_high_load(self):
        result = self.parser.parse('16.00 12.50 8.20 4/512 9999')
        assert result['Load (1m)'].value == pytest.approx(16.0)

    def test_empty_output_returns_empty_dict(self):
        assert self.parser.parse('') == {}

    def test_non_numeric_returns_empty_dict(self):
        assert self.parser.parse('error: permission denied') == {}


# ---------------------------------------------------------------------------
# MetricParser extensibility
# ---------------------------------------------------------------------------

class TestMetricParserExtensibility:
    """Verify that MetricParser is properly abstract and can be subclassed."""

    def test_subclass_must_implement_parse(self):
        class Incomplete(MetricParser):
            y_title = 'x'
            unit    = ''
            command = 'echo x'
            chart   = 'x'

        with pytest.raises(TypeError):
            Incomplete()  # type: ignore

    def test_custom_parser_works(self):
        class UptimeParser(MetricParser):
            y_title = 'Uptime'
            unit    = 'days'
            command = 'uptime -p'
            chart   = 'Uptime'

            def parse(self, output: str) -> dict[str, MetricDataPoint]:
                import re
                m = re.search(r'(\d+)\s+day', output)
                if m:
                    return {self.chart: MetricDataPoint(float(m.group(1)))}
                return {}

        p = UptimeParser()
        assert p.parse('up 3 days, 4 hours')['Uptime'].value == 3.0
        assert p.parse('up 5 hours') == {}
