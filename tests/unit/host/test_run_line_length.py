"""``Host.run()`` refuses a line the terminal would truncate on an ash host.

The ``run-command-line-length`` record in
:data:`~otto.host.userland.GAPS` says what happens without this guard: a
line over the terminal's canonical-mode buffer is SILENTLY TRUNCATED, a
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
   comfortably under the bound can still frame into a line over it. A guard that
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

import base64
import itertools
import re
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from otto.host import userland
from otto.host.command_frame import (
    AshFrame,
    BashFrame,
    ExecInput,
    ExecRestore,
    SessionMarkers,
    ZephyrFrame,
)
from otto.host.element import Element
from otto.host.errors import UnsupportedOnUserlandError
from otto.host.exec_route import ExecRoute
from otto.host.session import (
    _SESSION_ID_LEN,
    SessionManager,
    ShellSession,
    framed_line_overhead,
    refuse_if_typed_line_would_be_cut,
    render_payload,
    typed_line_budget,
    typed_line_ceiling,
    typed_line_room,
)
from otto.host.terminal_mapping import Measurement
from otto.host.transfer.shell import Base64Codec
from otto.host.unix_host import UnixHost
from otto.host.userland import (
    ASH_ECHOING_LINE_MAX,
    MEASURED_BROKEN,
    PTY_TYPED_LINE_MAX,
    UNTESTED,
    Gap,
    gap_for,
)
from otto.logger.mode import LogMode
from otto.result import CommandResult
from otto.utils import Status
from tests.unit.host._sessions import session_manager
from tests.unit.host.test_terminal_switch import _EitherConnection, _unix

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

    def __init__(self, *args, echoes: bool = False, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.ran: list[str] = []
        self._echoes = echoes

    async def open_transport(self) -> None: ...
    async def write_transport(self, data: str) -> None: ...

    async def _take_unread(self) -> str:
        return ""

    async def read_transport_until(self, pattern):  # pragma: no cover - never reads
        raise AssertionError("stub does not read")

    async def close(self) -> None:
        self._alive = False
        self._initialized = False

    async def _ensure_initialized(self) -> None:
        # What the handshake would have measured: echo off on the sunny path,
        # or still on where `stty -echo` failed (*echoes*).
        self._terminal = Measurement("onlcr", calibrated=True, echoes=self._echoes)
        self._initialized = True
        self._alive = True

    async def run_cmd(
        self,
        cmd,
        expects=None,
        timeout=None,
        on_output=None,
        redact=False,
        write_progress=None,
        exec_input=None,
    ) -> CommandResult:
        self.ran.append(cmd)
        return CommandResult(status=Status.Success, value="", command=cmd, retcode=0)


def _manager(
    frame,
    *,
    term: str = "ssh",
    name: str = "bb1",
    echoes: bool = False,
    terminal: bool = True,
    manager_terminal: bool | None = None,
):
    """A SessionManager wired to a recording stub, with *frame* as its dialect.

    Its sessions measure echo off at their handshake, or still on with *echoes*.
    They read a terminal unless *terminal* is false; the manager is told the
    same unless *manager_terminal* says otherwise.
    """
    sessions: list[_StubSession] = []

    def factory() -> _StubSession:
        session = _StubSession(command_frame=frame, echoes=echoes, terminal=terminal)
        sessions.append(session)
        return session

    mgr = session_manager(
        connections=cast("ConnectionManager", SimpleNamespace(term=term, proxy_hops=[])),
        name=name,
        command_frame=frame,
        session_factory=factory,
        types_into_terminal=terminal if manager_terminal is None else manager_terminal,
    )
    return mgr, sessions


@pytest.mark.asyncio
async def test_a_session_that_reads_a_pipe_is_not_refused_a_long_line():
    """A pipe has no line buffer: an ash host's session without a terminal types any length."""
    mgr, sessions = _manager(AshFrame(), terminal=False)
    cmd = "echo " + "x" * 5000
    await mgr.run_cmd(cmd=cmd)
    assert [session.ran for session in sessions] == [[cmd]]


@pytest.mark.asyncio
async def test_an_open_pipe_session_answers_for_itself_before_the_manager_does():
    """Once a session is open, the manager's own mode no longer refuses on its behalf."""
    mgr, sessions = _manager(AshFrame(), terminal=False, manager_terminal=True)
    await mgr.run_cmd(cmd="true")
    cmd = "echo " + "x" * 5000
    await mgr.run_cmd(cmd=cmd)
    assert sessions[0].ran == ["true", cmd]


@pytest.mark.asyncio
async def test_the_refusal_after_the_open_asks_the_session_not_the_manager():
    """A manager told "no terminal" whose session reads one still refuses once it is open."""
    mgr, sessions = _manager(AshFrame(), terminal=True, manager_terminal=False)
    with pytest.raises(UnsupportedOnUserlandError):
        await mgr.run_cmd(cmd="echo " + "x" * 5000)
    assert [session.ran for session in sessions] == [[]]


def _framing_overhead(frame) -> int:
    """Characters the frame's own sentinels add to the line the command is typed on."""
    markers = SessionMarkers.for_session(session_id="0" * _SESSION_ID_LEN)
    lines = frame.frame("CMD", m=markers).rstrip("\n").split("\n")
    return len(next(line for line in lines if "CMD" in line)) - len("CMD")


def _exec_overhead(frame, route: ExecRoute) -> int:
    """Characters the exec frame *route* types adds to the command's line."""
    typed = ExecInput(stdin_eof=True, restore=SessionManager._exec_restore_for(route))
    return framed_line_overhead(frame, exec_input=typed)


