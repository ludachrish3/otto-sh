"""A small simulated bed for ``check_tunnel`` tests: hosts, echoes and tunnels.

:class:`BedHost` is a :class:`~tests.unit.check._fakes.ScriptedHost` that also
plays the processes the check starts and stops. An echo launch command starts
a tagged "process" (it shows up in the sweep scan and in the socket listing)
unless a live process already binds its address, where socat would exit at
once; ``kill <pids>`` ends it, and a trips or bulk client only gets an echo back when
something on the simulated bed would really answer it: a live echo bound at
the address it aims at, or a live fake tunnel whose far end has its echo
running. So a check that forgets to start an echo, starts it on the wrong
host, or aims at the wrong address fails here the way it would on a bed.

:class:`Bed` is both the lab (``.hosts``) and the fake ``add_tunnel`` /
``remove_tunnel`` / ``discover_tunnels`` / ``probe_port_budget`` that
:meth:`Bed.install` patches into :mod:`otto.tunnel.check`. Explicit
``ScriptedHost`` rules still win over the simulation, for a test that needs
one command to answer something specific.

A ``--dest`` device is a host too. One with ``has_bash=False`` answers every
command with a failure, as an embedded target with no shell would, and its
``serves`` ports accept the check's data-less TCP handshake. A fake tunnel
built with ``dest=`` delivers FWD to an echo at the dest's own address.
"""

import asyncio
import re
from dataclasses import dataclass, field
from itertools import count
from typing import Any

import pytest

from otto.result import CommandResult, Status
from otto.tunnel.check_probes import CHECK_PREFIX, PS_FAILED, SWEEP_COMMAND
from otto.tunnel.discovery import DISCOVERY_PS_COMMAND, DiscoveredTunnel, TunnelDiscovery
from otto.tunnel.manage import AddedTunnel, PortBudget, RemovedReport
from otto.tunnel.model import Direction, Role, Tunnel, TunnelHop
from otto.tunnel.sentinel import encode_sentinel
from tests.unit.check._fakes import ScriptedHost

SCRATCH_FLOOR = 61000
"""The fake port budget's floor, so the scratch port is 61000 unless --port takes it."""


class _PickRNG:
    """Stub for the ``random.Random`` seam :mod:`otto.tunnel.check` draws its scratch port from.

    ``choice`` returns the lowest candidate by default, so a test can name
    the exact scratch port the check builds on. A test proving the pick is
    genuinely random pins a different value with ``pick=``.
    """

    def __init__(self, pick: int | None = None) -> None:
        self.pick = pick
        self.seen: list[int] | None = None

    def choice(self, seq: list[int]) -> int:
        self.seen = list(seq)
        return self.pick if self.pick is not None else min(seq)


_LAUNCH_RE = re.compile(
    r"(?P<token>otto-check:v1:[^\s']+).*?(?P<kind>TCP4-LISTEN|UDP4-RECVFROM):(?P<port>\d+),"
    r"bind=(?P<ip>[\d.]+)"
)
HANDSHAKE_MARK = "echo handshake ok"
"""Text only the ``--dest`` handshake script carries, so a fake host can tell it apart."""
_CLIENT_RE = re.compile(r"(?P<proto>TCP4|UDP4):(?P<ip>[\d.]+):(?P<port>\d+)")
_TRIPS_RE = re.compile(r"i<=(\d+)")
_SIZE_RE = re.compile(r"head -c (\d+)")
_PIDS = count(100)

TOOLS = ["socat", "bash", "cksum", "mktemp", "head", "tr", "wc"]


def fingerprint_output(missing: list[str], *, clock: bool) -> str:
    """What the fingerprint command prints on a modern bed host."""
    lines = ["kernel=6.8.0-86-generic", "isa=aarch64", "user=vagrant", "netns=1", "netem=1"]
    lines += [f"tool:{t}={0 if t in missing else 1}" for t in TOOLS]
    lines += [
        "ver:socat=socat by Gerhard Rieger - socat version 1.8.0.0 on 2023",
        "ver:bash=5.2.21(1)-release" if clock else "ver:bash=4.2.46(2)-release",
        f"ver:epochrealtime={'yes' if clock else ''}",
        "ver:launcher=systemd-run",
    ]
    return "\n".join(lines)


