"""
Telnet client for remote host connections.

TelnetClient handles the transport-level concerns: opening a telnet connection,
negotiating protocol options (echo suppression), authenticating, and optionally
sending NAWS (window-size) updates on SIGWINCH so remote TUIs reflow like
they do under SSH. After login the reader/writer streams are handed off to a
TelnetSession for command execution.
"""

import asyncio
import contextlib
import logging
import re
import shutil
import signal
import struct
import sys
from collections.abc import Awaitable, Generator
from dataclasses import (
    dataclass,
    field,
)
from typing import (
    Any,
)
from weakref import WeakSet

from pydantic import SecretStr
from typing_extensions import Self, override

from ..errors import OttoError
from ..models.base import as_secret
from ..utils import WaitTimeoutError, wait_for_async
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


@dataclass
class _OpenRecord:
    """What one open has built so far: its protocol, then its transport and socket.

    telnetlib3's opener returns nothing until negotiation ends, so this is
    how the opener reaches the stream when it is cancelled or fails first.
    """

    protocol: Any = None
    transport: Any = None
    sock: Any = None


def _owned_factory(factory: Any, *, single_client: bool, record: _OpenRecord) -> Any:
    """Wrap a telnetlib3 protocol factory so the opener can always release what it builds.

    The protocol is recorded as soon as it is built. Its ``connection_made``
    first records the transport and socket, then runs the protocol's own
    setup, then registers the transport when the stream is single-client,
    so the emergency sweep can abort it from the moment it exists. The
    registration happens even when that setup raises, since the transport is
    live either way.
    """

    def build(*args: Any, **kwargs: Any) -> Any:
        protocol = factory(*args, **kwargs)
        record.protocol = protocol
        setup = protocol.connection_made

        def connection_made(transport: Any) -> None:
            record.transport = transport
            record.sock = _transport_socket(transport)
            try:
                setup(transport)
            finally:
                if single_client:
                    _register_console_transport(transport)

        protocol.connection_made = connection_made
        return protocol

    return build


async def _release_failed_open(record: _OpenRecord, *, single_client: bool) -> None:
    """Release what an open that was cancelled or failed had already built.

    With no transport, nothing was opened. A transport asyncio is already
    closing (a cancellation while the connection was being set up) cannot be
    drained, so it is aborted and its release observed. A live transport is
    mid-negotiation with a guest that may already be sending its banner, so
    it gets the same release as any other stream, drain included. One whose
    protocol never built a reader and writer is aborted.

    Any error, not only a cancellation, releases a live transport this way,
    so the error reaches the caller up to :data:`CONSOLE_QUIESCE_BUDGET`
    later.
    """
    transport = record.transport
    if transport is None:
        return
    reader = getattr(record.protocol, "reader", None)
    writer = getattr(record.protocol, "writer", None)
    try:
        closing = bool(transport.is_closing())
    except Exception:  # noqa: BLE001 — a transport that cannot answer is not drained
        closing = True
    if reader is None or writer is None or closing:
        await _abort_and_observe(writer, transport, record.sock)
    else:
        _stop_negotiation(record.protocol)
        await release_telnet_stream(
            reader, writer, transport, single_client=single_client, sock=record.sock
        )


def _stop_negotiation(protocol: Any) -> None:
    """Cancel telnetlib3's pending negotiation timers on *protocol*.

    The cancellation that ended the open also cancelled the future those
    timers settle, so one firing during the drain would raise
    ``InvalidStateError`` into the loop. telnetlib3's own ``connection_lost``
    cancels them first in the same way. ``_tasks`` is private telnetlib3 API,
    coupled to the pinned version, hence the guard. Never raises.
    """
    try:
        for handle in list(getattr(protocol, "_tasks", ())):
            handle.cancel()
    except Exception as exc:  # noqa: BLE001 — private-API coupling; a release must not fail on it
        logger.debug(f"could not stop telnet negotiation timers (non-fatal): {exc!r}")


