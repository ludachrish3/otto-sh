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

from otto.host.transfer import validate_filename_lengths
from otto.host.embedded_transfer import (
    _FS_ABSENT_MSG,
    _WRITE_CHUNK,
    EmbeddedFileTransfer,
    _label_errno,
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
            # Zephyr 3.7's fs write syntax: `fs write <path> [-o <offset>] <byte>...`
            # otto always passes `-o <offset>` (the bare-integer form is
            # interpreted as a literal byte by Zephyr, which we found the
            # hard way against the live target).
            path = parts[2]
            if len(parts) >= 5 and parts[3] == '-o':
                offset = int(parts[4])
                hex_bytes = parts[5:]
            else:
                # Legacy form (no `-o`): all args after path are bytes,
                # implicit offset 0. Kept for completeness with the real
                # Zephyr shell's behavior, though otto no longer emits this.
                offset = 0
                hex_bytes = parts[3:]
            data = bytes(int(t, 16) for t in hex_bytes)
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
# Progress emission (PUT per-chunk, GET single-completion)
# ---------------------------------------------------------------------------

def _spy_handler():
    """Return ``(handler, calls)`` where calls captures (src, dst, done, total) tuples."""
    calls: list[tuple[str, str, int, int]] = []

    def handler(src, dst, done, total):
        calls.append((src, dst, done, total))

    return handler, calls


def _spy_factory(calls):
    """Return a TransferProgressFactory whose handlers all append to ``calls``.
    Mirrors the per-file factory pattern: each `factory()` call returns a
    fresh handler with its own (src, dst, done, total) appended events."""
    def factory():
        h, _ = _spy_handler()
        # rewrap so all handlers feed the same `calls` list — sufficient for
        # these tests since they only assert on per-file completion events.
        def shared(src, dst, done, total):
            calls.append((src, dst, done, total))
        return shared
    return factory


class TestPutProgress:
    """``EmbeddedFileTransfer`` emits per-32-byte-chunk progress events
    through the factory provided by ``BaseFileTransfer.put_files``."""

    @pytest.mark.asyncio
    async def test_per_chunk_emission_count_matches_ceiling(self, tmp_path):
        """A 100-byte file = ceil(100 / 32) = 4 chunks = 4 progress events."""
        fake = FakeZephyrFs()
        xfer = _console_transfer(fake)
        src = tmp_path / 'f.bin'
        src.write_bytes(b'x' * 100)

        calls: list = []
        status, err = await xfer._run_put([src], RAM, _spy_factory(calls))
        assert status == Status.Success, err
        assert len(calls) == 4
        # Bytes-done monotonically increases up to bytes-total.
        bytes_done = [done for _src, _dst, done, _total in calls]
        assert bytes_done == [32, 64, 96, 100]
        assert all(total == 100 for *_, total in calls)

    @pytest.mark.asyncio
    async def test_final_emission_is_at_completion(self, tmp_path):
        """Last progress event has bytes_done == bytes_total — the
        BaseFileTransfer contract that lets every file reach 100%."""
        fake = FakeZephyrFs()
        xfer = _console_transfer(fake)
        src = tmp_path / 'f.bin'
        src.write_bytes(b'y' * 50)

        calls: list = []
        await xfer._run_put([src], RAM, _spy_factory(calls))
        assert calls[-1][2] == calls[-1][3] == 50

    @pytest.mark.asyncio
    async def test_empty_file_emits_one_zero_event(self, tmp_path):
        """An empty file is a single ``fs write -o 0`` — emit one (0, 0)
        event so the progress bar still appears and completes."""
        fake = FakeZephyrFs()
        xfer = _console_transfer(fake)
        src = tmp_path / 'empty.bin'
        src.write_bytes(b'')

        calls: list = []
        status, err = await xfer._run_put([src], RAM, _spy_factory(calls))
        assert status == Status.Success, err
        assert calls == [(str(src), '/RAM:/empty.bin', 0, 0)]

    @pytest.mark.asyncio
    async def test_show_progress_false_runs_without_factory(self, tmp_path):
        """The public ``put_files(show_progress=False)`` path: BaseFileTransfer
        passes ``None``, ``_console_put_one`` short-circuits its handler
        check, no progress events fire — but the transfer still succeeds."""
        fake = FakeZephyrFs()
        xfer = _console_transfer(fake)
        src = tmp_path / 'f.bin'
        src.write_bytes(b'data')

        status, err = await xfer.put_files([src], RAM, show_progress=False)
        assert status == Status.Success, err
        # The factory was never built, so we can't directly capture "no events";
        # the success of put with no handler param is the contract.


class TestGetProgress:
    """GET is one monolithic ``fs read`` — single completion event per file."""

    @pytest.mark.asyncio
    async def test_single_completion_event_per_file(self, tmp_path):
        fake = FakeZephyrFs()
        fake.store['/RAM:/a.bin'] = bytearray(b'aaaa')
        fake.store['/RAM:/b.bin'] = bytearray(b'bbbbbb')
        xfer = _console_transfer(fake)

        calls: list = []
        status, err = await xfer._run_get(
            [RAM / 'a.bin', RAM / 'b.bin'], tmp_path, _spy_factory(calls),
        )
        assert status == Status.Success, err
        assert len(calls) == 2
        # Each event reports bytes_done == bytes_total (file-complete signal).
        for _src, _dst, done, total in calls:
            assert done == total > 0

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
# Errno translation — symbolic name + description on signed retcodes
# ---------------------------------------------------------------------------

class TestLabelErrno:
    """Zephyr's errnos are POSIX-aligned, so Python's stdlib ``errno`` is an
    authoritative source for symbol + description. ``_label_errno`` renders
    a signed retcode as ``-N (-NAME, description)`` so users don't have to
    grep ``errno.h`` to interpret ``(-22)`` vs ``(-28)``."""

    def test_negative_known_errno_labeled(self):
        # -22 = -EINVAL (the "Failed to seek" case from the user's report).
        assert _label_errno(-22) == "-22 (-EINVAL, Invalid argument)"

    def test_no_space_labeled(self):
        # -28 = -ENOSPC (the LittleFS out-of-space case).
        assert _label_errno(-28) == "-28 (-ENOSPC, No space left on device)"

    def test_negative_unknown_falls_back(self):
        # Sentinel that's deliberately outside any POSIX errno table.
        result = _label_errno(-9999)
        assert result == "-9999"  # no parens, no fabricated symbol

    def test_positive_passthrough(self):
        # Positive values aren't errnos — render as plain integer.
        assert _label_errno(0) == "0"
        assert _label_errno(127) == "127"

    @pytest.mark.asyncio
    async def test_write_error_message_includes_symbolic_errno(self, tmp_path):
        """End-to-end: a Zephyr ``fs write`` failure surfaces ``-N (-NAME, …)``
        in the user-facing error string instead of bare ``-N``."""

        class FailingFs(FakeZephyrFs):
            async def exec_cmd(self, cmd, timeout=None):
                if cmd.startswith('fs write'):
                    return CommandStatus(
                        cmd, 'Failed to seek /RAM:/f (-22)', Status.Failed, -8,
                    )
                return await super().exec_cmd(cmd, timeout)

        # Note: Zephyr returns -8 (-ENOEXEC) from the shell when a builtin
        # itself fails — the actual filesystem errno (-22) is in the
        # output text. _label_errno labels whatever it gets; verify it
        # decorates the shell retcode in the same message shape.
        fake = FailingFs()
        xfer = _console_transfer(fake)
        src = tmp_path / 'f.bin'
        src.write_bytes(b'x' * 64)
        status, err = await xfer.put_files([src], RAM)
        assert status == Status.Error
        assert '-ENOEXEC' in err
        assert 'Exec format error' in err


# ---------------------------------------------------------------------------
# Cleanup hook — partial dest file is removed on mid-transfer failure
# ---------------------------------------------------------------------------

class TestCleanupOnFailure:
    """A failed ``fs write`` mid-loop leaves a partial file on the device.
    On a capacity-bound filesystem (FAT/RAM, LittleFS) those leftover bytes
    block any retry — the next put tries to write the same file, gets the
    same -ENOSPC because the previous attempt's bytes are still there.
    The cleanup hook issues a best-effort ``fs rm <dest>`` so the next
    attempt starts from a clean slate."""

    class _FailAfterNWrites(FakeZephyrFs):
        """Fake whose Nth ``fs write`` returns -ENOSPC; earlier writes
        succeed normally. Models the "ran out of space mid-transfer"
        case from the user's report."""

        def __init__(self, fail_after: int) -> None:
            super().__init__()
            self.fail_after = fail_after
            self.write_count = 0

        async def exec_cmd(self, cmd, timeout=None):
            if cmd.startswith('fs write'):
                self.write_count += 1
                if self.write_count > self.fail_after:
                    self.calls.append(cmd)
                    return CommandStatus(
                        cmd, 'Failed to write /RAM:/f (-28)', Status.Failed, -8,
                    )
            return await super().exec_cmd(cmd, timeout)

    @pytest.mark.asyncio
    async def test_partial_write_triggers_cleanup_rm(self, tmp_path):
        """When ``fs write`` fails mid-loop, ``_console_put_one`` issues a
        final ``fs rm <dest>`` before returning the error."""
        fake = self._FailAfterNWrites(fail_after=2)  # 3rd write fails
        xfer = _console_transfer(fake)
        # 4 chunks worth of data (32 * 4 = 128 bytes) — the 3rd chunk write
        # triggers the simulated -ENOSPC, leaving 64 bytes already on the FS.
        src = tmp_path / 'f.bin'
        src.write_bytes(b'x' * 128)

        status, err = await xfer.put_files([src], RAM)
        assert status == Status.Error
        # The PRE-write rm at the top of _console_put_one is the first
        # rm; the cleanup rm after the failure is the second. Two `fs rm`
        # calls against the same dest path is the signal we want.
        rm_calls = [c for c in fake.calls if c == 'fs rm /RAM:/f.bin']
        assert len(rm_calls) == 2, (
            f"expected pre-write rm + post-failure cleanup rm, got: {fake.calls}"
        )

    @pytest.mark.asyncio
    async def test_cleanup_failure_does_not_mask_real_error(self, tmp_path):
        """If the cleanup ``fs rm`` itself errors (e.g. session dead), the
        original transfer error must still propagate — cleanup is
        best-effort, not a precondition for reporting failure."""

        class FlakyCleanupFs(self._FailAfterNWrites):
            async def exec_cmd(self, cmd, timeout=None):
                # Let the first rm (pre-write) succeed; raise on the
                # cleanup rm (the second one).
                if cmd.startswith('fs rm') and 'cleanup' not in cmd:
                    self.rm_seen = getattr(self, 'rm_seen', 0) + 1
                    if self.rm_seen == 2:
                        raise RuntimeError("device went away")
                return await super().exec_cmd(cmd, timeout)

        fake = FlakyCleanupFs(fail_after=1)
        xfer = _console_transfer(fake)
        src = tmp_path / 'f.bin'
        src.write_bytes(b'x' * 64)

        status, err = await xfer.put_files([src], RAM)
        # The original -ENOSPC error wins; the cleanup exception is logged
        # and swallowed.
        assert status == Status.Error
        assert 'Failed to write' in err or '-ENOSPC' in err

    @pytest.mark.asyncio
    async def test_successful_write_does_not_cleanup(self, tmp_path):
        """No spurious cleanup when the write succeeds — the pre-write
        ``fs rm`` is the only one we expect on the happy path."""
        fake = FakeZephyrFs()
        xfer = _console_transfer(fake)
        src = tmp_path / 'f.bin'
        src.write_bytes(b'x' * 50)
        status, err = await xfer.put_files([src], RAM)
        assert status == Status.Success, err
        rm_calls = [c for c in fake.calls if c.startswith('fs rm')]
        assert len(rm_calls) == 1, (
            f"happy path should issue only the pre-write rm; got: {rm_calls}"
        )


# ---------------------------------------------------------------------------
# Shared validator (used by FileTransfer and EmbeddedFileTransfer alike)
# ---------------------------------------------------------------------------

class TestValidateFilenameLengths:
    """Direct coverage for the shared helper — both Unix and embedded
    transfer paths call it, so the contract is unit-pinned here once and
    every backend inherits the same error shape."""

    def test_under_limit_returns_success(self):
        status, err = validate_filename_lengths(
            [Path('/a/short.bin')], limit=255, host_name='h',
        )
        assert status == Status.Success
        assert err == ''

    def test_at_limit_is_accepted(self):
        name = 'x' * 255  # exactly at the limit
        status, err = validate_filename_lengths(
            [Path('/a') / name], limit=255, host_name='h',
        )
        assert status == Status.Success, err

    def test_over_limit_reports_offending_name_and_host(self):
        name = 'x' * 256
        status, err = validate_filename_lengths(
            [Path('/a') / name], limit=255, host_name='myhost',
        )
        assert status == Status.Error
        assert name in err
        assert '255-character' in err
        assert 'myhost' in err

    def test_first_offender_short_circuits(self):
        """The first over-limit file in the list is what gets reported; the
        helper does not enumerate every offender (the user fixes one, retries,
        sees the next). This keeps the message focused."""
        ok = Path('/a/ok.bin')
        bad = Path('/a') / ('x' * 100)
        status, err = validate_filename_lengths(
            [ok, bad], limit=50, host_name='h',
        )
        assert status == Status.Error
        assert 'x' * 100 in err


# ---------------------------------------------------------------------------
# Per-host filename length limit
# ---------------------------------------------------------------------------

class TestMaxFilenameLen:
    """``max_filename_len`` exists so a target that rejects long names (FAT
    8.3 without ``CONFIG_FS_FATFS_LFN``) surfaces a clear, actionable error
    instead of the device's opaque ``Failed to open … (-2)``."""

    @pytest.mark.asyncio
    async def test_put_rejects_over_limit_basename_with_clear_message(
        self, tmp_path,
    ):
        fake = FakeZephyrFs()
        xfer = EmbeddedFileTransfer(
            transfer='console', name='sprout',
            exec_cmd=fake.exec_cmd, max_filename_len=12,
        )
        # 14 chars > 12: should be rejected before any fs command runs.
        src = tmp_path / 'concurrent.bin'
        src.write_bytes(b'x')
        status, err = await xfer.put_files([src], RAM)
        assert status == Status.Error
        assert 'concurrent.bin' in err
        assert '12-character' in err
        assert 'sprout' in err
        # The fake's store stays empty — the check fires before fs_write.
        assert fake.store == {}

    @pytest.mark.asyncio
    async def test_get_rejects_over_limit_basename(self, tmp_path):
        fake = FakeZephyrFs()
        xfer = EmbeddedFileTransfer(
            transfer='console', name='sprout',
            exec_cmd=fake.exec_cmd, max_filename_len=12,
        )
        status, err = await xfer.get_files([RAM / 'concurrent.bin'], tmp_path)
        assert status == Status.Error
        assert 'concurrent.bin' in err

    @pytest.mark.asyncio
    async def test_at_limit_basename_is_accepted(self, tmp_path):
        """A name exactly at the limit must be accepted — off-by-one matters
        because the existing contract test uses ``contract.bin`` (12 chars)."""
        fake = FakeZephyrFs()
        xfer = EmbeddedFileTransfer(
            transfer='console', name='sprout',
            exec_cmd=fake.exec_cmd, max_filename_len=12,
        )
        src = tmp_path / 'contract.bin'  # 12 chars exactly
        src.write_bytes(b'ok')
        status, err = await xfer.put_files([src], RAM)
        assert status == Status.Success, err

    @pytest.mark.asyncio
    async def test_default_limit_accepts_typical_filenames(self, tmp_path):
        """Default limit (255 — Linux ``NAME_MAX``) accommodates any name a
        normal filesystem will accept, so the default is essentially a
        runaway-guard rather than a real constraint for realistic workloads."""
        fake = FakeZephyrFs()
        xfer = _console_transfer(fake)  # default max_filename_len=255
        src = tmp_path / 'some_quite_long_filename.bin'
        src.write_bytes(b'ok')
        status, err = await xfer.put_files([src], RAM)
        assert status == Status.Success, err

    @pytest.mark.asyncio
    async def test_default_limit_rejects_runaway_filename(self):
        """A pathologically long name (over 255 chars) is rejected even with
        no per-host override — the default catches obvious bugs everywhere.

        The check inspects ``path.name`` only and never touches the host
        filesystem, so the source Path doesn't need a real file behind it
        (which is good — the host's own filesystem also caps at 255)."""
        fake = FakeZephyrFs()
        xfer = _console_transfer(fake)  # default max_filename_len=255
        runaway = Path('/nowhere') / ('x' * 260 + '.bin')
        status, err = await xfer.put_files([runaway], RAM)
        assert status == Status.Error
        assert '255-character' in err


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
