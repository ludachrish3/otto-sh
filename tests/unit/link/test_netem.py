"""NetEm impairer: exact tc argv (explicit units ALWAYS) + qdisc-show parsing
against canned modern and centos:7-era iproute2 output."""

import re

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from otto.link.impairer import IMPAIRERS, ScopedState
from otto.link.netem import NetEmImpairer, PortPrefix, netem_args, parse_qdisc_show, port_prefixes
from otto.link.params import ImpairmentParams, Selector, collides, scope_contains

from ._tc_render import render_scoped_tree

FULL = ImpairmentParams(
    delay_ms=50.0,
    jitter_ms=5.0,
    loss_pct=2.0,
    corrupt_pct=0.1,
    duplicate_pct=1.0,
    reorder_pct=5.0,
    rate="10mbit",
)


class TestCommands:
    def test_registered_as_netem_for_unix(self) -> None:
        assert IMPAIRERS.get("netem") is NetEmImpairer
        assert NetEmImpairer.host_families == frozenset({"unix"})

    def test_apply_command_exact(self) -> None:
        cmd = NetEmImpairer().apply_command(
            "eth1.100", ImpairmentParams(delay_ms=50.0, loss_pct=2.0)
        )
        assert cmd == "tc qdisc replace dev eth1.100 root netem delay 50ms loss 2%"

    def test_apply_all_params_explicit_units(self) -> None:
        assert netem_args(FULL) == (
            "delay 50ms 5ms loss 2% corrupt 0.1% duplicate 1% reorder 5% rate 10mbit"
        )

    def test_read_and_clear_commands(self) -> None:
        imp = NetEmImpairer()
        assert imp.read_command("eth1") == "tc qdisc show dev eth1"
        assert imp.clear_command("eth1") == "tc qdisc del dev eth1 root"


class TestParser:
    def test_modern_ubuntu_2404(self) -> None:
        # verified live on the bed (iproute2 6.1.0)
        out = "qdisc netem 8001: root refcnt 2 limit 1000 delay 50ms  5ms loss 2%\n"
        assert parse_qdisc_show(out) == ImpairmentParams(delay_ms=50.0, jitter_ms=5.0, loss_pct=2.0)

    def test_old_iproute2_float_times(self) -> None:
        # centos:7-era formatting: float time values
        out = "qdisc netem 8002: root refcnt 2 limit 1000 delay 50.0ms loss 2% rate 10Mbit\n"
        assert parse_qdisc_show(out) == ImpairmentParams(delay_ms=50.0, loss_pct=2.0, rate="10mbit")

    def test_no_netem_returns_none(self) -> None:
        assert parse_qdisc_show("qdisc noqueue 0: root refcnt 2\n") is None
        assert parse_qdisc_show("") is None

    def test_non_root_netem_ignored(self) -> None:
        # a netem leaf someone attached under a classful parent is not ours
        assert parse_qdisc_show("qdisc netem 10: parent 1:1 limit 1000 delay 5ms\n") is None

    def test_delay_without_jitter(self) -> None:
        out = "qdisc netem 8001: root refcnt 2 limit 1000 delay 100ms\n"
        assert parse_qdisc_show(out) == ImpairmentParams(delay_ms=100.0)

    @pytest.mark.parametrize("keyword", ["corrupt", "duplicate", "reorder"])
    def test_percent_keywords(self, keyword: str) -> None:
        out = f"qdisc netem 8001: root refcnt 2 limit 1000 {keyword} 3%\n"
        parsed = parse_qdisc_show(out)
        assert parsed is not None
        assert getattr(parsed, f"{keyword}_pct") == 3.0

    def test_roundtrip_apply_then_parse(self) -> None:
        # what we render is what we re-read: rendering tokens parse back equal
        rendered = f"qdisc netem 8003: root refcnt 2 limit 1000 {netem_args(FULL)}\n"
        assert parse_qdisc_show(rendered) == FULL


