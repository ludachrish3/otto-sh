"""Tests for the lcov merger."""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from otto.coverage.correlator.merger import LcovMerger
from otto.host.localHost import LocalHost
from otto.host.toolchain import Toolchain
from otto.utils import CommandStatus, Status



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

        with patch.object(localhost, 'oneshot', new_callable=AsyncMock) as mock_oneshot:
            mock_oneshot.return_value = CommandStatus(
                command="lcov --capture ...",
                output="",
                status=Status.Success,
                retcode=0,
            )
            result = await merger.capture(gcda_dir, gcno_dir, output)
            assert result == output
            mock_oneshot.assert_called_once()
        await localhost.close()

    @pytest.mark.asyncio
    async def test_capture_failure_raises(self, tmp_path):
        localhost = LocalHost()
        merger = LcovMerger(localhost)

        with patch.object(localhost, 'oneshot', new_callable=AsyncMock) as mock_oneshot:
            mock_oneshot.return_value = CommandStatus(
                command="lcov --capture ...",
                output="error: no gcda files found",
                status=Status.Failed,
                retcode=1,
            )
            with pytest.raises(RuntimeError, match="lcov --capture failed"):
                await merger.capture(
                    tmp_path / "gcda", tmp_path / "gcno", tmp_path / "out.info"
                )
        await localhost.close()

    @pytest.mark.asyncio
    async def test_merge_info_files(self, tmp_path):
        localhost = LocalHost()
        merger = LcovMerger(localhost)

        info1 = tmp_path / "a.info"
        info2 = tmp_path / "b.info"
        output = tmp_path / "merged.info"

        with patch.object(localhost, 'oneshot', new_callable=AsyncMock) as mock_oneshot:
            mock_oneshot.return_value = CommandStatus(
                command="lcov ...",
                output="",
                status=Status.Success,
                retcode=0,
            )
            result = await merger.merge_info_files([info1, info2], output)
            assert result == output
        await localhost.close()

    @pytest.mark.asyncio
    async def test_merge_empty_raises(self, tmp_path):
        localhost = LocalHost()
        merger = LcovMerger(localhost)
        with pytest.raises(ValueError, match="No .info files"):
            await merger.merge_info_files([], tmp_path / "out.info")
        await localhost.close()

    @pytest.mark.asyncio
    async def test_capture_and_merge_single_host(self, tmp_path):
        localhost = LocalHost()
        merger = LcovMerger(localhost)

        gcda_dir = tmp_path / "host0"
        gcda_dir.mkdir()
        work_dir = tmp_path / "work"

        with patch.object(localhost, 'oneshot', new_callable=AsyncMock) as mock_oneshot:
            mock_oneshot.return_value = CommandStatus(
                command="lcov ...",
                output="",
                status=Status.Success,
                retcode=0,
            )
            result = await merger.capture_and_merge(
                [gcda_dir], tmp_path / "gcno", work_dir
            )
            # Single host = no merge step, returns the captured info directly
            assert result == work_dir / "host_0.info"
            assert mock_oneshot.call_count == 1
        await localhost.close()


class TestLcovMergerToolchain:
    """Tests for per-host toolchain support in LcovMerger."""

    @pytest.mark.asyncio
    async def test_capture_uses_toolchain_gcov(self, tmp_path):
        """Verify capture() uses the toolchain's gcov and lcov binaries."""
        localhost = LocalHost()
        merger = LcovMerger(localhost)
        tc = Toolchain(
            sysroot=Path('/opt/arm'),
            gcov=Path('bin/arm-gcov'),
            lcov=Path('bin/arm-lcov'),
        )

        with patch.object(localhost, 'oneshot', new_callable=AsyncMock) as mock_oneshot:
            mock_oneshot.return_value = CommandStatus(
                command="lcov ...", output="",
                status=Status.Success, retcode=0,
            )
            await merger.capture(
                tmp_path / "gcda", tmp_path / "gcno",
                tmp_path / "out.info", toolchain=tc,
            )
            cmd = mock_oneshot.call_args[0][0]
            assert '/opt/arm/bin/arm-lcov' in cmd
            assert '/opt/arm/bin/arm-gcov' in cmd
        await localhost.close()

    @pytest.mark.asyncio
    async def test_capture_without_toolchain_uses_defaults(self, tmp_path):
        """Verify capture() falls back to instance defaults without toolchain."""
        localhost = LocalHost()
        merger = LcovMerger(localhost, lcov='my-lcov', gcov='my-gcov')

        with patch.object(localhost, 'oneshot', new_callable=AsyncMock) as mock_oneshot:
            mock_oneshot.return_value = CommandStatus(
                command="lcov ...", output="",
                status=Status.Success, retcode=0,
            )
            await merger.capture(
                tmp_path / "gcda", tmp_path / "gcno",
                tmp_path / "out.info",
            )
            cmd = mock_oneshot.call_args[0][0]
            assert 'my-lcov' in cmd
            assert 'my-gcov' in cmd
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

        tc_arm = Toolchain(sysroot=Path('/opt/arm'), gcov=Path('bin/arm-gcov'))
        tc_x86 = Toolchain(sysroot=Path('/opt/x86'), gcov=Path('bin/x86-gcov'))

        commands: list[str] = []

        async def mock_oneshot(cmd, timeout=None):
            commands.append(cmd)
            return CommandStatus(
                command=cmd, output="",
                status=Status.Success, retcode=0,
            )

        with patch.object(localhost, 'oneshot', side_effect=mock_oneshot):
            await merger.capture_and_merge(
                [host0_dir, host1_dir],
                tmp_path / "gcno",
                work_dir,
                toolchains=[tc_arm, tc_x86],
            )

        # First capture should use arm toolchain
        assert '/opt/arm/bin/arm-gcov' in commands[0]
        # Second capture should use x86 toolchain
        assert '/opt/x86/bin/x86-gcov' in commands[1]
        # Third command is the merge
        assert len(commands) == 3
        await localhost.close()
