"""A session catches the connection-lost errors of ITS transport; only SSH
knows asyncssh's.
"""

import asyncio
import subprocess
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

from otto.host.session import LocalSession, ShellSession, SshSession, _DockerSshSession
from otto.utils import Status
from tests.unit.host._session_feed import identity_handshake_reply
from tests.unit.host.test_session import MockSession


def test_local_session_never_imports_asyncssh():
    code = (
        "import asyncio, sys\n"
        "from otto.host.local_host import LocalHost\n"
        "async def main():\n"
        "    h = LocalHost()\n"
        "    try:\n"
        "        r = (await h.run('true')).only\n"
        "    finally:\n"
        "        await h.close()\n"
        "    assert r.retcode == 0, r\n"
        "asyncio.run(main())\n"
        "print('asyncssh' in sys.modules)\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip().splitlines()[-1] == "False", out.stdout + out.stderr


def test_base_session_names_only_transport_neutral_errors():
    """``ShellSession`` is an ABC (``object.__new__`` refuses it — R3): the
    method doesn't read ``self``, so call it unbound with ``self=None``."""
    assert ShellSession._connection_lost_errors(None) == [
        asyncio.IncompleteReadError,
        BrokenPipeError,
    ]


def test_local_session_names_only_transport_neutral_errors():
    """``LocalSession`` implements every abstract method, so it is concrete —
    ``object.__new__`` works here (unlike the base ABC above)."""
    assert LocalSession._connection_lost_errors(object.__new__(LocalSession)) == [
        asyncio.IncompleteReadError,
        BrokenPipeError,
    ]


@pytest.mark.parametrize("cls", [SshSession, _DockerSshSession])
def test_ssh_session_adds_asyncssh_connection_lost(cls: type[SshSession]):
    """``_DockerSshSession`` defines no override of its own — it inherits
    ``SshSession``'s, so the exact list must match for both classes."""
    import asyncssh

    errors = cls._connection_lost_errors(object.__new__(cls))
    assert errors == [asyncio.IncompleteReadError, BrokenPipeError, asyncssh.ConnectionLost]


@pytest.mark.asyncio
async def test_ssh_connection_lost_is_still_reported():
    """``run_cmd`` still turns a dropped SSH transport into a truthful result.

    A REAL ``SshSession``, not a stand-in: its transport is
    ``self._process.stdin.write``/``self._process.stdout.read`` (see
    ``SshSession.write_transport``/``read_transport_until``), so a ``MagicMock``
    connection whose ``create_process()`` returns a process with a scripted
    ``stdout.read`` — the ready marker once (satisfies the handshake),
    then a real ``asyncssh.ConnectionLost`` (satisfies the framed command's
    read) — drives the actual production class. Deleting
    ``SshSession._connection_lost_errors`` turns this test red (proved by
    hand while writing it: reverting the override raises an uncaught
    ``asyncssh.ConnectionLost`` here instead of returning the truthful
    result below).
    """
    import asyncssh

    conn = MagicMock()
    proc = MagicMock()
    conn.create_process = AsyncMock(return_value=proc)
    s = SshSession(conn)
    proc.stdout.read = AsyncMock(
        side_effect=[
            identity_handshake_reply(s._ready_marker).encode(),
            asyncssh.ConnectionLost("gone"),
        ]
    )

    result = await s.run_cmd(cmd="echo hi")

    assert result.status == Status.Error
    assert result.value == "Session died unexpectedly (connection lost)"
    assert result.retcode == -1


def test_mock_session_matches_ssh_session_connection_lost_errors():
    """The ``MockSession`` double's override can't drift from the real one.

    ``MockSession`` (tests/unit/host/test_session.py) needs the same
    ``_connection_lost_errors`` override ``SshSession`` gets, so its
    pre-existing ``feed_connection_lost()`` tests keep exercising "a
    transport whose connection-lost errors include asyncssh's" rather than
    silently testing nothing. Pinning it here means a future edit to either
    class's list fails loudly instead of leaving the double stale.
    """
    assert MockSession._connection_lost_errors(
        object.__new__(MockSession)
    ) == SshSession._connection_lost_errors(object.__new__(SshSession))


def test_telnet_session_adds_the_closed_read_gate():
    """A read refused because the stream's release began is a lost connection."""
    from otto.host.session import TelnetSession
    from otto.host.telnet import ReadGateClosed

    assert TelnetSession._connection_lost_errors(object.__new__(TelnetSession)) == [
        asyncio.IncompleteReadError,
        BrokenPipeError,
        ReadGateClosed,
    ]


@pytest.mark.asyncio
async def test_run_cmd_read_after_close_marks_session_dead(caplog):
    """A command still reading when another task starts the close fails as a dropped line.

    Its first read is admitted and finishes; the close takes the stream
    meanwhile, so the command's next read is refused at the gate. ``run_cmd``
    returns the connection-lost result, the session is dead, and the close
    drains the line on its own, with no "already waiting" collision.
    """
    import logging
    import re

    from otto.host.session import TelnetSession
    from otto.host.telnet import ReadGateClosed
    from tests.unit.host._telnet_fakes import RecordingWriter, fake_stream, stream_client

    class _Writer(RecordingWriter):
        def write(self, data: bytes) -> None:
            self.events.append("write")

    closing: list[asyncio.Task[None]] = []

    class _Reader:
        """One line of output; the close starts while that read is in flight."""

        def __init__(self) -> None:
            self.matches = 0
            self.drain_reads = 0

        async def readuntil_pattern(self, pattern: re.Pattern[bytes]) -> bytes:
            self.matches += 1
            if self.matches > 1:
                raise AssertionError("a read ran after the stream's release began")
            closing.append(asyncio.ensure_future(client.close()))
            await asyncio.sleep(0.05)  # the close takes the stream and waits on this read
            return b"partial output\n"

        async def read(self, n: int) -> bytes:
            self.drain_reads += 1
            return b""  # the guest has nothing left: EOF

    events: list[str] = []
    reader = _Reader()
    _, _, transport = fake_stream(events)
    writer = _Writer(events, transport)
    client = stream_client(reader, writer, single_client=True)
    session = TelnetSession(reader, writer, client=client)
    session._initialized = True
    session._alive = True

    with caplog.at_level(logging.DEBUG):
        result = await session.run_cmd(cmd="echo hi", timeout=5)
        await closing[0]  # run_cmd bounds itself; the close is bounded by its budget

    assert result.status == Status.Error
    assert result.value == "Session died unexpectedly (connection lost)"
    assert result.retcode == -1
    assert not session.alive
    assert reader.matches == 1
    assert reader.drain_reads >= 1, "the close never drained the line"
    assert writer.closes == 1
    assert "already waiting" not in caplog.text
    # The refusal itself is the read entry's: a read after the take never starts.
    with pytest.raises(ReadGateClosed):
        await session.read_transport_until(pattern=re.compile("x"))