class TestScopedCommands:
    imp = NetEmImpairer()

    def test_supports_selectors(self) -> None:
        assert NetEmImpairer.supports_selectors is True

    def test_root_command_golden(self) -> None:
        assert self.imp.scoped_root_command("eth1.100") == (
            "tc qdisc replace dev eth1.100 root handle 1: prio bands 11 "
            "priomap 1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1"
        )

    def test_band_command_golden_hex_handles(self) -> None:
        params = ImpairmentParams(delay_ms=200.0)
        assert self.imp.scoped_band_command("eth1.100", 4, params) == (
            "tc qdisc replace dev eth1.100 parent 1:4 handle 40: netem delay 200ms"
        )
        # bands >= 10: classid minor and handle are HEX
        assert self.imp.scoped_band_command("eth1.100", 11, params) == (
            "tc qdisc replace dev eth1.100 parent 1:b handle b0: netem delay 200ms"
        )

    def test_filter_commands_both_protos_both_sides_emits_four_tier_two(self) -> None:
        cmds = self.imp.scoped_filter_commands("eth1.100", 4, Selector(5201))
        assert cmds == [
            (
                "tc filter add dev eth1.100 parent 1: pref 440 protocol ip u32 "
                "match ip protocol 6 0xff match ip dport 5201 0xffff flowid 1:4"
            ),
            (
                "tc filter add dev eth1.100 parent 1: pref 441 protocol ip u32 "
                "match ip protocol 17 0xff match ip dport 5201 0xffff flowid 1:4"
            ),
            (
                "tc filter add dev eth1.100 parent 1: pref 1440 protocol ip u32 "
                "match ip protocol 6 0xff match ip sport 5201 0xffff flowid 1:4"
            ),
            (
                "tc filter add dev eth1.100 parent 1: pref 1441 protocol ip u32 "
                "match ip protocol 17 0xff match ip sport 5201 0xffff flowid 1:4"
            ),
        ]

    def test_filter_commands_single_proto_is_tier_one(self) -> None:
        tcp = self.imp.scoped_filter_commands("eth1.100", 5, Selector(5201, "tcp"))
        assert [c.split(" pref ")[1].split(" ")[0] for c in tcp] == ["250", "1250"]
        udp = self.imp.scoped_filter_commands("eth1.100", 5, Selector(53, "udp"))
        assert [c.split(" pref ")[1].split(" ")[0] for c in udp] == ["251", "1251"]

    def test_range_side_entries_share_one_pref(self) -> None:
        sel = Selector(5000, "tcp", end=5010, side="dst")
        assert self.imp.scoped_filter_commands("eth1.100", 4, sel) == [
            (
                "tc filter add dev eth1.100 parent 1: pref 40 protocol ip u32 "
                "match ip protocol 6 0xff match ip dport 5000 0xfff8 flowid 1:4"
            ),
            (
                "tc filter add dev eth1.100 parent 1: pref 40 protocol ip u32 "
                "match ip protocol 6 0xff match ip dport 5008 0xfffe flowid 1:4"
            ),
            (
                "tc filter add dev eth1.100 parent 1: pref 40 protocol ip u32 "
                "match ip protocol 6 0xff match ip dport 5010 0xffff flowid 1:4"
            ),
        ]
        src = Selector(40000, "udp", end=40003, side="src")
        assert self.imp.scoped_filter_commands("eth1.100", 5, src) == [
            (
                "tc filter add dev eth1.100 parent 1: pref 1051 protocol ip u32 "
                "match ip protocol 17 0xff match ip sport 40000 0xfffc flowid 1:5"
            ),
        ]

    def test_builders_reproduce_the_live_capture_script(self) -> None:
        """The commands capture.sh ran by hand on the bed are exactly what the builders emit."""

        def adds(band: int, sel: Selector) -> list[str]:
            return [
                c.replace("dev eth1.100", 'dev "$D"')
                for c in self.imp.scoped_filter_commands("eth1.100", band, sel)
            ]

        assert adds(4, Selector(5201, "tcp")) + adds(5, Selector(53, "udp")) == [
            (
                'tc filter add dev "$D" parent 1: pref 240 protocol ip u32 '
                "match ip protocol 6 0xff match ip dport 5201 0xffff flowid 1:4"
            ),
            (
                'tc filter add dev "$D" parent 1: pref 1240 protocol ip u32 '
                "match ip protocol 6 0xff match ip sport 5201 0xffff flowid 1:4"
            ),
            (
                'tc filter add dev "$D" parent 1: pref 251 protocol ip u32 '
                "match ip protocol 17 0xff match ip dport 53 0xffff flowid 1:5"
            ),
            (
                'tc filter add dev "$D" parent 1: pref 1251 protocol ip u32 '
                "match ip protocol 17 0xff match ip sport 53 0xffff flowid 1:5"
            ),
        ]
        assert adds(11, Selector(53)) == [
            (
                'tc filter add dev "$D" parent 1: pref 510 protocol ip u32 '
                "match ip protocol 6 0xff match ip dport 53 0xffff flowid 1:b"
            ),
            (
                'tc filter add dev "$D" parent 1: pref 511 protocol ip u32 '
                "match ip protocol 17 0xff match ip dport 53 0xffff flowid 1:b"
            ),
            (
                'tc filter add dev "$D" parent 1: pref 1510 protocol ip u32 '
                "match ip protocol 6 0xff match ip sport 53 0xffff flowid 1:b"
            ),
            (
                'tc filter add dev "$D" parent 1: pref 1511 protocol ip u32 '
                "match ip protocol 17 0xff match ip sport 53 0xffff flowid 1:b"
            ),
        ]

    def test_clear_selector_commands_one_delete_per_slot(self) -> None:
        assert self.imp.scoped_clear_selector_commands("eth1.100", 4, Selector(5201, "tcp")) == [
            "tc filter del dev eth1.100 parent 1: pref 240 protocol ip u32",
            "tc filter del dev eth1.100 parent 1: pref 1240 protocol ip u32",
            "tc qdisc del dev eth1.100 parent 1:4 handle 40:",
        ]
        rng = Selector(5000, "tcp", end=5010, side="dst")
        assert self.imp.scoped_clear_selector_commands("eth1.100", 4, rng) == [
            "tc filter del dev eth1.100 parent 1: pref 40 protocol ip u32",
            "tc qdisc del dev eth1.100 parent 1:4 handle 40:",
        ]

    def test_read_commands_golden(self) -> None:
        assert self.imp.scoped_read_commands("eth1.100") == [
            "tc qdisc show dev eth1.100",
            "tc filter show dev eth1.100 parent 1: 2>/dev/null || true",
        ]


# Live read-backs of the pref layout — pinned constants captured live on the
# bed (iproute2-6.1.0 and iproute2-ss170501); the capture files themselves
# are not kept in the tree. Each constant is one `### qdisc|filter <tree>`
# section minus the blank separator line the capture script prints before
# the next marker; to regenerate, rerun that capture script (written for
# this feature) against a throwaway netdev on both userlands. The old
# userland differs only cosmetically: plain `flowid` (no `*`),
# `priomap  1 2 ...`, `delay 200.0ms`, `refcnt 3`, and the qdisc leaves
# listed in a different order.
# captured live on test1 (throwaway netdev), 2026-09-24 (capture.sh tree A);
# tc -V: tc utility, iproute2-6.1.0, libbpf 1.3.0
QDISC_LIVE_A = (
    "qdisc prio 1: root refcnt 2 bands 11 priomap 1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1\n"
    "qdisc netem 40: parent 1:4 limit 1000 delay 200ms\n"
    "qdisc netem 50: parent 1:5 limit 1000 loss 5%\n"
)
FILTER_LIVE_A = (
    "filter protocol ip pref 240 u32 chain 0 \n"
    "filter protocol ip pref 240 u32 chain 0 fh 800: ht divisor 1 \n"
    "filter protocol ip pref 240 u32 chain 0 fh 800::800 order 2048 key ht 800 bkt 0"
    " *flowid 1:4 not_in_hw \n"
    "  match 00060000/00ff0000 at 8\n"
    "  match 00001451/0000ffff at 20\n"
    "filter protocol ip pref 251 u32 chain 0 \n"
    "filter protocol ip pref 251 u32 chain 0 fh 802: ht divisor 1 \n"
    "filter protocol ip pref 251 u32 chain 0 fh 802::800 order 2048 key ht 802 bkt 0"
    " *flowid 1:5 not_in_hw \n"
    "  match 00110000/00ff0000 at 8\n"
    "  match 00000035/0000ffff at 20\n"
    "filter protocol ip pref 1240 u32 chain 0 \n"
    "filter protocol ip pref 1240 u32 chain 0 fh 801: ht divisor 1 \n"
    "filter protocol ip pref 1240 u32 chain 0 fh 801::800 order 2048 key ht 801 bkt 0"
    " *flowid 1:4 not_in_hw \n"
    "  match 00060000/00ff0000 at 8\n"
    "  match 14510000/ffff0000 at 20\n"
    "filter protocol ip pref 1251 u32 chain 0 \n"
    "filter protocol ip pref 1251 u32 chain 0 fh 803: ht divisor 1 \n"
    "filter protocol ip pref 1251 u32 chain 0 fh 803::800 order 2048 key ht 803 bkt 0"
    " *flowid 1:5 not_in_hw \n"
    "  match 00110000/00ff0000 at 8\n"
    "  match 00350000/ffff0000 at 20\n"
)
QDISC_LIVE_A_CLEARED = (
    "qdisc prio 1: root refcnt 2 bands 11 priomap 1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1\n"
    "qdisc netem 40: parent 1:4 limit 1000 delay 200ms\n"
)
FILTER_LIVE_A_CLEARED = (
    "filter protocol ip pref 240 u32 chain 0 \n"
    "filter protocol ip pref 240 u32 chain 0 fh 800: ht divisor 1 \n"
    "filter protocol ip pref 240 u32 chain 0 fh 800::800 order 2048 key ht 800 bkt 0"
    " *flowid 1:4 not_in_hw \n"
    "  match 00060000/00ff0000 at 8\n"
    "  match 00001451/0000ffff at 20\n"
    "filter protocol ip pref 1240 u32 chain 0 \n"
    "filter protocol ip pref 1240 u32 chain 0 fh 801: ht divisor 1 \n"
    "filter protocol ip pref 1240 u32 chain 0 fh 801::800 order 2048 key ht 801 bkt 0"
    " *flowid 1:4 not_in_hw \n"
    "  match 00060000/00ff0000 at 8\n"
    "  match 14510000/ffff0000 at 20\n"
)

