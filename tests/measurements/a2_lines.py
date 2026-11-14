"""The dry run's lines: a local shell on a pty, typed into and read like a host's session."""

import asyncio
import contextlib
import os
import re
from pathlib import Path

from pydantic import SecretStr

from otto.host.secret_wire import reveal
from tests._fixtures.controlling_pty import ControllingPty, spawn_on_controlling_pty

READ_CHUNK = 65536


class PtyLine:
    """A local shell on a controlling pty: ``send`` and ``expect`` over the controller fd.

    The reader is the event loop's own (``add_reader``): no thread. Output
    is kept in one buffer; ``expect`` returns up to and including the match
    and keeps the rest for the next call, as a session's reader does.
    """

    def __init__(self, pty: ControllingPty) -> None:
        self._pty = pty
        self._buffer = ""
        self._arrived = asyncio.Event()
        self._closed = False
        os.set_blocking(pty.controller, False)
        asyncio.get_running_loop().add_reader(pty.controller, self._read)

    @classmethod
    def spawn(cls, argv: "list[str]", *, env: "dict[str, str]", cwd: "Path | str") -> "PtyLine":
        """Start *argv* on a fresh pty and return its line."""
        return cls(spawn_on_controlling_pty(argv, env=env, cwd=cwd))

    @property
    def pid(self) -> int:
        """The pid of the program started on the pty."""
        return self._pty.proc.pid

    def _read(self) -> None:
        try:
            data = os.read(self._pty.controller, READ_CHUNK)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if not data:
            self._stop_reading()
        else:
            self._buffer += data.decode(errors="replace")
        self._arrived.set()

    def _stop_reading(self) -> None:
        if not self._closed:
            self._closed = True
            with contextlib.suppress(ValueError, OSError):
                asyncio.get_running_loop().remove_reader(self._pty.controller)

    async def send(self, text: "str | SecretStr") -> None:
        """Type *text* at the terminal; a SecretStr as its value and a newline."""
        os.write(self._pty.controller, reveal(text).encode())

    async def expect(self, pattern: "re.Pattern[str]", *, timeout: float) -> str:
        """Return the output up to and including *pattern*'s first match."""

        async def wait() -> str:
            while True:
                found = pattern.search(self._buffer)
                if found is not None:
                    out, self._buffer = self._buffer[: found.end()], self._buffer[found.end() :]
                    return out
                if self._closed:
                    raise EOFError("the shell closed its terminal")
                self._arrived.clear()
                await self._arrived.wait()

        return await asyncio.wait_for(wait(), timeout)

    def close(self) -> None:
        """Stop reading, kill the shell's group and close the pty."""
        self._stop_reading()
        self._pty.close()
