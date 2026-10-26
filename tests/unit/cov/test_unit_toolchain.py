"""A unit tier's capture toolchain on notes stamped ``B11*``: llvm-cov's stamp.

clang 21 writes ``B11*`` by default, and so does gcc 11.1.0. The stamp alone
chooses the reader, so both go to llvm-cov unless a reader is configured; a
configured GNU gcov is the remedy for a gcc 11.1.0 build.
"""

import logging
import shutil
import stat
from pathlib import Path

import pytest

from otto.coverage.errors import CoverageInputError
from otto.coverage.unit_toolchain import ConfiguredTool, ConfiguredTools, UnitToolchains
from tests.unit.cov._gcno import gcc9_notes

DATA = Path(__file__).parent / "data"

# gcc 11.1.0's shape: an absolute cwd, support word 1, checksums that are not the stamp
GCC_SHAPED_B11_NOTES = gcc9_notes("/work", stamp=0x1234, support=1, funcs=[(1, 2, 0x9999)])


def _exe(bin_dir: Path, name: str) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    exe = bin_dir / name
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return exe


def _notes(directory: Path, *names: str) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    out = []
    for i, name in enumerate(names):
        dest = directory / f"{i}_{name}"
        shutil.copyfile(DATA / name, dest)
        out.append(dest)
    return out


def _gcc_shaped_b11(directory: Path) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    notes = directory / "a.gcno"
    notes.write_bytes(GCC_SHAPED_B11_NOTES)
    return [notes]


@pytest.fixture
def path_bin(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    _exe(bin_dir, "lcov")
    monkeypatch.setenv("PATH", str(bin_dir))
    return bin_dir


def _unconfigured(directory: Path, notes: list[Path]):
    return UnitToolchains().resolve(ConfiguredTools(tier="unit"), "unit", directory, notes)


def _configured(reader: Path, directory: Path, notes: list[Path]):
    configured = ConfiguredTools(tier="unit", coverage_tool=ConfiguredTool(reader.name, reader))
    return UnitToolchains().resolve(configured, "unit", directory, notes)


@pytest.mark.parametrize("shape", ["clang21", "gcc-shaped", "header-only"])
def test_b11_notes_choose_llvm_cov_unconfigured(tmp_path, path_bin, caplog, shape):
    llvm_cov = _exe(path_bin, "llvm-cov")
    _exe(path_bin, "gcov-11")
    build = tmp_path / "build"
    if shape == "clang21":
        notes = _notes(build, "clang-21.1.8-same.gcno")
    elif shape == "gcc-shaped":
        notes = _gcc_shaped_b11(build)
    else:
        build.mkdir()
        notes = [build / "a.gcno"]
        notes[0].write_bytes((DATA / "clang-21.1.8-same.gcno").read_bytes()[:14])

    with caplog.at_level(logging.WARNING):
        tc = _unconfigured(build, notes)

    assert tc.gcov_bin == str(llvm_cov)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.parametrize("shape", ["clang21", "gcc-shaped"])
def test_a_configured_llvm_cov_is_accepted_for_b11_notes(tmp_path, path_bin, shape):
    llvm_cov = _exe(path_bin, "llvm-cov")
    build = tmp_path / "build"
    if shape == "clang21":
        notes = _notes(build, "clang-21.1.8-same.gcno")
    else:
        notes = _gcc_shaped_b11(build)

    tc = _configured(llvm_cov, build, notes)

    assert tc.gcov_bin == str(llvm_cov)


@pytest.mark.parametrize("shape", ["clang21", "gcc-shaped"])
def test_a_configured_gnu_gcov_is_accepted_for_b11_notes(tmp_path, path_bin, shape):
    """The remedy for a gcc 11.1.0 build: name its gcov-11."""
    gcov_11 = _exe(path_bin, "gcov-11")
    build = tmp_path / "build"
    if shape == "clang21":
        notes = _notes(build, "clang-21.1.8-same.gcno")
    else:
        notes = _gcc_shaped_b11(build)

    tc = _configured(gcov_11, build, notes)

    assert tc.gcov_bin == str(gcov_11)


@pytest.mark.parametrize(
    "order",
    [
        ("clang-21.1.8-same.gcno", "clang-18.1.3-same.gcno"),
        ("clang-18.1.3-same.gcno", "clang-21.1.8-same.gcno"),
    ],
    ids=["B11-first", "408-first"],
)
def test_408_and_b11_notes_are_one_llvm_cov_directory(tmp_path, path_bin, order):
    llvm_cov = _exe(path_bin, "llvm-cov")
    build = tmp_path / "build"
    notes = _notes(build, *order)

    assert _unconfigured(build, notes).gcov_bin == str(llvm_cov)


@pytest.mark.parametrize(
    "order",
    [
        ("clang-21.1.8-same.gcno", "clang-18.1.3-same.gcno"),
        ("clang-18.1.3-same.gcno", "clang-21.1.8-same.gcno"),
    ],
    ids=["B11-first", "408-first"],
)
def test_a_configured_gnu_gcov_is_refused_when_any_notes_are_408(tmp_path, path_bin, order):
    """GNU gcov reads B11*, never 408*: one 408* file refuses it, whichever file comes first."""
    gcov_11 = _exe(path_bin, "gcov-11")
    build = tmp_path / "build"
    notes = _notes(build, *order)
    (old,) = [n for n in notes if "clang-18" in n.name]

    with pytest.raises(CoverageInputError) as info:
        _configured(gcov_11, build, notes)

    message = str(info.value)
    assert str(old) in message  # the evidence is the 408* file, whatever its position
    assert "'408*'" in message
    assert "'B11*'" not in message


def test_a_directory_of_clang21_and_gcc15_notes_is_refused(tmp_path, path_bin):
    build = tmp_path / "build"
    notes = _notes(build, "clang-21.1.8-same.gcno", "gcc-15.2.0-same.gcno")

    with pytest.raises(CoverageInputError) as info:
        _unconfigured(build, notes)

    assert str(info.value) == (
        f"unit tier unit: {build} holds notes for more than one reader — "
        f"{notes[0]} is llvm-cov's (gcov stamp 'B11*') and {notes[1]} is gcc 15's (gcov stamp "
        "'B52*'). No single reader decodes both; build them into separate directories."
    )