# captured live on test1 (throwaway netdev), 2026-09-24 (capture.sh tree B);
# tc -V: tc utility, iproute2-6.1.0, libbpf 1.3.0
QDISC_LIVE_B = (
    "qdisc prio 1: root refcnt 2 bands 11 priomap 1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1\n"
    "qdisc netem b0: parent 1:b limit 1000 delay 10ms\n"
    "qdisc netem 40: parent 1:4 limit 1000 delay 200ms\n"
    "qdisc netem 50: parent 1:5 limit 1000 loss 5%\n"
)
FILTER_LIVE_B = (
    "filter protocol ip pref 40 u32 chain 0 \n"
    "filter protocol ip pref 40 u32 chain 0 fh 800: ht divisor 1 \n"
    "filter protocol ip pref 40 u32 chain 0 fh 800::800 order 2048 key ht 800 bkt 0"
    " *flowid 1:4 not_in_hw \n"
    "  match 00060000/00ff0000 at 8\n"
    "  match 00001388/0000fff8 at 20\n"
    "filter protocol ip pref 40 u32 chain 0 fh 800::801 order 2049 key ht 800 bkt 0"
    " *flowid 1:4 not_in_hw \n"
    "  match 00060000/00ff0000 at 8\n"
    "  match 00001390/0000fffe at 20\n"
    "filter protocol ip pref 40 u32 chain 0 fh 800::802 order 2050 key ht 800 bkt 0"
    " *flowid 1:4 not_in_hw \n"
    "  match 00060000/00ff0000 at 8\n"
    "  match 00001392/0000ffff at 20\n"
    "filter protocol ip pref 510 u32 chain 0 \n"
    "filter protocol ip pref 510 u32 chain 0 fh 802: ht divisor 1 \n"
    "filter protocol ip pref 510 u32 chain 0 fh 802::800 order 2048 key ht 802 bkt 0"
    " *flowid 1:b not_in_hw \n"
    "  match 00060000/00ff0000 at 8\n"
    "  match 00000035/0000ffff at 20\n"
    "filter protocol ip pref 511 u32 chain 0 \n"
    "filter protocol ip pref 511 u32 chain 0 fh 803: ht divisor 1 \n"
    "filter protocol ip pref 511 u32 chain 0 fh 803::800 order 2048 key ht 803 bkt 0"
    " *flowid 1:b not_in_hw \n"
    "  match 00110000/00ff0000 at 8\n"
    "  match 00000035/0000ffff at 20\n"
    "filter protocol ip pref 1051 u32 chain 0 \n"
    "filter protocol ip pref 1051 u32 chain 0 fh 801: ht divisor 1 \n"
    "filter protocol ip pref 1051 u32 chain 0 fh 801::800 order 2048 key ht 801 bkt 0"
    " *flowid 1:5 not_in_hw \n"
    "  match 00110000/00ff0000 at 8\n"
    "  match 9c400000/fffc0000 at 20\n"
    "filter protocol ip pref 1510 u32 chain 0 \n"
    "filter protocol ip pref 1510 u32 chain 0 fh 804: ht divisor 1 \n"
    "filter protocol ip pref 1510 u32 chain 0 fh 804::800 order 2048 key ht 804 bkt 0"
    " *flowid 1:b not_in_hw \n"
    "  match 00060000/00ff0000 at 8\n"
    "  match 00350000/ffff0000 at 20\n"
    "filter protocol ip pref 1511 u32 chain 0 \n"
    "filter protocol ip pref 1511 u32 chain 0 fh 805: ht divisor 1 \n"
    "filter protocol ip pref 1511 u32 chain 0 fh 805::800 order 2048 key ht 805 bkt 0"
    " *flowid 1:b not_in_hw \n"
    "  match 00110000/00ff0000 at 8\n"
    "  match 00350000/ffff0000 at 20\n"
)
QDISC_LIVE_B_CLEARED = (
    "qdisc prio 1: root refcnt 2 bands 11 priomap 1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1\n"
    "qdisc netem b0: parent 1:b limit 1000 delay 10ms\n"
    "qdisc netem 50: parent 1:5 limit 1000 loss 5%\n"
)
FILTER_LIVE_B_CLEARED = (
    "filter protocol ip pref 510 u32 chain 0 \n"
    "filter protocol ip pref 510 u32 chain 0 fh 802: ht divisor 1 \n"
    "filter protocol ip pref 510 u32 chain 0 fh 802::800 order 2048 key ht 802 bkt 0"
    " *flowid 1:b not_in_hw \n"
    "  match 00060000/00ff0000 at 8\n"
    "  match 00000035/0000ffff at 20\n"
    "filter protocol ip pref 511 u32 chain 0 \n"
    "filter protocol ip pref 511 u32 chain 0 fh 803: ht divisor 1 \n"
    "filter protocol ip pref 511 u32 chain 0 fh 803::800 order 2048 key ht 803 bkt 0"
    " *flowid 1:b not_in_hw \n"
    "  match 00110000/00ff0000 at 8\n"
    "  match 00000035/0000ffff at 20\n"
    "filter protocol ip pref 1051 u32 chain 0 \n"
    "filter protocol ip pref 1051 u32 chain 0 fh 801: ht divisor 1 \n"
    "filter protocol ip pref 1051 u32 chain 0 fh 801::800 order 2048 key ht 801 bkt 0"
    " *flowid 1:5 not_in_hw \n"
    "  match 00110000/00ff0000 at 8\n"
    "  match 9c400000/fffc0000 at 20\n"
    "filter protocol ip pref 1510 u32 chain 0 \n"
    "filter protocol ip pref 1510 u32 chain 0 fh 804: ht divisor 1 \n"
    "filter protocol ip pref 1510 u32 chain 0 fh 804::800 order 2048 key ht 804 bkt 0"
    " *flowid 1:b not_in_hw \n"
    "  match 00060000/00ff0000 at 8\n"
    "  match 00350000/ffff0000 at 20\n"
    "filter protocol ip pref 1511 u32 chain 0 \n"
    "filter protocol ip pref 1511 u32 chain 0 fh 805: ht divisor 1 \n"
    "filter protocol ip pref 1511 u32 chain 0 fh 805::800 order 2048 key ht 805 bkt 0"
    " *flowid 1:b not_in_hw \n"
    "  match 00110000/00ff0000 at 8\n"
    "  match 00350000/ffff0000 at 20\n"
)

