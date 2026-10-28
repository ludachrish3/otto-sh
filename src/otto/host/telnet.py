"""
Telnet client for remote host connections.

TelnetClient handles the transport-level concerns: opening a telnet connection,
negotiating protocol options (echo suppression), authenticating, and optionally
sending NAWS (window-size) updates on SIGWINCH so remote TUIs reflow like
they do under SSH. After login the reader/writer streams are handed off to a
TelnetSession for command execution.
"""

import asyncio
import logging
import re
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
)
from weakref import WeakSet

from pydantic import SecretStr
from typing_extensions import Self

from ..models.base import as_secret
from .options import TelnetOptions
from .secret_wire import reveal

logger = logging.getLogger(__name__)

TELNET_EOL = "\r"
"""The line end otto types into a telnet shell session, a console's included.

CR alone, never CRLF: the CR executes the line in readline raw mode, and a
trailing LF would add a stray empty prompt (an extra ``>>> `` in a Python
REPL) that the next expect matches before the real output. In canonical mode
``icrnl`` maps the CR to the newline the shell expects.

Not used for a telnet daemon's own login: :meth:`TelnetClient.login` answers
the login and password prompts with CRLF, the line end a telnet login is
written to accept, before any shell exists."""


def telnet_line_ends(text: str) -> str:
    r"""Return *text* with every ``\n`` (or ``\r\n``) line end as :data:`TELNET_EOL`."""
    return re.sub(r"\r?\n", TELNET_EOL, text)


def open_connection_default(name: str) -> Any:
    """Return the default :func:`telnetlib3.open_connection` gives keyword *name*.

    For a caller sizing a wait otto leaves to telnetlib3 (its option
    negotiation, ``connect_maxwait``) without restating the library's value.
    """
    import inspect

    from telnetlib3 import open_connection

    return inspect.signature(open_connection).parameters[name].default


async def open_telnet_connection(*args: Any, **kwargs: Any) -> Any:
    """Lazy, patchable wrapper around :func:`telnetlib3.open_connection`.

    Kept as a module-level seam (tests monkeypatch it) while deferring the
    heavy ``telnetlib3`` import to connect-time — see
    ``tests/unit/host/test_lazy_network_imports.py``.

    Every telnet stream otto opens comes through here, so this is where the
    NVT decoding is installed (:mod:`otto.host.nvt`): the client class is the
    one ``open_connection`` would have picked (or the one a host's
    ``telnet_options.extra`` names), with a reader that decodes ``CR NUL``
    before anything reads it.
    """
    from telnetlib3 import open_connection

    from .nvt import nvt_client_factory

    base = kwargs.get("client_factory")
    if base is None or isinstance(base, type):
        kwargs["client_factory"] = nvt_client_factory(base)
    else:
        logger.warning(
            f"telnet client_factory {base!r} is not a class, so otto cannot add NVT "
            f"decoding: a bare CR from this server will arrive as CR NUL"
        )
    return await open_connection(*args, **kwargs)


# Registry of TelnetClients that want SIGWINCH-driven NAWS updates. A single
# process-level handler iterates this set so multiple concurrent telnet
# sessions all reflow on resize. Weak refs so closed clients drop out cleanly.
_naws_subscribers: "WeakSet[TelnetClient]" = WeakSet()
_naws_handler_installed: bool = False

# Live single-client console transports — populated when TelnetOptions.
# single_client_console is True. Strong refs (we keep them reachable so the
# embedded test teardown can force-release a console slot a timed-out test left
# half-open) and per-process (each xdist worker owns its own). See
# abort_console_transports().
_live_console_transports: set[Any] = set()


def send_naws(writer: Any, cols: int, rows: int, *, label: str) -> None:
    """Transmit a NAWS subnegotiation (RFC 1073) with the given terminal size.

    Sends the raw ``IAC SB NAWS <cols> <rows> IAC SE`` frame through
    the writer's ``send_iac`` rather than ``write``: ``write``
    escapes every 0xFF in the buffer to ``IAC IAC`` on the assumption the
    caller is passing literal payload bytes, which would corrupt the framing
    bytes of a raw command. The NAWS *payload itself* still doubles any
    literal 0xFF per telnet framing, and negative sizes clamp to 0. A failed
    write is logged against *label* and ignored: NAWS is best-effort. A
    ``None`` *writer* (not connected) sends nothing. Every telnet-framed
    client (the telnet and console clients) resizes through this.
    """
    if writer is None:
        return
    from telnetlib3.telopt import IAC, NAWS, SB, SE

    payload = struct.pack(">HH", max(0, cols), max(0, rows))
    payload = payload.replace(IAC, IAC + IAC)
    frame = IAC + SB + NAWS + payload + IAC + SE
    try:
        writer.send_iac(frame)
    except Exception as exc:  # noqa: BLE001 — best-effort NAWS write, any failure is non-fatal
        logger.debug(f"NAWS write failed for {label}: {exc}")