# ===========================================================================
# The refusal itself
# ===========================================================================


class TestAnAshHostRefusesInsteadOfTruncating:
    """The headline behaviour: loud and up front, where it used to be silent."""

    @pytest.mark.asyncio
    async def test_an_overlong_command_is_refused(self) -> None:
        mgr, sessions = _manager(AshFrame())
        with pytest.raises(UnsupportedOnUserlandError):
            await mgr.run_cmd(cmd="echo " + "x" * 5000)
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
            await mgr.run_cmd(cmd="echo " + "x" * 5000)
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
            await mgr.run_cmd(cmd="echo " + "x" * 5000)
        message = str(excinfo.value)
        assert "edge-router-3" in message
        assert str(PTY_TYPED_LINE_MAX) in message
        assert "exec()" in message, (
            "the pty-free path is the only thing a caller can actually do about this "
            "today, so the refusal has to name it"
        )
        assert str(5005) in message, "the refusal must state the command's own length"

    @pytest.mark.asyncio
    async def test_on_a_console_the_way_out_is_to_split_not_exec(self) -> None:
        """A console's exec types into this same session, so advising it would be false."""
        mgr, sessions = _manager(AshFrame(), term="console", name="bb1350")
        with pytest.raises(UnsupportedOnUserlandError) as excinfo:
            await mgr.exec_on_route(
                route=ExecRoute.DEFAULT_SESSION,
                cmd="echo " + "x" * 5000,
                timeout=10.0,
                log=LogMode.NORMAL,
            )
        message = str(excinfo.value)
        assert "Send it through `exec()`" not in message
        assert "split it" in message
        assert sessions == []
        assert str(5005) in message, "the refusal must state the command's own length"

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
        result = await mgr.run_cmd(cmd="echo " + "x" * 5000)
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

        Every other test in this module computes its boundary as
        ``PTY_TYPED_LINE_MAX - overhead`` and so moves with the constant. That
        is the shape of guard that pins a mechanism and lets the NUMBER drift,
        and the number is the whole measurement here: widening it by one
        re-opens the silent truncation for a one-character band.

        4095 was measured on 2026-11-13 through otto's own session on every
        BusyBox guest and route the bed has: 4095 intact, 4096 cut to 4095.
        The two registry assertions stop the constant and the record's prose
        from disagreeing, which is the realistic way this rots.
        """
        assert PTY_TYPED_LINE_MAX == 4095, (
            "4095 is the kernel's canonical-mode buffer less the newline, as measured on "
            "the BusyBox bed, not a budget otto chose. It may only change when a new "
            "measurement says something else -- see PTY_TYPED_LINE_MAX's own docstring."
        )
        gap = gap_for(SURFACE)
        assert gap is not None
        assert str(PTY_TYPED_LINE_MAX) in gap.reason, (
            "the record's reason must state the same bound the guard enforces"
        )
        assert str(PTY_TYPED_LINE_MAX + 1) in gap.measured_on, (
            "the record's measurement must state the first length that truncated, and it "
            "has to be one past the bound the guard enforces"
        )

    @pytest.mark.asyncio
    async def test_a_line_past_the_line_editors_1022_is_sent_on_ash(self) -> None:
        """BusyBox ash's 1022-character line editor does not bound what otto types.

        The editor engages only while the terminal echoes, and otto's handshake
        turns echo off before any command (measured 2026-11-13: 4095 intact
        after the handshake, 1022 after ``stty echo``). A 2000-character
        command used to be refused here; it is a working command.
        """
        mgr, sessions = _manager(AshFrame())
        result = await mgr.run_cmd(cmd="echo " + "x" * 2000)
        assert result.is_ok
        assert sessions[0].ran == ["echo " + "x" * 2000]

    @pytest.mark.asyncio
    async def test_a_command_under_the_bound_is_still_refused_once_framed(self) -> None:
        """The band a ``len(cmd) > 4095`` guard would leave silently truncated.

        A 4070-character command is under the terminal's 4095 all by itself and
        would sail past the naive guard, but otto types it inside its own
        BEGIN/END sentinels and the line that actually reaches ash is over.
        """
        cmd = "x" * 4070
        assert len(cmd) < PTY_TYPED_LINE_MAX
        assert len(cmd) + _framing_overhead(AshFrame()) > PTY_TYPED_LINE_MAX
        mgr, sessions = _manager(AshFrame())
        with pytest.raises(UnsupportedOnUserlandError):
            await mgr.run_cmd(cmd=cmd)
        assert sessions == []

    @pytest.mark.asyncio
    async def test_the_boundary_is_exact_in_both_directions(self) -> None:
        """One character either side of the measurement, and they must differ.

        A guard that is merely "somewhere around a thousand" would pass a
        one-sided test. Both rows run against the same manager shape, so the
        only difference between green and red is the single character.
        """
        budget = PTY_TYPED_LINE_MAX - _framing_overhead(AshFrame())

        mgr, sessions = _manager(AshFrame())
        result = await mgr.run_cmd(cmd="x" * budget)
        assert result.is_ok
        assert sessions[0].ran == ["x" * budget]

        mgr, sessions = _manager(AshFrame())
        with pytest.raises(UnsupportedOnUserlandError):
            await mgr.run_cmd(cmd="x" * (budget + 1))
        assert sessions == []

    @pytest.mark.asyncio
    async def test_a_multi_line_script_is_judged_by_its_longest_line(self) -> None:
        """The editor's buffer holds ONE line; a script is many.

        Summing a here-doc's total would refuse a script every line of which
        the device handles perfectly well.
        """
        script = "\n".join(["x" * 400] * 12)
        assert len(script) > PTY_TYPED_LINE_MAX
        mgr, sessions = _manager(AshFrame())
        result = await mgr.run_cmd(cmd=script)
        assert result.is_ok
        assert sessions[0].ran == [script]

    @pytest.mark.asyncio
    async def test_one_overlong_line_inside_a_script_is_enough(self) -> None:
        script = "\n".join(["x" * 400, "y" * 5000, "x" * 400])
        mgr, _ = _manager(AshFrame())
        with pytest.raises(UnsupportedOnUserlandError):
            await mgr.run_cmd(cmd=script)

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
        lines = session._frame.frame("CMD", m=session._markers).rstrip("\n").split("\n")
        real = len(next(line for line in lines if "CMD" in line)) - len("CMD")
        assert real == _framing_overhead(AshFrame())


# ===========================================================================
# Scope: this dialect, and this path
# ===========================================================================


class TestTheRefusalIsScoped:
    """Refusing something that works is the expensive mistake, not the cheap one."""

    @pytest.mark.parametrize("frame", [BashFrame(), ZephyrFrame()], ids=["bash", "zephyr"])
    @pytest.mark.asyncio
    async def test_another_dialect_is_not_refused(self, frame) -> None:
        """The refusal is measured only on ash, so it fires only on ash.

        bash reads its line in raw mode through readline, and the 2026-08-13
        pty harness carried 18437 characters into it and 20000+ into dash; a
        refusal there would decline commands that run.
        """
        mgr, sessions = _manager(frame)
        result = await mgr.run_cmd(cmd="echo " + "x" * 5000)
        assert result.is_ok
        assert sessions[0].ran

    @pytest.mark.asyncio
    async def test_exec_is_not_refused_on_an_ash_host(self, tmp_path) -> None:
        """An unset ``exec()`` on ssh runs on a session with no pty, so no line editor sees it.

        The number is ``ShellFileTransfer``'s own chunk line, which it emits
        routinely against exactly these devices. If this ever refuses, every
        file transfer to a BusyBox target stops working.
        """
        host = _unix()
        conn = _EitherConnection(tmp_path)
        host._connections.ssh = AsyncMock(return_value=conn)  # ty: ignore[invalid-assignment]
        host._session_mgr._command_frame = AshFrame()
        # BSD wc pads its count; tr leaves the bare number on every platform.
        line = "printf '%s' '" + "A" * 5500 + "' | wc -c | tr -d ' '"
        try:
            result = await host.exec(line, log=LogMode.NORMAL)
        finally:
            await host.close()
            await conn.reap()
        assert result.is_ok
        assert result.value == "5500"
        assert [asked.get("request_pty") for asked in conn.asked] == [False]

    @pytest.mark.asyncio
    async def test_a_pooled_exec_over_the_line_is_refused_before_anything_is_typed(self) -> None:
        """A pooled shell session is line-edited like ``run``'s, so it is refused like it.

        ``SessionManager.exec_route`` has no stateless primitive on telnet (and
        none on a proxied login), so ``exec`` types into a pooled SHELL session.
        The line is measured in the exec frame it is typed in, against the
        acquired session's echo fact, and refused before the session sees it.
        The advice names no ``exec()`` escape: there is none on this route.
        """
        mgr, sessions = _manager(AshFrame(), term="telnet", echoes=True)
        with pytest.raises(UnsupportedOnUserlandError) as excinfo:
            await mgr.exec_on_route(
                route=ExecRoute.POOLED_SHELL,
                cmd="a" * 5000,
                timeout=5,
                log=LogMode.QUIET,
                exec_input=ExecInput(stdin_eof=True),
            )
        assert all(s.ran == [] for s in sessions)
        message = str(excinfo.value)
        assert "Send it through `exec()`" not in message
        assert "`exec()` types into a shell session on this route too, so split it" in message

    @pytest.mark.asyncio
    async def test_a_pooled_exec_within_the_line_is_typed(self) -> None:
        """The control: a pooled exec under its budget reaches the session unchanged."""
        mgr, sessions = _manager(AshFrame(), term="telnet", echoes=True)
        budget = mgr.line_budget(route=ExecRoute.POOLED_SHELL)
        assert budget is not None
        cmd = "a" * budget
        result = await mgr.exec_on_route(
            route=ExecRoute.POOLED_SHELL,
            cmd=cmd,
            timeout=5,
            log=LogMode.QUIET,
            exec_input=ExecInput(stdin_eof=True),
        )
        assert result.is_ok
        assert sessions[0].ran == [cmd]

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
        session = await mgr.open_session(name="monitor")
        line = "echo " + "x" * 5000
        results = await session.run(line)
        assert results.only.is_ok
        assert sessions, "open_session built no session at all"
        assert sessions[0].ran == [line], (
            "the named session did not reach the device, so this test proves nothing "
            "about whether the guard fired"
        )


class TestRunAndExecShareOneFramedStep:
    """``run()`` and a pooled ``exec()`` type their framed command through one function.

    ``SessionManager._run_framed`` makes the measured refusal and stamps the
    route; each caller only chooses and releases its session.
    """

    @staticmethod
    def _spy(monkeypatch) -> list[str]:
        calls: list[str] = []
        real = SessionManager._run_framed

        async def spy(self, session, cmd, **kwargs):
            calls.append(cmd)
            return await real(self, session, cmd, **kwargs)

        monkeypatch.setattr(SessionManager, "_run_framed", spy)
        return calls

    @staticmethod
    def _raised_in_the_shared_step(excinfo) -> bool:
        names = [entry.name for entry in excinfo.traceback]
        return any(
            a == "_run_framed" and b == "refuse_if_typed_line_would_be_cut"
            for a, b in itertools.pairwise(names)
        )

    @pytest.mark.asyncio
    async def test_a_run_passes_through_it_once(self, monkeypatch) -> None:
        calls = self._spy(monkeypatch)
        mgr, sessions = _manager(AshFrame())
        result = await mgr.run_cmd(cmd="true")
        assert result.is_ok
        assert result.route == ExecRoute.DEFAULT_SESSION.value
        assert calls == ["true"]
        assert sessions[0].ran == ["true"]

    @pytest.mark.asyncio
    async def test_a_pooled_exec_passes_through_it_once(self, monkeypatch) -> None:
        calls = self._spy(monkeypatch)
        mgr, sessions = _manager(AshFrame(), term="telnet")
        result = await mgr.exec_on_route(
            route=ExecRoute.POOLED_SHELL,
            cmd="true",
            timeout=5,
            log=LogMode.QUIET,
            exec_input=ExecInput(stdin_eof=True),
        )
        assert result.is_ok
        assert result.route == ExecRoute.POOLED_SHELL.value
        assert calls == ["true"]
        assert [s.ran for s in sessions] == [["true"]]

    @pytest.mark.asyncio
    async def test_a_run_the_echo_cuts_is_refused_in_it(self) -> None:
        """Under the widest ceiling, over the echoing session's: refused once measured."""
        cmd = "a" * 2000
        assert len(cmd) + _framing_overhead(AshFrame()) < PTY_TYPED_LINE_MAX
        mgr, sessions = _manager(AshFrame(), echoes=True)
        with pytest.raises(UnsupportedOnUserlandError) as excinfo:
            await mgr.run_cmd(cmd=cmd)
        assert self._raised_in_the_shared_step(excinfo)
        assert [s.ran for s in sessions] == [[]]

    @pytest.mark.asyncio
    async def test_a_pooled_exec_the_echo_cuts_is_refused_in_it(self) -> None:
        mgr, sessions = _manager(AshFrame(), term="telnet", echoes=True)
        with pytest.raises(UnsupportedOnUserlandError) as excinfo:
            await mgr.exec_on_route(
                route=ExecRoute.POOLED_SHELL,
                cmd="a" * 2000,
                timeout=5,
                log=LogMode.QUIET,
                exec_input=ExecInput(stdin_eof=True),
            )
        assert self._raised_in_the_shared_step(excinfo)
        assert all(s.ran == [] for s in sessions)
        # The refusal typed nothing, so the keep-or-close rule kept the live
        # session on its free-list: it is not left checked out.
        idle = [h._session for pool in mgr._exec_pools.values() for h in pool]
        assert idle == sessions
        assert all(s.alive for s in sessions)


