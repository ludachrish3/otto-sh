"""impair_link orchestration against scripted fake hosts (no bed).

The fake dispatches on command text the way the tunnel manage fakes do, and
returns REAL CommandResult/Results objects (global constraint)."""

from dataclasses import dataclass, field

import pytest

from otto.link.impairer import LinkImpairer, register_impairer
from otto.link.manage import find_link, impair_link
from otto.link.model import Link, LinkEndpoint
from otto.link.params import ImpairmentParams, Selector
from otto.link.placement import FlowDirection
from otto.link.sentinel import IMPAIR_PS_COMMAND, encode_impair_sentinel_v2
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
    filter_texts: list[str] = field(default_factory=lambda: [""])
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
        # with (see otto.host.daemon.launch_command's docstring).
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
        lab, carrot, _, _ = _bed()
        carrot.qdisc_texts = [QDISC_SCOPED_ONE]
        carrot.filter_texts = [FILTER_SCOPED_ONE]
        with pytest.raises(ValueError, match="has port-scoped impairments — repair them first"):
            await impair_link(lab, "edge", ImpairmentParams(delay_ms=50.0), from_host="carrot_seed")
        assert not carrot.sudo_commands  # refused BEFORE any mutation

    @pytest.mark.asyncio
    async def test_bare_impair_against_foreign_root_refuses(self) -> None:
        lab, carrot, _, _ = _bed()
        carrot.qdisc_texts = ["qdisc htb 8001: root refcnt 2 r2q 10\n"]
        with pytest.raises(RuntimeError, match="foreign qdisc otto did not create"):
            await impair_link(lab, "edge", ImpairmentParams(delay_ms=50.0), from_host="carrot_seed")
        assert not carrot.sudo_commands

    @pytest.mark.asyncio
    async def test_exclusivity_error_mid_link_rolls_back_first_placement(self) -> None:
        # carrot clean (applies fine), tomato scoped -> error; carrot restored to clean.
        lab, carrot, tomato, _ = _bed()
        carrot.qdisc_texts = ["", DELAY_50_TEXT]
        tomato.qdisc_texts = [QDISC_SCOPED_ONE]
        tomato.filter_texts = [FILTER_SCOPED_ONE]
        with pytest.raises(ValueError, match="port-scoped impairments"):
            await impair_link(lab, "edge", ImpairmentParams(delay_ms=50.0))
        assert carrot.sudo_commands[-1] == "tc qdisc del dev eth1.100 root"
        assert not tomato.sudo_commands


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
        lab, carrot, _, _ = _bed()
        carrot.qdisc_texts = ["", QDISC_SCOPED_ONE]
        carrot.filter_texts = ["", FILTER_SCOPED_ONE]
        report = await impair_link(
            lab,
            "edge",
            ImpairmentParams(delay_ms=200.0),
            from_host="carrot_seed",
            selector=Selector(5201, "tcp"),
        )
        assert report.applied[0].selector == Selector(5201, "tcp")
        assert carrot.sudo_commands == [
            "tc qdisc replace dev eth1.100 root handle 1: prio bands 11 "
            "priomap 1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1",
            "tc qdisc replace dev eth1.100 parent 1:4 handle 40: netem delay 200ms",
            "tc filter add dev eth1.100 parent 1: pref 40 protocol ip u32 "
            "match ip protocol 6 0xff match ip dport 5201 0xffff flowid 1:4",
            "tc filter add dev eth1.100 parent 1: pref 41 protocol ip u32 "
            "match ip protocol 6 0xff match ip sport 5201 0xffff flowid 1:4",
        ]

    @pytest.mark.asyncio
    async def test_second_selector_takes_next_band_no_root_reissue(self) -> None:
        lab, carrot, _, _ = _bed()
        carrot.qdisc_texts = [QDISC_SCOPED_ONE, QDISC_SCOPED_TWO]
        carrot.filter_texts = [FILTER_SCOPED_ONE, FILTER_SCOPED_TWO]
        await impair_link(
            lab,
            "edge",
            ImpairmentParams(loss_pct=5.0),
            from_host="carrot_seed",
            selector=Selector(53, "udp"),
        )
        assert not any("prio bands" in c for c in carrot.sudo_commands)
        assert "tc qdisc replace dev eth1.100 parent 1:5 handle 50: netem loss 5%" in (
            carrot.sudo_commands
        )

    @pytest.mark.asyncio
    async def test_reimpair_merges_keeps_band_no_new_filters(self) -> None:
        merged_qdisc = QDISC_SCOPED_ONE.replace("delay 200ms", "delay 200ms loss 2%")
        lab, carrot, _, _ = _bed()
        carrot.qdisc_texts = [QDISC_SCOPED_ONE, merged_qdisc]
        carrot.filter_texts = [FILTER_SCOPED_ONE]
        await impair_link(
            lab,
            "edge",
            ImpairmentParams(loss_pct=2.0),
            from_host="carrot_seed",
            selector=Selector(5201, "tcp"),
        )
        assert carrot.sudo_commands == [
            "tc qdisc replace dev eth1.100 parent 1:4 handle 40: netem delay 200ms loss 2%"
        ]

    @pytest.mark.asyncio
    async def test_selector_merged_to_empty_clears_that_selector(self) -> None:
        # zeroing the only param of the only selector -> full clear back to pristine
        lab, carrot, _, _ = _bed()
        carrot.qdisc_texts = [QDISC_SCOPED_ONE, ""]
        carrot.filter_texts = [FILTER_SCOPED_ONE, ""]
        await impair_link(
            lab,
            "edge",
            ImpairmentParams(delay_ms=0.0),
            from_host="carrot_seed",
            selector=Selector(5201, "tcp"),
        )
        assert carrot.sudo_commands == ["tc qdisc del dev eth1.100 root"]

    @pytest.mark.asyncio
    async def test_scoped_against_whole_link_is_loud(self) -> None:
        lab, carrot, _, _ = _bed()
        carrot.qdisc_texts = ["qdisc netem 8001: root refcnt 2 limit 1000 delay 20ms\n"]
        with pytest.raises(ValueError, match="has a whole-link impairment — repair it first"):
            await impair_link(
                lab,
                "edge",
                ImpairmentParams(delay_ms=1.0),
                from_host="carrot_seed",
                selector=Selector(5201),
            )
        assert not carrot.sudo_commands

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
        lab, carrot, _, _ = _bed()
        carrot.qdisc_texts = [
            "qdisc prio 1: root refcnt 2 bands 11 priomap 1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1\n" + bands
        ]
        carrot.filter_texts = [filters]
        with pytest.raises(ValueError, match="8 port-scoped impairments"):
            await impair_link(
                lab,
                "edge",
                ImpairmentParams(delay_ms=1.0),
                from_host="carrot_seed",
                selector=Selector(9999, "tcp"),
            )
        # The cap error fires inside the mutation attempt, AFTER the rollback
        # entry is registered (same posture as a validate() failure today), so
        # a best-effort restore of the untouched prior mapping may run — but
        # nothing for the rejected selector may ever have been applied.
        assert not any("9999" in c for c in carrot.sudo_commands)

    @pytest.mark.asyncio
    async def test_capability_error_names_the_impairer(self) -> None:
        lab, carrot, _, _ = _bed()
        register_impairer("plainrec", _make_plain_recorder(), overwrite=True)
        carrot.impairer = "plainrec"
        with pytest.raises(ValueError, match="'plainrec' does not support port-scoped"):
            await impair_link(
                lab,
                "edge",
                ImpairmentParams(delay_ms=1.0),
                from_host="carrot_seed",
                selector=Selector(80),
            )

    @pytest.mark.asyncio
    async def test_scoped_verify_mismatch_restores_full_prior_mapping(self) -> None:
        # prior: one selector; apply second; verify re-read shows nothing -> rollback
        # must rebuild the COMPLETE prior scoped mapping (root + band + filters).
        lab, carrot, _, _ = _bed()
        carrot.qdisc_texts = [QDISC_SCOPED_ONE, ""]
        carrot.filter_texts = [FILTER_SCOPED_ONE, ""]
        with pytest.raises(RuntimeError, match="post-apply verify failed"):
            await impair_link(
                lab,
                "edge",
                ImpairmentParams(loss_pct=5.0),
                from_host="carrot_seed",
                selector=Selector(53, "udp"),
            )
        restore = carrot.sudo_commands[-4:]
        assert restore == [
            "tc qdisc replace dev eth1.100 root handle 1: prio bands 11 "
            "priomap 1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1",
            "tc qdisc replace dev eth1.100 parent 1:4 handle 40: netem delay 200ms",
            "tc filter add dev eth1.100 parent 1: pref 40 protocol ip u32 "
            "match ip protocol 6 0xff match ip dport 5201 0xffff flowid 1:4",
            "tc filter add dev eth1.100 parent 1: pref 41 protocol ip u32 "
            "match ip protocol 6 0xff match ip sport 5201 0xffff flowid 1:4",
        ]
        # and the root was cleared before the rebuild
        assert "tc qdisc del dev eth1.100 root" in carrot.sudo_commands


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
        lab, carrot, _, _ = _bed()
        carrot.qdisc_texts = ["", QDISC_SCOPED_ONE]
        carrot.filter_texts = ["", FILTER_SCOPED_ONE]
        await impair_link(
            lab,
            "edge",
            ImpairmentParams(delay_ms=200.0),
            from_host="carrot_seed",
            selector=Selector(5201, "tcp"),
            expire=30,
        )
        launch = next(c for c in carrot.sudo_commands if "otto-impair:" in c)
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
        lab, carrot, _, _ = _bed()
        carrot.ps_text = (
            f"  4242 05:00 {v2_mine} -c sleep 600\n  4243 05:00 {v2_other} -c sleep 600\n"
        )
        merged_qdisc = QDISC_SCOPED_ONE.replace("delay 200ms", "delay 200ms loss 2%")
        carrot.qdisc_texts = [QDISC_SCOPED_ONE, merged_qdisc]
        carrot.filter_texts = [FILTER_SCOPED_ONE]
        await impair_link(
            lab,
            "edge",
            ImpairmentParams(loss_pct=2.0),
            from_host="carrot_seed",
            selector=Selector(5201, "tcp"),
        )
        assert "kill 4242" in carrot.sudo_commands
        assert not any("4243" in c for c in carrot.sudo_commands)

    @pytest.mark.asyncio
    async def test_whole_link_impair_does_not_cancel_v2_timers(self) -> None:
        # a v2 timer for another link's netdev-sharing selector must survive a
        # bare impair (which only owns v1 whole-link timers)
        v2 = encode_impair_sentinel_v2(LINK.id, "eth1.100", Selector(5201, "tcp"))
        lab, carrot, _, _ = _bed()
        carrot.ps_text = f"  4242 05:00 {v2} -c sleep 600\n"
        carrot.qdisc_texts = ["", DELAY_50_TEXT]
        carrot.filter_texts = [""]
        await impair_link(lab, "edge", ImpairmentParams(delay_ms=50.0), from_host="carrot_seed")
        assert not any(c.startswith("kill") for c in carrot.sudo_commands)

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
        lab, carrot, _, _ = _bed()
        carrot.ps_text = f"  4243 05:00 {v2_sibling} -c sleep 600\n"
        carrot.qdisc_texts = [QDISC_SCOPED_TWO, ""]  # prior: both selectors; verify: nothing there
        carrot.filter_texts = [FILTER_SCOPED_TWO, ""]
        with pytest.raises(RuntimeError, match="post-apply verify failed"):
            await impair_link(
                lab,
                "edge",
                ImpairmentParams(loss_pct=2.0),
                from_host="carrot_seed",
                selector=Selector(5201, "tcp"),
            )
        # rollback ran: the full two-selector mapping was rebuilt
        assert "tc qdisc del dev eth1.100 root" in carrot.sudo_commands
        assert (
            "tc qdisc replace dev eth1.100 parent 1:4 handle 40: netem delay 200ms"
            in carrot.sudo_commands
        )
        assert (
            "tc qdisc replace dev eth1.100 parent 1:5 handle 50: netem loss 5%"
            in carrot.sudo_commands
        )
        # the sibling's OWN v2 timer (53/udp, pid 4243) survived: rollback was
        # scoped to THIS run's selector (5201/tcp), not "everything"
        assert not any("4243" in c for c in carrot.sudo_commands)
