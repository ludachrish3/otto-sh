"""Tests for the lcov merger."""

import struct
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from otto.coverage.errors import CoverageDataMismatchError
from otto.coverage.merge.merger import LcovMerger
from otto.host.local_host import LocalHost
from otto.host.toolchain import Toolchain
from otto.result import CommandResult
from otto.utils import Status


def _gcov_header(magic: bytes, version: bytes, stamp: int) -> bytes:
    """A minimal 12-byte gcov file header: magic, format version, build stamp.

    GNU gcov and clang's gcov-compatible mode agree on this layout for both
    ``.gcno`` and ``.gcda`` files; the merger's structural stamp check reads
    nothing beyond it.
    """
    return magic + version + struct.pack("<I", stamp)


def _write_pair(
    gcda_dir: Path,
    gcno_dir: Path,
    stem: str,
    *,
    gcno_stamp: int,
    gcda_stamp: int,
    gcno_version: bytes = b"B33*",
    gcda_version: bytes = b"B33*",
) -> None:
    gcda_dir.mkdir(parents=True, exist_ok=True)
    gcno_dir.mkdir(parents=True, exist_ok=True)
    (gcno_dir / f"{stem}.gcno").write_bytes(_gcov_header(b"oncg", gcno_version, gcno_stamp))
    (gcda_dir / f"{stem}.gcda").write_bytes(_gcov_header(b"adcg", gcda_version, gcda_stamp))


