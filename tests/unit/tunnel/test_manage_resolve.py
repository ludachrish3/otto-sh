"""Chain resolution, container rules, conflicts, and the process plan (spec §6-§8)."""

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from otto.result import CommandResult
from otto.tunnel.discovery import DiscoveredTunnel, TunnelDiscovery, TunnelNotMeasuredError
from otto.tunnel.manage import (
    _check_conflicts,
    _container_ip,
    _planned_chain,
    _process_plan,
    _resolve_chain,
    _resolve_one,
)
from otto.tunnel.model import Direction, Role, Tunnel, TunnelHop
from otto.tunnel.socat import SocatCarrier
from otto.utils import Status
from tests.conftest import active_context


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

    def test_busybox_profile_host_rejected_as_chain_member(self) -> None:
        """The busybox `os_type` profile's `has_bash=False` default is not just
        a value sitting in a dict — it reaches this exact guard.

        Built through the real factory (``create_host_from_dict``), not a
        ``FakeUnix`` with ``has_bash`` hand-set: that would only restate the
        guard's condition, not prove the profile actually produces it. This
        pins the consequence documented on
        ``otto.host.os_profile._register_builtin_os_profiles`` — a busybox
        host cannot occupy ANY position in a tunnel's ``--hosts`` chain (relay
        hop or path endpoint alike; this loop runs over every entry in
        ``specs``, not just interior ones). It can still be named via
        ``--dest`` (the far-end delivery target `add_tunnel` resolves through
        `_resolve_one`, which carries no `has_bash` check at all) —
        `test_resolve_one_accepts_a_busybox_host` below pins that other half
        directly, so the split is asserted on both sides, not just claimed
        here.
        """
        from otto.host.factory import create_host_from_dict

        busybox = create_host_from_dict(
            {
                "element": "bb1",
                "os_type": "busybox",
                "ip": "10.0.0.1",
                "creds": [{"login": "v", "password": "v"}],
            }
        )
        lab = _lab(bb=busybox, b=FakeUnix("b", ip="10.0.0.2"))
        with pytest.raises(ValueError, match="has_bash"):
            asyncio.run(_resolve_chain(lab, [("bb", None), ("b", None)]))

    def test_resolve_one_accepts_a_busybox_host(self) -> None:
        """The other half of the target-vs-hop split: `_resolve_one` — what
        `add_tunnel` resolves `--dest` through — carries no `has_bash` check
        at all, so a busybox host resolves here even though
        `test_busybox_profile_host_rejected_as_chain_member` above shows the
        same host refused as a `--hosts` chain member.

        This is not a redundant restatement of that test. A natural-looking
        DRY refactor — hoisting the `has_bash` check out of `_resolve_chain`'s
        per-spec loop and into `_resolve_one` itself, since `_resolve_chain`
        calls `_resolve_one` once per host anyway — would silently take away
        busybox's only supported tunnel role (as `--dest`) while leaving
        every other tunnel test green: none of them assert that `_resolve_one`
        itself accepts a `has_bash=False` host, only that `_resolve_chain`
        rejects one as a chain member. This test is what would catch that
        refactor.
        """
        from otto.host.factory import create_host_from_dict

        busybox = create_host_from_dict(
            {
                "element": "bb1",
                "os_type": "busybox",
                "ip": "10.0.0.1",
                "creds": [{"login": "v", "password": "v"}],
            }
        )
        lab = _lab(bb=busybox)

        resolved = asyncio.run(_resolve_one(lab, ("bb", None)))

        assert resolved.ip == "10.0.0.1"
        assert resolved.hop == TunnelHop("bb")


