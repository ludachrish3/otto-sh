"""
Unit tests for EmbeddedFileTransfer — console file transfer for embedded hosts.

The tests drive the real :class:`EmbeddedFileTransfer` against a
``FakeZephyrFs`` double: an in-memory model of a Zephyr target's ``fs`` shell
that interprets ``fs read``/``fs write``/``fs rm`` and renders ``fs read``
output in the device's real hexdump format. No connection is involved, so the
hexdump encode/decode and the chunked-write logic are exercised end to end.
"""

from pathlib import Path

import pytest

from otto.host.embedded_transfer import (
    _FS_ABSENT_MSG,
    _WRITE_CHUNK,
    EmbeddedFileTransfer,
)
from otto.utils import CommandStatus, Status

# A Zephyr filesystem mount path — the destination directory for `put`.
RAM = Path('/RAM:')


# ---------------------------------------------------------------------------
# Test double: an in-memory Zephyr `fs` shell
# ---------------------------------------------------------------------------

def _hexdump(data: bytes) -> str:
    """Render bytes as a Zephyr ``fs read`` hexdump (16 bytes/line, tab gutter)."""
    if not data:
        return ''
    lines = []
    for off in range(0, len(data), 16):
        chunk = data[off:off + 16]
        hexpart = ' '.join(f'{b:02X}' for b in chunk)
        ascii_part = ''.join(chr(b) if 32 <= b < 127 else '.' for b in chunk)
        lines.append(f'{off:08X}  {hexpart}\t{ascii_part}')
    return '\n'.join(lines)


class FakeZephyrFs:
    """In-memory stand-in for a Zephyr target's ``fs`` shell.

    Models just enough of ``fs read``/``fs write``/``fs rm`` to round-trip
    files. ``fs_available=False`` simulates a target whose firmware has no
    ``fs`` shell command at all.
    """

    def __init__(self, fs_available: bool = True) -> None:
        self.store: dict[str, bytearray] = {}
        self.fs_available = fs_available
        self.calls: list[str] = []

    async def exec_cmd(self, cmd: str, timeout: float | None = None) -> CommandStatus:
        self.calls.append(cmd)
        if not self.fs_available:
            return CommandStatus(cmd, 'fs: command not found', Status.Failed, -8)

        parts = cmd.split()
        sub = parts[1]

        if sub == 'read':
            path = parts[2]
            if path not in self.store:
                return CommandStatus(cmd, f'Failed to open {path} (-2)', Status.Failed, -2)
            return CommandStatus(cmd, _hexdump(bytes(self.store[path])), Status.Success, 0)

        if sub == 'write':
            path = parts[2]
            offset = int(parts[3])
            data = bytes(int(t, 16) for t in parts[4:])
            buf = self.store.setdefault(path, bytearray())
            if offset + len(data) > len(buf):
                buf.extend(b'\x00' * (offset + len(data) - len(buf)))
            buf[offset:offset + len(data)] = data
            return CommandStatus(cmd, '', Status.Success, 0)

        if sub == 'rm':
            path = parts[2]
            if path in self.store:
                del self.store[path]
                return CommandStatus(cmd, '', Status.Success, 0)
            return CommandStatus(cmd, f'Failed to remove {path} (-2)', Status.Failed, -2)

        return CommandStatus(cmd, f'{sub}: unknown subcommand', Status.Failed, -22)


def _console_transfer(fake: FakeZephyrFs) -> EmbeddedFileTransfer:
    return EmbeddedFileTransfer(transfer='console', name='sprout', exec_cmd=fake.exec_cmd)


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------

