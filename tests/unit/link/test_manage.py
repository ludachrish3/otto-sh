import asyncio
from types import SimpleNamespace

import pytest

from otto.link.discovery import discover_dynamic_links, discover_observations
from otto.link.manage import (
    AddedTunnel,
    RemovedReport,
    _alloc_carrier_port,
    add_link,
    remove_all_links,
    remove_link,
)
from otto.result import CommandResult
from otto.utils import Status


class FakeHost:
    """Minimal duck-typed host stand-in: sets ``has_bash = True`` so
    discovery's ``has_bash`` capability filter admits it, plus a trivial
    constructor and a scripted ``oneshot`` — no SSH, no creds, no real host
    base class at all.

    A plain subclassable class (Task 5/6 extend it as
    ``SpawnHost(FakeHost)`` / ``KillHost(FakeHost)`` with extra per-instance
    state).
    """

    def __init__(self, host_id, ip, ps_output="", *, unreachable=False):
        self.id = host_id
        self.ip = ip
        self.interfaces = {"eth0": ip}
        self.has_bash = True
        self._ps = ps_output
        self._unreachable = unreachable

    async def oneshot(self, cmd, timeout=None, log=None):
        if self._unreachable:
            raise ConnectionError(f"{self.id} down")
        return CommandResult(Status.Success, value=self._ps)


def _lab(*hosts):
    return SimpleNamespace(hosts={h.id: h for h in hosts}, static_links=list)


PS_A = (
    "  10    00:30 otto-link:v1:lnk-abc-161:udp:test1::161:test2::161 socat "
    "UDP4-LISTEN:161,fork,reuseaddr TCP4:10.0.0.2:50001\n"
)
PS_B = (
    "  20    00:29 otto-link:v1:lnk-abc-161:udp:test1::161:test2::161 socat "
    "TCP4-LISTEN:50001,fork,reuseaddr UDP4:10.0.0.2:161\n"
)


def test_discover_groups_processes_across_hosts_by_id():
    a = FakeHost("test1", "10.0.0.1", PS_A)
    b = FakeHost("test2", "10.0.0.2", PS_B)
    links = asyncio.run(discover_dynamic_links(_lab(a, b)))
    assert [link.id for link in links] == ["lnk-abc-161"]
    assert {links[0].a.host, links[0].b.host} == {"test1", "test2"}


def test_discover_is_best_effort_on_host_down(caplog):
    a = FakeHost("test1", "10.0.0.1", PS_A)
    down = FakeHost("test2", "10.0.0.2", unreachable=True)
    links = asyncio.run(discover_dynamic_links(_lab(a, down)))
    assert [link.id for link in links] == ["lnk-abc-161"]  # still returns what it found
    assert any("test2" in r.message for r in caplog.records)  # named loudly


class SpawnHost(FakeHost):
    def __init__(self, host_id, ip, ps_output=""):
        super().__init__(host_id, ip, ps_output)
        self.commands = []

    async def oneshot(self, cmd, timeout=None, log=None):
        self.commands.append(cmd)
        if "command -v socat" in cmd:
            return CommandResult(Status.Success, value="ok")
        # ss probe → no listeners; ps discovery/spawn → empty
        return CommandResult(Status.Success, value="")


def test_add_link_spawns_ingress_and_egress_and_returns_ids():
    a = SpawnHost("test1", "10.0.0.1")
    b = SpawnHost("test2", "10.0.0.2")
    lab = _lab(a, b)
    added = asyncio.run(
        add_link(lab, [("test1", "eth0"), ("test2", "eth0")], port=161, protocol="udp")
    )
    assert isinstance(added, AddedTunnel)
    assert added.link.id.endswith("-161")
    assert added.ingress_host == "test1"
    assert added.exit_host == "test2"
    # egress launched on B, ingress on A, both carry the sentinel
    assert any("otto-link:v1:" in c and "TCP4-LISTEN" in c for c in b.commands)
    assert any("otto-link:v1:" in c and "UDP4-LISTEN:161" in c for c in a.commands)


def test_add_link_relay_dest_targets_third_host():
    a = SpawnHost("test1", "10.0.0.1")
    b = SpawnHost("test2", "10.0.0.2")
    c = SpawnHost("test3", "10.0.0.3")
    lab = _lab(a, b, c)
    added = asyncio.run(
        add_link(
            lab,
            [("test1", "eth0"), ("test2", "eth0")],
            port=161,
            protocol="udp",
            dest=("test3", "eth0"),
        )
    )
    # logical endpoints are ingress + dest; exit host is still B
    assert {added.link.a.host, added.link.b.host} == {"test1", "test3"}
    assert added.exit_host == "test2"
    assert any("UDP4:10.0.0.3:161" in c2 for c2 in b.commands)  # egress → C
    # ingress on A must dial the EXIT host (B, 10.0.0.2), never the dest (C) —
    # the carrier is a direct A->B TCP hop; B is the one that relays to C.
    assert any("TCP4:10.0.0.2:" in c for c in a.commands)


