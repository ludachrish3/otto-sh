"""The invariant the two commands share: otto clears everything otto can place.

``impair`` and ``repair`` used to answer different questions about the same
wire, and the gap between the answers was reachable. Two mechanisms caused it:

* the self-lockout refusals read the LOADED lab, so a narrow ``-l`` hid the
  dependent they exist to protect and the guard reported "safe" from an
  incomplete view; and
* ``repair`` ran those same refusals, even though clearing a qdisc cannot lock
  anyone out — so the guard that protects you from the impairment also
  protected the impairment from you.

Composed, they let ``impair --from <the visible end>`` place an impairment that
no ``otto`` command under any single lab scope could then clear. The bed's
BusyBox guest is the shape that found it: one NIC, reached only by hopping
through the host that owns the other end of its wire.

Every test here INJECTS its hostile condition — a lab scope missing a host, a
live qdisc on a hop-transit netdev — rather than inheriting it from a fixture
that happens to have it.
"""

import pytest

from otto.link.manage import impair_link, repair_all, repair_link
from otto.link.model import Link, LinkEndpoint
from otto.link.params import ImpairmentParams

from .test_manage_impair import TEST1_ADDR, FakeHost, FakeLab

# test1 as the bed's hop: its management address on eth1, and the guest's TAP
# on bbeth-1350. The TAP deliberately carries NO management address, so the
# refusal these tests provoke is hop transit and not the mgmt rule — a
# distinction test_the_refusal_under_a_full_scope_is_hop_transit_not_lab_scope
# pins rather than assumes.
TEST1_WITH_TAP = TEST1_ADDR + (
    "9: bbeth-1350    inet 198.51.100.18/30 brd 198.51.100.19 scope global bbeth-1350\\  x\n"
)
GUEST_ADDR = "2: eth0    inet 198.51.100.17/30 brd 198.51.100.19 scope global eth0\\  x\n"

GUEST_LINK = Link(
    a=LinkEndpoint(host="test1", interface="bbeth-1350", ip="198.51.100.18"),
    b=LinkEndpoint(host="bb1350_qemu", interface="eth0", ip="198.51.100.17"),
    name="bb1350-wire",
)

IMPAIRED = "qdisc netem 8001: root refcnt 2 limit 1000 delay 50ms\n"
"""A live whole-link impairment, as ``tc qdisc show`` renders it."""


def _bed(*, see_the_guest: bool, link: Link = GUEST_LINK):
    """The bed, with the lab scope as the injected variable.

    *see_the_guest* is the whole experiment: the wire, the hosts and the hop
    are identical either way, and only whether ``lab.hosts`` contains the guest
    changes. That is what makes the pair of verdicts attributable to lab scope
    rather than to anything about the link.
    """
    test1 = FakeHost(id="test1", ip="10.10.200.11", addr_text=TEST1_WITH_TAP)
    guest = FakeHost(id="bb1350_qemu", ip="198.51.100.17", addr_text=GUEST_ADDR)
    # The hop is what makes test1's TAP a transit netdev; without it
    # `_hop_dependents` finds nothing and the guard under test never arms.
    guest.hop = "test1"
    hosts = {test1.id: test1}
    if see_the_guest:
        hosts[guest.id] = guest
    return FakeLab(hosts=hosts, links=[link]), test1, guest