# captured live on test3's oldos container (centos:7), 2026-09-24 (capture.sh tree A);
# tc -V: tc utility, iproute2-ss170501
QDISC_LIVE_A_OLD = (
    "qdisc prio 1: root refcnt 3 bands 11 priomap  1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1\n"
    "qdisc netem 40: parent 1:4 limit 1000 delay 200.0ms\n"
    "qdisc netem 50: parent 1:5 limit 1000 loss 5%\n"
)
FILTER_LIVE_A_OLD = (
    "filter protocol ip pref 240 u32 chain 0 \n"
    "filter protocol ip pref 240 u32 chain 0 fh 800: ht divisor 1 \n"
    "filter protocol ip pref 240 u32 chain 0 fh 800::800 order 2048 key ht 800 bkt 0"
    " flowid 1:4 not_in_hw \n"
    "  match 00060000/00ff0000 at 8\n"
    "  match 00001451/0000ffff at 20\n"
    "filter protocol ip pref 251 u32 chain 0 \n"
    "filter protocol ip pref 251 u32 chain 0 fh 802: ht divisor 1 \n"
    "filter protocol ip pref 251 u32 chain 0 fh 802::800 order 2048 key ht 802 bkt 0"
    " flowid 1:5 not_in_hw \n"
    "  match 00110000/00ff0000 at 8\n"
    "  match 00000035/0000ffff at 20\n"
    "filter protocol ip pref 1240 u32 chain 0 \n"
    "filter protocol ip pref 1240 u32 chain 0 fh 801: ht divisor 1 \n"
    "filter protocol ip pref 1240 u32 chain 0 fh 801::800 order 2048 key ht 801 bkt 0"
    " flowid 1:4 not_in_hw \n"
    "  match 00060000/00ff0000 at 8\n"
    "  match 14510000/ffff0000 at 20\n"
    "filter protocol ip pref 1251 u32 chain 0 \n"
    "filter protocol ip pref 1251 u32 chain 0 fh 803: ht divisor 1 \n"
    "filter protocol ip pref 1251 u32 chain 0 fh 803::800 order 2048 key ht 803 bkt 0"
    " flowid 1:5 not_in_hw \n"
    "  match 00110000/00ff0000 at 8\n"
    "  match 00350000/ffff0000 at 20\n"
)
QDISC_LIVE_A_CLEARED_OLD = (
    "qdisc prio 1: root refcnt 3 bands 11 priomap  1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1\n"
    "qdisc netem 40: parent 1:4 limit 1000 delay 200.0ms\n"
)
FILTER_LIVE_A_CLEARED_OLD = (
    "filter protocol ip pref 240 u32 chain 0 \n"
    "filter protocol ip pref 240 u32 chain 0 fh 800: ht divisor 1 \n"
    "filter protocol ip pref 240 u32 chain 0 fh 800::800 order 2048 key ht 800 bkt 0"
    " flowid 1:4 not_in_hw \n"
    "  match 00060000/00ff0000 at 8\n"
    "  match 00001451/0000ffff at 20\n"
    "filter protocol ip pref 1240 u32 chain 0 \n"
    "filter protocol ip pref 1240 u32 chain 0 fh 801: ht divisor 1 \n"
    "filter protocol ip pref 1240 u32 chain 0 fh 801::800 order 2048 key ht 801 bkt 0"
    " flowid 1:4 not_in_hw \n"
    "  match 00060000/00ff0000 at 8\n"
    "  match 14510000/ffff0000 at 20\n"
)

# captured live on test3's oldos container (centos:7), 2026-09-24 (capture.sh tree B);
# tc -V: tc utility, iproute2-ss170501
QDISC_LIVE_B_OLD = (
    "qdisc prio 1: root refcnt 3 bands 11 priomap  1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1\n"
    "qdisc netem b0: parent 1:b limit 1000 delay 10.0ms\n"
    "qdisc netem 40: parent 1:4 limit 1000 delay 200.0ms\n"
    "qdisc netem 50: parent 1:5 limit 1000 loss 5%\n"
)
FILTER_LIVE_B_OLD = (
    "filter protocol ip pref 40 u32 chain 0 \n"
    "filter protocol ip pref 40 u32 chain 0 fh 800: ht divisor 1 \n"
    "filter protocol ip pref 40 u32 chain 0 fh 800::800 order 2048 key ht 800 bkt 0"
    " flowid 1:4 not_in_hw \n"
    "  match 00060000/00ff0000 at 8\n"
    "  match 00001388/0000fff8 at 20\n"
    "filter protocol ip pref 40 u32 chain 0 fh 800::801 order 2049 key ht 800 bkt 0"
    " flowid 1:4 not_in_hw \n"
    "  match 00060000/00ff0000 at 8\n"
    "  match 00001390/0000fffe at 20\n"
    "filter protocol ip pref 40 u32 chain 0 fh 800::802 order 2050 key ht 800 bkt 0"
    " flowid 1:4 not_in_hw \n"
    "  match 00060000/00ff0000 at 8\n"
    "  match 00001392/0000ffff at 20\n"
    "filter protocol ip pref 510 u32 chain 0 \n"
    "filter protocol ip pref 510 u32 chain 0 fh 802: ht divisor 1 \n"
    "filter protocol ip pref 510 u32 chain 0 fh 802::800 order 2048 key ht 802 bkt 0"
    " flowid 1:b not_in_hw \n"
    "  match 00060000/00ff0000 at 8\n"
    "  match 00000035/0000ffff at 20\n"
    "filter protocol ip pref 511 u32 chain 0 \n"
    "filter protocol ip pref 511 u32 chain 0 fh 803: ht divisor 1 \n"
    "filter protocol ip pref 511 u32 chain 0 fh 803::800 order 2048 key ht 803 bkt 0"
    " flowid 1:b not_in_hw \n"
    "  match 00110000/00ff0000 at 8\n"
    "  match 00000035/0000ffff at 20\n"
    "filter protocol ip pref 1051 u32 chain 0 \n"
    "filter protocol ip pref 1051 u32 chain 0 fh 801: ht divisor 1 \n"
    "filter protocol ip pref 1051 u32 chain 0 fh 801::800 order 2048 key ht 801 bkt 0"
    " flowid 1:5 not_in_hw \n"
    "  match 00110000/00ff0000 at 8\n"
    "  match 9c400000/fffc0000 at 20\n"
    "filter protocol ip pref 1510 u32 chain 0 \n"
    "filter protocol ip pref 1510 u32 chain 0 fh 804: ht divisor 1 \n"
    "filter protocol ip pref 1510 u32 chain 0 fh 804::800 order 2048 key ht 804 bkt 0"
    " flowid 1:b not_in_hw \n"
    "  match 00060000/00ff0000 at 8\n"
    "  match 00350000/ffff0000 at 20\n"
    "filter protocol ip pref 1511 u32 chain 0 \n"
    "filter protocol ip pref 1511 u32 chain 0 fh 805: ht divisor 1 \n"
    "filter protocol ip pref 1511 u32 chain 0 fh 805::800 order 2048 key ht 805 bkt 0"
    " flowid 1:b not_in_hw \n"
    "  match 00110000/00ff0000 at 8\n"
    "  match 00350000/ffff0000 at 20\n"
)
QDISC_LIVE_B_CLEARED_OLD = (
    "qdisc prio 1: root refcnt 3 bands 11 priomap  1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1\n"
    "qdisc netem b0: parent 1:b limit 1000 delay 10.0ms\n"
    "qdisc netem 50: parent 1:5 limit 1000 loss 5%\n"
)
FILTER_LIVE_B_CLEARED_OLD = (
    "filter protocol ip pref 510 u32 chain 0 \n"
    "filter protocol ip pref 510 u32 chain 0 fh 802: ht divisor 1 \n"
    "filter protocol ip pref 510 u32 chain 0 fh 802::800 order 2048 key ht 802 bkt 0"
    " flowid 1:b not_in_hw \n"
    "  match 00060000/00ff0000 at 8\n"
    "  match 00000035/0000ffff at 20\n"
    "filter protocol ip pref 511 u32 chain 0 \n"
    "filter protocol ip pref 511 u32 chain 0 fh 803: ht divisor 1 \n"
    "filter protocol ip pref 511 u32 chain 0 fh 803::800 order 2048 key ht 803 bkt 0"
    " flowid 1:b not_in_hw \n"
    "  match 00110000/00ff0000 at 8\n"
    "  match 00000035/0000ffff at 20\n"
    "filter protocol ip pref 1051 u32 chain 0 \n"
    "filter protocol ip pref 1051 u32 chain 0 fh 801: ht divisor 1 \n"
    "filter protocol ip pref 1051 u32 chain 0 fh 801::800 order 2048 key ht 801 bkt 0"
    " flowid 1:5 not_in_hw \n"
    "  match 00110000/00ff0000 at 8\n"
    "  match 9c400000/fffc0000 at 20\n"
    "filter protocol ip pref 1510 u32 chain 0 \n"
    "filter protocol ip pref 1510 u32 chain 0 fh 804: ht divisor 1 \n"
    "filter protocol ip pref 1510 u32 chain 0 fh 804::800 order 2048 key ht 804 bkt 0"
    " flowid 1:b not_in_hw \n"
    "  match 00060000/00ff0000 at 8\n"
    "  match 00350000/ffff0000 at 20\n"
    "filter protocol ip pref 1511 u32 chain 0 \n"
    "filter protocol ip pref 1511 u32 chain 0 fh 805: ht divisor 1 \n"
    "filter protocol ip pref 1511 u32 chain 0 fh 805::800 order 2048 key ht 805 bkt 0"
    " flowid 1:b not_in_hw \n"
    "  match 00110000/00ff0000 at 8\n"
    "  match 00350000/ffff0000 at 20\n"
)

