"""repair_link / repair_all / read_link_states orchestration against scripted
fake hosts (no bed); fakes imported from ``test_manage_impair`` (same dir)."""

import pytest

from otto.link.manage import (
    LinkCommandFailedError,
    LinkNotMeasuredError,
    _cancel_timers,
    read_link_states,
    repair_all,
    repair_link,
)
from otto.link.model import Link, LinkEndpoint
from otto.link.params import ImpairmentParams, Selector
from otto.link.placement import BOTH_DIRECTIONS, FlowDirection, Placement, impairment_refusal
from otto.link.sentinel import IMPAIR_PS_COMMAND, encode_impair_sentinel, encode_impair_sentinel_v2
from otto.result import CommandResult
from tests.conftest import active_context

from .test_manage_impair import (
    FILTER_SCOPED_ONE,
    FILTER_SCOPED_TWO,
    INPATH,
    LINK,
    QDISC_SCOPED_ONE,
    QDISC_SCOPED_TWO,
    _bed,
)


def _raise_on(host: object, needle: str, exc: BaseException) -> None:
    """Make *host*'s ``exec`` raise *exc* for commands containing *needle* only.

    Scoped rather than blanket, and the scoping is what makes the test hit the
    arm it means to: `_resolve_placements` reads `ip -o addr show` on every
    placement host BEFORE any qdisc read, so a fake that fails EVERYTHING
    never reaches the per-placement arms at all — it aborts placement
    resolution and lands on the whole-link arm instead.
    """
    original = host.exec  # type: ignore[attr-defined]

    async def _exec(cmd: str, **kw: object) -> CommandResult:
        if needle in cmd:
            raise exc
        return await original(cmd, **kw)

    host.exec = _exec  # type: ignore[attr-defined]


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
        sweep = await repair_all(lab)
        assert sweep.repaired == []
        assert len(sweep.failures) == 1
        assert "repair failed" in sweep.failures[0]
        assert sweep.skipped == []

    @pytest.mark.asyncio
    async def test_repair_all_skips_unimpairable_collects_failures(self) -> None:
        unnamed = Link(
            a=LinkEndpoint(host="carrot_seed"), b=LinkEndpoint(host="tomato_seed"), name="bare"
        )
        lab, carrot, _, _ = _bed()
        lab.links.append(unnamed)
        carrot.fail_on = "tc qdisc show"  # the impairable link's read fails
        sweep = await repair_all(lab)
        assert sweep.repaired == []  # the impairable link failed, the bare one skipped
        assert len(sweep.failures) == 1
        assert LINK.id in sweep.failures[0]
        # The bare link is NAMED, not silently dropped: a sweep that declines
        # a link has to say which one and why.
        assert len(sweep.skipped) == 1
        assert "bare" in sweep.skipped[0] or unnamed.id in sweep.skipped[0]

    @pytest.mark.asyncio
    async def test_repair_all_skips_a_foreign_qdisc_rather_than_failing_it(self) -> None:
        """A qdisc otto did not create was never otto's impairment to clear.

        `repair --all` means "undo otto's work"; a foreign root is somebody
        else's, so a sweep has nothing to do about it. It was collected as a
        failure only because the refusal was a RuntimeError like a real
        breakage — the refusal is a ValueError now, which is the same
        convention every other structural refusal in the module uses. The
        single-link `repair <id>` still refuses loudly (TestScopedRepair).
        """
        lab, carrot, tomato, _ = _bed()
        carrot.qdisc_texts = ["qdisc htb 8001: root refcnt 2 r2q 10\n"]
        tomato.qdisc_texts = [""]
        sweep = await repair_all(lab)
        assert sweep.repaired == []
        assert sweep.failures == []
        assert not carrot.sudo_commands
        # ...but REPORTED. A silent skip made `repair --all` print
        # "repaired 0 link(s)" and exit 0, saying nothing about the link it
        # had declined to touch.
        assert len(sweep.skipped) == 1
        assert "foreign qdisc otto did not create" in sweep.skipped[0]
        assert LINK.id in sweep.skipped[0]