class TestImpairRefusesAnIncompleteView:
    """``impair`` declines when the loaded lab cannot see both endpoints."""

    @pytest.mark.asyncio
    async def test_a_lab_that_hides_the_dependent_refuses_instead_of_placing(self) -> None:
        """The side door, shut.

        Under this scope `_hop_dependents(lab, "test1")` returns `[]` —
        the guest is not a loaded host, so the guard cannot see the very
        dependent it protects — and `--from test1` narrows placement to
        the one endpoint that IS loaded. Before, that combination placed the
        impairment.
        """
        lab, test1, _ = _bed(see_the_guest=False)

        with pytest.raises(ValueError, match="does not contain") as excinfo:
            await impair_link(
                lab, "bb1350-wire", ImpairmentParams(delay_ms=50.0), from_host="test1"
            )

        assert "bb1350_qemu" in str(excinfo.value), "the refusal must name the host it cannot see"
        # And it refused before touching anything: a refusal that lands after
        # the qdisc does is not a refusal.
        assert not any("tc qdisc" in c for c in test1.commands), test1.commands
        assert test1.sudo_commands == []

    @pytest.mark.asyncio
    async def test_the_refusal_under_a_full_scope_is_hop_transit_not_lab_scope(self) -> None:
        """The discriminator: the new rule must not be what refuses everything.

        Same wire, same `--from`, only the lab scope differs. Here both
        endpoints are loaded, so the lab-scope rule passes and the verdict
        comes from the live hop-transit guard reading test1's address table.
        Without this, a rule that refused unconditionally would pass the test
        above for the wrong reason.
        """
        lab, test1, _ = _bed(see_the_guest=True)

        with pytest.raises(ValueError, match="hop transit") as excinfo:
            await impair_link(
                lab, "bb1350-wire", ImpairmentParams(delay_ms=50.0), from_host="test1"
            )

        assert "does not contain" not in str(excinfo.value)
        assert "bbeth-1350" in str(excinfo.value)
        # It took a live look to decide that — the lab-scope rule is pure.
        assert "ip -o addr show" in test1.commands

    @pytest.mark.asyncio
    async def test_a_dry_run_previews_the_refusal_rather_than_the_placement(self) -> None:
        """Decided from lab data alone, so it is as true of a preview as of a run.

        A preview that promised a placement the real command refuses would be
        previewing the wrong outcome — which is why the check sits ABOVE the
        dry-run short-circuit with `find_link` and `_directions`.
        """
        from tests.conftest import active_context

        lab, test1, _ = _bed(see_the_guest=False)

        with active_context(dry_run=True), pytest.raises(ValueError, match="does not contain"):
            await impair_link(
                lab, "bb1350-wire", ImpairmentParams(delay_ms=50.0), from_host="test1"
            )
        assert test1.commands == []

    @pytest.mark.asyncio
    async def test_the_local_host_refusal_still_outranks_this_one(self) -> None:
        """Precedence, pinned — this rule must not shadow a better message.

        `load_lab` injects `local` into every real lab, so it is never
        genuinely missing; a lab that omits it must still be told its link
        touches otto's own path to the bed, not that its lab is scoped wrong.
        Ordering the two rules the other way round produces exactly that
        swap, and nothing else in the suite catches it.
        """
        from otto.host.builtin_hosts import BUILTIN_LOCAL_HOST_ID

        local_link = Link(
            a=LinkEndpoint(host=BUILTIN_LOCAL_HOST_ID, interface="eth0", ip="10.0.0.1"),
            b=GUEST_LINK.b,
            name="to-local",
        )
        lab, _, _ = _bed(see_the_guest=True, link=local_link)
        assert BUILTIN_LOCAL_HOST_ID not in lab.hosts, "precondition: the lab omits `local`"

        with pytest.raises(ValueError, match="local host as an endpoint"):
            await impair_link(lab, "to-local", ImpairmentParams(delay_ms=50.0))


class TestRepairIsNotGatedByTheLockoutRefusals:
    """Clearing cannot lock anyone out, so the refusals do not apply to it."""

    @pytest.mark.asyncio
    async def test_repair_clears_a_placement_hop_transit_refuses_to_create(self) -> None:
        """The headline: the guard stops protecting the impairment from the operator.

        This is not only the side door's cleanup path. The refusals are
        time-of-check while lab data drifts: impair a netdev legitimately
        today, declare a host behind it tomorrow, and that netdev becomes hop
        transit — at which point otto used to refuse to clear its OWN live
        impairment.
        """
        lab, test1, _ = _bed(see_the_guest=True)
        test1.qdisc_texts = [IMPAIRED, ""]  # impaired, then clean on the verify re-read

        report = await repair_link(lab, "bb1350-wire")

        assert "tc qdisc del dev bbeth-1350 root" in test1.sudo_commands
        assert [p.netdev for p in report.cleared] == ["bbeth-1350"]
        assert report.unreachable == []

    @pytest.mark.asyncio
    async def test_repair_clears_a_placement_on_the_hosts_own_management_interface(self) -> None:
        """The other refusal, same reasoning.

        eth1 carries test1's management address, so `impair` refuses it as a
        self-lockout. Clearing it can only END a lockout, so `repair` must act.
        """
        mgmt_wire = Link(
            a=LinkEndpoint(host="test1", interface="eth1", ip="10.10.200.11"),
            b=LinkEndpoint(host="bb1350_qemu", interface="eth0", ip="198.51.100.17"),
            name="mgmt-wire",
        )
        lab, test1, guest = _bed(see_the_guest=True, link=mgmt_wire)
        test1.qdisc_texts = [IMPAIRED, ""]
        guest.qdisc_texts = [""]

        # Positive control: creating it here is still refused.
        with pytest.raises(ValueError, match="management interface"):
            await impair_link(lab, "mgmt-wire", ImpairmentParams(delay_ms=50.0), from_host="test1")

        report = await repair_link(lab, "mgmt-wire")
        assert "tc qdisc del dev eth1 root" in test1.sudo_commands
        assert [p.netdev for p in report.cleared] == ["eth1"]

    @pytest.mark.asyncio
    async def test_a_clearing_call_resolves_no_address_table_at_all(self) -> None:
        """The fetch existed only to feed the two skipped guards.

        Asserted because it is the difference between "the guards are off" and
        "the guards are off but we still pay for and depend on their input" —
        the latter keeps `repair` needing a reachable host merely to decide
        where to look.
        """
        lab, test1, guest = _bed(see_the_guest=True)
        test1.qdisc_texts = [IMPAIRED, ""]
        guest.qdisc_texts = [""]

        await repair_link(lab, "bb1350-wire")

        assert "ip -o addr show" not in test1.commands, test1.commands
        assert "ip -o addr show" not in guest.commands, guest.commands


