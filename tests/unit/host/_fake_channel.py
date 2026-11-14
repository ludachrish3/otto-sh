"""One stand-in for an asyncssh exec channel, and the manager the tests drive it through.

Every unit test of :meth:`otto.host.session.SessionManager.exec_on` builds its
channel here, so a change to what the product reads from the channel (its
``stdout``, its ``wait()`` result, the signals it sends) is made once.
"""

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from otto.host import UnixHost
from otto.host.element import Element
from otto.host.login_proxy import Cred
from otto.host.session import SessionManager
from otto.logger.mode import LogMode


class _FakeReader:
    """A channel's ``stdout``: one text buffer fed from *chunks*, read by line or by size.

    ``async for`` yields complete lines, then the unfinished remainder once the
    chunks are consumed (a lone ``""`` chunk is yielded as asyncssh's EOF
    partial); ``read(n)`` hands back the next chunk, then ``""``.
    Without *eof*, both block forever once the chunks are consumed, as a
    command that is still running and silent does; ``reading`` is set when
    they first block. ``fail_with``, when set, is raised by the next read, as
    a channel that is lost mid-drain raises.
    """

    def __init__(self, chunks: "list[str]", *, eof: bool) -> None:
        self._chunks = list(chunks)
        self._buffer = ""
        self._eof = eof
        self.reading = asyncio.Event()
        self.fail_with: "BaseException | None" = None

    async def _stall(self) -> None:
        self.reading.set()
        await asyncio.Event().wait()

    def __aiter__(self) -> "_FakeReader":
        return self

    async def __anext__(self) -> str:
        if not self._buffer and self._chunks and self._chunks[0] == "":
            # An empty chunk on its own is asyncssh's EOF partial: ``readline()``
            # handing back ``''`` when end-of-file lands after the last line (#711).
            self._chunks.pop(0)
            return ""
        while "\n" not in self._buffer and self._chunks:
            self._buffer += self._chunks.pop(0)
        if "\n" in self._buffer:
            line, _, self._buffer = self._buffer.partition("\n")
            return line + "\n"
        if not self._eof:
            await self._stall()
        if self._buffer:
            line, self._buffer = self._buffer, ""
            return line
        raise StopAsyncIteration

    async def read(self, _n: int = -1) -> str:
        if self.fail_with is not None:
            raise self.fail_with
        if self._chunks:
            return self._chunks.pop(0)
        if not self._eof:
            await self._stall()
        return ""


class FakeChannelProcess:
    """An asyncssh ``SSHClientProcess`` as the exec channel uses one.

    ``wait()`` returns ``exit_status`` and ``returncode`` as asyncssh's
    completed process carries them: at once when *eof* and ``exits_on_eof``
    (the default), otherwise only after a signal in *obeys* was sent; with
    ``exits_on_eof`` false the command closes its output and keeps running.
    Every signal sent is recorded in ``signals``. ``reading`` is the reader's:
    set when the drain first blocks on a silent command.
    """

    def __init__(
        self,
        chunks: "list[str]",
        *,
        returncode: "int | None" = 0,
        exit_status: "int | None" = 0,
        eof: bool = True,
        obeys: "frozenset[str]" = frozenset({"TERM", "HUP", "KILL"}),
    ) -> None:
        self.stdout = _FakeReader(chunks, eof=eof)
        self.returncode = returncode
        self.exit_status = exit_status
        self.obeys = obeys
        self.signals: list[str] = []
        self.exits_on_eof = True
        self.reading = self.stdout.reading
        self._eof = eof
        self._exited = asyncio.Event()

    async def wait(self) -> SimpleNamespace:
        if not (self._eof and self.exits_on_eof):
            await self._exited.wait()
        return SimpleNamespace(exit_status=self.exit_status, returncode=self.returncode)

    def send_signal(self, name: str) -> None:
        self.signals.append(name)
        if name in self.obeys:
            self._exited.set()

    def terminate(self) -> None:
        self.send_signal("TERM")

    def kill(self) -> None:
        self.send_signal("KILL")


class FakeConn:
    """An asyncssh connection whose ``create_process`` hands back *process*."""

    def __init__(self, process: Any) -> None:
        self.process = process
        self.kwargs: dict[str, Any] = {}

    async def create_process(self, _cmd: str, **kw: Any) -> Any:
        self.kwargs = kw
        return self.process


def channel_host(conn: "FakeConn | None" = None, *, element: str = "box") -> UnixHost:
    """An ssh ``UnixHost`` whose connection is *conn*."""
    host = UnixHost(
        ip="10.0.0.1",
        element=Element(name=element),
        creds=[Cred(login="u", password="p")],
        term="ssh",
        log=LogMode.QUIET,
    )
    connections = MagicMock()
    connections.term = "ssh"
    connections.proxy_hops = []
    connections.ssh = AsyncMock(return_value=conn)
    host._session_mgr._connections = connections
    return host


def channel_manager(conn: "FakeConn | None" = None, *, element: str = "box") -> SessionManager:
    """The session manager of :func:`channel_host`."""
    return channel_host(conn, element=element)._session_mgr
