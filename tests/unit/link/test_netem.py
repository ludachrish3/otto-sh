"""NetEm impairer: exact tc argv (explicit units ALWAYS) + qdisc-show parsing
against canned modern and centos:7-era iproute2 output."""

import pytest

from otto.link.impairer import IMPAIRERS
from otto.link.netem import NetEmImpairer, netem_args, parse_qdisc_show
from otto.link.params import ImpairmentParams, Selector

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

    def test_filter_commands_proto_none_emits_four(self) -> None:
        from otto.link.params import Selector

        cmds = self.imp.scoped_filter_commands("eth1.100", 4, Selector(5201))
        assert cmds == [
            "tc filter add dev eth1.100 parent 1: pref 40 protocol ip u32 "
            "match ip protocol 6 0xff match ip dport 5201 0xffff flowid 1:4",
            "tc filter add dev eth1.100 parent 1: pref 41 protocol ip u32 "
            "match ip protocol 6 0xff match ip sport 5201 0xffff flowid 1:4",
            "tc filter add dev eth1.100 parent 1: pref 42 protocol ip u32 "
            "match ip protocol 17 0xff match ip dport 5201 0xffff flowid 1:4",
            "tc filter add dev eth1.100 parent 1: pref 43 protocol ip u32 "
            "match ip protocol 17 0xff match ip sport 5201 0xffff flowid 1:4",
        ]

    def test_filter_commands_single_proto_uses_its_two_slots(self) -> None:
        from otto.link.params import Selector

        tcp = self.imp.scoped_filter_commands("eth1.100", 5, Selector(5201, "tcp"))
        assert [c.split(" pref ")[1].split(" ")[0] for c in tcp] == ["50", "51"]
        assert all("protocol 6 0xff" in c for c in tcp)
        udp = self.imp.scoped_filter_commands("eth1.100", 5, Selector(53, "udp"))
        assert [c.split(" pref ")[1].split(" ")[0] for c in udp] == ["52", "53"]
        assert all("protocol 17 0xff" in c for c in udp)
        assert all("flowid 1:5" in c for c in udp)

    def test_clear_selector_commands_golden(self) -> None:
        from otto.link.params import Selector

        cmds = self.imp.scoped_clear_selector_commands("eth1.100", 4, Selector(5201, "tcp"))
        assert cmds == [
            "tc filter del dev eth1.100 parent 1: pref 40 protocol ip u32",
            "tc filter del dev eth1.100 parent 1: pref 41 protocol ip u32",
            "tc qdisc del dev eth1.100 parent 1:4 handle 40:",
        ]

    def test_read_commands_golden(self) -> None:
        assert self.imp.scoped_read_commands("eth1.100") == [
            "tc qdisc show dev eth1.100",
            "tc filter show dev eth1.100 parent 1: 2>/dev/null || true",
        ]