# captured live on test3 in a throwaway centos:7 container, 2026-09-25: otto link
# check's read-back row (sandbox veth, 100ms delay) -- first the whole-link tree,
# then the port-scoped one for dst 5200-5210/tcp; tc -V: tc utility, iproute2-ss170501
CHECK_READBACK_PARAMS = ImpairmentParams(delay_ms=100.0)
CHECK_RANGE_SELECTOR = Selector(5200, "tcp", end=5210, side="dst")
QDISC_CHECK_WHOLE_OLD = "qdisc netem 80a9: root refcnt 3 limit 1000 delay 100.0ms\n"
QDISC_CHECK_SCOPED_OLD = (
    "qdisc prio 1: root refcnt 3 bands 11 priomap  1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1\n"
    "qdisc netem 40: parent 1:4 limit 1000 delay 100.0ms\n"
)
FILTER_CHECK_SCOPED_OLD = (
    "filter protocol ip pref 40 u32 chain 0 \n"
    "filter protocol ip pref 40 u32 chain 0 fh 800: ht divisor 1 \n"
    "filter protocol ip pref 40 u32 chain 0 fh 800::800 order 2048 key ht 800 bkt 0"
    " flowid 1:4 not_in_hw \n"
    "  match 00060000/00ff0000 at 8\n"
    "  match 00001450/0000fff8 at 20\n"
    "filter protocol ip pref 40 u32 chain 0 fh 800::801 order 2049 key ht 800 bkt 0"
    " flowid 1:4 not_in_hw \n"
    "  match 00060000/00ff0000 at 8\n"
    "  match 00001458/0000fffe at 20\n"
    "filter protocol ip pref 40 u32 chain 0 fh 800::802 order 2050 key ht 800 bkt 0"
    " flowid 1:4 not_in_hw \n"
    "  match 00060000/00ff0000 at 8\n"
    "  match 0000145a/0000ffff at 20\n"
)

LIVE_A = {
    Selector(5201, "tcp"): (4, ImpairmentParams(delay_ms=200.0)),
    Selector(53, "udp"): (5, ImpairmentParams(loss_pct=5.0)),
}
LIVE_A_CLEARED = {Selector(5201, "tcp"): (4, ImpairmentParams(delay_ms=200.0))}
LIVE_B = {
    Selector(5000, "tcp", end=5010, side="dst"): (4, ImpairmentParams(delay_ms=200.0)),
    Selector(40000, "udp", end=40003, side="src"): (5, ImpairmentParams(loss_pct=5.0)),
    Selector(53): (11, ImpairmentParams(delay_ms=10.0)),
}
LIVE_B_CLEARED = {
    Selector(40000, "udp", end=40003, side="src"): (5, ImpairmentParams(loss_pct=5.0)),
    Selector(53): (11, ImpairmentParams(delay_ms=10.0)),
}

LEAF4_ROOT = (
    "qdisc prio 1: root refcnt 2 bands 11 priomap 1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1\n"
    "qdisc netem 40: parent 1:4 limit 1000 delay 5ms\n"
)
LEAF5 = "qdisc netem 50: parent 1:5 limit 1000 delay 5ms\n"


def _blk(pref: int, band: int, proto_hex: str, port_match: str) -> str:
    return (
        f"filter parent 1: protocol ip pref {pref} u32 fh 800::800 flowid 1:{band:x}\n"
        f"  match {proto_hex}0000/00ff0000 at 8\n"
        f"  match {port_match} at 20\n"
    )


