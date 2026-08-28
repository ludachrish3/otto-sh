"""Discovery parsing, grouping, and status semantics (spec #2b §9)."""

import ast
import asyncio
from dataclasses import dataclass, field
from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock

import pytest

from otto.logger.mode import LogMode
from otto.result import CommandResult
from otto.tunnel.discovery import (
    DiscoveredTunnel,
    TunnelNotMeasuredError,
    _scan_hosts,
    discover_tunnels,
    parse_process_discovery,
)
from otto.tunnel.model import Direction, Role, Tunnel, TunnelHop
from otto.tunnel.sentinel import encode_sentinel
from otto.utils import Status
from tests.conftest import active_context

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
    timeout: bool = False
    """The scan ``exec`` comes back ``timed_out=True`` instead of a reply
    (the host's own timeout fires; it never raises ``asyncio.TimeoutError``)."""
    commands: list[str] = field(default_factory=list)

    async def exec(self, cmd: str, timeout: float | None = None, **_: object) -> CommandResult:
        self.commands.append(cmd)
        if self.timeout:
            return CommandResult(
                status=Status.Error,
                value=f"Command timed out after {timeout}s",
                command=cmd,
                retcode=-1,
                timed_out=True,
            )
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

    @pytest.mark.parametrize("attr", ["fail", "timeout"])
    def test_unreachable_chain_host_is_uncertain_not_degraded(self, attr: str) -> None:
        # A scan that raises (fail) and a scan that comes back timed_out=True
        # (timeout — no longer a raised asyncio.TimeoutError) must be handled
        # identically by discover_tunnels: unreachable, not degraded/missing.
        hosts = {h: FakeHost(h, _full_ps_for(h)) for h in ("a", "c", "b")}
        setattr(hosts["c"], attr, True)
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
    parent.id = "test1"
    parent.name = "test1"
    parent.term = "ssh"
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


# ── --dry-run: the backstop, and `list`'s third state ────────────────────────


class TestTheDryRunBackstop:
    """A dry run contacts nothing, so discovery must refuse rather than parse.

    The mechanism it replaces: ``BaseHost.exec`` answers a dry run with
    ``Status.Skipped`` (``is_ok`` is ``True``) carrying ``"[DRY RUN] Command
    not executed"``, and :func:`parse_process_discovery` finds no sentinel in
    it — so every host "answered" with zero processes and the lab looked
    reachable and idle.
    """

    def test_a_dry_run_scan_reports_not_measured_and_asks_no_host(self) -> None:
        hosts = {h: FakeHost(h, _full_ps_for(h)) for h in ("a", "c", "b")}
        lab = FakeLab(hosts)

        # POSITIVE CONTROL on the same bed: a real scan of this lab finds the
        # tunnel, so a `not_measured` verdict below cannot come from an empty
        # or broken fake.
        real = asyncio.run(discover_tunnels(lab))
        assert real.not_measured is False
        assert [d.tunnel.id for d in real.tunnels] == [TUNNEL.id]
        for host in hosts.values():
            host.commands.clear()

        with active_context(dry_run=True):
            dry = asyncio.run(discover_tunnels(lab))

        assert dry.not_measured is True
        assert dry.tunnels == []
        # NOT unreachable: nobody was asked, so nobody failed to answer.
        assert dry.unreachable == []
        assert [cmd for host in hosts.values() for cmd in host.commands] == []

    def test_the_backstop_is_not_filed_as_an_unreachable_host(self) -> None:
        """The inner arm: ``scan()``'s wide ``except Exception`` must not eat it.

        Filing the refusal there would name every host in the lab as
        unreachable — a different wrong story, and the one that sends an
        operator to the network.
        """
        lab = FakeLab({h: FakeHost(h, _full_ps_for(h)) for h in ("a", "c", "b")})
        with active_context(dry_run=True):
            with pytest.raises(TunnelNotMeasuredError, match="dry run makes no device contact"):
                asyncio.run(_scan_hosts(list(lab.hosts.values())))
            assert asyncio.run(discover_tunnels(lab)).unreachable == []

    def test_a_container_is_not_probed_for_liveness_either(self, monkeypatch) -> None:
        """The second device touch — ``is_running()`` never passes through ``exec``.

        Under a dry run the probe's own ``docker ps -q`` was answered by the
        banner, whose truthiness made ``_resolve_container_id`` report the
        container UP and cache ``"[DRY RUN] Command not executed"`` as its id.
        """
        compose_up = AsyncMock()
        monkeypatch.setattr("otto.docker.compose.compose_up", compose_up)
        ctr, calls = _container_placeholder(ps_out="abc123\n")
        lab = FakeLab(hosts={ctr.id: ctr})

        with active_context(dry_run=True):
            result = asyncio.run(discover_tunnels(lab))

        assert result.not_measured is True
        compose_up.assert_not_awaited()
        assert calls == []
        assert ctr.container_id == ""

    def test_a_real_scan_of_an_idle_lab_still_reports_measured_and_empty(self) -> None:
        """The state this replaces has to stay reachable and stay distinct."""
        lab = FakeLab({h: FakeHost(h, "") for h in ("a", "c", "b")})
        result = asyncio.run(discover_tunnels(lab))
        assert result.tunnels == []
        assert result.unreachable == []
        assert result.not_measured is False

    def test_a_lab_with_no_scannable_host_answers_rather_than_declining(self) -> None:
        """The one dry run that is NOT `not_measured`, and deliberately so.

        Discovery only ever scans `has_bash` hosts, and `_validate_chain_shape`
        refuses a non-bash chain member precisely so nothing otto builds can
        live outside that set. So on a lab that declares none, "there are no
        otto tunnels" follows from LAB DATA — the empty answer is complete, not
        deferred, and a real run produces the identical one. Reporting
        `not_measured` here would be the mirror-image of the bug this commit
        fixes: declining a question otto can answer.
        """
        lab = FakeLab({"nobash": FakeHost("nobash", "", has_bash=False)})

        with active_context(dry_run=True):
            dry = asyncio.run(discover_tunnels(lab))
        real = asyncio.run(discover_tunnels(lab))

        assert (dry.tunnels, dry.unreachable, dry.not_measured) == ([], [], False)
        assert (real.tunnels, real.unreachable, real.not_measured) == ([], [], False)

        # DISCRIMINATOR: the False is about the EMPTY SET, not about the dry
        # run. Give the same lab one scannable host and it flips.
        lab.hosts["a"] = FakeHost("a", _full_ps_for("a"))
        with active_context(dry_run=True):
            assert asyncio.run(discover_tunnels(lab)).not_measured is True


