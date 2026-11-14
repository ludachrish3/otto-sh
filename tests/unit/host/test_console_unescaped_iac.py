"""A console server that does not escape 0xFF: what reaches otto, pinned on the real reader.

QEMU's ``-serial telnet:`` bridge negotiates binary mode, then writes the
guest's bytes as they are, so a 0xFF the guest wrote arrives as a bare IAC
instead of ``IAC IAC``. Every test here serves bytes from a loopback server
behind QEMU's captured greeting and reads them through
:func:`otto.host.telnet.open_telnet_connection` (telnetlib3 and otto's NVT
reader, the path every console takes). That is what
:data:`otto.host.options.UNESCAPED_IAC` declares, and what the output
contract's declared arm expects
(:func:`tests.conformance.test_output_contract.carried_unescaped`).

Two evidence classes, kept apart:

- :data:`WIRE_CAPTURES` are bytes a raw TCP read of bb1350's console
  (test1:2450) captured on 2026-11-13, with one inferred row marked as such.
- :data:`REPLAYS` are byte sequences no capture holds, REPLAYED THROUGH THE
  READER (telnetlib3 5.0.1, 2026-11-13) to learn what a 0xFF followed by
  each kind of byte costs. They pin the reader, not the console.
"""

import asyncio

import pytest

from otto.host.option_specs import ConsoleOptionsSpec
from otto.host.options import UNESCAPED_IAC, ConsoleOptions
from otto.host.telnet import open_telnet_connection
from tests.conformance.test_output_contract import carried_unescaped

#: QEMU's greeting, as captured: WILL ECHO, WILL SGA, WILL BINARY, DO BINARY.
QEMU_GREETING = bytes.fromhex("fffb01fffb03fffb00fffd00")

#: What the guest wrote, and the bytes on the wire for it (before the prompt).
WIRE_CAPTURES = {
    # INFERRED, not captured: the wire bytes follow from the `SA\377BE`
    # capture plus the guest tty's `\n` -> `\r\n`. What is measured for this
    # row is the bed RESULT: `run` and `exec` of printf '\377\n' answer ''.
    "row invalid-utf8 (wire inferred)": (b"\xff\n", "ff0d0a"),
    "ff": (b"SA\xffBE\n", "5341ff42450d0a"),
    "ff ff": (b"SA\xff\xffBE\n", "5341ffff42450d0a"),
    "fe": (b"SA\xfeBE\n", "5341fe42450d0a"),
    "fd": (b"SA\xfdBE\n", "5341fd42450d0a"),
    "fb": (b"SA\xfbBE\n", "5341fb42450d0a"),
}

#: REPLAYED THROUGH THE READER: what the guest would write (it reaches the
#: wire unescaped, with `\n` as `\r\n`), and what the reader yields for it.
REPLAYS = {
    "ff + ordinary byte: the 0xFF only": (b"SA\xffBE\n", b"SABE\r\n"),
    "ff ff: one 0xFF": (b"SA\xff\xffBE\n", b"SA\xffBE\r\n"),
    "ff 00: the 0xFF only": (b"SA\xff\x00BE\n", b"SA\x00BE\r\n"),
    "ff f0: the 0xFF only": (b"SA\xff\xf0BE\n", b"SA\xf0BE\r\n"),
    "ff fb 01: three bytes": (b"SA\xff\xfb\x01BE\n", b"SABE\r\n"),
    "ff fc 01: three bytes": (b"SA\xff\xfc\x01BE\n", b"SABE\r\n"),
    "ff fd 01: three bytes": (b"SA\xff\xfd\x01BE\n", b"SABE\r\n"),
    "ff fe 01: three bytes": (b"SA\xff\xfe\x01BE\n", b"SABE\r\n"),
    "ff f1: two bytes": (b"SA\xff\xf1BE\n", b"SABE\r\n"),
    "ff f9: two bytes": (b"SA\xff\xf9BE\n", b"SABE\r\n"),
}

#: REPLAYED THROUGH THE READER: a 0xFF then 0xFA swallows output until 0xFF 0xF0.
SUBNEGOTIATION_REPLAYS = {
    "no ff f0 follows: everything after is swallowed": (
        "5341fffa0142434445460d0a",
        b"SA",
    ),
    "ff f0 follows: swallowed through it": ("5341fffa01424344fff0450d0a", b"SAE\r\n"),
}


async def _read_through_otto(wire: bytes) -> bytes:
    """Serve QEMU's greeting then *wire* on loopback; return every data byte the reader yields."""

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(QEMU_GREETING)
        await writer.drain()
        await reader.read(64)  # the client's replies to the greeting
        writer.write(wire)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        reader, writer = await open_telnet_connection(
            "127.0.0.1", port=port, **ConsoleOptions()._open_kwargs()
        )
        data = b""
        while chunk := await reader.read(4096):
            data += chunk
        writer.close()
        return data
    finally:
        server.close()
        await server.wait_closed()


def _on_the_wire(written: bytes) -> bytes:
    """*written* as the unescaping server sends it: as it is, the tty's ``\\n`` as ``\\r\\n``."""
    return written.replace(b"\n", b"\r\n")


@pytest.mark.asyncio
@pytest.mark.parametrize("name", list(WIRE_CAPTURES))
async def test_the_captured_wire_reads_as_the_declaration_says(name: str) -> None:
    written, wire = WIRE_CAPTURES[name]
    got = await _read_through_otto(bytes.fromhex(wire))
    assert got == _on_the_wire(carried_unescaped(written))


@pytest.mark.asyncio
@pytest.mark.parametrize("name", list(REPLAYS))
async def test_a_replay_through_the_reader_loses_what_the_helper_says(name: str) -> None:
    written, read = REPLAYS[name]
    got = await _read_through_otto(_on_the_wire(written))
    assert got == read
    assert _on_the_wire(carried_unescaped(written)) == read


@pytest.mark.asyncio
@pytest.mark.parametrize("name", list(SUBNEGOTIATION_REPLAYS))
async def test_a_0xff_then_0xfa_swallows_output_until_0xff_0xf0(name: str) -> None:
    wire, read = SUBNEGOTIATION_REPLAYS[name]
    assert await _read_through_otto(bytes.fromhex(wire)) == read


@pytest.mark.parametrize("written", [b"SA\xff\xfa\x01BE\n", b"SA\xff"])
def test_the_helper_refuses_what_it_cannot_model(written: bytes) -> None:
    with pytest.raises(ValueError, match="0xFF"):
        carried_unescaped(written)


def test_a_compliant_console_declares_nothing_and_an_unescaping_one_declares_unescaped_iac() -> (
    None
):
    assert ConsoleOptions().exact_output is None
    assert ConsoleOptions(escapes_iac=False).exact_output is UNESCAPED_IAC
    spec = ConsoleOptionsSpec(server="s", port=1, escapes_iac=False)
    assert spec.to_runtime().exact_output is UNESCAPED_IAC
    assert ConsoleOptionsSpec(server="s", port=1).to_runtime().escapes_iac is True
    assert ConsoleOptions.declaring_value("escapes_iac") is False
