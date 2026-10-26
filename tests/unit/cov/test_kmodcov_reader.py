"""otto_kmodcov's seed of each object's accumulator from the ``.gcda`` on disk.

The library's own C (``src/otto/kmodcov``: ``kmodcov.c`` and one backend) is
built into a user-space program per compiler by
``tests/_fixtures/kmodcov_reader/build.py`` and driven op by op: ``load`` and
``unload`` are ``kmodcov_register()``/``kmodcov_unregister()``, ``dump`` and
``reset`` the sysfs stores, ``run:<n>`` the instrumented unit's loop, whose
counted line runs n times. What a file holds is read back with the
compiler's own gcov, so every count asserted here is also proof that the
file the library wrote is one gcov accepts.

Three formats, one per backend arm: gcc before 12 (a 12-byte header, lengths
in words), gcc 12 and later (a 16-byte header, lengths in bytes) and clang
(a 12-byte header, lengths in words). clang runs twice: with its own stamp
(``408*`` up to clang 20, ``B11*`` from clang 21) and forced to ``B11*``, so a
clang older than 21 still proves the runtime on clang 21's stamp; both are
read with llvm-cov, the stamp's reader. The compilers are the dev VM's; a
missing one FAILS naming the package that provides it.
"""

import shutil
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests._fixtures.kmodcov_reader.build import (
    HERE,
    Toolchain,
    build,
    clang_toolchain,
    gcc_toolchain,
)

GCOV_TAG_FUNCTION = 0x01000000
PRE12_GCCS = ("gcc-11", "gcc-10", "gcc-9")


def _toolchain(which: str) -> Toolchain:
    if which == "gcc-pre12":
        cc = next((c for c in PRE12_GCCS if shutil.which(c)), None)
        if cc is None:
            pytest.fail(f"no gcc before 12 on PATH (looked for {PRE12_GCCS}); apt install gcc-11")
        return gcc_toolchain(cc)
    if which == "gcc-12+":
        if not shutil.which("gcc"):
            pytest.fail("no gcc on PATH; apt install gcc")
        tc = gcc_toolchain("gcc")
        assert tc.major >= 12, f"gcc is {tc.major}: this case needs gcc 12 or later"
        return tc
    if not shutil.which("clang"):
        pytest.fail("no clang on PATH; apt install clang llvm")
    tc = clang_toolchain(coverage_version="B11*" if which == "clang-B11" else None)
    if not shutil.which(tc.gcov[0]):
        pytest.fail(f"no {tc.gcov[0]} on PATH; apt install llvm")
    return tc


@dataclass
class Harness:
    tc: Toolchain
    program: Path

    @property
    def obj(self) -> Path:
        return self.program.parent


@pytest.fixture(scope="module", params=["gcc-pre12", "gcc-12+", "clang", "clang-B11"])
def harness(request, tmp_path_factory) -> Harness:
    tc = _toolchain(request.param)
    return Harness(tc, build(tc, tmp_path_factory.mktemp(f"kmodcov-{request.param}")))


@dataclass
class Session:
    """One harness process: what each op returned, and the library's log."""

    results: dict[str, list[int]]
    log: list[str]

    def warnings(self) -> list[str]:
        return [line for line in self.log if line.startswith("kmodcov[warn]:")]