class TestRoundTrip:

    @pytest.mark.asyncio
    async def test_small_text_file(self, tmp_path):
        fake = FakeZephyrFs()
        xfer = _console_transfer(fake)
        src = tmp_path / 'hello.txt'
        src.write_bytes(b'hello zephyr\n')

        status, err = await xfer.put_files([src], RAM)
        assert status == Status.Success, err

        dest_dir = tmp_path / 'pulled'
        dest_dir.mkdir()
        status, err = await xfer.get_files([RAM / 'hello.txt'], dest_dir)
        assert status == Status.Success, err
        assert (dest_dir / 'hello.txt').read_bytes() == b'hello zephyr\n'

    @pytest.mark.asyncio
    async def test_multichunk_file(self, tmp_path):
        """A file larger than _WRITE_CHUNK is split across several fs writes."""
        fake = FakeZephyrFs()
        xfer = _console_transfer(fake)
        payload = bytes(range(100))  # 100 > _WRITE_CHUNK
        src = tmp_path / 'blob.bin'
        src.write_bytes(payload)

        status, err = await xfer.put_files([src], RAM)
        assert status == Status.Success, err
        assert bytes(fake.store['/RAM:/blob.bin']) == payload

        writes = [c for c in fake.calls if c.startswith('fs write')]
        assert len(writes) == -(-len(payload) // _WRITE_CHUNK)  # ceil division

        status, err = await xfer.get_files([RAM / 'blob.bin'], tmp_path)
        assert status == Status.Success, err
        assert (tmp_path / 'blob.bin').read_bytes() == payload

    @pytest.mark.asyncio
    async def test_all_byte_values(self, tmp_path):
        """A partial last line (262 bytes) and every byte value round-trip."""
        fake = FakeZephyrFs()
        xfer = _console_transfer(fake)
        payload = bytes(range(256)) + b'\x00\xff\x10\x7f\x80\x41'
        src = tmp_path / 'full.bin'
        src.write_bytes(payload)

        await xfer.put_files([src], RAM)
        await xfer.get_files([RAM / 'full.bin'], tmp_path)
        assert (tmp_path / 'full.bin').read_bytes() == payload

    @pytest.mark.asyncio
    async def test_empty_file(self, tmp_path):
        fake = FakeZephyrFs()
        xfer = _console_transfer(fake)
        src = tmp_path / 'empty.txt'
        src.write_bytes(b'')

        status, err = await xfer.put_files([src], RAM)
        assert status == Status.Success, err
        assert '/RAM:/empty.txt' in fake.store

        status, err = await xfer.get_files([RAM / 'empty.txt'], tmp_path)
        assert status == Status.Success, err
        assert (tmp_path / 'empty.txt').read_bytes() == b''


# ---------------------------------------------------------------------------
# put — chunking and ordering
# ---------------------------------------------------------------------------

class TestPut:

    @pytest.mark.asyncio
    async def test_removes_destination_before_write(self, tmp_path):
        """fs write never truncates, so put removes the destination first."""
        fake = FakeZephyrFs()
        xfer = _console_transfer(fake)
        src = tmp_path / 'f.bin'
        src.write_bytes(b'data')

        await xfer.put_files([src], RAM)
        rm_idx = fake.calls.index('fs rm /RAM:/f.bin')
        first_write = next(i for i, c in enumerate(fake.calls) if c.startswith('fs write'))
        assert rm_idx < first_write

    @pytest.mark.asyncio
    async def test_stale_tail_does_not_survive_overwrite(self, tmp_path):
        """Overwriting a longer file with a shorter one leaves no stale bytes."""
        fake = FakeZephyrFs()
        xfer = _console_transfer(fake)
        src = tmp_path / 'f.bin'
        src.write_bytes(b'X' * 80)
        await xfer.put_files([src], RAM)

        src.write_bytes(b'short')
        await xfer.put_files([src], RAM)
        assert bytes(fake.store['/RAM:/f.bin']) == b'short'

    @pytest.mark.asyncio
    async def test_multiple_files_are_sequential(self, tmp_path):
        """Files transfer one fully before the next — a single console."""
        fake = FakeZephyrFs()
        xfer = _console_transfer(fake)
        a = tmp_path / 'a.bin'
        b = tmp_path / 'b.bin'
        a.write_bytes(b'aaa')
        b.write_bytes(b'bbb')

        await xfer.put_files([a, b], RAM)
        last_a = max(i for i, c in enumerate(fake.calls) if '/RAM:/a.bin' in c)
        first_b = min(i for i, c in enumerate(fake.calls) if '/RAM:/b.bin' in c)
        assert last_a < first_b


# ---------------------------------------------------------------------------
# get — failure modes
# ---------------------------------------------------------------------------

class TestGet:

    @pytest.mark.asyncio
    async def test_missing_file_is_a_file_error(self, tmp_path):
        """A missing file fails — but not with the fs-absent message."""
        fake = FakeZephyrFs()
        xfer = _console_transfer(fake)
        status, err = await xfer.get_files([RAM / 'nope.txt'], tmp_path)
        assert status == Status.Error
        assert err != _FS_ABSENT_MSG
        assert 'fs read' in err

    @pytest.mark.asyncio
    async def test_corrupt_hexdump_gap_is_reported(self, tmp_path):
        """A dropped hexdump line is caught, not silently mis-decoded."""
        async def gappy_exec(cmd: str, timeout: float | None = None) -> CommandStatus:
            # Line at offset 0x10 is missing — 0x00 then 0x20.
            dump = (
                '00000000  ' + ' '.join('41' for _ in range(16)) + '\tAAAA\n'
                '00000020  42 42\tBB'
            )
            return CommandStatus(cmd, dump, Status.Success, 0)

        xfer = EmbeddedFileTransfer(transfer='console', name='sprout', exec_cmd=gappy_exec)
        status, err = await xfer.get_files([RAM / 'x.bin'], tmp_path)
        assert status == Status.Error
        assert 'gap or overlap' in err


# ---------------------------------------------------------------------------
# fs shell absent
# ---------------------------------------------------------------------------

class TestFsAbsent:

    @pytest.mark.asyncio
    async def test_get_reports_clear_error(self, tmp_path):
        fake = FakeZephyrFs(fs_available=False)
        xfer = _console_transfer(fake)
        status, err = await xfer.get_files([RAM / 'f'], tmp_path)
        assert status == Status.Error
        assert err == _FS_ABSENT_MSG

    @pytest.mark.asyncio
    async def test_put_reports_clear_error(self, tmp_path):
        fake = FakeZephyrFs(fs_available=False)
        xfer = _console_transfer(fake)
        src = tmp_path / 'f'
        src.write_bytes(b'data')
        status, err = await xfer.put_files([src], RAM)
        assert status == Status.Error
        assert err == _FS_ABSENT_MSG


# ---------------------------------------------------------------------------
# tftp — reserved, not implemented
# ---------------------------------------------------------------------------

class TestTftp:

    @pytest.mark.asyncio
    async def test_get_raises_not_implemented(self, tmp_path):
        fake = FakeZephyrFs()
        xfer = EmbeddedFileTransfer(transfer='tftp', name='sprout', exec_cmd=fake.exec_cmd)
        with pytest.raises(NotImplementedError):
            await xfer.get_files([RAM / 'f'], tmp_path)

    @pytest.mark.asyncio
    async def test_put_raises_not_implemented(self, tmp_path):
        fake = FakeZephyrFs()
        xfer = EmbeddedFileTransfer(transfer='tftp', name='sprout', exec_cmd=fake.exec_cmd)
        src = tmp_path / 'f'
        src.write_bytes(b'data')
        with pytest.raises(NotImplementedError):
            await xfer.put_files([src], RAM)


# ---------------------------------------------------------------------------
# Hexdump decoding (the static method, directly)
# ---------------------------------------------------------------------------

class TestDecodeHexdump:

    def test_empty(self):
        assert EmbeddedFileTransfer._decode_hexdump('') == b''

    def test_partial_last_line(self):
        data = b'0123456789ABCDEF' + b'GHIJ'  # 16 + 4
        assert EmbeddedFileTransfer._decode_hexdump(_hexdump(data)) == data

    def test_no_tab_fixed_column_fallback(self):
        """A hexdump with no tab gutter still decodes via the fixed hex field."""
        chunk = b'hello'
        hex_field = ''.join(f'{b:02X} ' for b in chunk) + '   ' * (16 - len(chunk))
        line = f'00000000  {hex_field}hello'
        assert EmbeddedFileTransfer._decode_hexdump(line) == chunk

    def test_gap_raises_value_error(self):
        dump = (
            '00000000  ' + ' '.join('41' for _ in range(16)) + '\tAAAA\n'
            '00000020  42\tB'  # should be 00000010
        )
        with pytest.raises(ValueError, match='gap or overlap'):
            EmbeddedFileTransfer._decode_hexdump(dump)
