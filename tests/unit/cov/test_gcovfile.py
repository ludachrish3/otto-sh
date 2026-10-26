import shutil
import struct
import subprocess
from pathlib import Path

import pytest

from otto.coverage.merge import gcovfile
from otto.coverage.merge.gcovfile import GcnoSources, gcno_sources, incompatibility, read_header
from tests.unit.cov._gcno import (
    TAG_ARCS_COUNTER,
    TAG_FUNCTION,
    clang_gcno,
    gcc9_data,
    gcc9_notes,
    gcc13_gcno,
    words_str,
)

DATA = Path(__file__).parent / "data"

MAGIC_GCDA = b"adcg"
MAGIC_GCNO = b"oncg"


def _gcov(path: Path, magic: bytes, version: bytes, stamp: int, funcs=()) -> Path:
    body = b""
    for ident, lineno, cfg in funcs:
        body += struct.pack("<IIIII", 0x01000000, 3, ident, lineno, cfg)
    path.write_bytes(magic + version + struct.pack("<I", stamp) + body)
    return path


def test_read_header_short_file_is_none(tmp_path):
    p = tmp_path / "x.gcda"
    p.write_bytes(b"abc")
    assert read_header(p) is None


def test_matching_stamp_is_compatible(tmp_path):
    a = _gcov(tmp_path / "a.gcda", MAGIC_GCDA, b"B33*", 7)
    n = _gcov(tmp_path / "a.gcno", MAGIC_GCNO, b"B33*", 7)
    assert incompatibility(a, n) is None


def test_stamp_mismatch_is_named(tmp_path):
    a = _gcov(tmp_path / "a.gcda", MAGIC_GCDA, b"B33*", 7)
    n = _gcov(tmp_path / "a.gcno", MAGIC_GCNO, b"B33*", 8)
    assert "stamp mismatch" in incompatibility(a, n)


def test_version_mismatch_is_named(tmp_path):
    a = _gcov(tmp_path / "a.gcda", MAGIC_GCDA, b"B33*", 7)
    n = _gcov(tmp_path / "a.gcno", MAGIC_GCNO, b"A93*", 7)
    assert "format version" in incompatibility(a, n)


def test_clang_function_checksum_mismatch_is_named(tmp_path):
    a = _gcov(tmp_path / "a.gcda", MAGIC_GCDA, b"*804", 7, funcs=[(1, 10, 20)])
    n = _gcov(tmp_path / "a.gcno", MAGIC_GCNO, b"*804", 7, funcs=[(1, 11, 20)])
    assert "function checksums" in incompatibility(a, n)


def test_short_data_header_is_named(tmp_path):
    a = tmp_path / "a.gcda"
    a.write_bytes(b"adcgB33*")
    n = _gcov(tmp_path / "a.gcno", MAGIC_GCNO, b"B33*", 7)
    assert "header too short" in incompatibility(a, n)


def test_synthetic_clang_names(tmp_path):
    p = tmp_path / "a.gcno"
    p.write_bytes(clang_gcno("a.c", ["./inc/h.h", "a.c"]))
    assert gcno_sources(p) == GcnoSources(cwd=None, names=["a.c", "./inc/h.h"])


def test_synthetic_gcc13_names_and_cwd(tmp_path):
    p = tmp_path / "a.gcno"
    p.write_bytes(gcc13_gcno("/work", ["/work/src/a.c"]))
    assert gcno_sources(p) == GcnoSources(cwd="/work", names=["/work/src/a.c"])


def test_truncated_file_is_none(tmp_path):
    p = tmp_path / "a.gcno"
    p.write_bytes(gcc13_gcno("/work", ["/work/src/a.c"])[:-9])
    assert gcno_sources(p) is None


def test_function_fields_overrunning_their_record_is_none(tmp_path):
    # a function record whose declared length is shorter than its strings
    good = gcc13_gcno("/work", ["/work/src/a.c"])
    tag_at = 16 + 4 + len("/work") + 1 + 4  # header + cwd string + has_unexecuted
    bad = bytearray(good)
    struct.pack_into("<I", bad, tag_at + 4, 8)  # length 8 bytes: ident..cfg only partly
    p = tmp_path / "a.gcno"
    p.write_bytes(bytes(bad))
    assert gcno_sources(p) is None