class TestParseScoped:
    imp = NetEmImpairer()

    def test_clean_variants(self) -> None:
        for qdisc in (
            "",
            "qdisc noqueue 0: root refcnt 2\n",
            # captured live on the unix bed, iproute2 6.1.0, 2026-07-11:
            # `tc qdisc show` on eth1.240 after `tc qdisc del ... root`
            # (real output has a trailing space before the newline)
            "qdisc noqueue 0: root refcnt 2 \n",
            "qdisc pfifo_fast 0: root refcnt 2 bands 3 priomap 1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1\n",
            "qdisc fq_codel 0: root refcnt 2 limit 10240p flows 1024\n",
            "qdisc mq 0: root\n",
        ):
            assert self.imp.parse_scoped(qdisc, "").kind == "clean", qdisc

    def test_whole_link_delegates_to_v1_parser(self) -> None:
        out = "qdisc netem 8001: root refcnt 2 limit 1000 delay 50ms  5ms loss 2%\n"
        state = self.imp.parse_scoped(out, "")
        assert state.kind == "whole"
        assert state.whole == ImpairmentParams(delay_ms=50.0, jitter_ms=5.0, loss_pct=2.0)

    @pytest.mark.parametrize(
        ("qdisc", "filters", "expected"),
        [
            (QDISC_LIVE_A, FILTER_LIVE_A, LIVE_A),
            (QDISC_LIVE_A_CLEARED, FILTER_LIVE_A_CLEARED, LIVE_A_CLEARED),
            (QDISC_LIVE_B, FILTER_LIVE_B, LIVE_B),
            (QDISC_LIVE_B_CLEARED, FILTER_LIVE_B_CLEARED, LIVE_B_CLEARED),
            (QDISC_LIVE_A_OLD, FILTER_LIVE_A_OLD, LIVE_A),
            (QDISC_LIVE_A_CLEARED_OLD, FILTER_LIVE_A_CLEARED_OLD, LIVE_A_CLEARED),
            (QDISC_LIVE_B_OLD, FILTER_LIVE_B_OLD, LIVE_B),
            (QDISC_LIVE_B_CLEARED_OLD, FILTER_LIVE_B_CLEARED_OLD, LIVE_B_CLEARED),
        ],
        ids=[
            "A",
            "A-cleared",
            "B",
            "B-cleared",
            "A-old",
            "A-cleared-old",
            "B-old",
            "B-cleared-old",
        ],
    )
    def test_live_captures_parse_to_their_selectors(
        self, qdisc: str, filters: str, expected: dict
    ) -> None:
        state = self.imp.parse_scoped(qdisc, filters)
        assert state.kind == "scoped"
        assert state.selectors == expected

    @pytest.mark.parametrize(
        ("qdisc", "filters", "expected"),
        [
            (QDISC_CHECK_WHOLE_OLD, "", ScopedState.whole_link(CHECK_READBACK_PARAMS)),
            (
                QDISC_CHECK_SCOPED_OLD,
                FILTER_CHECK_SCOPED_OLD,
                ScopedState.from_selectors({CHECK_RANGE_SELECTOR: (4, CHECK_READBACK_PARAMS)}),
            ),
        ],
        ids=["whole-link", "port-scoped"],
    )
    def test_link_check_readback_parses_old_userland(
        self, qdisc: str, filters: str, expected: ScopedState
    ) -> None:
        """``otto link check``'s read-back row, both trees, as old userland prints them."""
        assert self.imp.parse_scoped(qdisc, filters) == expected

    def test_scoped_proto_none_selector_four_slots(self) -> None:
        qdisc = (
            "qdisc prio 1: root refcnt 2 bands 11 priomap 1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1\n"
            "qdisc netem 40: parent 1:4 limit 1000 delay 200ms\n"
        )
        blocks = []
        for pref, proto_hex, port_match in (
            (440, "0006", "match 00001451/0000ffff at 20"),
            (441, "0011", "match 00001451/0000ffff at 20"),
            (1440, "0006", "match 14510000/ffff0000 at 20"),
            (1441, "0011", "match 14510000/ffff0000 at 20"),
        ):
            blocks.append(
                f"filter parent 1: protocol ip pref {pref} u32 fh 800::800 flowid 1:4\n"
                f"  match {proto_hex}0000/00ff0000 at 8\n"
                f"  {port_match}\n"
            )
        state = self.imp.parse_scoped(qdisc, "".join(blocks))
        assert state.kind == "scoped"
        assert state.selectors == {Selector(5201): (4, ImpairmentParams(delay_ms=200.0))}

    def test_empty_tree_is_clean_not_scoped(self) -> None:
        qdisc = "qdisc prio 1: root refcnt 2 bands 11 priomap 1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1\n"
        assert self.imp.parse_scoped(qdisc, "").kind == "clean"

    def test_old_userland_double_space_priomap_still_recognized(self) -> None:
        # captured live on test3's oldos container, iproute2-ss170501
        # (centos:7), 2026-07-11: `tc qdisc show` renders TWO spaces after
        # "priomap" (`priomap  1 2 ...`); str.split() collapses the run so
        # this is a non-issue, but it's a genuine byte-level old-format
        # quirk worth pinning against regression.
        qdisc = "qdisc prio 1: root refcnt 2 bands 11 priomap  1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1\n"
        assert self.imp.parse_scoped(qdisc, "").kind == "clean"

    def test_hex_band_ten_and_eleven(self) -> None:
        qdisc = (
            "qdisc prio 1: root refcnt 2 bands 11 priomap 1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1\n"
            "qdisc netem a0: parent 1:a limit 1000 delay 1ms\n"
        )
        filt = (
            "filter parent 1: protocol ip pref 300 u32 fh 800::800 flowid 1:a\n"
            "  match 00060000/00ff0000 at 8\n"
            "  match 00000050/0000ffff at 20\n"
            "filter parent 1: protocol ip pref 1300 u32 fh 801::800 flowid 1:a\n"
            "  match 00060000/00ff0000 at 8\n"
            "  match 00500000/ffff0000 at 20\n"
        )
        state = self.imp.parse_scoped(qdisc, filt)
        assert state.kind == "scoped"
        assert state.selectors == {Selector(80, "tcp"): (10, ImpairmentParams(delay_ms=1.0))}

    def test_foreign_variants(self) -> None:
        ours_root = "qdisc prio 1: root refcnt 2 bands 11 priomap 1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1\n"
        ok_filter = (
            "filter parent 1: protocol ip pref 240 u32 fh 800::800 flowid 1:4\n"
            "  match 00060000/00ff0000 at 8\n"
            "  match 00001451/0000ffff at 20\n"
        )
        cases = [
            # human htb root (nonzero handle, not ours)
            ("qdisc htb 8001: root refcnt 2 r2q 10\n", ""),
            # prio root with wrong bands
            ("qdisc prio 1: root refcnt 2 bands 4 priomap 1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1\n", ""),
            # prio root with non-default priomap
            ("qdisc prio 1: root refcnt 2 bands 11 priomap 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0\n", ""),
            # non-netem child under our root
            (ours_root + "qdisc tbf 40: parent 1:4 rate 1Mbit\n", ok_filter),
            # netem child in a reserved band (1:1)
            (ours_root + "qdisc netem 10: parent 1:1 limit 1000 delay 5ms\n", ""),
            # handle/band mismatch (band 4 must be handle 40:)
            (ours_root + "qdisc netem 90: parent 1:4 limit 1000 delay 5ms\n", ok_filter),
            # band netem with NO filters (half-cleared tree)
            (ours_root + "qdisc netem 40: parent 1:4 limit 1000 delay 5ms\n", ""),
            # filters with no netem leaf
            (ours_root, ok_filter),
            # slot/proto mismatch: pref 240 is the dst/tcp slot but matches udp
            (
                ours_root + "qdisc netem 40: parent 1:4 limit 1000 delay 5ms\n",
                (
                    "filter parent 1: protocol ip pref 240 u32 fh 800::800 flowid 1:4\n"
                    "  match 00110000/00ff0000 at 8\n"
                    "  match 00001451/0000ffff at 20\n"
                ),
            ),
            # incomplete slot set: a tier-1 tcp selector with only its dst slot
            # (tier/scope mismatch)
            (ours_root + "qdisc netem 40: parent 1:4 limit 1000 delay 5ms\n", ok_filter),
            # non-minimal decomposition: 5000-5003 + 5004-5007 (minimal is one /fff8)
            (
                LEAF4_ROOT,
                _blk(40, 4, "0006", "00001388/0000fffc") + _blk(40, 4, "0006", "0000138c/0000fffc"),
            ),
            # a gap: ports 5000 through 5007, then 5010
            (
                LEAF4_ROOT,
                _blk(40, 4, "0006", "00001388/0000fff8") + _blk(40, 4, "0006", "00001392/0000ffff"),
            ),
            # pref says band 5, flowid says band 4
            (LEAF4_ROOT, _blk(50, 4, "0006", "00001451/0000ffff")),
            # dst pref carrying a sport-half match
            (LEAF4_ROOT, _blk(40, 4, "0006", "14510000/ffff0000")),
            # non-contiguous mask
            (LEAF4_ROOT, _blk(40, 4, "0006", "00001400/0000ff0f")),
            # duplicate entry
            (LEAF4_ROOT, _blk(40, 4, "0006", "00001451/0000ffff") * 2),
            # two tiers inside one band
            (
                LEAF4_ROOT,
                _blk(40, 4, "0006", "00001451/0000ffff")
                + _blk(1240, 4, "0006", "14510000/ffff0000"),
            ),
            # side index out of range
            (LEAF4_ROOT, _blk(2040, 4, "0006", "00001451/0000ffff")),
            # today's (pre-change) layout for 5201/tcp: prefs 40 + 41
            (
                LEAF4_ROOT,
                _blk(40, 4, "0006", "00001451/0000ffff") + _blk(41, 4, "0006", "14510000/ffff0000"),
            ),
            # same-scope collision: 5200-5207/tcp dst (band 4) and 5201/tcp dst (band 5)
            (
                LEAF4_ROOT + LEAF5,
                _blk(40, 4, "0006", "00001450/0000fff8") + _blk(50, 5, "0006", "00001451/0000ffff"),
            ),
            # cross-scope collision: 5201/tcp (band 4) and 5201 dst (band 5) — neither nests
            (
                LEAF4_ROOT + LEAF5,
                _blk(240, 4, "0006", "00001451/0000ffff")
                + _blk(1240, 4, "0006", "14510000/ffff0000")
                + _blk(250, 5, "0006", "00001451/0000ffff")
                + _blk(251, 5, "0011", "00001451/0000ffff"),
            ),
            # port-0 match: a single-port dst/tcp slot at port 0 reaches
            # port_prefixes(0, 0) via _port_span; _port_half accepts it, but
            # Selector(0, ...) raises (port must be 1-65535) — foreign, not a hang.
            (LEAF4_ROOT, _blk(40, 4, "0006", "00000000/0000ffff")),
        ]
        for qdisc, filt in cases:
            assert self.imp.parse_scoped(qdisc, filt).kind == "foreign", (qdisc, filt)

    def test_truncated_root_line_ending_in_bands_is_foreign(self) -> None:
        # a truncated read (host hiccup mid-command, escaping `read_link_state`'s
        # own nets) can drop every token after "bands"; must never IndexError
        # in `_is_our_prio_root` — just fail our-shape recognition like any
        # other malformed root.
        qdisc = "qdisc prio 1: root refcnt 2 priomap 1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1 bands\n"
        assert self.imp.parse_scoped(qdisc, "").kind == "foreign"

    def test_truncated_band_leaf_ending_in_parent_is_foreign(self) -> None:
        # same truncation story for `_parse_band_leaves`'s "parent" lookup.
        ours_root = "qdisc prio 1: root refcnt 2 bands 11 priomap 1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1\n"
        qdisc = ours_root + "qdisc netem 40: parent\n"
        assert self.imp.parse_scoped(qdisc, "").kind == "foreign"

    def test_truncated_filter_header_ending_in_flowid_is_foreign(self) -> None:
        # same truncation story for `_parse_filter_blocks`'s "flowid" lookup.
        ours_root = "qdisc prio 1: root refcnt 2 bands 11 priomap 1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1\n"
        qdisc = ours_root + "qdisc netem 40: parent 1:4 limit 1000 delay 5ms\n"
        filt = "filter parent 1: protocol ip pref 40 u32 fh 800::800 flowid\n"
        assert self.imp.parse_scoped(qdisc, filt).kind == "foreign"

    def test_builder_parse_roundtrip(self) -> None:
        """What the builders emit, rendered as canned tc output, parses back equal."""
        sel = Selector(5201, "tcp")
        params = ImpairmentParams(delay_ms=200.0)
        qdisc = (
            "qdisc prio 1: root refcnt 2 bands 11 priomap 1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1\n"
            f"qdisc netem 40: parent 1:4 limit 1000 {netem_args(params)}\n"
        )
        filt = (
            "filter parent 1: protocol ip pref 240 u32 fh 800::800 flowid 1:4\n"
            "  match 00060000/00ff0000 at 8\n"
            "  match 00001451/0000ffff at 20\n"
            "filter parent 1: protocol ip pref 1240 u32 fh 801::800 flowid 1:4\n"
            "  match 00060000/00ff0000 at 8\n"
            "  match 14510000/ffff0000 at 20\n"
        )
        state = self.imp.parse_scoped(qdisc, filt)
        assert state.kind == "scoped"
        assert state.selectors == {sel: (4, params)}


