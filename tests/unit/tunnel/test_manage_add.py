"""``add_tunnel``: ports, launch order, rollback, and post-add verify (spec §6-§7)."""

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any

import pytest

from otto.result import CommandResult
from otto.tunnel import manage
from otto.tunnel.manage import (
    AddedTunnel,
    ResolvedHop,
    _kill_tunnel_on,
    _probe_used_ports,
    _require_tools,
    _verify_chain,
    add_tunnel,
)
from otto.tunnel.model import Direction, ProcKey, Role, Tunnel, TunnelHop
from otto.tunnel.sentinel import ParsedSentinel, encode_sentinel, parse_sentinel
from otto.tunnel.socat import DISCOVERY_PS_COMMAND, FREE_PORT_PROBE_COMMAND
from otto.utils import Status

_LAUNCH_PREFIX = "bash -c 'if command -v systemd-run"
_SENTINEL_RE = re.compile(r"otto-tunnel:v1:\S+")

_LO = 49152


@dataclass
class FakeHost:
    """Scripted host: tool-check / port-probe / launch / discovery-scan, in that
    dispatch order, keyed off the exact command text each phase sends.

    ``ps_texts`` is a small queue consumed by every ``DISCOVERY_PS_COMMAND``
    call (conflict-check scan, verify scan(s), rollback scan): while more than
    one entry remains, each call pops the next one; once a single entry is
    left, it repeats forever. That lets a test express "empty, then this
    forever" or "empty, then incomplete, then complete" with a plain list.
    """

    id: str
    ip: str = ""
    interfaces: dict = field(default_factory=dict)
    has_bash: bool = True
    tools_ok: bool = True
    probe_ports: str = ""
    probe_ok: bool = True
    probe_timeout: bool = False
    launch_fail_at: int | None = None
    launch_hang_at: int | None = None
    """Launch-call index (this host's own launch counter) that hangs past any
    caller-side timeout instead of returning — simulates an ack that never
    arrives even though the command may have reached the host."""
    scan_fail: bool = False
    """Raise instead of answering the discovery scan (host unreachable)."""
    kill_fail: bool = False
    """Raise instead of acking a rollback ``kill`` command."""
    ps_texts: list = field(default_factory=lambda: [""])
    commands: list = field(default_factory=list)
    calls: list | None = None
    _launch_calls: int = field(default=0, init=False, repr=False)

    async def oneshot(self, cmd: str, timeout: float | None = None, **_: object) -> CommandResult:
        self.commands.append(cmd)
        if self.calls is not None:
            self.calls.append((self.id, cmd))
        if "command -v socat" in cmd:
            return CommandResult(
                status=Status.Success, value="ok" if self.tools_ok else "no", command=cmd
            )
        if cmd == FREE_PORT_PROBE_COMMAND:
            if self.probe_timeout:
                raise asyncio.TimeoutError("probe wedged")
            if not self.probe_ok:
                return CommandResult(status=Status.Failed, value="boom", command=cmd, retcode=1)
            return CommandResult(status=Status.Success, value=self.probe_ports, command=cmd)
        if cmd == DISCOVERY_PS_COMMAND:
            if self.scan_fail:
                raise ConnectionError("host is unreachable")
            text = self.ps_texts.pop(0) if len(self.ps_texts) > 1 else self.ps_texts[0]
            return CommandResult(status=Status.Success, value=text, command=cmd)
        if cmd.startswith("kill "):
            if self.kill_fail:
                raise ConnectionError("kill failed")
            return CommandResult(status=Status.Success, value="", command=cmd)
        # Anything else is a launch command.
        idx = self._launch_calls
        self._launch_calls += 1
        if self.launch_hang_at == idx:
            await asyncio.sleep(0.2)
        if self.launch_fail_at == idx:
            return CommandResult(status=Status.Failed, value="boom", command=cmd, retcode=1)
        return CommandResult(status=Status.Success, value="", command=cmd)


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
    omit: frozenset = frozenset(),
) -> str:
    """Every expected process for *host_id*, minus any keys in *omit*."""
    hop_index = next(i for i, h in enumerate(tunnel.path) if h.host == host_id)
    lines = []
    pid = pid_start
    for key in tunnel.expected_processes():
        host, direction, role = key
        if host != host_id or key in omit:
            continue
        carrier = carrier_fwd if direction is Direction.FWD else carrier_rev
        lines.append(_ps_line(tunnel, direction, role, hop_index, carrier, pid))
        pid += 1
    return "\n".join(lines)


