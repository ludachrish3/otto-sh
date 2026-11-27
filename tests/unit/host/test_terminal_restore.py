"""A terminal that outlives otto's session gets back the settings otto found.

On a console (a serial line) the bash handshake's ``stty -echo`` and ``stty
tab0`` would stay after otto logs out. The first handshake there saves the
settings (``stty -g``) on the line otto already sends, and the session's one
restore step types them back at the login bridge's hand-back and before the
session closes. An ssh or telnet login's pty goes away with the session, so
nothing is saved or restored there. See :mod:`otto.host.terminal_mapping`.
"""

import asyncio
import logging
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telnetlib3.stream_reader import TelnetReader

from otto.host import interact
from otto.host.command_frame import (
    READY_SUFFIX,
    RECOVER_SUFFIX,
    BashFrame,
    CommandFrame,
    RawFrame,
    SessionMarkers,
    WorkShellLines,
    ZephyrFrame,
    handshake_prefix,
    history_prefix,
)
from otto.host.errors import ConsoleError
from otto.host.login_proxy import Cred, _identity_probe
from otto.host.options import ConsoleOptions
from otto.host.session import (
    BridgeSetup,
    LocalSession,
    SshSession,
    TelnetSession,
    _WorkShell,
)
from otto.host.session_setup import SESSION_SETUPS, SessionSetup, register_session_setup
from otto.host.telnet import ReadGate
from otto.host.terminal_mapping import save_settings_command, saved_settings
from tests._fixtures.dialect_shell import CHANGED_SETTINGS, FOUND_SETTINGS, DialectShell
from tests._fixtures.fake_shell import probe_marker
from tests._fixtures.interactive_shell import (
    SHELLS,
    TAIL_MARKERS,
    interactive_shell,
    probe_answer,
    render_probe,
)
from tests.unit.host._session_feed import identity_handshake_reply, zephyr_handshake_reply
from tests.unit.host._telnet_fakes import stream_client
from tests.unit.host.test_interact_session_setup import _Console

RESTORE_FOUND = f"stty {FOUND_SETTINGS}"


def _saves(text: str) -> bool:
    """Whether *text* carries the console save of the terminal's settings, under any session's tag.

    Not the exec record every handshake also snapshots (``__otto_base``).
    """
    save = save_settings_command("TAG")
    head, tail = save.split("TAG")
    return re.search(re.escape(head) + r"\S+" + re.escape(tail), text) is not None


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------


_M = SessionMarkers.for_session(session_id="cafef00d")
_TAG = _M.saved
_TYPED = BashFrame().handshake(_M, prefix=BashFrame().save_terminal(_M))
_AFTER = "\r\nX\r\r\nY\r\nZ\rW\tT\r\n__OTTO_cafef00d_READY__\r\n"


def test_the_saved_record_is_the_first_one_behind_the_sessions_tag():
    """A resend runs the save again after the first run changed the settings: a later record."""
    reply = (
        f"{_TYPED}\r\n{_TAG}{FOUND_SETTINGS}\r\n{_AFTER}"
        f"{_TYPED}\r\n{_TAG}{CHANGED_SETTINGS}\r\n{_AFTER}"
    )
    assert saved_settings(reply, _TAG) == FOUND_SETTINGS


@pytest.mark.parametrize(
    "before",
    [
        "eth0: link up, hwaddr 52:54:00:12:34:56\r\n52:54:00:12:34:56\r\n",  # a MAC on a line
        "fe80:0:0:0:5054:ff:fe12:3456\r\n",  # a bare IPv6 address
        # An earlier session's tagged restore record, left unread on the line.
        f"__OTTO_0ddba11_SAVED__{CHANGED_SETTINGS}\r\n",
        f"{_TYPED}",  # the typed line's echo, echo still on: `$(` follows the tag
    ],
    ids=["mac", "ipv6", "stale-session", "typed-echo"],
)
def test_only_this_handshakes_own_record_is_saved(before):
    """Hex-and-colon lines already on the console, or the typed echo, never pass for the record."""
    assert saved_settings(f"{before}\r\n{_AFTER}", _TAG) is None
    reply = f"{before}\r\n{_TAG}{FOUND_SETTINGS}\r\n{_AFTER}"
    assert saved_settings(reply, _TAG) == FOUND_SETTINGS