def _min_blocks(lo: int, hi: int) -> int:
    """Independent oracle: fewest aligned power-of-two blocks tiling [lo, hi] (DP)."""
    best = {hi + 1: 0}
    for p in range(hi, lo - 1, -1):
        options = []
        size = 1
        while size <= 0x10000:
            if p % size == 0 and p + size <= hi + 1:
                options.append(best[p + size] + 1)
            size *= 2
        best[p] = min(options)
    return best[lo]


# Property campaigns: a pytest timeout bounds the whole campaign, not one
# example, so each is sized as its example count times a per-example budget
# for a stalled runner (the bodies themselves run in microseconds).
_PROPERTY_PER_EXAMPLE_BUDGET_S = 5
_COVER_MAX_EXAMPLES = 200
_COVER_TIMEOUT_S = _COVER_MAX_EXAMPLES * _PROPERTY_PER_EXAMPLE_BUDGET_S
_WALK_MAX_EXAMPLES = 60
_WALK_TIMEOUT_S = _WALK_MAX_EXAMPLES * _PROPERTY_PER_EXAMPLE_BUDGET_S


class TestPortPrefixes:
    def test_worked_example(self) -> None:
        assert port_prefixes(5000, 5010) == [
            PortPrefix(5000, 0xFFF8),
            PortPrefix(5008, 0xFFFE),
            PortPrefix(5010, 0xFFFF),
        ]

    def test_single_port_is_one_exact_prefix(self) -> None:
        assert port_prefixes(5201, 5201) == [PortPrefix(5201, 0xFFFF)]

    def test_lo_zero_whole_space_is_one_wide_open_prefix(self) -> None:
        """``lo == 0`` takes the ``_PORT_SPACE`` branch of the ``size`` guard.

        Without it, ``lo & -lo`` on ``lo == 0`` is ``0``, ``size`` never
        advances, and the loop hangs instead of returning the one prefix
        that matches every port.
        """
        assert port_prefixes(0, 65535) == [PortPrefix(0, 0)]

    def test_full_space_and_worst_case(self) -> None:
        assert len(port_prefixes(1, 65535)) == 16
        assert port_prefixes(1, 65535)[-1] == PortPrefix(32768, 0x8000)
        assert len(port_prefixes(1, 65534)) == 30

    @pytest.mark.parametrize(("lo", "hi"), [(10, 5), (-1, 5), (0, 65536)])
    def test_invalid_bounds(self, lo: int, hi: int) -> None:
        with pytest.raises(ValueError, match="port range"):
            port_prefixes(lo, hi)