class TestLcovMerger:
    @pytest.mark.asyncio
    async def test_capture_success(self, tmp_path):
        localhost = LocalHost()
        merger = LcovMerger(localhost)

        gcda_dir = tmp_path / "gcda"
        gcda_dir.mkdir()
        gcno_dir = tmp_path / "gcno"
        gcno_dir.mkdir()
        output = tmp_path / "out.info"

        with patch.object(localhost, "exec", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = CommandResult(
                Status.Success, value="", command="lcov --capture ...", retcode=0
            )
            result = await merger.capture(gcda_dir, gcno_dir, output)
            assert result == output
            mock_exec.assert_called_once()
        await localhost.close()

    @pytest.mark.asyncio
    async def test_capture_failure_raises(self, tmp_path):
        localhost = LocalHost()
        merger = LcovMerger(localhost)

        with patch.object(localhost, "exec", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = CommandResult(
                Status.Failed,
                value="error: no gcda files found",
                command="lcov --capture ...",
                retcode=1,
            )
            with pytest.raises(RuntimeError, match="lcov --capture failed"):
                await merger.capture(tmp_path / "gcda", tmp_path / "gcno", tmp_path / "out.info")
        await localhost.close()

    @pytest.mark.asyncio
    async def test_capture_stamp_mismatch_raises_typed_helpful_error(self, tmp_path):
        """A gcov 'stamp mismatch' (product rebuilt after the test run) must
        surface as CoverageDataMismatchError whose message names the likely
        cause and the remedy — not a bare RuntimeError of raw lcov output."""
        from otto.coverage.errors import CoverageDataMismatchError

        localhost = LocalHost()
        merger = LcovMerger(localhost)

        with patch.object(localhost, "exec", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = CommandResult(
                Status.Failed,
                value=(
                    "/x/cov/carrot_seed/product-math_ops.gcda:stamp mismatch with notes file\n"
                    "geninfo: ERROR: GCOV failed for /x/cov/carrot_seed/product-math_ops.gcda!"
                ),
                command="lcov --capture ...",
                retcode=1,
            )
            with pytest.raises(CoverageDataMismatchError) as ei:
                await merger.capture(tmp_path / "gcda", tmp_path / "gcno", tmp_path / "out.info")
        msg = str(ei.value)
        assert "rebuilt" in msg  # names the likely cause
        assert "otto test --cov" in msg  # names the remedy
        assert "stamp mismatch" in msg  # carries the underlying evidence
        await localhost.close()

    @pytest.mark.asyncio
    async def test_capture_incompatible_tool_raises_typed_helpful_error(self, tmp_path):
        """geninfo's 'Incompatible GCC/GCOV version' (e.g. a clang build
        captured with GNU gcov — clang emits the GCC 4.8-era file format) must
        surface as CoverageToolVersionError naming the cause and the fix, not
        a bare RuntimeError of raw lcov output."""
        from otto.coverage.errors import CoverageToolVersionError

        localhost = LocalHost()
        merger = LcovMerger(localhost)

        with patch.object(localhost, "exec", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = CommandResult(
                Status.Failed,
                value=(
                    "/x/cov/host0/sample-sample.gcda:version '408*', prefer version 'B33*'\n"
                    "geninfo: ERROR: Incompatible GCC/GCOV version found while processing "
                    "/x/cov/host0/sample-sample.gcda:\n"
                    "\tYour test was built with '4.8'.\n"
                    "\tYou are trying to capture with gcov tool '/usr/bin/gcov' "
                    "which is version 'B33*'."
                ),
                command="lcov --capture ...",
                retcode=1,
            )
            with pytest.raises(CoverageToolVersionError) as ei:
                await merger.capture(tmp_path / "gcda", tmp_path / "gcno", tmp_path / "out.info")
        msg = str(ei.value)
        assert "clang" in msg  # names the most likely cause
        assert "llvm-cov" in msg  # names the fix
        assert "408*" in msg  # carries the underlying evidence
        await localhost.close()

    @pytest.mark.asyncio
    async def test_merge_info_files(self, tmp_path):
        localhost = LocalHost()
        merger = LcovMerger(localhost)

        info1 = tmp_path / "a.info"
        info2 = tmp_path / "b.info"
        output = tmp_path / "merged.info"

        with patch.object(localhost, "exec", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = CommandResult(
                Status.Success, value="", command="lcov ...", retcode=0
            )
            result = await merger.merge_info_files([info1, info2], output)
            assert result == output
        await localhost.close()

    @pytest.mark.asyncio
    async def test_merge_empty_raises(self, tmp_path):
        localhost = LocalHost()
        merger = LcovMerger(localhost)
        with pytest.raises(ValueError, match=r"No \.info files"):
            await merger.merge_info_files([], tmp_path / "out.info")
        await localhost.close()

    @pytest.mark.asyncio
    async def test_capture_and_merge_single_host(self, tmp_path):
        localhost = LocalHost()
        merger = LcovMerger(localhost)

        gcda_dir = tmp_path / "host0"
        gcda_dir.mkdir()
        work_dir = tmp_path / "work"

        with patch.object(localhost, "exec", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = CommandResult(
                Status.Success, value="", command="lcov ...", retcode=0
            )
            result = await merger.capture_and_merge([gcda_dir], tmp_path / "gcno", work_dir)
            # Single host = no merge step, returns the captured info directly
            assert result == work_dir / "host_0.info"
            assert mock_exec.call_count == 1
        await localhost.close()


class TestLcovMergerStampGuard:
    """Structural .gcda↔.gcno stamp verification, run before lcov.

    GNU gcov refuses a stamp mismatch loudly, but llvm-cov (clang builds)
    prints its complaint and exits 0 — lcov then succeeds with the file
    recorded at all-zero hits and nothing in its output to parse. The only
    reliable detection is comparing the file headers directly, before lcov
    is invoked at all.
    """

    @pytest.mark.asyncio
    async def test_capture_detects_stamp_mismatch_before_lcov(self, tmp_path):
        """Mismatched stamps raise CoverageDataMismatchError without running
        lcov — even when lcov WOULD succeed (the silent clang mode)."""
        localhost = LocalHost()
        merger = LcovMerger(localhost)
        gcda_dir, gcno_dir = tmp_path / "gcda", tmp_path / "gcno"
        _write_pair(gcda_dir, gcno_dir, "prod", gcno_stamp=0x1111AAAA, gcda_stamp=0x2222BBBB)

        with patch.object(localhost, "exec", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = CommandResult(
                Status.Success, value="", command="lcov --capture ...", retcode=0
            )
            with pytest.raises(CoverageDataMismatchError) as ei:
                await merger.capture(gcda_dir, gcno_dir, tmp_path / "out.info")
            mock_exec.assert_not_called()
        msg = str(ei.value)
        assert "prod.gcda" in msg  # names the implicated data file
        assert "prod.gcno" in msg  # names the notes file it fails against
        assert "0x2222bbbb" in msg  # the data stamp, inspectable
        assert "0x1111aaaa" in msg  # the notes stamp, inspectable
        assert "rebuilt" in msg  # the framing still names cause + remedy
        await localhost.close()

    @pytest.mark.asyncio
    async def test_capture_matching_stamps_proceed_to_lcov(self, tmp_path):
        localhost = LocalHost()
        merger = LcovMerger(localhost)
        gcda_dir, gcno_dir = tmp_path / "gcda", tmp_path / "gcno"
        _write_pair(gcda_dir, gcno_dir, "prod", gcno_stamp=0x1111AAAA, gcda_stamp=0x1111AAAA)

        with patch.object(localhost, "exec", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = CommandResult(
                Status.Success, value="", command="lcov --capture ...", retcode=0
            )
            result = await merger.capture(gcda_dir, gcno_dir, tmp_path / "out.info")
            assert result == tmp_path / "out.info"
            mock_exec.assert_called_once()
        await localhost.close()

    @pytest.mark.asyncio
    async def test_capture_any_matching_candidate_notes_file_passes(self, tmp_path):
        """Several same-stem .gcno under the search root (e.g. two build
        variants): the pairing is fine if ANY of them matches, mirroring how
        lcov resolves multiple --build-directory args."""
        localhost = LocalHost()
        merger = LcovMerger(localhost)
        gcda_dir, gcno_root = tmp_path / "gcda", tmp_path / "gcno"
        _write_pair(
            gcda_dir, gcno_root / "stale_variant", "prod", gcno_stamp=0xDEAD, gcda_stamp=0xF00D
        )
        (gcno_root / "live_variant").mkdir(parents=True)
        (gcno_root / "live_variant" / "prod.gcno").write_bytes(
            _gcov_header(b"oncg", b"B33*", 0xF00D)
        )

        with patch.object(localhost, "exec", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = CommandResult(
                Status.Success, value="", command="lcov --capture ...", retcode=0
            )
            result = await merger.capture(gcda_dir, gcno_root, tmp_path / "out.info")
            assert result == tmp_path / "out.info"
        await localhost.close()

    @pytest.mark.asyncio
    async def test_capture_version_word_mismatch_raises(self, tmp_path):
        """Equal stamps but different format-version words (a .gcda written by
        a different compiler generation than the .gcno) is just as fatal."""
        localhost = LocalHost()
        merger = LcovMerger(localhost)
        gcda_dir, gcno_dir = tmp_path / "gcda", tmp_path / "gcno"
        _write_pair(
            gcda_dir,
            gcno_dir,
            "prod",
            gcno_stamp=0x1111AAAA,
            gcda_stamp=0x1111AAAA,
            gcno_version=b"B33*",
            gcda_version=b"408*",
        )

        with patch.object(localhost, "exec", new_callable=AsyncMock) as mock_exec:
            with pytest.raises(CoverageDataMismatchError) as ei:
                await merger.capture(gcda_dir, gcno_dir, tmp_path / "out.info")
            mock_exec.assert_not_called()
        assert "version" in str(ei.value)
        await localhost.close()

    @pytest.mark.asyncio
    async def test_capture_gcda_without_notes_falls_through_to_lcov(self, tmp_path):
        """A .gcda with no same-stem .gcno anywhere is not the guard's call —
        today's lcov behavior (its own warning/error) is preserved."""
        localhost = LocalHost()
        merger = LcovMerger(localhost)
        gcda_dir = tmp_path / "gcda"
        gcda_dir.mkdir()
        (gcda_dir / "orphan.gcda").write_bytes(_gcov_header(b"adcg", b"B33*", 0x1234))
        gcno_dir = tmp_path / "gcno"
        gcno_dir.mkdir()

        with patch.object(localhost, "exec", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = CommandResult(
                Status.Success, value="", command="lcov --capture ...", retcode=0
            )
            result = await merger.capture(gcda_dir, gcno_dir, tmp_path / "out.info")
            assert result == tmp_path / "out.info"
            mock_exec.assert_called_once()
        await localhost.close()

    @pytest.mark.asyncio
    async def test_capture_truncated_header_falls_through_to_lcov(self, tmp_path):
        """Files too short to carry the 12-byte header are left for lcov to
        reject with its own diagnostics."""
        localhost = LocalHost()
        merger = LcovMerger(localhost)
        gcda_dir, gcno_dir = tmp_path / "gcda", tmp_path / "gcno"
        gcda_dir.mkdir()
        gcno_dir.mkdir()
        (gcda_dir / "prod.gcda").write_bytes(b"adcg")  # truncated
        (gcno_dir / "prod.gcno").write_bytes(_gcov_header(b"oncg", b"B33*", 0x1234))

        with patch.object(localhost, "exec", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = CommandResult(
                Status.Success, value="", command="lcov --capture ...", retcode=0
            )
            result = await merger.capture(gcda_dir, gcno_dir, tmp_path / "out.info")
            assert result == tmp_path / "out.info"
            mock_exec.assert_called_once()
        await localhost.close()


def _clang_gcov_file(
    magic: bytes, stamp: int, functions: list[tuple[int, int, int]], *, trailer: bytes = b""
) -> bytes:
    """A minimal clang-dialect gcov file: 12-byte header + function records.

    clang emits the GCC 4.8-era layout — record lengths in 32-bit words —
    and each function record carries (ident, lineno_checksum,
    cfg_checksum), the triplet llvm-cov itself verifies at load.
    """
    blob = _gcov_header(magic, b"*804", stamp)
    for ident, lineno_checksum, cfg_checksum in functions:
        blob += struct.pack("<II", 0x01000000, 3)
        blob += struct.pack("<III", ident, lineno_checksum, cfg_checksum)
    return blob + trailer


class TestLcovMergerFunctionChecksumGuard:
    """Function-level .gcda↔.gcno verification for clang-dialect files.

    clang's file stamp is a structure hash, so an edit that only shifts
    lines keeps it — the header check passes — yet llvm-cov rejects every
    function whose line-number checksum moved, silently (exit 0, all-zero
    hits, nothing in lcov output). The per-function (ident,
    lineno_checksum, cfg_checksum) records in both files are the
    detectable difference.
    """

    @pytest.mark.asyncio
    async def test_capture_clang_function_checksum_mismatch_raises(self, tmp_path):
        """Same file stamp, drifted lineno_checksum (the shifted-lines stale
        deploy): raise before lcov, naming the function-checksum cause."""
        localhost = LocalHost()
        merger = LcovMerger(localhost)
        gcda_dir, gcno_dir = tmp_path / "gcda", tmp_path / "gcno"
        gcda_dir.mkdir()
        gcno_dir.mkdir()
        (gcno_dir / "prod.gcno").write_bytes(
            _clang_gcov_file(b"oncg", 0xD00D, [(0, 0x99FB9F22, 0xAB), (1, 0x3B3C1786, 0xCD)])
        )
        (gcda_dir / "prod.gcda").write_bytes(
            _clang_gcov_file(b"adcg", 0xD00D, [(0, 0xEA5C67C7, 0xAB), (1, 0x3B3C1786, 0xCD)])
        )

        with patch.object(localhost, "exec", new_callable=AsyncMock) as mock_exec:
            with pytest.raises(CoverageDataMismatchError) as ei:
                await merger.capture(gcda_dir, gcno_dir, tmp_path / "out.info")
            mock_exec.assert_not_called()
        msg = str(ei.value)
        assert "function checksum" in msg
        assert "prod.gcda" in msg
        assert "rebuilt" in msg  # framing still names cause + remedy
        await localhost.close()

    @pytest.mark.asyncio
    async def test_capture_clang_matching_function_records_proceed(self, tmp_path):
        """Identical function triplets (plus unrelated records to walk over)
        pair fine and reach lcov."""
        localhost = LocalHost()
        merger = LcovMerger(localhost)
        gcda_dir, gcno_dir = tmp_path / "gcda", tmp_path / "gcno"
        gcda_dir.mkdir()
        gcno_dir.mkdir()
        funcs = [(0, 0x99FB9F22, 0xAB), (1, 0x3B3C1786, 0xCD)]
        (gcno_dir / "prod.gcno").write_bytes(_clang_gcov_file(b"oncg", 0xD00D, funcs))
        counters = struct.pack("<II", 0x01A10000, 2) + struct.pack("<Q", 1)
        (gcda_dir / "prod.gcda").write_bytes(
            _clang_gcov_file(b"adcg", 0xD00D, funcs, trailer=counters)
        )

        with patch.object(localhost, "exec", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = CommandResult(
                Status.Success, value="", command="lcov --capture ...", retcode=0
            )
            result = await merger.capture(gcda_dir, gcno_dir, tmp_path / "out.info")
            assert result == tmp_path / "out.info"
            mock_exec.assert_called_once()
        await localhost.close()

    @pytest.mark.asyncio
    async def test_capture_clang_missing_function_raises(self, tmp_path):
        """A .gcda function absent from the notes entirely (the
        function-count flavor of staleness) is just as undecodable."""
        localhost = LocalHost()
        merger = LcovMerger(localhost)
        gcda_dir, gcno_dir = tmp_path / "gcda", tmp_path / "gcno"
        gcda_dir.mkdir()
        gcno_dir.mkdir()
        (gcno_dir / "prod.gcno").write_bytes(_clang_gcov_file(b"oncg", 0xD00D, [(0, 0x11, 0xAB)]))
        (gcda_dir / "prod.gcda").write_bytes(
            _clang_gcov_file(b"adcg", 0xD00D, [(0, 0x11, 0xAB), (7, 0x22, 0xCD)])
        )

        with patch.object(localhost, "exec", new_callable=AsyncMock) as mock_exec:
            with pytest.raises(CoverageDataMismatchError):
                await merger.capture(gcda_dir, gcno_dir, tmp_path / "out.info")
            mock_exec.assert_not_called()
        await localhost.close()

    @pytest.mark.asyncio
    async def test_capture_gnu_record_stream_skips_function_check(self, tmp_path):
        """GNU gcc-12+ files (16-byte header, byte-unit lengths) do not walk
        as the clang dialect — the deep check must skip them rather than
        false-positive; GCC's per-compile restamp covers them at the header
        level anyway."""
        localhost = LocalHost()
        merger = LcovMerger(localhost)
        gcda_dir, gcno_dir = tmp_path / "gcda", tmp_path / "gcno"
        gcda_dir.mkdir()
        gcno_dir.mkdir()
        # 4th header word (gcc12+ checksum), then records with BYTE lengths —
        # read as the clang dialect this is a bogus tag with a huge length.
        gnu_tail = struct.pack("<I", 0x732C642B) + struct.pack("<II", 0xA1000000, 8)
        (gcno_dir / "prod.gcno").write_bytes(_gcov_header(b"oncg", b"*33B", 0xBEEF) + gnu_tail)
        (gcda_dir / "prod.gcda").write_bytes(_gcov_header(b"adcg", b"*33B", 0xBEEF) + gnu_tail)

        with patch.object(localhost, "exec", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = CommandResult(
                Status.Success, value="", command="lcov --capture ...", retcode=0
            )
            result = await merger.capture(gcda_dir, gcno_dir, tmp_path / "out.info")
            assert result == tmp_path / "out.info"
            mock_exec.assert_called_once()
        await localhost.close()


class TestLcovMergerToolchain:
    """Tests for per-host toolchain support in LcovMerger."""

    @pytest.mark.asyncio
    async def test_capture_uses_toolchain_gcov(self, tmp_path):
        """Verify capture() uses the toolchain's gcov and lcov binaries."""
        localhost = LocalHost()
        merger = LcovMerger(localhost)
        tc = Toolchain(
            sysroot=Path("/opt/arm"),
            gcov=Path("bin/arm-gcov"),
            lcov=Path("bin/arm-lcov"),
        )

        with patch.object(localhost, "exec", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = CommandResult(
                Status.Success, value="", command="lcov ...", retcode=0
            )
            await merger.capture(
                tmp_path / "gcda",
                tmp_path / "gcno",
                tmp_path / "out.info",
                toolchain=tc,
            )
            cmd = mock_exec.call_args[0][0]
            assert "/opt/arm/bin/arm-lcov" in cmd
            assert "/opt/arm/bin/arm-gcov" in cmd
        await localhost.close()

    @pytest.mark.asyncio
    async def test_capture_without_toolchain_uses_defaults(self, tmp_path):
        """Verify capture() falls back to instance defaults without toolchain."""
        localhost = LocalHost()
        merger = LcovMerger(localhost, lcov="my-lcov", gcov="my-gcov")

        with patch.object(localhost, "exec", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = CommandResult(
                Status.Success, value="", command="lcov ...", retcode=0
            )
            await merger.capture(
                tmp_path / "gcda",
                tmp_path / "gcno",
                tmp_path / "out.info",
            )
            cmd = mock_exec.call_args[0][0]
            assert "my-lcov" in cmd
            assert "my-gcov" in cmd
        await localhost.close()

    @pytest.mark.asyncio
    async def test_capture_wraps_llvm_cov_gcov_tool(self, tmp_path):
        """A toolchain whose gcov is an llvm-cov binary cannot be handed to
        ``lcov --gcov-tool`` as-is (lcov takes one word; llvm-cov needs its
        ``gcov`` subcommand). capture() must substitute a generated one-word
        wrapper script."""
        localhost = LocalHost()
        merger = LcovMerger(localhost)
        tc = Toolchain(sysroot=Path("/"), gcov=Path("usr/bin/llvm-cov"))

        with patch.object(localhost, "exec", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = CommandResult(
                Status.Success, value="", command="lcov ...", retcode=0
            )
            await merger.capture(
                tmp_path / "gcda",
                tmp_path / "gcno",
                tmp_path / "out.info",
                toolchain=tc,
            )
            cmd = mock_exec.call_args[0][0]
        assert "--gcov-tool /usr/bin/llvm-cov" not in cmd
        wrapper = tmp_path / "llvm-gcov-wrapper.sh"
        assert f"--gcov-tool {wrapper}" in cmd
        assert 'exec /usr/bin/llvm-cov gcov "$@"' in wrapper.read_text()
        await localhost.close()

    @pytest.mark.asyncio
    async def test_capture_and_merge_per_host_toolchains(self, tmp_path):
        """Verify different toolchains are used per host in capture_and_merge."""
        localhost = LocalHost()
        merger = LcovMerger(localhost)

        host0_dir = tmp_path / "host0"
        host0_dir.mkdir()
        host1_dir = tmp_path / "host1"
        host1_dir.mkdir()
        work_dir = tmp_path / "work"

        tc_arm = Toolchain(sysroot=Path("/opt/arm"), gcov=Path("bin/arm-gcov"))
        tc_x86 = Toolchain(sysroot=Path("/opt/x86"), gcov=Path("bin/x86-gcov"))

        commands: list[str] = []

        async def mock_exec(cmd, timeout=None):
            commands.append(cmd)
            return CommandResult(Status.Success, value="", command=cmd, retcode=0)

        with patch.object(localhost, "exec", side_effect=mock_exec):
            await merger.capture_and_merge(
                [host0_dir, host1_dir],
                tmp_path / "gcno",
                work_dir,
                toolchains=[tc_arm, tc_x86],
            )

        # First capture should use arm toolchain
        assert "/opt/arm/bin/arm-gcov" in commands[0]
        # Second capture should use x86 toolchain
        assert "/opt/x86/bin/x86-gcov" in commands[1]
        # Third command is the merge
        assert len(commands) == 3
        await localhost.close()

    @pytest.mark.asyncio
    async def test_capture_and_merge_per_host_gcno(self, tmp_path):
        """Each host is captured against its own gcno dir when gcno_dirs is given."""
        localhost = LocalHost()
        merger = LcovMerger(localhost)

        host0 = tmp_path / "host0"
        host0.mkdir()
        host1 = tmp_path / "host1"
        host1.mkdir()
        gcno_a = tmp_path / "build_v3_7"
        gcno_a.mkdir()
        gcno_b = tmp_path / "build_v4_4"
        gcno_b.mkdir()
        work_dir = tmp_path / "work"

        commands: list[str] = []

        async def mock_exec(cmd, timeout=None):
            commands.append(cmd)
            return CommandResult(Status.Success, value="", command=cmd, retcode=0)

        with patch.object(localhost, "exec", side_effect=mock_exec):
            await merger.capture_and_merge(
                [host0, host1],
                tmp_path / "fallback_gcno",
                work_dir,
                gcno_dirs=[gcno_a, gcno_b],
            )

        assert str(gcno_a) in commands[0]  # host0 -> its own gcno
        assert str(gcno_b) in commands[1]  # host1 -> its own gcno
        assert str(tmp_path / "fallback_gcno") not in commands[0]
        await localhost.close()

    @pytest.mark.asyncio
    async def test_capture_and_merge_gcno_dirs_length_mismatch(self, tmp_path):
        """A gcno_dirs list that does not match host_gcda_dirs is rejected."""
        localhost = LocalHost()
        merger = LcovMerger(localhost)
        try:
            with pytest.raises(ValueError, match="gcno_dirs length"):
                await merger.capture_and_merge(
                    [tmp_path / "host0"],
                    tmp_path / "gcno",
                    tmp_path / "work",
                    gcno_dirs=[],
                )
        finally:
            await localhost.close()
