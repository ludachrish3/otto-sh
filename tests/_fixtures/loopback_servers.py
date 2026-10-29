"""Real library failures on loopback, for the tests that hide password-holding frames.

A traceback test about a library's frames must fail INSIDE that library: a
test-defined stand-in raises from a frame under ``tests/``, which no
suppression of the library's directory could hide, so its green would prove
nothing. Each helper here makes the real asyncssh, telnetlib3 or aioftp code
receive a password and then fail, against a server on ``127.0.0.1`` only.

The servers refuse every password, so a planted value is never accepted and
the failure is always the authentication one. :func:`ssh_server` alone can be
told the one password to accept, for the host-key tests, which need a
connection that would succeed but for its host key.
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
