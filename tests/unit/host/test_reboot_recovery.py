"""Reboot pipeline hardening: truthful probes, two-phase wait, liveness gate.

Chaos plan 6. Everything here is deterministic: scripted probe sequences,
``poll_interval=0`` (pure scheduling yields), the patchable ``ssh_connect``
dial seam — no real network, no wall-clock waits, and NEVER a real host.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import otto.host.connections as connections_mod
from otto.host.local_host import LocalHost
from otto.host.login_proxy import Cred
from otto.host.unix_host import UnixHost
from otto.logger.mode import LogMode
from otto.result import CommandResult, Result
from otto.utils import Status


def _unix_host() -> UnixHost:
    return UnixHost(
        ip="10.0.0.1", element="box", creds=[Cred(login="u", password="p")], log=LogMode.QUIET
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
        return Result(Status.Success)

    monkeypatch.setattr(h, "_soft_reboot", AsyncMock(side_effect=side_effect))
    monkeypatch.setattr(h, "rebuild_connections", lambda: order.append("rebuild"))

    async def up(self, timeout=10.0):
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
    monkeypatch.setattr(h, "_soft_reboot", AsyncMock(return_value=Result(Status.Success)))
    h._connections._ssh_conn = MagicMock()  # the dead cached connection
    monkeypatch.setattr(
        connections_mod, "ssh_connect", AsyncMock(side_effect=ConnectionRefusedError("booting"))
    )
    result = await h.reboot(wait=True, timeout=0.05, poll_interval=0)
    assert result.status == Status.Failed


def _scripted_reachable(monkeypatch, host, sequence: "list[bool]") -> None:
    """Drive is_reachable from a finite script; the last value repeats."""
    seq = list(sequence)

    async def fake(self, timeout=10.0):
        return seq.pop(0) if len(seq) > 1 else seq[0]

    monkeypatch.setattr(type(host), "is_reachable", fake)


def _reboot_ready_local(monkeypatch) -> LocalHost:
    """A LocalHost whose _soft_reboot is a recorder — drives BaseHost.reboot's
    orchestration without any real family behavior."""
    h = LocalHost()
    monkeypatch.setattr(h, "_soft_reboot", AsyncMock(return_value=Result(Status.Success)))
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
    monkeypatch.setattr(h, "_soft_reboot", AsyncMock(return_value=Result(Status.Success)))
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
    monkeypatch.setattr(h, "_soft_reboot", AsyncMock(return_value=Result(Status.Success)))

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
    monkeypatch.setattr(h, "_soft_reboot", AsyncMock(return_value=Result(Status.Success)))
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
