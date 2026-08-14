"""``Host.run()`` refuses a line BusyBox ash's line editor would truncate.

The ``run-command-line-length`` record in
:data:`~otto.host.userland.GAPS` says what happens without this guard: a
command over the device's line-editor buffer is SILENTLY TRUNCATED, a
different and shorter command runs, and its success is reported as the
caller's. That is the only place otto was knowingly wrong in the quiet
direction, and this module pins the conversion of it into a refusal.

**Three things are under test, and they fail in different directions.**

1. The refusal fires, up front, with the record's own evidence in it. A
   silent truncation is the failure this replaces; a refusal that says
   "command failed" without naming the buffer would be barely better.
2. The bound is on the TYPED LINE, not on the caller's command. otto wraps
   every command in BEGIN/END sentinels before typing it
   (:meth:`~otto.host.command_frame.BashFrame.frame`), so a command
   comfortably under 1022 can still frame into a line over it. A guard that
   measured ``len(cmd)`` would leave a band of commands silently truncated —
   the exact defect, narrowed rather than closed.
3. The refusal is SCOPED. ``exec()`` allocates no pty and is unaffected, and
   :class:`~otto.host.transfer.shell.ShellFileTransfer` rides ``exec()`` with
   5534-character lines as a matter of routine. A guard that reached those
   would break every file transfer to the devices this whole workstream
   exists to support.

Nothing here talks to a device. The guard is deliberately placed above
``_ensure_session``, so a manager with no transport at all is enough to drive
it — which is also the point: nothing was attempted, not even a connection.
"""

import re
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

from otto.host import userland
from otto.host.command_frame import AshFrame, BashFrame, SessionMarkers, ZephyrFrame
from otto.host.errors import UnsupportedOnUserlandError
from otto.host.session import (
    _SESSION_ID_LEN,
    SessionManager,
    ShellSession,
)
from otto.host.userland import ASH_TYPED_LINE_MAX, MEASURED_BROKEN, UNTESTED, Gap, gap_for
from otto.result import CommandResult
from otto.utils import Status

if TYPE_CHECKING:
    from otto.host.connections import ConnectionManager

SURFACE = "run-command-line-length"
"""The registry record this guard is the first product consumer of."""


class _StubSession(ShellSession):
    """Already-initialized session that records what it was asked to run.

    No transport and no handshake: this module is about what otto decides
    BEFORE it types anything, so the only interesting thing a session can do
    here is report that it was reached at all.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.ran: list[str] = []

    async def _open(self) -> None: ...
    async def _write(self, data: str) -> None: ...

    async def _read_until_pattern(self, pattern):  # pragma: no cover - never reads
        raise AssertionError("stub does not read")

    async def close(self) -> None:
        self._alive = False
        self._initialized = False

    async def _ensure_initialized(self) -> None:
        self._initialized = True
        self._alive = True

    async def run_cmd(
        self, cmd, expects=None, timeout=None, on_output=None, redact=False, write_progress=None
    ) -> CommandResult:
        self.ran.append(cmd)
        return CommandResult(status=Status.Success, value="", command=cmd, retcode=0)


def _manager(frame, *, term: str = "ssh", name: str = "bb1"):
    """A SessionManager wired to a recording stub, with *frame* as its dialect."""
    sessions: list[_StubSession] = []

    def factory() -> _StubSession:
        session = _StubSession(command_frame=frame)
        sessions.append(session)
        return session

    mgr = SessionManager(
        connections=cast("ConnectionManager", SimpleNamespace(term=term, proxy_hops=[])),
        name=name,
        command_frame=frame,
        session_factory=factory,
    )
    return mgr, sessions


def _framing_overhead(frame) -> int:
    """Characters the frame's own sentinels add to the typed line."""
    return len(frame.frame("", SessionMarkers.for_session("0" * _SESSION_ID_LEN)).rstrip("\n"))


# ===========================================================================
# The refusal itself
# ===========================================================================