@pytest.mark.parametrize(
    "reply",
    [
        f"{_TYPED}\r\n{_TAG}\r\n{_AFTER}",  # no stty: the save printed its tag alone
        f"stty: invalid argument '-g'\r\n{_TAG}\r\n",  # a stty without -g, had its error leaked
        f"{_TAG}500:5\r\n",  # too short to be a record
        f"{_TAG}{FOUND_SETTINGS};rm\r\n",  # not wholly hex and colons to the line end
    ],
)
def test_a_reply_without_a_record_saves_nothing(reply):
    assert saved_settings(reply, _TAG) is None


def test_bash_restores_the_record_in_one_step_and_zephyr_saves_nothing():
    frame = BashFrame()
    assert frame.save_terminal(_M) == f"{save_settings_command(_TAG)}; "
    assert save_settings_command(_TAG) == f"echo {_TAG}$(2>/dev/null stty -g)"
    assert frame.restore_terminal(FOUND_SETTINGS) == f"2>/dev/null {RESTORE_FOUND} || :"
    zephyr = ZephyrFrame()
    assert zephyr.save_terminal(_M) == ""
    assert zephyr.restore_terminal(FOUND_SETTINGS) == zephyr.restore_interactive()


# ---------------------------------------------------------------------------
# Which sessions' terminals outlive them: the one decision
# ---------------------------------------------------------------------------


def _console_client(
    events: list[str] | None = None, *, owes_logout: bool = True
) -> SimpleNamespace:
    """A console client stand-in whose close is logged in *events*.

    ``logout``, or ``close`` where it owes no logout.
    """

    async def close() -> None:
        if events is not None:
            events.append("logout" if client.owes_logout else "close")

    # These tests close the session "before it logs out": the client owes one,
    # unless the test asks for a console whose shell outlives the session.
    client = SimpleNamespace(
        logged_in=False,
        owes_logout=owes_logout,
        read_gate=ReadGate(),
        close=close,
        options=ConsoleOptions(),
        work_shell_refused=lambda *, started: ConsoleError(f"work shell refused {started=}"),
    )
    return client


def test_only_a_session_on_a_console_client_outlives_its_terminal():
    reader, writer = TelnetReader(), object()
    on_console = TelnetSession(reader=reader, writer=writer, console_client=_console_client())
    on_telnet = TelnetSession(reader=reader, writer=writer, client=stream_client(reader, writer))
    assert on_console.terminal_outlives_session
    assert not on_telnet.terminal_outlives_session
    assert not SshSession(conn=None).terminal_outlives_session
    assert not LocalSession().terminal_outlives_session


def test_only_a_console_that_owes_no_logout_outlives_its_shell():
    """The shell outlives the session where the console's close does not end it with a logout."""
    reader, writer = TelnetReader(), object()
    on_telnet = TelnetSession(reader=reader, writer=writer, client=stream_client(reader, writer))
    assert not on_telnet.shell_outlives_session
    assert not SshSession(conn=None).shell_outlives_session
    assert not LocalSession().shell_outlives_session
    client = _console_client()
    on_console = TelnetSession(reader=reader, writer=writer, console_client=client)
    assert not on_console.shell_outlives_session
    client.owes_logout = False  # read live, never cached
    assert on_console.shell_outlives_session
    client.owes_logout = True
    assert not on_console.shell_outlives_session


# ---------------------------------------------------------------------------
# A session over a telnetlib3 reader: the handshake saves, the close restores
# ---------------------------------------------------------------------------


