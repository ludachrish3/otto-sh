"""Tests for the Toolchain dataclass."""

from pathlib import Path

from otto.host.toolchain import Toolchain


class TestToolchainDefaults:
    """Verify default toolchain resolves to system paths."""

    def test_default_sysroot(self):
        tc = Toolchain()
        assert tc.sysroot == Path('/')

    def test_default_gcov_bin(self):
        tc = Toolchain()
        assert tc.gcov_bin == '/usr/bin/gcov'

    def test_default_lcov_bin(self):
        tc = Toolchain()
        assert tc.lcov_bin == '/usr/bin/lcov'


class TestToolchainCustomSysroot:
    """Verify sysroot-relative path resolution."""

    def test_custom_sysroot_gcov(self):
        tc = Toolchain(sysroot=Path('/opt/arm'))
        assert tc.gcov_bin == '/opt/arm/usr/bin/gcov'

    def test_custom_sysroot_lcov(self):
        tc = Toolchain(sysroot=Path('/opt/arm'))
        assert tc.lcov_bin == '/opt/arm/usr/bin/lcov'

    def test_custom_sysroot_with_custom_gcov(self):
        tc = Toolchain(
            sysroot=Path('/opt/arm'),
            gcov=Path('bin/arm-linux-gnueabihf-gcov'),
        )
        assert tc.gcov_bin == '/opt/arm/bin/arm-linux-gnueabihf-gcov'

    def test_custom_sysroot_with_custom_lcov(self):
        tc = Toolchain(
            sysroot=Path('/opt/arm'),
            lcov=Path('bin/lcov'),
        )
        assert tc.lcov_bin == '/opt/arm/bin/lcov'


class TestToolchainCompilerDerivation:
    """Verify compiler property derivation from gcov path."""

    def test_gcc_default(self):
        tc = Toolchain()
        assert tc.compiler == Path('/usr/bin/gcc')

    def test_gcc_cross_compiler(self):
        tc = Toolchain(
            sysroot=Path('/opt/arm'),
            gcov=Path('bin/arm-linux-gnueabihf-gcov'),
        )
        assert tc.compiler == Path('/opt/arm/bin/arm-linux-gnueabihf-gcc')

    def test_clang_llvm_cov(self):
        tc = Toolchain(
            sysroot=Path('/opt/llvm'),
            gcov=Path('bin/llvm-cov'),
        )
        assert tc.compiler == Path('/opt/llvm/bin/clang')

    def test_unknown_gcov_name_returns_none(self):
        tc = Toolchain(
            gcov=Path('bin/some-unknown-tool'),
        )
        assert tc.compiler is None

    def test_gcov_in_usr_bin(self):
        """Default path: usr/bin/gcov → usr/bin/gcc."""
        tc = Toolchain(sysroot=Path('/'))
        assert tc.compiler == Path('/usr/bin/gcc')