def test_add_link_rejects_more_than_two_hosts():
    a = SpawnHost("test1", "10.0.0.1")
    lab = _lab(a)
    with pytest.raises(ValueError, match="multi-hop"):
        asyncio.run(add_link(lab, [("a", None), ("b", None), ("c", None)], port=161))


def test_add_link_rejects_unsupported_protocol():
    a = SpawnHost("test1", "10.0.0.1")
    b = SpawnHost("test2", "10.0.0.2")
    lab = _lab(a, b)
    with pytest.raises(ValueError, match="unsupported protocol"):
        asyncio.run(
            add_link(lab, [("test1", "eth0"), ("test2", "eth0")], port=161, protocol="sctp")
        )


def test_add_link_rejects_addressless_endpoint():
    """A host with no interfaces and no top-level ip (e.g. the builtin
    ``local``) resolves to an empty ip — ``add_link`` must fail loud rather
    than silently spawn a tunnel to nowhere.
    """
    a = SpawnHost("test1", "")
    a.interfaces = {}
    b = SpawnHost("test2", "10.0.0.2")
    lab = _lab(a, b)
    with pytest.raises(ValueError, match="no usable address"):
        asyncio.run(add_link(lab, [("test1", None), ("test2", "eth0")], port=161, protocol="udp"))


def test_alloc_carrier_port_excludes_the_service_port():
    """Cheap-minor: the carrier-port pick must not hand back the service
    port itself even when the probe reports no listeners at all — without
    the exclusion, ``pick_free_port`` would happily return the very port
    the ingress/egress sockets are about to bind for the service traffic.
    """
    host = SpawnHost("test2", "10.0.0.2")  # "ss" probe → no listeners (empty output)
    carrier = asyncio.run(_alloc_carrier_port(host, 49152))
    assert carrier != 49152


def test_add_link_conflict_when_id_exists(monkeypatch):
    a = SpawnHost("test1", "10.0.0.1")
    b = SpawnHost("test2", "10.0.0.2")
    lab = _lab(a, b)
    added = asyncio.run(
        add_link(lab, [("test1", "eth0"), ("test2", "eth0")], port=161, protocol="udp")
    )

    async def fake_all(_lab, **_):
        return [added.link]

    monkeypatch.setattr("otto.link.manage.all_links", fake_all)
    with pytest.raises(ValueError, match="already exists"):
        asyncio.run(add_link(lab, [("test1", "eth0"), ("test2", "eth0")], port=161, protocol="udp"))


class KillHost(FakeHost):
    def __init__(self, host_id, ip, ps_output="", *, fail_kill=False):
        super().__init__(host_id, ip, ps_output)
        self.killed = []
        self._fail_kill = fail_kill

    async def oneshot(self, cmd, timeout=None, log=None):
        if cmd.startswith("kill "):
            self.killed.append(cmd)
            if self._fail_kill:
                # A real non-connection failure: the shell ran and returned
                # non-zero (e.g. "no such process") — oneshot does NOT raise
                # for this, it returns Status.Failed (see manage.py's
                # _reap docstring / the task's KEY FACT).
                return CommandResult(Status.Failed, value="kill: (10) - No such process", retcode=1)
            return CommandResult(Status.Success, value="")
        return CommandResult(Status.Success, value=self._ps)


def test_remove_link_kills_matching_pids_across_hosts():
    a = KillHost("test1", "10.0.0.1", PS_A)  # pid 10
    b = KillHost("test2", "10.0.0.2", PS_B)  # pid 20
    report = asyncio.run(remove_link(_lab(a, b), "lnk-abc-161"))
    assert isinstance(report, RemovedReport)
    assert report.removed_ids == ["lnk-abc-161"]
    assert report.killed == {"test1": [10], "test2": [20]}
    assert any("kill 10" in c for c in a.killed)
    assert any("kill 20" in c for c in b.killed)


def test_remove_all_reaps_every_tunnel():
    a = KillHost("test1", "10.0.0.1", PS_A)
    b = KillHost("test2", "10.0.0.2", PS_B)
    report = asyncio.run(remove_all_links(_lab(a, b)))
    assert report.removed_ids == ["lnk-abc-161"]