class _Line:
    """The writer end of a fake shell on a line: it answers the handshake, records the rest.

    The handshake is answered with the record its save prints (*record*, or
    nothing for a target without ``stty``; a later save prints
    *later_record*), then the identity reply. A work shell's entry proof is
    answered with the identity reply and the readback, ``1`` where the shell
    *owns* the work mark. With *work_handshake* false, no handshake after a
    proved entry is answered: the work shell never reaches a prompt. Every
    write is logged in *events*, as the console's logout is.
    """

    def __init__(
        self,
        reader: TelnetReader,
        events: list[str],
        record: str | None,
        *,
        owns: bool = True,
        later_record: str | None = None,
        work_handshake: bool = True,
    ) -> None:
        self._reader = reader
        self.events = events
        self._record = record
        self._owns = owns
        self._later_record = later_record
        self._work_handshake = work_handshake
        self._proved = False
        self._saves = 0

    def write(self, data: bytes) -> None:
        text = data.decode().replace("\r", "\n")
        self.events.append(text)
        marker = probe_marker(text.lstrip(" "))  # a line typed into the person's shell leads
        if marker is not None and marker.endswith(READY_SUFFIX):
            if self._proved and not self._work_handshake:
                return
            tag = re.search(r"(__OTTO_\w+?_SAVED__)\$\(", text)
            record = self._record if self._saves == 0 else self._later_record
            self._saves += tag is not None
            saved = f"{tag.group(1)}{record}\r\n" if record and tag else ""
            self._reader.feed_data((saved + identity_handshake_reply(marker)).encode())
        elif marker is not None and marker.endswith(RECOVER_SUFFIX) and "__otto_as_w" in text:
            readback = "1" if self._owns else ""
            self._proved = self._owns
            self._reader.feed_data(f"\n{marker}0__u__{readback}.\n".encode())
        elif ready := re.search(r"__OTTO_\w+?_READY__", text):  # Zephyr types READY whole
            self._reader.feed_data(zephyr_handshake_reply(ready.group(0)).encode())


def _session_on(
    *,
    console: bool,
    record: str | None,
    owes_logout: bool = True,
    owns: bool = True,
    later_record: str | None = None,
    shell_history: bool = True,
    command_frame: CommandFrame | None = None,
    work_handshake: bool = True,
    init_timeout: float | None = None,
) -> tuple[TelnetSession, list[str]]:
    events: list[str] = []
    reader = TelnetReader()
    writer = _Line(
        reader,
        events,
        record,
        owns=owns,
        later_record=later_record,
        work_handshake=work_handshake,
    )
    if console:
        session = TelnetSession(
            reader=reader,
            writer=writer,
            console_client=_console_client(events, owes_logout=owes_logout),
            shell_history=shell_history,
            command_frame=command_frame,
            init_timeout=init_timeout,
        )
    else:
        session = TelnetSession(
            reader=reader,
            writer=writer,
            client=stream_client(reader, writer),
            command_frame=command_frame,
        )
        session._client = SimpleNamespace(close=AsyncMock())  # the release is not under test
    return session, events


@pytest.mark.asyncio
async def test_a_console_session_restores_the_found_settings_before_it_logs_out():
    session, events = _session_on(console=True, record=FOUND_SETTINGS)

    await session._ensure_initialized()
    assert _saves(events[0])
    await session.close()

    assert events[-1] == "logout"
    restore = events[-2]
    assert RESTORE_FOUND in restore
    assert session.markers.begin in restore  # one framed command, its reply not awaited
    assert session._saved_terminal is None


@pytest.mark.asyncio
async def test_a_telnet_session_neither_saves_nor_restores():
    session, events = _session_on(console=False, record=FOUND_SETTINGS)

    await session._ensure_initialized()
    handshakes = list(events)
    await session.close()

    assert not any(_saves(e) for e in handshakes)
    assert events == handshakes  # the close wrote nothing


