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
    ``self._process.stdin.write``/``self._process.stdout.readuntil`` (see
    ``SshSession._write``/``_read_until_pattern``), so a ``MagicMock``
    connection whose ``create_process()`` returns a process with a scripted
    ``stdout.readuntil`` — the ready marker once (satisfies the handshake),
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
    proc.stdout.readuntil = AsyncMock(
        side_effect=[f"{s._ready_marker}\n", asyncssh.ConnectionLost("gone")]
    )

    result = await s.run_cmd("echo hi")

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
