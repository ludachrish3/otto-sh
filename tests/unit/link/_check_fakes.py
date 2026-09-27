"""Shared doubles for the ``otto link check`` tests: a netem-modelling host and a small lab.

``SandboxHost`` models the one thing a check needs to believe: devices whose
netem state changes with the ``tc`` commands otto sends, each device its
own. Pings, timed connects and transfers are answered from the state of the
device their target routes through, so a check that applied the wrong tree,
or to the wrong device, measures the wrong thing, exactly as on a real host.
It also keeps the host's routing table for sandbox addresses: the first
``ip addr add <a>/30 dev <veth>`` for a ``/30`` owns its route, as the
kernel's first connected route does, until that veth is deleted; ``ip route
get`` answers from it. A target no sandbox route covers goes out the host's
real link device. The namespaces ``ip netns add`` creates show in the
sweep's listing, created at the listing's own clock, until ``ip netns del``
removes them. Explicit ``answer`` rules still win over the model, for
scripting a rejected apply or a failed setup step.
"""

import asyncio
import re
from dataclasses import dataclass, field
from ipaddress import ip_address, ip_network
from typing import Any

import pytest

from otto.check.fingerprint import ELEVATION_PROBE
from otto.link import _check_rows
from otto.link import check as link_check
from otto.link.model import Link, LinkEndpoint
from otto.link.netem import netem_args, parse_qdisc_show
from otto.link.params import ImpairmentParams
from otto.link.sandbox import SWEEP_LIST_COMMAND, new_sandbox
from otto.result import CommandResult, Results, Status
from tests.unit.check._fakes import ScriptedHost
from tests.unit.check.test_fingerprint import MODERN

from ._tc_render import ROOT_LINE, filter_show_text

PINNED_TOKEN = "000000"
"""The sandbox token :func:`pin_sandbox` fixes, so its addresses are known."""
NS_IP = new_sandbox(PINNED_TOKEN).ns_ip
"""The namespace end of the pinned sandbox: ``198.18.0.2``, the first block's second host."""
TEST3_ADDR = (
    "3: eth1    inet 10.10.200.13/24 brd 10.10.200.255 scope global eth1\\  x\n"
    "4: eth1.100    inet 10.10.201.13/24 brd 10.10.201.255 scope global eth1.100\\  x\n"
    "5: eth1.200    inet 10.10.202.13/24 brd 10.10.202.255 scope global eth1.200\\  x\n"
)

_PING_RE = re.compile(r"ping -c (\d+) -i ([\d.]+).* (?P<ip>\d+\.\d+\.\d+\.\d+)")
_DEV_RE = re.compile(r"\bdev (?P<dev>[^\s;&|)]+)")
_WHOLE_RE = re.compile(r"^tc qdisc replace dev \S+ root netem (?P<args>.*)$")
_BAND_RE = re.compile(
    r"^tc qdisc replace dev \S+ parent 1:(?P<band>[0-9a-f]+) handle \S+ netem (?P<args>.*)$"
)
_FILTER_RE = re.compile(
    r"match ip (?P<field>dport|sport) (?P<val>\d+) 0x(?P<mask>[0-9a-f]{4}) "
    r"flowid 1:(?P<band>[0-9a-f]+)$"
)
_SOCAT_PORT_RE = re.compile(r"TCP:(?P<ip>[\d.]+):(?P<port>\d+)")
_PY_PORT_RE = re.compile(r"create_connection\(\(\S*?(?P<ip>\d+\.\d+\.\d+\.\d+)\S*, (?P<port>\d+)\)")
"""The python3 client's port, past its address's shlex ``'"'"'`` quote escaping."""
_NBYTES_RE = re.compile(r"head -c (\d+)|\* (\d+)\)")
_RATE_RE = re.compile(r"^(\d+(?:\.\d+)?)(kbit|mbit)$")
_ADDR_ADD_RE = re.compile(r"^ip addr add (?P<cidr>\S+) dev (?P<dev>\S+)$")
_LINK_DEL_RE = re.compile(r"^ip link del (?P<dev>\S+)")
_ROUTE_GET_RE = re.compile(r"^ip route get (?P<dst>\S+)")
_NETNS_RE = re.compile(r"^ip netns (?P<verb>add|del) (?P<name>\S+)")