@pytest.mark.asyncio
async def test_a_console_without_stty_reaches_ready_and_closes_without_a_restore():
    """#953: the save prints nothing, so nothing is saved, and the close goes straight to logout."""
    session, events = _session_on(console=True, record=None)

    await session._ensure_initialized()
    assert session.alive
    assert session._saved_terminal is None
    handshakes = list(events)
    await session.close()

    assert events == [*handshakes, "logout"]


# ---------------------------------------------------------------------------
# A console whose shell outlives the session opens in a work shell
# ---------------------------------------------------------------------------


_FRAME = BashFrame()


def _markers_of(probe: str) -> SessionMarkers:
    """The markers a probe line was rendered with, read from its echo word."""
    marker = probe_marker(probe.lstrip(" "))
    assert marker is not None, probe
    found = re.fullmatch(r"__OTTO_(\w+?)_(?:READY|RECOVER)__", marker)
    assert found is not None, marker
    return SessionMarkers.for_session(session_id=found.group(1))


@pytest.mark.parametrize("shell_history", [False, True], ids=["suppressed", "kept"])
@pytest.mark.asyncio
async def test_an_outliving_console_opens_in_a_work_shell(shell_history):
    """Line 1 into the person's shell, the start line, one entry proof, the work shell handshake."""
    session, events = _session_on(
        console=True, record=FOUND_SETTINGS, owes_logout=False, shell_history=shell_history
    )

    await session._ensure_initialized()

    lines = _FRAME.work_shell(prefix=history_prefix(_FRAME, shell_history))
    quiet, script = _FRAME.quiet_history(), _FRAME.script_parsing()
    first, start, proof, handshake = events
    assert first.startswith(lines.lead)
    assert _saves(first)
    assert script not in first
    assert quiet not in first
    assert start == lines.lead + lines.start
    assert proof.startswith(lines.lead)
    assert proof == _identity_probe(_markers_of(proof), "", work=lines)
    assert script not in proof
    if shell_history:
        assert quiet not in proof
        assert "then :; fi" in proof
    else:
        assert f"then {quiet}:; fi" in proof
    m = session.markers
    prefix = handshake_prefix(_FRAME, m, shell_history=shell_history, save_terminal=False)
    assert handshake == _FRAME.handshake(m, prefix=prefix)
    assert script in handshake
    assert (quiet in handshake) is not shell_history
    assert not _saves(handshake)
    assert session._work_shell is _WorkShell.ENTERED
    assert session._saved_terminal == FOUND_SETTINGS


@pytest.mark.asyncio
async def test_a_console_that_owes_a_logout_types_todays_first_line():
    session, events = _session_on(console=True, record=FOUND_SETTINGS, owes_logout=True)

    await session._ensure_initialized()

    m = session.markers
    prefix = handshake_prefix(_FRAME, m, shell_history=True, save_terminal=True)
    assert events == [_FRAME.handshake(m, prefix=prefix)]
    assert session._work_shell is _WorkShell.NOT_USED


class _PlainFrame(CommandFrame):
    """A project frame derived from CommandFrame itself: bash's bytes, and no work shell."""

    type_name = "plain-work-shell-test"
    measures_terminal_mapping = True

    def handshake(self, m, *, prefix=""):
        return _FRAME.handshake(m, prefix=prefix)

    def frame(self, cmd, *, m):
        return _FRAME.frame(cmd, m=m)

    def recover(self, m):
        return _FRAME.recover(m)

    def end_pattern(self, m):
        return _FRAME.end_pattern(m)

    def marks_begin(self, data, *, m):
        return _FRAME.marks_begin(data, m=m)

    def parse_output(self, buffer, *, cmd, m):
        return _FRAME.parse_output(buffer, cmd=cmd, m=m)

    def extract_retcode(self, buffer, *, m):
        return _FRAME.extract_retcode(buffer, m=m)