# ===========================================================================
# The other half of the same measurement: a BUDGET, for a caller that
# generates its own command lines
# ===========================================================================


def _planned_budget(host: UnixHost) -> int | None:
    """The line budget of the plan an unset ``exec()`` on *host* takes."""
    plan = host._exec_plan(user=None, needs_shell=False)
    return host._session_mgr.line_budget(route=plan.route, plain=plan.plain)


class TestTheExecLineBudget:
    """``line_budget`` on the plain exec route answers what the refusal above declines to.

    THE TWO ARE THE SAME MEASUREMENT WITH DIFFERENT VERBS, and the difference
    is who wrote the command. ``run()``'s caller wrote it and can shorten it,
    so a refusal is right. :class:`~otto.host.transfer.shell.ShellFileTransfer`
    GENERATES its command lines -- one per chunk -- so a refusal would stop
    every transfer to the devices the backend exists for. Handed a number
    instead, it emits a shorter line and the transfer works. The budget is
    measured in the exec frame the route types, so a chunk at the budget also
    passes the pooled exec's refusal.

    Nothing here talks to a device either: the budget is a function of the
    route and the frame, both of which are known before anything is connected.
    """

    def test_a_telnet_host_is_budgeted_by_the_ptys_ceiling(self) -> None:
        mgr, _ = _manager(BashFrame(), term="telnet")
        assert mgr.line_budget(route=mgr.exec_route()) == PTY_TYPED_LINE_MAX - _exec_overhead(
            BashFrame(), mgr.exec_route()
        )

    def test_an_ssh_host_has_no_budget_at_all(self) -> None:
        """``None`` is the answer that keeps the path that was never broken still.

        An unset ``exec`` runs on a session without a pty there (the plan's
        ``plain``), so no line discipline
        sees the command and the shell backend keeps sending full chunks --
        the 9000-character exec ceiling the retired dropbear rig measured (see
        ``otto.host.transfer.shell``'s module docstring, which records both the
        number and the fact that no test can re-take it) is a bound on the whole
        command, not a per-line budget, and it is not this property's business.
        """
        assert _planned_budget(_unix()) is None

    def test_a_proxied_login_is_budgeted_on_ssh_too(self) -> None:
        """The case that makes ``term`` the wrong thing to read.

        A proxied login cannot use the raw exec channel at all -- it
        authenticates as the direct cred and cannot replay the hops -- so
        ``exec`` routes through the pooled shell even here. A caller that
        predicted the budget from ``term == "telnet"`` would hand this host
        ``None`` and emit exactly the line that wedges.
        """
        mgr = session_manager(
            connections=cast("ConnectionManager", SimpleNamespace(term="ssh", proxy_hops=["v"])),
            name="bb1",
            command_frame=BashFrame(),
        )
        assert mgr.line_budget(route=mgr.exec_route()) == PTY_TYPED_LINE_MAX - _exec_overhead(
            BashFrame(), mgr.exec_route()
        )

    def test_a_subprocess_or_a_container_route_has_no_budget(self) -> None:
        """A local subprocess or a ``docker exec`` types into no shell of otto's."""
        mgr, _ = _manager(BashFrame(), term="telnet")
        assert mgr.line_budget(route=ExecRoute.LOCAL_SUBPROCESS) is None
        assert mgr.line_budget(route=ExecRoute.CONTAINER) is None
        # The control: the same manager budgets the route it types into.
        assert mgr.line_budget(route=ExecRoute.POOLED_SHELL) is not None

    def test_an_ash_host_with_no_session_yet_gets_the_echoing_ceiling(self) -> None:
        """Before any session measured echo off, an ash host plans on 1022.

        Nothing has shown the terminal stops echoing, and BusyBox ash's line
        editor cuts at 1022 while it echoes. A transfer planned this early
        only sends shorter chunks.
        """
        ash, _ = _manager(AshFrame(), term="telnet")
        assert ash.line_budget(route=ash.exec_route()) == ASH_ECHOING_LINE_MAX - _exec_overhead(
            AshFrame(), ash.exec_route()
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("echoes", "ceiling"), [(False, PTY_TYPED_LINE_MAX), (True, ASH_ECHOING_LINE_MAX)]
    )
    async def test_an_open_ash_session_sets_the_budget_by_its_echo(self, echoes, ceiling) -> None:
        """Once a session measured echo off, the kernel's 4095; still echoing, the editor's 1022."""
        ash, _ = _manager(AshFrame(), term="telnet", echoes=echoes)
        await ash.run_cmd(cmd="true")
        assert ash.line_budget(route=ash.exec_route()) == ceiling - _exec_overhead(
            AshFrame(), ash.exec_route()
        )

    def test_the_budget_is_exactly_what_frames_inside_the_ceiling(self) -> None:
        """The RELATIONSHIP, in both directions, so an off-by-one cannot pass.

        A command of exactly the budget must type as a line of exactly the
        ceiling, and one character more must be over it. Nothing here retypes
        either number: both sides are computed from the frame the manager
        actually holds, which is what makes this a guard on the arithmetic
        rather than on the constants.
        """
        frame = BashFrame()
        mgr, _ = _manager(frame, term="telnet")
        budget = mgr.line_budget(route=mgr.exec_route())
        assert budget is not None
        markers = SessionMarkers.for_session(session_id="0" * _SESSION_ID_LEN)

        route = mgr.exec_route()
        typed = ExecInput(stdin_eof=True, restore=SessionManager._exec_restore_for(route))

        def longest_typed_line(cmd: str) -> int:
            payload = render_payload(frame, cmd, m=markers, exec_input=typed, terminal=True)
            return max(len(line) for line in payload.rstrip("\n").split("\n"))

        assert longest_typed_line("x" * budget) == PTY_TYPED_LINE_MAX
        assert longest_typed_line("x" * (budget + 1)) == PTY_TYPED_LINE_MAX + 1

    def test_the_route_is_read_when_asked_rather_than_cached(self) -> None:
        """``term`` has a setter, and a proxied login can be re-targeted.

        A budget resolved once at construction would answer for the host as it
        was, which on a host whose term was switched after its transfer backend
        was built is the wrong answer in the expensive direction.
        """
        connections = SimpleNamespace(term="ssh", proxy_hops=[])
        host = _unix()
        host._session_mgr._connections = cast("ConnectionManager", connections)
        host._session_mgr._command_frame = BashFrame()
        assert _planned_budget(host) is None
        connections.term = "telnet"
        assert _planned_budget(host) == PTY_TYPED_LINE_MAX - _exec_overhead(
            BashFrame(), ExecRoute.POOLED_SHELL
        )

    def test_a_term_exec_cannot_run_on_is_not_handed_a_budget(self) -> None:
        """``exec`` raises for it before typing anything, so there is no line to budget."""
        mgr, _ = _manager(BashFrame(), term="serial")
        assert mgr.line_budget(route=mgr.exec_route()) is None


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
                "os_type": "busybox",
                "ip": "192.0.2.1",
                "creds": [{"login": "v", "password": "v"}],
            },
            element=Element(name="bb1"),
        )
        assert isinstance(host.command_frame, AshFrame)

        async def _no_connecting() -> None:
            raise AssertionError("run() opened a session for a command it had to refuse")

        monkeypatch.setattr(host._session_mgr, "_ensure_session", _no_connecting)
        with pytest.raises(UnsupportedOnUserlandError) as excinfo:
            await host.run("echo " + "x" * 5000)
        assert re.search(r"\bbb1\b", str(excinfo.value))

    @pytest.mark.asyncio
    async def test_host_run_refuses_on_a_unix_host_that_declares_the_ash_frame(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``command_frame: "ash"`` on a plain unix entry reaches the guard, and no probe is asked.

        This is the entry the bed's BusyBox guests carry. The declared frame is
        the whole answer: the session is framed as ash, and the host's
        ``Userland`` reads the same declaration, so nothing asks the device.
        """
        from otto.host.factory import create_host_from_dict

        host = create_host_from_dict(
            {
                "os_type": "unix",
                "command_frame": "ash",
                "ip": "192.0.2.1",
                "creds": [{"login": "v", "password": "v"}],
            },
            element=Element(name="bb1"),
        )
        assert isinstance(host.command_frame, AshFrame)

        async def _no_connecting() -> None:
            raise AssertionError("run() opened a session for a command it had to refuse")

        monkeypatch.setattr(host._session_mgr, "_ensure_session", _no_connecting)
        with pytest.raises(UnsupportedOnUserlandError) as excinfo:
            await host.run("echo " + "x" * 5000)
        assert re.search(r"\bbb1\b", str(excinfo.value))

        asked: list[str] = []

        async def _answers_no(cmd: str, **_kw: object) -> CommandResult:
            asked.append(cmd)
            return CommandResult(status=Status.Error, value="", retcode=1)

        monkeypatch.setattr(host, "exec", _answers_no)
        resolver = host._userland()
        await resolver.resolve()
        assert resolver.command_frame == "ash"
        assert asked, "the round issued nothing, so the next line would pass vacuously"
        assert not [c for c in asked if "BASH_VERSION" in c], asked


def test_the_framing_overhead_is_measured_for_a_frame_that_rewrites_the_command():
    """A frame may quote or escape the command (the getting-started REPL frame types
    `{cmd!r}`), so the command never appears verbatim. Measuring must still answer."""
    from otto.host.command_frame import BashFrame
    from otto.host.session import framed_line_overhead

    class _ReprFrame(BashFrame):
        def frame(self, cmd, *, m):
            return f"print({cmd!r})\n"

    assert framed_line_overhead(_ReprFrame()) == len("print('')")


def test_the_published_framing_numbers_are_the_code_s():
    """The architecture page and the gap's reason both quote the framing overhead
    and the ash line budget. Computed nowhere else, they drifted the last time the
    frame changed (74/948 survived a frame that costs 38): pin them to the code."""
    from otto.host.session import framed_line_overhead
    from tests._fixtures.paths import CHECKOUT_ROOT

    overhead = framed_line_overhead(AshFrame())
    budget = typed_line_budget(AshFrame(), echoes=False)
    assert budget == PTY_TYPED_LINE_MAX - overhead
    page = (CHECKOUT_ROOT / "docs/architecture/subsystems/busybox-support.md").read_text(
        encoding="utf-8"
    )
    gap = gap_for("run-command-line-length")
    assert gap is not None
    for text in (page, gap.reason):
        assert f"{overhead} characters" in text
        assert str(budget) in text
    # The echoing budget the page prints (984) is the code's too.
    assert f"**{typed_line_budget(AshFrame(), echoes=True)}**" in page


# ===========================================================================
# The ceiling follows whether the terminal still echoes
# ===========================================================================


class TestTheCeilingFollowsTheEcho:
    """One function decides the typed-line ceiling: the frame and the measured echo fact."""

    @pytest.mark.parametrize(
        ("frame", "echoes", "ceiling"),
        [
            (AshFrame(), True, 1022),
            (AshFrame(), False, 4095),
            (BashFrame(), True, 4095),
            (BashFrame(), False, 4095),
            (None, True, 4095),
        ],
        ids=["ash-echoing", "ash-echo-off", "bash-echoing", "bash-echo-off", "no-frame"],
    )
    def test_the_ceiling_by_frame_and_echo(self, frame, echoes, ceiling) -> None:
        """LITERALS: 1022 and 4095 are the two measurements, not budgets otto chose."""
        assert typed_line_ceiling(frame, echoes=echoes) == ceiling
        assert ASH_ECHOING_LINE_MAX == 1022

    @pytest.mark.parametrize("echoes", [False, True])
    def test_the_budget_is_the_ceiling_less_the_framing(self, echoes) -> None:
        assert typed_line_budget(AshFrame(), echoes=echoes) == typed_line_ceiling(
            AshFrame(), echoes=echoes
        ) - _framing_overhead(AshFrame())

    @pytest.mark.asyncio
    async def test_a_2000_character_run_is_sent_where_echo_is_off(self) -> None:
        mgr, sessions = _manager(AshFrame(), echoes=False)
        result = await mgr.run_cmd(cmd="echo " + "x" * 2000)
        assert result.is_ok
        assert sessions[0].ran == ["echo " + "x" * 2000]

    @pytest.mark.asyncio
    async def test_a_2000_character_run_is_refused_where_the_terminal_still_echoes(self) -> None:
        """The session opens (run() opens it anyway), measures echo on, and nothing is typed."""
        mgr, sessions = _manager(AshFrame(), echoes=True)
        with pytest.raises(UnsupportedOnUserlandError) as excinfo:
            await mgr.run_cmd(cmd="echo " + "x" * 2000)
        assert len(sessions) == 1
        assert sessions[0].ran == [], "nothing may be typed before the check"
        message = str(excinfo.value)
        assert "1022" in message
        assert "still echoes" in message
        assert str(1022 - _framing_overhead(AshFrame())) in message

    @pytest.mark.asyncio
    async def test_the_echoing_boundary_is_exact(self) -> None:
        budget = ASH_ECHOING_LINE_MAX - _framing_overhead(AshFrame())
        mgr, sessions = _manager(AshFrame(), echoes=True)
        assert (await mgr.run_cmd(cmd="x" * budget)).is_ok
        with pytest.raises(UnsupportedOnUserlandError):
            await mgr.run_cmd(cmd="x" * (budget + 1))
        assert sessions[0].ran == ["x" * budget]

    @pytest.mark.asyncio
    async def test_a_short_command_still_runs_after_a_refused_one(self) -> None:
        """The refusal types nothing, so the session it opened is still usable."""
        budget = ASH_ECHOING_LINE_MAX - _framing_overhead(AshFrame())
        mgr, sessions = _manager(AshFrame(), echoes=True)
        with pytest.raises(UnsupportedOnUserlandError):
            await mgr.run_cmd(cmd="x" * (budget + 1))
        assert (await mgr.run_cmd(cmd="echo hi")).is_ok
        assert sessions[0].ran == ["echo hi"]
        assert len(sessions) == 1

    @pytest.mark.asyncio
    async def test_a_bash_session_that_still_echoes_keeps_the_kernels_ceiling(self) -> None:
        """The 1022 cut is BusyBox ash's line editor; bash's lines are not refused."""
        mgr, sessions = _manager(BashFrame(), echoes=True)
        assert (await mgr.run_cmd(cmd="echo " + "x" * 2000)).is_ok
        assert sessions[0].ran


class TestTheMeasuredEchoOfAHost:
    """``SessionManager._measured_echoes`` reads the open sessions: echo off only if all show it."""

    @staticmethod
    def _mgr(*, default_echoes: bool, named_echoes: bool):
        def default() -> _StubSession:
            return _StubSession(command_frame=AshFrame(), echoes=default_echoes)

        def named() -> _StubSession:
            return _StubSession(command_frame=AshFrame(), echoes=named_echoes)

        return session_manager(
            connections=cast("ConnectionManager", SimpleNamespace(term="ssh", proxy_hops=[])),
            name="bb1",
            command_frame=AshFrame(),
            session_factory=default,
            named_session_factory=named,
        )

    @pytest.mark.asyncio
    async def test_a_named_echoing_session_beside_an_echo_off_default_makes_it_true(self) -> None:
        mgr = self._mgr(default_echoes=False, named_echoes=True)
        await mgr.run_cmd(cmd="echo hi")
        assert mgr._measured_echoes() is False
        await mgr.open_session(name="aux")
        assert mgr._measured_echoes() is True

    @pytest.mark.asyncio
    async def test_a_dead_echoing_session_is_ignored(self) -> None:
        mgr = self._mgr(default_echoes=False, named_echoes=True)
        await mgr.run_cmd(cmd="echo hi")
        aux = await mgr.open_session(name="aux")
        assert mgr._measured_echoes() is True
        await aux.close()
        assert mgr._measured_echoes() is False


class TestEachEntryIsMeasuredAsTheLineItTypes:
    """A console's ``exec`` types the exec frame, whose opening is longer than ``run``'s.

    The refusal must measure the payload the session will type
    (:func:`~otto.host.session.render_payload`), so the same command can fit
    ``run``'s budget and still be refused for ``exec``.
    """

    @staticmethod
    def _budgets() -> list[int]:
        from otto.host.session import _BEFORE_ANY_SESSION_ECHOES, framed_line_overhead

        frame = AshFrame()
        ceiling = typed_line_ceiling(frame, echoes=_BEFORE_ANY_SESSION_ECHOES)
        run_budget = ceiling - framed_line_overhead(frame)
        # The exec_input the default session types: with that route's restore.
        typed = SessionManager._with_restore_for(
            ExecRoute.DEFAULT_SESSION, ExecInput(stdin_eof=True)
        )
        exec_budget = ceiling - framed_line_overhead(frame, exec_input=typed)
        assert exec_budget < run_budget, "the exec frame's typed line is the longer one"
        return [run_budget, exec_budget]

    async def _drive(self, cmd: str, *, as_exec: bool):
        mgr, sessions = _manager(AshFrame(), term="console", name="bb1350")
        if as_exec:
            await mgr.exec_on_route(
                route=ExecRoute.DEFAULT_SESSION,
                cmd=cmd,
                timeout=10.0,
                log=LogMode.NORMAL,
                exec_input=ExecInput(stdin_eof=True),
            )
        else:
            await mgr.run_cmd(cmd=cmd)
        return sessions

    @pytest.mark.asyncio
    async def test_a_command_between_the_budgets_runs_but_is_refused_as_an_exec(self) -> None:
        run_budget, exec_budget = self._budgets()
        cmd = "echo " + "x" * (exec_budget + 1 - len("echo "))
        assert len(cmd) <= run_budget
        sessions = await self._drive(cmd, as_exec=False)
        assert sessions
        assert sessions[0].ran == [cmd]
        with pytest.raises(UnsupportedOnUserlandError) as excinfo:
            await self._drive(cmd, as_exec=True)
        assert str(PTY_TYPED_LINE_MAX) in str(excinfo.value)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("as_exec", [False, True])
    async def test_a_command_within_the_exec_budget_passes_both(self, as_exec: bool) -> None:
        _, exec_budget = self._budgets()
        cmd = "echo " + "x" * (exec_budget - len("echo "))
        sessions = await self._drive(cmd, as_exec=as_exec)
        assert sessions
        assert sessions[0].ran == [cmd]


# ===========================================================================
# One renderer for what is typed, what is refused and what is budgeted
# ===========================================================================

_EXEC_INPUTS = [
    None,
    ExecInput(stdin_eof=True),
    ExecInput(stdin_eof=False, restore=ExecRestore.START),
]


@pytest.mark.parametrize(
    ("frame", "echoes"), [(BashFrame(), False), (AshFrame(), True), (AshFrame(), False)]
)
@pytest.mark.parametrize("exec_input", _EXEC_INPUTS)
def test_the_command_at_the_budget_fits_and_one_more_is_refused(frame, echoes, exec_input):
    budget = typed_line_budget(frame, echoes=echoes, exec_input=exec_input)
    refuse_if_typed_line_would_be_cut(frame, "a" * budget, echoes=echoes, exec_input=exec_input)
    if isinstance(frame, AshFrame):
        with pytest.raises(UnsupportedOnUserlandError):
            refuse_if_typed_line_would_be_cut(
                frame, "a" * (budget + 1), echoes=echoes, exec_input=exec_input
            )


def test_the_refusal_measures_the_exec_frame_not_a_run_frame():
    n = typed_line_budget(AshFrame(), echoes=True)
    refuse_if_typed_line_would_be_cut(AshFrame(), "a" * n, echoes=True)
    with pytest.raises(UnsupportedOnUserlandError) as excinfo:
        refuse_if_typed_line_would_be_cut(
            AshFrame(),
            "a" * n,
            echoes=True,
            exec_input=ExecInput(stdin_eof=True, restore=ExecRestore.START),
        )
    overhead = framed_line_overhead(
        AshFrame(), exec_input=ExecInput(stdin_eof=True, restore=ExecRestore.START)
    )
    assert f"framing ({overhead} characters)" in str(excinfo.value)


def test_the_synthetic_render_measures_what_a_session_writes():
    real = SessionMarkers.for_session(session_id=uuid4().hex[:_SESSION_ID_LEN])
    synthetic = SessionMarkers.for_session(session_id="0" * _SESSION_ID_LEN)
    for exec_input in (
        None,
        ExecInput(stdin_eof=True),
        ExecInput(stdin_eof=True, restore=ExecRestore.START),
    ):

        def lens(m, exec_input=exec_input):
            payload = render_payload(AshFrame(), "a\nbb", m=m, exec_input=exec_input, terminal=True)
            return [len(x) for x in payload.split("\n")]

        assert lens(real) == lens(synthetic)


def test_a_multiline_command_is_measured_per_physical_line():
    x = ExecInput(stdin_eof=True)
    refuse_if_typed_line_would_be_cut(AshFrame(), "x\n" + "a" * 900, echoes=True, exec_input=x)
    with pytest.raises(UnsupportedOnUserlandError):
        refuse_if_typed_line_would_be_cut(AshFrame(), "a" * 1100 + "\nx", echoes=True, exec_input=x)


_EXEC_LINES = [ExecInput(stdin_eof=True), ExecInput(stdin_eof=True, restore=ExecRestore.START)]
"""A pooled exec's frame, which types a command as written, and a console's, which quotes it."""


@pytest.mark.parametrize("exec_input", _EXEC_LINES)
@pytest.mark.parametrize("echoes", [True, False])
def test_every_shell_transfer_chunk_fits_its_exec_line_and_one_quantum_more_does_not(
    echoes, exec_input
):
    codec = Base64Codec("-d")

    def room(cmd):
        return typed_line_room(AshFrame(), cmd, echoes=echoes, exec_input=exec_input)

    n = codec._fitted_chunk_bytes("'/tmp/x'", room)
    cmd = codec._chunk_command(base64.b64encode(b"A" * n).decode(), ">>", "'/tmp/x'")
    refuse_if_typed_line_would_be_cut(AshFrame(), cmd, echoes=echoes, exec_input=exec_input)
    over = codec._chunk_command(base64.b64encode(b"A" * (n + 3)).decode(), ">>", "'/tmp/x'")
    with pytest.raises(UnsupportedOnUserlandError):
        refuse_if_typed_line_would_be_cut(AshFrame(), over, echoes=echoes, exec_input=exec_input)


@pytest.mark.parametrize("exec_input", _EXEC_LINES)
@pytest.mark.parametrize("echoes", [True, False])
def test_a_quoted_command_at_its_room_fits_and_one_more_is_refused(echoes, exec_input):
    """The room is measured on the rendered line, so the command's own quotes are counted."""
    empty = "printf '%s' 'it''s' ''"
    n = typed_line_room(AshFrame(), empty, echoes=echoes, exec_input=exec_input)
    fits = f"printf '%s' 'it''s' '{'a' * n}'"
    refuse_if_typed_line_would_be_cut(AshFrame(), fits, echoes=echoes, exec_input=exec_input)
    with pytest.raises(UnsupportedOnUserlandError):
        refuse_if_typed_line_would_be_cut(
            AshFrame(), fits + "a", echoes=echoes, exec_input=exec_input
        )


@pytest.mark.parametrize("exec_input", [None, *_EXEC_LINES])
@pytest.mark.parametrize("echoes", [True, False])
def test_the_budget_is_the_room_of_an_empty_command(echoes, exec_input):
    assert typed_line_budget(AshFrame(), echoes=echoes, exec_input=exec_input) == typed_line_room(
        AshFrame(), "", echoes=echoes, exec_input=exec_input
    )


def test_a_console_exec_s_quotes_cost_what_its_quoting_types():
    """shlex quoting types a single quote as five characters: ``'"'"'``."""
    start = ExecInput(stdin_eof=True, restore=ExecRestore.START)
    plain = typed_line_room(AshFrame(), "aaa", echoes=False, exec_input=start)
    quoted = typed_line_room(AshFrame(), "a'a", echoes=False, exec_input=start)
    assert plain - quoted == 4
    pooled = ExecInput(stdin_eof=True)
    assert typed_line_room(AshFrame(), "aaa", echoes=False, exec_input=pooled) == typed_line_room(
        AshFrame(), "a'a", echoes=False, exec_input=pooled
    )