async def open_telnet_connection(*args: Any, single_client: bool = False, **kwargs: Any) -> Any:
    """Lazy, patchable wrapper around :func:`telnetlib3.open_connection`.

    Kept as a module-level seam (tests monkeypatch it) while deferring the
    heavy ``telnetlib3`` import to connect-time — see
    ``tests/unit/host/test_lazy_network_imports.py``.

    Every telnet stream otto opens comes through here, so this is where the
    NVT decoding is installed (:mod:`otto.host.nvt`): the client class is the
    one ``open_connection`` would have picked (or the one a host's
    ``telnet_options.extra`` names), with a reader that decodes ``CR NUL``
    before anything reads it.

    It is also where the stream's life begins. With *single_client* (the
    stream holds a console's only slot), the transport is registered for the
    emergency sweep from ``connection_made`` on. An open that is cancelled or
    fails before telnetlib3 hands the stream back releases whatever it had
    built (see ``_release_failed_open``), then re-raises. *single_client*
    is otto's, never passed to telnetlib3.

    The library's opener runs as its own task, observed through
    ``_await_within``, so this is a real cancellation boundary: a
    cancellation of the caller always ends here in that release and
    re-raises, even when telnetlib3 has dropped it (its own
    ``asyncio.wait_for`` around the TCP setup returns the result instead on
    Python 3.10 and 3.11, when both land in the same loop turn).
    """
    from telnetlib3 import open_connection

    from .nvt import nvt_client_factory

    base = kwargs.get("client_factory")
    if base is None or isinstance(base, type):
        factory = nvt_client_factory(base)
    else:
        logger.warning(
            f"telnet client_factory {base!r} is not a class, so otto cannot add NVT "
            f"decoding: a bare CR from this server will arrive as CR NUL"
        )
        factory = base
    record = _OpenRecord()
    kwargs["client_factory"] = _owned_factory(factory, single_client=single_client, record=record)
    try:
        opening = await _await_within(open_connection(*args, **kwargs), None)
        assert opening is not None  # noqa: S101 — internal invariant: with no timeout only the open ending returns
        opened = opening.result()
    except (asyncio.CancelledError, Exception):
        await _release_failed_open(record, single_client=single_client)
        raise
    # The protocol's hook holds the record and the record holds the
    # protocol; clearing it breaks that cycle once the stream is the caller's.
    record.protocol = record.transport = record.sock = None
    return opened


# Registry of TelnetClients that want SIGWINCH-driven NAWS updates. A single
# process-level handler iterates this set so multiple concurrent telnet
# sessions all reflow on resize. Weak refs so closed clients drop out cleanly.
_naws_subscribers: "WeakSet[TelnetClient]" = WeakSet()
_naws_handler_installed: bool = False

# Live single-client console transports — populated for every stream that
# single_client() says holds a single-client slot. Strong refs (we keep them
# reachable so the embedded test teardown can force-release a console slot a
# timed-out test left half-open) and per-process (each xdist worker owns its
# own). See abort_console_transports().
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
    agreed = await _await_within(writer.wait_for(remote={"ECHO": False}), timeout)
    if agreed is None:
        logger.debug("ECHO negotiation timed out — proceeding anyway")
    else:
        agreed.result()


def _register_console_transport(transport: Any) -> None:
    """Track a live single-client console transport (no-op if None)."""
    if transport is not None:
        _live_console_transports.add(transport)


def _unregister_console_transport(transport: Any) -> None:
    """Stop tracking a transport (no-op if absent)."""
    _live_console_transports.discard(transport)


def abort_console_transports() -> int:
    """Abort every tracked single-client console transport synchronously.

    Calls each transport's own synchronous ``abort()``, so it needs no running
    event loop and works after a pytest-timeout signal stops a test before
    its async ``close()`` could run. The abort *schedules* the FD's release:
    with a running loop the FD (and the server-side single-client slot) goes
    on the loop's next turn; with a stopped or closed loop it goes when the
    transport is garbage-collected or the process exits. Best-effort and
    idempotent; returns the count aborted. Per-process.
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


CONSOLE_QUIESCE_QUIET_WINDOW: float = 0.2
"""How long a single-client console's line must stay silent before otto closes it.

