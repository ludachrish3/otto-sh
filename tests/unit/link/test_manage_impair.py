"""impair_link orchestration against scripted fake hosts (no bed).

The fake dispatches on command text the way the tunnel manage fakes do, and
returns REAL CommandResult/Results objects (global constraint)."""

import asyncio
from dataclasses import dataclass, field

import pytest

from otto.link.impairer import LinkImpairer, register_impairer
from otto.link.manage import LinkNotMeasuredError, _exec, find_link, impair_link
from otto.link.model import Link, LinkEndpoint
from otto.link.params import ImpairmentParams, Selector
from otto.link.placement import FlowDirection
from otto.link.sentinel import IMPAIR_PS_COMMAND, encode_impair_sentinel_v2
from otto.result import CommandResult, Results, Status
from tests.conftest import active_context

TEST1_ADDR = (
    "3: eth1    inet 10.10.200.11/24 brd 10.10.200.255 scope global eth1\\  x\n"
    "4: eth1.100    inet 10.10.201.11/24 brd 10.10.201.255 scope global eth1.100\\  x\n"
)
TEST2_ADDR = (
    "3: eth1    inet 10.10.200.12/24 brd 10.10.200.255 scope global eth1\\  x\n"
    "4: eth1.200    inet 10.10.202.12/24 brd 10.10.202.255 scope global eth1.200\\  x\n"
)
TEST3_ADDR = (
    "3: eth1    inet 10.10.200.13/24 brd 10.10.200.255 scope global eth1\\  x\n"
    "4: eth1.100    inet 10.10.201.13/24 brd 10.10.201.255 scope global eth1.100\\  x\n"
    "5: eth1.200    inet 10.10.202.13/24 brd 10.10.202.255 scope global eth1.200\\  x\n"
)
DELAY_50_TEXT = "qdisc netem 8001: root refcnt 2 limit 1000 delay 50ms\n"
"""Post-apply ``tc qdisc show`` text matching ``ImpairmentParams(delay_ms=50.0)`` —
staged as the SECOND (post-apply-verify) entry wherever a test applies that
exact params and needs the verify re-read to observe it, mirroring how
``test_merge_reads_current_and_replaces`` stages its own two-entry queue."""


@dataclass
class FakeHost:
    """Self-consistent fake: `ip -o addr` -> addr_text; `tc qdisc show` -> qdisc_text
    (a queue: pop while >1 left, then repeat); IMPAIR_PS_COMMAND -> ps_text; every
    mutation is recorded verbatim in `commands` and succeeds unless `fail_on`
    matches."""

    id: str
    ip: str
    addr_text: str = ""
    qdisc_texts: list[str] = field(default_factory=lambda: [""])
    filter_texts: list[str] = field(default_factory=lambda: [""])
    ps_text: str = ""
    impairer: str = "netem"
    current_user: str = "vagrant"
    fail_on: str | None = None
    has_bash: bool = True
    """Defaults True, matching ``UnixHost``: only a lab entry or the ``busybox``
    os_profile declares otherwise, and an expire-timer launch is then refused —
    see ``TestExpireOnAHostWithoutBash``."""

    commands: list[str] = field(default_factory=list)
    sudo_commands: list[str] = field(default_factory=list)

    def _result(self, cmd: str) -> CommandResult:
        if self.fail_on is not None and self.fail_on in cmd:
            return CommandResult(
                status=Status.Failed, value="", command=cmd, msg="scripted failure"
            )
        if cmd == "ip -o addr show":
            return CommandResult(status=Status.Success, value=self.addr_text, command=cmd)
        if cmd == IMPAIR_PS_COMMAND:
            return CommandResult(status=Status.Success, value=self.ps_text, command=cmd)
        if cmd.startswith("tc filter show"):
            text = self.filter_texts.pop(0) if len(self.filter_texts) > 1 else self.filter_texts[0]
            return CommandResult(status=Status.Success, value=text, command=cmd)
        if cmd.startswith("tc qdisc show"):
            text = self.qdisc_texts.pop(0) if len(self.qdisc_texts) > 1 else self.qdisc_texts[0]
            return CommandResult(status=Status.Success, value=text, command=cmd)
        return CommandResult(status=Status.Success, value="", command=cmd)

    async def exec(self, cmd: str, timeout: float | None = None, **_: object) -> CommandResult:
        self.commands.append(cmd)
        return self._result(cmd)

    async def run(self, cmd: str, sudo: bool = False, **_: object) -> Results:
        self.commands.append(cmd)
        if sudo:
            self.sudo_commands.append(cmd)
        return Results.collect([self._result(cmd)])


@dataclass
class FakeLab:
    hosts: dict
    links: list

    def static_links(self) -> list:
        return list(self.links)


LINK = Link(
    a=LinkEndpoint(host="test1", interface="eth1.100", ip="10.10.201.11"),
    b=LinkEndpoint(host="test2", interface="eth1.200", ip="10.10.202.12"),
    name="edge",
)
INPATH = Link(a=LINK.a, b=LINK.b, name="dataplane", impair="test3")


def _bed(link: Link = LINK, **host_kw) -> tuple[FakeLab, FakeHost, FakeHost, FakeHost]:
    test1 = FakeHost(id="test1", ip="10.10.200.11", addr_text=TEST1_ADDR, **host_kw)
    test2 = FakeHost(id="test2", ip="10.10.200.12", addr_text=TEST2_ADDR)
    test3 = FakeHost(id="test3", ip="10.10.200.13", addr_text=TEST3_ADDR)
    lab = FakeLab(hosts={h.id: h for h in (test1, test2, test3)}, links=[link])
    return lab, test1, test2, test3


class TestFindLink:
    def test_by_id_and_by_name(self) -> None:
        lab, *_ = _bed()
        assert find_link(lab, LINK.id) is lab.links[0]
        assert find_link(lab, "edge") is lab.links[0]

    def test_unknown_lists_known_ids(self) -> None:
        lab, *_ = _bed()
        with pytest.raises(ValueError, match=f"known: {LINK.id}"):
            find_link(lab, "nope")