class TestRepairReachesWhatItCan:
    """Best-effort across placements: the unreachable end must not strand the other."""

    @pytest.mark.asyncio
    async def test_the_visible_end_is_cleared_and_the_other_is_reported(self) -> None:
        """Before, `_host` raised on the guest and the whole call died — so the
        test1 placement, which otto could see and clear, was stranded by the
        one it could not."""
        lab, test1, _ = _bed(see_the_guest=False)
        test1.qdisc_texts = [IMPAIRED, ""]

        report = await repair_link(lab, "bb1350-wire")

        assert [p.netdev for p in report.cleared] == ["bbeth-1350"]
        assert "tc qdisc del dev bbeth-1350 root" in test1.sudo_commands
        assert len(report.unreachable) == 1
        assert "bb1350_qemu/eth0" in report.unreachable[0]
        assert "not in the loaded lab" in report.unreachable[0]

    @pytest.mark.asyncio
    async def test_a_partial_clear_is_a_sweep_failure_not_a_skip(self) -> None:
        """`skipped` means otto declined a link it never impaired — reassurance
        this link has not earned. A sweep that could not finish must not exit 0."""
        lab, test1, _ = _bed(see_the_guest=False)
        test1.qdisc_texts = [IMPAIRED, ""]

        sweep = await repair_all(lab)

        assert sweep.skipped == [], sweep.skipped
        assert sweep.repaired == []
        assert len(sweep.failures) == 1
        assert "bb1350-wire" in sweep.failures[0] or GUEST_LINK.id in sweep.failures[0]
        assert "could not reach" in sweep.failures[0]

    @pytest.mark.asyncio
    async def test_a_bare_far_end_no_longer_strands_the_named_one(self) -> None:
        """The third instance of the same asymmetry.

        `impair --from <the named end>` against a link whose far end names no
        interface is explicitly supported — `impairment_refusal` documents it.
        `repair` resolves both directions, so the bare end (which could never
        hold state) used to abort the clear of the end that did.
        """
        half_named = Link(
            a=LinkEndpoint(host="test1", interface="bbeth-1350", ip="198.51.100.18"),
            b=LinkEndpoint(host="bb1350_qemu"),
            name="half-named",
        )
        lab, test1, _ = _bed(see_the_guest=True, link=half_named)
        test1.qdisc_texts = [IMPAIRED, ""]

        report = await repair_link(lab, "half-named")

        assert [p.netdev for p in report.cleared] == ["bbeth-1350"]
        # Not reported unreachable: a bare endpoint could never have been
        # impaired, so there is provably nothing there to be unsure about.
        assert report.unreachable == []

    @pytest.mark.asyncio
    async def test_a_link_with_no_named_interface_at_all_is_still_skipped(self) -> None:
        """The preserved half. Nothing could be impaired anywhere on such a
        link, so there is no repair to attempt and `repair --all` still declines
        it BY NAME rather than reporting a vacuous success."""
        bare = Link(a=LinkEndpoint(host="test1"), b=LinkEndpoint(host="bb1350_qemu"), name="bare")
        lab, test1, _ = _bed(see_the_guest=True, link=bare)

        sweep = await repair_all(lab)

        assert sweep.repaired == []
        assert sweep.failures == []
        assert len(sweep.skipped) == 1
        assert "no endpoint names an interface" in sweep.skipped[0]
        assert test1.sudo_commands == []