def _netem(args: str) -> ImpairmentParams:
    params = parse_qdisc_show(f"qdisc netem 8001: root refcnt 2 limit 1000 {args}\n")
    assert params is not None, args
    return params


def iputils(transmitted: int, replies: list[tuple[int, float, bool]]) -> str:
    """iputils ``ping`` output: one line per ``(seq, rtt_ms, duplicate)`` reply, arrival order."""
    lines = [f"PING {NS_IP} ({NS_IP}) 56(84) bytes of data."]
    for seq, rtt, dup in replies:
        suffix = " (DUP!)" if dup else ""
        lines.append(f"64 bytes from {NS_IP}: icmp_seq={seq} ttl=64 time={rtt:.3f} ms{suffix}")
    unique = sum(1 for *_, dup in replies if not dup)
    lines += [
        "",
        f"--- {NS_IP} ping statistics ---",
        f"{transmitted} packets transmitted, {unique} received, time 1000ms",
    ]
    return "\n".join(lines) + "\n"


@dataclass
class Netdev:
    """One device's modelled netem state."""

    whole: ImpairmentParams | None = None
    scoped: bool = False
    bands: dict[int, ImpairmentParams] = field(default_factory=dict)
    filters: list[str] = field(default_factory=list)


_NOW = 1_790_000_000
"""The host clock the sweep's listing reports, and every namespace it created was made at."""


