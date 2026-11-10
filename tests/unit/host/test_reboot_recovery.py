"""Reboot pipeline hardening: truthful probes, two-phase wait, liveness gate.

Chaos plan 6. Everything here is deterministic: scripted probe sequences,
``poll_interval=0`` (pure scheduling yields), the patchable ``ssh_connect``
dial seam — no real network, no wall-clock waits, and NEVER a real host.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import otto.host.connections as connections_mod
from otto.host.element import Element
from otto.host.local_host import LocalHost
from otto.host.login_proxy import Cred
from otto.host.options import RebootOptions
from otto.host.unix_host import UnixHost
from otto.logger.mode import LogMode
from otto.result import CommandResult, Result
from otto.utils import Status


def _unix_host() -> UnixHost:
    return UnixHost(
        ip="10.0.0.1",
        element=Element(name="box"),
        creds=[Cred(login="u", password="p")],
        log=LogMode.QUIET,
    )


def _ok(cmd: str = "true") -> CommandResult:
    return CommandResult(status=Status.Success, value="", command=cmd, retcode=0)


def _timed_out(cmd: str = "true") -> CommandResult:
    return CommandResult(
        status=Status.Error, value="timed out", command=cmd, retcode=-1, timed_out=True
    )


@pytest.mark.asyncio
async def test_soft_reboot_tolerates_connection_drop(monkeypatch):
    """Issuing `reboot` races the transport teardown: a raise from run() is
    indistinguishable from the host obeying quickly, so it must be tolerated
    — the (later) down-wait is the loud check for 'never actually took'."""
    h = _unix_host()
    monkeypatch.setattr(
        h, "run", AsyncMock(side_effect=ConnectionResetError("dropped mid-command"))
    )
    result = await h._soft_reboot()
    assert result.status == Status.Success


@pytest.mark.asyncio
async def test_reboot_rebuilds_connections_before_waiting(monkeypatch):
    """The cached transports are dead the moment the reboot is issued; every
    later probe must dial fresh. Order matters: issue, then rebuild, then wait."""
    h = _unix_host()
    order: "list[str]" = []

    def side_effect():
        order.append("issue")
        return Result(status=Status.Success)

    monkeypatch.setattr(h, "_soft_reboot", AsyncMock(side_effect=side_effect))
    monkeypatch.setattr(h, "rebuild_connections", lambda: order.append("rebuild"))

    async def up(self, *, timeout=10.0, fresh=False):
        order.append("probe")
        return True

    monkeypatch.setattr(type(h), "is_reachable", up)
    await h.reboot(wait=False)
    assert order == ["issue", "rebuild"]  # no wait requested: no probe, but rebuild still ran


@pytest.mark.asyncio
async def test_reboot_wait_does_not_trust_the_cached_connection(monkeypatch):
    """THE headline regression: with a cached (dead) ssh connection and a
    dial seam that refuses every fresh attempt, reboot(wait=True) must FAIL —
    today it vacuously succeeds because verify_connection() reads the cache."""
    h = _unix_host()
    monkeypatch.setattr(h, "_soft_reboot", AsyncMock(return_value=Result(status=Status.Success)))
    h._connections._ssh_conn = MagicMock()  # the dead cached connection
    monkeypatch.setattr(
        connections_mod, "ssh_connect", AsyncMock(side_effect=ConnectionRefusedError("booting"))
    )
    result = await h.reboot(wait=True, timeout=0.05, poll_interval=0)
    assert result.status == Status.Failed


def _scripted_reachable(monkeypatch, host, sequence: "list[bool]") -> None:
    """Drive is_reachable from a finite script; the last value repeats."""
    seq = list(sequence)

    async def fake(self, *, timeout=10.0, fresh=False):
        return seq.pop(0) if len(seq) > 1 else seq[0]

    monkeypatch.setattr(type(host), "is_reachable", fake)


def _reboot_ready_local(monkeypatch) -> LocalHost:
    """A LocalHost whose _soft_reboot is a recorder — drives BaseHost.reboot's
    orchestration without any real family behavior."""
    h = LocalHost()
    monkeypatch.setattr(h, "_soft_reboot", AsyncMock(return_value=Result(status=Status.Success)))
    return h


@pytest.mark.asyncio
async def test_reboot_wait_requires_down_before_up(monkeypatch):
    """Old OS still answering (True), then down (False), then back (True):
    the wait must observe the down transition and then succeed."""
    h = _reboot_ready_local(monkeypatch)
    _scripted_reachable(monkeypatch, h, [True, False, True])
    result = await h.reboot(wait=True, timeout=5.0, down_timeout=5.0, poll_interval=0)
    assert result.status == Status.Success


@pytest.mark.asyncio
async def test_reboot_that_never_takes_fails_loudly(monkeypatch):
    """Host never goes down: the up-before-down race of the old code turned
    this into instant false success; now it must fail naming the down phase."""
    h = _reboot_ready_local(monkeypatch)
    _scripted_reachable(monkeypatch, h, [True])  # reachable forever
    result = await h.reboot(wait=True, timeout=0.05, down_timeout=0.05, poll_interval=0)
    assert result.status == Status.Failed
    assert "never went down" in (result.msg or "")


@pytest.mark.asyncio
async def test_reboot_that_never_returns_fails_loudly(monkeypatch):
    """Host goes down and stays down: the failure must name the up phase."""
    h = _reboot_ready_local(monkeypatch)
    _scripted_reachable(monkeypatch, h, [False])  # down immediately, forever
    result = await h.reboot(wait=True, timeout=0.05, down_timeout=5.0, poll_interval=0)
    assert result.status == Status.Failed
    assert "did not become reachable" in (result.msg or "")


@pytest.mark.asyncio
async def test_reboot_down_timeout_clamped_by_overall_timeout(monkeypatch):
    """When down_timeout > timeout, the down phase is bounded by timeout.
    The failure message must report the enforced (clamped) bound, not the
    nominal down_timeout parameter."""
    h = _reboot_ready_local(monkeypatch)
    _scripted_reachable(monkeypatch, h, [True])  # reachable forever
    # down_timeout=5.0 (nominal), timeout=0.05 (overall), so down phase actually
    # waits at most 0.05s. Host never goes down, so it times out at the enforced bound.
    result = await h.reboot(wait=True, timeout=0.05, down_timeout=5.0, poll_interval=0)
    assert result.status == Status.Failed
    # The message must report 0.05 (the enforced bound), not 5.0 (the nominal down_timeout)
    assert "never went down within 0.05s" in (result.msg or "")


@pytest.mark.asyncio
async def test_reboot_down_timeout_zero_skips_down_phase(monkeypatch):
    """down_timeout<=0 is the documented opt-out for hosts whose probe target
    never dies (LocalHost's is_reachable is hard-coded True): skip straight
    to the up wait instead of failing a down phase that cannot be observed."""
    h = _reboot_ready_local(monkeypatch)
    _scripted_reachable(monkeypatch, h, [True])  # reachable forever
    result = await h.reboot(wait=True, timeout=5.0, down_timeout=0, poll_interval=0)
    assert result.status == Status.Success


@pytest.mark.asyncio
async def test_unix_recovery_survives_accept_then_stall(monkeypatch):
    """The false start: sshd accepts (reachable=True) but the shell stalls.
    Recovery must retry the command probe until it answers — two stalled
    probes then a clean round-trip is a recovery, not a failure."""
    h = _unix_host()
    monkeypatch.setattr(h, "_soft_reboot", AsyncMock(return_value=Result(status=Status.Success)))
    _scripted_reachable(monkeypatch, h, [True, False, True])
    monkeypatch.setattr(h, "exec", AsyncMock(side_effect=[_timed_out(), _timed_out(), _ok()]))
    result = await h.reboot(wait=True, timeout=5.0, down_timeout=5.0, poll_interval=0)
    assert result.status == Status.Success
    assert h.exec.await_count == 3


@pytest.mark.asyncio
async def test_unix_recovery_fails_when_shell_never_answers(monkeypatch):
    """Reachable forever, shell never answers: the failure must say so —
    this is the accept-then-stall pitfall made loud.

    The down and up phases are monkeypatched to return immediately (the
    test_power.py pattern) so the whole 0.05s budget is spent inside the
    recovery gate itself — otherwise this test would need to traverse both
    live waits before the shared deadline expires, and the asserted failure
    phase could flip under scheduler load."""
    h = _unix_host()
    monkeypatch.setattr(h, "_soft_reboot", AsyncMock(return_value=Result(status=Status.Success)))

    async def immediately_true(self, timeout, interval=2.0):
        return True

    monkeypatch.setattr(type(h), "wait_until_down", immediately_true)
    monkeypatch.setattr(type(h), "wait_until_up", immediately_true)
    monkeypatch.setattr(h, "exec", AsyncMock(return_value=_timed_out()))
    result = await h.reboot(wait=True, timeout=0.05, down_timeout=0.05, poll_interval=0)
    assert result.status == Status.Failed
    assert "shell never answered" in (result.msg or "")


@pytest.mark.asyncio
async def test_unix_recovery_gate_always_probes_at_least_once(monkeypatch):
    """A deadline already in the past must not fail the gate unprobed: one
    clean round-trip is a recovery, no matter what the clock says."""
    h = _unix_host()
    monkeypatch.setattr(h, "exec", AsyncMock(return_value=_ok()))
    past = asyncio.get_running_loop().time() - 1.0
    assert await h._confirm_recovered(past, 0) is True
    assert h.exec.await_count == 1


@pytest.mark.asyncio
async def test_unix_recovery_probe_tolerates_raising_exec(monkeypatch):
    """A probe that RAISES (connection reset mid-handshake) is 'not booted
    yet', never an error: the loop retries and later succeeds."""
    h = _unix_host()
    monkeypatch.setattr(h, "_soft_reboot", AsyncMock(return_value=Result(status=Status.Success)))
    _scripted_reachable(monkeypatch, h, [True, False, True])
    monkeypatch.setattr(h, "exec", AsyncMock(side_effect=[ConnectionResetError("reset"), _ok()]))
    result = await h.reboot(wait=True, timeout=5.0, down_timeout=5.0, poll_interval=0)
    assert result.status == Status.Success


@pytest.mark.asyncio
async def test_default_recovery_gate_keeps_reachability_semantics(monkeypatch):
    """Families without a shell probe (LocalHost stands in) recover on
    reachability alone — the base hook must not demand an exec round-trip."""
    h = _reboot_ready_local(monkeypatch)
    _scripted_reachable(monkeypatch, h, [True, False, True])
    result = await h.reboot(wait=True, timeout=5.0, down_timeout=5.0, poll_interval=0)
    assert result.status == Status.Success


def test_reboot_defaults_come_from_the_hosts_reboot_options(monkeypatch):
    host = _reboot_ready_local(monkeypatch)
    host.reboot_options = RebootOptions(timeout=0.3, down_timeout=0.1, poll_interval=0.01)
    _scripted_reachable(monkeypatch, host, [True])  # never goes down
    r = asyncio.run(host.reboot(wait=True))
    assert r.status is Status.Failed
    assert "never went down within 0.1s" in r.msg


def test_an_explicit_argument_is_the_final_word_over_the_table(monkeypatch):
    host = _reboot_ready_local(monkeypatch)
    host.reboot_options = RebootOptions(timeout=0.3, down_timeout=10.0, poll_interval=0.01)
    _scripted_reachable(monkeypatch, host, [True])
    r = asyncio.run(host.reboot(wait=True, down_timeout=0.05))
    assert "never went down within 0.05s" in r.msg


def test_an_explicit_argument_obeys_the_same_policy_as_lab_data(monkeypatch):
    host = _reboot_ready_local(monkeypatch)
    with pytest.raises(ValueError, match="down_timeout"):
        asyncio.run(host.reboot(wait=True, down_timeout=-1))
    with pytest.raises(ValueError, match="timeout"):
        asyncio.run(host.reboot(wait=True, timeout=0))


# --- #435: the down phase probes through a throwaway connection, never the cache ---


def test_wait_until_down_never_trusts_a_cached_transport(monkeypatch):
    """#435: the first probe during a shutdown hold logs in; every later probe must dial."""
    host = _unix_host()
    dials: "list[int]" = []

    async def fake_connect(*a, **kw):
        dials.append(1)
        if len(dials) == 1:
            return MagicMock()  # the hold: one login succeeds
        raise ConnectionRefusedError  # then sshd is gone

    monkeypatch.setattr(connections_mod, "ssh_connect", AsyncMock(side_effect=fake_connect))
    assert asyncio.run(host.wait_until_down(1.0, interval=0.01)) is True
    assert len(dials) >= 2


def _count_builds(monkeypatch, host) -> "tuple[list[object], list[object]]":
    """Wrap the host class's manager factory so each throwaway's build and close is recorded."""
    built: "list[object]" = []
    closed: "list[object]" = []
    real_build = type(host)._build_connections

    def counting_build(self):
        m = real_build(self)
        built.append(m)
        orig = m.close

        async def close():
            closed.append(m)
            await orig()

        monkeypatch.setattr(m, "close", close)
        return m

    monkeypatch.setattr(type(host), "_build_connections", counting_build)
    return built, closed


def test_a_fresh_probe_closes_its_throwaway_manager_every_poll(monkeypatch):
    host = _unix_host()
    built, closed = _count_builds(monkeypatch, host)
    monkeypatch.setattr(
        connections_mod, "ssh_connect", AsyncMock(side_effect=ConnectionRefusedError)
    )
    asyncio.run(host.wait_until_down(0.05, interval=0.01))
    assert built
    assert built == closed
    assert host._connections not in built


class _RecordingHop:
    """A hop transport stand-in: hands out a tunnel object and counts its closes."""

    def __init__(self) -> None:
        self.closes = 0

    async def get_tunnel(self):
        return MagicMock()

    async def close(self) -> None:
        self.closes += 1


@pytest.mark.parametrize("outcome", ["succeeds", "refused", "hangs"])
def test_a_fresh_probe_through_a_hop_closes_hop_and_target_every_poll(monkeypatch, outcome):
    hops: "list[_RecordingHop]" = []

    def build_hop(self):
        hop = _RecordingHop()
        hops.append(hop)
        return hop

    monkeypatch.setattr(UnixHost, "_build_hop_transport", build_hop)
    host = UnixHost(
        ip="10.0.0.1",
        element=Element(name="box"),
        creds=[Cred(login="u", password="p")],
        log=LogMode.QUIET,
        hop="h",
    )
    live_hop = hops[0]
    built, closed = _count_builds(monkeypatch, host)

    async def dial(*a, **kw):
        if outcome == "succeeds":
            return MagicMock()
        if outcome == "refused":
            raise ConnectionRefusedError
        await asyncio.sleep(60)
        return None

    monkeypatch.setattr(connections_mod, "ssh_connect", AsyncMock(side_effect=dial))

    async def polls() -> "list[bool]":
        return [await host.is_reachable(timeout=0.05, fresh=True) for _ in range(3)]

    results = asyncio.run(polls())
    assert results == [outcome == "succeeds"] * 3
    assert len(built) == 3
    assert built == closed
    probe_hops = hops[1:]
    assert len(probe_hops) == 3
    assert [h.closes for h in probe_hops] == [1, 1, 1]
    assert live_hop.closes == 0


def test_a_stalled_close_does_not_defeat_the_down_window(monkeypatch):
    import otto.host.remote_host as remote_host_mod

    host = _unix_host()
    real_build = type(host)._build_connections

    def stalled_build(self):
        m = real_build(self)

        async def close():
            await asyncio.sleep(60)

        monkeypatch.setattr(m, "close", close)
        return m

    monkeypatch.setattr(type(host), "_build_connections", stalled_build)
    monkeypatch.setattr(remote_host_mod, "_FRESH_CLOSE_S", 0.01)
    monkeypatch.setattr(
        connections_mod, "ssh_connect", AsyncMock(side_effect=ConnectionRefusedError)
    )

    async def probe() -> bool:
        return await asyncio.wait_for(host.is_reachable(timeout=0.05, fresh=True), 0.1)

    assert asyncio.run(probe()) is False


def test_a_fresh_probe_leaves_the_live_caches_alone(monkeypatch):
    host = _unix_host()
    sentinel = MagicMock()
    host._connections._ssh_conn = sentinel
    monkeypatch.setattr(connections_mod, "ssh_connect", AsyncMock(return_value=MagicMock()))
    assert asyncio.run(host.is_reachable(fresh=True)) is True
    assert host._connections._ssh_conn is sentinel


def test_a_console_term_releases_the_line_before_a_fresh_dial(monkeypatch):
    host = UnixHost(
        ip="10.0.0.1",
        element=Element(name="box"),
        creds=[Cred(login="u", password="p")],
        log=LogMode.QUIET,
        valid_terms=["console"],
        term="console",
    )
    order: "list[str]" = []

    async def close(self):
        order.append("close")

    def build(self):
        order.append("build")
        m = MagicMock()
        m.console = AsyncMock()
        m.close = AsyncMock()
        return m

    monkeypatch.setattr(UnixHost, "close", close)
    monkeypatch.setattr(UnixHost, "_build_connections", build)
    assert asyncio.run(host.is_reachable(fresh=True)) is True
    assert order == ["close", "build"]


def test_embedded_hosts_build_a_throwaway_manager_too():
    from otto.host.command_frame import ZephyrFrame
    from otto.host.connections import ConnectionManager
    from otto.host.embedded_host import EmbeddedHost

    h = EmbeddedHost(
        ip="192.0.2.1",
        element=Element(name="zephyr37_fat"),
        log=LogMode.QUIET,
        command_frame=ZephyrFrame(),
    )
    m = h._build_connections()
    assert isinstance(m, ConnectionManager)
    assert m is not h._connections
