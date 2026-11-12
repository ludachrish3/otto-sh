"""Reboot pipeline hardening: truthful probes, two-phase wait, liveness gate.

Chaos plan 6. Everything here is deterministic: scripted probe sequences,
``poll_interval=0`` (pure scheduling yields), the patchable ``ssh_connect``
dial seam — no real network, no wall-clock waits, and NEVER a real host.
"""

import asyncio
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

import otto.host.connections as connections_mod
from otto.host.console import ConsoleState
from otto.host.element import Element
from otto.host.embedded_host import ZephyrHost
from otto.host.errors import ConsoleError
from otto.host.host import PROBE_TIMEOUT_S, _ConsoleReset, _DownWait, _ProbeAnswer, _UpWait
from otto.host.local_host import LocalHost
from otto.host.login_proxy import Cred
from otto.host.options import ConsoleOptions, RebootOptions, SshOptions
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
    """Drive every probe from a finite script; the last value repeats.

    The down phase asks ``_probe_down`` and the up phase ``_probe_up`` (and
    ``is_reachable`` for any direct caller); all draw from the one script, in
    call order.
    """
    seq = list(sequence)

    def next_answer() -> bool:
        return seq.pop(0) if len(seq) > 1 else seq[0]

    async def fake(self, *, timeout=10.0, fresh=False):
        return next_answer()

    async def fake_probe(self, timeout):
        return _ProbeAnswer(reached=next_answer())

    monkeypatch.setattr(type(host), "is_reachable", fake)
    monkeypatch.setattr(type(host), "_probe_up", fake_probe)
    monkeypatch.setattr(type(host), "_probe_down", fake_probe)


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
    logout = AsyncMock()
    monkeypatch.setattr(h, "logout", logout)
    _scripted_reachable(monkeypatch, h, [True])  # reachable forever
    result = await h.reboot(wait=True, timeout=0.05, down_timeout=0.05, poll_interval=0)
    assert result.status == Status.Failed
    assert "never went down" in (result.msg or "")
    logout.assert_not_awaited()  # only "did not take" on a console logs the line out


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

    async def immediately_down(self, timeout, *, interval):
        return _DownWait(down=True, line_dropped=True, first_shell_prompt=None)

    async def immediately_up(self, timeout, *, interval):
        return _UpWait(reached=True, always_at_shell=False, last=None)

    monkeypatch.setattr(type(h), "_wait_until_down", immediately_down)
    monkeypatch.setattr(type(h), "_wait_until_up", immediately_up)
    monkeypatch.setattr(h, "exec", AsyncMock(return_value=_timed_out()))
    logout = AsyncMock()
    monkeypatch.setattr(h, "logout", logout)
    result = await h.reboot(wait=True, timeout=0.05, down_timeout=0.05, poll_interval=0)
    assert result.status == Status.Failed
    assert "shell never answered" in (result.msg or "")
    logout.assert_not_awaited()


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
    """A hop transport stand-in: hands out a tunnel object and counts its closes and aborts."""

    def __init__(self) -> None:
        self.closes = 0
        self.aborts = 0

    async def get_tunnel(self):
        return MagicMock()

    async def close(self) -> None:
        self.closes += 1

    def abort(self) -> None:
        self.aborts += 1


def _hopped(monkeypatch, family: str) -> "tuple[UnixHost | ZephyrHost, list[_RecordingHop]]":
    """A *family* host behind hop ``h``, every hop transport it builds recorded."""
    hops: "list[_RecordingHop]" = []

    def build_hop(self, hop_id=None):
        hop = _RecordingHop()
        hops.append(hop)
        return hop

    cls = UnixHost if family == "unix" else ZephyrHost
    monkeypatch.setattr(cls, "_build_hop_transport", build_hop)
    kwargs = {"creds": [Cred(login="u", password="p")]} if family == "unix" else {}
    host = cls(ip="10.0.0.1", element=Element(name="box"), log=LogMode.QUIET, hop="h", **kwargs)
    return host, hops