class TestEndpointImpair:
    @pytest.mark.asyncio
    async def test_both_directions_apply_on_both_endpoints(self) -> None:
        lab, test1, test2, _ = _bed()
        test1.qdisc_texts = ["", DELAY_50_TEXT]  # pre-read, then post-apply verify
        test2.qdisc_texts = ["", DELAY_50_TEXT]
        report = await impair_link(lab, "edge", ImpairmentParams(delay_ms=50.0))
        assert [a.placement.host_id for a in report.applied] == ["test1", "test2"]
        assert "tc qdisc replace dev eth1.100 root netem delay 50ms" in test1.sudo_commands
        assert "tc qdisc replace dev eth1.200 root netem delay 50ms" in test2.sudo_commands

    @pytest.mark.asyncio
    async def test_from_narrows_to_one_direction(self) -> None:
        lab, test1, test2, _ = _bed()
        test2.qdisc_texts = ["", DELAY_50_TEXT]  # pre-read, then post-apply verify
        report = await impair_link(lab, "edge", ImpairmentParams(delay_ms=50.0), from_host="test2")
        assert [a.placement.direction for a in report.applied] == [FlowDirection.B_TO_A]
        assert not test1.sudo_commands

    @pytest.mark.asyncio
    async def test_from_non_endpoint_rejected(self) -> None:
        lab, *_ = _bed()
        with pytest.raises(ValueError, match="--from 'test3' is not an endpoint"):
            await impair_link(lab, "edge", ImpairmentParams(delay_ms=1.0), from_host="test3")

    @pytest.mark.asyncio
    async def test_merge_reads_current_and_replaces(self) -> None:
        lab, test1, _, _ = _bed()
        applied = "qdisc netem 8001: root refcnt 2 limit 1000 delay 20ms\n"
        merged = "qdisc netem 8001: root refcnt 2 limit 1000 delay 10ms loss 2%\n"
        test1.qdisc_texts = [applied, merged]  # pre-read, then post-apply verify
        await impair_link(
            lab, "edge", ImpairmentParams(delay_ms=10.0, loss_pct=2.0), from_host="test1"
        )
        assert "tc qdisc replace dev eth1.100 root netem delay 10ms loss 2%" in test1.sudo_commands

    @pytest.mark.asyncio
    async def test_merged_to_empty_clears_instead(self) -> None:
        lab, test1, _, _ = _bed()
        test1.qdisc_texts = ["qdisc netem 8001: root refcnt 2 limit 1000 delay 20ms\n", ""]
        await impair_link(lab, "edge", ImpairmentParams(delay_ms=0.0), from_host="test1")
        assert "tc qdisc del dev eth1.100 root" in test1.sudo_commands
        assert not any("replace" in c for c in test1.sudo_commands)

    @pytest.mark.asyncio
    async def test_post_apply_verify_mismatch_raises(self) -> None:
        lab, test1, _, _ = _bed()
        test1.qdisc_texts = ["", ""]  # post-apply read shows nothing applied
        with pytest.raises(RuntimeError, match="post-apply verify failed"):
            await impair_link(lab, "edge", ImpairmentParams(delay_ms=50.0), from_host="test1")

    @pytest.mark.asyncio
    async def test_verify_passes_when_tc_canonicalizes_rate(self) -> None:
        # We apply `rate 1.5mbit`; tc reads it back canonicalized as `1500Kbit`.
        # Structural `==` would false-fail; verify must compare by meaning.
        lab, test1, _, _ = _bed()
        canonical = "qdisc netem 8001: root refcnt 2 limit 1000 rate 1500Kbit\n"
        test1.qdisc_texts = ["", canonical]  # pre-read clean, post-apply canonical form
        await impair_link(lab, "edge", ImpairmentParams(rate="1.5mbit"), from_host="test1")
        assert "tc qdisc replace dev eth1.100 root netem rate 1.5mbit" in test1.sudo_commands


class TestInpath:
    @pytest.mark.asyncio
    async def test_placements_land_on_middlebox(self) -> None:
        lab, test1, test2, test3 = _bed(link=INPATH)
        # one host, two netdevs, one shared read queue: pre-read/verify per direction
        test3.qdisc_texts = ["", DELAY_50_TEXT, "", DELAY_50_TEXT]
        report = await impair_link(lab, "dataplane", ImpairmentParams(delay_ms=50.0))
        assert {a.placement.host_id for a in report.applied} == {"test3"}
        assert {a.placement.netdev for a in report.applied} == {"eth1.100", "eth1.200"}
        assert not test1.sudo_commands
        assert not test2.sudo_commands


class TestRefusalsAndSafety:
    @pytest.mark.asyncio
    async def test_local_endpoint_refused_before_any_command(self) -> None:
        from otto.host.builtin_hosts import BUILTIN_LOCAL_HOST_ID

        local_link = Link(
            a=LinkEndpoint(host=BUILTIN_LOCAL_HOST_ID, interface="eth0", ip="10.0.0.1"),
            b=LINK.b,
            name="to-local",
        )
        lab, test1, test2, _ = _bed(link=local_link)
        with pytest.raises(ValueError, match="local host as an endpoint"):
            await impair_link(lab, "to-local", ImpairmentParams(delay_ms=1.0))
        assert not test1.commands
        assert not test2.commands

    @pytest.mark.asyncio
    async def test_local_impair_middlebox_refused_before_any_command(self) -> None:
        from otto.host.builtin_hosts import BUILTIN_LOCAL_HOST_ID

        mid_link = Link(a=LINK.a, b=LINK.b, name="local-mid", impair=BUILTIN_LOCAL_HOST_ID)
        lab, test1, test2, _ = _bed(link=mid_link)
        # Register a RESOLVABLE local host: without the refusal, impair would
        # actually resolve and run commands on otto's own machine.
        local = FakeHost(id=BUILTIN_LOCAL_HOST_ID, ip="127.0.0.1", addr_text=TEST3_ADDR)
        lab.hosts[BUILTIN_LOCAL_HOST_ID] = local
        with pytest.raises(ValueError, match="local host as its in-path middlebox"):
            await impair_link(lab, "local-mid", ImpairmentParams(delay_ms=1.0))
        assert not local.commands
        assert not test1.commands
        assert not test2.commands

    @pytest.mark.asyncio
    async def test_mgmt_interface_placement_refused(self) -> None:
        mgmt_link = Link(
            a=LinkEndpoint(host="test1", interface="eth1", ip="10.10.200.11"),
            b=LinkEndpoint(host="test2", interface="eth1", ip="10.10.200.12"),
            name="mgmt-edge",
        )
        lab, test1, _, _ = _bed(link=mgmt_link)
        with pytest.raises(ValueError, match="management interface"):
            await impair_link(lab, "mgmt-edge", ImpairmentParams(delay_ms=1.0))
        assert not test1.sudo_commands

    @pytest.mark.asyncio
    async def test_hop_transit_placement_refused_before_mutation(self) -> None:
        # A fourth host reaches otto only THROUGH test3 (its hop), with a mgmt
        # ip inside test3's eth1.200 subnet: impairing dataplane (in-path on
        # test3) would sever otto->beet. Refuse before any mutation.
        lab, _test1, _test2, test3 = _bed(link=INPATH)
        beet = FakeHost(id="beet_seed", ip="10.10.202.77", addr_text="")
        beet.hop = "test3"  # direct hop through the middlebox
        lab.hosts["beet_seed"] = beet
        with pytest.raises(ValueError, match="hop transit"):
            await impair_link(lab, "dataplane", ImpairmentParams(delay_ms=1.0))
        assert not test3.sudo_commands

    @pytest.mark.asyncio
    async def test_hop_transit_transitive_chain_refused(self) -> None:
        # beet -> onion -> test3: beet still transits test3 (transitive walk).
        lab, _test1, _test2, test3 = _bed(link=INPATH)
        onion = FakeHost(id="onion_seed", ip="10.10.99.1", addr_text="")
        onion.hop = "test3"
        beet = FakeHost(id="beet_seed", ip="10.10.202.77", addr_text="")
        beet.hop = "onion_seed"
        lab.hosts["onion_seed"] = onion
        lab.hosts["beet_seed"] = beet
        with pytest.raises(ValueError, match="beet_seed"):
            await impair_link(lab, "dataplane", ImpairmentParams(delay_ms=1.0))
        assert not test3.sudo_commands

    @pytest.mark.asyncio
    async def test_rollback_restores_prior_state_on_partial_failure(self) -> None:
        lab, test1, test2, _ = _bed()
        test1.qdisc_texts = [
            "qdisc netem 8001: root refcnt 2 limit 1000 delay 20ms\n",  # prior state
            "qdisc netem 8001: root refcnt 2 limit 1000 delay 50ms\n",  # verify ok
        ]
        test2.fail_on = "tc qdisc replace"  # second placement fails
        with pytest.raises(RuntimeError):
            await impair_link(lab, "edge", ImpairmentParams(delay_ms=50.0))
        # test1 restored to its PRIOR params, not cleared
        assert test1.sudo_commands[-1] == "tc qdisc replace dev eth1.100 root netem delay 20ms"

    @pytest.mark.asyncio
    async def test_verify_mismatch_rolls_back_own_placement(self) -> None:
        # Single placement: apply succeeds, verify mismatches. The just-mutated
        # placement must itself be restored to prior BEFORE the error propagates
        # (its own rollback entry, not only earlier placements').
        lab, test1, _, _ = _bed()
        test1.qdisc_texts = [
            "qdisc netem 8001: root refcnt 2 limit 1000 delay 20ms\n",  # prior state
            "",  # post-apply verify: nothing there -> mismatch
        ]
        with pytest.raises(RuntimeError, match="post-apply verify failed"):
            await impair_link(lab, "edge", ImpairmentParams(delay_ms=50.0), from_host="test1")
        # restored to prior (delay 20ms), not left half-applied at delay 50ms
        assert test1.sudo_commands[-1] == "tc qdisc replace dev eth1.100 root netem delay 20ms"

    @pytest.mark.asyncio
    async def test_unreachable_host_fails_loud_with_host_name(self) -> None:
        lab, test1, _, _ = _bed()

        async def _boom(cmd: str, **_: object) -> CommandResult:
            raise ConnectionError("boom")

        test1.exec = _boom  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="test1"):
            await impair_link(lab, "edge", ImpairmentParams(delay_ms=1.0))

    @pytest.mark.asyncio
    async def test_cancellation_mid_impair_still_rolls_back(self) -> None:
        """Ctrl+C between placements (CancelledError) must trigger the same
        no-half-impairments restore an Exception does; the restore itself is
        shielded by lifecycle.compensate. Today `except Exception` misses
        cancellation entirely and leaves the first placement half-impaired."""
        lab, test1, test2, _ = _bed()
        test1.qdisc_texts = [
            "qdisc netem 8001: root refcnt 2 limit 1000 delay 20ms\n",  # prior state
            "qdisc netem 8001: root refcnt 2 limit 1000 delay 50ms\n",  # verify ok
        ]

        second_placement_reached = asyncio.Event()

        def _park(host) -> None:
            """Hang every non-addr call on *host* until the test cancels."""

            async def parked(cmd: str, timeout: "float | None" = None, **kw: object):
                if cmd == "ip -o addr show":
                    return host._result(cmd)
                second_placement_reached.set()
                await asyncio.Event().wait()
                raise AssertionError("unreachable")  # parked forever; cancel unwinds

            host.exec = parked  # type: ignore[method-assign]
            host.run = parked  # type: ignore[method-assign]

        _park(test2)

        task = asyncio.ensure_future(impair_link(lab, "edge", ImpairmentParams(delay_ms=50.0)))
        await second_placement_reached.wait()  # test1's placement is fully applied by now
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # test1 (the completed first placement) restored to its PRIOR params
        assert test1.sudo_commands[-1] == "tc qdisc replace dev eth1.100 root netem delay 20ms"

    @pytest.mark.asyncio
    async def test_second_cancellation_mid_rollback_restore_does_not_tear_it(self) -> None:
        """One cancel triggers the no-half-impairments rollback; a SECOND
        cancel landing while the restore command is actually in flight must
        not tear it — that is exactly what lifecycle.compensate's shield at
        this call site is for. Without it (bare `await _rollback(...)`), the
        second cancel lands inside `_restore_state`'s `_root_run` await and
        test1's restore command never reaches the host."""
        lab, test1, test2, _ = _bed()
        test1.qdisc_texts = [
            "qdisc netem 8001: root refcnt 2 limit 1000 delay 20ms\n",  # prior state
            "qdisc netem 8001: root refcnt 2 limit 1000 delay 50ms\n",  # verify ok
        ]

        second_placement_reached = asyncio.Event()
        rollback_restoring = asyncio.Event()

        def _park(host) -> None:
            """Hang every non-addr call on *host* until the test cancels."""

            async def parked(cmd: str, timeout: "float | None" = None, **kw: object):
                if cmd == "ip -o addr show":
                    return host._result(cmd)
                second_placement_reached.set()
                await asyncio.Event().wait()
                raise AssertionError("unreachable")  # parked forever; cancel unwinds

            host.exec = parked  # type: ignore[method-assign]
            host.run = parked  # type: ignore[method-assign]

        _park(test2)

        orig_run = test1.run

        async def gated_run(cmd: str, sudo: bool = False, **kw: object):
            if sudo and "delay 20ms" in cmd:
                # The rollback's restore command: rendezvous with the test so
                # it can deliver the second cancel WHILE this await is live.
                rollback_restoring.set()
                await asyncio.sleep(0)
            return await orig_run(cmd, sudo=sudo, **kw)

        test1.run = gated_run  # type: ignore[method-assign]

        task = asyncio.ensure_future(impair_link(lab, "edge", ImpairmentParams(delay_ms=50.0)))
        await second_placement_reached.wait()  # test1's placement is fully applied by now
        task.cancel()  # 1st cancel: tears the parked test2 placement, triggers rollback
        await rollback_restoring.wait()
        task.cancel()  # 2nd cancel: lands inside the shielded restore — must be held
        with pytest.raises(asyncio.CancelledError):
            await task
        # the restore still reached test1 despite the second, mid-restore cancel
        assert test1.sudo_commands[-1] == "tc qdisc replace dev eth1.100 root netem delay 20ms"