@dataclass
class SandboxHost(ScriptedHost):
    """A ScriptedHost whose netem devices behave like netem (see module docstring)."""

    fingerprint: str = MODERN
    elevation: tuple[bool, str] = (True, "0\n")
    """What otto's own elevation (``id -u``, run elevated) answers: ok, and its output."""
    elevation_hangs: bool = False
    """The elevation waits on a password prompt nobody answers: the run times out."""
    stale: str = ""
    """Extra sweep-listing text: leftovers of runs this model never saw."""
    noisy: bool = False
    devs: dict[str, Netdev] = field(default_factory=dict)
    """Device name -> its netem state; a device never touched is clean."""
    routes: dict[str, str] = field(default_factory=dict)
    """``/30`` -> the veth whose connected route reaches it: the first one added wins."""
    namespaces: list[str] = field(default_factory=list)
    """Namespaces ``ip netns add`` made and ``ip netns del`` has not removed."""
    interleave: bool = False
    """Every command yields to the event loop first, so concurrent checks interleave."""
    signal_on: str | None = None
    """A command containing this sets :attr:`signalled` once it has run."""
    signalled: asyncio.Event = field(default_factory=asyncio.Event)

    async def exec(self, cmd: str, timeout: float | None = None, **kw: object) -> CommandResult:
        if self.interleave:
            await asyncio.sleep(0)
        result = await super().exec(cmd, timeout, **kw)
        self._signal(cmd)
        return result

    async def run(self, cmd: str, sudo: bool = False, **kw: object) -> Results:
        if self.interleave:
            await asyncio.sleep(0)
        results = await super().run(cmd, sudo, **kw)
        self._signal(self.commands[-1])
        return results

    def _signal(self, cmd: str) -> None:
        if self.signal_on is not None and self.signal_on in cmd:
            self.signalled.set()

    def clean(self) -> bool:
        """Whether no device carries any netem state."""
        return all(d.whole is None and not d.scoped for d in self.devs.values())

    def _dev(self, name: str) -> Netdev:
        return self.devs.setdefault(name, Netdev())

    def _dev_for(self, ip: str) -> Netdev:
        """The device a packet to *ip* leaves by: a sandbox veth whose ``/30`` covers it, or
        else the host's real link device (any device that is not a sandbox veth)."""
        dst = ip_address(ip)
        veth = next((d for net, d in self.routes.items() if dst in ip_network(net)), None)
        if veth is not None:
            return self._dev(veth)
        link = next((d for name, d in self.devs.items() if not name.startswith("ock")), None)
        return link or Netdev()

    def _result(self, cmd: str) -> CommandResult:
        if self.fail_all:
            raise ConnectionError(f"{self.id} is down")
        for needle, ok, output in self.rules:
            if needle in cmd:
                return _reply(cmd, output, ok=ok)
        if cmd == ELEVATION_PROBE:
            if self.elevation_hangs:
                return CommandResult(
                    status=Status.Error,
                    value="Command timed out after 8s\n[sudo] password for vagrant:",
                    command=cmd,
                    retcode=-1,
                    timed_out=True,
                )
            ok, output = self.elevation
            return _reply(cmd, output, ok=ok)
        return _reply(cmd, self._simulate(cmd))

    def netem(self, *cmds: str) -> None:
        """Apply *cmds* to the model as if otto's ``impair``/``repair`` had run them here."""
        for cmd in cmds:
            assert self._mutate(cmd), cmd

    def _simulate(self, cmd: str) -> str:
        routed = self._route(cmd)
        if routed is not None:
            return routed
        return "" if self._mutate(cmd) else self._read(cmd)

    def _route(self, cmd: str) -> str | None:
        """Model the root namespace's sandbox routes: add, delete, and ``ip route get``."""
        if m := _ADDR_ADD_RE.match(cmd):
            self.routes.setdefault(str(ip_network(m["cidr"], strict=False)), m["dev"])
            return ""
        if m := _NETNS_RE.match(cmd):
            if m["verb"] == "add":
                self.namespaces.append(m["name"])
            elif m["name"] in self.namespaces:
                self.namespaces.remove(m["name"])
            return ""
        if m := _LINK_DEL_RE.match(cmd):
            self.routes = {net: dev for net, dev in self.routes.items() if dev != m["dev"]}
            return ""
        if m := _ROUTE_GET_RE.match(cmd):
            dst = ip_address(m["dst"])
            dev = next((d for net, d in self.routes.items() if dst in ip_network(net)), "eth0")
            return f"{dst} dev {dev} src {self.ip} uid 0\n    cache\n"
        return None

    def _mutate(self, cmd: str) -> bool:
        """Apply one tc mutation (or the expire timer firing) to the modelled veth."""
        fired = "sleep 3 &&" in cmd  # the expire timer: model it as having fired
        named = _DEV_RE.search(cmd)
        dev = named["dev"] if named is not None else ""
        if fired or (cmd.startswith("tc qdisc del dev ") and cmd.endswith(" root")):
            self.devs[dev] = Netdev()
        elif m := _WHOLE_RE.match(cmd):
            self.devs[dev] = Netdev(whole=_netem(m["args"]))
        elif " root handle 1: prio " in cmd:
            self.devs[dev] = Netdev(scoped=True)
        elif m := _BAND_RE.match(cmd):
            self._dev(dev).bands[int(m["band"], 16)] = _netem(m["args"])
        elif cmd.startswith("tc filter add"):
            self._dev(dev).filters.append(cmd)
        else:
            return False
        return True

    def _read(self, cmd: str) -> str:
        """Answer one read or probe from the modelled veth's current state."""
        if "uname -r" in cmd:
            return self.fingerprint
        if cmd == SWEEP_LIST_COMMAND:
            return self.stale + self._listing()
        if cmd.startswith(("tc qdisc show", "tc filter show")):
            named = _DEV_RE.search(cmd)
            dev = self._dev(named["dev"]) if named is not None else Netdev()
            if cmd.startswith("tc qdisc show"):
                return self._qdisc_show(dev)
            return filter_show_text(dev.filters)
        if m := _PING_RE.search(cmd):
            return self._ping(int(m[1]), self._dev_for(m["ip"]))
        if (m := _SOCAT_PORT_RE.search(cmd) or _PY_PORT_RE.search(cmd)) is not None:
            return self._timed(cmd, int(m["port"]), self._dev_for(m["ip"]))
        return ""

    def _listing(self) -> str:
        """The live namespaces as the sweep's listing shows them, each just created."""
        if not self.namespaces:
            return ""
        lines = [f"{name} (id: {i})" for i, name in enumerate(self.namespaces)]
        lines.append(f"@now {_NOW}")
        lines += [f"@mtime {_NOW} /var/run/netns/{name}" for name in self.namespaces]
        return "\n".join(lines) + "\n"

    def _qdisc_show(self, dev: Netdev) -> str:
        if dev.whole is not None:
            return f"qdisc netem 8001: root refcnt 2 limit 1000 {netem_args(dev.whole)}\n"
        if dev.scoped:
            return ROOT_LINE + "".join(
                f"qdisc netem {band:x}0: parent 1:{band:x} limit 1000 {netem_args(params)}\n"
                for band, params in sorted(dev.bands.items())
            )
        return "qdisc noqueue 0: root refcnt 2\n"

    def _ping(self, count: int, dev: Netdev) -> str:
        if dev.whole is None and self.noisy:
            rtts = [1.0, 40.0, 3.0, 90.0, 2.0, 60.0, 1.5, 75.0, 4.0]
            return iputils(count, [(seq, rtt, False) for seq, rtt in enumerate(rtts, 1)])
        p = dev.whole or ImpairmentParams()
        delay, jitter = p.delay_ms or 0.0, p.jitter_ms or 0.0
        drop_pct = (p.loss_pct or 0.0) + (p.corrupt_pct or 0.0)
        replies: list[tuple[int, float, bool]] = []
        for seq in range(1, count + 1):
            if seq % 10 < drop_pct / 10:
                continue
            rtt = 0.05 + 0.002 * (seq % 2) + delay + (jitter if seq % 2 else -jitter)
            replies.append((seq, rtt, False))
            if p.duplicate_pct and seq % 2 == 0:
                replies.append((seq, rtt, True))
        if p.reorder_pct:
            unique = [r for r in replies if not r[2]]
            for i in range(0, len(unique) - 1, 2):
                unique[i], unique[i + 1] = unique[i + 1], unique[i]
            replies = unique
        return iputils(count, replies)

    def _port_delay(self, port: int, dev: Netdev) -> float:
        if dev.whole is not None:
            return dev.whole.delay_ms or 0.0
        for cmd in dev.filters:
            m = _FILTER_RE.search(cmd)
            assert m is not None, cmd
            if m["field"] == "dport" and port & int(m["mask"], 16) == int(m["val"]):
                return dev.bands[int(m["band"], 16)].delay_ms or 0.0
        return 0.0

    def _timed(self, cmd: str, port: int, dev: Netdev) -> str:
        if "head -c" in cmd or "SHUT_WR" in cmd:
            rate = dev.whole.rate if dev.whole is not None else None
            m = _RATE_RE.match(rate or "")
            kbit = float(m[1]) * (1000 if m[2] == "mbit" else 1) if m else 1e6
            nbytes = _NBYTES_RE.search(cmd)
            assert nbytes is not None, cmd
            ms = int(nbytes[1] or nbytes[2]) * 8 / kbit
        else:
            ms = 0.4 + 2 * self._port_delay(port, dev)
        if "socat" in cmd:
            return f"1000.000000 {1000 + ms / 1000:.6f}\n"
        return f"{ms}\n"