# captured live on the veggies bed, iproute2 6.1.0, 2026-07-11
# (carrot_seed, eth1.240 throwaway VLAN; `tc qdisc show dev eth1.240`
# after scoped_root_command + two scoped_band_command leaves — byte-exact,
# matched the hand-modeled fixture with zero drift)
QDISC_SCOPED = (
    "qdisc prio 1: root refcnt 2 bands 11 priomap 1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1\n"
    "qdisc netem 40: parent 1:4 limit 1000 delay 200ms\n"
    "qdisc netem 50: parent 1:5 limit 1000 loss 5%\n"
)
# captured live on the veggies bed, iproute2 6.1.0, 2026-07-11
# (carrot_seed, eth1.240 throwaway VLAN; `tc filter show dev eth1.240 parent 1:`
# after scoped_filter_commands for two selectors). Diverged from the
# hand-modeled Task 5/8 fixture in two ways real bytes proved:
#  1. filter lines carry NO "parent 1:" token at all (only qdisc lines do).
#  2. the flowid is prefixed with a bare "*" ("*flowid 1:4") whenever it
#     resolves into a prio-qdisc band, since those bands are implicit
#     classes (never registered via `tc class add`) that tc's classid
#     lookup can't verify — see _parse_filter_blocks for the parser fix
#     this fixture exercises.
FILTER_SCOPED = (
    "filter protocol ip pref 40 u32 chain 0 \n"
    "filter protocol ip pref 40 u32 chain 0 fh 800: ht divisor 1 \n"
    "filter protocol ip pref 40 u32 chain 0 fh 800::800 order 2048 key ht 800 bkt 0 "
    "*flowid 1:4 not_in_hw \n"
    "  match 00060000/00ff0000 at 8\n"
    "  match 00001451/0000ffff at 20\n"
    "filter protocol ip pref 41 u32 chain 0 \n"
    "filter protocol ip pref 41 u32 chain 0 fh 801: ht divisor 1 \n"
    "filter protocol ip pref 41 u32 chain 0 fh 801::800 order 2048 key ht 801 bkt 0 "
    "*flowid 1:4 not_in_hw \n"
    "  match 00060000/00ff0000 at 8\n"
    "  match 14510000/ffff0000 at 20\n"
    "filter protocol ip pref 52 u32 chain 0 \n"
    "filter protocol ip pref 52 u32 chain 0 fh 802: ht divisor 1 \n"
    "filter protocol ip pref 52 u32 chain 0 fh 802::800 order 2048 key ht 802 bkt 0 "
    "*flowid 1:5 not_in_hw \n"
    "  match 00110000/00ff0000 at 8\n"
    "  match 00000035/0000ffff at 20\n"
    "filter protocol ip pref 53 u32 chain 0 \n"
    "filter protocol ip pref 53 u32 chain 0 fh 803: ht divisor 1 \n"
    "filter protocol ip pref 53 u32 chain 0 fh 803::800 order 2048 key ht 803 bkt 0 "
    "*flowid 1:5 not_in_hw \n"
    "  match 00110000/00ff0000 at 8\n"
    "  match 00350000/ffff0000 at 20\n"
)