async def negotiate_no_echo(writer: Any, timeout: float) -> None:
    """Ask the server to stop echoing and wait up to *timeout* for it to agree.

    The non-interactive opener's step, on every telnet-framed client: with
    the remote echo on, each command would come back inside its own output.
    A negotiation that does not complete in time is logged and the
    connection proceeds anyway.
    """
    from telnetlib3.telopt import DONT, ECHO

    writer.iac(DONT, ECHO)
    try:
        await asyncio.wait_for(writer.wait_for(remote={"ECHO": False}), timeout=timeout)
    except asyncio.TimeoutError:
        logger.debug("ECHO negotiation timed out — proceeding anyway")


def _register_console_transport(transport: Any) -> None:
    """Track a live single-client console transport (no-op if None)."""
    if transport is not None:
        _live_console_transports.add(transport)


def _unregister_console_transport(transport: Any) -> None:
    """Stop tracking a transport (no-op if absent)."""
    _live_console_transports.discard(transport)


def abort_console_transports() -> int:
    """Abort every tracked single-client console transport synchronously.

    Releases each FD (and the server-side single-client slot) via the
    transport's own synchronous ``abort()`` — no event loop required, so this
    works even after a pytest-timeout signal aborts a test before its async
    ``close()`` could run. Best-effort and idempotent; returns the count
    aborted. Per-process.
    """
    count = 0
    for transport in list(_live_console_transports):
        try:
            transport.abort()
            count += 1
        except Exception as exc:  # noqa: PERF203,BLE001 — per-item resilience, best-effort transport abort
            logger.debug(f"Failed to abort console transport (non-fatal): {exc}")
    _live_console_transports.clear()
    return count


def _sigwinch_fanout() -> None:
    """SIGWINCH handler: push a fresh NAWS update to every subscribed client."""
    cols, rows = shutil.get_terminal_size((80, 24))
    for client in list(_naws_subscribers):
        try:
            client._send_naws(cols, rows)  # noqa: SLF001 — intra-package access to TelnetClient._send_naws for SIGWINCH fanout
        except Exception as exc:  # noqa: PERF203,BLE001 — per-item resilience, best-effort NAWS cleanup  # pragma: no cover
            logger.debug(f"NAWS push failed for {client.host}: {exc}")


def _install_sigwinch_handler() -> None:
    global _naws_handler_installed  # noqa: PLW0603 — module-level singleton/cache
    if _naws_handler_installed or sys.platform == "win32":
        return
    try:
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGWINCH, _sigwinch_fanout)
        _naws_handler_installed = True
    except (NotImplementedError, RuntimeError) as exc:  # pragma: no cover
        logger.debug(f"SIGWINCH handler not installed: {exc}")


def _uninstall_sigwinch_handler_if_unused() -> None:
    global _naws_handler_installed  # noqa: PLW0603 — module-level singleton/cache
    if not _naws_handler_installed or _naws_subscribers:
        return
    try:
        loop = asyncio.get_running_loop()
        loop.remove_signal_handler(signal.SIGWINCH)
    except (NotImplementedError, RuntimeError):  # pragma: no cover
        pass
    _naws_handler_installed = False


