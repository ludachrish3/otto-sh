"""The ``console`` term's transport: a serial console behind a telnet server.

:class:`ConsoleClient` opens the telnet connection exactly as
:class:`~otto.host.telnet.TelnetClient` does (same telnetlib3 open, same
``DONT ECHO`` negotiation, always registered as a single-client console
transport) and replaces the one-delimiter telnet login with a state machine
that first looks at what the line shows. A serial line is stateful: whoever
was there last is still there, and otto must never proceed on a session it
did not open. So the client nudges, classifies, and either logs in from a
login prompt it observed or raises a :class:`~otto.host.errors.ConsoleError`
that names which of five states the line was in.

Off the CLI startup import graph: imported lazily by the session manager and
the connection manager inside their ``console`` branches.
"""

import asyncio
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from pydantic import SecretBytes, SecretStr
from typing_extensions import override

from ..models.base import as_secret
from .errors import ConsoleError, ConsoleLoggedInError
from .options import ConsoleOptions
from .output_codec import OUTPUT_ENCODING, OUTPUT_ERRORS
from .secret_wire import reveal, reveal_bytes
from .telnet import (
    CONSOLE_QUIESCE_BUDGET,
    TELNET_EOL,
    ReadGate,
    _abort_and_observe,
    _read_within,
    _StreamHolder,
    _TakenStream,
    negotiate_no_echo,
    open_telnet_connection,
    release_telnet_stream,
    send_naws,
    single_client,
)

logger = logging.getLogger(__name__)

_ANSI_RE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"  # CSI: ESC [ params (0x30-0x3F) intermediates final
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC: ESC ] ... BEL or ST (e.g. a terminal title)
    r"|\x1b[()][0-9A-Za-z]"  # charset select: ESC ( B / ESC ) 0 / etc.
    r"|\x1b[@-Z\\-_]"  # other two-byte escapes
)
_QUOTE_BYTES = 200
"""How much of the line's tail an "already logged in" error quotes."""

_RESET_ROUNDS = 3
"""Ctrl-C, Ctrl-D, CR rounds ``reset`` tries: a nested shell plus an ``su`` level."""

_INTERRUPT_LANDED = re.compile(r"\^C|[$#>%] ?$")
"""What says a Ctrl-C has landed: its ``^C`` echo, or a redrawn prompt.

A tty's ISIG handling of Ctrl-C flushes its input queue, so a Ctrl-D that
arrives before the interrupt is processed is discarded and the shell only
redraws its prompt (measured on a bash login behind a serial getty, where a
Ctrl-C/Ctrl-D burst never logged out). ``reset`` therefore writes Ctrl-D
only once the line shows the interrupt was handled, waiting at most
``login_timeout`` for that and going ahead anyway when nothing shows it."""

_CLASSIFY_WINDOW = 4096
"""Bytes of tail ``_collect`` decodes/regex-matches per chunk. A chatty boot
log must not turn a raised ``login_timeout`` into an O(total²) rescan — only
the tail can ever hold a prompt, so classifying more than this buys nothing."""


class ConsoleState(Enum):
    """What the line shows after a nudge. ``classify_tail`` returns the first two or ``None``."""

    AT_LOGIN = "at-login"
    AT_PASSWORD = "at-password"  # noqa: S105 — a state-machine label, not a credential
    LOGGED_IN = "logged-in"
    SILENT = "silent"
    BUSY = "busy"


def console_prefix(name: str, server: str, options: ConsoleOptions) -> str:
    """Build the words every console failure message starts with: host, server, port, dial mode.

    Names the lab's own terms (the server's host ID and ``options.port``),
    never the address actually dialled, which may be a forwarded
    ``localhost`` port that means nothing to an operator.
    """
    return f"{name}: console {server}:{options.port} (dial={options.dial}) "


def strip_ansi(text: str) -> str:
    """Drop CSI, OSC and other escapes so prompt regexes see the characters a human sees."""
    return _ANSI_RE.sub("", text)


