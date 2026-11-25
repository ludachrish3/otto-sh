"""A real :class:`~otto.host.session.ShellSession` over an interactive shell on its own pty.

The exec frame's promises (isolation, errexit kept, end of file on stdin, the
post-recovery restore) are properties of a shell reading a TERMINAL: the
interrupt that a timeout sends is a terminal's ``^C``, and an interactive
shell abandons the rest of its line when the foreground job it ran dies of
it. So these sessions run the product's own handshake, frames, read loop and
recovery against each shell of :data:`tests._fixtures.interactive_shell.SHELLS`,
spawned on a controlling pty by the one spawner
(:func:`tests._fixtures.controlling_pty.spawn_on_controlling_pty`) in the
one environment (:func:`tests._fixtures.interactive_shell.shell_env`).
"""

import asyncio
import contextlib
import os
import re
import shutil
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from otto.host.command_frame import ExecInput, ExecRestore
from otto.host.host import exec_stdin_eof
from otto.host.output_codec import READ_CHUNK, PatternReader
from otto.host.secret_wire import reveal
from otto.host.session import ShellSession, _PatternReaderTransport
from otto.result import CommandResult
from tests._fixtures.controlling_pty import ControllingPty, spawn_on_controlling_pty
from tests._fixtures.interactive_shell import SHELL_ARGV, SHELLS, shell_env

PTY_SHELLS: list[str] = [name for name, _argv in SHELLS]
"""The matrix shells installed here, the required ones first."""


class PtySession(_PatternReaderTransport, ShellSession):
    """A :class:`ShellSession` whose transport is the controller of an interactive shell's pty."""

    reads_a_terminal = True

    def __init__(
        self,
        name: str,
        *,
        env: "dict[str, str] | None" = None,
        restore: ExecRestore = ExecRestore.BASELINE,
    ) -> None:
        super().__init__()
        self._shell_name = name
        self._env_overlay = env
        self._test_restore = restore
        self._pty: ControllingPty | None = None
        self._reader: PatternReader | None = None
        self._dirs = contextlib.ExitStack()
        self.began = asyncio.Event()
        """Set the first time a read returns the chunk carrying a frame's BEGIN marker."""

    async def open_transport(self) -> None:
        home = self._dirs.enter_context(tempfile.TemporaryDirectory(prefix="otto-ptys-"))
        fakes = self._dirs.enter_context(tempfile.TemporaryDirectory(prefix="otto-ptys-fakes-"))
        self._pty = spawn_on_controlling_pty(
            SHELL_ARGV[self._shell_name],
            env=shell_env(self._shell_name, home, fakes, overlay=self._env_overlay),
            onlcr=True,
        )
        controller = self._pty.controller
        loop = asyncio.get_running_loop()

        async def read_chunk() -> bytes:
            readable = loop.create_future()
            loop.add_reader(controller, lambda: readable.done() or readable.set_result(None))
            try:
                await readable
            finally:
                loop.remove_reader(controller)
            try:
                return os.read(controller, READ_CHUNK)
            except OSError:  # EIO: the shell side is gone, which a read sees as end of stream
                return b""

        self._reader = PatternReader(read_chunk)

    async def write_transport(self, *, data: str | SecretStr) -> None:
        assert self._pty is not None
        payload = reveal(data).encode()
        while payload:
            payload = payload[os.write(self._pty.controller, payload) :]

    async def read_transport_until(self, *, pattern: re.Pattern[str]) -> str:
        assert self._reader is not None
        data = await self._reader.read_transport_until(pattern)
        if not self.began.is_set() and self._frame.marks_begin(data, m=self._markers):
            self.began.set()
        return data

    async def close(self) -> None:
        try:
            if self._pty is not None:
                self._pty.close()
                self._pty = None
        finally:
            self._dirs.close()
            self._alive = False
            self._initialized = False

    async def exec_(self, cmd: str, *, credential: bool = False, **kw: Any) -> CommandResult:
        """Run *cmd* as an ``exec`` does, with the :class:`ExecInput` ``BaseHost.exec`` builds.

        *credential* is the elevation's
        :attr:`~otto.host.host.ElevatedCommand.reads_credential_fd`, as
        ``BaseHost.exec`` carries it into the frame.
        """
        exec_input = ExecInput(
            stdin_eof=exec_stdin_eof(kw.get("expects")),
            restore=self._test_restore,
            credential=credential,
        )
        return await self.run_cmd(cmd=cmd, exec_input=exec_input, **kw)


@contextlib.asynccontextmanager
async def pty_session(
    name: str,
    *,
    setup: str = "",
    env: "dict[str, str] | None" = None,
    restore: ExecRestore = ExecRestore.BASELINE,
) -> AsyncIterator[PtySession]:
    """Open a :class:`PtySession` on shell *name*, handshaken, with *setup* typed through ``run``.

    *restore* is what its ``exec_`` calls restore. The shell is killed on exit.
    """
    session = PtySession(name, env=env, restore=restore)
    try:
        await session._ensure_initialized()
        if setup:
            done = await session.run_cmd(cmd=setup, timeout=5)
            assert (done.timed_out, done.retcode) == (False, 0), done
        yield session
    finally:
        await session.close()


@contextlib.asynccontextmanager
async def console_session(
    name: str, *, setup: str = "", env: "dict[str, str] | None" = None
) -> AsyncIterator[PtySession]:
    """Open a :func:`pty_session` on shell *name* as a console's default session is.

    Its ``exec_`` calls restore :attr:`ExecRestore.START`, the restore a
    console's default session is given, so they type the console's exec
    frame; ``SHELL`` names *name*'s own program (BusyBox's through a link
    called ``sh``), so the sudo ``shell`` form a console exec types starts *name*.
    """
    with tempfile.TemporaryDirectory(prefix="otto-ptys-login-") as links:
        argv = dict(SHELLS)[name]
        program = shutil.which(argv[0])
        assert program is not None, f"{argv[0]} is not installed"
        if len(argv) > 1:  # a multi-call binary: the applet is chosen by its name
            link = Path(links) / argv[1]
            link.symlink_to(program)
            program = str(link)
        async with pty_session(
            name, setup=setup, env={"SHELL": program, **(env or {})}, restore=ExecRestore.START
        ) as s:
            yield s
