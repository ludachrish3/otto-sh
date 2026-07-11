"""``remove_tunnel`` / ``remove_all_tunnels``: kill + post-kill verify (spec §10)."""

import asyncio
from dataclasses import dataclass, field
from typing import Any

from otto.result import CommandResult
from otto.tunnel.manage import RemovedReport, remove_all_tunnels, remove_tunnel
from otto.tunnel.model import Direction, Role, Tunnel, TunnelHop
from otto.tunnel.sentinel import encode_sentinel
from otto.tunnel.socat import DISCOVERY_PS_COMMAND
from otto.utils import Status

_LO = 49152


@dataclass
class FakeHost:
    """Scripted host for the remove path: discovery scan, then kill, then a
    post-kill verify scan — both discovery and verify go over
    ``DISCOVERY_PS_COMMAND``.

    ``ps_texts`` is a small queue: while more than one entry remains, each
    ``DISCOVERY_PS_COMMAND`` call pops the next one; once a single entry is
    left, it repeats forever. First pop = the discovery scan; the (repeating)
    remainder = the post-kill verify scan(s). This lets a test express
    "processes present, then gone after kill" as ``[full, ""]``, or "still
    there" as ``[full, one_leftover_line]``.
    """

    id: str
    has_bash: bool = True
    scan_fail: bool = False
    """Raise instead of answering a discovery/verify scan (host unreachable)."""
    kill_ok: bool = True
    """If False, the ``kill`` command runs but reports a Failed result."""
    kill_raises: bool = False
    """If True, the ``kill`` command raises (host unreachable mid-reap)."""
    ps_texts: list = field(default_factory=lambda: [""])
    commands: list = field(default_factory=list)

    async def exec(self, cmd: str, timeout: float | None = None, **_: object) -> CommandResult:
        self.commands.append(cmd)
        if cmd == DISCOVERY_PS_COMMAND:
            if self.scan_fail:
                raise ConnectionError("host is unreachable")
            text = self.ps_texts.pop(0) if len(self.ps_texts) > 1 else self.ps_texts[0]
            return CommandResult(status=Status.Success, value=text, command=cmd)
        if cmd.startswith("kill "):
            if self.kill_raises:
                raise ConnectionError("kill failed")
            if not self.kill_ok:
                return CommandResult(status=Status.Failed, value="boom", command=cmd, retcode=1)
            return CommandResult(status=Status.Success, value="", command=cmd)
        raise AssertionError(f"unexpected command: {cmd!r}")


@dataclass
class FakeLab:
    hosts: dict


def _lab(**hosts: Any) -> FakeLab:
    return FakeLab(hosts=dict(hosts))


def _ps_line(
    tunnel: Tunnel, direction: Direction, role: Role, hop_index: int, carrier: int, pid: int
) -> str:
    token = encode_sentinel(
        tunnel, direction=direction, role=role, hop_index=hop_index, carrier_port=carrier
    )
    return f"  {pid} 00:10 {token} socat TCP4-LISTEN:{carrier},fork ..."


def _full_ps(
    tunnel: Tunnel,
    host_id: str,
    carrier_fwd: int,
    carrier_rev: int,
    pid_start: int = 100,
) -> tuple[str, list]:
    """Every expected process for *host_id*; returns ``(ps_text, pids)``."""
    hop_index = next(i for i, h in enumerate(tunnel.path) if h.host == host_id)
    lines = []
    pids = []
    pid = pid_start
    for key in tunnel.expected_processes():
        host, direction, role = key
        if host != host_id:
            continue
        carrier = carrier_fwd if direction is Direction.FWD else carrier_rev
        lines.append(_ps_line(tunnel, direction, role, hop_index, carrier, pid))
        pids.append(pid)
        pid += 1
    return "\n".join(lines), pids


def _three_hop_tunnel() -> Tunnel:
    return Tunnel(
        protocol="tcp", service_port=8080, path=(TunnelHop("a"), TunnelHop("b"), TunnelHop("c"))
    )