# Module-level, not a TestPortPrefixes method: the unit-repeat lane calls every
# test twice in one process, each time on a fresh class instance, and Hypothesis
# fails a @given method seen under two `self`s (HealthCheck.differing_executors).
@settings(max_examples=_COVER_MAX_EXAMPLES, deadline=None)
@given(lo=st.integers(1, 65535), width=st.integers(0, 2047))
@pytest.mark.timeout(_COVER_TIMEOUT_S)
def test_port_prefixes_cover_exactly_disjointly_and_minimally(lo: int, width: int) -> None:
    hi = min(lo + width, 65535)
    prefixes = port_prefixes(lo, hi)
    covered: list[int] = []
    for p in prefixes:
        inverse = ~p.mask & 0xFFFF
        assert inverse & (inverse + 1) == 0, f"mask {p.mask:#06x} not leading-ones"
        assert p.value & inverse == 0, f"{p} not aligned"
        covered.extend(range(p.value, p.value + inverse + 1))
    assert covered == list(range(lo, hi + 1))
    assert len(prefixes) == _min_blocks(lo, hi) <= 30


ROUNDTRIP_CASES = [
    {Selector(5201, "tcp"): (4, ImpairmentParams(delay_ms=200.0))},
    {Selector(5201): (4, ImpairmentParams(delay_ms=200.0))},
    {Selector(1, end=65535): (4, ImpairmentParams(loss_pct=5.0))},
    {Selector(65530, "udp", end=65535, side="src"): (11, ImpairmentParams(loss_pct=1.0))},
    {
        Selector(5200, end=5220): (4, ImpairmentParams(delay_ms=10.0)),
        Selector(5205, "tcp", side="dst"): (5, ImpairmentParams(loss_pct=1.0)),
    },
    {  # eight selectors, every tier, bands 4..11
        Selector(100 * i + 1, proto, end=100 * i + 7, side=side): (
            4 + i,
            ImpairmentParams(delay_ms=float(i + 1)),
        )
        for i, (proto, side) in enumerate(
            [
                (None, None),
                ("tcp", None),
                ("udp", None),
                (None, "dst"),
                (None, "src"),
                ("tcp", "dst"),
                ("udp", "src"),
                ("tcp", "src"),
            ]
        )
    },
]


@pytest.mark.parametrize("mapping", ROUNDTRIP_CASES)
def test_builder_output_reads_back_to_the_same_mapping(mapping: dict) -> None:
    tree = render_scoped_tree(mapping)
    state = NetEmImpairer().parse_scoped(tree.qdisc, tree.filters)
    assert state.kind == "scoped"
    assert state.selectors == mapping


_WALK_RE = re.compile(
    r"pref (?P<pref>\d+) protocol ip u32 match ip protocol (?P<proto>\d+) 0xff "
    r"match ip (?P<field>dport|sport) (?P<val>\d+) 0x(?P<mask>[0-9a-f]{4}) "
    r"flowid 1:(?P<band>[0-9a-f]+)$"
)


def _entries(cmds: list[str]) -> list[list[int]]:
    """``[pref, proto, is_dport, value, mask, band]`` per u32 entry, in the kernel's walk order.

    Pre-decoded to ints once: the property below walks ~1k packets per example.
    """
    rows: list[list[int]] = []
    for cmd in cmds:
        m = _WALK_RE.search(cmd)
        assert m is not None, cmd
        rows.append(
            [
                int(m["pref"]),
                int(m["proto"]),
                int(m["field"] == "dport"),
                int(m["val"]),
                int(m["mask"], 16),
                int(m["band"], 16),
            ]
        )
    return sorted(rows, key=lambda row: row[0])  # stable: a slot's entries keep their order


def _kernel_walk(entries: list[list[int]], proto_num: int, sport: int, dport: int) -> int | None:
    """First match in ascending pref — the band the kernel steers the packet to."""
    for _pref, proto, is_dport, value, mask, band in entries:
        port = dport if is_dport else sport
        if proto == proto_num and port & mask == value:
            return band
    return None


def _spec_winner(chosen: dict[int, Selector], proto: str, sport: int, dport: int) -> int | None:
    """The spec's rule, stated without prefs: destination first, then the narrowest."""
    for side, port in (("dst", dport), ("src", sport)):
        hits = [
            (band, s)
            for band, s in chosen.items()
            if proto in s.protos and side in s.sides and s.port <= port <= s.last
        ]
        if hits:
            narrowest = [h for h in hits if all(scope_contains(o[1], h[1]) for o in hits)]
            assert len(narrowest) == 1, [s.describe() for _, s in hits]
            return narrowest[0][0]
    return None


_SELECTORS = st.builds(
    lambda lo, width, proto, side: Selector(lo, proto, end=min(lo + width, 24), side=side),
    st.integers(1, 24),
    st.integers(0, 6),
    st.sampled_from([None, "tcp", "udp"]),
    st.sampled_from([None, "dst", "src"]),
)


@settings(max_examples=_WALK_MAX_EXAMPLES, deadline=None)
@given(candidates=st.lists(_SELECTORS, min_size=1, max_size=20))
@pytest.mark.timeout(_WALK_TIMEOUT_S)
def test_kernel_walk_is_destination_first_narrowest_wins(candidates: list[Selector]) -> None:
    chosen: dict[int, Selector] = {}
    for sel in candidates:
        if len(chosen) == 8:
            break
        if sel in chosen.values() or any(collides(sel, c) for c in chosen.values()):
            continue
        chosen[4 + len(chosen)] = sel
    imp = NetEmImpairer()
    entries = _entries(
        [c for band, s in chosen.items() for c in imp.scoped_filter_commands("d0", band, s)]
    )
    for proto, num in (("tcp", 6), ("udp", 17)):
        for sport in range(1, 25):
            for dport in range(1, 25):
                assert _kernel_walk(entries, num, sport, dport) == _spec_winner(
                    chosen, proto, sport, dport
                )
