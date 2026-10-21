"""Tests for the lcov merger."""

import shlex
import struct
import tempfile
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


class TestLcovMergerStampGuard:
    """Structural .gcda↔.gcno stamp verification, run before lcov.

    GNU gcov refuses a stamp mismatch loudly, but llvm-cov (clang builds)
    prints its complaint and exits 0 — lcov then succeeds with the file
    recorded at all-zero hits and nothing in its output to parse. The only
    reliable detection is comparing the file headers directly, before lcov
    is invoked at all.
    """

    @pytest.mark.asyncio
    async def test_capture_dir_detects_stamp_mismatch_before_lcov(self, tmp_path):
        """Mismatched stamps raise CoverageDataMismatchError without running
        lcov — even when lcov WOULD succeed (the silent clang mode)."""
        localhost = LocalHost()
        merger = LcovMerger(localhost)
        gcda_dir = gcno_dir = tmp_path / "build"
        _write_pair(gcda_dir, gcno_dir, "prod", gcno_stamp=0x1111AAAA, gcda_stamp=0x2222BBBB)

        with patch.object(localhost, "exec", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = CommandResult(
                Status.Success, value="", command="lcov --capture ...", retcode=0
            )
            with pytest.raises(CoverageDataMismatchError) as ei:
                await merger.capture_dir(gcda_dir, tmp_path / "out.info")
            mock_exec.assert_not_called()
        msg = str(ei.value)
        assert "prod.gcda" in msg  # names the implicated data file
        assert "prod.gcno" in msg  # names the notes file it fails against
        assert "0x2222bbbb" in msg  # the data stamp, inspectable
        assert "0x1111aaaa" in msg  # the notes stamp, inspectable
        assert "rebuilt" in msg  # the framing still names cause + remedy
        await localhost.close()

    @pytest.mark.asyncio
    async def test_capture_dir_matching_stamps_proceed_to_lcov(self, tmp_path):
        localhost = LocalHost()
        merger = LcovMerger(localhost)
        gcda_dir = gcno_dir = tmp_path / "build"
        _write_pair(gcda_dir, gcno_dir, "prod", gcno_stamp=0x1111AAAA, gcda_stamp=0x1111AAAA)

        with patch.object(localhost, "exec", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = CommandResult(
                Status.Success, value="", command="lcov --capture ...", retcode=0
            )
            result = await merger.capture_dir(gcda_dir, tmp_path / "out.info")
            assert result == tmp_path / "out.info"
            mock_exec.assert_called_once()
        await localhost.close()

    @pytest.mark.asyncio
    async def test_capture_dir_version_word_mismatch_raises(self, tmp_path):
        """Equal stamps but different format-version words (a .gcda written by
        a different compiler generation than the .gcno) is just as fatal."""
        localhost = LocalHost()
        merger = LcovMerger(localhost)
        gcda_dir = gcno_dir = tmp_path / "build"
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
                await merger.capture_dir(gcda_dir, tmp_path / "out.info")
            mock_exec.assert_not_called()
        assert "version" in str(ei.value)
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
    async def test_capture_dir_clang_function_checksum_mismatch_raises(self, tmp_path):
        """Same file stamp, drifted lineno_checksum (the shifted-lines stale
        deploy): raise before lcov, naming the function-checksum cause."""
        localhost = LocalHost()
        merger = LcovMerger(localhost)
        gcda_dir = gcno_dir = tmp_path / "build"
        gcda_dir.mkdir()
        (gcno_dir / "prod.gcno").write_bytes(
            _clang_gcov_file(b"oncg", 0xD00D, [(0, 0x99FB9F22, 0xAB), (1, 0x3B3C1786, 0xCD)])
        )
        (gcda_dir / "prod.gcda").write_bytes(
            _clang_gcov_file(b"adcg", 0xD00D, [(0, 0xEA5C67C7, 0xAB), (1, 0x3B3C1786, 0xCD)])
        )

        with patch.object(localhost, "exec", new_callable=AsyncMock) as mock_exec:
            with pytest.raises(CoverageDataMismatchError) as ei:
                await merger.capture_dir(gcda_dir, tmp_path / "out.info")
            mock_exec.assert_not_called()
        msg = str(ei.value)
        assert "function checksum" in msg
        assert "prod.gcda" in msg
        assert "rebuilt" in msg  # framing still names cause + remedy
        await localhost.close()

    @pytest.mark.asyncio
    async def test_capture_dir_clang_matching_function_records_proceed(self, tmp_path):
        """Identical function triplets (plus unrelated records to walk over)
        pair fine and reach lcov."""
        localhost = LocalHost()
        merger = LcovMerger(localhost)
        gcda_dir = gcno_dir = tmp_path / "build"
        gcda_dir.mkdir()
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
            result = await merger.capture_dir(gcda_dir, tmp_path / "out.info")
            assert result == tmp_path / "out.info"
            mock_exec.assert_called_once()
        await localhost.close()

    @pytest.mark.asyncio
    async def test_capture_dir_clang_missing_function_raises(self, tmp_path):
        """A .gcda function absent from the notes entirely (the
        function-count flavor of staleness) is just as undecodable."""
        localhost = LocalHost()
        merger = LcovMerger(localhost)
        gcda_dir = gcno_dir = tmp_path / "build"
        gcda_dir.mkdir()
        (gcno_dir / "prod.gcno").write_bytes(_clang_gcov_file(b"oncg", 0xD00D, [(0, 0x11, 0xAB)]))
        (gcda_dir / "prod.gcda").write_bytes(
            _clang_gcov_file(b"adcg", 0xD00D, [(0, 0x11, 0xAB), (7, 0x22, 0xCD)])
        )

        with patch.object(localhost, "exec", new_callable=AsyncMock) as mock_exec:
            with pytest.raises(CoverageDataMismatchError):
                await merger.capture_dir(gcda_dir, tmp_path / "out.info")
            mock_exec.assert_not_called()
        await localhost.close()

    @pytest.mark.asyncio
    async def test_capture_dir_gnu_record_stream_skips_function_check(self, tmp_path):
        """GNU gcc-12+ files (16-byte header, byte-unit lengths) do not walk
        as the clang dialect — the deep check must skip them rather than
        false-positive; GCC's per-compile restamp covers them at the header
        level anyway."""
        localhost = LocalHost()
        merger = LcovMerger(localhost)
        gcda_dir = gcno_dir = tmp_path / "build"
        gcda_dir.mkdir()
        # 4th header word (gcc12+ checksum), then records with BYTE lengths —
        # read as the clang dialect this is a bogus tag with a huge length.
        gnu_tail = struct.pack("<I", 0x732C642B) + struct.pack("<II", 0xA1000000, 8)
        (gcno_dir / "prod.gcno").write_bytes(_gcov_header(b"oncg", b"*33B", 0xBEEF) + gnu_tail)
        (gcda_dir / "prod.gcda").write_bytes(_gcov_header(b"adcg", b"*33B", 0xBEEF) + gnu_tail)

        with patch.object(localhost, "exec", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = CommandResult(
                Status.Success, value="", command="lcov --capture ...", retcode=0
            )
            result = await merger.capture_dir(gcda_dir, tmp_path / "out.info")
            assert result == tmp_path / "out.info"
            mock_exec.assert_called_once()
        await localhost.close()


class TestLcovMergerToolchain:
    """Tests for per-host toolchain support in LcovMerger.capture_dir."""

    @pytest.mark.asyncio
    async def test_capture_uses_toolchain_gcov(self, tmp_path):
        """Verify capture_dir() uses the toolchain's gcov and lcov binaries."""
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
            await merger.capture_dir(
                tmp_path,
                tmp_path / "out.info",
                toolchain=tc,
            )
            cmd = mock_exec.call_args[0][0]
            assert "/opt/arm/bin/arm-lcov" in cmd
            assert "/opt/arm/bin/arm-gcov" in cmd
        await localhost.close()

    @pytest.mark.asyncio
    async def test_capture_without_toolchain_uses_defaults(self, tmp_path):
        """Verify capture_dir() falls back to instance defaults without toolchain."""
        localhost = LocalHost()
        merger = LcovMerger(localhost, lcov="my-lcov", gcov="my-gcov")

        with patch.object(localhost, "exec", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = CommandResult(
                Status.Success, value="", command="lcov ...", retcode=0
            )
            await merger.capture_dir(
                tmp_path,
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
        ``gcov`` subcommand). capture_dir() must substitute a generated one-word
        wrapper script."""
        localhost = LocalHost()
        merger = LcovMerger(localhost)
        tc = Toolchain(sysroot=Path("/"), gcov=Path("usr/bin/llvm-cov"))

        with patch.object(localhost, "exec", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = CommandResult(
                Status.Success, value="", command="lcov ...", retcode=0
            )
            await merger.capture_dir(
                tmp_path,
                tmp_path / "out.info",
                toolchain=tc,
            )
            cmd = mock_exec.call_args[0][0]
        assert "--gcov-tool /usr/bin/llvm-cov" not in cmd
        wrapper = Path(_gcov_tool(cmd))
        assert 'exec /usr/bin/llvm-cov gcov "$@"' in wrapper.read_text()
        merger.close()
        await localhost.close()

    @pytest.mark.asyncio
    async def test_the_wrapper_lives_outside_the_output_dir_and_goes_with_the_merger(
        self, tmp_path
    ):
        """The output directory is the user's choice (a space in it would break geninfo's
        shell); the wrapper sits in otto's own temp dir and is removed with the merger."""
        localhost = LocalHost()
        out = tmp_path / "my report" / "_work" / "out.info"
        tc = Toolchain(sysroot=Path("/"), gcov=Path("usr/bin/llvm-cov"))
        with patch.object(localhost, "exec", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = CommandResult(
                Status.Success, value="", command="lcov ...", retcode=0
            )
            with LcovMerger(localhost) as merger:
                await merger.capture_dir(tmp_path, out, toolchain=tc)
                wrapper = Path(_gcov_tool(mock_exec.call_args[0][0]))
                assert wrapper.is_file()
                assert wrapper.parent.parent == Path(tempfile.gettempdir())
                assert tmp_path not in wrapper.parents
        assert not wrapper.parent.exists()
        await localhost.close()


@pytest.mark.asyncio
async def test_a_system_temp_dir_lcov_cannot_run_puts_the_wrapper_under_tmp(tmp_path, monkeypatch):
    """geninfo runs the reader through a shell: a TMPDIR with a space cannot hold it."""
    spaced = tmp_path / "my tmp"
    spaced.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(spaced))
    localhost = LocalHost()
    tc = Toolchain(sysroot=Path("/"), gcov=Path("usr/bin/llvm-cov"))
    with patch.object(localhost, "exec", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = CommandResult(
            Status.Success, value="", command="lcov ...", retcode=0
        )
        with LcovMerger(localhost) as merger:
            await merger.capture_dir(tmp_path, tmp_path / "out.info", toolchain=tc)
            wrapper = Path(_gcov_tool(mock_exec.call_args[0][0]))
            assert wrapper.parent.parent == Path("/tmp")
            assert wrapper.is_file()
    assert not wrapper.parent.exists()
    await localhost.close()


def _gcov_tool(cmd: str) -> str:
    argv = shlex.split(cmd)
    return argv[argv.index("--gcov-tool") + 1]


class TestLcovMergerExtraArguments:
    """``toolchain.lcov_args`` — extra arguments for the capture of this host's data.

    The knob exists because a clang-built kernel module's ``.gcno`` records
    kernel headers by a path relative to the kernel tree, which geninfo cannot
    open from the fetched data directory (issue #385). The strings below are
    pinned whole: a stray space or a flag landing after ``--output-file`` is a
    different command line.
    """

    @staticmethod
    def _paired(tmp_path: Path) -> "tuple[Path, Path]":
        build = tmp_path / "build"
        _write_pair(build, build, "prod", gcno_stamp=0x1111AAAA, gcda_stamp=0x1111AAAA)
        return build, tmp_path / "out.info"

    @staticmethod
    def _expected_capture(lcov: str, directory: Path, gcov: str, extra: str, output: Path) -> str:
        return (
            f"{lcov} --capture --directory {directory} --gcov-tool {gcov}"
            f" --rc branch_coverage=1 --ignore-errors source{extra} --output-file {output}"
        )

    async def _capture_cmd(self, tmp_path, toolchain):
        localhost = LocalHost()
        merger = LcovMerger(localhost, lcov="lcov", gcov="gcov")
        directory, output = self._paired(tmp_path)
        with patch.object(localhost, "exec", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = CommandResult(
                Status.Success, value="", command="lcov ...", retcode=0
            )
            await merger.capture_dir(directory, output, toolchain=toolchain)
            cmd = mock_exec.call_args[0][0]
        await localhost.close()
        return cmd, directory, output

    @pytest.mark.asyncio
    async def test_no_toolchain_leaves_the_command_as_it_was(self, tmp_path):
        cmd, directory, output = await self._capture_cmd(tmp_path, None)
        assert cmd == self._expected_capture("lcov", directory, "gcov", "", output)

    @pytest.mark.asyncio
    async def test_a_toolchain_with_no_extra_arguments_changes_nothing(self, tmp_path):
        tc = Toolchain(sysroot=Path("/"), lcov=Path("usr/bin/lcov"), gcov=Path("usr/bin/gcov"))
        cmd, directory, output = await self._capture_cmd(tmp_path, tc)
        assert cmd == self._expected_capture(
            "/usr/bin/lcov", directory, "/usr/bin/gcov", "", output
        )

    @pytest.mark.asyncio
    async def test_the_arguments_land_before_the_output_file(self, tmp_path):
        tc = Toolchain(
            sysroot=Path("/"),
            lcov=Path("usr/bin/lcov"),
            gcov=Path("usr/bin/gcov"),
            lcov_args=["--ignore-errors", "source"],
        )
        cmd, directory, output = await self._capture_cmd(tmp_path, tc)
        assert cmd == self._expected_capture(
            "/usr/bin/lcov", directory, "/usr/bin/gcov", " --ignore-errors source", output
        )

    @pytest.mark.asyncio
    async def test_an_argument_holding_a_space_is_quoted_for_the_shell(self, tmp_path):
        # The command is a STRING run through a shell, so a value with a space
        # in it must reach lcov as one argument.
        tc = Toolchain(
            sysroot=Path("/"),
            lcov=Path("usr/bin/lcov"),
            gcov=Path("usr/bin/gcov"),
            lcov_args=["--rc", "geninfo_adjust_src_path=/a b/=/c/"],
        )
        cmd, directory, output = await self._capture_cmd(tmp_path, tc)
        assert cmd == self._expected_capture(
            "/usr/bin/lcov",
            directory,
            "/usr/bin/gcov",
            " --rc 'geninfo_adjust_src_path=/a b/=/c/'",
            output,
        )
