"""Chain resolution, container rules, conflicts, and the process plan (spec §6-§8)."""

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from otto.result import CommandResult
from otto.tunnel.discovery import DiscoveredTunnel, TunnelDiscovery
from otto.tunnel.manage import (
    _check_conflicts,
    _process_plan,
    _resolve_chain,
)
from otto.tunnel.model import Direction, Role, Tunnel, TunnelHop
from otto.utils import Status


@dataclass
class FakeUnix:
    id: str
    ip: str = ""
    interfaces: dict = field(default_factory=dict)
    has_bash: bool = True


@dataclass
class FakeLab:
    hosts: dict


def _lab(**hosts: Any) -> FakeLab:
    return FakeLab(hosts=dict(hosts))


class TestResolveChain:
    def test_plain_pair_resolves_ips(self) -> None:
        lab = _lab(
            a=FakeUnix("a", interfaces={"eth1": "10.0.0.1"}),
            b=FakeUnix("b", ip="10.0.0.2"),
        )
        resolved = asyncio.run(_resolve_chain(lab, [("a", "eth1"), ("b", None)]))
        assert [r.ip for r in resolved] == ["10.0.0.1", "10.0.0.2"]
        assert resolved[0].hop == TunnelHop("a", "eth1")
        assert resolved[1].hop == TunnelHop("b", None)

    def test_single_host_chain_rejected(self) -> None:
        lab = _lab(a=FakeUnix("a", ip="10.0.0.1"))
        with pytest.raises(ValueError, match="at least 2"):
            asyncio.run(_resolve_chain(lab, [("a", None)]))

    def test_duplicate_host_rejected(self) -> None:
        lab = _lab(a=FakeUnix("a", ip="10.0.0.1"), c=FakeUnix("c", ip="10.0.0.2"))
        with pytest.raises(ValueError, match="more than once"):
            asyncio.run(_resolve_chain(lab, [("a", None), ("c", None), ("a", None)]))

    def test_unknown_host_and_iface_fail_loud(self) -> None:
        lab = _lab(a=FakeUnix("a", ip="10.0.0.1"), b=FakeUnix("b", ip="10.0.0.2"))
        with pytest.raises(ValueError, match="unknown host"):
            asyncio.run(_resolve_chain(lab, [("a", None), ("ghost", None)]))
        with pytest.raises(ValueError, match="no interface"):
            asyncio.run(_resolve_chain(lab, [("a", "eth9"), ("b", None)]))

    def test_ambiguous_and_addressless_fail_loud(self) -> None:
        multi = FakeUnix("m", interfaces={"eth0": "10.0.0.3", "eth1": "10.0.1.3"})
        bare = FakeUnix("bare")
        lab = _lab(m=multi, bare=bare, a=FakeUnix("a", ip="10.0.0.1"))
        with pytest.raises(ValueError, match="ambiguous interface"):
            asyncio.run(_resolve_chain(lab, [("m", None), ("a", None)]))
        with pytest.raises(ValueError, match="no usable address"):
            asyncio.run(_resolve_chain(lab, [("bare", None), ("a", None)]))

    def test_non_bash_chain_host_rejected(self) -> None:
        """A chain host that can't run ``bash -c 'exec -a…'`` can't host the
        tagged socat, and discovery/remove only scan ``has_bash`` hosts — it
        would leak un-reapable processes and read as permanently degraded."""
        lab = _lab(
            a=FakeUnix("a", ip="10.0.0.1", has_bash=False),
            b=FakeUnix("b", ip="10.0.0.2"),
        )
        with pytest.raises(ValueError, match="has_bash"):
            asyncio.run(_resolve_chain(lab, [("a", None), ("b", None)]))


def _container(cid: str, parent: FakeUnix, inspect_ip: str = "172.17.0.2"):
    """A stand-in that IS a DockerContainerHost for the manage layer's isinstance check.

    Built via ``__new__`` so ``__post_init__`` (session manager, etc.) never
    runs; only the attributes the manage layer touches are set. The parent is
    a small proxy whose ``exec`` answers the ``docker inspect`` with a real
    ``CommandResult`` (global constraint: never SimpleNamespace fakes).
    """
    from otto.host.docker_host import DockerContainerHost

    class _ParentProxy:
        def __init__(self) -> None:
            self.id = parent.id
            self.calls: list[str] = []

        async def exec(self, cmd: str, timeout: float | None = None, **_: object):
            self.calls.append(cmd)
            return CommandResult(status=Status.Success, value=f"{inspect_ip}\n", command=cmd)

    ctr = DockerContainerHost.__new__(DockerContainerHost)
    object.__setattr__(ctr, "id", cid)
    object.__setattr__(ctr, "parent", _ParentProxy())
    object.__setattr__(ctr, "container_id", "abc123")
    object.__setattr__(ctr, "has_bash", True)
    return ctr