@pytest.mark.parametrize("family", ["unix", "embedded"])
def test_a_rebuild_releases_the_dropped_managers_hops(monkeypatch, family):
    """A rebuild drops the old manager unclosed, but not its hops (#1023).

    A hop reaches a box a reboot does not touch, so it is still open; left
    to the dropped manager, its ssh connection and forward listener leak.
    Both families' rebuilds go through ``ConnectionManager.abandon``.
    """
    host, hops = _hopped(monkeypatch, family)
    old = host._connections
    console_hop = _RecordingHop()
    old._console_endpoint = connections_mod.ConsoleEndpoint(server_ip="10.0.0.2", hop=console_hop)

    host.rebuild_connections()

    assert host._connections is not old
    assert [hops[0].aborts, console_hop.aborts] == [1, 1]
    assert [h.aborts for h in hops[1:]] == [0] * len(hops[1:])  # the new manager's hop is live
    assert len(hops) == 2


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
        m.connect_allowance = MagicMock(return_value=0.0)
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


# -- a console line that still has a shell (#451) ------------------------------
#
# A reboot that never took leaves the abandoned client's shell running on the
# line, so every dial finds output but no login prompt and the real
# ``ConsoleClient.login_sequence`` refuses with "already logged in". Shutdown
# and boot output on a console server that keeps its link through the reboot
# look exactly the same, so a probe reads that refusal as unreachable; only a
# wait that ENDS with the line still at a shell says the reboot did not take,
# and only then does otto log the line out (through ``logout``, the path
# ``otto host <id> logout`` takes). The ambiguous ending types nothing: the
# line may be at an initramfs shell or a bootloader prompt.


def _console_host() -> UnixHost:
    return UnixHost(
        ip="10.0.0.1",
        element=Element(name="box"),
        creds=[Cred(login="u", password="p")],
        log=LogMode.QUIET,
        valid_terms=["console"],
        term="console",
        console_options=ConsoleOptions(server="srv", port=4001),
    )


class _DialClock:
    """The running loop's clock, advanced one second per dial and by nothing else.

    A reboot wait then ends after a known number of dials, whatever the
    machine's speed: with a down phase that ends on its first dial, a
    ``timeout`` of ``k + 0.5`` gives the up phase exactly ``k`` dials.
    """

    def __init__(self, monkeypatch) -> None:
        self.now = 0.0
        monkeypatch.setattr(asyncio.get_running_loop(), "time", lambda: self.now)


def _scripted_line(
    monkeypatch, script: "list[str]", *, clock: "_DialClock | None" = None
) -> "list[str]":
    """Every manager the host builds dials one line that answers from *script*.

    One entry per dial, the last repeating: ``"shell"`` is output with no
    login prompt ending at ``u@box:~$`` (a running shell, or shutdown/boot
    messages), refused by the real ``login_sequence``, and ``"initramfs"``
    and ``"uboot"`` likewise end at another console's prompt, ``"boot1"``
    and ``"boot2"`` at two different boot-log lines, ``"blank"`` with only
    whitespace; ``"busy"`` is the server hanging up; ``"hang"``
    never answers (the probe's bound cuts it); ``"login"`` is the device up
    at its login prompt (the client is returned logged in). Returns the
    answers given, in order. Each dial advances *clock*, if given.
    """
    from otto.host.console import ConsoleClient, ConsoleState

    seq = list(script)
    answered: "list[str]" = []

    shown = {
        "shell": "booting...\r\nu@box:~$ ",
        "initramfs": "booting...\r\n(initramfs) ",
        "uboot": "booting...\r\n=> ",
        "boot1": "[  OK  ] Started A.\r\n",
        "boot2": "[  OK  ] Started B.\r\n",
        "blank": "\r\n\x00",
    }
    tail = [""]

    async def at_a_shell(self, login_re, password_re):
        return ConsoleState.LOGGED_IN, tail[0]

    monkeypatch.setattr(ConsoleClient, "_nudge_and_classify", at_a_shell)

    async def console(self):
        answer = seq.pop(0) if len(seq) > 1 else seq[0]
        answered.append(answer)
        if clock is not None:
            clock.now += 1.0
        if answer == "busy":
            raise ConsoleError("box: console srv:4001 (dial=ssh) is busy")
        if answer == "hang":
            await asyncio.Event().wait()
        if answer == "login":
            return MagicMock()
        tail[0] = shown[answer]
        client = ConsoleClient(
            host="localhost",
            port=4001,
            user="u",
            password="p",
            options=ConsoleOptions(
                server="srv", port=4001, login_prompt="login: ?$", password_prompt="assword: ?$"
            ),
            name="box",
            server="srv",
        )
        await client.login_sequence()
        return client

    def build(self):
        m = MagicMock()
        m.console = types.MethodType(console, m)
        m.close = AsyncMock()
        m.connect_allowance = MagicMock(return_value=0.0)
        return m

    async def close(self):
        pass

    monkeypatch.setattr(UnixHost, "_build_connections", build)
    monkeypatch.setattr(UnixHost, "close", close)
    return answered


