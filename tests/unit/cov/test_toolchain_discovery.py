"""Tests for toolchain auto-discovery from ``.gcno`` files.

Discovery is stamp-based: the 8-byte ``.gcno`` header carries a gcov format
version (GCC writes its own release, e.g. ``B33*`` for 13.3; clang always
writes the GCC 4.8-era ``408*`` unless overridden), and that stamp — not any
embedded compiler path, because .gcno files embed none — tells the coverage
pipeline which gcov tool family can read the build's counters.

Header bytes in these tests are real ones observed from gcc 13.3 and
clang 18 (``oncg`` magic = little-endian file, stamp chars reversed on disk).
"""

import os
import stat
from pathlib import Path

from otto.host.toolchain_discovery import (
    discover_toolchain_from_gcno,
    ensure_gcov_tool,
    read_gcno_version,
)

# Real on-disk headers: 4-byte magic + 4-byte version stamp.
GCC13_LE_HEADER = b"oncg*33B" + b"\x28\x88\xf5\x39"  # gcc 13.3, stamp word follows
CLANG18_LE_HEADER = b"oncg*804" + b"\xa2\x1c\x9d\x13"  # clang 18 (4.8 emulation)
GCC13_BE_HEADER = b"gcnoB33*" + b"\x28\x88\xf5\x39"  # big-endian target
CLANG_BE_HEADER = b"gcno408*" + b"\xa2\x1c\x9d\x13"


def _write_gcno(path: Path, header: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header)
    return path


def _fake_llvm_cov(bin_dir: Path, name: str = "llvm-cov") -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    exe = bin_dir / name
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return exe


class TestReadGcnoVersion:
    def test_little_endian_gcc(self, tmp_path):
        gcno = _write_gcno(tmp_path / "a.gcno", GCC13_LE_HEADER)
        assert read_gcno_version(gcno) == "B33*"

    def test_little_endian_clang(self, tmp_path):
        gcno = _write_gcno(tmp_path / "a.gcno", CLANG18_LE_HEADER)
        assert read_gcno_version(gcno) == "408*"

    def test_big_endian_gcc(self, tmp_path):
        """A big-endian target's .gcno stores magic and stamp unreversed."""
        gcno = _write_gcno(tmp_path / "a.gcno", GCC13_BE_HEADER)
        assert read_gcno_version(gcno) == "B33*"

    def test_big_endian_clang(self, tmp_path):
        gcno = _write_gcno(tmp_path / "a.gcno", CLANG_BE_HEADER)
        assert read_gcno_version(gcno) == "408*"

    def test_truncated_file_returns_none(self, tmp_path):
        gcno = _write_gcno(tmp_path / "a.gcno", b"oncg")
        assert read_gcno_version(gcno) is None

    def test_not_a_gcno_returns_none(self, tmp_path):
        gcno = _write_gcno(tmp_path / "a.gcno", b"\x7fELF\x02\x01\x01\x00")
        assert read_gcno_version(gcno) is None

    def test_missing_file_returns_none(self, tmp_path):
        assert read_gcno_version(tmp_path / "nope.gcno") is None


