import struct
from pathlib import Path

from otto.coverage.merge.gcovfile import incompatibility, read_header

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
