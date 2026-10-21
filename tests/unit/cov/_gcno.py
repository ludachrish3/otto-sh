"""Synthetic ``.gcno`` writers for the notes-file reader tests."""

import struct


def words_str(s: str) -> bytes:
    """Encode a string the clang / gcc<12 way: a word count, then NUL-padded words."""
    raw = s.encode() + b"\0"
    raw += b"\0" * (-len(raw) % 4)
    return struct.pack("<I", len(raw) // 4) + raw


def bytes_str(s: str) -> bytes:
    """Encode a string the gcc>=12 way: a byte count, then the NUL-terminated bytes."""
    raw = s.encode() + b"\0"
    return struct.pack("<I", len(raw)) + raw


def clang_gcno(func_file: str, line_files: list[str]) -> bytes:
    """Build a clang notes file: one function in *func_file*, lines from *line_files*."""
    fn = struct.pack("<III", 1, 2, 3) + words_str("f") + words_str(func_file) + struct.pack("<I", 2)
    lines = struct.pack("<I", 0)
    for name in line_files:
        lines += struct.pack("<I", 0) + words_str(name) + struct.pack("<I", 7)
    lines += struct.pack("<I", 0) + struct.pack("<I", 0)  # lineno 0 + empty string ends
    return (
        b"oncg"
        b"*804"
        + struct.pack("<I", 0xABCD)
        + struct.pack("<II", 0x01000000, len(fn) // 4)
        + fn
        + struct.pack("<II", 0x01450000, len(lines) // 4)
        + lines
    )


def gcc13_gcno(cwd: str, names: list[str]) -> bytes:
    """Build a gcc 13 notes file: *names*[0] in a function record, the rest in a lines record."""
    fn = (
        struct.pack("<III", 1, 2, 3)
        + bytes_str("main")
        + struct.pack("<I", 0)
        + bytes_str(names[0])
        + struct.pack("<IIII", 1, 1, 2, 1)
    )
    out = (
        b"oncg"
        b"*33B"
        + struct.pack("<II", 0xABCD, 0)
        + bytes_str(cwd)
        + struct.pack("<I", 1)
        + struct.pack("<II", 0x01000000, len(fn))
        + fn
    )
    if names[1:]:
        lines = struct.pack("<I", 0)
        for name in names[1:]:
            lines += struct.pack("<I", 0) + bytes_str(name) + struct.pack("<I", 7)
        lines += struct.pack("<I", 0) + struct.pack("<I", 0)  # lineno 0 + empty string ends
        out += struct.pack("<II", 0x01450000, len(lines)) + lines
    return out