class TestExpireTimers:
    @pytest.mark.asyncio
    async def test_expire_launches_sentinel_tagged_timer(self) -> None:
        lab, test1, _, _ = _bed()
        test1.qdisc_texts = ["", DELAY_50_TEXT]  # pre-read, then post-apply verify
        await impair_link(
            lab, "edge", ImpairmentParams(delay_ms=50.0), from_host="test1", expire=30
        )
        # skip the qdisc-mutation command; find the timer launch
        launch = next(c for c in test1.sudo_commands if "otto-impair:" in c)
        assert "otto-impair:v1:" in launch
        assert "eth1.100" in launch
        assert "sleep 30 && tc qdisc del dev eth1.100 root" in launch
        # Whole conditional wrapped in an outer `bash -c` so the launch string
        # is one opaque word, safe for `_root_run`'s sudo-prefixing to compose
        # with (see otto.host.daemon.launch_command's docstring).
        assert launch.startswith("bash -c 'if command -v systemd-run")

    @pytest.mark.asyncio
    async def test_impair_cancels_stale_timers_first(self) -> None:
        from otto.link.sentinel import encode_impair_sentinel

        lab, test1, _, _ = _bed()
        token = encode_impair_sentinel(LINK.id, "eth1.100")
        test1.ps_text = f"  4242 05:00 {token} -c sleep 600 && tc qdisc del dev eth1.100 root\n"
        test1.qdisc_texts = ["", DELAY_50_TEXT]  # pre-read, then post-apply verify
        await impair_link(lab, "edge", ImpairmentParams(delay_ms=50.0), from_host="test1")
        assert "kill 4242" in test1.sudo_commands


class TestRegistryRoundtrip:
    """Spec §12: a fake impairer selected via the host pin drives the EXACT
    commands run — registration -> selection -> build -> orchestration."""

    @pytest.mark.asyncio
    async def test_fake_impairer_commands_execute(self) -> None:
        from typing import ClassVar

        class _Recorder(LinkImpairer):
            host_families: ClassVar[frozenset[str]] = frozenset({"unix"})

            def apply_command(self, netdev: str, params: ImpairmentParams) -> str:
                return f"FAKE-APPLY {netdev} {params.describe()}"

            def read_command(self, netdev: str) -> str:
                return f"FAKE-READ {netdev}"

            def clear_command(self, netdev: str) -> str:
                return f"FAKE-CLEAR {netdev}"

            def parse_read(self, output: str) -> ImpairmentParams | None:
                return ImpairmentParams(delay_ms=50.0) if "APPLIED" in output else None

        register_impairer("recorder", _Recorder)
        lab, test1, _, _ = _bed()
        test1.impairer = "recorder"  # the host-level pin, post-resolution

        def _fake_result(cmd: str) -> CommandResult:
            if cmd.startswith("FAKE-READ"):
                texts = test1.qdisc_texts
                text = texts.pop(0) if len(texts) > 1 else texts[0]
                return CommandResult(status=Status.Success, value=text, command=cmd)
            return FakeHost._result(test1, cmd)

        test1._result = _fake_result  # type: ignore[method-assign]
        test1.qdisc_texts = ["", "APPLIED"]
        await impair_link(lab, "edge", ImpairmentParams(delay_ms=50.0), from_host="test1")
        assert "FAKE-APPLY eth1.100 delay 50ms" in test1.sudo_commands
        assert not any(c.startswith("tc ") for c in test1.sudo_commands)  # netem never ran

    @pytest.mark.asyncio
    async def test_host_without_impairer_support_fails_loud(self) -> None:
        lab, test1, _, _ = _bed()
        test1.impairer = ""  # e.g. an embedded host: no impairer attribute/value
        with pytest.raises(ValueError, match="no impairer support"):
            await impair_link(lab, "edge", ImpairmentParams(delay_ms=1.0), from_host="test1")


