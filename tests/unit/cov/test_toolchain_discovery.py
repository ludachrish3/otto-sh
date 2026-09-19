"""Tests for toolchain auto-discovery from ``.gcno`` files.

Discovery is stamp-based: the 8-byte ``.gcno`` header carries a gcov format
version (GCC writes its own release, e.g. ``B33*`` for 13.3; clang always
writes the GCC 4.8-era ``408*`` unless overridden), and that stamp — not any
embedded compiler path, because .gcno files embed none — tells the coverage
pipeline which gcov tool family can read the build's counters.

Header bytes in these tests are real ones observed from gcc 13.3 and
clang 18 (``oncg`` magic = little-endian file, stamp chars reversed on disk).
"""

import logging
import os
import stat
import subprocess
from pathlib import Path

import pytest

from otto.host.errors import CoverageToolMissingError
from otto.host.toolchain_discovery import (
    discover_toolchain_from_gcda,
    discover_toolchain_from_gcno,
    ensure_gcov_tool,
    gcov_stamp_major,
    read_gcno_version,
    read_gcov_version,
    system_gcov_major,
)

# Real on-disk headers: 4-byte magic + 4-byte version stamp.
GCC13_LE_HEADER = b"oncg*33B" + b"\x28\x88\xf5\x39"  # gcc 13.3, stamp word follows
CLANG18_LE_HEADER = b"oncg*804" + b"\xa2\x1c\x9d\x13"  # clang 18 (4.8 emulation)
GCC13_BE_HEADER = b"gcnoB33*" + b"\x28\x88\xf5\x39"  # big-endian target
CLANG_BE_HEADER = b"gcno408*" + b"\xa2\x1c\x9d\x13"

# .gcda headers share the layout; only the magic differs.
GCC13_LE_GCDA = b"adcg*33B" + b"\x28\x88\xf5\x39"
GCC12_LE_GCDA = b"adcg*42B" + b"\x28\x88\xf5\x39"
GCC9_LE_GCDA = b"adcg*59A" + b"\x28\x88\xf5\x39"
CLANG_LE_GCDA = b"adcg*804" + b"\xa2\x1c\x9d\x13"
GCC13_BE_GCDA = b"gcdaB33*" + b"\x28\x88\xf5\x39"


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


class TestReadGcovVersion:
    """One reader for both file kinds: the stamp sits at the same offset."""

    def test_little_endian_gcda(self, tmp_path):
        path = _write_gcno(tmp_path / "a.gcda", GCC12_LE_GCDA)
        assert read_gcov_version(path) == "B24*"

    def test_big_endian_gcda(self, tmp_path):
        path = _write_gcno(tmp_path / "a.gcda", GCC13_BE_GCDA)
        assert read_gcov_version(path) == "B33*"

    def test_clang_gcda(self, tmp_path):
        path = _write_gcno(tmp_path / "a.gcda", CLANG_LE_GCDA)
        assert read_gcov_version(path) == "408*"

    def test_gcno_still_reads(self, tmp_path):
        path = _write_gcno(tmp_path / "a.gcno", GCC13_LE_HEADER)
        assert read_gcov_version(path) == "B33*"
        assert read_gcno_version(path) == "B33*"

    def test_other_magic_returns_none(self, tmp_path):
        path = _write_gcno(tmp_path / "a.gcda", b"\x7fELF\x02\x01\x01\x00")
        assert read_gcov_version(path) is None


class TestGcovStampMajor:
    """gcc's word is <lead letter><major digit><minor digit>*: 'A' for 5-9,
    'B' for 10-19, each later letter adding ten. gcc 4 and clang write a
    digit lead, which is no gcc 5+ major."""

    @pytest.mark.parametrize(
        ("stamp", "major"),
        [("A95*", 9), ("B05*", 10), ("B24*", 12), ("B33*", 13), ("B42*", 14), ("C01*", 20)],
    )
    def test_letter_lead_decodes(self, stamp, major):
        assert gcov_stamp_major(stamp) == major

    @pytest.mark.parametrize("stamp", ["408*", "402*", "407*", "B*4*", "", "B2", "b24*"])
    def test_anything_else_decodes_to_none(self, stamp):
        assert gcov_stamp_major(stamp) is None


