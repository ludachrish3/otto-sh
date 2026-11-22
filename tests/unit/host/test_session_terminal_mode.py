"""A shell session knows, per instance, whether its shell reads a terminal.

An ssh session can be opened without a pty: its shell then reads a pipe, as
the local host's does. Two things follow, and each is pinned here.

1. Nothing is spelled for a terminal: a TAB is written bare.
2. A pipe has no Ctrl-C. The shell is started with a token in its
   environment, and a command otto gives up on is stopped by killing every
   process that carries it, from a second channel: the same on every ssh
   server. The session ends with it. The shell is not kept: a shell that
   reads a pipe is not interactive, so one that outlived the stop would run
   the rest of the line otto gave up on.

The sessions here are real: a local shell reading a pipe stands in for the
remote login shell, behind what otto uses of an asyncssh connection
(``create_process`` with its ``stdin.write``, ``stdout.read`` and
``send_signal``, and ``run``). Nothing talks to a host.
"""

import asyncio
import contextlib
import os
import re
import shutil
import signal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from otto.host.command_frame import LITERAL_NEXT, SESSION_SHELL
from otto.host.session import SshSession
from otto.utils import wait_for_async
from scripts.process_group import process_state


class _PipeProcess:
    """An asyncssh process over a local ``bash`` that reads a pipe.

    ``send_signal`` reaches the shell's process group when *signals_group*
    (what OpenSSH does), or the shell alone (what dropbear does).
    """

    def __init__(self, proc: asyncio.subprocess.Process, *, signals_group: bool) -> None:
        self._proc = proc
        self._signals_group = signals_group
        self.written: list[bytes] = []
        self.signals: list[str] = []
        assert proc.stdin is not None
        assert proc.stdout is not None
        self.stdin = SimpleNamespace(write=self._write)
        self.stdout = proc.stdout

    def _write(self, data: bytes) -> None:
        self.written.append(data)
        assert self._proc.stdin is not None
        self._proc.stdin.write(data)

    def send_signal(self, name: str) -> None:
        self.signals.append(name)
        number = getattr(signal, f"SIG{name}")
        if self._signals_group:
            os.killpg(self._proc.pid, number)
        else:
            os.kill(self._proc.pid, number)

    def close(self) -> None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self._proc.pid, signal.SIGKILL)


class _PipeConnection:
    """An asyncssh connection over local processes.

    ``create_process`` hands its command to ``sh``, as a server hands it to
    the user's shell, with ``SHELL`` naming bash and an empty *home* (no
    profile of the developer's is read). ``run`` runs its command the same
    way, on a channel of its own; with *runs* false it fails, as a
    connection that can open no second channel does.
    """

    def __init__(self, home: Path, *, signals_group: bool = True, runs: bool = True) -> None:
        self._signals_group = signals_group
        self._runs = runs
        self._env = {**os.environ, "SHELL": shutil.which("bash") or "bash", "HOME": str(home)}
        self.opened: list[tuple[Any, ...]] = []
        self.asked: list[dict[str, Any]] = []
        self.ran: list[str] = []
        self.processes: list[_PipeProcess] = []
        self._children: list[asyncio.subprocess.Process] = []

    async def create_process(self, *args: Any, **kwargs: Any) -> _PipeProcess:
        self.opened.append(args)
        self.asked.append(kwargs)
        proc = await asyncio.create_subprocess_exec(
            "sh",
            "-c",
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
            env=self._env,
        )
        self._children.append(proc)
        process = _PipeProcess(proc, signals_group=self._signals_group)
        self.processes.append(process)
        return process

    async def run(self, command: str, *, check: bool, request_pty: bool) -> SimpleNamespace:
        assert request_pty is False
        self.ran.append(command)
        if not self._runs:
            raise OSError("no second channel")
        proc = await asyncio.create_subprocess_exec(
            "sh", "-c", command, stdout=asyncio.subprocess.DEVNULL, start_new_session=True
        )
        return SimpleNamespace(exit_status=await proc.wait())

    async def reap(self) -> None:
        for proc in self._children:
            # The whole group, the shell gone or not: a command that outlived
            # its shell is what some of these tests leave behind.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            # communicate() closes stdin and reads stdout to its end, so no
            # pipe of the child's is left open behind the test.
            await proc.communicate()