def _container(
    cid: str, parent: FakeUnix, inspect_ip: str = "172.17.0.2", *, inspect_timeout: bool = False
):
    """A stand-in that IS a DockerContainerHost for the manage layer's isinstance check.

    Built via ``__new__`` so ``__post_init__`` (session manager, etc.) never
    runs; only the attributes the manage layer touches are set. The parent is
    a small proxy whose ``exec`` answers the ``docker inspect`` with a real
    ``CommandResult`` (global constraint: never SimpleNamespace fakes).

    *inspect_timeout* makes the ``docker inspect`` call come back
    ``timed_out=True`` instead of an ip, to exercise ``_container_ip``'s
    timeout-to-host-named-RuntimeError conversion.
    """
    from otto.host.docker_host import DockerContainerHost

    class _ParentProxy:
        def __init__(self) -> None:
            self.id = parent.id
            self.calls: list[str] = []

        async def exec(self, cmd: str, timeout: float | None = None, **_: object):
            self.calls.append(cmd)
            if inspect_timeout:
                return CommandResult(
                    status=Status.Error,
                    value=f"Command timed out after {timeout}s",
                    command=cmd,
                    retcode=-1,
                    timed_out=True,
                )
            return CommandResult(status=Status.Success, value=f"{inspect_ip}\n", command=cmd)

    ctr = DockerContainerHost.__new__(DockerContainerHost)
    object.__setattr__(ctr, "id", cid)
    object.__setattr__(ctr, "parent", _ParentProxy())
    object.__setattr__(ctr, "container_id", "abc123")
    object.__setattr__(ctr, "has_bash", True)
    return ctr


class TestContainerRules:
    def _setup(self):
        parent = FakeUnix("test1", ip="10.10.200.11")
        ctr = _container("test1.repo2.oldos", parent)
        other = FakeUnix("test2_soil", ip="10.10.200.12")
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

    def test_inspect_timeout_raises_host_named(self) -> None:
        """``docker inspect`` timing out on the parent is a host-named
        RuntimeError (spec §9), not a generic failure — ``_container_ip``
        converts ``CommandResult.timed_out`` rather than catching a raised
        ``asyncio.TimeoutError`` (the host call no longer raises one)."""
        parent = FakeUnix("test1", ip="10.10.200.11")
        ctr = _container("test1.repo2.oldos", parent, inspect_timeout=True)
        other = FakeUnix("test2_soil", ip="10.10.200.12")
        lab = _lab(**{parent.id: parent, ctr.id: ctr, other.id: other})

        with pytest.raises(RuntimeError, match="timed out inspecting container"):
            asyncio.run(_resolve_chain(lab, [(other.id, None), (parent.id, None), (ctr.id, None)]))


def _real_placeholder(running_cid: str = "", inspect_ip: str = "172.17.0.2"):
    """A REAL placeholder DockerContainerHost (empty container_id) on a mocked parent.

    The parent answers the liveness probe (``docker ps -q``) with
    *running_cid* and ``docker inspect`` with *inspect_ip* — but only for
    that exact cid, mirroring real docker (inspecting an empty/unknown id
    fails). Unlike :func:`_container` above, ``__post_init__`` runs, so the
    resolve path is exercised end-to-end at the unit level.
    """
    from unittest.mock import AsyncMock, MagicMock

    from otto.host.docker_host import DockerContainerHost

    parent = MagicMock()
    parent.id = "test1"
    parent.name = "test1"
    parent.term = "ssh"

    async def _exec(cmd: str, timeout: float | None = None, **_: object) -> CommandResult:
        if cmd.startswith("docker ps -q"):
            return CommandResult(status=Status.Success, value=f"{running_cid}\n", command=cmd)
        if cmd.startswith("docker inspect") and running_cid and running_cid in cmd:
            return CommandResult(status=Status.Success, value=f"{inspect_ip}\n", command=cmd)
        return CommandResult(status=Status.Failed, value="Error: No such object", command=cmd)

    parent.exec = AsyncMock(side_effect=_exec)
    return DockerContainerHost(
        parent=parent,
        container_id="",
        project="repo1",
        service="api",
        compose_project="otto-repo1-x",
    )


