"""Discovery parsing, grouping, and status semantics (spec #2b §9)."""

import asyncio
from dataclasses import dataclass, field

from otto.result import CommandResult
from otto.tunnel.discovery import (
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