@dataclass(eq=False)
class TelnetClient:
    """Telnet transport for a single remote host.

    Manages the asyncio reader/writer streams for one telnet connection,
    handling option negotiation (echo suppression, NAWS) and authentication.
    After :meth:`connect` the streams are consumed by a
    :class:`~otto.host.session.TelnetSession`, which owns command framing and
    output capture. Concurrent callers should not share a single
    ``TelnetClient`` instance — use :class:`~otto.host.session.SessionManager`'s
    exec pool, which opens additional clients as needed.
    """

    host: str
    user: str
    password: SecretStr | None = field(repr=False)
    """The login password, or ``None`` to answer the prompt with an empty line.

    A ``SecretStr``, unwrapped only inside the write that types it, and never
    shown by ``repr()``. A plain ``str`` given here is wrapped on construction."""

    options: TelnetOptions = field(default_factory=TelnetOptions)
    """Connection options. Default reproduces otto's historical behavior."""

    prompt: str | None = None
    """Shell prompt string the device displays after each command (e.g. '$ ' or '# ').
    Used during login to confirm authentication succeeded."""

    connect_port: int | None = None
    """Override port for ConnectionManager's tunneled case. When None,
    ``options.port`` is used. Keeps tunnel port-forwarding transparent
    to the TelnetOptions carried through the rest of the stack."""

    reader: Any = field(init=False, repr=False, default=None)
    writer: Any = field(init=False, repr=False, default=None)

    def __post_init__(self) -> None:
        self.password = as_secret(self.password)

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
        # Detailed entry log so a future-embedded-OS bring-up has the
        # parameters that drove the connect right next to whatever went wrong.
        logger.debug(
            f"TelnetClient.connect host={self.host}:{port} "
            f"user={self.user!r} login={self.options.login} "
            f"login_prompt={self.options.login_prompt!r} "
            f"interactive={interactive}"
        )

        start = asyncio.get_event_loop().time()

        open_kwargs = self.options._open_kwargs()  # noqa: SLF001 — intra-package access to TelnetOptions._open_kwargs
        open_kwargs["port"] = port  # override for tunneled case
        self.reader, self.writer = await open_telnet_connection(
            self.host,
            **open_kwargs,  # type: ignore[arg-type]
        )
        if self.options.single_client_console:
            _register_console_transport(getattr(self.writer, "transport", None))

        if not interactive:
            await negotiate_no_echo(self.writer, self.options.echo_negotiation_timeout)
        if self.options.login:
            logger.debug(f"Performing telnet login for {self.host}")
            await self.login()
        else:
            logger.debug(f"Skipping telnet login for {self.host} (options.login=False)")

        # Hook up NAWS after login so banners/MOTD don't get mixed with
        # subnegotiation bytes on the wire.
        if self.options.auto_window_resize and sys.stdin.isatty():
            cols, rows = shutil.get_terminal_size((self.options.cols, self.options.rows))
            self._send_naws(cols, rows)
            _naws_subscribers.add(self)
            _install_sigwinch_handler()

        elapsed = asyncio.get_event_loop().time() - start
        logger.debug(f"Telnet connected to {self.host}:{port} in {elapsed:.2f}s")

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
        send_naws(self.writer, cols, rows, label=str(self.host))

    async def login(self) -> None:
        """Send credentials through an established telnet stream.

        Readiness after the password is *not* confirmed here. When no
        ``prompt`` is configured, ``login()`` returns as soon as the password
        is written and the session's marker handshake
        (``ShellSession._ensure_initialized``, which
        runs immediately after) is the deterministic readiness check — it
        reads through any banner/MOTD to a unique sentinel and is bounded by
        a timeout that surfaces a bad-credential login as a clear error.
        """
        prompt_delim = self.options.login_prompt
        # Wait for the login prompt ("login:", "Username:", etc.) — any line ending in the delimiter.  # noqa: E501 — long inline comment
        await self.reader.readuntil(prompt_delim)
        self.writer.write(self.user.encode() + b"\r\n")

        # Wait for the password prompt ("Password:", "password:", etc.)
        await self.reader.readuntil(prompt_delim)
        self.writer.write(reveal(self.password, "\r\n").encode())

        if self.prompt is not None:
            # Opt-in fast path: a caller with a stable prompt can have login
            # confirmed here directly. Otherwise the marker handshake does it.
            await self.reader.readuntil(self.prompt.encode())

    @property
    def alive(self) -> bool:
        """Whether the underlying TCP transport is still usable.

        ``close()`` clears ``writer``/``reader`` to None, and the asyncio
        writer reports ``is_closing()`` after a peer-initiated EOF. Either
        signal means the next read/write would fail, so callers (notably
        ``ConnectionManager.telnet()``) should treat the client as stale.
        """
        if self.writer is None or self.reader is None:
            return False
        return not self.writer.is_closing()

    async def close(self) -> None:
        """Gracefully close the telnet connection."""
        _naws_subscribers.discard(self)
        _uninstall_sigwinch_handler_if_unused()
        if self.writer:
            # ``writer.close()`` schedules a graceful close; the underlying
            # ``_SelectorSocketTransport`` releases its socket only when the
            # event loop next drains its callbacks. When ``connect()`` is
            # cancelled mid-handshake (e.g. caller-side ``wait_for`` timeout),
            # control returns to the test before the loop gets that chance —
            # the socket then surfaces later as a ``ResourceWarning`` that
            # pytest's ``[unraisable]`` plugin escalates into an end-of-session
            # failure. ``transport.abort()`` skips the graceful drain and
            # releases the FD synchronously, which is fine for a half-built
            # connection we're discarding.
            transport = getattr(self.writer, "transport", None)
            _unregister_console_transport(transport)
            self.writer.close()
            if transport is not None:
                transport.abort()

        self.writer = None
        self.reader = None

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()