QDISC_SCOPED_ONE = (
    "qdisc prio 1: root refcnt 2 bands 11 priomap 1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1\n"
    "qdisc netem 40: parent 1:4 limit 1000 delay 200ms\n"
)
FILTER_SCOPED_ONE = (
    "filter parent 1: protocol ip pref 40 u32 fh 800::800 flowid 1:4\n"
    "  match 00060000/00ff0000 at 8\n"
    "  match 00001451/0000ffff at 20\n"
    "filter parent 1: protocol ip pref 41 u32 fh 801::800 flowid 1:4\n"
    "  match 00060000/00ff0000 at 8\n"
    "  match 14510000/ffff0000 at 20\n"
)
"""One selector, 5201/tcp delay 200ms, band 4 — the canned scoped read."""


class TestExclusivityAndForeign:
    @pytest.mark.asyncio
    async def test_bare_impair_against_scoped_state_is_loud(self) -> None:
        lab, test1, _, _ = _bed()
        test1.qdisc_texts = [QDISC_SCOPED_ONE]
        test1.filter_texts = [FILTER_SCOPED_ONE]
        with pytest.raises(ValueError, match="has port-scoped impairments — repair them first"):
            await impair_link(lab, "edge", ImpairmentParams(delay_ms=50.0), from_host="test1")
        assert not test1.sudo_commands  # refused BEFORE any mutation

    @pytest.mark.asyncio
    async def test_bare_impair_against_foreign_root_refuses(self) -> None:
        lab, test1, _, _ = _bed()
        test1.qdisc_texts = ["qdisc htb 8001: root refcnt 2 r2q 10\n"]
        # ValueError, like every other structural refusal in this module —
        # otto is declining, nothing failed. See repair_all's skip.
        with pytest.raises(ValueError, match="foreign qdisc otto did not create"):
            await impair_link(lab, "edge", ImpairmentParams(delay_ms=50.0), from_host="test1")
        assert not test1.sudo_commands

    @pytest.mark.asyncio
    async def test_exclusivity_error_mid_link_rolls_back_first_placement(self) -> None:
        # test1 clean (applies fine), test2 scoped -> error; test1 restored to clean.
        lab, test1, test2, _ = _bed()
        test1.qdisc_texts = ["", DELAY_50_TEXT]
        test2.qdisc_texts = [QDISC_SCOPED_ONE]
        test2.filter_texts = [FILTER_SCOPED_ONE]
        with pytest.raises(ValueError, match="port-scoped impairments"):
            await impair_link(lab, "edge", ImpairmentParams(delay_ms=50.0))
        assert test1.sudo_commands[-1] == "tc qdisc del dev eth1.100 root"
        assert not test2.sudo_commands


QDISC_SCOPED_TWO = (
    "qdisc prio 1: root refcnt 2 bands 11 priomap 1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1\n"
    "qdisc netem 40: parent 1:4 limit 1000 delay 200ms\n"
    "qdisc netem 50: parent 1:5 limit 1000 loss 5%\n"
)
FILTER_SCOPED_TWO = FILTER_SCOPED_ONE + (
    "filter parent 1: protocol ip pref 52 u32 fh 802::800 flowid 1:5\n"
    "  match 00110000/00ff0000 at 8\n"
    "  match 00000035/0000ffff at 20\n"
    "filter parent 1: protocol ip pref 53 u32 fh 803::800 flowid 1:5\n"
    "  match 00110000/00ff0000 at 8\n"
    "  match 00350000/ffff0000 at 20\n"
)
"""5201/tcp (band 4, delay 200ms) + 53/udp (band 5, loss 5%)."""


