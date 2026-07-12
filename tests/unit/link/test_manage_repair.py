"""repair_link / repair_all / read_link_states orchestration against scripted
fake hosts (no bed); fakes imported from ``test_manage_impair`` (same dir)."""

import pytest

from otto.link.manage import read_link_states, repair_all, repair_link
from otto.link.model import Link, LinkEndpoint
from otto.link.params import ImpairmentParams, Selector
from otto.link.placement import FlowDirection
from otto.link.sentinel import encode_impair_sentinel, encode_impair_sentinel_v2
from otto.result import CommandResult

from .test_manage_impair import (
    FILTER_SCOPED_ONE,
    FILTER_SCOPED_TWO,
    LINK,
    QDISC_SCOPED_ONE,
    QDISC_SCOPED_TWO,
    _bed,
)


class TestRepair:
    @pytest.mark.asyncio
    async def test_repair_clears_impaired_placements_and_timers(self) -> None:
        lab, carrot, tomato, _ = _bed()
        token = encode_impair_sentinel(LINK.id, "eth1.100")
        carrot.ps_text = f"  4242 05:00 {token} -c sleep 600\n"
        # pre-clear read shows impairment; post-clear re-read shows it gone
        carrot.qdisc_texts = ["qdisc netem 8001: root refcnt 2 limit 1000 delay 20ms\n", ""]
        tomato.qdisc_texts = [""]  # b-side has nothing to clear
        report = await repair_link(lab, "edge")
        assert "tc qdisc del dev eth1.100 root" in carrot.sudo_commands
        assert "kill 4242" in carrot.sudo_commands
        assert not any("del" in c for c in tomato.sudo_commands)
        assert [p.netdev for p in report.cleared] == ["eth1.100"]
        assert report.timers_cancelled == 1

    @pytest.mark.asyncio
    async def test_clear_that_does_not_take_raises_host_named(self) -> None:
        # `tc qdisc del` "succeeds" transport-wise but the impairment is still
        # present on re-read -> must fail loud, host/netdev named, not report cleared.
        lab, carrot, tomato, _ = _bed()
        # single-element queue -> the fake keeps returning netem state after del
        carrot.qdisc_texts = ["qdisc netem 8001: root refcnt 2 limit 1000 delay 20ms\n"]
        tomato.qdisc_texts = [""]
        with pytest.raises(RuntimeError, match=r"repair failed on carrot_seed/eth1\.100"):
            await repair_link(lab, "edge")

    @pytest.mark.asyncio
    async def test_repair_all_collects_clear_that_does_not_take(self) -> None:
        lab, carrot, tomato, _ = _bed()
        carrot.qdisc_texts = ["qdisc netem 8001: root refcnt 2 limit 1000 delay 20ms\n"]
        tomato.qdisc_texts = [""]
        reports, failures = await repair_all(lab)
        assert reports == []
        assert len(failures) == 1
        assert "repair failed" in failures[0]

    @pytest.mark.asyncio
    async def test_repair_all_skips_unimpairable_collects_failures(self) -> None:
        unnamed = Link(
            a=LinkEndpoint(host="carrot_seed"), b=LinkEndpoint(host="tomato_seed"), name="bare"
        )
        lab, carrot, _, _ = _bed()
        lab.links.append(unnamed)
        carrot.fail_on = "tc qdisc show"  # the impairable link's read fails
        reports, failures = await repair_all(lab)
        assert reports == []  # the impairable link failed, the bare one skipped
        assert len(failures) == 1
        assert LINK.id in failures[0]


