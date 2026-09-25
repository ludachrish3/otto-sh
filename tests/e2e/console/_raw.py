"""Raw access to a bed console with NO otto console code: the wiring proof and the failure setups.

A bed console is a telnet listener on a console SERVER (ser2net on test1, a
QEMU ``-serial telnet:`` on test4), usually bound where only an SSH forward
into the server reaches it. :func:`raw_console` opens exactly that with
asyncssh + telnetlib3 and nothing of otto's, so a test built on it proves the
wiring, and the fail-loudly tests can put a line into a state (logged in and
abandoned, held by another client) that otto must then refuse.

Every helper here releases the line on every path: a console is
single-client, and a leaked connection makes the next test's otto see
``busy``.
"""

import asyncio
import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import asyncssh
import telnetlib3

from otto.host.login_proxy import Cred
from tests._fixtures.labdata import host_data

_CONNECT_TIMEOUT = 20.0
_ANSI = re.compile(rb"\x1b\[[0-9;?]*[ -/]*[@-~]")
_SHELL_PROMPT = re.compile(rb"[$#] ?$")


def _at_shell_prompt(out: bytes) -> bool:
    return _SHELL_PROMPT.search(out.rstrip(b"\r\n")) is not None


def strip_ansi_bytes(out: bytes) -> bytes:
    """*out* without ANSI CSI sequences (a getty may colour its issue banner)."""
    return _ANSI.sub(b"", out)


@dataclass
class RawLine:
    """One raw telnet connection to a console port."""

    reader: Any
    writer: Any


def server_cred(server: str) -> Cred:
    """The cred the raw helper tunnels into console server *server* as (its first)."""
    return Cred(**host_data(server)["creds"][0])


@asynccontextmanager
async def raw_console(
    server_ip: str, user: str, password: str, port: int
) -> AsyncIterator[RawLine]:
    """Telnet to ``localhost:<port>`` INSIDE *server_ip* through an SSH local forward.

    On exit the telnet connection is dropped WITHOUT logging out (a serial
    line keeps whatever shell it has), then the forward and the SSH
    connection close.
    """
    conn = await asyncssh.connect(
        server_ip,
        username=user,
        password=password,
        known_hosts=None,
        connect_timeout=_CONNECT_TIMEOUT,
    )
    try:
        listener = await conn.forward_local_port("127.0.0.1", 0, "localhost", port)
        try:
            reader, writer = await telnetlib3.open_connection(
                "127.0.0.1",
                listener.get_port(),
                encoding=False,
                connect_timeout=_CONNECT_TIMEOUT,
            )
            try:
                yield RawLine(reader, writer)
            finally:
                writer.close()
        finally:
            listener.close()
    finally:
        conn.close()
        await conn.wait_closed()


@asynccontextmanager
async def raw_direct(ip: str, port: int) -> AsyncIterator[RawLine]:
    """Telnet straight to ``ip:port``: the ``dial: direct`` path, with no tunnel."""
    reader, writer = await telnetlib3.open_connection(
        ip, port, encoding=False, connect_timeout=_CONNECT_TIMEOUT
    )
    try:
        yield RawLine(reader, writer)
    finally:
        writer.close()


async def collect(
    line: RawLine,
    timeout: float = 8.0,
    until: Callable[[bytes], bool] | None = None,
) -> bytes:
    """Read for up to *timeout* seconds.

    Stops early once *until* holds for what was read, or (with no *until*)
    once the line has gone quiet for 1 s after the first byte, or at EOF.
    """
    buf = b""
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while (left := end - loop.time()) > 0:
        try:
            chunk = await asyncio.wait_for(line.reader.read(4096), min(1.0, left))
        except asyncio.TimeoutError:
            if buf and until is None:
                break
            continue
        if not chunk:
            break
        buf += chunk
        if until is not None and until(buf):
            break
    return buf


async def nudge(line: RawLine, timeout: float = 8.0) -> bytes:
    """Write CR and collect what the line answers (see :func:`collect`)."""
    line.writer.write(b"\r")
    return await collect(line, timeout)


_LOGIN_PROMPT = re.compile(rb"login: ?$")
_PASSWORD_PROMPT = re.compile(rb"[Pp]assword: ?$")


def ends_at_login(out: bytes) -> bool:
    """Whether *out* (ANSI-stripped, line ends trimmed) ends at a login prompt."""
    return _LOGIN_PROMPT.search(strip_ansi_bytes(out).rstrip(b"\r\n")) is not None


def _ends_at_a_prompt(out: bytes) -> bool:
    tail = strip_ansi_bytes(out).rstrip(b"\r\n")
    return _LOGIN_PROMPT.search(tail) is not None or _PASSWORD_PROMPT.search(tail) is not None


async def console_answer(server: str, port: int, timeout: float = 12.0) -> bytes:
    """What the console on *server*:*port* answers a CR with.

    Reads until the answer ends at a login or password prompt, or *timeout*
    (a ``login`` still in its failed-attempt delay answers only after it).
    """
    cred = server_cred(server)
    async with raw_console(host_data(server)["ip"], cred.login, cred.password, port) as line:
        line.writer.write(b"\r")
        return await collect(line, timeout, _ends_at_a_prompt)


async def login_and_abandon(server: str, port: int, login: str, login_password: str) -> None:
    """Log in over raw telnet and DROP the connection: the line keeps its shell.

    Asserts each step (login prompt, password prompt, shell prompt) so a
    test that relies on the abandoned shell fails HERE, naming the step,
    rather than later on an unexplained otto error. No message quotes the
    password.
    """
    cred = server_cred(server)
    async with raw_console(host_data(server)["ip"], cred.login, cred.password, port) as line:
        out = await nudge(line)
        assert b"login:" in out, f"{server}:{port}: no login prompt before the setup login: {out!r}"
        line.writer.write(login.encode() + b"\r")
        out = await collect(line, 10.0, lambda b: b"assword:" in b)
        assert b"assword:" in out, f"{server}:{port}: no password prompt after {login!r}: {out!r}"
        line.writer.write(login_password.encode() + b"\r")
        out = await collect(line, 15.0, _at_shell_prompt)
        shown = strip_ansi_bytes(out).replace(login_password.encode(), b"***")
        assert _at_shell_prompt(out), (
            f"{server}:{port}: {login!r} reached no shell prompt: {shown!r}"
        )