@pytest.mark.parametrize("frame", [ZephyrFrame(), _PlainFrame()], ids=["zephyr", "command-frame"])
@pytest.mark.asyncio
async def test_a_frame_without_work_shell_lines_types_todays_bytes_on_an_outliving_console(frame):
    """Today's bytes are what the same frame types on a console that owes a logout."""
    writes = {}
    for owes in (True, False):
        session, events = _session_on(
            console=True, record=FOUND_SETTINGS, owes_logout=owes, command_frame=frame
        )
        await session._ensure_initialized()
        writes[owes] = [e.replace(session._session_id, "ID") for e in events]
        assert session._work_shell is _WorkShell.UNDECIDED
    assert writes[False] == writes[True]
    assert len(writes[False]) == 1


@pytest.mark.asyncio
async def test_an_outliving_console_whose_shell_owns_no_mark_is_refused():
    """The proof found no mark: the terminal is put back and the open refused, never retried."""
    session, events = _session_on(
        console=True, record=FOUND_SETTINGS, owes_logout=False, owns=False
    )

    with pytest.raises(ConsoleError) as refused:
        await session._ensure_initialized()

    assert str(refused.value) == str(
        session.console_client.work_shell_refused(started=False)  # ty: ignore[possibly-missing-attribute]
    )
    assert events[-1] == "close"  # the stand-in's close, which owes no logout
    restore = events[-2]
    assert RESTORE_FOUND in restore
    # Every line ran in the person's shell, and each leads: line 1, the start
    # line, each entry proof and the restore. No exit: no work shell was entered.
    lines = _FRAME.work_shell(prefix="")
    proofs = [e for e in events if "__otto_as_w" in e and RECOVER_SUFFIX in e]
    assert proofs
    assert all(e.startswith(lines.lead) for e in events[:-1])
    assert not any(lines.exit in e for e in events)
    script = _FRAME.script_parsing()
    assert not any(script in e for e in events)  # no handshake with a prefix was written
    assert not session.alive


@pytest.mark.asyncio
async def test_a_work_shell_whose_handshake_never_answers_fails_naming_the_work_shell():
    """The proof found the mark, then the work shell's own handshake never came back."""
    session, events = _session_on(
        console=True,
        record=FOUND_SETTINGS,
        owes_logout=False,
        work_handshake=False,
        init_timeout=0.3,
    )

    with pytest.raises(ConnectionError) as failed:
        await session._ensure_initialized()

    message = str(failed.value)
    assert "the work shell otto started" in message
    assert "startup file" in message
    assert "credentials" not in message
    assert not session.alive
    assert events[-1] == "close"


@pytest.mark.asyncio
async def test_a_failed_save_in_the_persons_shell_is_not_tried_again_in_the_work_shell():
    """Line 1's save printed no record; the work shell's handshake saves nothing in its place."""
    session, events = _session_on(
        console=True, record=None, later_record=CHANGED_SETTINGS, owes_logout=False
    )

    await session._ensure_initialized()

    assert _saves(events[0])
    assert not _saves(events[-1])
    assert session._work_shell is _WorkShell.ENTERED
    assert session._saved_terminal is None


@pytest.mark.asyncio
async def test_a_failed_save_is_not_tried_again_at_a_hooks_frame_entry():
    """On a console that owes a logout, line 1's save printed no record; frame entry saves none."""
    session, events = _session_on(
        console=True, record=None, later_record=CHANGED_SETTINGS, owes_logout=True
    )

    await session._ensure_initialized()
    await session.enter_frame(frame=BashFrame())

    first, second = events
    assert _saves(first)
    assert probe_marker(second) is not None  # the frame entry's handshake
    assert not _saves(second)
    assert session._saved_terminal is None
    assert session._work_shell is _WorkShell.NOT_USED


# ---------------------------------------------------------------------------
# The close: the restore, then the work shell's exit
# ---------------------------------------------------------------------------


def _close_lines(session: TelnetSession, *lines: str) -> list[str]:
    """What a close types for *lines* in *session*'s frame now: each framed, after the lead."""
    lead = _FRAME.work_shell(prefix="").lead
    return [lead + session._frame.frame(line, m=session.markers) for line in lines]


