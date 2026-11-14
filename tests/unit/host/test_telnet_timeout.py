"""A telnet session's timed-out command keeps what it had printed, and nothing after the interrupt.

Driven through a real telnetlib3 ``TelnetReader`` fed with ``feed_data``,
answered by a scripted shell on the writer side: the handshake (``onlcr``,
as a default Linux tty), the recovery probe, and one reply per command.
Asserted on values, never on time.
"""

import asyncio
import re
from typing import Any

import pytest
from telnetlib3.stream_reader import TelnetReader

from otto.host.session import TelnetSession
from tests._fixtures.fake_shell import probe_marker
from tests.unit.host._session_feed import onlcr_handshake_reply, writes_handshake
from tests.unit.host._telnet_fakes import stream_client

pytestmark = [pytest.mark.asyncio]

LIMIT = 2**16  # telnetlib3's default reader limit


class _ScriptedShell:
    """The writer side: answers each line the session writes, by what it is."""

    def __init__(self, reader: TelnetReader, *, after_interrupt: bytes = b"") -> None:
        self.reader = reader
        self.after_interrupt = after_interrupt
        self.replies: list[bytes] = []
        self.session: TelnetSession | None = None

    def write(self, data: bytes) -> None:
        s = self.session
        assert s is not None
        text = data.decode(errors="replace").replace("\r", "\n")
        if data == b"\x03":
            self.reader.feed_data(self.after_interrupt)
        elif writes_handshake(text, s.markers.ready):
            self.reader.feed_data(onlcr_handshake_reply(s.markers.ready).encode())
        elif probe_marker(text) == s._recover_marker:
            self.reader.feed_data(f"{s._recover_marker}0__\r\n".encode())
        elif self.replies:
            self.reader.feed_data(self.replies.pop(0))

    def is_closing(self) -> bool:
        return False

    def close(self) -> None:
        pass


async def _opened(*, after_interrupt: bytes = b"") -> "tuple[TelnetSession, _ScriptedShell]":
    reader = TelnetReader()
    shell = _ScriptedShell(reader, after_interrupt=after_interrupt)
    writer: Any = shell
    s = TelnetSession(reader=reader, writer=writer, client=stream_client(reader, writer))
    shell.session = s
    await s._ensure_initialized()
    assert s.terminal_mapping == "onlcr"
    return s, shell


async def test_a_split_character_is_held_for_the_next_read_not_replaced():
    s, shell = await _opened()
    shell.replies.append(f"{s.markers.begin}\r\n".encode() + b"a\r\nhal" + "é".encode()[:1])
    r = await s.run_cmd(cmd="c", timeout=0.05)
    assert r.value == "Command timed out after 0.05s\na\nhal"

    # The take itself, with no recovery after it: the first byte of `é` stays
    # in the decoder, and the second byte completes it in the next read.
    reader = s._reader
    await s._take_unread()  # what the recovery left after its match
    reader.feed_data(b"a\r\nhal" + "é".encode()[:1])
    assert await s.read_transport_until(pattern=re.compile(r"\n")) == "a\r\n"
    read = asyncio.ensure_future(s.read_transport_until(pattern=re.compile(r"\n")))
    await asyncio.sleep(0)
    read.cancel()
    with pytest.raises(asyncio.CancelledError):
        await read
    assert await s._take_unread() == "hal"
    reader.feed_data("é".encode()[1:] + b"\n")
    assert await s.read_transport_until(pattern=re.compile(r"\n")) == "é\n"


async def test_an_unfinished_line_past_twice_the_limit_is_kept_whole():
    s, shell = await _opened()
    line = b"x" * (2 * LIMIT + 10)
    shell.replies.append(f"{s.markers.begin}\r\n".encode() + line)
    r = await s.run_cmd(cmd="c", timeout=0.5)
    assert r.value == "Command timed out after 0.5s\n" + line.decode()


async def test_a_reader_carrying_the_connections_failure_takes_without_raising():
    s, _ = await _opened()
    s._reader.feed_data(b"held")
    s._reader.set_exception(ConnectionResetError())
    assert await s._take_unread() == ""


async def test_what_arrives_after_the_interrupt_is_not_the_commands():
    s, shell = await _opened(after_interrupt=b"^C\r\nfake$ ")
    m = s.markers
    shell.replies.append(f"{m.begin}\r\nhalf".encode())
    r = await s.run_cmd(cmd="c", timeout=0.05)
    assert r.value == "Command timed out after 0.05s\nhalf"
    shell.replies.append(f"{m.begin}\r\nok\r\n{m.end_prefix}0__\r\n".encode())
    nxt = await s.run_cmd(cmd="echo ok")
    assert nxt.value == "ok"