A Zephyr ``shell_telnet`` guest tears its backend down when a send fails
because its client has gone, so otto reads and discards the guest's queued
output until the line has been quiet this long, and only then closes."""

CONSOLE_QUIESCE_BUDGET: float = 2.0
"""The most one release spends waiting for reads to finish, draining and flushing.

A line that never goes quiet, a read that never finishes or a socket that
never flushes is aborted when this runs out."""


def single_client(term: str, telnet_options: TelnetOptions | None = None) -> bool:
    """Whether a stream on *term* holds a single-client slot.

    A single-client stream is registered with the emergency sweep when it
    opens, and drained to quiet before every release; the release's time
    budget counts that drain. Those are one decision, made here:

    - ``console``: always, since a console server serves one client.
    - ``telnet``: when *telnet_options* sets ``single_client_console``.

    Any other term: never. The command frame's ``single_client_console`` is
    diagnostic only and is not consulted.
    """
    if term == "console":
        return True
    if term == "telnet":
        return telnet_options is not None and telnet_options.single_client_console
    return False


_ABORT_GRACE: float = 0.1
"""How long an abort waits to observe the socket's release before it gives up.

asyncio's ``abort()`` closes the socket on the loop's next turn, so a few
turns are enough. A release not seen in this time leaves the transport
registered, for the emergency sweep."""


class ReadGateClosed(OttoError, ConnectionError):  # noqa: N818 — interface-fixed name: a closed gate, not an "error" the caller made
    """A read of a stream whose release has begun: the connection is gone.

    A :class:`ConnectionError`, so a reader handles it as a dropped line.
    """


class ReadGate:
    """Counts a stream's in-flight reads, and admits no new one once its release begins.

    Reads run inline in their callers' tasks, so a close in another task
    cannot cancel them, and telnetlib3's own waiter is no reliable signal (it
    clears when data arrives, before the reading task resumes, and a buffered
    read never sets it). So otto counts its own reads: every ordinary read of
    the stream runs inside :meth:`reading`, and the release calls
    :meth:`wait_idle` before it reads the stream itself. Without that, its
    read would collide with the caller's and fail with telnetlib3's "already
    waiting" error.
    """

    def __init__(self) -> None:
        self.in_flight: int = 0
        self.closed: bool = False

    @contextlib.contextmanager
    def reading(self) -> Generator[None, None, None]:
        """Run one read inside the gate; raise :class:`ReadGateClosed` once it is closed."""
        self.check_open()
        self.in_flight += 1
        try:
            yield
        finally:
            self.in_flight -= 1

    def check_open(self) -> None:
        """Raise :class:`ReadGateClosed` once the gate is closed: the stream is being released."""
        if self.closed:
            raise ReadGateClosed("the stream is being released")

    def close(self) -> None:
        """Admit no new read from now on. Synchronous and idempotent.

        A read already in flight keeps its reader until it finishes.
        """
        self.closed = True

    async def wait_idle(self, deadline: float) -> bool:
        """Close the gate, then wait until no read is in flight.

        *deadline* is absolute, on the running loop's clock. Returns True once
        idle, or False when the deadline passes first. Idempotent. Admission
        is shut before the first await, so no read can enter between the
        close and the wait.
        """
        self.close()
        loop = asyncio.get_running_loop()
        try:
            await wait_for_async(
                lambda: self.in_flight == 0,
                max(0.0, deadline - loop.time()),
                interval=0.02,
                on_timeout="a read of the stream did not finish before its release",
            )
        except WaitTimeoutError:
            return False
        return True


_SOCKET_UNKNOWN: Any = object()
"""What :func:`_transport_socket` answers when the transport fails to say.