class TestSystemGcovMajor:
    def _run(self, monkeypatch, *, stdout="", returncode=0, raises=None):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            if raises is not None:
                raise raises
            return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        return calls

    def test_reads_the_major_from_the_first_line(self, monkeypatch):
        calls = self._run(
            monkeypatch,
            stdout="gcov (Ubuntu 13.3.0-6ubuntu2~24.04.1) 13.3.0\nCopyright (C) 2023\n",
        )
        assert system_gcov_major() == 13
        assert calls == [["gcov", "--version"]]

    def test_probes_the_tool_it_is_given(self, monkeypatch):
        calls = self._run(monkeypatch, stdout="gcov-9 (Ubuntu 9.5.0-6ubuntu2) 9.5.0\n")
        assert system_gcov_major("/usr/bin/gcov-9") == 9
        assert calls == [["/usr/bin/gcov-9", "--version"]]

    def test_takes_the_last_version_on_the_line(self, monkeypatch):
        """A packager's build string can carry its own version-like token
        before gcov's actual version — crosstool-NG's ``1.25.0.196_227d99d``
        must not be mistaken for the major."""
        self._run(
            monkeypatch,
            stdout="gcov (crosstool-NG 1.25.0.196_227d99d) 12.2.0\n",
        )
        assert system_gcov_major() == 12

    def test_a_missing_tool_is_none(self, monkeypatch):
        self._run(monkeypatch, raises=FileNotFoundError("gcov"))
        assert system_gcov_major() is None

    def test_a_failing_tool_is_none(self, monkeypatch):
        self._run(
            monkeypatch,
            stdout="gcov (Ubuntu 13.3.0-6ubuntu2) 13.3.0\n",
            returncode=1,
        )
        assert system_gcov_major() is None

    def test_empty_stdout_is_none(self, monkeypatch):
        self._run(monkeypatch, stdout="", returncode=0)
        assert system_gcov_major() is None

    def test_no_version_in_the_output_is_none(self, monkeypatch):
        self._run(monkeypatch, stdout="gcov: unknown option\n")
        assert system_gcov_major() is None


def _gcda_dir(root: Path, host: str, product: str, header: bytes) -> Path:
    """A ``<cov>/<host>/<product>`` directory holding one ``.gcda`` with *header*."""
    d = root / "cov" / host / product
    _write_gcno(d / "obj" / "main.gcda", header)
    return d