class TestParseScoped:
    imp = NetEmImpairer()

    def test_clean_variants(self) -> None:
        for qdisc in (
            "",
            "qdisc noqueue 0: root refcnt 2\n",
            # captured live on the veggies bed, iproute2 6.1.0, 2026-07-11:
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

    def test_scoped_after_live_selector_clear(self) -> None:
        # captured live on the veggies bed, iproute2 6.1.0, 2026-07-11: ran
        # scoped_clear_selector_commands for the udp/53 selector (pref 52/53
        # `tc filter del` + `tc qdisc del parent 1:5 handle 50:`) against the
        # QDISC_SCOPED/FILTER_SCOPED tree above, then re-read both. Both
        # builder commands ran clean on the real bed (no tc error) and the
        # remaining tree parses back to exactly the surviving tcp/5201
        # selector — proves the Task 4 clear builders end-to-end, not just
        # their argv shape.
        qdisc_after_clear = (
            "qdisc prio 1: root refcnt 2 bands 11 priomap 1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1\n"
            "qdisc netem 40: parent 1:4 limit 1000 delay 200ms\n"
        )
        filter_after_clear = (
            "filter protocol ip pref 40 u32 chain 0 \n"
            "filter protocol ip pref 40 u32 chain 0 fh 800: ht divisor 1 \n"
            "filter protocol ip pref 40 u32 chain 0 fh 800::800 order 2048 key ht 800 bkt 0 "
            "*flowid 1:4 not_in_hw \n"
            "  match 00060000/00ff0000 at 8\n"
            "  match 00001451/0000ffff at 20\n"
            "filter protocol ip pref 41 u32 chain 0 \n"
            "filter protocol ip pref 41 u32 chain 0 fh 801: ht divisor 1 \n"
            "filter protocol ip pref 41 u32 chain 0 fh 801::800 order 2048 key ht 801 bkt 0 "
            "*flowid 1:4 not_in_hw \n"
            "  match 00060000/00ff0000 at 8\n"
            "  match 14510000/ffff0000 at 20\n"
        )
        state = self.imp.parse_scoped(qdisc_after_clear, filter_after_clear)
        assert state.kind == "scoped"
        assert state.selectors == {Selector(5201, "tcp"): (4, ImpairmentParams(delay_ms=200.0))}

    def test_scoped_two_selectors_roundtrip(self) -> None:
        state = self.imp.parse_scoped(QDISC_SCOPED, FILTER_SCOPED)
        assert state.kind == "scoped"
        assert state.selectors == {
            Selector(5201, "tcp"): (4, ImpairmentParams(delay_ms=200.0)),
            Selector(53, "udp"): (5, ImpairmentParams(loss_pct=5.0)),
        }

    def test_scoped_proto_none_selector_four_slots(self) -> None:
        qdisc = (
            "qdisc prio 1: root refcnt 2 bands 11 priomap 1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1\n"
            "qdisc netem 40: parent 1:4 limit 1000 delay 200ms\n"
        )
        blocks = []
        for pref, proto_hex, port_match in (
            (40, "0006", "match 00001451/0000ffff at 20"),
            (41, "0006", "match 14510000/ffff0000 at 20"),
            (42, "0011", "match 00001451/0000ffff at 20"),
            (43, "0011", "match 14510000/ffff0000 at 20"),
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
        # captured live on pepper_seed's oldos container, iproute2-ss170501
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
            "filter parent 1: protocol ip pref 100 u32 fh 800::800 flowid 1:a\n"
            "  match 00060000/00ff0000 at 8\n"
            "  match 00000050/0000ffff at 20\n"
            "filter parent 1: protocol ip pref 101 u32 fh 801::800 flowid 1:a\n"
            "  match 00060000/00ff0000 at 8\n"
            "  match 00500000/ffff0000 at 20\n"
        )
        state = self.imp.parse_scoped(qdisc, filt)
        assert state.kind == "scoped"
        assert state.selectors == {Selector(80, "tcp"): (10, ImpairmentParams(delay_ms=1.0))}

    def test_foreign_variants(self) -> None:
        ours_root = "qdisc prio 1: root refcnt 2 bands 11 priomap 1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1\n"
        ok_filter = (
            "filter parent 1: protocol ip pref 40 u32 fh 800::800 flowid 1:4\n"
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
            # slot/proto mismatch: pref 40 is the dport/tcp slot but matches udp
            (
                ours_root + "qdisc netem 40: parent 1:4 limit 1000 delay 5ms\n",
                "filter parent 1: protocol ip pref 40 u32 fh 800::800 flowid 1:4\n"
                "  match 00110000/00ff0000 at 8\n"
                "  match 00001451/0000ffff at 20\n",
            ),
            # incomplete slot set: tcp selector with only its dport filter
            (
                ours_root + "qdisc netem 40: parent 1:4 limit 1000 delay 5ms\n",
                ok_filter.replace("pref 41", "pref 99"),
            ),
        ]
        for qdisc, filt in cases:
            assert self.imp.parse_scoped(qdisc, filt).kind == "foreign", (qdisc, filt)

    def test_truncated_root_line_ending_in_bands_is_foreign(self) -> None:
        # a truncated read (host hiccup mid-command, escaping `_link_state`'s
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
            "filter parent 1: protocol ip pref 40 u32 fh 800::800 flowid 1:4\n"
            "  match 00060000/00ff0000 at 8\n"
            "  match 00001451/0000ffff at 20\n"
            "filter parent 1: protocol ip pref 41 u32 fh 801::800 flowid 1:4\n"
            "  match 00060000/00ff0000 at 8\n"
            "  match 14510000/ffff0000 at 20\n"
        )
        state = self.imp.parse_scoped(qdisc, filt)
        assert state.kind == "scoped"
        assert state.selectors == {sel: (4, params)}
