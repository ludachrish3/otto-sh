"""``remove_tunnel`` / ``remove_all_tunnels``: kill + post-kill verify (spec §10)."""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from otto.result import CommandResult
from otto.tunnel.discovery import DISCOVERY_PS_COMMAND
from otto.tunnel.manage import RemovedReport, remove_all_tunnels, remove_tunnel
from otto.tunnel.model import Direction, Role, Tunnel, TunnelHop
from otto.tunnel.sentinel import encode_sentinel
from otto.utils import Status
from tests.conftest import active_context

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
    kill_timeout: bool = False
    """If True, the ``kill`` command comes back ``timed_out=True`` instead of acking."""
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
            if self.kill_timeout:
                return CommandResult(
                    status=Status.Error,
                    value=f"Command timed out after {timeout}s",
                    command=cmd,
                    retcode=-1,
                    timed_out=True,
                )
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

    def test_kill_timeout_marks_unreachable_and_continues(self) -> None:
        # A timed-out kill exec (CommandResult.timed_out) must land exactly
        # like a failed/unreachable kill — logged with its OWN "timed out
        # reaping" message (not the generic "kill failed"), host added to
        # `unreachable`, reap CONTINUES to the other hosts — never a raise
        # that aborts the whole reap (the 4 raising sites don't apply here).
        # A plain report-shape assertion can't tell "the timed_out branch
        # ran" from "it fell through to the generic is_ok failure branch"
        # (both produce the same RemovedReport) — the log message is what
        # pins that a distinct branch fired, so capture it directly on the
        # module logger (caplog's root-propagation can be toggled off by
        # unrelated tests; see test_local_host.py's own note on this).
        tunnel = Tunnel(protocol="tcp", service_port=8080, path=(TunnelHop("a"), TunnelHop("b")))
        carrier_fwd, carrier_rev = _LO, _LO + 1
        text_a, _pids_a = _full_ps(tunnel, "a", carrier_fwd, carrier_rev, 100)
        text_b, pids_b = _full_ps(tunnel, "b", carrier_fwd, carrier_rev, 200)
        a = FakeHost("a", ps_texts=[text_a, text_a], kill_timeout=True)
        b = FakeHost("b", ps_texts=[text_b, ""])
        lab = _lab(a=a, b=b)

        captured: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                captured.append(record)

        manage_logger = logging.getLogger("otto.tunnel.manage")
        handler = _Capture()
        manage_logger.addHandler(handler)
        try:
            report = asyncio.run(remove_tunnel(lab, tunnel.id))
        finally:
            manage_logger.removeHandler(handler)

        messages = [r.getMessage() for r in captured]
        assert any("timed out reaping host 'a'" in m for m in messages)
        assert not any("kill failed" in m for m in messages)
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


# ── --dry-run: the reap that never happened ──────────────────────────────────


class TestRemoveDryRunPreviewsTheSweep:
    """`removed (none found)`, exit 0, was a claim about hosts nobody scanned.

    It is also byte-identical to the answer a real reap of a clean lab gives —
    the only wrong answer indistinguishable from a right one.
    """

    def _bed(self):
        tunnel = _three_hop_tunnel()
        hosts = {}
        for host_id in ("a", "b", "c"):
            text, _pids = _full_ps(tunnel, host_id, _LO, _LO + 1, 100)
            hosts[host_id] = FakeHost(host_id, ps_texts=[text, ""])
        hosts["shell_less"] = FakeHost("shell_less", has_bash=False)
        return _lab(**hosts), tunnel

    def test_by_id_previews_the_scope_and_kills_nothing(self) -> None:
        lab, tunnel = self._bed()

        with active_context(dry_run=True):
            report = asyncio.run(remove_tunnel(lab, tunnel.id))

        assert [cmd for h in lab.hosts.values() for cmd in h.commands] == []
        assert report.plan is not None
        assert (report.removed_ids, report.killed, report.unreachable, report.survivors) == (
            [],
            {},
            [],
            [],
        )
        would = "\n".join(report.plan.would)
        assert "scan 3 has_bash host(s)" in would
        assert "a, b, c" in would
        # The has_bash=False host is EXCLUDED from the reap, which is exactly
        # the leak the chain refusal exists to prevent — and it is lab data,
        # so a dry run can show it.
        assert "shell_less" not in would
        assert f"tunnel {tunnel.id!r}" in would

    def test_all_says_it_would_reap_every_tunnel_not_one(self) -> None:
        lab, tunnel = self._bed()
        with active_context(dry_run=True):
            report = asyncio.run(remove_all_tunnels(lab))
        would = "\n".join(report.plan.would)
        assert "EVERY otto tunnel" in would
        assert tunnel.id not in would

    def test_it_names_what_only_a_scan_could_decide(self) -> None:
        lab, tunnel = self._bed()
        with active_context(dry_run=True):
            plan = asyncio.run(remove_tunnel(lab, tunnel.id)).plan
        joined = "\n".join(plan.unchecked)
        assert "which tunnels are live" in joined
        assert "SURVIVOR" in joined
        assert plan.would
        assert plan.unchecked

    def test_a_real_remove_on_the_same_bed_still_reaps_and_reports(self) -> None:
        """Positive control: the short-circuit did not swallow the product."""
        lab, tunnel = self._bed()
        report = asyncio.run(remove_tunnel(lab, tunnel.id))
        assert report.plan is None
        assert report.removed_ids == [tunnel.id]
        assert sorted(report.killed) == ["a", "b", "c"]
