import shutil
import struct
import subprocess
from pathlib import Path

import pytest

from otto.coverage.merge.gcovfile import GcnoSources, gcno_sources, incompatibility, read_header
from tests.unit.cov._gcno import clang_gcno, gcc13_gcno, words_str

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
    a = _gcov(tmp_path / "a.gcda", MAGIC_GCDA, b"408*", 7, funcs=[(1, 10, 20)])
    n = _gcov(tmp_path / "a.gcno", MAGIC_GCNO, b"408*", 7, funcs=[(1, 11, 20)])
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
