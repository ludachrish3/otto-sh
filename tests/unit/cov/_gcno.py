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


TAG_FUNCTION = 0x01000000
TAG_ARCS_COUNTER = 0x01A10000


def gcc9_notes(
    cwd: str,
    *,
    version: bytes = b"*11B",
    stamp: int = 0xABCD,
    support: int = 1,
    funcs: list[tuple[int, int, int]] | None = None,
    filename: str = "a.c",
) -> bytes:
    """Build gcc 9-11 layout notes: a cwd string and support word, then one record per function.

    *version* is the on-disk (little-endian, reversed) tag: ``*11B`` is ``B11*``.
    """
    out = b"oncg" + version + struct.pack("<I", stamp) + words_str(cwd) + struct.pack("<I", support)
    for ident, lineno, cfg in [(1, 2, 3)] if funcs is None else funcs:
        fn = (
            struct.pack("<III", ident, lineno, cfg)
            + words_str("f")
            + struct.pack("<I", 0)
            + words_str(filename)
            + struct.pack("<IIII", 1, 1, 2, 1)
        )
        out += struct.pack("<II", TAG_FUNCTION, len(fn) // 4) + fn
    return out


def gcc9_data(
    *, version: bytes = b"*11B", stamp: int = 0xABCD, funcs: list[tuple[int, int, int]]
) -> bytes:
    """Build gcc 9-11 layout data: per function its checksums and one arc counter, then a 0 word."""
    out = b"adcg" + version + struct.pack("<I", stamp)
    for ident, lineno, cfg in funcs:
        out += struct.pack("<IIIII", TAG_FUNCTION, 3, ident, lineno, cfg)
        out += struct.pack("<II", TAG_ARCS_COUNTER, 2) + struct.pack("<Q", 1)
    return out + struct.pack("<I", 0)


STAMP_VERSION = "B24*"
"""The gcov version the stamp-guard fixtures write (gcc 12.4)."""


def stamp_notes(stamp: int, version: str = STAMP_VERSION, *, big_endian: bool = False) -> bytes:
    """A notes file's 12-byte header (magic, version, stamp) in the given byte order."""
    if big_endian:
        return b"gcno" + version.encode() + struct.pack(">I", stamp)
    return b"oncg" + version.encode()[::-1] + struct.pack("<I", stamp)


def stamp_image(
    *stamps: int, version: str = STAMP_VERSION, bits: int = 32, big_endian: bool = False
) -> bytes:
    """An ELF image holding one ``gcov_info`` head (version, next pointer, stamp) per stamp.

    It opens with the ELF identification its class and byte order are read
    from; every word after it is in that byte order.
    """
    ident = b"\x7fELF" + bytes([1 if bits == 32 else 2, 2 if big_endian else 1]) + bytes(58)
    word = version.encode() if big_endian else version.encode()[::-1]
    gap = bytes(4 if bits == 32 else 12)  # the next pointer, and 64-bit alignment padding
    fmt = ">I" if big_endian else "<I"
    return ident + b"".join(b"\xaa" * 8 + word + gap + struct.pack(fmt, s) for s in stamps)