def _reply(cmd: str, output: str, *, ok: bool = True) -> CommandResult:
    return CommandResult(
        status=Status.Success if ok else Status.Failed,
        value=output,
        command=cmd,
        retcode=0 if ok else 1,
    )


@dataclass
class FakeLab:
    hosts: dict
    links: list

    def static_links(self) -> list:
        return list(self.links)


EDGE = Link(
    a=LinkEndpoint(host="test1", interface="eth1.100", ip="10.10.201.11"),
    b=LinkEndpoint(host="test2", interface="eth1.200", ip="10.10.202.12"),
    name="edge",
)
INPATH = Link(a=EDGE.a, b=EDGE.b, name="dataplane", impair="test3")
BARE = Link(a=LinkEndpoint(host="test1", ip="10.10.200.11"), b=EDGE.b, name="bare")


def bed(**test1_kw: Any) -> tuple[FakeLab, SandboxHost, SandboxHost, SandboxHost]:
    test1 = SandboxHost(id="test1", ip="10.10.200.11", **test1_kw)
    test2 = SandboxHost(id="test2", ip="10.10.200.12")
    test3 = SandboxHost(id="test3", ip="10.10.200.13").answer("ip -o addr show", TEST3_ADDR)
    lab = FakeLab(hosts={h.id: h for h in (test1, test2, test3)}, links=[EDGE, INPATH, BARE])
    return lab, test1, test2, test3


def pin_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every sandbox a check builds :data:`PINNED_TOKEN`'s, so a rule can name :data:`NS_IP`.

    Each sandbox's addresses come from its random token; a test that scripts
    an answer to a probe aimed at the namespace needs to know where it aims.
    """
    monkeypatch.setattr(link_check, "fresh_sandbox", lambda listed: new_sandbox(PINNED_TOKEN))


def patch_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every wait a check makes instant."""

    async def instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr(_check_rows, "sleep", instant)