class TestDiscoverToolchainFromGcno:
    """Family detection: clang stamps resolve to llvm-cov; GCC stamps mean
    the default gcov already applies (a cross-GCC toolchain cannot be located
    from the .gcno alone and must be configured on the host)."""

    def test_clang_build_resolves_llvm_cov(self, tmp_path, monkeypatch):
        _write_gcno(tmp_path / "build" / "obj" / "a.gcno", CLANG18_LE_HEADER)
        llvm_cov = _fake_llvm_cov(tmp_path / "bin")
        monkeypatch.setenv("PATH", str(tmp_path / "bin"))

        tc = discover_toolchain_from_gcno(tmp_path / "build")

        assert tc is not None
        assert tc.gcov_bin == str(llvm_cov)

    def test_clang_build_resolves_versioned_llvm_cov(self, tmp_path, monkeypatch):
        """Ubuntu/Debian ship only ``llvm-cov-<N>`` unless the meta package
        is installed; the highest version wins."""
        _write_gcno(tmp_path / "build" / "a.gcno", CLANG18_LE_HEADER)
        _fake_llvm_cov(tmp_path / "bin", "llvm-cov-17")
        want = _fake_llvm_cov(tmp_path / "bin", "llvm-cov-18")
        monkeypatch.setenv("PATH", str(tmp_path / "bin"))

        tc = discover_toolchain_from_gcno(tmp_path / "build")

        assert tc is not None
        assert tc.gcov_bin == str(want)

    def test_clang_build_without_llvm_cov_returns_none_with_warning(
        self, tmp_path, monkeypatch, caplog
    ):
        _write_gcno(tmp_path / "build" / "a.gcno", CLANG18_LE_HEADER)
        monkeypatch.setenv("PATH", str(tmp_path / "emptybin"))

        tc = discover_toolchain_from_gcno(tmp_path / "build")

        assert tc is None
        assert any("llvm-cov" in r.message for r in caplog.records)

    def test_gcc_build_returns_none(self, tmp_path, monkeypatch):
        """GCC-family stamp: no override — the merger's default gcov applies."""
        _write_gcno(tmp_path / "build" / "a.gcno", GCC13_LE_HEADER)
        _fake_llvm_cov(tmp_path / "bin")
        monkeypatch.setenv("PATH", str(tmp_path / "bin"))

        assert discover_toolchain_from_gcno(tmp_path / "build") is None

    def test_no_gcno_files_returns_none(self, tmp_path):
        (tmp_path / "build").mkdir()
        assert discover_toolchain_from_gcno(tmp_path / "build") is None

    def test_missing_dir_returns_none(self, tmp_path):
        assert discover_toolchain_from_gcno(tmp_path / "nope") is None

    def test_skips_unreadable_gcno_and_uses_next(self, tmp_path, monkeypatch):
        _write_gcno(tmp_path / "build" / "a_bad.gcno", b"oncg")  # truncated
        _write_gcno(tmp_path / "build" / "b_good.gcno", CLANG18_LE_HEADER)
        llvm_cov = _fake_llvm_cov(tmp_path / "bin")
        monkeypatch.setenv("PATH", str(tmp_path / "bin"))

        tc = discover_toolchain_from_gcno(tmp_path / "build")

        assert tc is not None
        assert tc.gcov_bin == str(llvm_cov)

    def test_discovered_toolchain_uses_host_lcov(self, tmp_path, monkeypatch):
        """lcov is a host-side orchestrator; the discovered clang toolchain
        must resolve the host lcov, not a path under a clang sysroot."""
        _write_gcno(tmp_path / "build" / "a.gcno", CLANG18_LE_HEADER)
        _fake_llvm_cov(tmp_path / "bin")
        fake_lcov = _fake_llvm_cov(tmp_path / "bin", "lcov")
        monkeypatch.setenv("PATH", str(tmp_path / "bin"))

        tc = discover_toolchain_from_gcno(tmp_path / "build")

        assert tc is not None
        assert tc.lcov_bin == str(fake_lcov)


class TestEnsureGcovTool:
    """``lcov --gcov-tool`` accepts exactly one word, but llvm-cov is only
    gcov-compatible via its ``gcov`` subcommand — llvm-cov paths are wrapped
    in a one-word exec script; real gcov binaries pass through untouched."""

    def test_plain_gcov_passes_through(self, tmp_path):
        assert ensure_gcov_tool("/usr/bin/gcov", tmp_path) == "/usr/bin/gcov"

    def test_cross_gcov_passes_through(self, tmp_path):
        gcov = "/opt/zephyr-sdk/arm-zephyr-eabi/bin/arm-zephyr-eabi-gcov"
        assert ensure_gcov_tool(gcov, tmp_path) == gcov

    def test_llvm_cov_gets_wrapped(self, tmp_path):
        wrapped = ensure_gcov_tool("/usr/bin/llvm-cov", tmp_path)

        wrapper = Path(wrapped)
        assert wrapper.parent == tmp_path
        content = wrapper.read_text()
        assert 'exec /usr/bin/llvm-cov gcov "$@"' in content
        assert os.access(wrapper, os.X_OK)

    def test_versioned_llvm_cov_gets_wrapped(self, tmp_path):
        wrapped = ensure_gcov_tool("/usr/lib/llvm-18/bin/llvm-cov-18", tmp_path)
        assert 'exec /usr/lib/llvm-18/bin/llvm-cov-18 gcov "$@"' in Path(wrapped).read_text()

    def test_wrapping_is_idempotent(self, tmp_path):
        first = ensure_gcov_tool("/usr/bin/llvm-cov", tmp_path)
        second = ensure_gcov_tool("/usr/bin/llvm-cov", tmp_path)
        assert first == second

    def test_creates_missing_work_dir(self, tmp_path):
        wrapped = ensure_gcov_tool("/usr/bin/llvm-cov", tmp_path / "deep" / "dir")
        assert Path(wrapped).exists()