def _extract_sentinel(cmd: str) -> ParsedSentinel:
    match = _SENTINEL_RE.search(cmd)
    assert match, f"no sentinel token found in launch command: {cmd!r}"
    parsed = parse_sentinel(match.group(0))
    assert parsed is not None, f"sentinel token failed to parse: {match.group(0)!r}"
    return parsed


def _launches(calls: list) -> list:
    """(host_id, ParsedSentinel) for every launch command, in call order."""
    return [(host, _extract_sentinel(cmd)) for host, cmd in calls if cmd.startswith(_LAUNCH_PREFIX)]


def _pair(port: int = 8080, protocol: str = "tcp") -> tuple[FakeLab, list, Tunnel]:
    """A bare a/b pair with a shared ``calls`` log and the tunnel they'll form."""
    calls: list = []
    a = FakeHost("a", ip="10.0.0.1", calls=calls)
    b = FakeHost("b", ip="10.0.0.2", calls=calls)
    tunnel = Tunnel(protocol=protocol, service_port=port, path=(TunnelHop("a"), TunnelHop("b")))
    return _lab(a=a, b=b), calls, tunnel


class TestAddDirectPair:
    def test_add_direct_pair_launches_four_processes_in_order(self) -> None:
        lab, calls, tunnel = _pair()
        a, b = lab.hosts["a"], lab.hosts["b"]
        carrier_fwd, carrier_rev = _LO, _LO + 1
        a.ps_texts = ["", _full_ps(tunnel, "a", carrier_fwd, carrier_rev)]
        b.ps_texts = ["", _full_ps(tunnel, "b", carrier_fwd, carrier_rev)]

        added = asyncio.run(add_tunnel(lab, [("a", None), ("b", None)], port=8080))

        assert isinstance(added, AddedTunnel)
        assert added.tunnel.id == tunnel.id
        assert {added.carrier_fwd, added.carrier_rev} == {carrier_fwd, carrier_rev}
        assert added.carrier_fwd != added.carrier_rev
        for carrier in (added.carrier_fwd, added.carrier_rev):
            assert 49152 <= carrier <= 65535
            assert carrier != 8080

        launches = _launches(calls)
        assert len(launches) == 4
        hosts_in_order = [h for h, _p in launches]
        assert hosts_in_order == ["b", "a", "a", "b"]
        assert (launches[0][1].direction, launches[0][1].role) == (Direction.FWD, Role.EGRESS)
        assert (launches[1][1].direction, launches[1][1].role) == (Direction.FWD, Role.INGRESS)
        assert (launches[2][1].direction, launches[2][1].role) == (Direction.REV, Role.EGRESS)
        assert (launches[3][1].direction, launches[3][1].role) == (Direction.REV, Role.INGRESS)
        for _host, parsed in launches:
            assert parsed.tunnel.id == tunnel.id


class TestProtocolCase:
    def test_protocol_uppercased_accepted(self) -> None:
        """``--protocol UDP`` (spec §11 shows ``udp|tcp``) must be normalized
        to lowercase before it reaches the library, so the stored
        ``Tunnel.protocol``/sentinel/id stay consistently lowercase."""
        lab, _calls, tunnel = _pair(protocol="udp")
        a, b = lab.hosts["a"], lab.hosts["b"]
        carrier_fwd, carrier_rev = _LO, _LO + 1
        a.ps_texts = ["", _full_ps(tunnel, "a", carrier_fwd, carrier_rev)]
        b.ps_texts = ["", _full_ps(tunnel, "b", carrier_fwd, carrier_rev)]

        added = asyncio.run(add_tunnel(lab, [("a", None), ("b", None)], port=8080, protocol="UDP"))

        assert added.tunnel.id == tunnel.id
        assert added.tunnel.protocol == "udp"


class TestAddThreeHop:
    def test_add_three_hop_launches_six(self) -> None:
        calls: list = []
        a = FakeHost("a", ip="10.0.0.1", calls=calls)
        b = FakeHost("b", ip="10.0.0.2", calls=calls)
        c = FakeHost("c", ip="10.0.0.3", calls=calls)
        lab = _lab(a=a, b=b, c=c)
        tunnel = Tunnel(
            protocol="tcp", service_port=8080, path=(TunnelHop("a"), TunnelHop("b"), TunnelHop("c"))
        )
        carrier_fwd, carrier_rev = _LO, _LO + 1
        for host_id, host in (("a", a), ("b", b), ("c", c)):
            host.ps_texts = ["", _full_ps(tunnel, host_id, carrier_fwd, carrier_rev)]

        added = asyncio.run(add_tunnel(lab, [("a", None), ("b", None), ("c", None)], port=8080))

        assert added.tunnel.id == tunnel.id
        launches = _launches(calls)
        assert len(launches) == 6
        got = [(h, p.direction, p.role) for h, p in launches]
        assert got == [
            ("c", Direction.FWD, Role.EGRESS),
            ("b", Direction.FWD, Role.RELAY),
            ("a", Direction.FWD, Role.INGRESS),
            ("a", Direction.REV, Role.EGRESS),
            ("b", Direction.REV, Role.RELAY),
            ("c", Direction.REV, Role.INGRESS),
        ]