class TestScopedImpair:
    @pytest.mark.asyncio
    async def test_first_selector_builds_root_band_filters(self) -> None:
        lab, test1, _, _ = _bed()
        test1.qdisc_texts = ["", QDISC_SCOPED_ONE]
        test1.filter_texts = ["", FILTER_SCOPED_ONE]
        report = await impair_link(
            lab,
            "edge",
            ImpairmentParams(delay_ms=200.0),
            from_host="test1",
            selector=Selector(5201, "tcp"),
        )
        assert report.applied[0].selector == Selector(5201, "tcp")
        assert test1.sudo_commands == [
            (
                "tc qdisc replace dev eth1.100 root handle 1: prio bands 11 "
                "priomap 1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1"
            ),
            "tc qdisc replace dev eth1.100 parent 1:4 handle 40: netem delay 200ms",
            (
                "tc filter add dev eth1.100 parent 1: pref 40 protocol ip u32 "
                "match ip protocol 6 0xff match ip dport 5201 0xffff flowid 1:4"
            ),
            (
                "tc filter add dev eth1.100 parent 1: pref 41 protocol ip u32 "
                "match ip protocol 6 0xff match ip sport 5201 0xffff flowid 1:4"
            ),
        ]

    @pytest.mark.asyncio
    async def test_second_selector_takes_next_band_no_root_reissue(self) -> None:
        lab, test1, _, _ = _bed()
        test1.qdisc_texts = [QDISC_SCOPED_ONE, QDISC_SCOPED_TWO]
        test1.filter_texts = [FILTER_SCOPED_ONE, FILTER_SCOPED_TWO]
        await impair_link(
            lab,
            "edge",
            ImpairmentParams(loss_pct=5.0),
            from_host="test1",
            selector=Selector(53, "udp"),
        )
        assert not any("prio bands" in c for c in test1.sudo_commands)
        assert "tc qdisc replace dev eth1.100 parent 1:5 handle 50: netem loss 5%" in (
            test1.sudo_commands
        )

    @pytest.mark.asyncio
    async def test_reimpair_merges_keeps_band_no_new_filters(self) -> None:
        merged_qdisc = QDISC_SCOPED_ONE.replace("delay 200ms", "delay 200ms loss 2%")
        lab, test1, _, _ = _bed()
        test1.qdisc_texts = [QDISC_SCOPED_ONE, merged_qdisc]
        test1.filter_texts = [FILTER_SCOPED_ONE]
        await impair_link(
            lab,
            "edge",
            ImpairmentParams(loss_pct=2.0),
            from_host="test1",
            selector=Selector(5201, "tcp"),
        )
        assert test1.sudo_commands == [
            "tc qdisc replace dev eth1.100 parent 1:4 handle 40: netem delay 200ms loss 2%"
        ]

    @pytest.mark.asyncio
    async def test_selector_merged_to_empty_clears_that_selector(self) -> None:
        # zeroing the only param of the only selector -> full clear back to pristine
        lab, test1, _, _ = _bed()
        test1.qdisc_texts = [QDISC_SCOPED_ONE, ""]
        test1.filter_texts = [FILTER_SCOPED_ONE, ""]
        await impair_link(
            lab,
            "edge",
            ImpairmentParams(delay_ms=0.0),
            from_host="test1",
            selector=Selector(5201, "tcp"),
        )
        assert test1.sudo_commands == ["tc qdisc del dev eth1.100 root"]

    @pytest.mark.asyncio
    async def test_scoped_against_whole_link_is_loud(self) -> None:
        lab, test1, _, _ = _bed()
        test1.qdisc_texts = ["qdisc netem 8001: root refcnt 2 limit 1000 delay 20ms\n"]
        with pytest.raises(ValueError, match="has a whole-link impairment — repair it first"):
            await impair_link(
                lab,
                "edge",
                ImpairmentParams(delay_ms=1.0),
                from_host="test1",
                selector=Selector(5201),
            )
        assert not test1.sudo_commands

    @pytest.mark.asyncio
    async def test_ninth_selector_hits_the_cap(self) -> None:
        bands = "".join(
            f"qdisc netem {b:x}0: parent 1:{b:x} limit 1000 delay 1ms\n" for b in range(4, 12)
        )
        filters = "".join(
            f"filter parent 1: protocol ip pref {b * 10} u32 fh 800::800 flowid 1:{b:x}\n"
            f"  match 00060000/00ff0000 at 8\n"
            f"  match {5000 + b:08x}/0000ffff at 20\n"
            f"filter parent 1: protocol ip pref {b * 10 + 1} u32 fh 801::800 flowid 1:{b:x}\n"
            f"  match 00060000/00ff0000 at 8\n"
            f"  match {(5000 + b) << 16:08x}/ffff0000 at 20\n"
            for b in range(4, 12)
        )
        lab, test1, _, _ = _bed()
        test1.qdisc_texts = [
            "qdisc prio 1: root refcnt 2 bands 11 priomap 1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1\n" + bands
        ]
        test1.filter_texts = [filters]
        with pytest.raises(ValueError, match="8 port-scoped impairments"):
            await impair_link(
                lab,
                "edge",
                ImpairmentParams(delay_ms=1.0),
                from_host="test1",
                selector=Selector(9999, "tcp"),
            )
        # The cap error fires inside the mutation attempt, AFTER the rollback
        # entry is registered (same posture as a validate() failure today), so
        # a best-effort restore of the untouched prior mapping may run — but
        # nothing for the rejected selector may ever have been applied.
        assert not any("9999" in c for c in test1.sudo_commands)

    @pytest.mark.asyncio
    async def test_capability_error_names_the_impairer(self) -> None:
        lab, test1, _, _ = _bed()
        register_impairer("plainrec", _make_plain_recorder(), overwrite=True)
        test1.impairer = "plainrec"
        with pytest.raises(ValueError, match="'plainrec' does not support port-scoped"):
            await impair_link(
                lab,
                "edge",
                ImpairmentParams(delay_ms=1.0),
                from_host="test1",
                selector=Selector(80),
            )

    @pytest.mark.asyncio
    async def test_scoped_verify_mismatch_restores_full_prior_mapping(self) -> None:
        # prior: one selector; apply second; verify re-read shows nothing -> rollback
        # must rebuild the COMPLETE prior scoped mapping (root + band + filters).
        lab, test1, _, _ = _bed()
        test1.qdisc_texts = [QDISC_SCOPED_ONE, ""]
        test1.filter_texts = [FILTER_SCOPED_ONE, ""]
        with pytest.raises(RuntimeError, match="post-apply verify failed"):
            await impair_link(
                lab,
                "edge",
                ImpairmentParams(loss_pct=5.0),
                from_host="test1",
                selector=Selector(53, "udp"),
            )
        restore = test1.sudo_commands[-4:]
        assert restore == [
            (
                "tc qdisc replace dev eth1.100 root handle 1: prio bands 11 "
                "priomap 1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1"
            ),
            "tc qdisc replace dev eth1.100 parent 1:4 handle 40: netem delay 200ms",
            (
                "tc filter add dev eth1.100 parent 1: pref 40 protocol ip u32 "
                "match ip protocol 6 0xff match ip dport 5201 0xffff flowid 1:4"
            ),
            (
                "tc filter add dev eth1.100 parent 1: pref 41 protocol ip u32 "
                "match ip protocol 6 0xff match ip sport 5201 0xffff flowid 1:4"
            ),
        ]
        # and the root was cleared before the rebuild
        assert "tc qdisc del dev eth1.100 root" in test1.sudo_commands


def _make_plain_recorder():
    """A minimal legacy impairer class (supports_selectors stays False)."""
    from typing import ClassVar

    class _Plain(LinkImpairer):
        host_families: ClassVar[frozenset[str]] = frozenset({"unix"})

        def apply_command(self, netdev: str, params: ImpairmentParams) -> str:
            return f"PLAIN-APPLY {netdev}"

        def read_command(self, netdev: str) -> str:
            return f"PLAIN-READ {netdev}"

        def clear_command(self, netdev: str) -> str:
            return f"PLAIN-CLEAR {netdev}"

        def parse_read(self, output: str) -> ImpairmentParams | None:
            return None

    return _Plain


class TestScopedTimers:
    @pytest.mark.asyncio
    async def test_expire_launches_v2_timer_with_conditional_root_cleanup(self) -> None:
        lab, test1, _, _ = _bed()
        test1.qdisc_texts = ["", QDISC_SCOPED_ONE]
        test1.filter_texts = ["", FILTER_SCOPED_ONE]
        await impair_link(
            lab,
            "edge",
            ImpairmentParams(delay_ms=200.0),
            from_host="test1",
            selector=Selector(5201, "tcp"),
            expire=30,
        )
        launch = next(c for c in test1.sudo_commands if "otto-impair:" in c)
        # LINK.id may percent-encode in the sentinel; assert the frame + payload
        # tail rather than interpolating the raw id (mirrors the v1 test).
        assert "otto-impair:v2:" in launch
        assert ":eth1.100:5201:tcp" in launch
        assert "sleep 30 && " in launch
        assert "tc filter del dev eth1.100 parent 1: pref 40 protocol ip u32" in launch
        assert "tc qdisc del dev eth1.100 parent 1:4 handle 40:" in launch
        assert (
            'if [ -z "$(tc filter show dev eth1.100 parent 1: 2>/dev/null || true)" ]; '
            "then tc qdisc del dev eth1.100 root; fi" in launch
        )
        assert launch.startswith("bash -c 'if command -v systemd-run")

    @pytest.mark.asyncio
    async def test_scoped_impair_cancels_only_its_selectors_v2_timer(self) -> None:
        v2_mine = encode_impair_sentinel_v2(LINK.id, "eth1.100", Selector(5201, "tcp"))
        v2_other = encode_impair_sentinel_v2(LINK.id, "eth1.100", Selector(53, "udp"))
        lab, test1, _, _ = _bed()
        test1.ps_text = (
            f"  4242 05:00 {v2_mine} -c sleep 600\n  4243 05:00 {v2_other} -c sleep 600\n"
        )
        merged_qdisc = QDISC_SCOPED_ONE.replace("delay 200ms", "delay 200ms loss 2%")
        test1.qdisc_texts = [QDISC_SCOPED_ONE, merged_qdisc]
        test1.filter_texts = [FILTER_SCOPED_ONE]
        await impair_link(
            lab,
            "edge",
            ImpairmentParams(loss_pct=2.0),
            from_host="test1",
            selector=Selector(5201, "tcp"),
        )
        assert "kill 4242" in test1.sudo_commands
        assert not any("4243" in c for c in test1.sudo_commands)

    @pytest.mark.asyncio
    async def test_whole_link_impair_does_not_cancel_v2_timers(self) -> None:
        # a v2 timer for another link's netdev-sharing selector must survive a
        # bare impair (which only owns v1 whole-link timers)
        v2 = encode_impair_sentinel_v2(LINK.id, "eth1.100", Selector(5201, "tcp"))
        lab, test1, _, _ = _bed()
        test1.ps_text = f"  4242 05:00 {v2} -c sleep 600\n"
        test1.qdisc_texts = ["", DELAY_50_TEXT]
        test1.filter_texts = [""]
        await impair_link(lab, "edge", ImpairmentParams(delay_ms=50.0), from_host="test1")
        assert not any(c.startswith("kill") for c in test1.sudo_commands)

    @pytest.mark.asyncio
    async def test_rollback_scoped_to_own_selector_leaves_sibling_timer_running(self) -> None:
        # sibling 53/udp has a LIVE v2 timer; re-impairing 5201/tcp fails verify
        # -> rollback must cancel/restore only 5201/tcp's own scope, never
        # reaping 53/udp's timer (final-review: rollback used to reap
        # EVERY v1+v2 timer on the netdev via `everything=True`, killing a
        # live sibling selector's expire timer too — leaving that sibling's
        # restored impairment with a dead timer, persisting forever instead
        # of expiring).
        v2_sibling = encode_impair_sentinel_v2(LINK.id, "eth1.100", Selector(53, "udp"))
        lab, test1, _, _ = _bed()
        test1.ps_text = f"  4243 05:00 {v2_sibling} -c sleep 600\n"
        test1.qdisc_texts = [QDISC_SCOPED_TWO, ""]  # prior: both selectors; verify: nothing there
        test1.filter_texts = [FILTER_SCOPED_TWO, ""]
        with pytest.raises(RuntimeError, match="post-apply verify failed"):
            await impair_link(
                lab,
                "edge",
                ImpairmentParams(loss_pct=2.0),
                from_host="test1",
                selector=Selector(5201, "tcp"),
            )
        # rollback ran: the full two-selector mapping was rebuilt
        assert "tc qdisc del dev eth1.100 root" in test1.sudo_commands
        assert (
            "tc qdisc replace dev eth1.100 parent 1:4 handle 40: netem delay 200ms"
            in test1.sudo_commands
        )
        assert (
            "tc qdisc replace dev eth1.100 parent 1:5 handle 50: netem loss 5%"
            in test1.sudo_commands
        )
        # the sibling's OWN v2 timer (53/udp, pid 4243) survived: rollback was
        # scoped to THIS run's selector (5201/tcp), not "everything"
        assert not any("4243" in c for c in test1.sudo_commands)