def trips_output(trips: int, *, clock: bool, fail_at: int | None = None) -> str:
    """A trips transcript: every trip ok (1 ms each), or stopping at *fail_at*."""
    lines = []
    matched = 0
    for i in range(1, trips + 1):
        if fail_at is not None and i >= fail_at:
            lines.append(f"trip {i} timeout")
            break
        stamps = f"{i}.000000 {i}.001000" if clock else " "
        lines.append(f"trip {i} ok {stamps}")
        matched += 1
    lines.append(f"trips {trips} matched {matched}")
    return "\n".join(lines)


def etime(seconds: int) -> str:
    """Format *seconds* the way procps prints ``etime``: ``[[DD-]HH:]MM:SS``."""
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    text = f"{hours:02d}:{minutes:02d}:{secs:02d}" if hours or days else f"{minutes:02d}:{secs:02d}"
    return f"{days}-{text}" if days else text


@dataclass
class Proc:
    """One simulated process: a tagged echo, or a planted process of any kind."""

    pid: int
    token: str
    protocol: str = "tcp"
    ip: str = "127.0.0.1"
    port: int = 0
    started: float = 0.0
    """The bed's clock when it started."""
    age_s: int = 5
    """How long ago it started, as ``ps`` reports it: young unless a test plants it old."""
    etime: str | None = None
    """What ``ps`` prints as its elapsed time; ``None`` formats :attr:`age_s`."""