# ── IMPORTANT #1: a non-zero `kill` (CommandResult(Status.Failed), no raise) ─
# must land the host in `unreachable`, NOT `killed` — before the fix, `_reap`
# only wrapped `oneshot` in a bare try/except (which never fires here, since
# oneshot doesn't raise on shell failure) and unconditionally recorded the
# host as killed regardless of the kill's actual exit status.


def test_remove_link_kill_failure_lands_in_unreachable_not_killed():
    a = KillHost("test1", "10.0.0.1", PS_A, fail_kill=True)  # pid 10, kill fails
    b = KillHost("test2", "10.0.0.2", PS_B)  # pid 20, kill succeeds
    report = asyncio.run(remove_link(_lab(a, b), "lnk-abc-161"))
    assert "test1" not in report.killed
    assert report.killed == {"test2": [20]}
    assert report.unreachable == ["test1"]
    # the CLI's non-zero-exit path (`if report.unreachable: raise typer.Exit(1)`)
    # fires because `unreachable` is non-empty.


# ── IMPORTANT #2: a host that's unreachable at DISCOVERY time (never got to
# contribute an observation, so it never appears in `_reap`'s per-host kill
# loop at all) must still show up in `RemovedReport.unreachable` — before the
# fix it was silently dropped since `_reap` only populated `unreachable` from
# kill-time failures.


def test_remove_all_reports_discovery_time_unreachable_host():
    a = KillHost("test1", "10.0.0.1", PS_A)  # pid 10, reachable
    down = FakeHost("test2", "10.0.0.2", unreachable=True)  # never scanned successfully
    report = asyncio.run(remove_all_links(_lab(a, down)))
    assert report.removed_ids == ["lnk-abc-161"]  # only test1's process was ever observed
    assert report.killed == {"test1": [10]}
    assert report.unreachable == ["test2"]
    assert "test2" not in report.killed


# ── IMPORTANT #2 (discovery layer): discover_observations returns the
# unreachable host ids alongside the observations it did manage to collect.


def test_discover_observations_returns_unreachable_hosts():
    a = FakeHost("test1", "10.0.0.1", PS_A)
    down = FakeHost("test2", "10.0.0.2", unreachable=True)
    observations, unreachable = asyncio.run(discover_observations(_lab(a, down)))
    assert [origin for origin, _obs in observations] == ["test1"]
    assert unreachable == ["test2"]


# ── IMPORTANT #3: a wedged host's oneshot must not hang the feature — bound
# by `_LINK_HOST_TIMEOUT`, patched down here so the test doesn't take 30s.


class SlowHost(FakeHost):
    """A host whose ``oneshot`` never returns within any sane test timeout."""

    async def oneshot(self, cmd, timeout=None, log=None):
        await asyncio.sleep(10)
        return CommandResult(Status.Success, value=self._ps)  # pragma: no cover — never reached


def test_discover_times_out_on_wedged_host_marks_unreachable(monkeypatch, caplog):
    monkeypatch.setattr("otto.link.discovery._LINK_HOST_TIMEOUT", 0.01)
    a = FakeHost("test1", "10.0.0.1", PS_A)
    wedged = SlowHost("test2", "10.0.0.2")
    observations, unreachable = asyncio.run(discover_observations(_lab(a, wedged)))
    assert [origin for origin, _obs in observations] == ["test1"]
    assert unreachable == ["test2"]
    assert any("test2" in r.message and "timed out" in r.message for r in caplog.records)


def test_add_link_times_out_checking_tools_raises_runtime_error(monkeypatch):
    # Bound both the manage-path timeout (tool check) AND the discovery-path
    # timeout (all_links' conflict scan) — otherwise the scan runs the wedged
    # host's oneshot through discovery's unpatched 30s ceiling and the fake's
    # 10s sleep runs to completion before add_link ever reaches the tool
    # check, making the test both slow and weakly asserted.
    monkeypatch.setattr("otto.link.manage._LINK_HOST_TIMEOUT", 0.01)
    monkeypatch.setattr("otto.link.discovery._LINK_HOST_TIMEOUT", 0.01)
    a = SlowHost("test1", "10.0.0.1")
    b = SpawnHost("test2", "10.0.0.2")
    lab = _lab(a, b)
    with pytest.raises(RuntimeError, match="timed out"):
        asyncio.run(add_link(lab, [("test1", "eth0"), ("test2", "eth0")], port=161, protocol="udp"))