class TestReadStates:
    @pytest.mark.asyncio
    async def test_states_report_per_direction_whole_params(self) -> None:
        lab, carrot, tomato, _ = _bed()
        carrot.qdisc_texts = ["qdisc netem 8001: root refcnt 2 limit 1000 delay 50ms\n"]
        tomato.qdisc_texts = [""]
        (state,) = await read_link_states(lab)
        assert state.impairable
        assert not state.unreachable
        a = state.by_direction[FlowDirection.A_TO_B]
        b = state.by_direction[FlowDirection.B_TO_A]
        assert a is not None
        assert a.whole == ImpairmentParams(delay_ms=50.0)
        assert a.scoped == {}
        assert not a.foreign
        assert b is not None
        assert b.whole is None
        assert b.scoped == {}
        assert not b.foreign

    @pytest.mark.asyncio
    async def test_states_report_scoped_selectors(self) -> None:
        from otto.link.manage import DirectionState
        from otto.link.params import Selector

        from .test_manage_impair import FILTER_SCOPED_ONE, QDISC_SCOPED_ONE

        lab, carrot, tomato, _ = _bed()
        carrot.qdisc_texts = [QDISC_SCOPED_ONE]
        carrot.filter_texts = [FILTER_SCOPED_ONE]
        tomato.qdisc_texts = [""]
        (state,) = await read_link_states(lab)
        assert state.by_direction[FlowDirection.A_TO_B] == DirectionState(
            whole=None, scoped={Selector(5201, "tcp"): ImpairmentParams(delay_ms=200.0)}
        )

    @pytest.mark.asyncio
    async def test_states_report_foreign_flag(self) -> None:
        lab, carrot, tomato, _ = _bed()
        carrot.qdisc_texts = ["qdisc htb 8001: root refcnt 2 r2q 10\n"]
        tomato.qdisc_texts = [""]
        (state,) = await read_link_states(lab)
        a = state.by_direction[FlowDirection.A_TO_B]
        assert a is not None
        assert a.foreign
        assert a.whole is None
        assert a.scoped == {}

    @pytest.mark.asyncio
    async def test_unimpairable_link_marked_not_error(self) -> None:
        bare = Link(
            a=LinkEndpoint(host="carrot_seed"), b=LinkEndpoint(host="tomato_seed"), name="bare"
        )
        lab, *_ = _bed(link=bare)
        (state,) = await read_link_states(lab)
        assert not state.impairable

    @pytest.mark.asyncio
    async def test_unreachable_host_direction_is_none(self) -> None:
        lab, carrot, _, _ = _bed()

        async def _boom(cmd: str, **_: object) -> CommandResult:
            raise ConnectionError("down")

        carrot.exec = _boom  # type: ignore[method-assign]
        (state,) = await read_link_states(lab)
        assert state.unreachable