class TestTimeoutStillNamesTheHost:
    """A timed-out host command is a loud, host-named RuntimeError (spec §9).

    ``_exec``/``_root_run`` no longer wrap the host call in an external
    ``asyncio.wait_for`` — the host's own ``timeout=`` now enforces the bound
    and reports it back via ``CommandResult.timed_out`` instead of raising.
    These sites must still convert that into the same host-named error."""

    @pytest.mark.asyncio
    async def test_exec_timeout_raises_host_named_runtime_error(self) -> None:
        from otto.link.manage import _exec

        class _Host:
            id = "test1"

            async def exec(self, cmd: str, timeout: float | None = None, log=None) -> CommandResult:
                return CommandResult(
                    status=Status.Error,
                    value=f"Command timed out after {timeout}s",
                    command=cmd,
                    retcode=-1,
                    timed_out=True,
                )

        with pytest.raises(RuntimeError, match=r"test1.*unreachable"):
            await _exec(_Host(), "tc qdisc show")

    @pytest.mark.asyncio
    async def test_root_run_timeout_raises_host_named_runtime_error(self) -> None:
        """``host.run()`` returns a ``Results``, not a bare ``CommandResult`` —
        the fake mirrors that shape so a regression to ``results.timed_out``
        (``Results`` has no such attribute; only entries do) fails loud here
        instead of only at a real timeout in production."""
        from otto.link.manage import _root_run

        class _Host:
            id = "test1"
            current_user = "vagrant"

            async def run(
                self, cmd: str, sudo: bool = False, timeout: float | None = None, log=None
            ) -> Results:
                return Results.collect(
                    [
                        CommandResult(
                            status=Status.Error,
                            value=f"Command timed out after {timeout}s",
                            command=cmd,
                            retcode=-1,
                            timed_out=True,
                        )
                    ]
                )

        with pytest.raises(RuntimeError, match=r"test1.*unreachable"):
            await _root_run(_Host(), "tc qdisc replace dev eth1.100 root netem delay 50ms")


