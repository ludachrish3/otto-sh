"""impair_link orchestration against scripted fake hosts (no bed).

The fake dispatches on command text the way the tunnel manage fakes do, and
returns REAL CommandResult/Results objects (global constraint)."""

from dataclasses import dataclass, field

import pytest

from otto.link.impairer import LinkImpairer, register_impairer
from otto.link.manage import find_link, impair_link
from otto.link.model import Link, LinkEndpoint
from otto.link.params import ImpairmentParams
from otto.link.placement import FlowDirection
from otto.link.sentinel import IMPAIR_PS_COMMAND
from otto.result import CommandResult, Results, Status

CARROT_ADDR = (
    "3: eth1    inet 10.10.200.11/24 brd 10.10.200.255 scope global eth1\\  x\n"
    "4: eth1.100    inet 10.10.201.11/24 brd 10.10.201.255 scope global eth1.100\\  x\n"
)
TOMATO_ADDR = (
    "3: eth1    inet 10.10.200.12/24 brd 10.10.200.255 scope global eth1\\  x\n"
    "4: eth1.200    inet 10.10.202.12/24 brd 10.10.202.255 scope global eth1.200\\  x\n"
)
PEPPER_ADDR = (
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
    ps_text: str = ""
    impairer: str = "netem"
    current_user: str = "vagrant"
    fail_on: str | None = None
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
    a=LinkEndpoint(host="carrot_seed", interface="eth1.100", ip="10.10.201.11"),
    b=LinkEndpoint(host="tomato_seed", interface="eth1.200", ip="10.10.202.12"),
    name="edge",
)
INPATH = Link(a=LINK.a, b=LINK.b, name="dataplane", impair="pepper_seed")


def _bed(link: Link = LINK, **host_kw) -> tuple[FakeLab, FakeHost, FakeHost, FakeHost]:
    carrot = FakeHost(id="carrot_seed", ip="10.10.200.11", addr_text=CARROT_ADDR, **host_kw)
    tomato = FakeHost(id="tomato_seed", ip="10.10.200.12", addr_text=TOMATO_ADDR)
    pepper = FakeHost(id="pepper_seed", ip="10.10.200.13", addr_text=PEPPER_ADDR)
    lab = FakeLab(hosts={h.id: h for h in (carrot, tomato, pepper)}, links=[link])
    return lab, carrot, tomato, pepper


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
        lab, carrot, tomato, _ = _bed()
        carrot.qdisc_texts = ["", DELAY_50_TEXT]  # pre-read, then post-apply verify
        tomato.qdisc_texts = ["", DELAY_50_TEXT]
        report = await impair_link(lab, "edge", ImpairmentParams(delay_ms=50.0))
        assert [a.placement.host_id for a in report.applied] == ["carrot_seed", "tomato_seed"]
        assert "tc qdisc replace dev eth1.100 root netem delay 50ms" in carrot.sudo_commands
        assert "tc qdisc replace dev eth1.200 root netem delay 50ms" in tomato.sudo_commands

    @pytest.mark.asyncio
    async def test_from_narrows_to_one_direction(self) -> None:
        lab, carrot, tomato, _ = _bed()
        tomato.qdisc_texts = ["", DELAY_50_TEXT]  # pre-read, then post-apply verify
        report = await impair_link(
            lab, "edge", ImpairmentParams(delay_ms=50.0), from_host="tomato_seed"
        )
        assert [a.placement.direction for a in report.applied] == [FlowDirection.B_TO_A]
        assert not carrot.sudo_commands

    @pytest.mark.asyncio
    async def test_from_non_endpoint_rejected(self) -> None:
        lab, *_ = _bed()
        with pytest.raises(ValueError, match="--from 'pepper_seed' is not an endpoint"):
            await impair_link(lab, "edge", ImpairmentParams(delay_ms=1.0), from_host="pepper_seed")

    @pytest.mark.asyncio
    async def test_merge_reads_current_and_replaces(self) -> None:
        lab, carrot, _, _ = _bed()
        applied = "qdisc netem 8001: root refcnt 2 limit 1000 delay 20ms\n"
        merged = "qdisc netem 8001: root refcnt 2 limit 1000 delay 10ms loss 2%\n"
        carrot.qdisc_texts = [applied, merged]  # pre-read, then post-apply verify
        await impair_link(
            lab, "edge", ImpairmentParams(delay_ms=10.0, loss_pct=2.0), from_host="carrot_seed"
        )
        assert "tc qdisc replace dev eth1.100 root netem delay 10ms loss 2%" in carrot.sudo_commands

    @pytest.mark.asyncio
    async def test_merged_to_empty_clears_instead(self) -> None:
        lab, carrot, _, _ = _bed()
        carrot.qdisc_texts = ["qdisc netem 8001: root refcnt 2 limit 1000 delay 20ms\n", ""]
        await impair_link(lab, "edge", ImpairmentParams(delay_ms=0.0), from_host="carrot_seed")
        assert "tc qdisc del dev eth1.100 root" in carrot.sudo_commands
        assert not any("replace" in c for c in carrot.sudo_commands)

    @pytest.mark.asyncio
    async def test_post_apply_verify_mismatch_raises(self) -> None:
        lab, carrot, _, _ = _bed()
        carrot.qdisc_texts = ["", ""]  # post-apply read shows nothing applied
        with pytest.raises(RuntimeError, match="post-apply verify failed"):
            await impair_link(lab, "edge", ImpairmentParams(delay_ms=50.0), from_host="carrot_seed")

    @pytest.mark.asyncio
    async def test_verify_passes_when_tc_canonicalizes_rate(self) -> None:
        # We apply `rate 1.5mbit`; tc reads it back canonicalized as `1500Kbit`.
        # Structural `==` would false-fail; verify must compare by meaning.
        lab, carrot, _, _ = _bed()
        canonical = "qdisc netem 8001: root refcnt 2 limit 1000 rate 1500Kbit\n"
        carrot.qdisc_texts = ["", canonical]  # pre-read clean, post-apply canonical form
        await impair_link(lab, "edge", ImpairmentParams(rate="1.5mbit"), from_host="carrot_seed")
        assert "tc qdisc replace dev eth1.100 root netem rate 1.5mbit" in carrot.sudo_commands


