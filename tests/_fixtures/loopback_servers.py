"""Real library failures on loopback, for the tests that hide password-holding frames.

A traceback test about a library's frames must fail INSIDE that library: a
test-defined stand-in raises from a frame under ``tests/``, which no
suppression of the library's directory could hide, so its green would prove
nothing. Each helper here makes the real asyncssh, telnetlib3 or aioftp code
receive a password and then fail, against a server on ``127.0.0.1`` only.

The servers refuse every password, so a planted value is never accepted and
the failure is always the authentication one. :func:`ssh_server` alone can be
told the one password to accept, for the host-key tests, which need a
connection that would succeed but for its host key. :class:`TelnetDevice`
is the one server here with a shell behind its login: a scripted device
for the tests of how otto opens a session on it.
"""

import asyncio
import contextlib
import socket
from collections.abc import AsyncIterator, Iterator
from typing import Any

ACCEPTED_PASSWORD = "the-only-password-these-servers-take"
"""The one password the FTP server accepts: never the planted one."""


class Offered:
    """The passwords clients offered a refusing server, behind a repr that shows none.

    A test that proves its password reached the server keeps this object in
    frames that are on the failing stack, and a traceback renders their
    locals: a plain list there would print the very value under test.
    """

    def __init__(self) -> None:
        self.passwords: list[str] = []

    def __repr__(self) -> str:
        return f"<Offered: {len(self.passwords)} password(s)>"


@contextlib.contextmanager
def closed_port() -> Iterator[int]:
    """A loopback port that refuses every connection while the block runs; yields the port.

    The socket stays bound and never listens, so no other process can take
    the port in the meantime, and Linux answers a dial to it with
    ``ECONNREFUSED``. A port bound and then released could be reused by
    anything before the dial.
    """
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        yield sock.getsockname()[1]


@contextlib.asynccontextmanager
async def refusing_ftp_server(login: str, offered: Offered | None = None) -> AsyncIterator[int]:
    """An aioftp server on loopback that knows *login* and answers its ``PASS`` with 530.

    Yields its port; each password offered is added to *offered*.
    """
    import aioftp

    sink = offered if offered is not None else Offered()

    class _Recording(aioftp.MemoryUserManager):
        async def authenticate(self, user: aioftp.User, password: str) -> bool:
            sink.passwords.append(password)
            return await super().authenticate(user, password)

    server = aioftp.Server(_Recording([aioftp.User(login, ACCEPTED_PASSWORD)]))
    await server.start("127.0.0.1", 0)
    try:
        yield server.server.sockets[0].getsockname()[1]
    finally:
        await server.close()
        # The dispatcher's ``stream.close()`` frees its socket on the loop's
        # next turn; yield it, or the transport is collected unclosed.
        await asyncio.sleep(0)


@contextlib.asynccontextmanager
async def ssh_server(
    offered: Offered,
    *,
    host_keys: "list[Any] | None" = None,
    accepted_password: str | None = None,
    forwarding: bool = False,
) -> AsyncIterator[int]:
    """An asyncssh server on loopback that offers password auth; yields its port.

    Each password offered is added to *offered*, so a test can prove a
    password never reached the server. *accepted_password* is the one it
    accepts (``None``: it refuses every one). *host_keys* are asyncssh's
    ``server_host_keys`` (private keys, or ``(key, certificate)`` pairs); one
    fresh ed25519 key by default. *forwarding* lets a client open
    ``direct-tcpip`` channels through it, so it can serve as a hop.
    """
    import asyncssh

    class _Server(asyncssh.SSHServer):
        def begin_auth(self, username: str) -> bool:
            return True

        def password_auth_supported(self) -> bool:
            return True

        def validate_password(self, username: str, password: str) -> bool:
            offered.passwords.append(password)
            return accepted_password is not None and password == accepted_password

        def connection_requested(
            self, dest_host: str, dest_port: int, orig_host: str, orig_port: int
        ) -> bool:
            return forwarding

    keys = host_keys if host_keys is not None else [asyncssh.generate_private_key("ssh-ed25519")]
    server = await asyncssh.create_server(_Server, "127.0.0.1", 0, server_host_keys=keys)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()
        await server.wait_closed()


@contextlib.asynccontextmanager
async def refusing_ssh_server(offered: Offered) -> AsyncIterator[int]:
    """An asyncssh server on loopback that offers password auth and refuses every password.

    Yields its port; each password offered is added to *offered*.
    """
    async with ssh_server(offered) as port:
        yield port