def test_the_down_probe_reads_a_line_at_a_shell_as_down(monkeypatch):
    host = _console_host()
    answered = _scripted_line(monkeypatch, ["shell"])
    assert asyncio.run(host.is_reachable(fresh=True)) is False
    assert answered == ["shell"]


def test_the_live_probe_reads_a_line_at_a_shell_as_unreachable(monkeypatch):
    host = _console_host()
    _scripted_line(monkeypatch, ["shell"])
    host.rebuild_connections()  # the live manager is a scripted one too
    assert asyncio.run(host.is_reachable()) is False


def test_a_probe_names_the_shell_it_met_and_nothing_else(monkeypatch):
    host = _console_host()
    _scripted_line(monkeypatch, ["shell", "busy", "shell", "busy"])
    host.rebuild_connections()
    shell = _ProbeAnswer(reached=False, shell_seen=_SHELL_SEEN, shell_prompt="u@box:~$")
    assert asyncio.run(host._probe_up(PROBE_TIMEOUT_S)) == shell
    assert asyncio.run(host._probe_up(PROBE_TIMEOUT_S)) == _ProbeAnswer(reached=False)
    assert asyncio.run(host._probe_down(PROBE_TIMEOUT_S)) == shell
    assert asyncio.run(host._probe_down(PROBE_TIMEOUT_S)) == _ProbeAnswer(reached=False)


def _rebooting(monkeypatch, host, logout: "AsyncMock | None" = None) -> AsyncMock:
    """Issue a no-op reboot; returns the ``run_logout`` spy (*logout*, or one that succeeds).

    ``run_logout`` is the hook only :meth:`~otto.host.host.BaseHost.logout`
    calls, so an awaited spy proves the reboot went through ``logout``.
    """
    monkeypatch.setattr(host, "_soft_reboot", AsyncMock(return_value=Result(status=Status.Success)))
    if logout is None:
        logout = AsyncMock(return_value=_reset(already_at_login=False))
    monkeypatch.setattr(host, "run_logout", logout)
    return logout


_SHELL_SEEN = repr("booting...\r\nu@box:~$ ")
_RESTORED = "box: console srv:4001 reset: login prompt restored"
_ALREADY = "box: console srv:4001 already at login prompt"


def _reset(*, already_at_login: bool) -> _ConsoleReset:
    """What the console host's own ``_reset_outcome`` answers (checked below)."""
    return _console_host()._reset_outcome(
        ConsoleState.AT_LOGIN if already_at_login else ConsoleState.LOGGED_IN
    )


def test_the_reset_outcome_says_whether_the_line_was_already_at_login():
    assert _reset(already_at_login=False) == _ConsoleReset(
        status=Status.Success, value=_RESTORED, already_at_login=False
    )
    assert _reset(already_at_login=True) == _ConsoleReset(
        status=Status.Success, value=_ALREADY, already_at_login=True
    )


def _not_reachable(timeout: float) -> str:
    return f"'box' did not become reachable within {timeout}s after reboot"


def _did_not_take(timeout: float, seen: str = _SHELL_SEEN, outcome: str | None = None) -> str:
    """The "did not take" message; *outcome* is what came of the logout (logged out by default)."""
    if outcome is None:
        outcome = f"; otto logged the console line out ({_RESTORED})"
    return (
        f"'box' was still at a logged-in shell {timeout}s after the reboot (last seen: {seen}) "
        f"— the reboot likely did not take{outcome}"
    )