class TestContainerRules:
    def _setup(self):
        parent = FakeUnix("carrot_seed", ip="10.10.200.11")
        ctr = _container("carrot_seed.repo2.oldos", parent)
        other = FakeUnix("tomato_soil", ip="10.10.200.12")
        return _lab(**{parent.id: parent, ctr.id: ctr, other.id: other}), parent, ctr, other

    def test_container_endpoint_with_parent_neighbor_ok(self) -> None:
        lab, parent, ctr, other = self._setup()
        resolved = asyncio.run(
            _resolve_chain(lab, [(other.id, None), (parent.id, None), (ctr.id, None)])
        )
        assert resolved[-1].ip == "172.17.0.2"
        assert resolved[-1].hop.interface is None

    def test_container_neighbor_must_be_parent(self) -> None:
        lab, _parent, ctr, other = self._setup()
        with pytest.raises(ValueError, match="parent"):
            asyncio.run(_resolve_chain(lab, [(other.id, None), (ctr.id, None)]))

    def test_container_cannot_be_relay(self) -> None:
        lab, parent, ctr, other = self._setup()
        with pytest.raises(ValueError, match="endpoint"):
            asyncio.run(_resolve_chain(lab, [(parent.id, None), (ctr.id, None), (other.id, None)]))

    def test_iface_on_container_rejected(self) -> None:
        lab, parent, ctr, other = self._setup()
        with pytest.raises(ValueError, match="interface"):
            asyncio.run(
                _resolve_chain(lab, [(other.id, None), (parent.id, None), (ctr.id, "eth0")])
            )


def _discovered(tunnel: Tunnel) -> TunnelDiscovery:
    return TunnelDiscovery(
        tunnels=[
            DiscoveredTunnel(
                tunnel=tunnel, present=set(), missing=set(), age_seconds=0, uncertain=False
            )
        ],
        unreachable=[],
    )


class TestConflicts:
    def test_same_id_rejected(self) -> None:
        t = Tunnel(protocol="udp", service_port=5000, path=(TunnelHop("a"), TunnelHop("b")))
        with pytest.raises(ValueError, match="already exists"):
            _check_conflicts(_discovered(t), t)

    def test_endpoint_bind_conflict_rejected(self) -> None:
        live = Tunnel(
            protocol="udp", service_port=5000, path=(TunnelHop("a"), TunnelHop("c"), TunnelHop("b"))
        )
        reversed_dup = Tunnel(
            protocol="udp", service_port=5000, path=(TunnelHop("b"), TunnelHop("d"), TunnelHop("a"))
        )
        with pytest.raises(ValueError, match="binds"):
            _check_conflicts(_discovered(live), reversed_dup)

    def test_different_port_or_proto_coexists(self) -> None:
        live = Tunnel(protocol="udp", service_port=5000, path=(TunnelHop("a"), TunnelHop("b")))
        other_port = Tunnel(
            protocol="udp", service_port=6000, path=(TunnelHop("a"), TunnelHop("b"))
        )
        other_proto = Tunnel(
            protocol="tcp", service_port=5000, path=(TunnelHop("a"), TunnelHop("b"))
        )
        _check_conflicts(_discovered(live), other_port)
        _check_conflicts(_discovered(live), other_proto)


class TestProcessPlan:
    def test_three_hop_plan_order_and_argv(self) -> None:
        t = Tunnel(
            protocol="udp", service_port=5000, path=(TunnelHop("a"), TunnelHop("c"), TunnelHop("b"))
        )
        plan = _process_plan(
            t,
            ips=["10.0.0.1", "10.0.0.2", "10.0.0.3"],
            p_fwd=50001,
            p_rev=50002,
            deliver_fwd="127.0.0.1",
        )
        keys = [(p.hop_index, p.direction, p.role) for p in plan]
        # FWD downstream-first (egress, relay, ingress), then REV downstream-first.
        assert keys == [
            (2, Direction.FWD, Role.EGRESS),
            (1, Direction.FWD, Role.RELAY),
            (0, Direction.FWD, Role.INGRESS),
            (0, Direction.REV, Role.EGRESS),
            (1, Direction.REV, Role.RELAY),
            (2, Direction.REV, Role.INGRESS),
        ]
        by_key = {(p.hop_index, p.direction): p for p in plan}
        assert by_key[(0, Direction.FWD)].socat_args[1] == (
            "UDP4-LISTEN:5000,bind=10.0.0.1,fork,reuseaddr"
        )
        assert by_key[(0, Direction.FWD)].socat_args[2] == "TCP4:10.0.0.2:50001"
        assert by_key[(1, Direction.FWD)].socat_args[2] == "TCP4:10.0.0.3:50001"
        assert by_key[(2, Direction.FWD)].socat_args[2] == "UDP4:127.0.0.1:5000"
        assert by_key[(2, Direction.REV)].socat_args[1] == (
            "UDP4-LISTEN:5000,bind=10.0.0.3,fork,reuseaddr"
        )
        assert by_key[(1, Direction.REV)].socat_args[2] == "TCP4:10.0.0.1:50002"
        assert by_key[(0, Direction.REV)].socat_args[2] == "UDP4:127.0.0.1:5000"

    def test_dest_overrides_fwd_delivery_only(self) -> None:
        t = Tunnel(
            protocol="udp", service_port=5000, path=(TunnelHop("a"), TunnelHop("b")), dest="x"
        )
        plan = _process_plan(
            t, ips=["10.0.0.1", "10.0.0.2"], p_fwd=50001, p_rev=50002, deliver_fwd="10.9.9.9"
        )
        by_key = {(p.hop_index, p.direction): p for p in plan}
        assert by_key[(1, Direction.FWD)].socat_args[2] == "UDP4:10.9.9.9:5000"
        assert by_key[(0, Direction.REV)].socat_args[2] == "UDP4:127.0.0.1:5000"