class TestDiscoverToolchainFromGcda:
    """Per gcda directory: the stamp names the family and the GCC major; the
    tool comes from PATH; a missing tool is a named failure, never a
    fall-through to a gcov geninfo would refuse."""

    def test_an_llvm_stamp_resolves_llvm_cov(self, tmp_path, monkeypatch):
        d = _gcda_dir(tmp_path, "test1", "app", CLANG_LE_GCDA)
        llvm_cov = _fake_llvm_cov(tmp_path / "bin")
        monkeypatch.setenv("PATH", str(tmp_path / "bin"))

        tc = discover_toolchain_from_gcda(d, system_major=13)

        assert tc is not None
        assert tc.gcov_bin == str(llvm_cov)
        assert tc.sysroot == Path("/")

    def test_the_system_major_is_the_default(self, tmp_path, monkeypatch):
        d = _gcda_dir(tmp_path, "test1", "app", GCC13_LE_GCDA)
        _fake_llvm_cov(tmp_path / "bin", "gcov-13")
        monkeypatch.setenv("PATH", str(tmp_path / "bin"))

        assert discover_toolchain_from_gcda(d, system_major=13) is None

    def test_another_major_resolves_its_gcov(self, tmp_path, monkeypatch):
        d = _gcda_dir(tmp_path, "test1", "app", GCC12_LE_GCDA)
        gcov_12 = _fake_llvm_cov(tmp_path / "bin", "gcov-12")
        monkeypatch.setenv("PATH", str(tmp_path / "bin"))

        tc = discover_toolchain_from_gcda(d, system_major=13)

        assert tc is not None
        assert tc.gcov_bin == str(gcov_12)

    def test_an_unprobed_system_major_resolves_every_gcc_by_name(self, tmp_path, monkeypatch):
        d = _gcda_dir(tmp_path, "test1", "app", GCC13_LE_GCDA)
        gcov_13 = _fake_llvm_cov(tmp_path / "bin", "gcov-13")
        monkeypatch.setenv("PATH", str(tmp_path / "bin"))

        tc = discover_toolchain_from_gcda(d, system_major=None)

        assert tc is not None
        assert tc.gcov_bin == str(gcov_13)

    def test_an_undecodable_stamp_is_the_default_with_a_warning(self, tmp_path, caplog):
        d = _gcda_dir(tmp_path, "test1", "app", b"adcg*704" + b"\x00" * 4)  # gcc 4.7

        with caplog.at_level(logging.WARNING):
            assert discover_toolchain_from_gcda(d, system_major=13) is None

        assert any("407*" in r.message and str(d) in r.message for r in caplog.records)

    def test_no_readable_gcda_is_none_without_a_warning(self, tmp_path, caplog):
        d = tmp_path / "cov" / "test1" / "app"
        d.mkdir(parents=True)
        (d / "short.gcda").write_bytes(b"adcg")

        with caplog.at_level(logging.WARNING):
            assert discover_toolchain_from_gcda(d, system_major=13) is None

        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_a_missing_gcov_major_is_a_named_failure(self, tmp_path, monkeypatch):
        d = _gcda_dir(tmp_path, "test2", "otto_kmod_demo", GCC12_LE_GCDA)
        monkeypatch.setenv("PATH", str(tmp_path / "emptybin"))

        with pytest.raises(CoverageToolMissingError) as info:
            discover_toolchain_from_gcda(d, system_major=13)

        message = str(info.value)
        for part in (
            "test2",
            "otto_kmod_demo",
            "'B24*'",
            "gcc 12",
            "gcov-12",
            "apt install gcc-12",
            "toolchain.gcov",
        ):
            assert part in message, part

    def test_a_missing_llvm_cov_is_a_named_failure(self, tmp_path, monkeypatch):
        d = _gcda_dir(tmp_path, "test1", "app", CLANG_LE_GCDA)
        monkeypatch.setenv("PATH", str(tmp_path / "emptybin"))

        with pytest.raises(CoverageToolMissingError) as info:
            discover_toolchain_from_gcda(d, system_major=13)

        message = str(info.value)
        for part in (
            "test1",
            "app",
            "'408*'",
            "clang",
            "llvm-cov",
            "apt install llvm",
            "toolchain.gcov",
        ):
            assert part in message, part

    def test_a_chosen_tool_is_logged_once_at_info(self, tmp_path, monkeypatch, caplog):
        d = _gcda_dir(tmp_path, "test1", "app", GCC12_LE_GCDA)
        gcov_12 = _fake_llvm_cov(tmp_path / "bin", "gcov-12")
        monkeypatch.setenv("PATH", str(tmp_path / "bin"))

        with caplog.at_level(logging.INFO, logger="otto.host.toolchain_discovery"):
            discover_toolchain_from_gcda(d, system_major=13)

        lines = [r.message for r in caplog.records if r.levelno == logging.INFO]
        assert len(lines) == 1
        assert "test1" in lines[0]
        assert "app" in lines[0]
        assert "B24*" in lines[0]
        assert str(gcov_12) in lines[0]

    def test_the_discovered_toolchain_uses_the_host_lcov(self, tmp_path, monkeypatch):
        d = _gcda_dir(tmp_path, "test1", "app", GCC12_LE_GCDA)
        _fake_llvm_cov(tmp_path / "bin", "gcov-12")
        lcov = _fake_llvm_cov(tmp_path / "bin", "lcov")
        monkeypatch.setenv("PATH", str(tmp_path / "bin"))

        tc = discover_toolchain_from_gcda(d, system_major=13)

        assert tc is not None
        assert tc.lcov_bin == str(lcov)