Not None: a failed lookup is no evidence that the transport has no socket,
so a release under it is never observed and the transport stays registered."""


def _transport_socket(transport: Any) -> Any:
    """Return the socket under *transport*, captured before a release so it can be observed.

    None with no transport and for a transport without a socket (an SSH
    channel, a test double); :data:`_SOCKET_UNKNOWN` for one that fails to
    answer. Never raises.
    """
    if transport is None:
        return None
    try:
        return transport.get_extra_info("socket")
    except Exception as exc:  # noqa: BLE001 — an unanswered lookup is recorded as unknown, never raised
        logger.debug(f"could not read the telnet transport's socket (non-fatal): {exc!r}")
        return _SOCKET_UNKNOWN


def _locally_released(transport: Any, sock: Any) -> bool:
    """Whether otto's end of *transport* is released: flushed, and its socket closed.

    With no socket to observe (an SSH channel, a test double), an empty
    buffer on a closing transport is the best signal there is. True with no
    transport. A socket that could not be looked up
    (:data:`_SOCKET_UNKNOWN`) may still be open, so it never counts as
    released. A transport whose buffer or closing query fails, or a socket
    whose ``fileno()`` fails, counts as not released either, so a caller
    polling this never raises from it. The transport then stays registered,
    for the emergency sweep.
    """
    if transport is None:
        return True
    if sock is _SOCKET_UNKNOWN:
        return False
    try:
        if transport.get_write_buffer_size() != 0:
            return False
        if sock is not None:
            return sock.fileno() == -1
        return bool(transport.is_closing())
    except Exception as exc:  # noqa: BLE001 — an unanswerable transport is not observed released
        logger.debug(f"could not observe the telnet stream's release (non-fatal): {exc!r}")
        return False


async def _await_local_release(transport: Any, sock: Any, deadline: float) -> bool:
    """Wait for :func:`_locally_released` until the absolute *deadline*; False if it never is.

    telnetlib3 reports its writer closed before asyncio has flushed and closed
    the socket, and asyncio closes the socket on a later loop turn, so the
    release is observed, never assumed.
    """
    if transport is None:
        return True
    loop = asyncio.get_running_loop()
    try:
        await wait_for_async(
            lambda: _locally_released(transport, sock),
            max(0.0, deadline - loop.time()),
            interval=0.02,
            on_timeout="the telnet stream's socket was not released",
        )
    except WaitTimeoutError:
        return False
    return True


def abort_telnet_stream(writer: Any, transport: Any) -> None:
    """Release a stream now, with no drain: close the writer and abort the transport.

    Synchronous, for a caller with no time to wait. It does not unregister the
    transport: only an observed release does that. Never raises.
    """
    if writer is not None:
        try:
            writer.close()
        except Exception as exc:  # noqa: BLE001 — best-effort close ahead of the abort
            logger.debug(f"telnet writer close failed during abort (non-fatal): {exc}")
    if transport is not None:
        try:
            transport.abort()
        except Exception as exc:  # noqa: BLE001 — best-effort abort; the registry still holds it
            logger.debug(f"telnet transport abort failed (non-fatal): {exc}")


def _unregister_if_released(transport: Any, sock: Any) -> None:
    """Loop callback: unregister *transport* if its release is observed now."""
    if _locally_released(transport, sock):
        _unregister_console_transport(transport)


def _abort_and_unregister_soon(writer: Any, transport: Any, sock: Any) -> None:
    """Abort a stream synchronously, and unregister it once its release is seen.

    asyncio's ``abort()`` closes the socket from a callback it schedules
    first, so a check scheduled after it runs once the socket is closed. With
    no running loop the transport stays registered: the emergency sweep owns
    it. Never raises.
    """
    abort_telnet_stream(writer, transport)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.call_soon(_unregister_if_released, transport, sock)


async def _abort_and_observe(writer: Any, transport: Any, sock: Any) -> None:
    """Abort a stream, wait briefly to see its release, then unregister it.

    The one async abort path: every async caller that must abort routes here.

    A transport whose release is not seen within :data:`_ABORT_GRACE` stays
    registered, so the emergency sweep still owns it. A cancellation during
    the grace propagates at once and leaves it registered too.
    """
    abort_telnet_stream(writer, transport)
    loop = asyncio.get_running_loop()
    if await _await_local_release(transport, sock, loop.time() + _ABORT_GRACE):
        _unregister_console_transport(transport)


async def _await_within(
    awaitable: Awaitable[Any], timeout: float | None
) -> "asyncio.Future[Any] | None":
    """Await *awaitable* for up to *timeout*: its finished future, or None on a timeout.

    A *timeout* of None waits for as long as *awaitable* runs, so only its
    end or the caller's cancellation ends the wait.

    The cancel-safe replacement for ``asyncio.wait_for`` on a stream. On
    Python 3.10 and 3.11, ``wait_for`` returns the inner result when the
    inner await completes in the same loop turn the caller is cancelled, and
    the cancellation is lost: a release keeps draining instead of aborting,
    a connect carries on with its stream installed. Here the caller's
    cancellation always propagates. On a timeout or a cancellation the inner
    await is cancelled and awaited, so nothing stays pending on the stream.
    It is cancelled again for as long as it keeps running (see
    :func:`_cancel_until_done`). The caller reads the outcome with
    ``.result()``, which re-raises the inner await's own error.
    """
    inner = asyncio.ensure_future(awaitable)
    try:
        done, _ = await asyncio.wait({inner}, timeout=timeout)
    except asyncio.CancelledError:
        await _cancel_until_done(inner)
        raise
    if not done:
        await _cancel_until_done(inner)
        if inner.cancelled():
            return None
    return inner


_RECANCEL_INTERVAL: float = 0.05
"""How long :func:`_cancel_until_done` waits on a cancelled await before cancelling it again."""


async def _cancel_until_done(inner: "asyncio.Future[Any]") -> None:
    """Cancel *inner* until it has finished, then retrieve its outcome.

    One cancel is not always enough: telnetlib3's opener runs its TCP setup
    under ``asyncio.wait_for``, which on Python 3.10 and 3.11 can drop a
    cancel and carry on into its negotiation wait (up to
    ``connect_maxwait``). So *inner* is cancelled again every
    :data:`_RECANCEL_INTERVAL` while it keeps running. A cancellation of the
    caller that arrives meanwhile is held until *inner* has finished, then
    raised, so nothing is left pending on the stream. An *inner* whose own
    cancellation cleanup awaits longer than :data:`_RECANCEL_INTERVAL` is
    cancelled again mid-cleanup, so a new caller whose await cleans up that
    way must check it can bear that.
    """
    held: asyncio.CancelledError | None = None
    while not inner.done():
        inner.cancel()
        try:
            await asyncio.wait({inner}, timeout=_RECANCEL_INTERVAL)
        except asyncio.CancelledError as exc:
            held = exc
    if not inner.cancelled():
        inner.exception()  # retrieved, so the loop never reports it as unhandled
    if held is not None:
        raise held


async def _read_within(reader: Any, timeout: float) -> bytes | None:
    """One 4 KiB ``reader.read``, or None if nothing arrives within *timeout*.

    Never swallows the caller's cancellation (see :func:`_await_within`).
    """
    read = await _await_within(reader.read(4096), timeout)
    return None if read is None else read.result()


async def _drain_to_quiet(reader: Any, deadline: float) -> bool:
    """Read and discard until the line is quiet or at EOF; False if the deadline comes first.

    Only a read that times out after the full quiet window, or EOF, means
    quiet. A read cut short by the deadline proves nothing about the line.
    """
    loop = asyncio.get_running_loop()
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return False
        window = min(CONSOLE_QUIESCE_QUIET_WINDOW, remaining)
        data = await _read_within(reader, window)
        if data is None:
            return window >= CONSOLE_QUIESCE_QUIET_WINDOW
        if not data:
            return True


async def _release_gracefully(
    reader: Any,
    writer: Any,
    transport: Any,
    sock: Any,
    *,
    single_client: bool,
    reads_idle: "ReadGate | None",
    deadline: float,
) -> bool:
    """Wait for reads, drain, close and observe the release; False where it must abort."""
    if single_client and reads_idle is not None and not await reads_idle.wait_idle(deadline):
        logger.debug("a read of the console did not finish before its release; aborting")
        return False
    if single_client and not await _drain_to_quiet(reader, deadline):
        logger.debug("the console did not go quiet before its release deadline; aborting")
        return False
    writer.close()
    if not await _await_local_release(transport, sock, deadline):
        logger.debug("the telnet stream did not flush before its release deadline; aborting")
        return False
    return True


async def release_telnet_stream(
    reader: Any,
    writer: Any,
    transport: Any,
    *,
    single_client: bool,
    reads_idle: "ReadGate | None" = None,
    deadline: float | None = None,
    sock: Any = None,
) -> None:
    """Release a telnet-framed stream the caller has already taken from its owner.

    A single-client console (Zephyr ``shell_telnet``, ser2net) can wedge when
    its client goes away while it still has output queued. So for one, the
    release first waits for otto's in-flight reads (*reads_idle*), then reads
    and discards until the line has been quiet for
    :data:`CONSOLE_QUIESCE_QUIET_WINDOW` or reaches EOF. Every stream is then
    closed, and the release waits until otto's socket is flushed and closed.
    Only then is the transport dropped from the emergency registry.

    *deadline* is absolute, on the running loop's clock, and defaults to now
    plus :data:`CONSOLE_QUIESCE_BUDGET`. A deadline that runs out, any error
    and a cancellation all abort the stream instead, and the transport stays
    registered unless that abort's release is observed. A transport that
    cannot answer the release check is treated as not yet released, so its
    release waits out the deadline, then aborts, and it stays registered.
    Never raises, except ``CancelledError``, which is re-raised after the
    abort.

    *sock* is the socket the caller captured when it took the stream; None
    looks it up here.
    """
    loop = asyncio.get_running_loop()
    if deadline is None:
        deadline = loop.time() + CONSOLE_QUIESCE_BUDGET
    if sock is None:
        sock = _transport_socket(transport)
    try:
        released = await _release_gracefully(
            reader,
            writer,
            transport,
            sock,
            single_client=single_client,
            reads_idle=reads_idle,
            deadline=deadline,
        )
    except asyncio.CancelledError:
        await _abort_and_observe(writer, transport, sock)
        raise
    except Exception as exc:  # noqa: BLE001 — any failure to release gracefully aborts instead
        logger.debug(f"telnet stream release failed, aborting: {exc!r}")
        released = False
    if released:
        _unregister_console_transport(transport)
    else:
        await _abort_and_observe(writer, transport, sock)


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


@dataclass(frozen=True)
class _TakenStream:
    """A stream its client has let go of: the closer's to release, to the end.

    *sock* is captured at the take, so the release observes the socket the
    stream had then. *gate* is the stream's own read gate, already closed.
    """

    reader: Any
    writer: Any
    transport: Any
    sock: Any
    gate: ReadGate


def _set_event() -> asyncio.Event:
    """Return a set event: a client holding no stream has nothing being released."""
    event = asyncio.Event()
    event.set()
    return event


@dataclass(eq=False)
class _StreamHolder:
    """The telnet-framed stream a client holds, and how it is installed, taken and released.

    Both telnet-framed clients (:class:`TelnetClient` and
    :class:`~otto.host.console.ConsoleClient`) own their stream through this,
    so these rules are decided once:

    - a dial first waits for the release of a stream the client let go of
      (:meth:`_await_prior_release`), then installs the new stream, a fresh
      read gate and a pending release together (:meth:`_install`);
    - a closer takes the stream synchronously (:meth:`_take`), so exactly
      one caller owns its release and a second closer waits for it;
    - the release's end is signalled only for the stream it belonged to
      (:meth:`_release_ended`), never for a newer one;
    - :meth:`abort` releases with no drain, for a caller that cannot await.

    A client supplies only how a taken stream is released gracefully
    (:meth:`_release`).
    """

    reader: Any = field(init=False, repr=False, default=None)
    writer: Any = field(init=False, repr=False, default=None)

    _read_gate: ReadGate = field(init=False, repr=False, default_factory=ReadGate)
    """Counts the installed stream's in-flight reads; a fresh one with each stream.

    The release closes it before it reads the stream itself, so every
    ordinary read of the stream runs inside it."""

    _released: asyncio.Event = field(init=False, repr=False, default_factory=_set_event)
    """Set while the client holds no stream that is still being released.

    Cleared only when a stream is installed. Whoever takes the stream sets
    it once the release ends, however it ends, so a second closer waits on
    it and returns only once the stream is gone."""

    @property
    def read_gate(self) -> ReadGate:
        """The installed stream's read gate, which a session reading the stream borrows."""
        return self._read_gate

    async def _await_prior_release(self) -> None:
        """Wait for the release of a stream this client let go of, if one is still running.

        So the old stream is gone (a single-client console's slot free)
        before a new one dials, and that release's end cannot be mistaken
        for the new stream's.
        """
        if self.reader is None and self.writer is None:
            await self._released.wait()

    def _install(self, reader: Any, writer: Any) -> None:
        """Install a freshly opened stream, with its own read gate and a pending release.

        Called with no await between the open's return and this, so from
        here on the stream is the client's to release. An open that fails or
        is cancelled releases what it built itself, so nothing is installed
        and ``_released`` stays set.
        """
        self.reader, self.writer = reader, writer
        self._read_gate = ReadGate()
        self._released.clear()

    def _held_writer(self) -> Any:
        """Return the installed stream's writer, for an ordinary write.

        Raises :class:`ReadGateClosed`, a lost connection, once the stream
        has been taken, so a write that comes after a close took the stream
        fails like a read would, never on a nulled attribute.
        """
        self._read_gate.check_open()
        if self.writer is None:
            raise ReadGateClosed("no stream is open")
        return self.writer

    def _take(self) -> "_TakenStream | None":
        """Detach the stream, or None when none is held.

        Synchronous, so a closer takes the stream before its first await and
        a second closer finds nothing left to take: exactly one caller owns
        the stream's release. The stream's read gate closes with the take,
        so a read that starts afterwards fails as a dropped line instead of
        reading a stream that is being released or was aborted. The gate is
        returned with the stream it belongs to: a reconnect installs a fresh
        one. The socket is captured here, before any release step, so it can
        be observed. Never raises.
        """
        reader, writer = self.reader, self.writer
        if reader is None and writer is None:
            return None
        self.reader = self.writer = None
        gate = self._read_gate
        gate.close()
        transport = getattr(writer, "transport", None)
        return _TakenStream(reader, writer, transport, _transport_socket(transport), gate)

    def _release_ended(self, gate: ReadGate) -> bool:
        """Mark the release of the stream that owned *gate* as ended; True if it did.

        A no-op returning False once a newer stream is installed:
        ``_released`` then belongs to that stream, not to the one whose
        release just ended.
        """
        if self._read_gate is not gate:
            return False
        self._released.set()
        return True

    async def _release(self, taken: _TakenStream) -> None:
        """Release *taken* gracefully: the client's own part of :meth:`close`."""
        raise NotImplementedError

    def abort(self) -> None:
        """Release the stream now, synchronously, with no drain. Never raises.

        For a caller that cannot await (a manager dropped on a reboot, a
        cancelled teardown). The transport leaves the emergency registry
        only once its release is observed, a loop turn later; with no
        running loop it stays there, for the emergency sweep. A client whose
        stream is already taken is left alone.
        """
        taken = self._take()
        if taken is None:
            return
        _abort_and_unregister_soon(taken.writer, taken.transport, taken.sock)
        self._release_ended(taken.gate)

    async def close(self) -> None:
        """Take the stream, then release it through the client's :meth:`_release`.

        A close that finds nothing to take (a second closer, or a client
        that never connected) waits until the first closer's release ends.
        The taker marks the release ended however it ends, cancelled
        included.
        """
        taken = self._take()
        if taken is None:
            await self._released.wait()
            return
        try:
            await self._release(taken)
        finally:
            self._release_ended(taken.gate)