@pytest_asyncio.fixture(params=["openssh", "dropbear"])
async def server(request, tmp_path):
    """Each kind of server: a signal request reaches the group (OpenSSH) or the shell alone."""
    conn = _PipeConnection(tmp_path, signals_group=request.param == "openssh")
    yield conn
    await conn.reap()


@pytest_asyncio.fixture
async def openssh(tmp_path):
    """A connection whose signal request reaches the whole process group."""
    conn = _PipeConnection(tmp_path, signals_group=True)
    yield conn
    await conn.reap()


@pytest_asyncio.fixture(params=["openssh", "dropbear"])
async def no_second_channel(request, tmp_path):
    """A connection of each kind on which the marked stop cannot run."""
    conn = _PipeConnection(tmp_path, signals_group=request.param == "openssh", runs=False)
    yield conn
    await conn.reap()


def _session(conn: _PipeConnection) -> SshSession:
    return SshSession(conn, terminal=False)  # ty: ignore[invalid-argument-type]


@pytest.mark.asyncio
async def test_a_session_without_a_terminal_asks_for_no_pty_and_a_marked_login_shell(openssh):
    """No pty is requested, and the shell is the user's login shell started with the token."""
    session = _session(openssh)
    await session.open_transport()
    assert "term_type" not in openssh.asked[0]
    assert openssh.asked[0]["request_pty"] is False
    (command,) = openssh.opened[0]
    token = re.fullmatch(r"OTTO_SESSION=([0-9a-f]{32}) exec (.*) -l", command)
    assert token is not None
    assert token[2] == SESSION_SHELL
    assert session.reads_a_terminal is False


@pytest.mark.asyncio
async def test_a_session_with_a_terminal_still_asks_for_one():
    """The default is unchanged: a dumb terminal on the channel's own shell, unmarked."""
    conn = SimpleNamespace(create_process=AsyncMock(return_value=MagicMock()))
    session = SshSession(conn)  # ty: ignore[invalid-argument-type]
    await session.open_transport()
    assert conn.create_process.await_args.args == ()
    assert conn.create_process.await_args.kwargs["term_type"] == "dumb"
    assert session.reads_a_terminal is True


@pytest.mark.asyncio
async def test_a_tab_is_written_bare_and_arrives(openssh):
    """Nothing is spelled for a line reader that is not there."""
    session = _session(openssh)
    result = await session.run_cmd(cmd="printf 'a\\tb\tc'", timeout=10)
    assert result.retcode == 0
    assert result.value == "a\tb\tc"
    written = b"".join(openssh.processes[0].written)
    assert LITERAL_NEXT.encode() not in written
    assert session.echoes is False


@pytest.mark.asyncio
async def test_every_command_carries_the_session_s_token(openssh):
    """What the shell starts inherits the token the stop looks for."""
    session = _session(openssh)
    result = await session.run_cmd(cmd="sh -c 'echo $OTTO_SESSION'", timeout=10)
    assert re.fullmatch(r"[0-9a-f]{32}", result.value)
    assert openssh.opened[0][0].startswith(f"OTTO_SESSION={result.value} ")


async def _pid_in(path: Path) -> int:
    """Return the pid a command wrote to *path*, once it is written whole."""
    await wait_for_async(
        lambda: path.exists() and path.read_text().endswith("\n"),
        5.0,
        interval=0.01,
        on_timeout=f"{path.name} was never written",
    )
    return int(path.read_text())


async def _wait_gone(pid: int) -> None:
    """Wait until process *pid* runs nothing more (gone, or a zombie)."""
    await wait_for_async(
        lambda: process_state(pid) in (None, "Z"),
        5.0,
        interval=0.01,
        on_timeout=f"process {pid} is still running",
    )


async def _cancel_once_written(session: SshSession, cmd: str, pids: list[Path]) -> list[int]:
    """Run *cmd*, cancel it once every pid file is written, and return the pids.

    A cancel is stopped as a timeout is, and it comes when the test says: no
    budget has to be guessed for a loaded machine.
    """
    running = asyncio.ensure_future(session.run_cmd(cmd=cmd, timeout=60))
    try:
        written = [await _pid_in(pid) for pid in pids]
    finally:
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
    return written