class TestCancelTimersHygiene:
    """Cancellation is best-effort against an unreachable host, and ONLY that.

    A scan that could not reach the host is skipped because the caller's very
    next read raises anyway; a scan that RAN and failed means otto cannot see
    the timers about to fire against the state it is midway through changing,
    which is a real failure and not hygiene.
    """

    @pytest.mark.asyncio
    async def test_unreachable_host_cancels_nothing_and_does_not_raise(self) -> None:
        _lab, carrot, *_ = _bed()

        async def _boom(cmd: str, **_: object) -> CommandResult:
            raise ConnectionError("down")

        carrot.exec = _boom  # type: ignore[method-assign]
        assert await _cancel_timers(carrot, LINK.id, "eth1.100") == 0

    @pytest.mark.asyncio
    async def test_failed_scan_on_a_reachable_host_propagates(self) -> None:
        _lab, carrot, *_ = _bed()
        carrot.fail_on = IMPAIR_PS_COMMAND
        with pytest.raises(LinkCommandFailedError, match="carrot_seed"):
            await _cancel_timers(carrot, LINK.id, "eth1.100")


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
    async def test_unimpairable_link_carries_a_reason_naming_every_bad_endpoint(self) -> None:
        """The flag alone made `list` print a bare `n/a`, which on a lab with no
        declared links was the whole output and explained nothing.

        Asking the predicate rather than catching the placement layer's
        ValueError is what makes the cell useful: that exception stops at the
        FIRST bad endpoint, so a user fixing `carrot_seed` would rerun and be
        told about `tomato_seed`. Both are named here."""
        bare = Link(
            a=LinkEndpoint(host="carrot_seed"), b=LinkEndpoint(host="tomato_seed"), name="bare"
        )
        lab, *_ = _bed(link=bare)
        (state,) = await read_link_states(lab)
        assert not state.impairable
        assert state.refusal is not None
        assert "no named interface" in state.refusal
        assert "carrot_seed" in state.refusal
        assert "tomato_seed" in state.refusal

    @pytest.mark.asyncio
    async def test_live_refusal_is_reported_with_its_reason(self) -> None:
        """The other half of `refusal`, and the half a pure predicate CANNOT
        answer: the management-interface and hop-transit checks read each
        placement host's live address table.

        Its own guard, because the structural short-circuit above now returns
        before the `try` — so without this the `except ValueError` branch is
        never entered by any test, and could return `refusal=None` (or raise)
        unnoticed.
        """
        # eth1 IS carrot's management address (10.10.200.11, per CARROT_ADDR),
        # so placing an impairment there would sever otto's path to the host.
        mgmt = Link(
            a=LinkEndpoint(host="carrot_seed", interface="eth1", ip="10.10.200.11"),
            b=LinkEndpoint(host="tomato_seed", interface="eth1.200", ip="10.10.202.12"),
            name="mgmt-edge",
        )
        lab, carrot, *_ = _bed(link=mgmt)
        assert impairment_refusal(mgmt) is None, "positive control: structurally fine"

        (state,) = await read_link_states(lab)
        assert not state.impairable
        assert state.refusal is not None
        assert "management interface" in state.refusal
        assert "eth1" in state.refusal
        # ...and it really did take a live look to find that out.
        assert "ip -o addr show" in carrot.commands

    @pytest.mark.asyncio
    async def test_unreachable_host_direction_is_none(self) -> None:
        lab, carrot, _, _ = _bed()

        async def _boom(cmd: str, **_: object) -> CommandResult:
            raise ConnectionError("down")

        carrot.exec = _boom  # type: ignore[method-assign]
        (state,) = await read_link_states(lab)
        assert state.unreachable
        assert state.read_errors == {}

    @pytest.mark.asyncio
    async def test_reachable_host_with_a_failing_read_is_not_unreachable(self) -> None:
        """The headline read-path defect: one `except RuntimeError` covered both.

        A host that ANSWERS "tc: command not found" was reached. Reporting it
        under `unreachable` sends the operator to look at the network for a
        fault that is the host's own tooling, and the message tc gave was
        thrown away entirely. `read_error` carries it; `unreachable` stays
        False.
        """
        lab, carrot, tomato, _ = _bed()
        carrot.fail_on = "tc qdisc show"
        tomato.qdisc_texts = [""]
        (state,) = await read_link_states(lab)
        assert not state.unreachable
        assert state.read_failed
        message = state.read_errors[FlowDirection.A_TO_B]
        assert "carrot_seed" in message
        assert "tc qdisc show" in message
        assert state.by_direction[FlowDirection.A_TO_B] is None
        # ...and the healthy direction is still reported, not lost with it —
        # nor does it inherit a->b's failure message.
        assert state.by_direction[FlowDirection.B_TO_A] is not None
        assert FlowDirection.B_TO_A not in state.read_errors

    @pytest.mark.asyncio
    async def test_one_endpoint_down_and_the_other_broken_keeps_both_stories(self) -> None:
        """The shape a single link-wide read_error string could not express.

        carrot's qdisc read never answers; tomato's answers and fails. One
        field had to pick a story for both cells; per-direction keeps each
        cell its own.
        """
        lab, carrot, tomato, _ = _bed()
        _raise_on(carrot, "tc qdisc show", ConnectionError("down"))
        tomato.fail_on = "tc qdisc show"
        (state,) = await read_link_states(lab)
        assert state.unreachable  # carrot, on a->b
        assert list(state.read_errors) == [FlowDirection.B_TO_A]  # tomato
        assert "tomato_seed" in state.read_errors[FlowDirection.B_TO_A]

    @pytest.mark.asyncio
    async def test_a_bare_runtimeerror_from_the_host_stack_does_not_kill_the_scan(self) -> None:
        """`link list` NEVER dies (spec §9), and this module's two classes do
        not cover everything a read can raise.

        The host stack underneath raises unnamed RuntimeErrors that no
        ast-grep rule scopes — a dead session (`host/session.py`), a
        declared-but-not-running container (`host/docker_host.py`), an
        unresolvable hop (`host/remote_host.py`). Narrowing the read arms to
        LinkHostUnreachableError/LinkCommandFailedError let those propagate
        straight out of read_link_states, and neither `otto link list` nor any
        other caller has a try. Filed as a read failure, since whether the
        host answered is exactly what such an error does not say.
        """
        lab, carrot, tomato, _ = _bed()
        _raise_on(carrot, "tc qdisc show", RuntimeError("carrot_seed: session is not alive"))
        tomato.qdisc_texts = [""]
        (state,) = await read_link_states(lab)  # must not raise
        assert state.read_failed
        assert "session is not alive" in state.read_errors[FlowDirection.A_TO_B]
        assert state.by_direction[FlowDirection.A_TO_B] is None
        assert not state.unreachable

    @pytest.mark.asyncio
    async def test_a_bare_runtimeerror_resolving_placements_does_not_kill_the_scan(self) -> None:
        """The same guarantee one level up, where the whole link has no shape.

        `_resolve_placements` reads every placement host's address table
        BEFORE any qdisc read, so the host stack's unnamed RuntimeErrors reach
        that call too — and a host failing THERE takes the link's whole
        reading with it, which is why the outer arm needs the same width.
        """
        lab, carrot, _tomato, _ = _bed()

        async def _dead_session(cmd: str, **_: object) -> CommandResult:
            raise RuntimeError("carrot_seed: session is not alive")

        carrot.exec = _dead_session  # type: ignore[method-assign]
        (state,) = await read_link_states(lab)  # must not raise
        assert state.read_failed
        assert state.by_direction == {}
        # Neither direction has a shape, so both carry the same message — the
        # CLI dedupes by message rather than printing the sentence twice.
        assert set(state.read_errors) == set(BOTH_DIRECTIONS)


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
        # ValueError: the structural-refusal convention. `repair --all` skips
        # it (see TestRepairAll) — a foreign qdisc was never otto's to clear.
        with pytest.raises(ValueError, match="foreign qdisc otto did not create"):
            await repair_link(lab, "edge")
        assert not carrot.sudo_commands