_RESTORE = _FRAME.restore_terminal(FOUND_SETTINGS) or ""
_EXIT = _FRAME.work_shell(prefix="").exit


async def _opened_in_a_work_shell(
    record: str | None = FOUND_SETTINGS,
) -> tuple[TelnetSession, list[str], int]:
    """An outliving console's session, opened in its work shell, and the open's write count."""
    session, events = _session_on(console=True, record=record, owes_logout=False)
    await session._ensure_initialized()
    assert session._work_shell is _WorkShell.ENTERED
    return session, events, len(events)


@pytest.mark.asyncio
async def test_a_work_shell_close_restores_the_terminal_then_leaves_the_work_shell():
    session, events, opened = await _opened_in_a_work_shell()

    await session.close()

    assert events[opened:] == [*_close_lines(session, _RESTORE, _EXIT), "close"]
    assert session._work_shell is _WorkShell.NOT_USED


@pytest.mark.asyncio
async def test_a_work_shell_is_left_without_a_terminal_record():
    """No ``stty`` record was saved: the close still leaves the work shell."""
    session, events, opened = await _opened_in_a_work_shell(record=None)
    assert session._saved_terminal is None

    await session.close()

    assert events[opened:] == [*_close_lines(session, _EXIT), "close"]


def _mid_command(session: TelnetSession) -> None:
    session._needs_recovery = True


def _app_shell_attached(session: TelnetSession) -> None:
    session._app_shell = object()


def _dead(session: TelnetSession) -> None:
    session._alive = False


@pytest.mark.parametrize(
    "state",
    [_mid_command, _app_shell_attached, _dead],
    ids=["running-command", "app-shell", "dead"],
)
@pytest.mark.asyncio
async def test_no_close_line_goes_into_a_running_command_an_app_shell_or_a_dead_session(state):
    session, events, opened = await _opened_in_a_work_shell()
    state(session)

    await session.close()

    assert events[opened:] == ["close"]
    assert session._work_shell is _WorkShell.NOT_USED


@pytest.mark.asyncio
async def test_no_close_line_is_typed_after_a_hook_entered_a_raw_frame(caplog):
    """RawFrame restores nothing and offers no work shell, so no line is rendered at all."""
    session, events, opened = await _opened_in_a_work_shell()
    await session.enter_frame(frame=RawFrame())
    assert len(events) == opened  # a raw landing types no handshake

    with caplog.at_level(logging.WARNING, logger="otto.host.session"):
        await session.close()

    assert events[opened:] == ["close"]
    assert caplog.records == []


class _NoWorkShellFrame(BashFrame):
    """A project's bash frame that offers no work shell."""

    def work_shell(self, *, prefix: str) -> None:
        return None


@pytest.mark.asyncio
async def test_no_exit_is_typed_after_a_hook_entered_a_frame_without_a_work_shell():
    session, events, _opened = await _opened_in_a_work_shell()
    await session.enter_frame(frame=_NoWorkShellFrame())
    entered = len(events)

    await session.close()

    assert events[entered:] == [*_close_lines(session, _RESTORE), "close"]


class _FramingFails(BashFrame):
    def frame(self, cmd: str, *, m: SessionMarkers) -> str:
        raise RuntimeError("frame() failed")


class _RestoreFails(BashFrame):
    def restore_terminal(self, saved: str) -> str | None:
        raise RuntimeError("restore_terminal() failed")


class _WorkShellFails(BashFrame):
    def work_shell(self, *, prefix: str) -> WorkShellLines:
        raise RuntimeError("work_shell() failed")


@pytest.mark.asyncio
async def test_a_session_off_a_console_never_asks_its_frame_for_a_work_shell():
    """A telnet session opens and closes without its frame's ``work_shell``, which raises here."""
    session, events = _session_on(console=False, record=None, command_frame=_WorkShellFails())

    await session._ensure_initialized()
    opened = len(events)
    await session.close()

    assert session._work_shell is _WorkShell.UNDECIDED
    assert events[opened:] == []