@pytest.mark.asyncio
async def test_a_stopped_line_is_stopped_whole_on_either_server(server, tmp_path):
    """A subshell, a wrapped command and a loop are all stopped, and nothing after them runs."""
    session = _session(server)
    pids = [tmp_path / f"pid{i}" for i in range(3)]
    ran = [tmp_path / f"ran{i}" for i in range(3)]
    cmd = (
        f"(sh -c 'echo $$ > {pids[0]}; sleep 60' | cat; touch {ran[0]}) & "
        f"echo x | while read a; do sh -c 'echo $$ > {pids[1]}; sleep 60'; touch {ran[1]}; done & "
        f"sh -c 'echo $$ > {pids[2]}; sleep 60'; touch {ran[2]}; sleep 60"
    )
    written = await _cancel_once_written(session, cmd, pids)
    assert not session.alive
    assert len(server.ran) == 1
    assert server.processes[0].signals == ["KILL"]
    for pid in written:
        await _wait_gone(pid)
    await asyncio.sleep(0.3)
    assert [path.exists() for path in ran] == [False, False, False]


@pytest.mark.asyncio
async def test_a_command_that_left_its_shell_s_group_is_stopped_too(server, tmp_path):
    """The token goes where a process group does not: a command in a session of its own."""
    session = _session(server)
    pid = tmp_path / "pid"
    cmd = f"setsid sh -c 'echo $$ > {pid}; sleep 60' & sleep 60"
    (left,) = await _cancel_once_written(session, cmd, [pid])
    try:
        await _wait_gone(left)
    finally:
        # A group of its own, outside the one the fixture kills: never left
        # behind by a failure.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(left, signal.SIGKILL)


@pytest.mark.asyncio
async def test_nothing_but_otto_s_frames_is_written_to_the_shell(openssh):
    """No setup line of its own: the first thing the shell reads is the handshake."""
    session = _session(openssh)
    await session.run_cmd(cmd="true", timeout=10)
    assert b"__otto_probe=" in openssh.processes[0].written[0]


@pytest.mark.asyncio
async def test_where_the_marked_stop_cannot_run_the_line_is_still_abandoned(
    no_second_channel, tmp_path
):
    """The channel's ``KILL`` ends the shell on either server, so the rest never runs.

    bash alone, signalled with ``INT``, would wait for its command and carry
    on; so would a line that set its own handler.
    """
    session = _session(no_second_channel)
    after = tmp_path / "after"
    result = await session.run_cmd(cmd=f"trap : INT; sleep 2; touch {after}", timeout=0.3)
    assert result.timed_out
    assert not session.alive
    assert no_second_channel.processes[0].signals == ["KILL"]
    await asyncio.sleep(2.5)
    assert not after.exists()


@pytest.mark.asyncio
async def test_a_signal_the_channel_cannot_send_leaves_the_session_dead(openssh):
    """A channel that is already gone is a dead session, not a traceback."""
    session = _session(openssh)
    await session.run_cmd(cmd="true", timeout=10)

    def gone(name: str) -> None:
        raise BrokenPipeError("Channel not open for sending")

    openssh.processes[0].send_signal = gone
    result = await session.run_cmd(cmd="sleep 60", timeout=0.5)
    assert result.timed_out
    assert not session.alive


@pytest.mark.asyncio
async def test_a_session_with_a_terminal_is_still_interrupted_with_ctrl_c():
    """The default mode's stop is unchanged: the Ctrl-C byte, no second channel, no signal."""
    process = MagicMock()
    conn = SimpleNamespace(create_process=AsyncMock(return_value=process), run=AsyncMock())
    session = SshSession(conn)  # ty: ignore[invalid-argument-type]
    await session.open_transport()
    session._confirm_recovered = AsyncMock()  # ty: ignore[invalid-assignment]
    await session._recover_session()
    process.stdin.write.assert_called_once_with(b"\x03")
    process.send_signal.assert_not_called()
    conn.run.assert_not_called()