# ===========================================================================
# --dry-run: repair previews, and `list` gets a third state
# ===========================================================================


class TestDryRunRepairPlansWithoutContact:
    """`repair --dry-run` previews conditionally and reports nothing cleared.

    Before the short-circuit, the synthetic reply parsed as a CLEAN netdev, so
    every placement took `repair_link`'s `state.kind == "clean"` skip and the
    command reported `cleared (nothing to clear), timers cancelled 0` and
    exited 0 — three claims about a device it never contacted.
    """

    @pytest.mark.asyncio
    async def test_the_clears_are_conditional_and_nothing_is_reported_cleared(self) -> None:
        lab, carrot, tomato, _ = _bed()
        with active_context(dry_run=True):
            report = await repair_link(lab, "edge")

        assert report.plan is not None
        assert report.cleared == []
        assert report.timers_cancelled == 0
        assert carrot.commands + tomato.commands == [], "a dry run contacted a host"
        assert report.plan.would == [
            (
                "carrot_seed/eth1.100: tc qdisc del dev eth1.100 root "
                "(only if the netdev carries otto impairment)"
            ),
            "carrot_seed/eth1.100: cancel every live expire timer for this link",
            (
                "tomato_seed/eth1.200: tc qdisc del dev eth1.200 root "
                "(only if the netdev carries otto impairment)"
            ),
            "tomato_seed/eth1.200: cancel every live expire timer for this link",
        ]
        unchecked = " ".join(report.plan.unchecked)
        assert "timers cancelled 0" in unchecked, (
            "the count the CLI would otherwise print is exactly the claim that has to be "
            "disowned, so the plan names it"
        )

    @pytest.mark.asyncio
    async def test_a_real_repair_on_the_same_bed_still_clears_and_counts(self) -> None:
        """The control: the bed HAS something to clear and a timer to kill, so
        the assertions above are about suppression and not about an empty lab."""
        lab, carrot, tomato, _ = _bed()
        token = encode_impair_sentinel(LINK.id, "eth1.100")
        carrot.ps_text = f"  4242 05:00 {token} -c sleep 600\n"
        carrot.qdisc_texts = ["qdisc netem 8001: root refcnt 2 limit 1000 delay 20ms\n", ""]
        tomato.qdisc_texts = [""]
        report = await repair_link(lab, "edge")
        assert [p.netdev for p in report.cleared] == ["eth1.100"]
        assert report.timers_cancelled == 1
        assert report.plan is None

    @pytest.mark.asyncio
    async def test_repair_all_files_previews_apart_from_repairs(self) -> None:
        """`repaired` counts links CLEARED AND VERIFIED GONE; a preview is neither.

        The sweep's summary line counts that list, so a preview filed there is
        the `repaired N link(s)` fabrication in a second shape.
        """
        lab, *_ = _bed()
        with active_context(dry_run=True):
            sweep = await repair_all(lab)

        assert sweep.repaired == []
        assert sweep.failures == []
        assert [r.link_id for r in sweep.planned] == [LINK.id]
        assert sweep.planned[0].plan is not None

    @pytest.mark.asyncio
    async def test_a_structural_refusal_is_still_a_skip_under_a_dry_run(self) -> None:
        """Skips come from lab data, so a dry run is as entitled to them as a real run."""
        bare = Link(
            a=LinkEndpoint(host="carrot_seed", ip="10.10.201.11"),
            b=LinkEndpoint(host="tomato_seed", ip="10.10.202.12"),
            name="bare-edge",
        )
        lab, *_ = _bed(link=bare)
        with active_context(dry_run=True):
            sweep = await repair_all(lab)

        assert sweep.planned == []
        assert len(sweep.skipped) == 1
        assert "no named interface" in sweep.skipped[0]
        # …and the sweep still knows it was a dry run. `planned` is empty here,
        # so anything derived from it cannot say so — see the CLI half in
        # `test_cli.py::…::test_a_sweep_that_previews_nothing_still_says_it_was_dry`.
        assert sweep.dry_run is True

    @pytest.mark.asyncio
    async def test_a_real_sweep_of_the_same_lab_is_not_marked_dry(self) -> None:
        """The control for the flag: `dry_run` is read from the run, not defaulted on."""
        bare = Link(
            a=LinkEndpoint(host="carrot_seed", ip="10.10.201.11"),
            b=LinkEndpoint(host="tomato_seed", ip="10.10.202.12"),
            name="bare-edge",
        )
        lab, *_ = _bed(link=bare)
        sweep = await repair_all(lab)

        assert sweep.dry_run is False
        assert len(sweep.skipped) == 1


