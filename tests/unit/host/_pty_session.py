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
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from otto.host.command_frame import CommandFrame, ExecInput, ExecRestore
from otto.host.console import ConsoleClient
from otto.host.host import exec_stdin_eof
from otto.host.options import ConsoleOptions
from otto.host.output_codec import READ_CHUNK, PatternReader
from otto.host.secret_wire import reveal
from otto.host.session import ShellSession, _PatternReaderTransport
from otto.result import CommandResult
from tests._fixtures.controlling_pty import ControllingPty, spawn_on_controlling_pty
from tests._fixtures.interactive_shell import (
    SHELL_ARGV,
    SHELLS,
    PersonState,
    parse_person_state,
    person_state_line,
    shell_env,
)

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
        rc: str = "",
        console_client: "ConsoleClient | None" = None,
        command_frame: "CommandFrame | None" = None,
        shell_history: bool = True,
        pty: "ControllingPty | None" = None,
        keep_shell: bool = False,
    ) -> None:
        """*console_client*, *command_frame* and *shell_history* are the session's own.

        *pty* adopts a shell already running on its pty: the open spawns
        nothing, and types *rc* only where one is given (a shell the test
        spawned itself, with argv of its own). With *keep_shell*, :meth:`close` leaves the
        shell running for the test, which ends it with :meth:`end_shell`.
        """
        super().__init__(
            console_client=console_client,
            command_frame=command_frame,
            shell_history=shell_history,
        )
        self._shell_name = name
        self._rc = rc
        self._env_overlay = env
        self._test_restore = restore
        self._pty: ControllingPty | None = None
        self._adopted = pty
        self._keep_shell = keep_shell
        self._reader: PatternReader | None = None
        self._dirs = contextlib.ExitStack()
        self.home = ""
        """The shell's ``HOME``, an empty directory of its own; empty for an adopted shell."""
        self.began = asyncio.Event()
        """Set the first time a read returns the chunk carrying a frame's BEGIN marker."""

    person_before: "PersonState"
    """The person's shell and terminal before the open, as :func:`work_shell_session` read them."""

    async def open_transport(self) -> None:
        if self._adopted is not None:
            self._pty = self._adopted
        else:
            self.home = self._dirs.enter_context(tempfile.TemporaryDirectory(prefix="otto-ptys-"))
            fakes = self._dirs.enter_context(tempfile.TemporaryDirectory(prefix="otto-ptys-fakes-"))
            self._pty = spawn_on_controlling_pty(
                SHELL_ARGV[self._shell_name],
                env=shell_env(self._shell_name, self.home, fakes, overlay=self._env_overlay),
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
        if self._rc:  # a user's rc file, run before otto's first line
            await self.write_transport(data=f"{self._rc}; echo rc''-done\n")
            await self._reader.read_transport_until(re.compile(r"rc-done\r?\n"))

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
        """Type a console session's close lines (restore, work-shell exit), then end the shell.

        With *keep_shell* the shell is left running: :meth:`end_shell` ends it.
        """
        try:
            await self._hand_back_line()
        finally:
            self._alive = False
            self._initialized = False
            if not self._keep_shell:
                self.end_shell()

    def end_shell(self) -> None:
        """Kill the shell and remove its directories."""
        try:
            if self._pty is not None:
                self._pty.close()
                self._pty = None
        finally:
            self._dirs.close()

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
async def pty_session(  # noqa: PLR0913 — PtySession's keywords, passed through
    name: str,
    *,
    setup: str = "",
    env: "dict[str, str] | None" = None,
    restore: ExecRestore = ExecRestore.BASELINE,
    rc: str = "",
    console_client: "ConsoleClient | None" = None,
    command_frame: "CommandFrame | None" = None,
    shell_history: bool = True,
    pty: "ControllingPty | None" = None,
    keep_shell: bool = False,
    before_open: "Callable[[PtySession], Awaitable[None]] | None" = None,
) -> AsyncIterator[PtySession]:
    """Open a :class:`PtySession` on shell *name*, handshaken, with *setup* typed through ``run``.

    *restore* is what its ``exec_`` calls restore. *rc* is typed before the
    handshake, as a user's rc file would have run it. *before_open* is
    awaited after the transport is up (the shell spawned, *rc* typed) and
    before the handshake. The other keywords are :class:`PtySession`'s. The
    shell is killed on exit, unless *keep_shell*.
    """
    session = PtySession(
        name,
        env=env,
        restore=restore,
        rc=rc,
        console_client=console_client,
        command_frame=command_frame,
        shell_history=shell_history,
        pty=pty,
        keep_shell=keep_shell,
    )
    try:
        await session.open_transport()
        if before_open is not None:
            await before_open(session)
        await session._handshake(opening=True)
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
        program = applet_program(name, links)
        async with pty_session(
            name, setup=setup, env={"SHELL": program, **(env or {})}, restore=ExecRestore.START
        ) as s:
            yield s


def is_multi_call(name: str) -> bool:
    """Whether shell *name* is an applet of a multi-call binary (BusyBox), named by its argv."""
    return len(dict(SHELLS)[name]) > 1


def applet_program(name: str, links: str) -> str:
    """The program that runs shell *name*: its binary, or for a multi-call one a link in *links*.

    BusyBox picks its applet by the name it runs under, so ``busybox-sh``
    gets a link called ``sh`` to the BusyBox binary in *links*, which the
    caller keeps until the shell is done.
    """
    argv = dict(SHELLS)[name]
    program = shutil.which(argv[0])
    assert program is not None, f"{argv[0]} is not installed"
    if is_multi_call(name):  # the applet is chosen by its name
        link = Path(links) / argv[1]
        link.symlink_to(program)
        program = str(link)
    return program


async def reads_bang_and_hash_as_a_script_does(s: PtySession) -> None:
    """The three typed lines an interactive bash or zsh would read otherwise (#1346)."""
    assert (await s.run_cmd(cmd="echo a !b c", timeout=5)).value == "a !b c"
    assert (await s.run_cmd(cmd='echo "x!y" "!z"', timeout=5)).value == "x!y !z"
    r = await s.run_cmd(cmd="echo kept # it's a comment", timeout=5)
    assert (r.timed_out, r.value, r.retcode) == (False, "kept", 0)


def outliving_console(*, logged_in: bool = False, logout: bool = True) -> ConsoleClient:
    """A real, unopened console client whose shell outlives the session unless it owes a logout.

    ``login`` is off, as on a console otto drives without logging in;
    *logged_in* is what a hook's ``console_login()`` leaves, and with
    *logout* the client then owes a logout.
    """
    client = ConsoleClient(
        host="pty",
        port=1,
        user="",
        password=None,
        options=ConsoleOptions(server="s", port=1, login=False, logout=logout, settle=0),
    )
    client._logged_in = logged_in
    return client


async def person_state(s: PtySession, *, mark: str = "__otto_as_w") -> PersonState:
    """Read the state of the shell reading *s*'s terminal now, by one line typed straight in.

    The line goes through ``write_transport``, not a frame, so it reads the
    shell as it is (:func:`~tests._fixtures.interactive_shell.person_state_line`,
    which *mark* is passed to).
    """
    typed = person_state_line(mark=mark)
    await s.write_transport(data=typed.line)
    return parse_person_state(await s.read_transport_until(pattern=typed.done), typed)


_STARTUP_FILES = {"bash": ".bashrc", "zsh": ".zshrc"}
"""The startup file in ``HOME`` an interactive non-login shell reads; the others read ``$ENV``."""


@contextlib.asynccontextmanager
async def work_shell_session(  # noqa: PLR0913 — PtySession's keywords, and what the person's shell holds
    name: str,
    *,
    rc: str = "",
    env: "dict[str, str] | None" = None,
    client: "ConsoleClient | None" = None,
    frame: "CommandFrame | None" = None,
    shell_history: bool = True,
    keep_shell: bool = False,
    pty: "ControllingPty | None" = None,
    startup: "str | None" = "",
    path_first: "list[str] | None" = None,
    kept: "list[PtySession] | None" = None,
) -> AsyncIterator[PtySession]:
    """Open a :func:`pty_session` on shell *name* as a console whose shell outlives the session.

    The client is :func:`outliving_console` unless *client* is given. Before
    the handshake the person's shell state is kept on ``s.person_before``
    (:func:`person_state`), then *startup* is written to the startup file a
    work shell of *name*'s kind reads (``$HOME/.bashrc``, ``$HOME/.zshrc``,
    else the file ``ENV`` names), which the person's shell, already started,
    never reads. A zsh gets a ``.zshrc`` even for an empty *startup*, so its
    work shell never starts the new-user menu; *startup* ``None`` writes no
    startup file at all. *path_first* leads ``PATH``; for ``busybox-sh``
    a ``sh`` link to the BusyBox binary leads it, as :func:`console_session`
    builds its ``SHELL`` link, so the work shell is BusyBox too. *kept*, when
    given, gets the session before its handshake, so a test reaches the
    shell of an open that was refused (with *keep_shell*).
    """
    with tempfile.TemporaryDirectory(prefix="otto-ptys-work-") as work:
        first = list(path_first or [])
        if is_multi_call(name):  # BusyBox: its `sh` link leads PATH
            applet_program(name, work)
            first.insert(0, work)
        env_file = Path(work, "env.sh")
        env_file.write_text("")
        overlay = dict(env or {})
        if name not in _STARTUP_FILES:
            overlay.setdefault("ENV", str(env_file))
        if first:
            base = overlay.get("PATH", os.environ["PATH"])
            overlay["PATH"] = os.pathsep.join([*first, base])

        async def before_open(s: PtySession) -> None:
            if kept is not None:
                kept.append(s)
            s.person_before = await person_state(s)
            rc_file = _STARTUP_FILES.get(name)
            if startup is None:
                return
            if rc_file is None:
                Path(overlay.get("ENV", str(env_file))).write_text(startup)
            elif s.home:
                Path(s.home, rc_file).write_text(startup)

        async with pty_session(
            name,
            env=overlay,
            rc=rc,
            console_client=client or outliving_console(),
            command_frame=frame,
            shell_history=shell_history,
            pty=pty,
            keep_shell=keep_shell,
            before_open=before_open,
        ) as s:
            yield s
