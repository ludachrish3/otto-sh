"""Discovery parsing, grouping, and status semantics (spec #2b §9)."""

import asyncio
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock

from otto.logger.mode import LogMode
from otto.result import CommandResult
from otto.tunnel.discovery import (
    DiscoveredTunnel,
    discover_tunnels,
    parse_process_discovery,
)
from otto.tunnel.model import Direction, Role, Tunnel, TunnelHop
from otto.tunnel.sentinel import encode_sentinel
from otto.utils import Status

TUNNEL = Tunnel(
    protocol="udp", service_port=5000, path=(TunnelHop("a"), TunnelHop("c"), TunnelHop("b"))
)


def _ps_line(
    pid: int,
    etime: str,
    direction: Direction,
    role: Role,
    hop: int,
    carrier: int = 50001,
    tunnel: Tunnel = TUNNEL,
) -> str:
    token = encode_sentinel(
        tunnel, direction=direction, role=role, hop_index=hop, carrier_port=carrier
    )
    return f"  {pid} {etime} {token} UDP4-LISTEN:5000,fork ..."


@dataclass
class FakeHost:
    id: str
    ps_text: str = ""
    has_bash: bool = True
    fail: bool = False
    slow: bool = False
    commands: list[str] = field(default_factory=list)

    async def exec(self, cmd: str, timeout: float | None = None, **_: object) -> CommandResult:
        self.commands.append(cmd)
        if self.slow:
            await asyncio.sleep(3600)
        if self.fail:
            raise ConnectionError("boom")
        return CommandResult(status=Status.Success, value=self.ps_text, command=cmd)


@dataclass
class FakeLab:
    hosts: dict


class TestParsing:
    def test_parse_skips_non_otto_and_malformed(self) -> None:
        text = "\n".join(
            [
                "  10 05:00 socat UDP4-LISTEN:9999 STDIO",  # stranger's socat
                "  11 05:00 otto-tunnel:v1:mangled",  # malformed token
                _ps_line(12, "05:00", Direction.FWD, Role.INGRESS, 0),
                "not a ps line at all",
            ]
        )
        out = parse_process_discovery(text)
        assert [o.pid for o in out] == [12]
        assert out[0].age_seconds == 300
        assert out[0].parsed.tunnel.id == TUNNEL.id


def _full_ps_for(host: str) -> str:
    """Every process this host should run for TUNNEL (2 per host, spec §6.1)."""
    lines = {
        "a": [
            _ps_line(1, "10:00", Direction.FWD, Role.INGRESS, 0),
            _ps_line(2, "10:00", Direction.REV, Role.EGRESS, 0, carrier=50002),
        ],
        "c": [
            _ps_line(3, "09:59", Direction.FWD, Role.RELAY, 1),
            _ps_line(4, "09:59", Direction.REV, Role.RELAY, 1, carrier=50002),
        ],
        "b": [
            _ps_line(5, "09:58", Direction.FWD, Role.EGRESS, 2),
            _ps_line(6, "09:58", Direction.REV, Role.INGRESS, 2, carrier=50002),
        ],
    }
    return "\n".join(lines[host])


class TestDiscoverTunnels:
    def test_healthy_tunnel_is_ok(self) -> None:
        lab = FakeLab(hosts={h: FakeHost(h, _full_ps_for(h)) for h in ("a", "c", "b")})
        result = asyncio.run(discover_tunnels(lab))
        assert result.unreachable == []
        (d,) = result.tunnels
        assert d.tunnel == TUNNEL
        assert d.missing == set()
        assert d.status == "ok"
        assert d.age_seconds == 600  # oldest observed

    def test_dead_hop_is_degraded(self) -> None:
        hosts = {h: FakeHost(h, _full_ps_for(h)) for h in ("a", "c", "b")}
        hosts["c"].ps_text = ""  # both relay processes died; host still reachable
        result = asyncio.run(discover_tunnels(FakeLab(hosts)))
        (d,) = result.tunnels
        assert d.missing == {("c", Direction.FWD, Role.RELAY), ("c", Direction.REV, Role.RELAY)}
        assert d.status == "degraded (4/6)"

    def test_unreachable_chain_host_is_uncertain_not_degraded(self) -> None:
        hosts = {h: FakeHost(h, _full_ps_for(h)) for h in ("a", "c", "b")}
        hosts["c"].fail = True
        result = asyncio.run(discover_tunnels(FakeLab(hosts)))
        assert result.unreachable == ["c"]
        (d,) = result.tunnels
        assert d.uncertain
        assert d.missing == set()  # absence on an unscanned host is unknown
        assert d.status == "ok?"

    def test_non_bash_hosts_not_scanned(self) -> None:
        zephyr = FakeHost("z", has_bash=False)
        lab = FakeLab(hosts={"a": FakeHost("a", _full_ps_for("a")), "z": zephyr})
        asyncio.run(discover_tunnels(lab))
        assert zephyr.commands == []

    def test_two_tunnels_group_separately(self) -> None:
        other = Tunnel(protocol="tcp", service_port=80, path=(TunnelHop("a"), TunnelHop("b")))
        extra = "\n".join(
            [
                _full_ps_for("a"),
                _ps_line(7, "01:00", Direction.FWD, Role.INGRESS, 0, carrier=50003, tunnel=other),
            ]
        )
        hosts = {h: FakeHost(h, _full_ps_for(h)) for h in ("c", "b")}
        hosts["a"] = FakeHost("a", extra)
        result = asyncio.run(discover_tunnels(FakeLab(hosts)))
        assert {d.tunnel.id for d in result.tunnels} == {TUNNEL.id, other.id}


