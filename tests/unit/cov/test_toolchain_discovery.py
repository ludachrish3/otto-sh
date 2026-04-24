"""Tests for toolchain auto-discovery from .gcno files."""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from otto.host.localHost import LocalHost
from otto.host.toolchain import Toolchain
from otto.host.toolchain_discovery import (
    _clang_toolchain,
    _derive_sysroot,
    _gcc_toolchain,
    _toolchain_from_compiler,
    discover_toolchain_from_gcno,
)
from otto.utils import CommandStatus, Status


class TestDeriveSysroot:

    def test_usr_bin(self):
        assert _derive_sysroot(Path('/opt/arm/usr/bin')) == Path('/opt/arm')

    def test_plain_bin(self):
        assert _derive_sysroot(Path('/opt/arm/bin')) == Path('/opt/arm')

    def test_system_usr_bin(self):
        assert _derive_sysroot(Path('/usr/bin')) == Path('/')


class TestGccToolchain:

    def test_simple_gcc(self):
        tc = _gcc_toolchain('gcc', Path('/usr/bin'), Path('/'))
        assert tc.gcov_bin == '/usr/bin/gcov'

    def test_cross_gcc(self):
        tc = _gcc_toolchain(
            'arm-linux-gnueabihf-gcc',
            Path('/opt/arm/bin'),
            Path('/opt/arm'),
        )
        assert tc.gcov_bin == '/opt/arm/bin/arm-linux-gnueabihf-gcov'
        assert tc.sysroot == Path('/opt/arm')

    def test_gpp(self):
        tc = _gcc_toolchain('g++', Path('/usr/bin'), Path('/'))
        assert tc.gcov_bin == '/usr/bin/gcov'


class TestClangToolchain:

    def test_creates_wrapper(self, tmp_path):
        tc = _clang_toolchain(
            Path('/opt/llvm/bin'), Path('/opt/llvm'), tmp_path,
        )
        wrapper = tmp_path / 'llvm-gcov-wrapper.sh'
        assert wrapper.exists()
        content = wrapper.read_text()
        assert 'llvm-cov gcov' in content
        assert 'exec' in content

    def test_wrapper_is_executable(self, tmp_path):
        _clang_toolchain(
            Path('/opt/llvm/bin'), Path('/opt/llvm'), tmp_path,
        )
        wrapper = tmp_path / 'llvm-gcov-wrapper.sh'
        import os
        assert os.access(wrapper, os.X_OK)


class TestToolchainFromCompiler:

    def test_gcc(self, tmp_path):
        tc = _toolchain_from_compiler(
            Path('/opt/arm/bin/arm-linux-gnueabihf-gcc'), tmp_path,
        )
        assert tc is not None
        assert tc.gcov_bin == '/opt/arm/bin/arm-linux-gnueabihf-gcov'

    def test_clang(self, tmp_path):
        tc = _toolchain_from_compiler(
            Path('/opt/llvm/bin/clang'), tmp_path,
        )
        assert tc is not None
        # Should have generated a wrapper
        assert 'llvm-gcov-wrapper' in tc.gcov_bin

    def test_unknown_compiler(self, tmp_path):
        tc = _toolchain_from_compiler(
            Path('/opt/arm/bin/unknown-compiler'), tmp_path,
        )
        assert tc is None


class TestDiscoverToolchainFromGcno:

    @pytest.mark.asyncio
    async def test_discovers_gcc(self, tmp_path):
        localhost = LocalHost()
        gcno_dir = tmp_path / 'build'
        gcno_dir.mkdir()

        find_output = str(gcno_dir / 'main.gcno')
        strings_output = (
            "main.c\n"
            "/opt/arm-toolchain/bin/arm-linux-gnueabihf-gcc\n"
            "some-other-string\n"
        )

        call_count = 0

        async def mock_oneshot(cmd, timeout=None):
            nonlocal call_count
            call_count += 1
            if 'find' in cmd:
                return CommandStatus(
                    command=cmd, output=find_output,
                    status=Status.Success, retcode=0,
                )
            if 'strings' in cmd:
                return CommandStatus(
                    command=cmd, output=strings_output,
                    status=Status.Success, retcode=0,
                )
            return CommandStatus(
                command=cmd, output='', status=Status.Failed, retcode=1,
            )

        with patch.object(localhost, 'oneshot', side_effect=mock_oneshot):
            tc = await discover_toolchain_from_gcno(gcno_dir, localhost)

        assert tc is not None
        assert tc.sysroot == Path('/opt/arm-toolchain')
        assert 'arm-linux-gnueabihf-gcov' in tc.gcov_bin
        await localhost.close()

    @pytest.mark.asyncio
    async def test_discovers_clang(self, tmp_path):
        localhost = LocalHost()
        gcno_dir = tmp_path / 'build'
        gcno_dir.mkdir()
        work_dir = tmp_path / 'work'

        find_output = str(gcno_dir / 'main.gcno')
        strings_output = (
            "main.c\n"
            "/opt/llvm-15/bin/clang\n"
        )

        async def mock_oneshot(cmd, timeout=None):
            if 'find' in cmd:
                return CommandStatus(
                    command=cmd, output=find_output,
                    status=Status.Success, retcode=0,
                )
            if 'strings' in cmd:
                return CommandStatus(
                    command=cmd, output=strings_output,
                    status=Status.Success, retcode=0,
                )
            return CommandStatus(
                command=cmd, output='', status=Status.Failed, retcode=1,
            )

        with patch.object(localhost, 'oneshot', side_effect=mock_oneshot):
            tc = await discover_toolchain_from_gcno(gcno_dir, localhost, work_dir)

        assert tc is not None
        assert 'llvm-gcov-wrapper' in tc.gcov_bin
        await localhost.close()

    @pytest.mark.asyncio
    async def test_no_gcno_files_returns_none(self, tmp_path):
        localhost = LocalHost()

        with patch.object(localhost, 'oneshot', new_callable=AsyncMock) as mock_oneshot:
            mock_oneshot.return_value = CommandStatus(
                command='find ...', output='',
                status=Status.Success, retcode=0,
            )
            tc = await discover_toolchain_from_gcno(tmp_path, localhost)

        assert tc is None
        await localhost.close()

    @pytest.mark.asyncio
    async def test_no_compiler_in_strings_returns_none(self, tmp_path):
        localhost = LocalHost()
        gcno_dir = tmp_path / 'build'
        gcno_dir.mkdir()

        async def mock_oneshot(cmd, timeout=None):
            if 'find' in cmd:
                return CommandStatus(
                    command=cmd, output=str(gcno_dir / 'main.gcno'),
                    status=Status.Success, retcode=0,
                )
            if 'strings' in cmd:
                return CommandStatus(
                    command=cmd, output='main.c\nsome-random-data\n',
                    status=Status.Success, retcode=0,
                )
            return CommandStatus(
                command=cmd, output='', status=Status.Failed, retcode=1,
            )

        with patch.object(localhost, 'oneshot', side_effect=mock_oneshot):
            tc = await discover_toolchain_from_gcno(gcno_dir, localhost)

        assert tc is None
        await localhost.close()