def test_trailing_partial_record_header_is_none(tmp_path):
    p = tmp_path / "a.gcno"
    p.write_bytes(gcc13_gcno("/work", ["/work/src/a.c"]) + b"\x00\x00\x00\x01\x05")
    assert gcno_sources(p) is None


def test_clang_function_missing_its_line_word_is_none(tmp_path):
    fn = struct.pack("<III", 1, 2, 3) + words_str("f") + words_str("a.c")  # no line word
    data = (
        b"oncg*804" + struct.pack("<I", 0xABCD) + struct.pack("<II", 0x01000000, len(fn) // 4) + fn
    )
    p = tmp_path / "a.gcno"
    p.write_bytes(data)
    assert gcno_sources(p) is None


def test_lines_record_without_terminator_is_none(tmp_path):
    lines = struct.pack("<I", 0)  # block only
    data = (
        b"oncg"
        b"*804"
        + struct.pack("<I", 0xABCD)
        + struct.pack("<II", 0x01450000, len(lines) // 4)
        + lines
    )
    p = tmp_path / "a.gcno"
    p.write_bytes(data)
    assert gcno_sources(p) is None


def test_gcc8_layout_has_no_cwd(tmp_path):
    fn = (
        struct.pack("<III", 1, 2, 3)
        + words_str("f")
        + struct.pack("<I", 0)
        + words_str("a.c")
        + struct.pack("<III", 1, 1, 2)
    )
    data = (
        b"oncg"
        b"*08A"
        + struct.pack("<I", 0xABCD)
        + struct.pack("<I", 1)
        + struct.pack("<II", 0x01000000, len(fn) // 4)
        + fn
    )
    p = tmp_path / "a.gcno"
    p.write_bytes(data)
    assert gcno_sources(p) == GcnoSources(cwd=None, names=["a.c"])


def test_not_a_gcno_is_none(tmp_path):
    p = tmp_path / "a.gcno"
    p.write_bytes(b"not a notes file")
    assert gcno_sources(p) is None


def _compile(tmp_path, cc: str) -> GcnoSources | None:
    (tmp_path / "inc").mkdir()
    (tmp_path / "inc" / "h.h").write_text("static inline int h(int x){return x+1;}\n")
    (tmp_path / "a.c").write_text('#include "inc/h.h"\nint f(int a){\n  return h(a)*2;\n}\n')
    subprocess.run([cc, "--coverage", "-c", "a.c", "-o", "a.o"], cwd=tmp_path, check=True)
    return gcno_sources(tmp_path / "a.gcno")


@pytest.mark.skipif(shutil.which("gcc-11") is None, reason="gcc-11 not installed")
def test_real_gcc11_gcno_names(tmp_path):
    got = _compile(tmp_path, "gcc-11")
    assert got is not None
    assert got.cwd == str(tmp_path)
    assert "a.c" in got.names
    assert any(n.endswith("inc/h.h") for n in got.names)


@pytest.mark.skipif(shutil.which("gcc") is None, reason="gcc not installed")
def test_real_gcc_gcno_names(tmp_path):
    got = _compile(tmp_path, "gcc")
    assert got is not None
    assert got.cwd == str(tmp_path)
    assert "a.c" in got.names
    assert any(n.endswith("inc/h.h") for n in got.names)


@pytest.mark.skipif(shutil.which("clang") is None, reason="clang not installed")
def test_real_clang_gcno_names(tmp_path):
    got = _compile(tmp_path, "clang")
    assert got is not None
    assert got.cwd is None
    assert "a.c" in got.names
    assert any(n.endswith("inc/h.h") for n in got.names)


@pytest.mark.parametrize(
    ("version", "body"),
    [
        (b"*37A", struct.pack("<III", 1, 0, 0)),  # has_unexecuted_blocks, end tag
        (b"*704", struct.pack("<II", 0, 0)),
        (b"*204", struct.pack("<II", 0, 0)),
    ],
    ids=["gcc7", "llvm47", "llvm42"],
)
def test_unsupported_version_is_none(tmp_path, version, body):
    # Each body walks cleanly in its own dialect: only the version gate rejects it.
    p = tmp_path / "a.gcno"
    p.write_bytes(b"oncg" + version + struct.pack("<I", 0xABCD) + body)
    assert gcno_sources(p) is None


def test_gcc9_layout_has_cwd_and_word_lengths(tmp_path):
    fn = (
        struct.pack("<III", 1, 2, 3)
        + words_str("f")
        + struct.pack("<I", 0)
        + words_str("src/a.c")
        + struct.pack("<IIII", 1, 1, 2, 1)
    )
    lines = (
        struct.pack("<I", 0)
        + struct.pack("<I", 0)
        + words_str("inc/h.h")
        + struct.pack("<I", 7)
        + struct.pack("<II", 0, 0)
    )
    data = (
        b"oncg"
        b"*59A"
        + struct.pack("<I", 0xABCD)
        + words_str("/work")
        + struct.pack("<I", 1)
        + struct.pack("<II", 0x01000000, len(fn) // 4)
        + fn
        + struct.pack("<II", 0x01450000, len(lines) // 4)
        + lines
    )
    p = tmp_path / "a.gcno"
    p.write_bytes(data)
    assert gcno_sources(p) == GcnoSources(cwd="/work", names=["src/a.c", "inc/h.h"])


@pytest.mark.skipif(shutil.which("g++") is None, reason="g++ not installed")
def test_real_gxx_gcno_names_template_and_inline_header(tmp_path):
    (tmp_path / "inc").mkdir()
    (tmp_path / "inc" / "t.hpp").write_text(
        "template <typename T> T twice(T x) { return x + x; }\n"
        "inline int inc(int x) { return x + 1; }\n"
    )
    (tmp_path / "b.cpp").write_text(
        '#include "inc/t.hpp"\nint f(int a) { return twice(a) + (int)twice(1.5) + inc(a); }\n'
    )
    subprocess.run(["g++", "--coverage", "-c", "b.cpp", "-o", "b.o"], cwd=tmp_path, check=True)
    got = gcno_sources(tmp_path / "b.gcno")
    assert got is not None
    assert got.cwd == str(tmp_path)
    assert "b.cpp" in got.names
    assert any(n.endswith("inc/t.hpp") for n in got.names)


@pytest.mark.parametrize(
    ("name", "names"),
    [
        ("clang-21.1.8-same.gcno", ["a.c", "./inc/h.h"]),
        ("clang-21.1.8-nested.gcno", ["../src/a.c", "../src/inc/h.h"]),
        ("clang-21.1.8-abs.gcno", ["/tmp/clang-abs/a.c", "/tmp/clang-abs/inc/h.h"]),
        ("clang-18.1.3-same-forced-B11.gcno", ["a.c", "./inc/h.h"]),
        ("clang-18.1.3-same.gcno", ["a.c", "./inc/h.h"]),
    ],
)
def test_real_clang_notes_record_no_compile_directory(name, names):
    # clang 21 writes "." where gcc writes its cwd, clang 18 forced to B11* writes ""
    assert gcno_sources(DATA / name) == GcnoSources(cwd=None, names=names)


@pytest.mark.parametrize(
    ("name", "got"),
    [
        ("gcc-15.2.0-same.gcno", GcnoSources(cwd="/tmp/gcc-same", names=["a.c", "inc/h.h"])),
        (
            "gcc-15.2.0-nested.gcno",
            GcnoSources(cwd="/tmp/gcc-nested/build", names=["../src/a.c", "../src/inc/h.h"]),
        ),
        (
            "gcc-15.2.0-abs.gcno",
            GcnoSources(cwd="/", names=["/tmp/gcc-abs/a.c", "/tmp/gcc-abs/inc/h.h"]),
        ),
    ],
)
def test_real_gcc15_notes_keep_their_absolute_cwd(name, got):
    assert gcno_sources(DATA / name) == got


@pytest.mark.parametrize("cwd", ["", "."])
def test_a_placeholder_cwd_reads_as_none_whoever_wrote_it(tmp_path, cwd):
    p = tmp_path / "a.gcno"
    p.write_bytes(gcc9_notes(cwd, version=b"*59A", support=1, funcs=[(1, 2, 0x9999)]))
    assert gcno_sources(p) == GcnoSources(cwd=None, names=["a.c"])


def test_a_relative_gcc_cwd_is_kept(tmp_path):
    p = tmp_path / "a.gcno"
    p.write_bytes(gcc9_notes("src/core", version=b"*59A"))
    assert gcno_sources(p) == GcnoSources(cwd="src/core", names=["a.c"])


def test_real_clang21_notes_and_data_carry_the_same_functions():
    notes = gcovfile.function_records(DATA / "clang-21.1.8-run.gcno")
    data = gcovfile.function_records(DATA / "clang-21.1.8-run.gcda")
    assert notes == data == {(0, 0xF9BECDE3, 0xD03AECD7), (1, 0x62011EBB, 0xD03AECD7)}


def _flip_first_lineno_checksum(gcda: Path) -> None:
    raw = bytearray(gcda.read_bytes())
    # header (12) + function tag and length (8) + ident (4): the lineno_checksum's low byte
    raw[24] ^= 0xFF
    gcda.write_bytes(bytes(raw))


def test_real_clang21_pair_is_compatible(tmp_path):
    gcda = tmp_path / "a.gcda"
    gcno = tmp_path / "a.gcno"
    shutil.copyfile(DATA / "clang-21.1.8-run.gcda", gcda)
    shutil.copyfile(DATA / "clang-21.1.8-run.gcno", gcno)
    assert incompatibility(gcda, gcno) is None


def test_real_clang21_stale_pair_is_named(tmp_path):
    gcda = tmp_path / "a.gcda"
    gcno = tmp_path / "a.gcno"
    shutil.copyfile(DATA / "clang-21.1.8-run.gcda", gcda)
    shutil.copyfile(DATA / "clang-21.1.8-run.gcno", gcno)
    _flip_first_lineno_checksum(gcda)
    assert "function checksums do not match" in (incompatibility(gcda, gcno) or "")


@pytest.mark.parametrize("version", [b"*59A", b"*51B"], ids=["gcc9", "gcc11.5"])
def test_gcc_pairs_are_checked_by_header_never_by_function(tmp_path, version):
    """GNU gcov checks a gcc pair's functions itself; only llvm-cov's stamps are compared here."""
    funcs = [(1, 0x10, 0x20), (2, 0x30, 0x40)]
    gcno = tmp_path / "a.gcno"
    gcno.write_bytes(gcc9_notes("/work", version=version, stamp=7, funcs=funcs))
    gcda = tmp_path / "a.gcda"
    gcda.write_bytes(gcc9_data(version=version, stamp=7, funcs=[(1, 0x11, 0x20), funcs[1]]))
    assert incompatibility(gcda, gcno) is None
    gcda.write_bytes(gcc9_data(version=version, stamp=8, funcs=funcs))
    assert "stamp mismatch" in (incompatibility(gcda, gcno) or "")


def test_a_gcc_pairs_files_are_never_read_past_their_headers(tmp_path, monkeypatch):
    """The function comparison is gated on the header already read: a gcc pair reads no body."""
    funcs = [(1, 0x10, 0x20)]
    gcno = tmp_path / "a.gcno"
    gcno.write_bytes(gcc9_notes("/work", version=b"*51B", stamp=7, funcs=funcs))
    gcda = tmp_path / "a.gcda"
    gcda.write_bytes(gcc9_data(version=b"*51B", stamp=7, funcs=funcs))
    whole_reads: list[Path] = []
    real = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda self: whole_reads.append(self) or real(self))
    assert incompatibility(gcda, gcno) is None
    assert whole_reads == []


GCC11_1_FUNCS = [(1, 0x10, 0x20), (2, 0x30, 0x40)]


def _gcc11_1_data(stamp: int, funcs: list[tuple[int, int, int]]) -> bytes:
    """gcc 11.1.0-shaped B11* data: the first function's counters all zero (a negative length,
    no payload), then a zero-length function record (a function another unit owns)."""
    out = b"adcg*11B" + struct.pack("<I", stamp)
    for i, (ident, lineno, cfg) in enumerate(funcs):
        out += struct.pack("<IIIII", TAG_FUNCTION, 3, ident, lineno, cfg)
        if i == 0:
            out += struct.pack("<Ii", TAG_ARCS_COUNTER, -2)
            out += struct.pack("<II", TAG_FUNCTION, 0)
        else:
            out += struct.pack("<II", TAG_ARCS_COUNTER, 2) + struct.pack("<Q", 1)
    return out + struct.pack("<I", 0)


def _gcc11_1_pair(tmp_path: Path, data_funcs: list[tuple[int, int, int]]) -> tuple[Path, Path]:
    """A B11* pair as gcc 11.1.0 writes it: notes with an absolute cwd and support word 1."""
    gcno = tmp_path / "a.gcno"
    gcno.write_bytes(gcc9_notes("/work", stamp=7, support=1, funcs=GCC11_1_FUNCS))
    gcda = tmp_path / "a.gcda"
    gcda.write_bytes(_gcc11_1_data(7, data_funcs))
    return gcda, gcno


def test_a_gcc11_1_shaped_b11_pair_is_compatible(tmp_path):
    gcda, gcno = _gcc11_1_pair(tmp_path, GCC11_1_FUNCS)
    assert incompatibility(gcda, gcno) is None


def test_a_gcc11_1_shaped_b11_pair_names_a_changed_function(tmp_path):
    # the changed function sits past the zeroed counters and the zero-length record
    gcda, gcno = _gcc11_1_pair(tmp_path, [GCC11_1_FUNCS[0], (2, 0x31, 0x40)])
    assert "1 of 2 functions differ" in (incompatibility(gcda, gcno) or "")


@pytest.mark.parametrize(
    "cut", [14, 44, 60], ids=["first-record", "absent-record", "last-function"]
)
def test_a_gcc11_1_shaped_b11_pair_cut_short_makes_no_claim(tmp_path, cut):
    gcda, gcno = _gcc11_1_pair(tmp_path, GCC11_1_FUNCS)
    gcda.write_bytes(gcda.read_bytes()[:cut])
    assert incompatibility(gcda, gcno) is None


def test_a_real_gcc11_data_file_restamped_b11_is_compared_by_function(tmp_path):
    # a real gcc 11.4 .gcda whose first function's counters are all zero (length -2 words)
    raw = bytearray((DATA / "gcc-11.4-zero-counters.gcda").read_bytes())
    raw[4:8] = b"*11B"
    (stamp,) = struct.unpack_from("<I", raw, 8)
    funcs = [(0x0EA7D626, 0xD581357B, 0xDB5DE9E8), (0x090A34EC, 0xF57D68DC, 0xDB5DE9E8)]
    gcda = tmp_path / "a.gcda"
    gcda.write_bytes(bytes(raw))
    gcno = tmp_path / "a.gcno"
    gcno.write_bytes(gcc9_notes("/work", stamp=stamp, support=1, funcs=funcs))
    assert incompatibility(gcda, gcno) is None
    changed = [funcs[0], (funcs[1][0], funcs[1][1] ^ 1, funcs[1][2])]
    gcno.write_bytes(gcc9_notes("/work", stamp=stamp, support=1, funcs=changed))
    assert "1 of 2 functions differ" in (incompatibility(gcda, gcno) or "")


def test_gcc_b11_notes_walk_by_layout(tmp_path):
    p = tmp_path / "a.gcno"
    p.write_bytes(gcc9_notes("/work", stamp=7, support=1, funcs=[(3, 4, 5)]))
    assert gcovfile.function_records(p) == {(3, 4, 5)}