class TestScopedRepair:
    @pytest.mark.asyncio
    async def test_bare_repair_clears_scoped_tree_and_all_timers(self) -> None:
        v1 = encode_impair_sentinel(LINK.id, "eth1.100")
        v2 = encode_impair_sentinel_v2(LINK.id, "eth1.100", Selector(5201, "tcp"))
        lab, carrot, tomato, _ = _bed()
        carrot.ps_text = f"  4242 05:00 {v1} -c sleep 600\n  4243 05:00 {v2} -c sleep 600\n"
        carrot.qdisc_texts = [QDISC_SCOPED_ONE, ""]
        carrot.filter_texts = [FILTER_SCOPED_ONE, ""]
        tomato.qdisc_texts = [""]
        report = await repair_link(lab, "edge")
        assert "kill 4242 4243" in carrot.sudo_commands
        assert "tc qdisc del dev eth1.100 root" in carrot.sudo_commands
        assert report.timers_cancelled == 2

    @pytest.mark.asyncio
    async def test_selector_repair_clears_one_of_two(self) -> None:
        lab, carrot, tomato, _ = _bed()
        carrot.qdisc_texts = [QDISC_SCOPED_TWO, QDISC_SCOPED_ONE]
        carrot.filter_texts = [FILTER_SCOPED_TWO, FILTER_SCOPED_ONE]
        tomato.qdisc_texts = [""]
        report = await repair_link(lab, "edge", selector=Selector(53, "udp"))
        assert carrot.sudo_commands == [
            "tc filter del dev eth1.100 parent 1: pref 52 protocol ip u32",
            "tc filter del dev eth1.100 parent 1: pref 53 protocol ip u32",
            "tc qdisc del dev eth1.100 parent 1:5 handle 50:",
        ]
        assert [p.netdev for p in report.cleared] == ["eth1.100"]

    @pytest.mark.asyncio
    async def test_selector_repair_of_last_selector_deletes_root(self) -> None:
        lab, carrot, tomato, _ = _bed()
        carrot.qdisc_texts = [QDISC_SCOPED_ONE, ""]
        carrot.filter_texts = [FILTER_SCOPED_ONE, ""]
        tomato.qdisc_texts = [""]
        await repair_link(lab, "edge", selector=Selector(5201, "tcp"))
        assert carrot.sudo_commands == ["tc qdisc del dev eth1.100 root"]

    @pytest.mark.asyncio
    async def test_selector_repair_cancels_only_matching_v2_timer(self) -> None:
        mine = encode_impair_sentinel_v2(LINK.id, "eth1.100", Selector(5201, "tcp"))
        other = encode_impair_sentinel_v2(LINK.id, "eth1.100", Selector(53, "udp"))
        lab, carrot, tomato, _ = _bed()
        carrot.ps_text = f"  4242 05:00 {mine} -c x\n  4243 05:00 {other} -c x\n"
        # post-clear re-read: only 53/udp (band 5) remains
        qdisc_53_only = (
            "qdisc prio 1: root refcnt 2 bands 11 priomap 1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1\n"
            "qdisc netem 50: parent 1:5 limit 1000 loss 5%\n"
        )
        filter_53_only = (
            "filter parent 1: protocol ip pref 52 u32 fh 802::800 flowid 1:5\n"
            "  match 00110000/00ff0000 at 8\n"
            "  match 00000035/0000ffff at 20\n"
            "filter parent 1: protocol ip pref 53 u32 fh 803::800 flowid 1:5\n"
            "  match 00110000/00ff0000 at 8\n"
            "  match 00350000/ffff0000 at 20\n"
        )
        carrot.qdisc_texts = [QDISC_SCOPED_TWO, qdisc_53_only]
        carrot.filter_texts = [FILTER_SCOPED_TWO, filter_53_only]
        tomato.qdisc_texts = [""]
        await repair_link(lab, "edge", selector=Selector(5201, "tcp"))
        assert "kill 4242" in carrot.sudo_commands
        assert not any("4243" in c for c in carrot.sudo_commands)

    @pytest.mark.asyncio
    async def test_selector_repair_against_whole_link_is_loud(self) -> None:
        lab, carrot, tomato, _ = _bed()
        carrot.qdisc_texts = ["qdisc netem 8001: root refcnt 2 limit 1000 delay 20ms\n"]
        tomato.qdisc_texts = [""]
        with pytest.raises(ValueError, match="repair it without --port"):
            await repair_link(lab, "edge", selector=Selector(5201, "tcp"))
        assert not carrot.sudo_commands

    @pytest.mark.asyncio
    async def test_selector_repair_absent_selector_clears_nothing(self) -> None:
        lab, carrot, tomato, _ = _bed()
        carrot.qdisc_texts = [QDISC_SCOPED_ONE]
        carrot.filter_texts = [FILTER_SCOPED_ONE]
        tomato.qdisc_texts = [""]
        report = await repair_link(lab, "edge", selector=Selector(9999, "tcp"))
        assert report.cleared == []
        assert not carrot.sudo_commands

    @pytest.mark.asyncio
    async def test_selector_clear_that_does_not_take_raises(self) -> None:
        lab, carrot, tomato, _ = _bed()
        # single-element queues: state unchanged after the clear commands
        carrot.qdisc_texts = [QDISC_SCOPED_ONE]
        carrot.filter_texts = [FILTER_SCOPED_ONE]
        tomato.qdisc_texts = [""]
        with pytest.raises(RuntimeError, match=r"repair failed on carrot_seed/eth1\.100"):
            await repair_link(lab, "edge", selector=Selector(5201, "tcp"))

    @pytest.mark.asyncio
    async def test_bare_repair_against_foreign_root_refuses(self) -> None:
        lab, carrot, tomato, _ = _bed()
        carrot.qdisc_texts = ["qdisc htb 8001: root refcnt 2 r2q 10\n"]
        tomato.qdisc_texts = [""]
        with pytest.raises(RuntimeError, match="foreign qdisc otto did not create"):
            await repair_link(lab, "edge")
        assert not carrot.sudo_commands