class TestContainerLiveness:
    """Issue #139: `add` never starts containers — docker is a test aid only."""

    def _lab_with(self, ctr):
        parent = FakeUnix("test1", ip="10.10.200.11")
        other = FakeUnix("test2_soil", ip="10.10.200.12")
        return _lab(**{parent.id: parent, ctr.id: ctr, other.id: other}), parent, other

    def test_down_container_endpoint_fails_loud_without_compose(self, monkeypatch) -> None:
        from unittest.mock import AsyncMock, MagicMock

        compose_up = AsyncMock()
        monkeypatch.setattr("otto.docker.compose.compose_up", compose_up)
        monkeypatch.setattr("otto.config.get_repos", MagicMock(return_value=[]))
        monkeypatch.setattr("otto.config.get_lab", MagicMock())
        ctr = _real_placeholder(running_cid="")
        lab, parent, other = self._lab_with(ctr)

        with pytest.raises(ValueError, match="otto docker up"):
            asyncio.run(_resolve_chain(lab, [(other.id, None), (parent.id, None), (ctr.id, None)]))
        compose_up.assert_not_awaited()

    def test_running_container_endpoint_resolves_from_probe(self) -> None:
        ctr = _real_placeholder(running_cid="abc123")
        lab, parent, other = self._lab_with(ctr)

        resolved = asyncio.run(
            _resolve_chain(lab, [(other.id, None), (parent.id, None), (ctr.id, None)])
        )

        assert resolved[-1].ip == "172.17.0.2"
        assert ctr.container_id == "abc123"


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
            carrier=SocatCarrier(),
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
        assert by_key[(0, Direction.FWD)].argv[1] == (
            "UDP4-LISTEN:5000,bind=10.0.0.1,fork,reuseaddr"
        )
        assert by_key[(0, Direction.FWD)].argv[2] == "TCP4:10.0.0.2:50001"
        assert by_key[(1, Direction.FWD)].argv[2] == "TCP4:10.0.0.3:50001"
        assert by_key[(2, Direction.FWD)].argv[2] == "UDP4:127.0.0.1:5000"
        assert by_key[(2, Direction.REV)].argv[1] == (
            "UDP4-LISTEN:5000,bind=10.0.0.3,fork,reuseaddr"
        )
        assert by_key[(1, Direction.REV)].argv[2] == "TCP4:10.0.0.1:50002"
        assert by_key[(0, Direction.REV)].argv[2] == "UDP4:127.0.0.1:5000"

    def test_dest_overrides_fwd_delivery_only(self) -> None:
        t = Tunnel(
            protocol="udp", service_port=5000, path=(TunnelHop("a"), TunnelHop("b")), dest="x"
        )
        plan = _process_plan(
            t,
            ips=["10.0.0.1", "10.0.0.2"],
            p_fwd=50001,
            p_rev=50002,
            deliver_fwd="10.9.9.9",
            carrier=SocatCarrier(),
        )
        by_key = {(p.hop_index, p.direction): p for p in plan}
        assert by_key[(1, Direction.FWD)].argv[2] == "UDP4:10.9.9.9:5000"
        assert by_key[(0, Direction.REV)].argv[2] == "UDP4:127.0.0.1:5000"


# ── --dry-run: the pure/device split in hop resolution ───────────────────────