class Bed:
    """One test's cov_dir, with the harness that writes under it."""

    def __init__(self, h: Harness, root: Path):
        self.h = h
        self.root = root
        self.cov = root / "cov"

    @property
    def gcda(self) -> Path:
        """Where the library writes unit.c's counters: cov_dir + the object's absolute path."""
        return Path(str(self.cov) + str(self.h.obj / "unit.gcda"))

    def run(self, *ops: str) -> Session:
        done = subprocess.run(
            [str(self.h.program), str(self.cov), *ops],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert done.returncode == 0, done.stderr
        results: dict[str, list[int]] = {}
        for line in done.stdout.splitlines():
            op, value = line.rsplit(" ", 1)
            results.setdefault(op, []).append(int(value))
        return Session(results, done.stderr.splitlines())

    def hits(self) -> int:
        """The counted line's hits in the file on disk, as the compiler's gcov reads it."""
        work = self.root / f"gcov-{len(list(self.root.glob('gcov-*')))}"
        work.mkdir()
        shutil.copy(self.h.obj / "unit.gcno", work / "unit.gcno")
        shutil.copy(self.gcda, work / "unit.gcda")
        forced = self.h.tc.coverage_version
        if forced is not None:
            # the forced stamp reached the file, or this case would silently re-test the default
            assert self.gcda.read_bytes()[4:8] == forced.encode()[::-1]
        done = subprocess.run(
            [*self.h.tc.gcov, "-o", str(work), str(HERE / "unit.c")],
            cwd=work,
            capture_output=True,
            text=True,
            check=False,
        )
        assert done.returncode == 0, done.stdout + done.stderr
        for line in (work / "unit.c.gcov").read_text().splitlines():
            if "KC_HIT" in line:
                count = line.split(":", 1)[0].strip()
                return 0 if count == "#####" else int(count.rstrip("*"))
        raise AssertionError("unit.c.gcov has no KC_HIT line")

    def records(self) -> list[tuple[int, int, int]]:
        """(offset, tag, payload bytes) of every record in the file, by the compiler's rules."""
        data = self.gcda.read_bytes()
        pos, out = self.h.tc.header_bytes, []
        while pos + 8 <= len(data):
            tag, length = struct.unpack_from("<II", data, pos)
            size = length * 4 // self.h.tc.unit_bytes
            out.append((pos, tag, size))
            pos += 8 + size
        return out

    def patch_word(self, offset: int, value: int) -> None:
        data = bytearray(self.gcda.read_bytes())
        struct.pack_into("<I", data, offset, value)
        self.gcda.write_bytes(bytes(data))

    def earlier_load(self, n: int) -> None:
        """A load that ran the loop *n* times and wrote its file at unload."""
        self.run("load", f"run:{n}", "unload")
        assert self.hits() == n


@pytest.fixture
def bed(harness: Harness, tmp_path: Path) -> Bed:
    return Bed(harness, tmp_path)


def test_counts_accumulate_across_loads(bed: Bed):
    """The #670 repro: two loads of ten each read twenty, not the last load's ten."""
    session = bed.run("load", "run:10", "unload", "load", "run:10", "unload")
    assert session.results["load"] == [0, 0]
    assert session.warnings() == []
    assert bed.hits() == 20


def test_counts_accumulate_across_separate_sessions(bed: Bed):
    bed.earlier_load(10)
    bed.run("load", "run:7", "dump", "run:3", "unload")
    assert bed.hits() == 20


def test_with_no_file_the_object_is_seeded_and_never_imports_its_own_dump(bed: Bed):
    """No file at the first dump: seeded. The second dump must not read the first one back."""
    assert not bed.gcda.exists()
    session = bed.run("load", "run:10", "dump", "dump", "run:5", "dump", "unload")
    assert session.results["dump"] == [2, 2, 2]
    assert session.warnings() == []
    assert bed.hits() == 15


def test_an_imported_object_does_not_import_again_in_the_same_load(bed: Bed):
    bed.earlier_load(10)
    bed.run("load", "run:1", "dump", "dump", "run:1", "dump", "unload")
    assert bed.hits() == 12


def test_reset_moves_a_pending_object_to_skipped(bed: Bed):
    """Reset before the first dump: the earlier load's file is never imported."""
    bed.earlier_load(10)
    session = bed.run("load", "reset", "run:5", "unload")
    assert session.results["reset"] == [2]
    assert bed.hits() == 5


def test_reset_after_a_seed_zeroes_the_accumulator(bed: Bed):
    bed.earlier_load(10)
    bed.run("load", "run:4", "dump", "reset", "run:3", "unload")
    assert bed.hits() == 3


def _assert_one_mismatch_warning(bed: Bed, session: Session, reason: str) -> None:
    warnings = session.warnings()
    assert len(warnings) == 1, session.log
    assert str(bed.gcda) in warnings[0], warnings[0]
    assert reason in warnings[0], warnings[0]


def test_a_version_mismatch_is_skipped_and_overwritten(bed: Bed):
    bed.earlier_load(10)
    bed.patch_word(4, 0x41393921)  # some other compiler's version word
    session = bed.run("load", "run:5", "unload")
    _assert_one_mismatch_warning(bed, session, "version")
    assert bed.hits() == 5


def test_a_stamp_mismatch_is_skipped_and_overwritten(bed: Bed):
    bed.earlier_load(10)
    data = bed.gcda.read_bytes()
    bed.patch_word(8, struct.unpack_from("<I", data, 8)[0] ^ 0x5A5A5A5A)
    session = bed.run("load", "run:5", "unload")
    _assert_one_mismatch_warning(bed, session, "stamp")
    assert bed.hits() == 5


def test_a_function_checksum_mismatch_leaves_the_accumulator_untouched(bed: Bed):
    """The LAST function differs: validate-then-mutate adds nothing, not even the first."""
    bed.earlier_load(10)
    functions = [r for r in bed.records() if r[1] == GCOV_TAG_FUNCTION]
    assert len(functions) == 2, bed.records()
    offset, _tag, _size = functions[-1]
    cfg = offset + 8 + 8  # past tag and length, past ident and the line checksum
    bed.patch_word(cfg, struct.unpack_from("<I", bed.gcda.read_bytes(), cfg)[0] ^ 1)
    session = bed.run("load", "run:5", "unload")
    _assert_one_mismatch_warning(bed, session, "checksum")
    assert bed.hits() == 5


def test_a_truncated_file_is_skipped_and_overwritten(bed: Bed):
    bed.earlier_load(10)
    keep = bed.h.tc.header_bytes + 4  # half of the first record's tag and length
    bed.gcda.write_bytes(bed.gcda.read_bytes()[:keep])
    session = bed.run("load", "run:5", "unload")
    _assert_one_mismatch_warning(bed, session, "truncated")
    assert bed.hits() == 5


def test_a_file_with_no_functions_is_truncated(bed: Bed):
    bed.earlier_load(10)
    bed.gcda.write_bytes(bed.gcda.read_bytes()[: bed.h.tc.header_bytes])
    session = bed.run("load", "run:5", "unload")
    _assert_one_mismatch_warning(bed, session, "truncated")
    assert bed.hits() == 5


def test_a_file_longer_than_the_object_is_skipped_before_it_is_decoded(bed: Bed):
    """Read stops one byte past the object's own image: a longer file is never decoded."""
    bed.earlier_load(10)
    bed.gcda.write_bytes(bed.gcda.read_bytes() + b"\0" * 64)
    session = bed.run("load", "run:5", "unload")
    _assert_one_mismatch_warning(bed, session, "longer than this object's")
    assert bed.hits() == 5


def test_a_counter_length_overrunning_the_file_is_skipped(bed: Bed):
    bed.earlier_load(10)
    counters = [r for r in bed.records() if r[1] != GCOV_TAG_FUNCTION]
    offset = counters[0][0]
    bed.patch_word(offset + 4, 0x10000 * bed.h.tc.unit_bytes)
    session = bed.run("load", "run:5", "unload")
    _assert_one_mismatch_warning(bed, session, "overruns")
    assert bed.hits() == 5


def test_a_read_failure_keeps_the_file_and_the_unload_retry_imports_it(bed: Bed):
    bed.earlier_load(10)
    before = bed.gcda.read_bytes()
    session = bed.run("load", "run:5", "fail-read:5", "dump")
    assert session.results["dump"] == [-5], session.log  # -EIO: nothing was written
    assert bed.gcda.read_bytes() == before, "a sysfs dump overwrote a file it could not read"
    assert len(session.warnings()) == 1, session.log
    assert str(bed.gcda) in session.warnings()[0]

    session = bed.run("load", "run:5", "fail-read:5", "dump", "dump", "fail-read:0", "unload")
    assert session.results["dump"] == [-5, -5]
    assert len(session.warnings()) == 1, "one warning per load, not one per dump"
    assert bed.hits() == 15


def test_a_read_failure_that_persists_to_unload_writes_this_loads_counts(bed: Bed):
    bed.earlier_load(10)
    session = bed.run("load", "run:5", "fail-read:5", "dump", "unload")
    warnings = session.warnings()
    assert len(warnings) == 2, session.log
    assert "lost" in warnings[1], warnings[1]
    assert str(bed.gcda) in warnings[1], warnings[1]
    assert bed.hits() == 5
