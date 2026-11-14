"""A terminal that outlives otto's session gets back the settings otto found.

On a console (a serial line) the bash handshake's ``stty -echo`` and ``stty
tab0`` would stay after otto logs out. The first handshake there saves the
settings (``stty -g``) on the line otto already sends, and the session's one
restore step types them back at the login bridge's hand-back and before the
session closes. An ssh or telnet login's pty goes away with the session, so
nothing is saved or restored there. See :mod:`otto.host.terminal_mapping`.
"""

import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telnetlib3.stream_reader import TelnetReader

from otto.host import interact
from otto.host.command_frame import READY_SUFFIX, BashFrame, SessionMarkers, ZephyrFrame
from otto.host.login_proxy import Cred
from otto.host.session import BridgeSetup, LocalSession, SshSession, TelnetSession
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
from tests.unit.host._session_feed import identity_handshake_reply
from tests.unit.host._telnet_fakes import stream_client
from tests.unit.host.test_interact_session_setup import _Console

RESTORE_FOUND = f"stty {FOUND_SETTINGS}"


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


def _console_client(events: list[str] | None = None) -> SimpleNamespace:
    async def close() -> None:
        if events is not None:
            events.append("logout")

    return SimpleNamespace(logged_in=False, read_gate=ReadGate(), close=close)


def test_only_a_session_on_a_console_client_outlives_its_terminal():
    reader, writer = TelnetReader(), object()
    on_console = TelnetSession(reader=reader, writer=writer, console_client=_console_client())
    on_telnet = TelnetSession(reader=reader, writer=writer, client=stream_client(reader, writer))
    assert on_console.terminal_outlives_session
    assert not on_telnet.terminal_outlives_session
    assert not SshSession(conn=None).terminal_outlives_session
    assert not LocalSession().terminal_outlives_session


# ---------------------------------------------------------------------------
# A session over a telnetlib3 reader: the handshake saves, the close restores
# ---------------------------------------------------------------------------


class _Line:
    """The writer end of a fake shell on a line: it answers the handshake, records the rest.

    The handshake is answered with the record its save prints (*record*, or
    nothing for a target without ``stty``), then the identity reply. Every
    write is logged in *events*, as the console's logout is.
    """

    def __init__(self, reader: TelnetReader, events: list[str], record: str | None) -> None:
        self._reader = reader
        self.events = events
        self._record = record

    def write(self, data: bytes) -> None:
        text = data.decode().replace("\r", "\n")
        self.events.append(text)
        marker = probe_marker(text)
        if marker is not None and marker.endswith(READY_SUFFIX):
            tag = re.search(r"(__OTTO_\w+?_SAVED__)\$\(", text)
            saved = f"{tag.group(1)}{self._record}\r\n" if self._record and tag else ""
            self._reader.feed_data((saved + identity_handshake_reply(marker)).encode())


def _session_on(*, console: bool, record: str | None) -> tuple[TelnetSession, list[str]]:
    events: list[str] = []
    reader = TelnetReader()
    writer = _Line(reader, events, record)
    if console:
        session = TelnetSession(
            reader=reader, writer=writer, console_client=_console_client(events)
        )
    else:
        session = TelnetSession(reader=reader, writer=writer, client=stream_client(reader, writer))
        session._client = SimpleNamespace(close=AsyncMock())  # the release is not under test
    return session, events


@pytest.mark.asyncio
async def test_a_console_session_restores_the_found_settings_before_it_logs_out():
    session, events = _session_on(console=True, record=FOUND_SETTINGS)

    await session._ensure_initialized()
    assert "stty -g" in events[0]
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

    assert all("stty -g" not in e for e in handshakes)
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
    assert "stty -g" in writes[first]
    assert "stty -g" not in writes[second]
    assert len(restores) == 2
    assert first < restores[0] < second < restores[1]
    assert not any("stty echo" in w for w in writes)


@pytest.mark.asyncio
@pytest.mark.usefixtures("hook")
async def test_an_ssh_bridge_saves_nothing_and_hands_back_echo_only():
    writes = await _bridge(_Console(DialectShell()), console_client=None)

    assert not any("stty -g" in w for w in writes)
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
        shell.send(render_probe("console-handshake", TAIL_MARKERS, history=False))
        reply = shell.wait_for_output(probe_answer("console-handshake", TAIL_MARKERS), 5.0)
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
