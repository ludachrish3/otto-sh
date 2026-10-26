"""Read just enough of a ``.gcda`` / ``.gcno`` to tell whether the two decode together.

A ``.gcda`` decodes only against the notes of the compilation that produced
its binary. GNU gcov refuses a bad pair loudly; llvm-cov prints a complaint,
exits 0 and records the file at zero hits. So the pair is checked structurally
before lcov runs: for every pair, the header's format version and build stamp
(bytes 4:12, compared raw so endianness never enters); for a pair llvm-cov
reads (:func:`~otto.host.gcovformat.is_llvm_stamp`), the per-function
checksums too, because LLVM's build stamp is a structure hash that does not
move when lines shift. Records are walked by the layout the notes' version
names (:mod:`otto.host.gcovformat`): ``B11*`` is gcc 11.1's layout.
"""

import struct
from dataclasses import dataclass
from pathlib import Path

from ...host.gcovformat import (
    LLVM_402,
    PLACEHOLDER_CWDS,
    Layout,
    is_llvm_stamp,
    layout_for,
    notes_preamble,
    parse_header,
)
from ...host.gcovformat import function_records as walk_function_records

_HEADER_LEN = 12
_RECORD_HEAD_LEN = 8  # tag + length
_WORD = 4
_TAG_FUNCTION = 0x01000000
_TAG_LINES = 0x01450000


def read_header(path: Path) -> bytes | None:
    """Return the 12-byte gcov header (magic, version, stamp), or ``None`` for a shorter file."""
    with path.open("rb") as f:
        header = f.read(_HEADER_LEN)
    return header if len(header) == _HEADER_LEN else None


def _records(data: bytes, layout: Layout | None) -> set[tuple[int, ...]] | None:
    header = parse_header(data)
    if header is None:
        return None
    layout = layout if layout is not None else layout_for(header.version)
    if layout is None:
        return None
    walked = walk_function_records(data, header, layout)
    return None if walked is None else set(walked)


def function_records(path: Path) -> set[tuple[int, ...]] | None:
    """Return the (ident, checksums...) set of a gcov notes or data file.

    The file's version names its layout: notes skip their compile directory
    and support word, data has neither. ``None`` for a file that does not
    walk cleanly in that layout, or whose version no layout covers.
    """
    return _records(path.read_bytes(), None)


def incompatibility(gcda: Path, gcno: Path) -> str | None:
    """Return why *gcda* cannot be decoded against *gcno*, or ``None`` when it can.

    The headers are compared for every pair; the function records only for
    llvm-cov's stamps, where a walk that does not complete makes no claim.
    """
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
    parsed = parse_header(notes_header)
    if parsed is None or not is_llvm_stamp(parsed.version):
        return None  # GNU gcov checks a gcc pair's functions itself, and loudly
    layout = layout_for(parsed.version)
    if layout is None:
        return None
    # the notes choose the layout both files are walked in
    data_funcs = _records(gcda.read_bytes(), layout)
    if not data_funcs:
        return None
    notes_funcs = _records(gcno.read_bytes(), layout)
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
    resolves against; ``None`` when the file records none, or only a
    placeholder.
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


def gcno_sources(path: Path) -> GcnoSources | None:
    """Return every source name *path* records, or ``None`` for a file this reader does not know.

    Function records name the file each function lives in; line records name
    every file a block's lines come from, headers included. A recorded cwd of
    ``"."`` or ``""`` is a placeholder (clang 21 writes ``"."``, older clang
    forced to a gcc tag ``""``) and reads as ``None``, as no cwd at all does.
    """
    try:
        data = path.read_bytes()
    except OSError:
        return None
    header = parse_header(data)
    if header is None or not header.notes or header.big_endian:
        return None
    layout = layout_for(header.version)
    if layout is None or layout is LLVM_402:
        return None
    preamble = notes_preamble(data, header, layout)
    if preamble is None:
        return None
    cwd = None if preamble.cwd in PLACEHOLDER_CWDS else preamble.cwd
    r = _Reader(data, byte_lengths=layout.byte_lengths)
    r.pos = preamble.end
    names: dict[str, None] = {}
    try:
        while r.pos < len(data):
            r.limit = len(data)
            if r.pos + _RECORD_HEAD_LEN > len(data):
                return None  # a trailing partial tag/length
            tag, length = r.u32(), r.u32()
            if tag == 0:
                break
            end = r.pos + (length if layout.byte_lengths else length * _WORD)
            if end > len(data):
                return None
            r.limit = end  # field reads cannot run into the next record
            if tag == _TAG_FUNCTION:
                for _ in range(layout.announce_words):
                    r.u32()
                r.string()  # function name
                if layout.function_artificial:
                    r.u32()
                names.setdefault(r.string(), None)
                for _ in range(layout.function_tail_words):
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