@dataclass
class BedHost(ScriptedHost):
    """A scripted host that also runs (simulated) echo listeners."""

    bed: "Bed | None" = None
    missing: list[str] = field(default_factory=list)
    """Tools the fingerprint reports absent."""
    clock: bool = True
    """Whether bash here has ``$EPOCHREALTIME`` (bash >= 5)."""
    procs: dict[int, Proc] = field(default_factory=dict)
    held: dict[str, list[str]] = field(default_factory=dict)
    """protocol -> extra socket-listing lines (something else holding a port)."""
    raise_on: str | None = None
    """A command containing this raises ``ConnectionError``, as a host that dropped."""
    silent_echoes: bool = False
    """Echo launches succeed but never bind (the process died at once)."""
    block_on: str | None = None
    """A command containing this never returns (until cancelled); ``entered`` is set first."""
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    unkillable: bool = False
    """``kill`` succeeds but nothing dies."""
    kills: list[int] = field(default_factory=list)
    serves: list[int] = field(default_factory=list)
    """TCP ports a service of the device's own (nothing of otto's) accepts a handshake on."""
    bind_delay_s: float = 0.0
    """How long a launched echo takes, on the bed's clock, to show in the socket listing."""
    slow_exit_scans: int = 0
    """A killed process is still listed by this many more sweep scans before it is gone,
    as a socat still exiting after its SIGTERM would be."""
    dying: dict[int, int] = field(default_factory=dict)
    """pid -> sweep scans left that still list it (see :attr:`slow_exit_scans`)."""
    ps_fails: bool = False
    """``ps`` rejects the scan's ``-o`` fields (a busybox ``ps`` without them), so the
    sweep scan prints only its failure marker."""

    def plant(self, token: str, **kw: Any) -> int:
        """Start a process tagged *token* that the check did not start; return its pid."""
        pid = next(_PIDS)
        started = self.bed.now if self.bed is not None else 0.0
        self.procs[pid] = Proc(pid, token, started=started, **kw)
        return pid

    def alive(self, prefix: str = CHECK_PREFIX) -> list[Proc]:
        """Live processes whose tag starts with *prefix*."""
        return [p for p in self.procs.values() if p.token.startswith(prefix + ":")]

    def _ok(self, cmd: str, value: str = "") -> CommandResult:
        return CommandResult(status=Status.Success, value=value, command=cmd, retcode=0)

    async def exec(self, cmd: str, timeout: float | None = None, **kw: object) -> CommandResult:
        """Answer from the explicit rules first, then from the simulated bed.

        Every call yields to the event loop while it is "running", and the bed
        counts how many are in flight at once, so a check that overlaps two
        probes is caught (``Bed.peak``).
        """
        assert self.bed is not None
        self.bed.in_flight += 1
        self.bed.peak = max(self.bed.peak, self.bed.in_flight)
        try:
            await asyncio.sleep(0)
            if self.block_on is not None and self.block_on in cmd:
                self.entered.set()
                await asyncio.Event().wait()
            return self._answer(cmd)
        finally:
            self.bed.in_flight -= 1

    def _answer(self, cmd: str) -> CommandResult:
        self.commands.append(cmd)
        if self.fail_all or (self.raise_on is not None and self.raise_on in cmd):
            raise ConnectionError(f"{self.id} is down")
        if self.has_bash is False:
            # An embedded target: no shell, so no command of otto's can run here.
            return CommandResult(
                status=Status.Failed, value="sh: not found", command=cmd, retcode=127
            )
        for needle, ok, output in self.rules:
            if needle in cmd:
                status = Status.Success if ok else Status.Failed
                return CommandResult(status=status, value=output, command=cmd, retcode=int(not ok))
        return self._simulate(cmd)

    def _simulate(self, cmd: str) -> CommandResult:
        for handler in (self._scan, self._process, self._listing, self._client):
            value = handler(cmd)
            if value is not None:
                return self._ok(cmd, value)
        return self._ok(cmd)

    def _scan(self, cmd: str) -> str | None:
        if 'echo "kernel=' in cmd:
            return fingerprint_output(self.missing, clock=self.clock)
        if cmd == SWEEP_COMMAND:
            listed = PS_FAILED if self.ps_fails else self._ps(" otto-check:")
            self._age_dying()
            return listed
        if cmd == DISCOVERY_PS_COMMAND:
            return self._ps(" otto-tunnel:")
        return None

    def _process(self, cmd: str) -> str | None:
        if cmd.startswith("kill "):
            for pid in (int(p) for p in cmd.split()[1:]):
                self.kills.append(pid)
                if self.unkillable or pid not in self.procs:
                    continue
                if self.slow_exit_scans:
                    self.dying.setdefault(pid, self.slow_exit_scans)
                else:
                    self.procs.pop(pid, None)
            return ""
        launch = _LAUNCH_RE.search(cmd)
        if launch is None:
            return None
        proto = "tcp" if launch["kind"].startswith("TCP") else "udp"
        port = int(launch["port"])
        # socat exits at once when its address is taken: nothing of this launch keeps running.
        if not self.silent_echoes and not self._held(proto, launch["ip"], port):
            self.plant(launch["token"], protocol=proto, ip=launch["ip"], port=port)
        return ""

    def _age_dying(self) -> None:
        """One more sweep scan has listed every dying process: end those it was the last for."""
        for pid in list(self.dying):
            self.dying[pid] -= 1
            if self.dying[pid] <= 0:
                del self.dying[pid]
                self.procs.pop(pid, None)

    def _held(self, proto: str, ip: str, port: int) -> bool:
        """Whether a live process already binds *ip*:*port*, or the wildcard there, for *proto*."""
        wild = "0.0.0.0"
        return any(
            p.protocol == proto and p.port == port and (p.ip == ip or wild in (p.ip, ip))
            for p in self.procs.values()
        )

    def _listing(self, cmd: str) -> str | None:
        if not cmd.startswith("ss -H"):
            return None
        proto = "tcp" if cmd.startswith("ss -Ht") else "udp"
        lines = list(self.held.get(proto, []))
        assert self.bed is not None
        lines += [
            f"LISTEN 0 5 {p.ip}:{p.port} 0.0.0.0:*"
            for p in self.procs.values()
            if p.port and p.protocol == proto and self.bed.now - p.started >= self.bind_delay_s
        ]
        return "\n".join(lines)

    def _client(self, cmd: str) -> str | None:
        client = _CLIENT_RE.search(cmd)
        if client is None:
            return None
        assert self.bed is not None
        proto = "tcp" if client["proto"] == "TCP4" else "udp"
        if HANDSHAKE_MARK in cmd:
            return self._handshake(client["ip"], int(client["port"]))
        answers = self.bed.answers(self, proto, client["ip"], int(client["port"]))
        trips = _TRIPS_RE.search(cmd)
        if trips is not None:
            count_ = int(trips[1])
            return trips_output(count_, clock=self.clock, fail_at=None if answers else 1)
        size = _SIZE_RE.search(cmd)
        if size is not None and "cksum" in cmd:
            sent = int(size[1])
            got = sent if answers else 0
            verdict = "match" if got == sent else "mismatch"
            return f"sent={sent} got={got} {verdict}\n1.000000 1.004000"
        return None

    def _handshake(self, ip: str, port: int) -> str:
        """A data-less TCP connect: it completes when anything on the target listens there."""
        assert self.bed is not None
        target = self.bed.by_ip(ip)
        listening = target is not None and (
            port in target.serves or self.bed.echo_at(target, "tcp", ip, port)
        )
        if listening and target is not None and (self.id, target.id) not in self.bed.dead:
            return "handshake ok"
        return f"socat E connect(5, AF=2 {ip}:{port}, 16): Connection refused\nhandshake failed"

    def _ps(self, needle: str) -> str:
        lines = [
            f"{p.pid} {p.etime or etime(p.age_s)} {p.token} socat -b 65535"
            for p in self.procs.values()
        ]
        return "\n".join(line for line in lines if needle in f" {line}")