class TestRemoveTunnel:
    def test_remove_kills_all_hops_and_verifies_gone(self) -> None:
        tunnel = _three_hop_tunnel()
        carrier_fwd, carrier_rev = _LO, _LO + 1
        pids_by_host = {}
        hosts = {}
        for host_id, pid_start in (("a", 100), ("b", 200), ("c", 300)):
            text, pids = _full_ps(tunnel, host_id, carrier_fwd, carrier_rev, pid_start)
            pids_by_host[host_id] = pids
            hosts[host_id] = FakeHost(host_id, ps_texts=[text, ""])
        lab = _lab(**hosts)

        report = asyncio.run(remove_tunnel(lab, tunnel.id))

        assert isinstance(report, RemovedReport)
        assert report.removed_ids == [tunnel.id]
        assert report.killed == {h: sorted(pids_by_host[h]) for h in ("a", "b", "c")}
        assert report.survivors == []
        assert report.unreachable == []
        for host in hosts.values():
            assert any(cmd.startswith("kill ") for cmd in host.commands)

    def test_remove_unknown_id_reports_empty(self) -> None:
        tunnel = _three_hop_tunnel()
        text_a, _pids_a = _full_ps(tunnel, "a", _LO, _LO + 1)
        text_b, _pids_b = _full_ps(tunnel, "b", _LO, _LO + 1)
        text_c, _pids_c = _full_ps(tunnel, "c", _LO, _LO + 1)
        hosts = {
            "a": FakeHost("a", ps_texts=[text_a]),
            "b": FakeHost("b", ps_texts=[text_b]),
            "c": FakeHost("c", ps_texts=[text_c]),
        }
        lab = _lab(**hosts)

        report = asyncio.run(remove_tunnel(lab, "tun-doesnotexist-1"))

        assert report == RemovedReport(removed_ids=[], killed={}, unreachable=[], survivors=[])
        for host in hosts.values():
            assert not any(cmd.startswith("kill ") for cmd in host.commands)

    def test_survivor_reported(self) -> None:
        tunnel = _three_hop_tunnel()
        carrier_fwd, carrier_rev = _LO, _LO + 1
        text_a, _pids_a = _full_ps(tunnel, "a", carrier_fwd, carrier_rev, 100)
        text_b, _pids_b = _full_ps(tunnel, "b", carrier_fwd, carrier_rev, 200)
        text_c, pids_c = _full_ps(tunnel, "c", carrier_fwd, carrier_rev, 300)
        survivor_pid = pids_c[0]
        hop_index_c = next(i for i, h in enumerate(tunnel.path) if h.host == "c")
        survivor_key = next(k for k in tunnel.expected_processes() if k[0] == "c")
        survivor_line = _ps_line(
            tunnel,
            survivor_key[1],
            survivor_key[2],
            hop_index_c,
            carrier_fwd if survivor_key[1] is Direction.FWD else carrier_rev,
            survivor_pid,
        )
        hosts = {
            "a": FakeHost("a", ps_texts=[text_a, ""]),
            "b": FakeHost("b", ps_texts=[text_b, ""]),
            "c": FakeHost("c", ps_texts=[text_c, survivor_line]),
        }
        lab = _lab(**hosts)

        report = asyncio.run(remove_tunnel(lab, tunnel.id))

        assert report.removed_ids == [tunnel.id]
        assert report.survivors == [("c", survivor_pid)]
        assert "c" in report.killed  # the kill itself was ack'd fine
        assert report.unreachable == []

    def test_kill_failure_marks_unreachable(self) -> None:
        tunnel = Tunnel(protocol="tcp", service_port=8080, path=(TunnelHop("a"), TunnelHop("b")))
        carrier_fwd, carrier_rev = _LO, _LO + 1
        text_a, _pids_a = _full_ps(tunnel, "a", carrier_fwd, carrier_rev, 100)
        text_b, pids_b = _full_ps(tunnel, "b", carrier_fwd, carrier_rev, 200)
        a = FakeHost("a", ps_texts=[text_a, text_a], kill_ok=False)
        b = FakeHost("b", ps_texts=[text_b, ""])
        lab = _lab(a=a, b=b)

        report = asyncio.run(remove_tunnel(lab, tunnel.id))

        assert report.removed_ids == [tunnel.id]
        assert "a" not in report.killed
        assert report.killed == {"b": sorted(pids_b)}
        assert report.unreachable == ["a"]
        assert report.survivors == []
        assert any(cmd.startswith("kill ") for cmd in a.commands)

    def test_discovery_unreachable_host_propagates(self) -> None:
        tunnel = Tunnel(protocol="tcp", service_port=8080, path=(TunnelHop("a"), TunnelHop("b")))
        carrier_fwd, carrier_rev = _LO, _LO + 1
        text_a, _pids_a = _full_ps(tunnel, "a", carrier_fwd, carrier_rev, 100)
        text_b, pids_b = _full_ps(tunnel, "b", carrier_fwd, carrier_rev, 200)
        a = FakeHost("a", ps_texts=[text_a, ""])
        b = FakeHost("b", ps_texts=[text_b, ""])
        ghost = FakeHost("ghost", scan_fail=True)
        lab = _lab(a=a, b=b, ghost=ghost)

        report = asyncio.run(remove_tunnel(lab, tunnel.id))

        assert report.removed_ids == [tunnel.id]
        assert report.killed == {"a": sorted(_pids_a), "b": sorted(pids_b)}
        assert report.unreachable == ["ghost"]
        assert report.survivors == []


class TestRemoveAllTunnels:
    def test_remove_all_reaps_multiple_tunnels(self) -> None:
        tunnel1 = Tunnel(protocol="tcp", service_port=8080, path=(TunnelHop("a"), TunnelHop("b")))
        tunnel2 = Tunnel(protocol="tcp", service_port=9090, path=(TunnelHop("c"), TunnelHop("d")))
        carrier_fwd, carrier_rev = _LO, _LO + 1
        text_a, pids_a = _full_ps(tunnel1, "a", carrier_fwd, carrier_rev, 100)
        text_b, pids_b = _full_ps(tunnel1, "b", carrier_fwd, carrier_rev, 200)
        text_c, pids_c = _full_ps(tunnel2, "c", carrier_fwd, carrier_rev, 300)
        text_d, pids_d = _full_ps(tunnel2, "d", carrier_fwd, carrier_rev, 400)
        hosts = {
            "a": FakeHost("a", ps_texts=[text_a, ""]),
            "b": FakeHost("b", ps_texts=[text_b, ""]),
            "c": FakeHost("c", ps_texts=[text_c, ""]),
            "d": FakeHost("d", ps_texts=[text_d, ""]),
        }
        lab = _lab(**hosts)

        report = asyncio.run(remove_all_tunnels(lab))

        assert report.removed_ids == sorted([tunnel1.id, tunnel2.id])
        assert report.killed == {
            "a": sorted(pids_a),
            "b": sorted(pids_b),
            "c": sorted(pids_c),
            "d": sorted(pids_d),
        }
        assert report.survivors == []
        assert report.unreachable == []