class TestDryRunListReportsNotMeasured:
    """`list --dry-run` says "nothing was asked", not "clean", "?" or "!".

    Reporting a dry run as CLEAN was the dangerous one: it is the only wrong
    answer indistinguishable from a real one. Reporting it as `unreachable`
    sends the operator to the network and as `read_errors` to the host's `tc`,
    both by the same argument that split those two in the first place.
    """

    @pytest.mark.asyncio
    async def test_nothing_is_reported_clean_unreachable_or_read_failed(self) -> None:
        lab, carrot, tomato, _ = _bed()
        with active_context(dry_run=True):
            (state,) = await read_link_states(lab)

        assert state.not_measured is True
        assert state.by_direction == {}, (
            "a per-direction shape here is a parsed answer, and the only thing there was "
            "to parse is a banner"
        )
        assert state.unreachable is False
        assert state.read_errors == {}
        assert state.read_failed is False
        assert state.impairable is True, (
            "the STRUCTURAL predicate is pure and did answer; what it does not cover is "
            "the live refusals, which is what not_measured warns about"
        )
        assert carrot.commands + tomato.commands == []

    @pytest.mark.asyncio
    async def test_the_same_bed_read_for_real_reports_the_impairment(self) -> None:
        """The control. Without it, `not_measured` could be reported by a
        `_link_state` that had simply stopped reading anything."""
        lab, carrot, tomato, _ = _bed()
        carrot.qdisc_texts = ["qdisc netem 8001: root refcnt 2 limit 1000 delay 20ms\n"]
        tomato.qdisc_texts = [""]
        (state,) = await read_link_states(lab)

        assert state.not_measured is False
        assert state.by_direction[FlowDirection.A_TO_B].whole == ImpairmentParams(delay_ms=20.0)

    @pytest.mark.asyncio
    async def test_a_structural_refusal_still_wins_and_is_not_not_measured(self) -> None:
        """`impairment_refusal` needs no lab, no await and no address fetch, so
        it is answered first and completely — there is nothing left unread."""
        bare = Link(
            a=LinkEndpoint(host="carrot_seed", ip="10.10.201.11"),
            b=LinkEndpoint(host="tomato_seed", ip="10.10.202.12"),
            name="bare-edge",
        )
        lab, *_ = _bed(link=bare)
        with active_context(dry_run=True):
            (state,) = await read_link_states(lab)

        assert state.impairable is False
        assert state.refusal is not None
        assert "no named interface" in state.refusal
        assert state.not_measured is False

    @pytest.mark.asyncio
    async def test_the_never_raise_promise_survives_the_new_error(self) -> None:
        """`LinkNotMeasuredError` subclasses `RuntimeError` so the existing arms
        would have caught it anyway; the point of its own arm is the BUCKET.

        A class outside that hierarchy would escape all three arms and break
        `read_link_states`'s per-link never-raise promise, which is asserted
        here by the call above returning at all rather than by inspection.
        """
        assert issubclass(LinkNotMeasuredError, RuntimeError)
        lab, *_ = _bed(link=INPATH)
        with active_context(dry_run=True):
            (state,) = await read_link_states(lab)
        assert state.not_measured is True

    @pytest.mark.asyncio
    async def test_a_backstop_raise_inside_the_read_loop_is_not_filed_as_a_read_failure(
        self, monkeypatch
    ) -> None:
        """The INNER arm, reached by injecting the one refactor that would reach it.

        Unreachable today, and the proof is in `_link_state` beside the arm:
        under a dry run the loop is entered only if `_resolve_placements`
        returned, and that always issues at least one `_exec` first, so the
        OUTER arm always wins. The premise is one plausible change away — cache
        address tables across links and placement resolution goes pure for a
        warm host — and this test IS that change, made locally: a
        `_resolve_placements` that hands back a placement without contacting
        anything. Nothing else is faked; the raise comes from the real backstop
        under the real `_read_state`.

        Simulated, and deliberately not skipped as "cannot happen": the arm can
        be shown red (delete the `except LinkNotMeasuredError: raise` and this
        goes to `read_errors`, rendering `!` cells and "host reachable, read
        command failed" — both false), so it is a guard that CAN fail, which is
        the bar. An arm left open because its bad path is currently
        unreachable is how "no read path can quietly start fabricating"
        weakens to "fails into the wrong bucket".
        """
        from otto.link import manage

        lab, *_ = _bed()

        async def _pure_resolve(_lab: object, _link: object, _directions: object) -> list:
            return [Placement("carrot_seed", "eth1.100", FlowDirection.A_TO_B)]

        monkeypatch.setattr(manage, "_resolve_placements", _pure_resolve)
        with active_context(dry_run=True):
            (state,) = await read_link_states(lab)

        assert state.not_measured is True
        assert state.read_errors == {}
        assert state.read_failed is False
        assert state.unreachable is False