class TestPlannedChainRefusesFromLabDataAlone:
    """``_planned_chain`` must make the SAME refusals ``_resolve_chain`` does.

    Every one of them is decided from declared lab fields, so a dry run
    answers each completely — a chain refused here is not "not measured", it
    is decided, and swallowing the refusal would make the preview less
    faithful rather than safer.
    """

    def test_it_resolves_a_normal_chain_exactly_and_touches_nothing(self) -> None:
        lab = _lab(
            a=FakeUnix("a", interfaces={"eth1": "10.0.0.1"}),
            b=FakeUnix("b", ip="10.0.0.2"),
        )
        with active_context(dry_run=True):
            planned = _planned_chain(lab, [("a", "eth1"), ("b", None)])
        assert [p.ip for p in planned] == ["10.0.0.1", "10.0.0.2"]
        assert [p.hop for p in planned] == [TunnelHop("a", "eth1"), TunnelHop("b", None)]

    @pytest.mark.parametrize(
        ("specs", "match"),
        [
            ([("a", None)], "at least 2"),
            ([("a", None), ("b", None), ("a", None)], "more than once"),
            ([("a", None), ("ghost", None)], "unknown host"),
            ([("a", "eth9"), ("b", None)], "no interface"),
            ([("m", None), ("b", None)], "ambiguous interface"),
            ([("bare", None), ("b", None)], "no usable address"),
            ([("nobash", None), ("b", None)], "has_bash=False"),
        ],
    )
    def test_each_pure_refusal_still_fires(self, specs, match: str) -> None:
        lab = _lab(
            a=FakeUnix("a", interfaces={"eth1": "10.0.0.1"}),
            b=FakeUnix("b", ip="10.0.0.2"),
            m=FakeUnix("m", interfaces={"eth0": "10.0.0.3", "eth1": "10.0.1.3"}),
            bare=FakeUnix("bare"),
            nobash=FakeUnix("nobash", ip="10.0.0.9", has_bash=False),
        )
        with active_context(dry_run=True), pytest.raises(ValueError, match=match):
            _planned_chain(lab, specs)

    def test_a_container_hop_is_named_but_its_address_is_left_unread(self) -> None:
        parent = FakeUnix("test1", ip="10.10.200.11")
        ctr = _container("test1.repo2.oldos", parent)
        other = FakeUnix("test2_soil", ip="10.10.200.12")
        lab = _lab(**{parent.id: parent, ctr.id: ctr, other.id: other})

        # POSITIVE CONTROL: a real run DOES resolve it, off the device, to the
        # ip the parent's `docker inspect` reports.
        chain = [(other.id, None), (parent.id, None), (ctr.id, None)]
        real = asyncio.run(_resolve_chain(lab, chain))
        assert real[-1].ip == "172.17.0.2"
        ctr.parent.calls.clear()

        with active_context(dry_run=True):
            planned = _planned_chain(lab, chain)

        # The hop's IDENTITY is declared, so it is known; its ADDRESS is not.
        assert planned[-1].hop == TunnelHop(ctr.id, None)
        assert planned[-1].ip is None
        assert [p.ip for p in planned[:-1]] == ["10.10.200.12", "10.10.200.11"]
        assert ctr.parent.calls == []

    def test_the_container_interface_refusal_still_fires(self) -> None:
        parent = FakeUnix("test1", ip="10.10.200.11")
        ctr = _container("test1.repo2.oldos", parent)
        other = FakeUnix("test2_soil", ip="10.10.200.12")
        lab = _lab(**{parent.id: parent, ctr.id: ctr, other.id: other})
        with active_context(dry_run=True), pytest.raises(ValueError, match="no @interface"):
            _planned_chain(lab, [(other.id, None), (parent.id, None), (ctr.id, "eth0")])


class TestTheBackstopGuardsResolutionToo:
    """``_resolve_one`` is public-API-reachable; a dry run must not fabricate there.

    Unreachable from ``add_tunnel`` today — it short-circuits into
    ``_plan_add`` above — and closed anyway, because ``_resolve_one`` is what
    a future caller (or a re-ordered ``add_tunnel``) reaches first. Before the
    backstop, the ``docker ps -q`` probe's synthetic reply made the container
    read as RUNNING and cached the banner as its container id, and
    ``docker inspect``'s made that same banner the hop's IP ADDRESS.
    """

    def test_a_container_hop_raises_instead_of_answering(self) -> None:
        parent = FakeUnix("test1", ip="10.10.200.11")
        ctr = _real_placeholder(running_cid="abc123")
        other = FakeUnix("test2_soil", ip="10.10.200.12")
        lab = _lab(**{parent.id: parent, ctr.id: ctr, other.id: other})

        with (
            active_context(dry_run=True),
            pytest.raises(TunnelNotMeasuredError, match="not probed for liveness"),
        ):
            asyncio.run(_resolve_one(lab, (ctr.id, None)))
        assert ctr.container_id == ""

    def test_container_ip_raises_instead_of_returning_the_banner(self) -> None:
        parent = FakeUnix("test1", ip="10.10.200.11")
        ctr = _container("test1.repo2.oldos", parent)

        # POSITIVE CONTROL: outside a dry run it really does read an address.
        assert asyncio.run(_container_ip(ctr)) == "172.17.0.2"

        with (
            active_context(dry_run=True),
            pytest.raises(TunnelNotMeasuredError, match="docker inspect"),
        ):
            asyncio.run(_container_ip(ctr))