class TestInpath:
    @pytest.mark.asyncio
    async def test_placements_land_on_middlebox(self) -> None:
        lab, carrot, tomato, pepper = _bed(link=INPATH)
        # one host, two netdevs, one shared read queue: pre-read/verify per direction
        pepper.qdisc_texts = ["", DELAY_50_TEXT, "", DELAY_50_TEXT]
        report = await impair_link(lab, "dataplane", ImpairmentParams(delay_ms=50.0))
        assert {a.placement.host_id for a in report.applied} == {"pepper_seed"}
        assert {a.placement.netdev for a in report.applied} == {"eth1.100", "eth1.200"}
        assert not carrot.sudo_commands
        assert not tomato.sudo_commands


class TestRefusalsAndSafety:
    @pytest.mark.asyncio
    async def test_local_endpoint_refused_before_any_command(self) -> None:
        from otto.host.builtin_hosts import BUILTIN_LOCAL_HOST_ID

        local_link = Link(
            a=LinkEndpoint(host=BUILTIN_LOCAL_HOST_ID, interface="eth0", ip="10.0.0.1"),
            b=LINK.b,
            name="to-local",
        )
        lab, carrot, tomato, _ = _bed(link=local_link)
        with pytest.raises(ValueError, match="local host as an endpoint"):
            await impair_link(lab, "to-local", ImpairmentParams(delay_ms=1.0))
        assert not carrot.commands
        assert not tomato.commands

    @pytest.mark.asyncio
    async def test_local_impair_middlebox_refused_before_any_command(self) -> None:
        from otto.host.builtin_hosts import BUILTIN_LOCAL_HOST_ID

        mid_link = Link(a=LINK.a, b=LINK.b, name="local-mid", impair=BUILTIN_LOCAL_HOST_ID)
        lab, carrot, tomato, _ = _bed(link=mid_link)
        # Register a RESOLVABLE local host: without the refusal, impair would
        # actually resolve and run commands on otto's own machine.
        local = FakeHost(id=BUILTIN_LOCAL_HOST_ID, ip="127.0.0.1", addr_text=PEPPER_ADDR)
        lab.hosts[BUILTIN_LOCAL_HOST_ID] = local
        with pytest.raises(ValueError, match="local host as its in-path middlebox"):
            await impair_link(lab, "local-mid", ImpairmentParams(delay_ms=1.0))
        assert not local.commands
        assert not carrot.commands
        assert not tomato.commands

    @pytest.mark.asyncio
    async def test_mgmt_interface_placement_refused(self) -> None:
        mgmt_link = Link(
            a=LinkEndpoint(host="carrot_seed", interface="eth1", ip="10.10.200.11"),
            b=LinkEndpoint(host="tomato_seed", interface="eth1", ip="10.10.200.12"),
            name="mgmt-edge",
        )
        lab, carrot, _, _ = _bed(link=mgmt_link)
        with pytest.raises(ValueError, match="management interface"):
            await impair_link(lab, "mgmt-edge", ImpairmentParams(delay_ms=1.0))
        assert not carrot.sudo_commands

    @pytest.mark.asyncio
    async def test_hop_transit_placement_refused_before_mutation(self) -> None:
        # A fourth host reaches otto only THROUGH pepper (its hop), with a mgmt
        # ip inside pepper's eth1.200 subnet: impairing dataplane (in-path on
        # pepper) would sever otto->beet. Refuse before any mutation.
        lab, _carrot, _tomato, pepper = _bed(link=INPATH)
        beet = FakeHost(id="beet_seed", ip="10.10.202.77", addr_text="")
        beet.hop = "pepper_seed"  # direct hop through the middlebox
        lab.hosts["beet_seed"] = beet
        with pytest.raises(ValueError, match="hop transit"):
            await impair_link(lab, "dataplane", ImpairmentParams(delay_ms=1.0))
        assert not pepper.sudo_commands

    @pytest.mark.asyncio
    async def test_hop_transit_transitive_chain_refused(self) -> None:
        # beet -> onion -> pepper: beet still transits pepper (transitive walk).
        lab, _carrot, _tomato, pepper = _bed(link=INPATH)
        onion = FakeHost(id="onion_seed", ip="10.10.99.1", addr_text="")
        onion.hop = "pepper_seed"
        beet = FakeHost(id="beet_seed", ip="10.10.202.77", addr_text="")
        beet.hop = "onion_seed"
        lab.hosts["onion_seed"] = onion
        lab.hosts["beet_seed"] = beet
        with pytest.raises(ValueError, match="beet_seed"):
            await impair_link(lab, "dataplane", ImpairmentParams(delay_ms=1.0))
        assert not pepper.sudo_commands

    @pytest.mark.asyncio
    async def test_rollback_restores_prior_state_on_partial_failure(self) -> None:
        lab, carrot, tomato, _ = _bed()
        carrot.qdisc_texts = [
            "qdisc netem 8001: root refcnt 2 limit 1000 delay 20ms\n",  # prior state
            "qdisc netem 8001: root refcnt 2 limit 1000 delay 50ms\n",  # verify ok
        ]
        tomato.fail_on = "tc qdisc replace"  # second placement fails
        with pytest.raises(RuntimeError):
            await impair_link(lab, "edge", ImpairmentParams(delay_ms=50.0))
        # carrot restored to its PRIOR params, not cleared
        assert carrot.sudo_commands[-1] == "tc qdisc replace dev eth1.100 root netem delay 20ms"

    @pytest.mark.asyncio
    async def test_verify_mismatch_rolls_back_own_placement(self) -> None:
        # Single placement: apply succeeds, verify mismatches. The just-mutated
        # placement must itself be restored to prior BEFORE the error propagates
        # (its own rollback entry, not only earlier placements').
        lab, carrot, _, _ = _bed()
        carrot.qdisc_texts = [
            "qdisc netem 8001: root refcnt 2 limit 1000 delay 20ms\n",  # prior state
            "",  # post-apply verify: nothing there -> mismatch
        ]
        with pytest.raises(RuntimeError, match="post-apply verify failed"):
            await impair_link(lab, "edge", ImpairmentParams(delay_ms=50.0), from_host="carrot_seed")
        # restored to prior (delay 20ms), not left half-applied at delay 50ms
        assert carrot.sudo_commands[-1] == "tc qdisc replace dev eth1.100 root netem delay 20ms"

    @pytest.mark.asyncio
    async def test_unreachable_host_fails_loud_with_host_name(self) -> None:
        lab, carrot, _, _ = _bed()

        async def _boom(cmd: str, **_: object) -> CommandResult:
            raise ConnectionError("boom")

        carrot.exec = _boom  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="carrot_seed"):
            await impair_link(lab, "edge", ImpairmentParams(delay_ms=1.0))