@dataclass
class Bed:
    """The lab, plus fake tunnel management that the simulated clients route through."""

    hosts: dict[str, BedHost]
    tunnels: dict[str, Tunnel] = field(default_factory=dict)
    adds: list[dict[str, Any]] = field(default_factory=list)
    removes: list[str] = field(default_factory=list)
    lists: int = 0
    dead: set[tuple[str, str]] = field(default_factory=set)
    """(client host, target host) pairs whose packets never arrive."""
    add_error: Exception | None = None
    degraded: bool = False
    """The throwaway tunnel lists as ``degraded (5/6)`` instead of ``ok``."""
    remove_survivors: dict[str, list[tuple[str, int]]] = field(default_factory=dict)
    """tunnel id -> the ``(host, pid)`` survivors its removal reports."""
    remove_unreachable: dict[str, list[str]] = field(default_factory=dict)
    """tunnel id -> the lab hosts its removal's scan could not reach."""
    in_flight: int = 0
    peak: int = 0
    """The most host commands that were ever running at once."""
    budget_hosts: list[list[str]] = field(default_factory=list)
    """The hosts each ``probe_port_budget`` call was asked about."""
    now: float = 0.0
    """The bed's clock, in seconds: only the check's own waits move it."""

    def __post_init__(self) -> None:
        for host in self.hosts.values():
            host.bed = self

    def by_ip(self, ip: str) -> BedHost | None:
        """The host carrying *ip*, if any."""
        return next((h for h in self.hosts.values() if h.ip == ip), None)

    def echo_at(self, host: BedHost, proto: str, ip: str, port: int) -> bool:
        """Whether a live echo on *host* is bound at *ip*:*port* for *proto*."""
        return any(p.protocol == proto and p.ip == ip and p.port == port for p in host.alive())

    def answers(self, client: BedHost, proto: str, ip: str, port: int) -> bool:
        """Would a client on *client* aiming at *ip*:*port* get its echo back?"""
        target = self.by_ip(ip)
        if target is None or (client.id, target.id) in self.dead:
            return False
        if self.echo_at(target, proto, ip, port):
            return True
        for tunnel in self.tunnels.values():
            if tunnel.protocol != proto or tunnel.service_port != port:
                continue
            first, last = tunnel.path[0].host, tunnel.path[-1].host
            if target.id == first and client.id == first and tunnel.dest is not None:
                # The FWD egress on the last hop delivers to the dest's own address.
                far = self.hosts[tunnel.dest]
                if (last, far.id) in self.dead:
                    return False
                return self.echo_at(far, proto, far.ip, port)
            if target.id == first and client.id == first:
                far = self.hosts[last]
            elif target.id == last and client.id == last:
                far = self.hosts[first]
            else:
                continue
            return self.echo_at(far, proto, "127.0.0.1", port)
        return False

    def plant_tunnel(self, host_ids: list[str], protocol: str, port: int) -> Tunnel:
        """A user's live tunnel, with its ingress processes on both endpoints."""
        tunnel = Tunnel(
            protocol=protocol, service_port=port, path=tuple(TunnelHop(h) for h in host_ids)
        )
        self.tunnels[tunnel.id] = tunnel
        for index, direction in [(0, Direction.FWD), (len(host_ids) - 1, Direction.REV)]:
            token = encode_sentinel(
                tunnel, direction=direction, role=Role.INGRESS, hop_index=index, carrier_port=62000
            )
            host = self.hosts[host_ids[index]]
            host.plant(token, protocol=protocol, ip=host.ip, port=port)
        return tunnel

    async def add_tunnel(
        self, lab: Any, hosts: list[Any], *, port: int, protocol: str = "tcp", **kw: Any
    ) -> AddedTunnel:
        self.adds.append({"hosts": hosts, "port": port, "protocol": protocol, **kw})
        if self.add_error is not None:
            raise self.add_error
        path = tuple(TunnelHop(h, i) for h, i in hosts)
        dest = kw.get("dest")
        tunnel = Tunnel(
            protocol=protocol,
            service_port=port,
            path=path,
            dest=dest[0] if dest is not None else None,
        )
        self.tunnels[tunnel.id] = tunnel
        return AddedTunnel(tunnel=tunnel, carrier_fwd=61001, carrier_rev=61002)

    async def remove_tunnel(self, lab: Any, tunnel_id: str) -> RemovedReport:
        self.removes.append(tunnel_id)
        gone = self.tunnels.pop(tunnel_id, None)
        return RemovedReport(
            removed_ids=[tunnel_id] if gone else [],
            killed={},
            unreachable=self.remove_unreachable.get(tunnel_id, []),
            survivors=self.remove_survivors.get(tunnel_id, []),
        )

    async def discover_tunnels(self, lab: Any) -> TunnelDiscovery:
        self.lists += 1
        found = []
        for tunnel in self.tunnels.values():
            expected = tunnel.expected_processes()
            missing = {next(iter(expected))} if self.degraded else set()
            found.append(
                DiscoveredTunnel(
                    tunnel=tunnel,
                    present=expected - missing,
                    missing=missing,
                    age_seconds=1,
                    uncertain=False,
                )
            )
        return TunnelDiscovery(tunnels=found, unreachable=[])

    async def probe_port_budget(self, resolved: list[Any]) -> PortBudget:
        self.budget_hosts.append([r.hop.host for r in resolved])
        return PortBudget(used=set(), floor=SCRATCH_FLOOR)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> "Bed":
        """Patch this bed's fakes into :mod:`otto.tunnel.check`; return self."""
        from otto.tunnel import _tunnel_echoes, check

        monkeypatch.setattr(check, "add_tunnel", self.add_tunnel)
        monkeypatch.setattr(check, "remove_tunnel", self.remove_tunnel)
        monkeypatch.setattr(check, "discover_tunnels", self.discover_tunnels)
        monkeypatch.setattr(check, "probe_port_budget", self.probe_port_budget)
        monkeypatch.setattr(check, "_scratch_rng", _PickRNG())

        async def no_wait(seconds: float) -> None:
            self.now += seconds

        monkeypatch.setattr(_tunnel_echoes, "sleep", no_wait)
        monkeypatch.setattr(_tunnel_echoes, "clock", lambda: self.now)
        return self


def bed(*ids: str, **per_host: dict[str, Any]) -> Bed:
    """A bed of hosts ``a``, ``b``, ... at 10.0.0.1, 10.0.0.2, ...; kwargs tweak one host."""
    names = list(ids) or ["a", "b", "c"]
    hosts = {
        name: BedHost(id=name, ip=f"10.0.0.{i}", **per_host.get(name, {}))
        for i, name in enumerate(names, start=1)
    }
    return Bed(hosts=hosts)