_RESTORE_WARNING = "could not restore the terminal's settings"
_EXIT_WARNING = "could not leave the work shell"


@pytest.mark.parametrize(
    ("frame", "written", "warnings"),
    [
        (_FramingFails(), [], [_RESTORE_WARNING, _EXIT_WARNING]),
        (_RestoreFails(), [_EXIT], [_RESTORE_WARNING]),
        (_WorkShellFails(), [_RESTORE], [_EXIT_WARNING]),
    ],
    ids=["frame", "restore_terminal", "work_shell"],
)
@pytest.mark.asyncio
async def test_a_close_line_that_cannot_be_rendered_still_releases_the_line(
    frame, written, warnings, caplog
):
    """Each failing line is logged and not written; the other is; the client still closes."""
    session, events, _opened = await _opened_in_a_work_shell()
    await session.enter_frame(frame=frame)
    entered = len(events)

    with caplog.at_level(logging.WARNING, logger="otto.host.session"):
        await session.close()

    expected = [
        BashFrame.frame(frame, line, m=session.markers) for line in written
    ]  # the framing the failing frame inherits
    lead = _FRAME.work_shell(prefix="").lead
    assert events[entered:] == [*(lead + e for e in expected), "close"]
    logged = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(logged) == len(warnings)
    for message, what in zip(logged, warnings, strict=True):
        assert what in message
        assert "failed" in message


@pytest.mark.asyncio
async def test_a_close_line_whose_write_fails_still_releases_the_line(caplog):
    """The line is gone under the close: each write is logged as lost; the client still closes."""
    session, events, opened = await _opened_in_a_work_shell()
    writer = session._writer
    lost: list[bytes] = []

    def reset(data: bytes) -> None:
        lost.append(data)
        raise ConnectionResetError("connection reset by peer")

    writer.write = reset  # ty: ignore[invalid-assignment]

    with caplog.at_level(logging.WARNING, logger="otto.host.session"):
        await session.close()

    assert len(lost) == 2  # the restore, then the exit: each tried once
    assert events[opened:] == ["close"]
    logged = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(logged) == 2
    for message, what in zip(logged, [_RESTORE_WARNING, _EXIT_WARNING], strict=True):
        assert what in message
        assert "connection reset by peer" in message
    assert session._work_shell is _WorkShell.NOT_USED


@pytest.mark.parametrize(
    ("record", "cut"), [(FOUND_SETTINGS, _RESTORE), (None, _EXIT)], ids=["restore", "exit"]
)
@pytest.mark.asyncio
async def test_cancelled_work_shell_close_still_releases_the_client(record, cut):
    """A close cancelled inside a paced close line releases the client, then re-raises.

    The line is written in 8-byte chunks with a pause after each. The test
    cancels the close once the first chunk is on the line, so the close is
    cancelled at that pause, inside the first close line it types: the
    restore where a record was saved, else the exit.
    """
    session, events, opened = await _opened_in_a_work_shell(record=record)
    (line,) = _close_lines(session, cut)
    session._write_chunk_size = 8
    session._write_chunk_delay = 3600.0  # never reached: the close is cancelled at its first pause
    on_the_line = asyncio.Event()
    writer = session._writer
    write = writer.write

    def paced(data: bytes) -> None:
        write(data)
        on_the_line.set()

    writer.write = paced  # ty: ignore[invalid-assignment]
    closing = asyncio.create_task(session.close())
    await on_the_line.wait()
    closing.cancel()

    with pytest.raises(asyncio.CancelledError):
        await closing

    assert events[opened:] == [line[:8], "close"]
    assert not session.alive
    assert not session._initialized