def classify_tail(
    text: str, login_re: "re.Pattern[str]", password_re: "re.Pattern[str]"
) -> ConsoleState | None:
    """Match the END of *text* (ANSI-stripped, line ends trimmed) against the two prompts.

    Only the tail can decide: a MOTD's ``Last login:`` line is followed by a
    shell prompt, so it never ends the buffer. Returns ``None`` when neither
    prompt is at the end — the caller decides whether that is "logged in"
    (bytes arrived) or "silent" (none did).
    """
    tail = strip_ansi(text).rstrip("\r\n\x00")
    if login_re.search(tail):
        return ConsoleState.AT_LOGIN
    if password_re.search(tail):
        return ConsoleState.AT_PASSWORD
    return None


@dataclass(eq=False)
class ConsoleClient(_StreamHolder):
    """Telnet transport to one serial console, with a classify-then-login handshake.

    ``host``/``port`` are WHERE TO DIAL (``localhost`` and a forwarded port
    behind a tunnel); ``name``/``server`` and ``options.port``/``options.dial``
    are what error messages name, so an operator reads the lab's words, not
    a local ephemeral port.
    """

    host: str
    port: int
    user: str
    password: SecretStr | None = field(repr=False)
    """The login password, or ``None`` to answer the prompt with an empty line.

    A ``SecretStr``, unwrapped only inside the write that types it, and never
    shown by ``repr()``. A plain ``str`` given here is wrapped on construction."""
    options: ConsoleOptions = field(default_factory=ConsoleOptions)
    name: str = ""
    server: str = ""

    _logged_in: bool = field(init=False, repr=False, default=False)
    _shell_unheard: bool = field(init=False, repr=False, default=False)
    """True from the moment the password is typed until the line speaks
    again: ``login(1)`` flushes the tty before it starts the shell, so
    anything typed in that window is discarded (measured on the bed)."""

    def __post_init__(self) -> None:
        self.password = as_secret(self.password)

    # -- addressing words for messages ------------------------------------

    def _prefix(self) -> str:
        return console_prefix(self.name, self.server, self.options)

    def _fail(self, what: str) -> ConsoleError:
        return ConsoleError(self._prefix() + what)

    @property
    def logged_in(self) -> bool:
        """Whether THIS client performed the login (and so owes a logout)."""
        return self._logged_in

    @property
    def alive(self) -> bool:
        """Whether the underlying transport is still usable (mirrors ``TelnetClient.alive``)."""
        if self.writer is None or self.reader is None:
            return False
        return not self.writer.is_closing()

    # -- prompts -----------------------------------------------------------

    def _prompts(self) -> "tuple[re.Pattern[str], re.Pattern[str]]":
        lp, pp = self.options.login_prompt, self.options.password_prompt
        if lp is None or pp is None:
            missing = [
                name
                for name, value in (("login_prompt", lp), ("password_prompt", pp))
                if value is None
            ]
            words = " or ".join(name.replace("_", " ") for name in missing)
            fields = " / ".join(f"console_options.{name}" for name in missing)
            raise self._fail(f"no {words} pattern: set {fields}, or give the host's OS profile one")
        return re.compile(lp), re.compile(pp)

    # -- I/O primitives ----------------------------------------------------

    def _write(self, data: bytes | SecretBytes, writer: Any = None) -> None:
        """Write *data* to the line: the one console write primitive.

        A ``SecretBytes`` (the password) is unwrapped only inside the call.
        *writer* is a stream already taken from this client, for the logout
        that runs during its release. By default the client's own writer,
        which fails as a lost connection once a close has taken the stream.
        """
        (self._held_writer() if writer is None else writer).write(reveal_bytes(data))

    def _send_naws(self, cols: int, rows: int) -> None:
        """Transmit a NAWS subnegotiation with the given terminal size.

        The interactive bridge (``otto host <id> login``) forwards the local
        ``SIGWINCH`` through this, exactly as for
        :meth:`TelnetClient._send_naws <otto.host.telnet.TelnetClient._send_naws>`,
        whose frame this is: raw ``IAC SB NAWS <cols> <rows> IAC SE`` (RFC
        1073) through ``send_iac``, since ``write`` would re-escape the
        framing IAC bytes; a literal 0xFF inside the size is still doubled.
        The initial size comes from ``options.cols``/``options.rows`` at open.
        """
        send_naws(self.writer, cols, rows, label=self.name)

    async def _read_chunk(self, timeout: float) -> bytes | None:
        """One read of the line, inside its read gate; see :meth:`_read_chunk_core`.

        Every ordinary read of the console comes through here, so a release
        knows when no read is in flight and can use the stream alone. Once a
        release has begun it raises :class:`~otto.host.telnet.ReadGateClosed`,
        a lost connection, rather than racing the release's own reads.
        """
        with self._read_gate.reading():
            return await self._read_chunk_core(self.reader, timeout)

    async def _read_chunk_core(self, reader: Any, timeout: float) -> bytes | None:
        """One read of *reader* bounded by *timeout*; ``None`` on timeout, ``b""`` on EOF.

        Ungated: only the release, which already has the stream to itself,
        reads through this directly. Wraps any ``OSError`` the stream raises
        (e.g. a reset connection) in a :class:`~otto.host.errors.ConsoleError`
        carrying this console's own prefix, so it reads like every other
        failure this client raises instead of a raw ``ConnectionResetError``
        with no host/server/port context.
        """
        try:
            return await _read_within(reader, timeout)
        except OSError as exc:
            raise self._fail(f"lost the connection: {exc}") from exc

    async def _collect(
        self, budget: float, stop: "Any", reader: Any = None
    ) -> "tuple[str, bool, bool]":
        """Read until *stop(tail)* is true, EOF, or *budget* seconds.

        Returns ``(text, eof, any_byte)``. *text* is everything accumulated
        (for quoting/messages). *any_byte* is whether at least one byte ever
        arrived — spec §4 step 4's SILENT test is "timeout, zero bytes", not
        "no non-whitespace": a foreground program (``sleep``, ``cat``, a
        script blocked on ``read``) echoes the nudge CR back as a bare CRLF,
        which is bytes, just not a prompt, and must classify as already
        logged in (or, in ``reset()``, fall through to the Ctrl-C/Ctrl-D
        rounds) rather than SILENT. *stop* only ever sees the last
        :data:`_CLASSIFY_WINDOW` bytes, decoded, so a chatty boot log costs
        O(window) per chunk rather than O(total) — see that constant.

        Reads go through the gated :meth:`_read_chunk`, or, given *reader*
        (a stream the release has taken), straight through the ungated core.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + budget
        buf = b""
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return buf.decode(OUTPUT_ENCODING, OUTPUT_ERRORS), False, bool(buf)
            if reader is None:
                chunk = await self._read_chunk(remaining)
            else:
                chunk = await self._read_chunk_core(reader, remaining)
            if chunk is None:
                return buf.decode(OUTPUT_ENCODING, OUTPUT_ERRORS), False, bool(buf)
            if chunk == b"":
                return buf.decode(OUTPUT_ENCODING, OUTPUT_ERRORS), True, bool(buf)
            buf += chunk
            self._shell_unheard = False
            # Decoded whole, not through the session's decoder on this same
            # stream: if a session read ever ended mid-character and these
            # bytes completed it, that session would show one U+FFFD. Harmless
            # while every session pattern ends on an ASCII marker or prompt.
            tail = buf[-_CLASSIFY_WINDOW:].decode(OUTPUT_ENCODING, OUTPUT_ERRORS)
            if stop(tail):
                return buf.decode(OUTPUT_ENCODING, OUTPUT_ERRORS), False, bool(buf)

    async def _settle(self, reader: Any = None) -> None:
        """Drain whatever is already on the line; nothing here is classified.

        *reader* is passed on to :meth:`_collect`.
        """
        if self.options.settle <= 0:
            return
        await self._collect(self.options.settle, lambda _t: False, reader=reader)

    # -- the state machine ---------------------------------------------------

    async def _nudge_and_classify(
        self, login_re: Any, password_re: Any
    ) -> "tuple[ConsoleState, str]":
        """Write CR, collect until a prompt ends the buffer, and say what the line is in."""
        self._write(b"\r")
        text, eof, any_byte = await self._collect(
            self.options.login_timeout,
            lambda t: classify_tail(t, login_re, password_re) is not None,
        )
        state = classify_tail(text, login_re, password_re)
        if state is not None:
            return state, text
        if eof:
            return ConsoleState.BUSY, text
        if not any_byte:
            return ConsoleState.SILENT, text
        return ConsoleState.LOGGED_IN, text

    def _shown(self, text: str) -> str:
        """*text* as a human sees it: ANSI-stripped, NULs dropped, the password as ``***``."""
        shown = strip_ansi(text).replace("\x00", "")
        if self.password:
            shown = shown.replace(self.password.get_secret_value(), "***")
        return shown

    def _quote(self, text: str) -> str:
        """Quote the line's last ``_QUOTE_BYTES``: ANSI-stripped, password scrubbed, repr'd.

        The scrub runs before the tail is cut, so a password straddling the
        cut is never half shown. It matters once the password has been typed:
        a line that echoes it (a getty or serial app that leaves echo on, a
        prompt that only looked like a password prompt) would otherwise carry
        it into every message and log line that quotes the line.
        """
        return repr(self._shown(text)[-_QUOTE_BYTES:])

    def _seen_suffix(self, text: str) -> str:
        """Render a trailing ``" Seen: '...'."`` for BUSY, or ``""`` if nothing arrived."""
        return f" Seen: {self._quote(text)}." if strip_ansi(text).strip() else ""

    async def login_sequence(self) -> None:
        """Run steps 4-6 of the spec: nudge, classify, username, password.

        Raises :class:`~otto.host.errors.ConsoleError` for every state but a login prompt
        (its :class:`~otto.host.errors.ConsoleLoggedInError` subclass for a line
        already at a shell). On return the password has been written; the
        session's marker handshake is the readiness check, exactly as for
        telnet — per spec §4 step 6, a
        returning login prompt after the password (a refusal) is not
        inspected here; a bad password surfaces later, through the marker
        handshake's own timeout.

        Refuses at once, writing and reading nothing, when THIS connection
        already logged in (:attr:`logged_in`) — a second call (or a hook
        calling this on a ``login: true`` console that already logged in
        during ``connect()``) must not retype the password into the line.
        Without this guard the stray nudge still runs the full state machine
        against a shell prompt, which classifies as ``LOGGED_IN`` and raises
        the "run ``otto host <id> logout``" message after the full
        ``login_timeout`` wait — technically harmless (the password is never
        retyped) but slow and wrong advice, since otto itself just logged in.
        """
        if self._logged_in:
            raise self._fail(
                "is already logged in by this connection — console_login() runs once, "
                "and only when console_options.login is false"
            )
        login_re, password_re = self._prompts()
        state, text = await self._nudge_and_classify(login_re, password_re)
        if state is ConsoleState.AT_PASSWORD:
            raise self._fail(
                "is stuck at a password prompt; run `otto host <id> logout` to reset it"
            )
        if state is ConsoleState.BUSY:
            raise self._fail(
                "is busy: the server closed the connection before any prompt — "
                "another client holds the port, or the server has no serial device "
                f"behind it right now (the line is down).{self._seen_suffix(text)}"
            )
        if state is ConsoleState.SILENT:
            raise self._fail(
                f"is silent: no login prompt within {self.options.login_timeout:g}s and no bytes "
                f"received — wrong port, no getty on the line, or a dead cable"
            )
        if state is ConsoleState.LOGGED_IN:
            seen = self._quote(text)
            lines = self._shown(text).strip().splitlines()
            raise ConsoleLoggedInError(
                self._prefix() + "did not show a login prompt; it is likely already logged "
                f"in. Seen: {seen}. Run `otto host <id> logout` to reset it",
                seen=seen,
                prompt=lines[-1].strip() if lines else "",
            )

        self._write((self.user + TELNET_EOL).encode())
        text, eof, _any_byte = await self._collect(
            self.options.login_timeout,
            lambda t: classify_tail(t, login_re, password_re) is not None,
        )
        after_user = classify_tail(text, login_re, password_re)
        if after_user is ConsoleState.AT_LOGIN:
            raise self._fail(f"login refused for {self.user!r}")
        if after_user is not ConsoleState.AT_PASSWORD:
            what = "closed the connection" if eof else "showed no password prompt"
            raise self._fail(f"{what} after the username {self.user!r}; seen: {self._quote(text)}")

        self._write(SecretBytes(reveal(self.password, TELNET_EOL).encode()))
        self._logged_in = True
        self._shell_unheard = True
        logger.debug(f"{self._prefix()}logged in as {self.user!r}")

    async def reset(self) -> ConsoleState:
        """Bring the line back to a login prompt, bounded, or fail loud.

        1. Nudge: at ``login:`` → nudge again to confirm it is getty's
           prompt (``login(1)``'s retry prompt answers the second CR with
           ``Password:``) and, if so, done; at ``Password:`` → Ctrl-C and wait
           for ``login:`` (``login(1)`` exits and getty respawns a clean
           prompt; a CR would only land at ``login(1)``'s own retry prompt,
           which answers the next client's CR nudge with ``Password:``, and
           at a ``su``/``sudo`` prompt inside a shell Ctrl-C cancels where
           CR would submit an empty password); SILENT is diagnosed right
           here, not after wasting three rounds of Ctrl-C/Ctrl-D finding out
           the line never says anything.
        2. Up to three rounds of Ctrl-C, a bounded wait for the interrupt to
           land, then Ctrl-D and CR, classifying after each (the wait: see
           ``_INTERRUPT_LANDED``).
        3. Fail, quoting the tail.

        Returns what the nudges found, which says whether the reset had
        to act: ``ConsoleState.AT_LOGIN`` means the line was already at
        getty's login prompt and nothing was sent but the two nudges;
        ``ConsoleState.AT_PASSWORD`` or ``ConsoleState.LOGGED_IN`` means it
        was not, and the login prompt has now been restored. Every return
        leaves the line at its login prompt. ``logged_in`` is cleared on
        every path that returns — whoever otto's session had logged in as is
        no longer logged in once the line is back at a login prompt, however
        that happened.
        """
        login_re, password_re = self._prompts()
        state, text = await self._nudge_and_classify(login_re, password_re)
        found = state
        if state is ConsoleState.AT_LOGIN:
            # Confirm with a second nudge: getty re-prints its prompt, but
            # login(1)'s own retry prompt (where a CR at its Password: also
            # lands, after the failure delay) takes the CR as an empty
            # username and asks for a password. Only getty's prompt is clean.
            state, text = await self._nudge_and_classify(login_re, password_re)
            if state is ConsoleState.AT_LOGIN:
                self._logged_in = False
                return found
            found = ConsoleState.AT_PASSWORD
        if state is ConsoleState.BUSY:
            raise self._fail(
                "is busy: the server closed the connection — another client "
                f"holds the port.{self._seen_suffix(text)}"
            )
        if state is ConsoleState.SILENT:
            raise self._fail(
                f"is silent: no login prompt within {self.options.login_timeout:g}s and no bytes "
                f"received — wrong port, no getty on the line, or a dead cable"
            )

        def at_login(t: str) -> bool:
            return classify_tail(t, login_re, password_re) is ConsoleState.AT_LOGIN

        if state is ConsoleState.AT_PASSWORD:
            self._write(b"\x03")
            text, _eof, _any_byte = await self._collect(self.options.login_timeout, at_login)
            if at_login(text):
                self._logged_in = False
                return found

        def landed(t: str) -> bool:
            return at_login(t) or _INTERRUPT_LANDED.search(strip_ansi(t).rstrip("\r\n")) is not None

        for _round in range(_RESET_ROUNDS):
            # Two writes, not one, and the second only once the first has
            # landed: Ctrl-C's input-queue flush would eat a Ctrl-D that
            # arrives before it (see _INTERRUPT_LANDED).
            self._write(b"\x03")
            text, _eof, _any_byte = await self._collect(self.options.login_timeout, landed)
            if at_login(text):
                self._logged_in = False
                return found
            self._write(b"\x04\r")
            more, _eof, _any_byte = await self._collect(self.options.login_timeout, at_login)
            text += more
            if at_login(text):
                self._logged_in = False
                return found
        raise self._fail(
            f"could not reset console after {_RESET_ROUNDS} rounds of Ctrl-C/Ctrl-D; "
            f"last seen: {self._quote(text)}"
        )

    async def logout(self, reader: Any, writer: Any) -> None:
        """Send EOF and wait up to ``login_timeout`` for the login prompt. Never raises.

        Runs on *reader* and *writer*, the stream ``close()`` has already
        taken from this client and has to itself, so it reads past the gate.
        """
        if not self._logged_in or writer is None:
            return
        try:
            login_re, password_re = self._prompts()
            if self._shell_unheard:
                # Nothing has been read since the password was typed, so the
                # shell may not exist yet: login(1) flushes the tty before it
                # execs the shell, and an EOF typed into that window is
                # discarded (measured on the bed: an immediate logout after
                # a fresh login left the shell up and waited out the whole
                # budget). Wait for the line's first bytes, then let it
                # settle, then log out.
                await self._collect(self.options.login_timeout, bool, reader=reader)
                await self._settle(reader=reader)
            self._write(b"\x04", writer)
            text, _eof, _any_byte = await self._collect(
                self.options.login_timeout,
                lambda t: classify_tail(t, login_re, password_re) is ConsoleState.AT_LOGIN,
                reader=reader,
            )
            if classify_tail(text, login_re, password_re) is not ConsoleState.AT_LOGIN:
                logger.warning(f"{self._prefix()}login prompt did not return after logout")
        except Exception as exc:  # noqa: BLE001 — best-effort courtesy on close
            logger.warning(f"{self._prefix()}logout failed: {exc}")

    async def refused_after_login(self) -> str:
        """After a failed marker handshake: raise if getty is back on the line, else quote the line.

        Spec §4 step 6: :meth:`login_sequence` returns right after typing the
        password, so a refused login is found by the session's marker
        handshake timing out — which then asks here, before the session is
        torn down. This reads what the handshake left unread, bounded by
        ``login_timeout`` and ending as soon as a prompt ends the buffer.

        A tail at ``login_prompt`` OR ``password_prompt`` means getty still
        owns the line and no shell ever started: ``login(1)`` printed ``Login
        incorrect`` and prompted again, and may already have read the
        handshake's resent probes as the next username, parking the line at
        the password prompt. Its failure delay (~3 s) is silent, which is why
        this waits for a prompt rather than stopping at the first quiet gap.
        Before raising, Ctrl-C ends ``login(1)`` and a bounded wait lets
        getty respawn its own prompt: ``login(1)``'s retry prompt answers a
        CR nudge with ``Password:`` (an empty username), so a line left there
        would meet the next client as "stuck at a password prompt" (measured
        on the bed). No shell ever started, so :attr:`logged_in` is cleared
        and ``close()`` sends no logout. Either raises
        :class:`~otto.host.errors.ConsoleError` ``login refused for '<user>'``
        (never the password).

        Otherwise returns ``_seen_suffix()`` of the line — ``" Seen:
        '<quote>'."``, or ``""`` when nothing arrived — for the caller to
        append to its own failure message (the quote scrubs the password,
        like every ``_quote()``). A no-op returning ``""`` unless this client
        typed a password (:attr:`logged_in`): a ``login=False`` console was
        refused nothing.
        """
        if not self._logged_in or self.reader is None or self.writer is None:
            return ""
        login_re, password_re = self._prompts()
        text, _eof, _any_byte = await self._collect(
            self.options.login_timeout,
            lambda t: classify_tail(t, login_re, password_re) is not None,
        )
        if classify_tail(text, login_re, password_re) is None:
            return self._seen_suffix(text)
        # login(1) still owns the line, at its own retry prompt or parked at a
        # password prompt for a probe-as-username. Its retry prompt answers
        # the next client's CR nudge with "Password:" (an empty username), so
        # end login(1) with Ctrl-C and let getty respawn a fresh prompt.
        # Bounded; never types a credential.
        self._write(b"\x03")
        await self._collect(
            self.options.login_timeout,
            lambda t: classify_tail(t, login_re, password_re) is ConsoleState.AT_LOGIN,
        )
        self._logged_in = False  # no shell started: close() owes no logout
        raise self._fail(f"login refused for {self.user!r}")

    # -- lifecycle ---------------------------------------------------------

    async def open(self, interactive: bool = False) -> None:
        """Dial (registered as single-client from the start), negotiate, then settle.

        Every phase is bounded: the TCP connect by ``login_timeout`` (passed
        as telnetlib3's ``connect_timeout``; ``options.extra`` may override
        it), telnetlib3's option negotiation by its ``connect_maxwait``, echo
        negotiation by ``echo_negotiation_timeout``, and the settle window by
        ``settle``. A dial that is refused or times out raises a
        :class:`~otto.host.errors.ConsoleError` naming host, server, port and
        dial mode.
        """
        logger.debug(
            f"ConsoleClient.open {self.host}:{self.port} user={self.user!r} "
            f"login={self.options.login} dial={self.options.dial} interactive={interactive}"
        )
        await self._await_prior_release()
        open_kwargs = self.options._open_kwargs()  # noqa: SLF001 — intra-package access
        open_kwargs["port"] = self.port
        try:
            reader, writer = await open_telnet_connection(
                self.host, single_client=single_client("console"), **open_kwargs
            )
        except ConsoleError:
            raise
        except OSError as exc:
            raise self._fail(f"could not be dialled: {exc}") from exc
        self._install(reader, writer)
        if not interactive:
            await negotiate_no_echo(self.writer, self.options.echo_negotiation_timeout)
        await self._settle()

    async def connect(self, interactive: bool = False) -> None:
        """Open, then log in when ``options.login`` says so."""
        await self.open(interactive=interactive)
        if self.options.login:
            await self.login_sequence()
        else:
            logger.debug(f"{self._prefix()}no login step (options.login=False)")

    @override
    async def _release(self, taken: _TakenStream) -> None:
        """Log out if this client logged in, then release the stream. Never raises.

        The console's part of :meth:`close <otto.host.telnet._StreamHolder.close>`,
        which takes the stream first and marks its release ended afterwards.

        The release is the one every telnet-framed stream gets (see
        :func:`~otto.host.telnet.release_telnet_stream`): a serial line can
        wedge when its client goes away with output still queued, so the
        release drains it to quiet before it closes, then waits for otto's
        socket to be released.

        The stream is *taken* first, before any await: the client forgets it,
        so a second, concurrent ``close()`` (a manager teardown racing a
        session teardown) finds nothing to take and waits for this one's
        release to end, instead of logging out or draining a second time.
        Next, the reads already in flight are waited for: logout reads the
        line, and a read of its own beside one of theirs would collide. Then
        the logout, on the taken stream, then the release. The idle wait and
        the drain share one :data:`~otto.host.telnet.CONSOLE_QUIESCE_BUDGET`;
        logout's time is added to the release's deadline, not charged to it,
        since logout has its own allowance.

        A read that never finishes makes the close skip logout and the drain
        and abort the stream. A cancellation while waiting for reads or
        during logout aborts the taken stream and re-raises: the caller asked
        to stop, so no fresh drain starts. Either way the console server's
        single-client slot is freed, or, when the release is not seen, the
        transport stays registered for the emergency sweep.
        """
        reader, writer, transport, sock, gate = (
            taken.reader,
            taken.writer,
            taken.transport,
            taken.sock,
            taken.gate,
        )
        owes_logout = self.options.logout and self._logged_in
        loop = asyncio.get_running_loop()
        idle_deadline = loop.time() + CONSOLE_QUIESCE_BUDGET
        logout_elapsed = 0.0
        try:
            idle = await gate.wait_idle(idle_deadline)
            if idle and owes_logout:
                started = loop.time()
                await self.logout(reader, writer)
                logout_elapsed = loop.time() - started
        except asyncio.CancelledError:
            await _abort_and_observe(writer, transport, sock)
            raise
        except Exception as exc:  # noqa: BLE001 — close never raises: a failure aborts instead
            logger.debug(f"{self._prefix()}console close failed before its release: {exc!r}")
            await _abort_and_observe(writer, transport, sock)
            return
        if not idle:
            logger.debug(
                f"{self._prefix()}a read did not finish before the release; "
                f"aborting without logging out"
            )
            await _abort_and_observe(writer, transport, sock)
            return
        await release_telnet_stream(
            reader,
            writer,
            transport,
            single_client=single_client("console"),
            reads_idle=gate,
            deadline=idle_deadline + logout_elapsed,
            sock=sock,
        )

    @override
    def _release_ended(self, gate: ReadGate) -> bool:
        """Mark the release ended, and forget the login that belonged to its stream.

        A no-op once a newer stream is installed: ``_released`` and the login
        then belong to that stream, not to the one whose release just ended.
        """
        if not super()._release_ended(gate):
            return False
        self._logged_in = False
        return True