class TestEveryDeviceTouchInThePackageIsDeclared:
    """A future touch cannot reach a device under `--dry-run` without being named here.

    The two funnels are the backstop, but a funnel only guarantees anything
    while everything routes through it — and `await host.exec(...)` is one line
    for a future author to write. This scans the package for every call that
    could reach a device and compares the enclosing sites against a declared
    roster, so a new one has to be named here, with its class and its dry-run
    story, before it can land.

    BOTH TOUCH CLASSES, which is the whole point. A first version of this
    scanned only `.exec(`, and that pinned exactly half of what the commit it
    guards is about: `is_running()` never passes through `BaseHost.exec` — it
    is why `_device_running` exists — so an undeclared `await host.is_running()`
    passed the scan while an undeclared `.exec(` reddened it. Under `--dry-run`
    that call is a real `docker ps` on the parent, and
    `_resolve_container_id`'s truthiness parse both answers "running" and
    caches the banner as the container id: the headline mechanism, back, with
    nothing red anywhere. `run` / `put` / `get` are the same escape class
    (`otto.host.host.BaseHost`), and tunnel has no use for them today — which
    is exactly when widening is cheap.

    WHAT IT DOES NOT CATCH, stated so nobody reads it as more than it is. The
    scan is by NAME over this package's own source: a touch reached through an
    alias (`f = host.exec`), through a helper in another package, or on an
    object this scan cannot tell from a host, is invisible to it. It makes a
    direct, literal touch impossible to add SILENTLY; it does not make one
    impossible.
    """

    #: Every attribute whose call can reach a device. `run`/`put`/`get` are
    #: `BaseHost`'s and are unused here; they are listed because an unused
    #: escape is the cheap one to close.
    TOUCHES: ClassVar[frozenset[str]] = frozenset({"exec", "run", "put", "get", "is_running"})

    #: `get` ONLY counts when the call is directly awaited. `Host.get` is a
    #: coroutine; `dict.get` and `Registry.get` are not, and this package makes
    #: five mapping lookups spelled exactly the same way — including `get`
    #: unconditionally would mean four permanent "this is a dict" exemptions
    #: and a re-declaration on every new lookup, which is the kind of noise
    #: that gets a roster ignored.
    AWAITED_ONLY: ClassVar[frozenset[str]] = frozenset({"get"})

    #: (function, attribute) -> the class it belongs to, and why it is allowed.
    #: Keyed by PAIR, not by function: a site that grows a SECOND touch class
    #: has to say so rather than inheriting the first one's exemption.
    DECLARED: ClassVar[dict[tuple[str, str], str]] = {
        ("_device_read", "exec"): (
            "FUNNEL. Every parsed read in the package arrives here and it carries the backstop."
        ),
        ("_device_running", "is_running"): (
            "FUNNEL. The other touch class: a liveness probe is not a command, so no "
            "CommandResult guard can see it. Carries the same backstop."
        ),
        ("scan", "is_running"): (
            "PRESENCE, in _scan_hosts' inner per-host worker. "
            "`getattr(host, 'is_running', None) is not None` asks whether this host HAS a "
            "probe; it never calls it, and hands the call itself to _device_running."
        ),
        ("add_tunnel", "exec"): (
            "MUTATION (the launch loop). The reply is read only for is_ok/timed_out and for "
            "the failure message, never parsed for device state, and a dry run never reaches "
            "it: add_tunnel short-circuits into _plan_add above."
        ),
        ("_kill_tunnel_on", "exec"): (
            "MUTATION (rollback reap). Reached only from add_tunnel's failure path, which a "
            "dry run never enters."
        ),
        ("_reap", "exec"): (
            "MUTATION (the kill). remove_tunnel/remove_all_tunnels short-circuit into "
            "_plan_remove above it."
        ),
    }

    def _touch_sites(self) -> set[tuple[str, str]]:
        """Every ``(enclosing function, touched attribute)`` pair in ``otto/tunnel``."""
        from pathlib import Path

        import otto.tunnel

        package = Path(next(iter(otto.tunnel.__path__)))
        found: set[tuple[str, str]] = set()
        for source in sorted(package.glob("*.py")):
            tree = ast.parse(source.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                    continue
                found |= {(node.name, attr) for attr in self._touched_in(node)}
        return found

    def _own_body(self, node: ast.AST) -> list[ast.AST]:
        """Every node inside *node* that is not inside a NESTED function.

        Attribution is to the INNERMOST enclosing def, so `_scan_hosts`' inner
        `scan` worker is named as itself rather than reported twice. `ast.walk`
        alone descends through nested defs and would credit an outer function
        with a touch it does not make.
        """
        own: list[ast.AST] = []
        queue: list[ast.AST] = list(ast.iter_child_nodes(node))
        while queue:
            child = queue.pop()
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
                continue
            own.append(child)
            queue.extend(ast.iter_child_nodes(child))
        return own

    def _touched_in(self, node: ast.AST) -> set[str]:
        body = self._own_body(node)
        awaited = {id(a.value) for a in body if isinstance(a, ast.Await)}
        out: set[str] = set()
        for inner in body:
            if not isinstance(inner, ast.Call):
                continue
            # `getattr(host, "is_running", ...)` is the bypass spelling this
            # package ALREADY uses, so a scan that only saw attribute access
            # would miss the one form it has proof of.
            if (
                isinstance(inner.func, ast.Name)
                and inner.func.id == "getattr"
                and len(inner.args) >= 2  # getattr(obj, name[, default])
                and isinstance(inner.args[1], ast.Constant)
                and inner.args[1].value in self.TOUCHES
            ):
                out.add(inner.args[1].value)
                continue
            if not isinstance(inner.func, ast.Attribute) or inner.func.attr not in self.TOUCHES:
                continue
            if inner.func.attr in self.AWAITED_ONLY and id(inner) not in awaited:
                continue
            out.add(inner.func.attr)
        return out

    def test_the_scan_finds_both_funnels(self) -> None:
        """Anti-vacuity: a walk that found nothing would satisfy the tests below.

        Both funnels, not one — the half-blind version of this scan passed its
        own anti-vacuity check on `_device_read` alone.
        """
        sites = self._touch_sites()
        assert ("_device_read", "exec") in sites
        assert ("_device_running", "is_running") in sites

    def test_a_mapping_lookup_is_not_mistaken_for_a_transfer(self) -> None:
        """The `get` rule earns its keep: five `dict.get`s here must stay invisible."""
        assert not [pair for pair in self._touch_sites() if pair[1] == "get"]

    def test_no_undeclared_site_touches_a_device(self) -> None:
        undeclared = sorted(self._touch_sites() - set(self.DECLARED))
        assert not undeclared, (
            f"these (function, attribute) pairs in otto/tunnel can reach a device and are not "
            f"declared: {undeclared}. A READ must go through `_device_read` and a liveness "
            f"probe through `_device_running` — both refuse under a dry run, and without them "
            f"the caller parses `[DRY RUN] Command not executed`, or gets a real `docker ps` "
            f"whose synthetic reply reads as 'running'. A MUTATION may touch directly, but "
            f"only if a dry run cannot reach it; add it to DECLARED with the class it belongs "
            f"to and the reason it is allowed."
        )

    def test_every_declared_site_still_exists(self) -> None:
        """A roster entry for a site that is gone is an exemption nobody notices."""
        stale = sorted(set(self.DECLARED) - self._touch_sites())
        assert not stale, f"DECLARED names sites that no longer touch a device: {stale}"