class TestExpireOnAHostWithoutBash:
    """`otto.link` is the ONLY product path that reaches a tagged-daemon launch.

    ``otto.host.daemon.refuse_if_launch_wrapper_needs_bash`` is the guard; its
    own contract is pinned in ``tests/unit/host/test_daemon_launch_refusal.py``.
    What this class establishes is the half that file cannot: that the two
    launch sites in ``otto.link.manage`` are REACHABLE with a ``has_bash=False``
    host, so the guard is not decoration. Both tests below arrive at the launch
    only after the host has taken a real qdisc mutation, and they assert that
    mutation ran — a guard hoisted to the top of ``impair_link`` would still
    refuse, but these two would no longer be proving that the sites downstream
    of it exist.

    The ``--dry-run`` planner asks the SAME guard the same question, up front,
    where there is no mutation to roll back —
    ``TestDryRunSurfacesTheExpireRefusalUpFront`` below. That is a second
    caller of the guard and not a second launch: it emits nothing.

    otto's only OTHER caller of ``launch_command`` — the socat launch in
    ``otto.tunnel.manage.add_tunnel`` — is NOT reachable with such a host and
    carries no guard, deliberately: ``_resolve_chain`` refuses a
    ``has_bash=False`` host as a chain member, loudly, before the launch plan is
    built. ``tests/unit/tunnel/test_manage_resolve.py``'s
    ``test_busybox_profile_host_rejected_as_chain_member`` pins that through the
    real ``busybox`` profile.

    WHAT THE REFUSAL REPLACES was silence, not a loud failure.
    ``otto.link.manage._root_run`` deliberately does not raise on a non-ok
    result — a qdisc mutation's failure is caught by the caller's own re-read
    instead — and NOTHING re-reads after a timer launch, so the ``bash: not
    found`` came back, was discarded, and ``impair_link`` reported SUCCESS for
    an impairment whose timer did not exist and which therefore never expired.
    ``test_any_other_failed_launch_is_still_unnoticed`` pins that mechanism,
    which is still live for every launch failure that is not this one.
    """

    @pytest.mark.asyncio
    async def test_a_whole_link_expire_is_refused_and_rolled_back(self) -> None:
        from otto.host.errors import UnsupportedOnUserlandError

        lab, test1, _, _ = _bed(has_bash=False)
        test1.qdisc_texts = ["", DELAY_50_TEXT]  # pre-read, then post-apply verify
        with pytest.raises(UnsupportedOnUserlandError) as excinfo:
            await impair_link(
                lab, "edge", ImpairmentParams(delay_ms=50.0), from_host="test1", expire=30
            )
        message = str(excinfo.value)
        assert "daemon-launch" in message
        assert "test1" in message
        assert "--expire" in message, (
            "the refusal has to name the option the operator can drop; the impairment "
            "itself needs no bash"
        )
        assert "tc qdisc replace dev eth1.100 root netem delay 50ms" in test1.sudo_commands, (
            "the launch site sits AFTER this mutation, so without it the test would not "
            "be reaching the site it claims to reach"
        )
        assert not [c for c in test1.sudo_commands if "exec -a" in c], (
            "the bash-only launch line must never be emitted — refusing after sending it "
            "would be the old silent failure with extra steps"
        )
        assert test1.sudo_commands[-1] == "tc qdisc del dev eth1.100 root", (
            "no half-impairments: the refusal takes impair_link's rollback path, so the "
            "link is left as it was found (clean, here)"
        )

    @pytest.mark.asyncio
    async def test_a_port_scoped_expire_is_refused_too(self) -> None:
        """The v2 timer is a SECOND launch site on a mutually exclusive branch.

        `--port` routes through `_launch_selector_timer`, which the whole-link
        test above never touches; both funnel through `_launch_daemon`, and
        this is what says so.
        """
        from otto.host.errors import UnsupportedOnUserlandError

        lab, test1, _, _ = _bed(has_bash=False)
        test1.qdisc_texts = ["", QDISC_SCOPED_ONE]
        test1.filter_texts = ["", FILTER_SCOPED_ONE]
        with pytest.raises(UnsupportedOnUserlandError, match="daemon-launch"):
            await impair_link(
                lab,
                "edge",
                ImpairmentParams(delay_ms=200.0),
                from_host="test1",
                selector=Selector(5201, "tcp"),
                expire=30,
            )
        assert [c for c in test1.sudo_commands if c.startswith("tc filter add")], (
            "the scoped mutation runs before the launch site, so reaching the site "
            "requires having got past it"
        )
        assert not [c for c in test1.sudo_commands if "exec -a" in c]

    @pytest.mark.asyncio
    async def test_an_impair_without_expire_is_not_refused(self) -> None:
        """Scope, and the expensive mistake is refusing something that works.

        `tc` needs no bash. A guard on `impair_link` rather than on the daemon
        launch would take whole-link impairment away from every BusyBox device,
        which is the one thing this workstream exists to support.
        """
        lab, test1, _, _ = _bed(has_bash=False)
        test1.qdisc_texts = ["", DELAY_50_TEXT]
        report = await impair_link(lab, "edge", ImpairmentParams(delay_ms=50.0), from_host="test1")
        assert [a.placement.host_id for a in report.applied] == ["test1"]
        assert "tc qdisc replace dev eth1.100 root netem delay 50ms" in test1.sudo_commands

    @pytest.mark.asyncio
    async def test_repair_is_not_refused_either(self) -> None:
        """Repair kills timers, it never launches one, so it needs no bash.

        `_cancel_timers` reaps through a `ps` scan and `kill`, neither of which
        is the bash wrapper. Nothing on this path goes near `launch_command`.
        """
        from otto.link.manage import repair_link
        from otto.link.sentinel import encode_impair_sentinel

        lab, test1, _, _ = _bed(has_bash=False)
        token = encode_impair_sentinel(LINK.id, "eth1.100")
        test1.ps_text = f"  4242 05:00 {token} -c sleep 600 && tc qdisc del dev eth1.100 root\n"
        test1.qdisc_texts = [DELAY_50_TEXT, ""]
        await repair_link(lab, "edge")
        assert "kill 4242" in test1.sudo_commands

    @pytest.mark.asyncio
    async def test_any_other_failed_launch_is_still_unnoticed(self) -> None:
        """The silence this refusal replaces, on the path where it still lives.

        A host that DOES declare bash reaches the launch, and a launch that
        comes back non-ok is discarded: `_root_run` does not raise, and nothing
        re-reads after a timer launch. So `impair_link` reports success for an
        impairment that will never expire.

        Pinned deliberately rather than fixed here. It is the same defect for a
        different cause, and the fix is a post-launch verify — a change to
        `otto.link`'s own contract, not to the gap registry, and so not this
        change's business. Without this test the claim that the refusal
        replaces SILENCE would be prose nothing executes.
        """
        lab, test1, _, _ = _bed(fail_on="exec -a")
        test1.qdisc_texts = ["", DELAY_50_TEXT]
        report = await impair_link(
            lab, "edge", ImpairmentParams(delay_ms=50.0), from_host="test1", expire=30
        )
        assert [a.placement.host_id for a in report.applied] == ["test1"]
        assert [c for c in test1.sudo_commands if "exec -a" in c], "the launch was not attempted"
        assert test1.sudo_commands[-1].startswith("bash -c 'if command -v systemd-run"), (
            "nothing ran after the failed launch — no re-read, no rollback, no error"
        )


# ===========================================================================
# --dry-run: a preview built from lab data, never a fabricated measurement
# ===========================================================================


MGMT_LINK = Link(
    a=LinkEndpoint(host="test1", interface="eth1", ip="10.10.200.11"),
    b=LinkEndpoint(host="test2", interface="eth1", ip="10.10.200.12"),
    name="mgmt-edge",
)
"""A link declaring each endpoint's MANAGEMENT interface as its data interface.

``TEST1_ADDR``/``TEST2_ADDR`` put ``10.10.200.11``/``10.10.200.12`` — the
hosts' own ``ip`` — on ``eth1``, so ``ensure_not_mgmt`` matches positively and a
real impair of this link is refused as a self-lockout. That refusal is what a
dry run cannot make, and this link is the bed for proving it says so.
"""