class TestExpireTimers:
    @pytest.mark.asyncio
    async def test_expire_launches_sentinel_tagged_timer(self) -> None:
        lab, carrot, _, _ = _bed()
        carrot.qdisc_texts = ["", DELAY_50_TEXT]  # pre-read, then post-apply verify
        await impair_link(
            lab, "edge", ImpairmentParams(delay_ms=50.0), from_host="carrot_seed", expire=30
        )
        # skip the qdisc-mutation command; find the timer launch
        launch = next(c for c in carrot.sudo_commands if "otto-impair:" in c)
        assert "otto-impair:v1:" in launch
        assert "eth1.100" in launch
        assert "sleep 30 && tc qdisc del dev eth1.100 root" in launch
        # Whole conditional wrapped in an outer `bash -c` so the launch string
        # is one opaque word, safe for `_root_run`'s sudo-prefixing to compose
        # with (see otto.host.detached.launch_command's docstring).
        assert launch.startswith("bash -c 'if command -v systemd-run")

    @pytest.mark.asyncio
    async def test_impair_cancels_stale_timers_first(self) -> None:
        from otto.link.sentinel import encode_impair_sentinel

        lab, carrot, _, _ = _bed()
        token = encode_impair_sentinel(LINK.id, "eth1.100")
        carrot.ps_text = f"  4242 05:00 {token} -c sleep 600 && tc qdisc del dev eth1.100 root\n"
        carrot.qdisc_texts = ["", DELAY_50_TEXT]  # pre-read, then post-apply verify
        await impair_link(lab, "edge", ImpairmentParams(delay_ms=50.0), from_host="carrot_seed")
        assert "kill 4242" in carrot.sudo_commands


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
        lab, carrot, _, _ = _bed()
        carrot.impairer = "recorder"  # the host-level pin, post-resolution

        def _fake_result(cmd: str) -> CommandResult:
            if cmd.startswith("FAKE-READ"):
                texts = carrot.qdisc_texts
                text = texts.pop(0) if len(texts) > 1 else texts[0]
                return CommandResult(status=Status.Success, value=text, command=cmd)
            return FakeHost._result(carrot, cmd)

        carrot._result = _fake_result  # type: ignore[method-assign]
        carrot.qdisc_texts = ["", "APPLIED"]
        await impair_link(lab, "edge", ImpairmentParams(delay_ms=50.0), from_host="carrot_seed")
        assert "FAKE-APPLY eth1.100 delay 50ms" in carrot.sudo_commands
        assert not any(c.startswith("tc ") for c in carrot.sudo_commands)  # netem never ran

    @pytest.mark.asyncio
    async def test_host_without_impairer_support_fails_loud(self) -> None:
        lab, carrot, _, _ = _bed()
        carrot.impairer = ""  # e.g. an embedded host: no impairer attribute/value
        with pytest.raises(ValueError, match="no impairer support"):
            await impair_link(lab, "edge", ImpairmentParams(delay_ms=1.0), from_host="carrot_seed")