class _StreamServer:
    """An ``asyncio`` stream server whose connections are all closed before the loop ends.

    pytest-asyncio closes the loop when the test returns; a transport whose
    close is still pending then is collected unclosed, and pytest reports
    its ``ResourceWarning`` on whichever test happens to be running.
    """

    def __init__(self) -> None:
        self.writers: list[asyncio.StreamWriter] = []
        self.server: asyncio.AbstractServer | None = None

    async def start(self, handle: Any) -> int:
        async def tracked(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            self.writers.append(writer)
            with contextlib.suppress(Exception):
                await handle(reader, writer)

        self.server = await asyncio.start_server(tracked, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    async def close(self) -> None:
        """Stop listening, close every connection, then wait for the server.

        The connections close BEFORE ``wait_closed``: from Python 3.12 it
        waits for every connection the server accepted to close, so awaited
        first, with a connection open (a client that failed mid-login and
        never closed its end), it never returns. 3.10 returned at once.
        """
        if self.server is not None:
            self.server.close()
        for writer in self.writers:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        if self.server is not None:
            await self.server.wait_closed()


@contextlib.asynccontextmanager
async def refusing_telnet_login(offered: Offered) -> AsyncIterator[int]:
    """A telnet login on loopback: prompts, reads the password, says ``Login incorrect``.

    It then prompts for a login again and reads whatever arrives without
    answering, as ``login(1)`` does, so the client's shell handshake times
    out. Yields its port; the password line is added to *offered*.
    """

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"login: ")
        await reader.readuntil(b"\n")
        writer.write(b"Password: ")
        line = await reader.readuntil(b"\n")
        # The bytes after the client's telnet option negotiation, if any.
        offered.passwords.append(line.rsplit(b"\xff", 1)[-1].decode(errors="replace").strip())
        writer.write(b"\r\nLogin incorrect\r\nlogin: ")
        while await reader.read(4096):
            pass

    server = _StreamServer()
    try:
        yield await server.start(handle)
    finally:
        await server.close()


HANGUP_WORDS = b"\r\nsession limit reached\r\n"
"""What a ``"hangup"`` :class:`TelnetDevice` login says before it closes."""

NOISE = (b"line noise \x01\x02\x07\x08\x0b\x0e\x7f\r\n" * 500)[:10_000]
"""What a ``"noise"`` :class:`TelnetDevice` login writes before it closes: 10,000 bytes."""

LOGIN_LINE_MAX = 62
"""The longest user name BusyBox ``login`` reads; it exits on a longer line."""


def _typed(line: bytes) -> bytes:
    """The bytes of *line* after the client's telnet option negotiation, if any."""
    return line.rsplit(b"\xff", 1)[-1]


def _typed_text(data: bytes) -> str:
    """What a terminal reads from *data*: every telnet line end (CR LF, CR NUL, CR) as LF."""
    text = _typed(data).decode(errors="replace")
    return text.replace("\r\n", "\n").replace("\r\x00", "\n").replace("\r", "\n")


async def _read_line(reader: asyncio.StreamReader) -> bytes:
    """One typed line, its end included; the LF or NUL of a CR pair stays for the next read."""
    return (await reader.readuntil(b"\r")).lstrip(b"\n\x00")


def _caret(line: bytes) -> bytes:
    """*line* as a tty with ``echoctl`` echoes it: a control character but CR as ``^X``."""
    return b"".join(
        b"^" + bytes([c ^ 0x40]) if (c < 0x20 or c == 0x7F) and c != 0x0D else bytes([c])
        for c in line
    )


class TelnetDevice:
    """A telnet login with a shell behind it, scripted per connection.

    *scripts* names what each connection's login does, in order (the last
    one repeats): ``"accept"`` logs in; ``"hold"`` logs in to a shell that
    never answers (``holding`` is set once it is there); ``"refuse"`` refuses the password
    it reads, as for a wrong one; ``"discard"`` is BusyBox 1.21.1
    ``login``, which prints ``Password:`` before it flushes pending input,
    so the password that arrives is thrown away and the next line is read
    as the password and refused. After a refusal the next line is read as a
    user name, echoed as a tty echoes it, on which ``login`` exits when it is
    longer than :data:`LOGIN_LINE_MAX`. ``"hangup"`` writes
    :data:`HANGUP_WORDS` after the password and closes; ``"noise"`` writes
    :data:`NOISE` (control characters among text) and closes.

    The shell answers otto's handshake probe (its marker, through
    :func:`~tests._fixtures.fake_shell.probe_marker`) and every frame:
    ``echo X`` prints ``X``, anything else prints nothing; both exit 0.
    ``connections`` counts what was accepted, ``open`` what is still open,
    ``passwords`` holds every line the login read as a password, and
    ``discarded`` every password line a ``"discard"`` login threw away.
    ``"discard-caret"`` is ``"discard"`` on a tty that echoes a control
    character as ``^X`` (``echoctl``).

    With *one_client*, the device serves one client at a time, as a Zephyr
    ``shell_telnet`` does: a connection made while another is open is
    logged in and closed at once (counted, but taking no script).
    """

    def __init__(self, scripts: list[str], *, one_client: bool = False) -> None:
        self._scripts = list(scripts)
        self._one_client = one_client
        self.connections = 0
        self.open = 0
        self.passwords: list[str] = []
        self.discarded: list[str] = []
        self.holding = asyncio.Event()

    def _script(self) -> str:
        index = min(self.connections, len(self._scripts) - 1)
        self.connections += 1
        return self._scripts[index]

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._one_client and self.open:
            self.connections += 1
            script = "busy"
        else:
            script = self._script()
        self.open += 1
        try:
            if not await self._answer_login(script, reader, writer):
                return
            if script == "hold":
                self.holding.set()
                while await reader.read(4096):
                    pass
            else:
                await self._shell(reader, writer)
        finally:
            self.open -= 1
            writer.close()

    async def _answer_login(
        self, script: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> bool:
        writer.write(b"login: ")
        user = _typed(await _read_line(reader))
        writer.write(user + b"\nPassword: ")
        password = _typed(await _read_line(reader))
        if script in ("accept", "hold"):
            self.passwords.append(password.decode(errors="replace").strip())
            writer.write(b"\r\n$ ")
            return True
        if script == "busy":
            return False
        if script in ("hangup", "noise"):
            writer.write(HANGUP_WORDS if script == "hangup" else NOISE)
            return False
        refused = password
        if script in ("discard", "discard-caret"):
            # The password arrived before the flush: echoed, then discarded.
            writer.write(_caret(password) if script == "discard-caret" else password)
            self.discarded.append(password.decode(errors="replace").strip())
            refused = _typed(await _read_line(reader))
        self.passwords.append(refused.decode(errors="replace").strip())
        writer.write(b"\r\nLogin incorrect\r\nlogin: ")
        while True:
            name = _typed(await _read_line(reader))
            writer.write(name)
            if len(name.rstrip(b"\r")) > LOGIN_LINE_MAX:
                return False
            writer.write(b"\nPassword: ")
            await _read_line(reader)
            writer.write(b"\r\nLogin incorrect\r\nlogin: ")

    async def _shell(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        from tests._fixtures.fake_shell import end_record_for, parse_bash_frame, probe_marker
        from tests.unit.host._session_feed import identity_handshake_reply

        pending = ""
        while chunk := await reader.read(4096):
            pending = (pending + _typed_text(chunk)).lstrip("\n\x00")
            if (marker := probe_marker(pending)) is not None and pending.endswith("\n"):
                pending = ""
                writer.write(identity_handshake_reply(marker).encode())
                continue
            if (framed := parse_bash_frame(pending)) is not None:
                pending = ""
                words = framed.cmd.split(maxsplit=1)
                value = words[1] + "\n" if words[:1] == ["echo"] and len(words) > 1 else ""
                writer.write(f"{framed.begin}\n{value}{end_record_for(framed, 0)}\n".encode())


DEVICE_PASSWORD = "the-device-password"
"""The fake, test-only password every :func:`telnet_host` logs in with."""


def telnet_host(
    port: int,
    *,
    init_timeout: float | None = None,
    password: str = DEVICE_PASSWORD,
    single_client_console: bool = False,
) -> Any:
    """A telnet host on the loopback :class:`TelnetDevice` at *port*; no backoff between opens."""
    from otto.host.element import Element
    from otto.host.options import TelnetOptions
    from otto.host.unix_host import UnixHost
    from otto.logger.mode import LogMode
    from otto.models.base import Cred

    host = UnixHost(
        ip="127.0.0.1",
        element=Element(name="h1"),
        term="telnet",
        creds=[Cred(login="admin", password=password)],
        telnet_options=TelnetOptions(
            port=port,
            echo_negotiation_timeout=0.1,
            single_client_console=single_client_console,
            extra={"connect_minwait": 0.0, "connect_maxwait": 0.1},
        ),
        log=LogMode.QUIET,
        init_timeout=init_timeout,
    )
    host._session_mgr._retry_backoff = 0.0
    return host


@contextlib.asynccontextmanager
async def telnet_device(
    scripts: list[str], *, one_client: bool = False
) -> AsyncIterator[tuple[int, TelnetDevice]]:
    """A :class:`TelnetDevice` on loopback: yields its port and the device."""
    device = TelnetDevice(scripts, one_client=one_client)
    server = _StreamServer()
    try:
        yield await server.start(device.handle), device
    finally:
        await server.close()


@contextlib.asynccontextmanager
async def telnet_writer_after_eof() -> AsyncIterator[Any]:
    """A real telnetlib3 writer whose transport has sent EOF; yields the writer.

    Writing to it fails inside asyncio's own transport ``write``, which raises
    ``RuntimeError`` with the bytes still bound as ``data``, under
    telnetlib3's ``write`` frame that holds them too.
    """
    import telnetlib3

    async def silent(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read()

    server = _StreamServer()
    try:
        port = await server.start(silent)
        _reader, writer = await telnetlib3.open_connection(
            "127.0.0.1", port, encoding=False, connect_minwait=0.0, connect_maxwait=0.1
        )
        writer.transport.write_eof()
        try:
            yield writer
        finally:
            writer.close()
    finally:
        await server.close()