@dataclass(eq=False)
class TelnetClient(_StreamHolder):
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

        await self._await_prior_release()

        open_kwargs = self.options._open_kwargs()  # noqa: SLF001 — intra-package access to TelnetOptions._open_kwargs
        open_kwargs["port"] = port  # override for tunneled case
        reader, writer = await open_telnet_connection(
            self.host,
            single_client=single_client("telnet", self.options),
            **open_kwargs,  # type: ignore[arg-type]
        )
        self._install(reader, writer)

        if not interactive:
            await negotiate_no_echo(writer, self.options.echo_negotiation_timeout)
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
        # The stream is bound once, so a close that takes it mid-login never
        # leaves a read or write on a None attribute. Every read and write
        # checks the stream's gate, which the take closes, so the next one
        # after a take fails as a dropped line (ReadGateClosed) and no
        # credential is typed into a stream that is being released.
        reader, writer, gate = self.reader, self.writer, self._read_gate

        async def read_until(separator: bytes) -> bytes:
            with gate.reading():
                return await reader.readuntil(separator)

        prompt_delim = self.options.login_prompt
        # Wait for the login prompt ("login:", "Username:", etc.) — any line ending in the delimiter.  # noqa: E501 — long inline comment
        await read_until(prompt_delim)
        gate.check_open()
        writer.write(self.user.encode() + b"\r\n")

        # Wait for the password prompt ("Password:", "password:", etc.)
        await read_until(prompt_delim)
        # Checked inline, not through a helper: the revealed password is
        # passed straight to the library's write, never held in an otto frame.
        gate.check_open()
        writer.write(reveal(self.password, "\r\n").encode())

        if self.prompt is not None:
            # Opt-in fast path: a caller with a stable prompt can have login
            # confirmed here directly. Otherwise the marker handshake does it.
            await read_until(self.prompt.encode())

    @property
    def alive(self) -> bool:
        """Whether the underlying TCP transport is still usable.

        A close or abort takes ``writer``/``reader`` (sets them to None)
        before anything else, and the asyncio writer reports ``is_closing()``
        after a peer-initiated EOF. Either signal means the next read/write
        would fail, so callers (notably ``ConnectionManager.telnet()``)
        should treat the client as stale, and never hand out a stream that
        is mid-release.
        """
        if self.writer is None or self.reader is None:
            return False
        return not self.writer.is_closing()

    def _stop_naws(self) -> None:
        """Stop pushing window-size updates to this client."""
        _naws_subscribers.discard(self)
        _uninstall_sigwinch_handler_if_unused()

    @override
    def abort(self) -> None:
        """Stop window-size updates, then release the stream now, with no drain. Never raises."""
        self._stop_naws()
        super().abort()

    @override
    async def close(self) -> None:
        """Release the telnet connection gracefully.

        The stream is taken before the first await, then released through
        :func:`release_telnet_stream`. For a single-client console that
        drains the guest's queued output to quiet before the close, since
        closing under it can wedge the guest's shell, and every release
        waits until otto's socket is really released. A close that finds
        nothing to take (a second closer, or a client that never connected)
        waits until the first closer's release ends.
        """
        self._stop_naws()
        await super().close()

    @override
    async def _release(self, taken: _TakenStream) -> None:
        await release_telnet_stream(
            taken.reader,
            taken.writer,
            taken.transport,
            single_client=single_client("telnet", self.options),
            reads_idle=taken.gate,
            sock=taken.sock,
        )

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()
