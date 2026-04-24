"""
Telnet client for remote host connections.

TelnetClient handles the transport-level concerns: opening a telnet connection,
negotiating protocol options (echo suppression), authenticating, and optionally
sending NAWS (window-size) updates on SIGWINCH so remote TUIs reflow like
they do under SSH. After login the reader/writer streams are handed off to a
TelnetSession for command execution.
"""

import asyncio
import shutil
import signal
import struct
import sys
from dataclasses import (
    dataclass,
    field,
)
from typing import (
    Any,
    Optional,
)
from weakref import WeakSet

from telnetlib3 import (
    open_connection as open_telnet_connection,
)
from telnetlib3.telopt import DONT, ECHO, IAC, NAWS, SB, SE

from ..logger import getOttoLogger
from .options import TelnetOptions

logger = getOttoLogger()


# Registry of TelnetClients that want SIGWINCH-driven NAWS updates. A single
# process-level handler iterates this set so multiple concurrent telnet
# sessions all reflow on resize. Weak refs so closed clients drop out cleanly.
_naws_subscribers: 'WeakSet[TelnetClient]' = WeakSet()
_naws_handler_installed: bool = False


def _sigwinch_fanout() -> None:
    """SIGWINCH handler: push a fresh NAWS update to every subscribed client."""
    cols, rows = shutil.get_terminal_size((80, 24))
    for client in list(_naws_subscribers):
        try:
            client._send_naws(cols, rows)
        except Exception as exc:  # pragma: no cover - best-effort cleanup
            logger.debug(f"NAWS push failed for {client.host}: {exc}")


def _install_sigwinch_handler() -> None:
    global _naws_handler_installed
    if _naws_handler_installed or sys.platform == 'win32':
        return
    try:
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGWINCH, _sigwinch_fanout)
        _naws_handler_installed = True
    except (NotImplementedError, RuntimeError) as exc:  # pragma: no cover
        logger.debug(f"SIGWINCH handler not installed: {exc}")


def _uninstall_sigwinch_handler_if_unused() -> None:
    global _naws_handler_installed
    if not _naws_handler_installed or _naws_subscribers:
        return
    try:
        loop = asyncio.get_running_loop()
        loop.remove_signal_handler(signal.SIGWINCH)
    except (NotImplementedError, RuntimeError):  # pragma: no cover
        pass
    _naws_handler_installed = False


@dataclass(eq=False)
class TelnetClient():
    host: str
    user: str
    password: str

    options: TelnetOptions = field(default_factory=TelnetOptions)
    """Connection options. Default reproduces otto's historical behavior."""

    prompt: Optional[str] = None
    """Shell prompt string the device displays after each command (e.g. '$ ' or '# ').
    Used during login to confirm authentication succeeded."""

    connect_port: Optional[int] = None
    """Override port for ConnectionManager's tunneled case. When None,
    ``options.port`` is used. Keeps tunnel port-forwarding transparent
    to the TelnetOptions carried through the rest of the stack."""

    reader: Any = field(init=False, repr=False, default=None)
    writer: Any = field(init=False, repr=False, default=None)

    async def connect(self, interactive: bool = False) -> None:
        """Open the telnet connection, negotiate options, and log in.

        Args:
            interactive: When True, skip the ``DONT ECHO`` negotiation so the
                remote shell echoes keystrokes back — required for
                :func:`otto.host.interact.run_telnet_login` so the user sees
                what they type. Non-interactive callers (the default) get
                ``DONT ECHO`` so command echoes don't mix with captured output.
        """

        port = self.connect_port if self.connect_port is not None else self.options.port
        logger.debug(f"Connecting to {self.host} via telnet on port {port}")

        open_kwargs = self.options._open_kwargs()
        open_kwargs['port'] = port  # override for tunneled case
        self.reader, self.writer = await open_telnet_connection(
            self.host,
            **open_kwargs,  # type: ignore[arg-type]
        )

        if not interactive:
            # Tell the server not to echo our input so commands don't appear in output,
            # then wait for the negotiation to complete before proceeding to login.
            self.writer.iac(DONT, ECHO)  # type: ignore[union-attr]
            try:
                await asyncio.wait_for(
                    self.writer.wait_for(remote={"ECHO": False}),  # type: ignore[union-attr]
                    timeout=self.options.echo_negotiation_timeout,
                )
            except asyncio.TimeoutError:
                logger.debug("ECHO negotiation timed out — proceeding anyway")
        await self.login()

        # Hook up NAWS after login so banners/MOTD don't get mixed with
        # subnegotiation bytes on the wire.
        if self.options.auto_window_resize and sys.stdin.isatty():
            cols, rows = shutil.get_terminal_size((self.options.cols, self.options.rows))
            self._send_naws(cols, rows)
            _naws_subscribers.add(self)
            _install_sigwinch_handler()

        logger.debug(f"Telnet connected to {self.host}")

    def _send_naws(self, cols: int, rows: int) -> None:
        """Transmit a NAWS subnegotiation with the given terminal size.

        Sends the raw ``IAC SB NAWS <cols> <rows> IAC SE`` byte sequence
        (RFC 1073). Safe to call any time after the initial negotiation.

        Uses :meth:`telnetlib3.TelnetWriter.send_iac` rather than ``write``:
        ``write`` escapes every 0xFF in the buffer to ``IAC IAC`` on the
        assumption the caller is passing literal payload bytes, which would
        corrupt the framing bytes (``IAC SB``/``IAC SE``) of a raw command.
        The NAWS *payload itself* still escapes any literal 0xFF per RFC 1073.
        """
        if self.writer is None:
            return
        payload = struct.pack('>HH', max(0, cols), max(0, rows))
        # Double any 0xFF (IAC) bytes inside the payload per telnet framing.
        payload = payload.replace(IAC, IAC + IAC)
        frame = IAC + SB + NAWS + payload + IAC + SE
        try:
            self.writer.send_iac(frame)  # type: ignore[union-attr]
        except Exception as exc:  # pragma: no cover
            logger.debug(f"NAWS write failed for {self.host}: {exc}")

    async def login(self) -> None:
        """Send credentials through an established telnet stream."""

        prompt_delim = self.options.login_prompt
        # Wait for the login prompt ("login:", "Username:", etc.) — any line ending in the delimiter.
        await self.reader.readuntil(prompt_delim)
        self.writer.write(self.user.encode() + b'\r\n')

        # Wait for the password prompt ("Password:", "password:", etc.)
        await self.reader.readuntil(prompt_delim)
        self.writer.write(self.password.encode() + b'\r\n')

        if self.prompt is not None:
            # Wait for the user-supplied prompt to confirm login succeeded.
            await self.reader.readuntil(self.prompt.encode())
        else:
            # Drain any remaining login output (banners, MOTD, last-login lines) by
            # reading until the stream goes quiet for 1 second. We can't wait for a
            # specific string here because we don't yet know what the prompt looks like.
            while True:
                try:
                    await asyncio.wait_for(self.reader.read(4096), timeout=1.0)
                except asyncio.TimeoutError:
                    break  # silence means the shell is ready

    async def close(self) -> None:
        """Gracefully close the telnet connection."""
        _naws_subscribers.discard(self)
        _uninstall_sigwinch_handler_if_unused()
        if self.writer:
            self.writer.close()

        self.writer = None
        self.reader = None

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *args: Any):
        await self.close()
