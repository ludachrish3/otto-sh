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
from dataclasses import dataclass
from pathlib import Path

from ...host.toolchain_discovery import gcov_stamp_major

_HEADER_LEN = 12
_GCC12_HEADER_LEN = 16  # gcc 12 added a checksum word after the stamp
_MIN_GCC_MAJOR = 8
_RECORD_HEAD_LEN = 8  # tag + length
_WORD = 4
_TAG_FUNCTION = 0x01000000
_TAG_LINES = 0x01450000
_FUNCTION_ANNOUNCE_WORDS = 3  # ident, lineno_checksum, cfg_checksum
_CLANG_VERSION = b"*804"  # LLVM's 4.8 layout, what clang writes by default
_GCC_FIRST_CWD = 9  # gcc 9 added the compile directory and the end column
_GCC_BYTE_LENGTHS = 12  # gcc 12 switched record and string lengths to bytes


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


@dataclass(frozen=True)
class GcnoSources:
    """The source names one notes file records, as written (absolute or relative).

    ``cwd`` is gcc's recorded compile directory — the base a relative gcc name
    resolves against; clang records none.
    """

    cwd: str | None
    names: list[str]


class _Reader:
    """Walk bytes with bounds checks: reads stay below ``limit`` or raise ``ValueError``."""

    def __init__(self, data: bytes, *, byte_lengths: bool) -> None:
        self.data = data
        self.pos = 0
        self.limit = len(data)
        self.byte_lengths = byte_lengths

    def u32(self) -> int:
        if self.pos + _WORD > self.limit:
            raise ValueError("truncated")
        (value,) = struct.unpack_from("<I", self.data, self.pos)
        self.pos += _WORD
        return value

    def string(self) -> str:
        n = self.u32()
        size = n if self.byte_lengths else n * _WORD
        if self.pos + size > self.limit:
            raise ValueError("truncated string")
        raw = self.data[self.pos : self.pos + size]
        self.pos += size
        return raw.split(b"\0", 1)[0].decode("utf-8", "surrogateescape")


def _function_tail_words(*, clang: bool, major: int | None) -> int:
    """Count the words after a function's filename: clang 1 (line); gcc 8 three; gcc 9+ four."""
    if clang:
        return 1
    return 4 if major is not None and major >= _GCC_FIRST_CWD else 3


def gcno_sources(path: Path) -> GcnoSources | None:
    """Return every source name *path* records, or ``None`` for a file this reader does not know.

    Function records name the file each function lives in; line records name
    every file a block's lines come from, headers included.
    """
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) < _HEADER_LEN or data[:4] != b"oncg":
        return None
    version = data[4:8]
    clang = version == _CLANG_VERSION
    major = None if clang else gcov_stamp_major(version[::-1].decode("ascii", "replace"))
    if not clang and (major is None or major < _MIN_GCC_MAJOR):
        return None
    byte_lengths = not clang and major is not None and major >= _GCC_BYTE_LENGTHS
    r = _Reader(data, byte_lengths=byte_lengths)
    names: dict[str, None] = {}
    try:
        r.pos = _GCC12_HEADER_LEN if byte_lengths else _HEADER_LEN
        cwd = None
        if not clang:
            if major is not None and major >= _GCC_FIRST_CWD:
                cwd = r.string()
            r.u32()  # has_unexecuted_blocks
        while r.pos < len(data):
            r.limit = len(data)
            if r.pos + _RECORD_HEAD_LEN > len(data):
                return None  # a trailing partial tag/length
            tag, length = r.u32(), r.u32()
            if tag == 0:
                break
            end = r.pos + (length if byte_lengths else length * _WORD)
            if end > len(data):
                return None
            r.limit = end  # field reads cannot run into the next record
            if tag == _TAG_FUNCTION:
                for _ in range(_FUNCTION_ANNOUNCE_WORDS):
                    r.u32()
                r.string()  # function name
                if not clang:
                    r.u32()  # artificial
                names.setdefault(r.string(), None)
                for _ in range(_function_tail_words(clang=clang, major=major)):
                    r.u32()  # start line (+ columns, end line on gcc) — mandatory
            elif tag == _TAG_LINES:
                r.u32()  # block
                terminated = False
                while r.pos < end:
                    if r.u32() == 0:
                        name = r.string()
                        if not name:
                            terminated = True
                            break
                        names.setdefault(name, None)
                if not terminated:
                    return None  # a lines record must end with an empty name
            r.pos = end
    except ValueError:
        return None
    return GcnoSources(cwd=cwd, names=[n for n in names if n])