def _discovered_tunnel(
    missing: frozenset = frozenset(), uncertain: bool = False
) -> DiscoveredTunnel:
    expected = TUNNEL.expected_processes()
    return DiscoveredTunnel(
        tunnel=TUNNEL,
        present=expected - missing,
        missing=set(missing),
        age_seconds=120,
        uncertain=uncertain,
    )


class TestHealth:
    """``health`` — the single shared tri-state primitive (spec 2026-07-16 §2).

    ``uncertain`` dominates ``degraded`` dominates ``ok``; this is the ONLY
    place the precedence is decided — ``tunnel_record`` and any future
    consumer must read it, never re-derive from ``missing``/``uncertain``.
    """

    def test_clean_is_ok(self) -> None:
        assert _discovered_tunnel().health == "ok"

    def test_missing_only_is_degraded(self) -> None:
        some = frozenset(list(TUNNEL.expected_processes())[:2])
        assert _discovered_tunnel(missing=some).health == "degraded"

    def test_uncertain_and_missing_is_uncertain(self) -> None:
        some = frozenset(list(TUNNEL.expected_processes())[:2])
        assert _discovered_tunnel(missing=some, uncertain=True).health == "uncertain"

    def test_uncertain_with_nothing_missing_is_uncertain(self) -> None:
        assert _discovered_tunnel(uncertain=True).health == "uncertain"

    def test_status_stays_composite_while_health_collapses_to_one_word(self) -> None:
        """The human string shows degradation AND uncertainty at once; health
        can only ever say one word — that's the whole reason it exists."""
        some = frozenset(list(TUNNEL.expected_processes())[:2])
        d = _discovered_tunnel(missing=some, uncertain=True)
        expected = len(TUNNEL.expected_processes())
        present = len(d.present)
        assert d.status == f"degraded ({present}/{expected})?"
        assert d.health == "uncertain"


def _container_placeholder(ps_out: str, exec_ps_text: str = ""):
    """A real placeholder DockerContainerHost on a mocked parent.

    The parent answers ``docker ps -q`` probes with *ps_out* and any
    ``docker exec`` (the discovery scan) with *exec_ps_text*; every parent
    call is recorded as ``(cmd, log_kwarg)`` for probe-shape assertions.
    """
    from otto.host.docker_host import DockerContainerHost

    parent = MagicMock()
    parent.id = "carrot_seed"
    parent.name = "carrot_seed"
    parent.term = "ssh"
    parent.resources = set()
    calls: list[tuple[str, object]] = []

    async def _exec(cmd: str, timeout: float | None = None, **kw: object) -> CommandResult:
        calls.append((cmd, kw.get("log")))
        if cmd.startswith("docker ps -q"):
            return CommandResult(status=Status.Success, value=ps_out, command=cmd)
        if cmd.startswith("docker exec"):
            return CommandResult(status=Status.Success, value=exec_ps_text, command=cmd)
        return CommandResult(status=Status.Success, value="", command=cmd)

    parent.exec = AsyncMock(side_effect=_exec)
    ctr = DockerContainerHost(
        parent=parent,
        container_id="",
        project="repo1",
        service="api",
        compose_project="otto-repo1-x",
    )
    return ctr, calls


class TestContainerScanning:
    """Issue #139: discovery must never start docker — it is a read-only scan."""

    def test_down_placeholder_contributes_nothing_and_never_composes(self, monkeypatch) -> None:
        compose_up = AsyncMock()
        monkeypatch.setattr("otto.docker.compose.compose_up", compose_up)
        monkeypatch.setattr("otto.config.get_repos", MagicMock(return_value=[]))
        monkeypatch.setattr("otto.config.get_lab", MagicMock())
        ctr, calls = _container_placeholder(ps_out="")
        lab = FakeLab(hosts={"a": FakeHost("a", _full_ps_for("a")), ctr.id: ctr})

        result = asyncio.run(discover_tunnels(lab))

        compose_up.assert_not_awaited()
        # A stopped container definitively has no processes: that is a clean
        # scan result, not an unreachable host.
        assert result.unreachable == []
        # The parent saw at most the read-only probe — never an exec/compose.
        assert all(cmd.startswith("docker ps -q") for cmd, _log in calls)

    def test_running_container_scans_through_docker_exec(self, monkeypatch) -> None:
        compose_up = AsyncMock()
        monkeypatch.setattr("otto.docker.compose.compose_up", compose_up)
        ctr, calls = _container_placeholder(
            ps_out="abc123\n",
            exec_ps_text=_ps_line(9, "02:00", Direction.FWD, Role.EGRESS, 2),
        )
        lab = FakeLab(hosts={"a": FakeHost("a", _full_ps_for("a")), ctr.id: ctr})

        result = asyncio.run(discover_tunnels(lab))

        compose_up.assert_not_awaited()
        assert result.unreachable == []
        # The running container WAS scanned: its observation joined the group.
        (d,) = result.tunnels
        assert (ctr.id, Direction.FWD, Role.EGRESS) in d.present
        # The liveness probe is quiet — `tunnel list` output stays clean.
        probe_logs = [log for cmd, log in calls if cmd.startswith("docker ps -q")]
        assert probe_logs == [LogMode.QUIET]