class TestDryRunImpairPlansWithoutContact:
    """`impair --dry-run` previews from lab data and touches nothing.

    The hostile condition is INJECTED, not inherited: every test here installs
    ``active_context(dry_run=True)`` itself, and every one asserts against a
    fake whose ``commands`` list would record any contact. ``is_dry_run()`` is
    ``False`` with no context installed, so a test that forgot the context
    would exercise the real path and fail on the plan being ``None`` rather
    than passing quietly.
    """

    @pytest.mark.asyncio
    async def test_endpoint_mode_names_host_netdev_and_the_exact_command(self) -> None:
        lab, test1, test2, _ = _bed()
        with active_context(dry_run=True):
            report = await impair_link(lab, "edge", ImpairmentParams(delay_ms=50.0))

        assert report.plan is not None
        assert report.applied == [], (
            "`applied` means 'verified present after the mutation'; a dry run mutated and "
            "verified nothing, so anything here is a fabricated measurement"
        )
        assert test1.commands + test2.commands == [], "a dry run contacted a host"
        assert report.plan.would == [
            "a->b on test1/eth1.100: tc qdisc replace dev eth1.100 root netem delay 50ms",
            "b->a on test2/eth1.200: tc qdisc replace dev eth1.200 root netem delay 50ms",
        ]

    @pytest.mark.asyncio
    async def test_the_self_lockout_refusals_are_named_as_unchecked(self) -> None:
        """THE HEADLINE. A dry run must never read as "no lockout risk here".

        ``ensure_not_mgmt``/``ensure_not_hop_transit`` refuse only on a POSITIVE
        match in the host's live address table, and the synthetic dry-run reply
        parses to an EMPTY table — so before this change a dry run of an impair
        that WOULD lock otto out of the bed printed no refusal at all, silently.
        Both halves are asserted on the SAME bed: the real run refuses, and the
        dry run says out loud that it could not make that call.
        """
        lab, test1, _, _ = _bed(link=MGMT_LINK)

        with pytest.raises(ValueError, match="self-lockout") as excinfo:
            await impair_link(lab, "mgmt-edge", ImpairmentParams(delay_ms=50.0))
        assert "management interface" in str(excinfo.value), (
            "the control: without a bed whose REAL answer is a refusal, the dry-run half "
            "below would be asserting about a link that was never at risk"
        )

        test1.commands.clear()
        with active_context(dry_run=True):
            report = await impair_link(lab, "mgmt-edge", ImpairmentParams(delay_ms=50.0))

        assert report.plan is not None
        unchecked = " ".join(report.plan.unchecked)
        assert "self-lockout" in unchecked, (
            f"a dry run of an impair that would lock otto out said nothing about it: "
            f"{report.plan.unchecked}"
        )
        assert "management address" in unchecked, (
            "the refusal is named but not the interface it protects"
        )
        assert "hops through it" in unchecked, "the hop-transit half of the refusal is unnamed"
        assert not test1.commands, "the dry run read the address table after all"

    @pytest.mark.asyncio
    async def test_in_path_says_placements_are_unresolvable_not_that_none_exist(self) -> None:
        """The in-path story used to be actively wrong, not merely absent.

        `inpath_placements` subnet-matches the middlebox's live table; on the
        EMPTY one a dry run parses out, `_facing_netdev` finds nothing and the
        command died with `'test3' has no interface on 'test2''s
        subnet` — a real sentence about a host it never asked, and test3 does
        have that interface (the control below).
        """
        lab, _, _, test3 = _bed(link=INPATH)
        test3.qdisc_texts = ["", DELAY_50_TEXT, "", DELAY_50_TEXT]
        real = await impair_link(lab, "dataplane", ImpairmentParams(delay_ms=50.0))
        assert {a.placement.netdev for a in real.applied} == {"eth1.100", "eth1.200"}, (
            "the control: test3 really does face both endpoints, so 'no interface on that "
            "subnet' was never a true statement about this bed"
        )

        test3.commands.clear()
        with active_context(dry_run=True):
            report = await impair_link(lab, "dataplane", ImpairmentParams(delay_ms=50.0))

        assert report.plan is not None
        assert report.plan.would == [], "an in-path placement cannot be named without a device"
        assert "in-path middlebox" in report.plan.unchecked[0]
        assert "no interface on" not in " ".join(report.plan.unchecked), (
            "the dry run repeated the old fabricated finding"
        )
        assert not test3.commands

    @pytest.mark.asyncio
    async def test_an_explicit_zero_plans_a_clear_not_a_zero_valued_netem(self) -> None:
        """`--delay 0` clears that param; the clear only appears after a merge.

        Planning from the params AS GIVEN would render
        `tc qdisc replace ... root netem delay 0ms`, a command otto never emits.
        """
        lab, *_ = _bed()
        with active_context(dry_run=True):
            report = await impair_link(lab, "edge", ImpairmentParams(delay_ms=0.0))

        assert report.plan is not None
        assert report.plan.would == [
            "a->b on test1/eth1.100: tc qdisc del dev eth1.100 root",
            "b->a on test2/eth1.200: tc qdisc del dev eth1.200 root",
        ]

    @pytest.mark.asyncio
    async def test_a_coupling_that_only_the_merge_can_satisfy_is_reported_not_raised(self) -> None:
        """`--jitter` with no `--delay` may be joining an already-applied delay.

        `ImpairmentParams.validate` documents itself as evaluated AFTER the
        merge for exactly that reason, so a dry run must neither refuse (a real
        run might not) nor render the command anyway — `describe()` drops a
        jitter with no delay, so the line would come out as a truncated
        `tc qdisc replace dev eth1.100 root netem ` that no run emits.
        """
        lab, *_ = _bed()
        with active_context(dry_run=True):
            report = await impair_link(
                lab, "edge", ImpairmentParams(jitter_ms=5.0), from_host="test1"
            )

        assert report.plan is not None
        (line,) = report.plan.would
        assert "no command line can be shown" in line
        assert "--jitter requires a delay" in line
        assert "root netem" not in line, f"a truncated netem command was rendered: {line}"

    @pytest.mark.asyncio
    async def test_a_scoped_impair_does_not_render_a_whole_link_command(self) -> None:
        """A `--port` impair never touches the root netem, and its band is live data.

        Falling through to the whole-link branch would print
        `tc qdisc replace ... root netem delay 50ms` — the command for the
        OTHER kind of impairment, and the one this call is refused for
        colliding with.
        """
        lab, *_ = _bed()
        with active_context(dry_run=True):
            report = await impair_link(
                lab,
                "edge",
                ImpairmentParams(delay_ms=50.0),
                from_host="test1",
                selector=Selector(5201, "tcp"),
            )

        assert report.plan is not None
        assert report.plan.would == ["a->b on test1/eth1.100: 5201/tcp delay 50ms"]
        assert "prio band" in report.plan.unchecked[0]

    @pytest.mark.asyncio
    async def test_a_local_link_is_refused_by_a_dry_run_too(self) -> None:
        """`ensure_not_local_link` needs no device, so the dry run makes the same call.

        A planner that swallowed the pure refusals would be LESS faithful than
        the real command, not safer.
        """
        local_link = Link(
            a=LinkEndpoint(host="local", interface="eth0", ip="10.0.0.1"),
            b=LinkEndpoint(host="test2", interface="eth1.200", ip="10.10.202.12"),
            name="loopback-edge",
        )
        lab, *_rest = _bed(link=local_link)
        with active_context(dry_run=True), pytest.raises(ValueError, match="local host"):
            await impair_link(lab, "loopback-edge", ImpairmentParams(delay_ms=50.0))

    @pytest.mark.asyncio
    async def test_a_dry_run_never_reaches_the_read_backstop(self) -> None:
        """`impair_link` short-circuits ABOVE `_exec`, so its raise never fires here.

        Asserted because the backstop is real and loud: if the short-circuit
        were removed or moved below `_resolve_placements`, this command would
        report `LinkNotMeasuredError` instead of a plan — honest, and not a
        product.
        """
        lab, *_ = _bed()
        with active_context(dry_run=True):
            report = await impair_link(lab, "edge", ImpairmentParams(delay_ms=50.0))
        assert report.plan is not None

        # The control: the backstop IS armed, on the same lab and the same context.
        with active_context(dry_run=True), pytest.raises(LinkNotMeasuredError, match="no device"):
            await _exec(lab.hosts["test1"], "ip -o addr show")

    @pytest.mark.asyncio
    async def test_the_backstop_is_inert_outside_a_dry_run(self) -> None:
        _lab, test1, _, _ = _bed()
        result = await _exec(test1, "ip -o addr show")
        assert result.value == TEST1_ADDR


class TestDryRunSurfacesTheExpireRefusalUpFront:
    """The `daemon-launch` refusal a real run only reaches AFTER mutating.

    `_launch_daemon` sits below this placement's applied-and-verified qdisc
    mutation, so a real `--expire` against a bash-less host mutates, refuses,
    and rolls back (`TestExpireOnAHostWithoutBash`). It reads a DECLARED
    `has_bash` and needs no device, so a dry run can and does ask first.
    """

    @pytest.mark.asyncio
    async def test_a_bashless_expire_is_refused_before_anything_is_planned(self) -> None:
        from otto.host.errors import UnsupportedOnUserlandError

        lab, test1, _, _ = _bed(has_bash=False)
        with (
            active_context(dry_run=True),
            pytest.raises(UnsupportedOnUserlandError, match="daemon-launch") as excinfo,
        ):
            await impair_link(
                lab, "edge", ImpairmentParams(delay_ms=50.0), from_host="test1", expire=30
            )
        assert "--expire" in str(excinfo.value), (
            "the refusal has to name the option the operator can drop"
        )
        assert not test1.commands, "a refusal that costs a round trip is not a dry run"

    @pytest.mark.asyncio
    async def test_the_same_dry_run_without_expire_is_not_refused(self) -> None:
        """Only the TIMER needs bash; the impairment itself does not.

        Without this, the test above passes against a planner that refused
        every bash-less host outright.
        """
        lab, *_rest = _bed(has_bash=False)
        with active_context(dry_run=True):
            report = await impair_link(
                lab, "edge", ImpairmentParams(delay_ms=50.0), from_host="test1"
            )
        assert report.plan is not None
        assert report.plan.would == [
            "a->b on test1/eth1.100: tc qdisc replace dev eth1.100 root netem delay 50ms"
        ]

    @pytest.mark.asyncio
    async def test_a_bash_host_plans_the_timer_instead(self) -> None:
        lab, *_ = _bed()
        with active_context(dry_run=True):
            report = await impair_link(
                lab, "edge", ImpairmentParams(delay_ms=50.0), from_host="test1", expire=30
            )
        assert report.plan is not None
        assert report.plan.would[-1] == (
            "a->b on test1/eth1.100: launch an expire timer that clears it after 30s"
        )
