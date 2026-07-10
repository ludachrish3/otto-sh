"""NetEm impairer: exact tc argv (explicit units ALWAYS) + qdisc-show parsing
against canned modern and centos:7-era iproute2 output."""

import pytest

from otto.link.impairer import IMPAIRERS
from otto.link.netem import NetEmImpairer, netem_args, parse_qdisc_show
from otto.link.params import ImpairmentParams

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
