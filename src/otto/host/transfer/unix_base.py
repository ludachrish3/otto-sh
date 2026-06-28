"""Shared unix scaffolding for SSH/Telnet-based file transfer backends.

:class:`UnixFileTransfer` holds the common ``__init__`` fields (connections,
exec_cmd), the :meth:`_warmup_for_transfer` helper, and a no-op default
:meth:`prepare`.  Protocol-specific subclasses
(:class:`~otto.host.transfer.ScpFileTransfer`,
:class:`~otto.host.transfer.SftpFileTransfer`,
:class:`~otto.host.transfer.FtpFileTransfer`,
:class:`~otto.host.transfer.NcFileTransfer`) inherit from this and override
:meth:`prepare` when they need a real probe.
"""

import asyncio
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..connections import ConnectionManager

from ...utils import CommandStatus
from .base import BaseFileTransfer


class UnixFileTransfer(BaseFileTransfer):
    """Common unix scaffolding shared by all SSH/Telnet transfer backends.

    Stores the two mandatory unix fields (``_connections``, ``_exec_cmd``),
    provides ``_warmup_for_transfer`` (concurrent strategy-probe + pool
    warming), and supplies a no-op :meth:`prepare` that subclasses override
    when they need a real probe.
    """

    host_families = frozenset({"unix"})

    def __init__(
        self,
        connections: "ConnectionManager",
        name: str,
        exec_cmd: Callable[..., Coroutine[Any, Any, CommandStatus]],
        max_filename_len: int = 255,
    ) -> None:
        super().__init__(name=name, max_filename_len=max_filename_len)
        self._connections = connections
        self._exec_cmd = exec_cmd

    async def prepare(self) -> None:
        """No-op default — subclasses override to run a strategy probe."""
        return

    async def _warmup_for_transfer(self, file_count: int) -> None:
        """Probe strategies and pre-open exec sessions for the upcoming
        transfer — all concurrently.

        Without this, the first transfer on a cold telnet host pays its
        handshakes serially: strategy-probe → (per-file) exec-session-open.
        By firing them together we collapse wall-clock cost from ~N
        handshakes to ~max(handshakes).

        ``file_count`` sessions are pre-opened on telnet so each concurrent
        ``nc -l`` can pull a warm session from the pool.  On SSH the exec
        path uses channels over the live connection, so no pool warming is
        needed and we just run :meth:`prepare`.

        Safe to call multiple times; :meth:`prepare` is idempotent and
        extra ``_exec_cmd('true')`` calls are cheap on warm sessions.
        """
        tasks: list[Coroutine[Any, Any, Any]] = [self.prepare()]
        if self._connections.term == "telnet":
            tasks.extend(self._exec_cmd("true") for _ in range(max(file_count, 1)))
        await asyncio.gather(*tasks, return_exceptions=True)