class TestRejectedBeforeLaunch:
    def test_unsupported_protocol_rejected(self) -> None:
        lab, calls, _tunnel = _pair()
        with pytest.raises(ValueError, match="unsupported protocol"):
            asyncio.run(add_tunnel(lab, [("a", None), ("b", None)], port=8080, protocol="icmp"))
        assert calls == []

    def test_conflict_id_rejected_before_launch(self) -> None:
        lab, calls, tunnel = _pair()
        a, b = lab.hosts["a"], lab.hosts["b"]
        # host a already reports a live process for the SAME tunnel id.
        a.ps_texts = [_full_ps(tunnel, "a", _LO, _LO + 1)]
        b.ps_texts = [""]

        with pytest.raises(ValueError, match="already exists"):
            asyncio.run(add_tunnel(lab, [("a", None), ("b", None)], port=8080))

        assert all("command -v socat" not in cmd for _h, cmd in calls)
        assert all(not cmd.startswith(_LAUNCH_PREFIX) for _h, cmd in calls)
        assert all(cmd != FREE_PORT_PROBE_COMMAND for _h, cmd in calls)

    def test_dest_in_chain_rejected(self) -> None:
        """``--dest`` naming a chain host would deliver into the reverse
        ingress and form a loop the post-add verify cannot detect (spec
        §6.3) — must be rejected before any host is touched."""
        lab, calls, _tunnel = _pair()

        with pytest.raises(ValueError, match="--dest") as exc_info:
            asyncio.run(add_tunnel(lab, [("a", None), ("b", None)], port=8080, dest=("a", None)))

        message = str(exc_info.value)
        assert "path" in message
        assert "--dest" in message
        assert calls == []  # guard fired before any launch/probe/tools-check


class TestPortProbe:
    def test_port_probe_union_excludes_used_everywhere(self) -> None:
        lab, _calls, tunnel = _pair()
        a, b = lab.hosts["a"], lab.hosts["b"]
        a.probe_ports = "LISTEN 0 128 0.0.0.0:49152 0.0.0.0:*\n"
        b.probe_ports = "LISTEN 0 128 0.0.0.0:49153 0.0.0.0:*\n"
        carrier_fwd, carrier_rev = _LO + 2, _LO + 3
        a.ps_texts = ["", _full_ps(tunnel, "a", carrier_fwd, carrier_rev)]
        b.ps_texts = ["", _full_ps(tunnel, "b", carrier_fwd, carrier_rev)]

        added = asyncio.run(add_tunnel(lab, [("a", None), ("b", None)], port=8080))

        assert added.carrier_fwd == _LO + 2
        assert added.carrier_rev == _LO + 3

    def test_probe_command_failure_tolerated(self) -> None:
        lab, _calls, tunnel = _pair()
        a, b = lab.hosts["a"], lab.hosts["b"]
        a.probe_ok = False  # a returns a Failed CommandResult for the probe
        carrier_fwd, carrier_rev = _LO, _LO + 1
        a.ps_texts = ["", _full_ps(tunnel, "a", carrier_fwd, carrier_rev)]
        b.ps_texts = ["", _full_ps(tunnel, "b", carrier_fwd, carrier_rev)]

        added = asyncio.run(add_tunnel(lab, [("a", None), ("b", None)], port=8080))

        assert added.carrier_fwd == carrier_fwd
        assert added.carrier_rev == carrier_rev

    def test_probe_timeout_raises_host_named(self) -> None:
        lab, calls, _tunnel = _pair()
        lab.hosts["b"].probe_timeout = True

        with pytest.raises(RuntimeError, match="host 'b' timed out probing"):
            asyncio.run(add_tunnel(lab, [("a", None), ("b", None)], port=8080))

        assert all(not cmd.startswith(_LAUNCH_PREFIX) for _h, cmd in calls)
        assert all(not cmd.startswith("kill ") for _h, cmd in calls)


class TestRequireTools:
    def test_require_tools_missing_socat_raises_host_named(self) -> None:
        lab, calls, _tunnel = _pair()
        lab.hosts["b"].tools_ok = False

        with pytest.raises(RuntimeError, match="host 'b' is missing socat"):
            asyncio.run(add_tunnel(lab, [("a", None), ("b", None)], port=8080))

        assert all(cmd != FREE_PORT_PROBE_COMMAND for _h, cmd in calls)
        assert all(not cmd.startswith(_LAUNCH_PREFIX) for _h, cmd in calls)


