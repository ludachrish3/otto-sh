"""Shared doubles for the ``otto link check`` tests: a netem-modelling host and a small lab.

``SandboxHost`` models the one thing a check needs to believe: a device whose
netem state changes with the ``tc`` commands otto sends. Pings, timed
connects and transfers are answered from that state, so a check that
applied the wrong tree measures the wrong thing, exactly as on a real host.
Explicit ``answer`` rules still win over the model, for scripting a rejected
apply or a failed setup step.
"""

import re
from dataclasses import dataclass, field
from typing import Any

import pytest

from otto.check.fingerprint import ELEVATION_PROBE
from otto.link import _check_rows
from otto.link.model import Link, LinkEndpoint
from otto.link.netem import netem_args, parse_qdisc_show
from otto.link.params import ImpairmentParams
from otto.link.sandbox import SWEEP_LIST_COMMAND
from otto.result import CommandResult, Status
from tests.unit.check._fakes import ScriptedHost
from tests.unit.check.test_fingerprint import MODERN

from ._tc_render import ROOT_LINE, filter_show_text

NS_IP = "198.18.0.2"
TEST3_ADDR = (
    "3: eth1    inet 10.10.200.13/24 brd 10.10.200.255 scope global eth1\\  x\n"
    "4: eth1.100    inet 10.10.201.13/24 brd 10.10.201.255 scope global eth1.100\\  x\n"
    "5: eth1.200    inet 10.10.202.13/24 brd 10.10.202.255 scope global eth1.200\\  x\n"
)

_PING_RE = re.compile(r"ping -c (\d+) -i ([\d.]+)")
_WHOLE_RE = re.compile(r"^tc qdisc replace dev \S+ root netem (?P<args>.*)$")
_BAND_RE = re.compile(
    r"^tc qdisc replace dev \S+ parent 1:(?P<band>[0-9a-f]+) handle \S+ netem (?P<args>.*)$"
)
_FILTER_RE = re.compile(
    r"match ip (?P<field>dport|sport) (?P<val>\d+) 0x(?P<mask>[0-9a-f]{4}) "
    r"flowid 1:(?P<band>[0-9a-f]+)$"
)
_SOCAT_PORT_RE = re.compile(r"TCP:[\d.]+:(\d+)")
_PY_PORT_RE = re.compile(r"create_connection\(\(\S*?\d+\.\d+\.\d+\.\d+\S*, (\d+)\)")
"""The python3 client's port, past its address's shlex ``'"'"'`` quote escaping."""
_NBYTES_RE = re.compile(r"head -c (\d+)|\* (\d+)\)")
_RATE_RE = re.compile(r"^(\d+(?:\.\d+)?)(kbit|mbit)$")


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
class SandboxHost(ScriptedHost):
    """A ScriptedHost whose netem device behaves like netem (see module docstring)."""

    fingerprint: str = MODERN
    elevation: tuple[bool, str] = (True, "0\n")
    """What otto's own elevation (``id -u``, run elevated) answers: ok, and its output."""
    elevation_hangs: bool = False
    """The elevation waits on a password prompt nobody answers: the run times out."""
    stale: str = ""
    noisy: bool = False
    whole: ImpairmentParams | None = None
    scoped: bool = False
    bands: dict[int, ImpairmentParams] = field(default_factory=dict)
    filters: list[str] = field(default_factory=list)

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

    def _clear(self) -> None:
        self.whole, self.scoped, self.bands, self.filters = None, False, {}, []

    def _simulate(self, cmd: str) -> str:
        return "" if self._mutate(cmd) else self._read(cmd)

    def _mutate(self, cmd: str) -> bool:
        """Apply one tc mutation (or the expire timer firing) to the modelled veth."""
        fired = "sleep 3 &&" in cmd  # the expire timer: model it as having fired
        if fired or (cmd.startswith("tc qdisc del dev ") and cmd.endswith(" root")):
            self._clear()
        elif m := _WHOLE_RE.match(cmd):
            self._clear()
            self.whole = _netem(m["args"])
        elif " root handle 1: prio " in cmd:
            self._clear()
            self.scoped = True
        elif m := _BAND_RE.match(cmd):
            self.bands[int(m["band"], 16)] = _netem(m["args"])
        elif cmd.startswith("tc filter add"):
            self.filters.append(cmd)
        else:
            return False
        return True

    def _read(self, cmd: str) -> str:
        """Answer one read or probe from the modelled veth's current state."""
        if "uname -r" in cmd:
            return self.fingerprint
        if cmd == SWEEP_LIST_COMMAND:
            return self.stale
        if cmd.startswith("tc qdisc show"):
            return self._qdisc_show()
        if cmd.startswith("tc filter show"):
            return filter_show_text(self.filters)
        if m := _PING_RE.search(cmd):
            return self._ping(int(m[1]))
        if (m := _SOCAT_PORT_RE.search(cmd) or _PY_PORT_RE.search(cmd)) is not None:
            return self._timed(cmd, int(m[1]))
        return ""

    def _qdisc_show(self) -> str:
        if self.whole is not None:
            return f"qdisc netem 8001: root refcnt 2 limit 1000 {netem_args(self.whole)}\n"
        if self.scoped:
            return ROOT_LINE + "".join(
                f"qdisc netem {band:x}0: parent 1:{band:x} limit 1000 {netem_args(params)}\n"
                for band, params in sorted(self.bands.items())
            )
        return "qdisc noqueue 0: root refcnt 2\n"

    def _ping(self, count: int) -> str:
        if self.whole is None and self.noisy:
            rtts = [1.0, 40.0, 3.0, 90.0, 2.0, 60.0, 1.5, 75.0, 4.0]
            return iputils(count, [(seq, rtt, False) for seq, rtt in enumerate(rtts, 1)])
        p = self.whole or ImpairmentParams()
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

    def _port_delay(self, port: int) -> float:
        if self.whole is not None:
            return self.whole.delay_ms or 0.0
        for cmd in self.filters:
            m = _FILTER_RE.search(cmd)
            assert m is not None, cmd
            if m["field"] == "dport" and port & int(m["mask"], 16) == int(m["val"]):
                return self.bands[int(m["band"], 16)].delay_ms or 0.0
        return 0.0

    def _timed(self, cmd: str, port: int) -> str:
        if "head -c" in cmd or "SHUT_WR" in cmd:
            rate = self.whole.rate if self.whole is not None else None
            m = _RATE_RE.match(rate or "")
            kbit = float(m[1]) * (1000 if m[2] == "mbit" else 1) if m else 1e6
            nbytes = _NBYTES_RE.search(cmd)
            assert nbytes is not None, cmd
            ms = int(nbytes[1] or nbytes[2]) * 8 / kbit
        else:
            ms = 0.4 + 2 * self._port_delay(port)
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


def patch_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every wait a check makes instant."""

    async def instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr(_check_rows, "sleep", instant)