class TestAnAshHostRefusesInsteadOfTruncating:
    """The headline behaviour: loud and up front, where it used to be silent."""

    @pytest.mark.asyncio
    async def test_an_overlong_command_is_refused(self) -> None:
        mgr, sessions = _manager(AshFrame())
        with pytest.raises(UnsupportedOnUserlandError):
            await mgr.run_cmd("echo " + "x" * 2000)
        assert sessions == [], (
            "the refusal must happen before a session is built: `nothing was attempted` "
            "is the one thing this exception means that the other host errors do not"
        )

    @pytest.mark.asyncio
    async def test_the_message_is_rendered_from_the_registry_record(self) -> None:
        """Not a hand-written string: the record's own reason, evidence and anchor."""
        gap = gap_for(SURFACE)
        assert gap is not None
        assert gap.status == MEASURED_BROKEN
        mgr, _ = _manager(AshFrame())
        with pytest.raises(UnsupportedOnUserlandError) as excinfo:
            await mgr.run_cmd("echo " + "x" * 2000)
        message = str(excinfo.value)
        assert SURFACE in message
        assert gap.reason in message
        assert gap.measured_on in message
        assert gap.docs_anchor in message

    @pytest.mark.asyncio
    async def test_the_message_names_the_host_the_numbers_and_the_way_out(self) -> None:
        """An operator has to be able to act on this without reading the source."""
        mgr, _ = _manager(AshFrame(), name="edge-router-3")
        with pytest.raises(UnsupportedOnUserlandError) as excinfo:
            await mgr.run_cmd("echo " + "x" * 2000)
        message = str(excinfo.value)
        assert "edge-router-3" in message
        assert str(ASH_TYPED_LINE_MAX) in message
        assert "exec()" in message, (
            "the pty-free path is the only thing a caller can actually do about this "
            "today, so the refusal has to name it"
        )
        assert str(2005) in message, "the refusal must state the command's own length"

    @pytest.mark.asyncio
    async def test_flipping_the_record_to_untested_stops_the_refusal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Proof the REGISTRY decides, not a constant that happens to sit nearby.

        This is the whole claim of the change — the table became load-bearing.
        If the guard kept refusing with the record downgraded, it would be a
        hard-coded block wearing the registry's message.
        """
        gap = gap_for(SURFACE)
        assert gap is not None
        downgraded = Gap(
            surface=gap.surface,
            status=UNTESTED,
            reason=gap.reason,
            measured_on="",
            queued_for=gap.queued_for,
        )
        monkeypatch.setattr(userland, "GAPS", [downgraded])
        mgr, sessions = _manager(AshFrame())
        result = await mgr.run_cmd("echo " + "x" * 2000)
        assert result.is_ok
        assert sessions, "no session was built, so the command was never sent"
        assert sessions[0].ran, "the command should have been sent"


# ===========================================================================
# The bound is on the line otto TYPES, not on the command the caller passed
# ===========================================================================


class TestTheBoundIsTheTypedLine:
    """``frame()`` adds sentinels before anything is typed; they count."""

    def test_the_framing_is_not_free(self) -> None:
        """Premise for the two tests below — fails loudly if framing ever goes away."""
        assert _framing_overhead(AshFrame()) > 0

    def test_the_bound_is_the_number_that_was_measured(self) -> None:
        """A LITERAL, because every other assertion here derives from the constant.

        Measured directly: with only ``ASH_TYPED_LINE_MAX`` changed from 1022
        to 1023, every other test in this module stayed green — they compute
        their boundary as ``ASH_TYPED_LINE_MAX - overhead`` and so move with
        it. That is the shape of guard that pins a mechanism and lets the
        NUMBER drift, and the number is the whole measurement here: widening
        it by one re-opens the silent truncation for a one-character band.

        The two registry assertions do the other half. They stop the constant
        and the record's prose from disagreeing, which is the realistic way
        this rots — someone re-measures, edits one of the two, and the other
        keeps telling operators the old figure.
        """
        assert ASH_TYPED_LINE_MAX == 1022, (
            "1022 is the far side's buffer, not a budget otto chose. It may only change "
            "when a new measurement against real artifacts says something else — see "
            "ASH_TYPED_LINE_MAX's own docstring, and re-measure before editing this."
        )
        gap = gap_for(SURFACE)
        assert gap is not None
        assert str(ASH_TYPED_LINE_MAX) in gap.reason, (
            "the record's reason must state the same bound the guard enforces"
        )
        assert str(ASH_TYPED_LINE_MAX + 1) in gap.measured_on, (
            "the record's measurement must state the first length that truncated, and it "
            "has to be one past the bound the guard enforces"
        )

    @pytest.mark.asyncio
    async def test_a_command_under_the_bound_is_still_refused_once_framed(self) -> None:
        """The band a ``len(cmd) > 1022`` guard would leave silently truncated.

        A 1000-character command is under the device's 1022 all by itself and
        would sail past the naive guard, but otto types it inside its own
        BEGIN/END sentinels and the line that actually reaches ash is over.
        """
        cmd = "x" * 1000
        assert len(cmd) < ASH_TYPED_LINE_MAX
        assert len(cmd) + _framing_overhead(AshFrame()) > ASH_TYPED_LINE_MAX
        mgr, sessions = _manager(AshFrame())
        with pytest.raises(UnsupportedOnUserlandError):
            await mgr.run_cmd(cmd)
        assert sessions == []

    @pytest.mark.asyncio
    async def test_the_boundary_is_exact_in_both_directions(self) -> None:
        """One character either side of the measurement, and they must differ.

        A guard that is merely "somewhere around a thousand" would pass a
        one-sided test. Both rows run against the same manager shape, so the
        only difference between green and red is the single character.
        """
        budget = ASH_TYPED_LINE_MAX - _framing_overhead(AshFrame())

        mgr, sessions = _manager(AshFrame())
        result = await mgr.run_cmd("x" * budget)
        assert result.is_ok
        assert sessions[0].ran == ["x" * budget]

        mgr, sessions = _manager(AshFrame())
        with pytest.raises(UnsupportedOnUserlandError):
            await mgr.run_cmd("x" * (budget + 1))
        assert sessions == []

    @pytest.mark.asyncio
    async def test_a_multi_line_script_is_judged_by_its_longest_line(self) -> None:
        """The editor's buffer holds ONE line; a script is many.

        Summing a here-doc's total would refuse a script every line of which
        the device handles perfectly well.
        """
        script = "\n".join(["x" * 400] * 10)
        assert len(script) > ASH_TYPED_LINE_MAX
        mgr, sessions = _manager(AshFrame())
        result = await mgr.run_cmd(script)
        assert result.is_ok
        assert sessions[0].ran == [script]

    @pytest.mark.asyncio
    async def test_one_overlong_line_inside_a_script_is_enough(self) -> None:
        script = "\n".join(["x" * 400, "y" * 2000, "x" * 400])
        mgr, _ = _manager(AshFrame())
        with pytest.raises(UnsupportedOnUserlandError):
            await mgr.run_cmd(script)

    def test_the_probe_markers_are_the_length_a_real_session_uses(self) -> None:
        """The guard sizes the framing without a session; that has to be honest.

        It renders the frame against a synthetic marker set because it runs
        before any session exists. Only the LENGTH matters, and this pins it
        against what a real session actually builds.

        Read as a DIVERGENCE check, not as a check on the number 12. Changing
        ``_SESSION_ID_LEN`` alone moves both sides together and is harmless —
        measured: with it set to 16 every test in this module stayed green,
        because the constant is genuinely shared. What it catches is one side
        going its own way: a ``ShellSession`` that slices its own literal, or a
        guard that keeps a stale copy. Then the guard's budget silently differs
        from the line otto actually types, in whichever direction the drift
        went, and nothing else here would notice.
        """
        session = _StubSession(command_frame=AshFrame())
        assert len(session._session_id) == _SESSION_ID_LEN, (
            "a real session no longer builds its id from _SESSION_ID_LEN, so the guard is "
            "sizing otto's framing against markers of a length nothing uses"
        )
        real = len(session._frame.frame("", session._markers).rstrip("\n"))
        assert real == _framing_overhead(AshFrame())


# ===========================================================================
# Scope: this dialect, and this path
# ===========================================================================


class TestTheRefusalIsScoped:
    """Refusing something that works is the expensive mistake, not the cheap one."""

    @pytest.mark.parametrize("frame", [BashFrame(), ZephyrFrame()], ids=["bash", "zephyr"])
    @pytest.mark.asyncio
    async def test_another_dialect_is_not_refused(self, frame) -> None:
        """The buffer belongs to BusyBox ash's line editor, not to every shell.

        Measured on this machine with the same pty harness that measured the
        matrix: bash carried 18437 characters and dash carried 20000+, against
        ash's 1022.
        """
        mgr, sessions = _manager(frame)
        result = await mgr.run_cmd("echo " + "x" * 2000)
        assert result.is_ok
        assert sessions[0].ran

    @pytest.mark.asyncio
    async def test_exec_is_not_refused_on_an_ash_host(self) -> None:
        """``exec()`` opens a bare channel with no pty, so no line editor sees it.

        The number is ``ShellFileTransfer``'s own chunk line, which it emits
        routinely against exactly these devices. If this ever refuses, every
        file transfer to a BusyBox target stops working.
        """
        seen: list[str] = []

        async def exec_factory(cmd: str, timeout: float) -> CommandResult:
            seen.append(cmd)
            return CommandResult(status=Status.Success, value="", command=cmd, retcode=0)

        mgr = SessionManager(
            connections=cast("ConnectionManager", SimpleNamespace(term="ssh", proxy_hops=[])),
            name="bb1",
            command_frame=AshFrame(),
            exec_factory=exec_factory,
        )
        line = "printf '%s' '" + "A" * 5500 + "' | base64 -d >> /tmp/x"
        result = await mgr.exec(line)
        assert result.is_ok
        assert seen == [line]

    @pytest.mark.asyncio
    async def test_the_pooled_shell_session_exec_path_is_not_refused_either(self) -> None:
        """A scope statement about where the guard may NOT be moved.

        ``SessionManager.exec`` has no stateless primitive on telnet (and none
        on a proxied login), so it routes through a pooled SHELL session — the
        line-edited path. ``ShellFileTransfer``'s ``exec_cmd`` is
        ``UnixHost.exec``, so pushing this guard down into
        ``ShellSession.run_cmd`` would make every transfer to a telnet or
        proxied BusyBox device refuse instead of transferring. That path stays
        unguarded deliberately; see the gap record, which now says so.
        """
        mgr, sessions = _manager(AshFrame(), term="telnet")
        line = "printf '%s' '" + "A" * 5500 + "' | base64 -d >> /tmp/x"
        result = await mgr.exec(line)
        assert result.is_ok
        assert sessions, "the pooled exec path built no session at all"
        assert sessions[0].ran == [line]

    @pytest.mark.asyncio
    async def test_a_named_session_is_not_refused_either(self) -> None:
        """The other path the guard deliberately does not cover, asserted.

        ``HostSession.run`` calls ``ShellSession.run_cmd`` directly — one layer
        BELOW ``SessionManager.run_cmd``, where the guard lives — so an
        over-long line on a named session is still silently truncated by the
        device. That is recorded as an ``OPEN`` path on the
        ``run-command-line-length`` record and rendered on the docs page, and it
        is open deliberately: this is the path ``ShellFileTransfer`` rides with
        5534-character chunk lines, so a guard here would stop every shell
        transfer to the devices the backend exists for.

        THIS TEST IS NOT A CLAIM THAT THE HOLE IS FINE. It is the thing that
        makes closing it a deliberate act: whoever finds a way to guard this
        path reds this test, and the fix is to move the record's path from
        ``OPEN`` to ``WIRED`` in the same change — not to delete the assertion.
        """
        mgr, sessions = _manager(AshFrame())
        session = await mgr.open_session("monitor")
        line = "echo " + "x" * 2000
        results = await session.run(line)
        assert results.only.is_ok
        assert sessions, "open_session built no session at all"
        assert sessions[0].ran == [line], (
            "the named session did not reach the device, so this test proves nothing "
            "about whether the guard fired"
        )


# ===========================================================================
# End to end, through the profile a lab entry actually declares
# ===========================================================================


class TestThroughAHostBuiltFromLabData:
    """`os_type: busybox` is what a user writes; the chain has to reach the guard."""

    @pytest.mark.asyncio
    async def test_host_run_refuses_on_a_busybox_profile_host(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole chain: lab entry -> profile -> ash frame -> guard, no transport.

        ``_ensure_session`` is stubbed to a hard failure rather than left
        alone, for two reasons that both matter. It ASSERTS the refusal
        precedes connecting, which is the "nothing was attempted" claim at
        product level. And it keeps the failure FAST: measured against a build
        with the guard removed, the unstubbed version instead spent 134
        seconds waiting out a real TCP connect to the address in the lab
        entry before failing for the wrong reason.
        """
        from otto.host.factory import create_host_from_dict

        host = create_host_from_dict(
            {
                "element": "bb1",
                "os_type": "busybox",
                "ip": "192.0.2.1",
                "creds": [{"login": "v", "password": "v"}],
            }
        )
        assert isinstance(host.command_frame, AshFrame)

        async def _no_connecting() -> None:
            raise AssertionError("run() opened a session for a command it had to refuse")

        monkeypatch.setattr(host._session_mgr, "_ensure_session", _no_connecting)
        with pytest.raises(UnsupportedOnUserlandError) as excinfo:
            await host.run("echo " + "x" * 2000)
        assert re.search(r"\bbb1\b", str(excinfo.value))