# (script, down_timeout, timeout, message). On the dial clock a down phase
# that ends on dial d leaves the up phase k dials when timeout - d is in
# (k - 1, k]: so 2.5 is one down dial and two up dials, 2.0 with no down
# phase two up dials, 1.0 one.
_VERDICTS = {
    # The shell the reboot was typed into, unchanged throughout: did not take.
    "same-prompt": (["shell", "shell", "shell"], 5.0, 2.5, _did_not_take(2.5)),
    "same-prompt-no-down-phase": (["shell", "shell"], 0, 2.0, _did_not_take(2.0)),
    # Several down-phase answers before it reads down; the first shell decides.
    "down-phase-answers-first": (
        ["login", "login", "shell", "shell", "shell"],
        5.0,
        4.5,
        _did_not_take(4.5),
    ),
    # Whitespace only throughout: the empty prompt is unchanged too.
    "blank-throughout": (["blank", "blank", "blank"], 5.0, 2.5, _did_not_take(2.5, repr("\r\n"))),
    # It went down, and came up into another console: a reboot that took.
    "initramfs": (["shell", "initramfs", "initramfs"], 5.0, 2.5, _not_reachable(2.5)),
    "bootloader": (["shell", "uboot", "uboot"], 5.0, 2.5, _not_reachable(2.5)),
    # The prompt changed between up-phase probes: a log still streaming.
    "up-prompt-changed": (["shell", "shell", "boot1"], 5.0, 2.5, _not_reachable(2.5)),
    # It ends on the first down-phase prompt, but changed in between.
    "up-prompt-changed-and-back": (["shell", "boot1", "shell"], 5.0, 2.5, _not_reachable(2.5)),
    # The line dropped in the down phase, then came up chatty: it took.
    "down-phase-saw-the-line-drop": (["busy", "shell", "shell"], 5.0, 2.5, _not_reachable(2.5)),
    # Some up-phase probe saw something other than a shell: it may well have taken.
    "up-busy": (["shell", "busy", "busy"], 5.0, 2.5, _not_reachable(2.5)),
    "up-shell-then-busy": (["shell", "shell", "busy"], 5.0, 2.5, _not_reachable(2.5)),
    "up-busy-then-shell": (["shell", "busy", "shell"], 5.0, 2.5, _not_reachable(2.5)),
    # No down phase: the up phase must see one prompt at least twice.
    "no-down-phase-up-busy-then-shell": (["busy", "shell"], 0, 2.0, _not_reachable(2.0)),
    "no-down-phase-boot-lines-change": (["boot1", "boot2"], 0, 2.0, _not_reachable(2.0)),
    "no-down-phase-one-probe": (["shell"], 0, 1.0, _not_reachable(1.0)),
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("script", "down_timeout", "timeout", "msg"), list(_VERDICTS.values()), ids=list(_VERDICTS)
)
async def test_what_the_waits_saw_decides_the_verdict(
    monkeypatch, script, down_timeout, timeout, msg
):
    """ "Did not take" only on an unchanged shell throughout, and only then a logout.

    The dial clock makes the counts exact (see ``_VERDICTS``): every entry
    of *script* is dialled, no more. Every "did not become reachable"
    leaves the line alone.
    """
    clock = _DialClock(monkeypatch)
    host = _console_host()
    logout = _rebooting(monkeypatch, host)
    answered = _scripted_line(monkeypatch, script, clock=clock)
    result = await host.reboot(
        wait=True, timeout=timeout, down_timeout=down_timeout, poll_interval=0
    )
    assert answered == script
    assert result.status == Status.Failed
    assert result.msg == msg, result.msg
    assert logout.await_count == (0 if msg.startswith("'box' did not become reachable") else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("logout", "reason"),
    [
        pytest.param(
            AsyncMock(side_effect=ConsoleError("box: console srv:4001 (dial=ssh) is busy")),
            "box: console srv:4001 (dial=ssh) is busy",
            id="raises",
        ),
        pytest.param(AsyncMock(side_effect=TimeoutError()), "TimeoutError", id="raises-bare"),
        pytest.param(
            AsyncMock(return_value=Result(status=Status.Error, msg="reset gave up")),
            "reset gave up",
            id="not-ok",
        ),
    ],
)
async def test_a_logout_that_fails_keeps_the_verdict_and_the_hint(
    monkeypatch, caplog, logout, reason
):
    """The logout failing never masks "did not take": it says so and keeps the manual hint."""
    clock = _DialClock(monkeypatch)
    host = _console_host()
    _rebooting(monkeypatch, host, logout)
    _scripted_line(monkeypatch, ["shell", "shell", "shell"], clock=clock)
    with caplog.at_level("WARNING", logger="otto"):
        result = await host.reboot(wait=True, timeout=2.5, down_timeout=5.0, poll_interval=0)
    assert result.status == Status.Failed
    assert result.msg == _did_not_take(
        2.5,
        outcome=(
            f"; otto could not log the console line out ({reason}); "
            "run `otto host box logout` to log it out"
        ),
    ), result.msg
    logout.assert_awaited_once()
    assert any("could not log the console line out" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_line_already_at_login_when_otto_logs_it_out_may_have_rebooted_late(monkeypatch):
    """The reset found ``login:``: the device got there after the last probe.

    The reboot still fails (it did not come up within the timeout), but the
    message says it may have taken late rather than that it did not take.
    """
    clock = _DialClock(monkeypatch)
    host = _console_host()
    logout = _rebooting(monkeypatch, host, AsyncMock(return_value=_reset(already_at_login=True)))
    _scripted_line(monkeypatch, ["shell", "shell", "shell"], clock=clock)
    result = await host.reboot(wait=True, timeout=2.5, down_timeout=5.0, poll_interval=0)
    assert result.status == Status.Failed
    assert result.msg == (
        f"'box' was still at a logged-in shell 2.5s after the reboot (last seen: {_SHELL_SEEN}), "
        "but the console line was already back at its login prompt when otto went to log it "
        f"out ({_ALREADY}) — the reboot may have taken late"
    ), result.msg
    logout.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_console_reboot_that_never_went_down_leaves_the_line_alone(monkeypatch):
    """ "Never went down" is the down phase's verdict: no logout, whatever the line shows."""
    clock = _DialClock(monkeypatch)
    host = _console_host()
    logout = _rebooting(monkeypatch, host)
    _scripted_line(monkeypatch, ["login"], clock=clock)
    result = await host.reboot(wait=True, timeout=5.0, down_timeout=2.5, poll_interval=0)
    assert (
        result.msg
        == "'box' never went down within 2.5s of the reboot — the reboot likely did not take"
    )
    logout.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_logout_that_is_cancelled_is_not_swallowed(monkeypatch):
    """Cancellation is not a logout failure: it propagates out of the reboot."""
    clock = _DialClock(monkeypatch)
    host = _console_host()
    _rebooting(monkeypatch, host, AsyncMock(side_effect=asyncio.CancelledError()))
    _scripted_line(monkeypatch, ["shell", "shell", "shell"], clock=clock)
    with pytest.raises(asyncio.CancelledError):
        await host.reboot(wait=True, timeout=2.5, down_timeout=5.0, poll_interval=0)


@pytest.mark.asyncio
async def test_a_console_reboot_that_took_through_chatty_output_succeeds(monkeypatch):
    """Boot output reads like a shell, then the prompt returns: a reboot that took."""
    host = _console_host()
    logout = _rebooting(monkeypatch, host)
    answered = _scripted_line(monkeypatch, ["shell", "shell", "shell", "login"])
    monkeypatch.setattr(host, "_confirm_recovered", AsyncMock(return_value=True))
    result = await host.reboot(wait=True, timeout=5.0, down_timeout=5.0, poll_interval=0)
    assert result.status == Status.Success, result.msg
    assert answered == ["shell", "shell", "shell", "login"]
    logout.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_up_probe_cut_by_its_bound_is_not_a_shell(monkeypatch):
    """A probe the hard bound cuts says nothing about the line: plain "did not become reachable"."""
    host = _console_host()
    logout = _rebooting(monkeypatch, host)
    _scripted_line(monkeypatch, ["shell", "hang"])
    monkeypatch.setattr(UnixHost, "probe_bound", lambda self, timeout=PROBE_TIMEOUT_S: 0.01)
    result = await host.reboot(wait=True, timeout=0.05, down_timeout=0.05, poll_interval=0)
    assert result.msg == "'box' did not become reachable within 0.05s after reboot"
    logout.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("term", ["ssh", "telnet"])
async def test_an_ssh_or_telnet_host_that_never_comes_back_keeps_the_plain_message(
    monkeypatch, term
):
    """Off the console term nothing changed: a failed up phase is "did not become reachable"."""
    host = UnixHost(
        ip="10.0.0.1",
        element=Element(name="box"),
        creds=[Cred(login="u", password="p")],
        log=LogMode.QUIET,
        valid_terms=["ssh", "telnet"],
        term=term,
    )
    logout = _rebooting(monkeypatch, host)

    async def refused(self, conns):
        raise ConnectionRefusedError("booting")

    monkeypatch.setattr(UnixHost, "_probe_with", refused)
    result = await host.reboot(wait=True, timeout=0.05, down_timeout=0.05, poll_interval=0)
    assert result.msg == "'box' did not become reachable within 0.05s after reboot"
    logout.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_probe_bound_that_cannot_be_sized_falls_back_and_reads_as_unreachable(
    monkeypatch,
):
    """``probe_bound`` is total: a failure sizing it returns the unlifted bound.

    The probe then meets the same failure and reads as unreachable, as
    ``is_reachable`` reads every failure: the up wait never succeeds.
    """
    host = _console_host()
    _scripted_line(monkeypatch, ["shell"])

    from otto.host.loop_owner import HostLoopError

    loop = asyncio.get_running_loop()

    def owned_elsewhere(self):
        raise HostLoopError("box", owner=loop, caller=loop)

    monkeypatch.setattr(UnixHost, "_live_connections", owned_elsewhere)
    assert host.probe_bound() == PROBE_TIMEOUT_S
    assert host.probe_bound(timeout=3.0) == 3.0
    assert await host.is_reachable() is False
    assert await host.wait_until_up(0.05, interval=0) is False


def test_any_other_failure_sizing_the_probe_bound_propagates(monkeypatch):
    """Only a loop claim falls back; a bug in sizing the allowance must not hide as a probe."""
    host = _console_host()

    def broken(self):
        raise ValueError("allowance arithmetic is wrong")

    monkeypatch.setattr(type(host._connections), "connect_allowance", broken)
    with pytest.raises(ValueError, match="allowance arithmetic"):
        host.probe_bound()


@pytest.mark.asyncio
async def test_both_waits_bound_each_probe_by_the_hosts_probe_bound(monkeypatch):
    """``is_reachable``'s timeout is hard; the waits lift it through ``probe_bound``."""
    host = _console_host()
    bounds: "list[tuple[float, bool]]" = []

    async def fresh(self, timeout):
        bounds.append((timeout, True))
        return OSError("down")  # down at once

    async def live(self, timeout):
        bounds.append((timeout, False))
        # up at once

    monkeypatch.setattr(UnixHost, "_probe_fresh", fresh)
    monkeypatch.setattr(UnixHost, "_probe_live", live)
    monkeypatch.setattr(UnixHost, "probe_bound", lambda self, timeout=PROBE_TIMEOUT_S: 42.0)
    assert await host.wait_until_down(1.0, interval=0) is True
    assert await host.wait_until_up(1.0, interval=0) is True
    assert bounds == [(42.0, True), (42.0, False)]


def test_only_a_console_lifts_the_probe_bound_to_its_connect_allowance():
    """A console needs its full login to answer; ssh keeps the hard cut even when slow."""
    console = _console_host()
    allowance = console._connections.connect_allowance()
    assert allowance > PROBE_TIMEOUT_S
    assert console.probe_bound() == allowance
    slow_ssh = UnixHost(
        ip="10.0.0.1",
        element=Element(name="box"),
        creds=[Cred(login="u", password="p")],
        log=LogMode.QUIET,
        ssh_options=SshOptions(connect_timeout=60),
    )
    assert slow_ssh._connections.connect_allowance() == 60
    assert slow_ssh.probe_bound() == PROBE_TIMEOUT_S
