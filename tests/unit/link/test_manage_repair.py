"""repair_link / repair_all / read_link_states orchestration against scripted
fake hosts (no bed); fakes imported from ``test_manage_impair`` (same dir)."""

import pytest

from otto.link.manage import read_link_states, repair_all, repair_link
from otto.link.model import Link, LinkEndpoint
from otto.link.params import ImpairmentParams
from otto.link.placement import FlowDirection
from otto.link.sentinel import encode_impair_sentinel
from otto.result import CommandResult

from .test_manage_impair import LINK, _bed


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
    async def test_states_report_per_direction_params(self) -> None:
        lab, carrot, tomato, _ = _bed()
        carrot.qdisc_texts = ["qdisc netem 8001: root refcnt 2 limit 1000 delay 50ms\n"]
        tomato.qdisc_texts = [""]
        (state,) = await read_link_states(lab)
        assert state.impairable
        assert not state.unreachable
        assert state.by_direction[FlowDirection.A_TO_B] == ImpairmentParams(delay_ms=50.0)
        assert state.by_direction[FlowDirection.B_TO_A] is None

    @pytest.mark.asyncio
    async def test_unimpairable_link_marked_not_error(self) -> None:
        bare = Link(
            a=LinkEndpoint(host="carrot_seed"), b=LinkEndpoint(host="tomato_seed"), name="bare"
        )
        lab, *_ = _bed(link=bare)
        (state,) = await read_link_states(lab)
        assert not state.impairable

    @pytest.mark.asyncio
    async def test_unreachable_host_marks_state_uncertain(self) -> None:
        lab, carrot, _, _ = _bed()

        async def _boom(cmd: str, **_: object) -> CommandResult:
            raise ConnectionError("down")

        carrot.oneshot = _boom  # type: ignore[method-assign]
        (state,) = await read_link_states(lab)
        assert state.unreachable