@pytest.mark.asyncio
async def test_closing_twice_types_each_close_line_once():
    session, events, opened = await _opened_in_a_work_shell()

    await session.close()
    await session.close()

    assert events[opened:] == [*_close_lines(session, _RESTORE, _EXIT), "close", "close"]


# ---------------------------------------------------------------------------
# The login bridge's hand-back
# ---------------------------------------------------------------------------


@pytest.fixture
def hook():
    async def fn(session, ctx):
        await session.run("true")

    register_session_setup("tty-restore", fn, overwrite=True)
    yield
    SESSION_SETUPS.unregister(name="tty-restore")


async def _bridge(console: _Console, *, console_client) -> list[str]:
    await interact._run_session_setup_on_bridge(
        write_remote=console.write_remote,
        read_remote=console.read_remote,
        newline=b"\n",
        host_name="h",
        host_id="h",
        bridge=BridgeSetup(
            setup=SessionSetup(name="tty-restore"),
            landing_frame=None,
            target_frame=BashFrame(),
            creds=[Cred(login="admin", password="pw")],
            init_timeout=None,
        ),
        proxy_hops=[],
        via_login="admin",
        log_line=lambda _l: None,
        term="console" if console_client is not None else "ssh",
        console_client=console_client,
    )
    return [w.decode() for w in console.sent]


def _handshakes(writes: list[str]) -> list[int]:
    return [i for i, w in enumerate(writes) if "READY__" in w]


@pytest.mark.asyncio
@pytest.mark.usefixtures("hook")
async def test_the_console_bridge_hands_back_the_found_settings_at_both_restores():
    """Landing restore, hook, frame entry (which saves nothing more), target restore."""
    writes = await _bridge(_Console(DialectShell()), console_client=_console_client())

    first, second = _handshakes(writes)
    restores = [i for i, w in enumerate(writes) if RESTORE_FOUND in w]
    assert _saves(writes[first])
    assert not _saves(writes[second])
    assert len(restores) == 2
    assert first < restores[0] < second < restores[1]
    assert not any("stty echo" in w for w in writes)


@pytest.mark.asyncio
@pytest.mark.usefixtures("hook")
async def test_an_ssh_bridge_saves_nothing_and_hands_back_echo_only():
    writes = await _bridge(_Console(DialectShell()), console_client=None)

    assert not any(_saves(w) for w in writes)
    assert not any(FOUND_SETTINGS in w for w in writes)
    assert sum("2>/dev/null stty echo || :" in w for w in writes) == 2


# ---------------------------------------------------------------------------
# On real shells on a real pty: the record is the settings, the restore is exact
# ---------------------------------------------------------------------------


def _settings(shell) -> str:
    found = saved_settings(shell.run(save_settings_command(TAIL_MARKERS.saved)), TAIL_MARKERS.saved)
    assert found is not None
    return found


@pytest.mark.parametrize("name", [name for name, _ in SHELLS])
def test_the_console_handshake_saves_the_settings_it_found_and_the_restore_puts_them_back(name):
    """The settings before the handshake, its saved record and the settings after restore match."""
    with interactive_shell(name) as shell:
        found = _settings(shell)
        mark = len(shell.output)
        reply = shell.type_line(
            render_probe("console-handshake", TAIL_MARKERS, history=False),
            probe_answer("console-handshake", TAIL_MARKERS),
            5.0,
        )
        saved = saved_settings(reply[mark:], TAIL_MARKERS.saved)
        changed = _settings(shell)
        restore = BashFrame().restore_terminal(saved or "")
        assert restore is not None
        # Typed with echo still off, so the prompt leads the line: the bare
        # echo puts run()'s completion token at a line start.
        shell.run(f"echo; {restore}")
        restored = _settings(shell)

    assert saved == found
    # The handshake did change them (echo off, tab0), so the restore had work
    # to do; except on zsh, measured here: its line editor puts the terminal
    # modes it saved back before each command, so the next one already reads
    # the settings found.
    assert (changed == found) == (name == "zsh")
    assert restored == found