class TestRollback:
    def test_launch_failure_rolls_back(self) -> None:
        lab, _calls, tunnel = _pair()
        a, b = lab.hosts["a"], lab.hosts["b"]
        carrier_fwd, carrier_rev = _LO, _LO + 1
        # Launch order for a pair: FWD egress@b (1st), FWD ingress@a (2nd),
        # REV egress@a (3rd), REV ingress@b (4th). Fail a's FIRST launch call
        # (its own launch index 0), i.e. the 2nd launch overall.
        a.launch_fail_at = 0
        # a's process never actually started -> its scans always come up empty.
        a.ps_texts = ["", ""]
        # b's first launch (FWD egress) DID succeed -> its rollback scan shows it running.
        b.ps_texts = [
            "",
            _full_ps(
                tunnel,
                "b",
                carrier_fwd,
                carrier_rev,
                omit=frozenset({("b", Direction.REV, Role.INGRESS)}),
            ),
        ]

        with pytest.raises(RuntimeError, match="host 'a' failed to launch"):
            asyncio.run(add_tunnel(lab, [("a", None), ("b", None)], port=8080))

        assert not any(cmd.startswith("kill ") for cmd in a.commands)
        assert any(cmd.startswith("kill ") for cmd in b.commands)

    def test_verify_missing_process_rolls_back_and_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(manage, "_VERIFY_RETRY_DELAY", 0.0)
        verify_calls: list = []
        original = manage._verify_chain

        async def counting_verify_chain(resolved: Any, t: Tunnel) -> Any:
            verify_calls.append(1)
            return await original(resolved, t)

        monkeypatch.setattr(manage, "_verify_chain", counting_verify_chain)

        lab, _calls, tunnel = _pair()
        a, b = lab.hosts["a"], lab.hosts["b"]
        carrier_fwd, carrier_rev = _LO, _LO + 1
        a.ps_texts = ["", _full_ps(tunnel, "a", carrier_fwd, carrier_rev)]
        # b's REV ingress process never shows up, on either verify attempt.
        missing_key: ProcKey = ("b", Direction.REV, Role.INGRESS)
        incomplete = _full_ps(tunnel, "b", carrier_fwd, carrier_rev, omit=frozenset({missing_key}))
        b.ps_texts = ["", incomplete]

        with pytest.raises(RuntimeError, match=r"not running: .*b/rev/ingress") as exc_info:
            asyncio.run(manage.add_tunnel(lab, [("a", None), ("b", None)], port=8080))

        assert "b/rev/ingress" in str(exc_info.value)
        assert len(verify_calls) == 2  # initial + exactly one retry, then gives up
        # Rollback: a had both procs running -> killed; b had 1 of 2 -> killed too.
        assert any(cmd.startswith("kill ") for cmd in a.commands)
        assert any(cmd.startswith("kill ") for cmd in b.commands)

    def test_verify_retries_once_before_failing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(manage, "_VERIFY_RETRY_DELAY", 0.0)
        verify_calls: list = []
        original = manage._verify_chain

        async def counting_verify_chain(resolved: Any, t: Tunnel) -> Any:
            verify_calls.append(1)
            return await original(resolved, t)

        monkeypatch.setattr(manage, "_verify_chain", counting_verify_chain)

        lab, _calls, tunnel = _pair()
        a, b = lab.hosts["a"], lab.hosts["b"]
        carrier_fwd, carrier_rev = _LO, _LO + 1
        a.ps_texts = ["", _full_ps(tunnel, "a", carrier_fwd, carrier_rev)]
        settling_key: ProcKey = ("b", Direction.REV, Role.INGRESS)
        incomplete = _full_ps(tunnel, "b", carrier_fwd, carrier_rev, omit=frozenset({settling_key}))
        complete = _full_ps(tunnel, "b", carrier_fwd, carrier_rev)
        b.ps_texts = ["", incomplete, complete]

        added = asyncio.run(manage.add_tunnel(lab, [("a", None), ("b", None)], port=8080))

        assert added.tunnel.id == tunnel.id
        assert len(verify_calls) == 2
        assert not any(cmd.startswith("kill ") for cmd in a.commands)
        assert not any(cmd.startswith("kill ") for cmd in b.commands)

    def test_first_launch_timeout_rolls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A timed-out FIRST launch bounds the ack, not the send: the command
        may have already reached the host, so rollback must still run
        (spec §6.4 — no half-tunnels survive a failed add)."""
        monkeypatch.setattr(manage, "_TUNNEL_HOST_TIMEOUT", 0.05)
        lab, calls, tunnel = _pair()
        a, b = lab.hosts["a"], lab.hosts["b"]
        carrier_fwd = _LO
        # First launch overall is b's FWD egress (launch index 0 on b) — hang it.
        b.launch_hang_at = 0
        # Rollback scan on b reports the process DID actually start.
        b.ps_texts = ["", _ps_line(tunnel, Direction.FWD, Role.EGRESS, 1, carrier_fwd, 999)]
        a.ps_texts = ["", ""]

        with pytest.raises(RuntimeError, match="host 'b' timed out spawning"):
            asyncio.run(add_tunnel(lab, [("a", None), ("b", None)], port=8080))

        launch_idx = next(i for i, (_h, cmd) in enumerate(calls) if cmd.startswith(_LAUNCH_PREFIX))
        scan_idx = next(
            i for i, (_h, cmd) in enumerate(calls) if cmd == DISCOVERY_PS_COMMAND and i > launch_idx
        )
        assert scan_idx > launch_idx  # rollback scan happened AFTER the failed launch
        assert any(cmd.startswith("kill ") for _h, cmd in calls[scan_idx:])


class TestInternals:
    def test_require_tools_ok_host_passes(self) -> None:
        host = FakeHost("a", ip="10.0.0.1")
        asyncio.run(_require_tools(host))  # no raise

    def test_probe_used_ports_gathers_across_hosts(self) -> None:
        a = FakeHost("a", ip="10.0.0.1", probe_ports="LISTEN 0 0.0.0.0:49200 *:*\n")
        b = FakeHost("b", ip="10.0.0.2", probe_ports="LISTEN 0 0.0.0.0:49201 *:*\n")

        resolved = [
            ResolvedHop(hop=TunnelHop("a"), ip="10.0.0.1", host=a),
            ResolvedHop(hop=TunnelHop("b"), ip="10.0.0.2", host=b),
        ]
        used = asyncio.run(_probe_used_ports(resolved))
        assert used == {49200, 49201}

    def test_kill_tunnel_on_is_best_effort_on_dead_host(self) -> None:
        """A host that raises during the rollback scan/kill must not blow up rollback."""

        @dataclass
        class ExplodingHost:
            id: str

            async def oneshot(self, cmd: str, timeout: float | None = None, **_: object) -> Any:
                raise ConnectionError("host is gone")

        asyncio.run(_kill_tunnel_on([ExplodingHost("ghost")], "tun-deadbeefdead-8080"))  # no raise

    def test_kill_tunnel_on_swallows_kill_failure(self) -> None:
        """Scan succeeds and finds a live process, but the ``kill`` itself
        raises — the per-host kill guard (not the scan guard) must swallow it."""
        tunnel = Tunnel(protocol="tcp", service_port=8080, path=(TunnelHop("a"), TunnelHop("b")))
        host = FakeHost(
            "a",
            ip="10.0.0.1",
            ps_texts=[_full_ps(tunnel, "a", _LO, _LO + 1)],
            kill_fail=True,
        )

        asyncio.run(_kill_tunnel_on([host], tunnel.id))  # no raise

        assert any(cmd.startswith("kill ") for cmd in host.commands)

    def test_verify_chain_reports_present_and_unreachable(self) -> None:
        tunnel = Tunnel(protocol="tcp", service_port=8080, path=(TunnelHop("a"), TunnelHop("b")))
        a = FakeHost("a", ip="10.0.0.1", ps_texts=[_full_ps(tunnel, "a", _LO, _LO + 1)])
        b = FakeHost("b", ip="10.0.0.2", scan_fail=True)

        resolved = [
            ResolvedHop(hop=TunnelHop("a"), ip="10.0.0.1", host=a),
            ResolvedHop(hop=TunnelHop("b"), ip="10.0.0.2", host=b),
        ]
        present, unreachable = asyncio.run(_verify_chain(resolved, tunnel))
        assert unreachable == ["b"]
        assert ("a", Direction.FWD, Role.INGRESS) in present
        assert ("b", Direction.FWD, Role.EGRESS) not in present

    def test_raise_verify_failure_includes_unreachable_note(self) -> None:
        tunnel = Tunnel(protocol="tcp", service_port=8080, path=(TunnelHop("a"), TunnelHop("b")))
        missing: set = {("b", Direction.REV, Role.INGRESS)}

        with pytest.raises(RuntimeError, match="unreachable during verify: b"):
            manage._raise_verify_failure(tunnel, missing, ["b"])
