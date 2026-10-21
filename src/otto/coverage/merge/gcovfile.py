"""Read just enough of a ``.gcda`` / ``.gcno`` to tell whether the two decode together.

A ``.gcda`` decodes only against the notes of the compilation that produced
its binary. GNU gcov refuses a bad pair loudly; llvm-cov (clang builds) prints
a complaint, exits 0 and records the file at zero hits. So the pair is checked
structurally before lcov runs: the header's format version and build stamp
(bytes 4:12, compared raw so endianness never enters), and for clang notes the
per-function checksums, because clang's stamp is a structure hash that does not
move when lines shift.
"""

import struct
from pathlib import Path

_HEADER_LEN = 12
_TAG_FUNCTION = 0x01000000
_FUNCTION_ANNOUNCE_WORDS = 3  # ident, lineno_checksum, cfg_checksum


def read_header(path: Path) -> bytes | None:
    """Return the 12-byte gcov header (magic, version, stamp), or ``None`` for a shorter file."""
    with path.open("rb") as f:
        header = f.read(_HEADER_LEN)
    return header if len(header) == _HEADER_LEN else None


def clang_function_records(path: Path) -> set[tuple[int, int, int]] | None:
    """Return the (ident, lineno_checksum, cfg_checksum) set of a clang-dialect gcov file.

    clang writes the GCC 4.8-era layout: the 12-byte header, then tag/length
    records with lengths counted in 32-bit words. ``None`` when the file does
    not walk cleanly as that dialect — GNU gcc 12+ files never do (16-byte
    headers, byte lengths), which is deliberate: gcc restamps every compile,
    so its header check is already decisive.
    """
    data = path.read_bytes()
    pos = _HEADER_LEN
    functions: set[tuple[int, int, int]] = set()
    while pos + 8 <= len(data):
        tag, length = struct.unpack_from("<II", data, pos)
        pos += 8
        if tag == 0:
            break
        nbytes = length * 4
        if pos + nbytes > len(data):
            return None
        if tag == _TAG_FUNCTION:
            if length < _FUNCTION_ANNOUNCE_WORDS:
                return None
            functions.add(struct.unpack_from("<III", data, pos))
        pos += nbytes
    return functions


def incompatibility(gcda: Path, gcno: Path) -> str | None:
    """Return why *gcda* cannot be decoded against *gcno*, or ``None`` when it can."""
    data_header = read_header(gcda)
    notes_header = read_header(gcno)
    if data_header is None or notes_header is None:
        return f"{gcda}: header too short to read (data or notes file {gcno})"
    if notes_header[4:8] != data_header[4:8]:
        return (
            f"{gcda}: format version {data_header[4:8]!r} does not match notes file "
            f"{gcno} ({notes_header[4:8]!r}) — written by different compiler generations"
        )
    if notes_header[8:12] != data_header[8:12]:
        data_stamp, notes_stamp = (
            int.from_bytes(h[8:12], "little") for h in (data_header, notes_header)
        )
        return (
            f"{gcda}: stamp mismatch with notes file {gcno} "
            f"(data {data_stamp:#010x} vs notes {notes_stamp:#010x})"
        )
    data_funcs = clang_function_records(gcda)
    if not data_funcs:
        return None
    notes_funcs = clang_function_records(gcno)
    if not notes_funcs:
        return None
    missing = data_funcs - notes_funcs
    if not missing:
        return None
    return (
        f"{gcda}: function checksums do not match notes file {gcno} "
        f"({len(missing)} of {len(data_funcs)} functions differ) — source lines "
        f"changed since the shipped binary was built"
    )
