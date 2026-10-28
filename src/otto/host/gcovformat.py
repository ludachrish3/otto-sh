"""Read a gcov notes or data file's header: its record layout and the stamps llvm-cov reads.

Every ``.gcno`` / ``.gcda`` opens with a magic (``gcno`` / ``gcda``, stored
reversed on a little-endian target), a 4-character format version (the gcov
*stamp*) and a 32-bit build stamp. Two things follow from the version alone:

* **The layout** — where the records start and how lengths are counted
  (:func:`layout_for`).
* **The reader** — :func:`is_llvm_stamp` names the stamps ``llvm-cov`` reads:
  ``402*`` and ``408*``, which LLVM writes up to clang 20, and ``B11*``, gcc
  11.1.0's stamp, which LLVM writes from clang 21. Every other stamp is read
  by the GNU gcov of the GCC major it names; one that names no major
  (``407*``) falls to the system gcov with a warning. Nothing past the
  12-byte header takes part in that choice.

Standard library only, so every package that reads gcov files can import it.
"""

import struct
from dataclasses import dataclass
from pathlib import Path

LLVM_COV_STAMPS = frozenset({"402*", "408*", "B11*"})
"""The gcov stamps ``llvm-cov`` reads: LLVM's ``402*``/``408*`` up to clang 20, and ``B11*``
from clang 21. gcc 11.1.0 writes ``B11*`` too; a configured GNU gcov reads its data."""

PLACEHOLDER_CWDS = frozenset({"", "."})
"""The compile directories that record none (clang 21 writes ``"."``, and older clang forced
to a gcc stamp writes ``""``)."""

_VERSION_LEN = 4
_MAGIC_LEN = 4
_HEADER_LEN = 12  # magic, version, stamp
_WORD = 4
_RECORD_HEAD = 8  # tag + length
_TAG_FUNCTION = 0x01000000
_NOTES_MAGICS = {b"oncg": False, b"gcno": True}  # magic -> big-endian
_DATA_MAGICS = {b"adcg": False, b"gcda": True}
_NEGATIVE = 0x80000000  # a data length's sign bit
_GCC_FIRST_CWD = 9
_GCC_BYTE_LENGTHS = 12
_GCC_FIRST_SUPPORTED = 8


@dataclass(frozen=True)
class Layout:
    """How one generation of the gcov format lays out its records."""

    name: str
    header_len: int
    """Bytes before the first record (data) or the notes preamble."""
    byte_lengths: bool
    """Record and string lengths count bytes, not 32-bit words."""
    notes_cwd: bool
    """Notes record the compile directory after the header."""
    notes_support_word: bool
    """Notes carry the has-unexecuted-blocks word after the header (and the cwd)."""
    announce_words: int
    """The checksum words leading a function record, ident plus one or two checksums."""
    function_artificial: bool
    """A notes function record carries an artificial-function word after its name."""
    function_tail_words: int
    """Location words after a notes function record's filename."""
    negative_counter_lengths: bool
    """Whether a data record's length may be negative, as gcc 11+ writes all-zero counters
    (their negated count, and no payload); gcc 9 and 10 never set the sign bit."""


LLVM_402 = Layout("llvm 4.2", 12, False, False, False, 2, False, 1, False)
LLVM_408 = Layout("llvm 4.8", 12, False, False, False, 3, False, 1, False)
GCC_8 = Layout("gcc 8", 12, False, False, True, 3, True, 3, False)
GCC_9 = Layout("gcc 9-11", 12, False, True, True, 3, True, 4, True)
GCC_12 = Layout("gcc 12+", 16, True, True, True, 3, True, 4, True)


def gcov_stamp_major(stamp: str) -> int | None:
    """Decode the GCC major a gcov version word names, or ``None``.

    gcc writes the word as ``<lead><major digit><minor digit>*``: the lead is
    ``'A'`` for majors 5-9 and ``'B'`` for 10-19 (``'A95*'`` is 9.5, ``'B24*'``
    12.4), each later letter adding ten. gcc 4 wrote a digit lead (``'407*'``),
    the form LLVM's ``408*`` emulates; that, and anything else that is not a
    capital letter followed by a digit, decodes to ``None``.
    """
    if len(stamp) != _VERSION_LEN or not ("A" <= stamp[0] <= "Z") or not stamp[1].isdigit():
        return None
    return (ord(stamp[0]) - ord("A")) * 10 + int(stamp[1])


def is_llvm_stamp(stamp: str) -> bool:
    """Return whether gcov *stamp* is one ``llvm-cov`` reads (:data:`LLVM_COV_STAMPS`)."""
    return stamp in LLVM_COV_STAMPS


def layout_for(version: str) -> Layout | None:
    """Return the record layout of *version*, or ``None`` for one no reader here supports."""
    if version == "402*":
        return LLVM_402
    if version == "408*":
        return LLVM_408
    major = gcov_stamp_major(version)
    if major is None or major < _GCC_FIRST_SUPPORTED:
        return None
    if major < _GCC_FIRST_CWD:
        return GCC_8
    return GCC_9 if major < _GCC_BYTE_LENGTHS else GCC_12


@dataclass(frozen=True)
class GcovHeader:
    """A gcov file's magic, version and stamp, decoded in the byte order its magic shows."""

    notes: bool
    """A notes (``.gcno``) file; otherwise a data (``.gcda``) file."""
    big_endian: bool
    version: str
    stamp: int | None
    """``None`` when the file ends before it."""

    @property
    def order(self) -> str:
        """The :mod:`struct` byte-order prefix the file's words use."""
        return ">" if self.big_endian else "<"


def parse_header(data: bytes) -> GcovHeader | None:
    """Return the header leading *data*, or ``None`` when it is no gcov file."""
    if len(data) < _MAGIC_LEN + _VERSION_LEN:
        return None
    magic = data[:_MAGIC_LEN]
    if magic in _NOTES_MAGICS:
        notes, big_endian = True, _NOTES_MAGICS[magic]
    elif magic in _DATA_MAGICS:
        notes, big_endian = False, _DATA_MAGICS[magic]
    else:
        return None
    raw = data[_MAGIC_LEN : _MAGIC_LEN + _VERSION_LEN]
    version = (raw if big_endian else raw[::-1]).decode(errors="replace")
    stamp = None
    if len(data) >= _HEADER_LEN:
        (stamp,) = struct.unpack_from(">I" if big_endian else "<I", data, 8)
    return GcovHeader(notes=notes, big_endian=big_endian, version=version, stamp=stamp)


INFO_STAMP_OFFSETS = {32: 8, 64: 16}
"""Where the build stamp sits in a ``gcov_info``, by pointer width: past the 4-byte version
word and the ``next`` pointer, which a 64-bit target aligns to 8 bytes."""


def version_word(version: str, *, big_endian: bool) -> bytes:
    """Return the 4 bytes a target of the given byte order stores for gcov *version*.

    The word is a 32-bit integer whose bytes spell *version* most significant
    first, so a big-endian target stores the characters in order and a
    little-endian one reversed.
    """
    raw = version.encode()
    return raw if big_endian else raw[::-1]


def info_stamps(image: bytes, version: str, *, pointer_bits: int, big_endian: bool) -> list[int]:
    """Return every build stamp of the ``gcov_info`` structs baked into *image*.

    A ``gcov_info`` opens with the version word for *version*; the stamp follows
    ``INFO_STAMP_OFFSETS[pointer_bits]`` bytes in. Both are words of the target
    the image was built for, so *pointer_bits* and *big_endian* come from the
    image's own ELF header: the notes file's byte order is the compiler's choice
    and need not be the target's.
    """
    offset = INFO_STAMP_OFFSETS[pointer_bits]
    word = version_word(version, big_endian=big_endian)
    fmt = ">I" if big_endian else "<I"
    stamps = []
    at = image.find(word)
    while at != -1 and at + offset + _WORD <= len(image):
        stamps.append(struct.unpack_from(fmt, image, at + offset)[0])
        at = image.find(word, at + 1)
    return stamps


def read_gcov_version(path: Path) -> str | None:
    """Return a ``.gcno``/``.gcda`` file's 4-char version (``'B33*'``, ``'408*'``).

    Both file kinds carry it at bytes 4:8 behind their magic, in either byte
    order. ``None`` for anything unreadable or not a gcov file.
    """
    try:
        with path.open("rb") as f:
            head = f.read(_MAGIC_LEN + _VERSION_LEN)
    except OSError:
        return None
    header = parse_header(head)
    return None if header is None else header.version


@dataclass(frozen=True)
class NotesPreamble:
    """What a notes file records between its header and its first record."""

    cwd: str | None
    """The compile directory as written; ``None`` for a layout without one."""
    support: int | None
    """The has-unexecuted-blocks word; ``None`` for a layout without one."""
    end: int
    """The offset of the first record."""


def notes_preamble(data: bytes, header: GcovHeader, layout: Layout) -> NotesPreamble | None:
    """Return the cwd and support word of notes *data*, or ``None`` when it is cut short."""
    pos = layout.header_len
    cwd = support = None
    try:
        if layout.notes_cwd:
            (n,) = struct.unpack_from(f"{header.order}I", data, pos)
            pos += _WORD
            size = n if layout.byte_lengths else n * _WORD
            if pos + size > len(data):
                return None
            cwd = data[pos : pos + size].split(b"\0", 1)[0].decode("utf-8", "surrogateescape")
            pos += size
        if layout.notes_support_word:
            (support,) = struct.unpack_from(f"{header.order}I", data, pos)
            pos += _WORD
    except struct.error:
        return None
    return NotesPreamble(cwd=cwd, support=support, end=pos)


def function_records(
    data: bytes, header: GcovHeader, layout: Layout
) -> list[tuple[int, ...]] | None:
    """Return each function record's leading words (ident, checksums) in file order.

    *layout* says where the records start (notes skip their cwd and support
    word, data has neither) and how lengths count. A gcc function record of
    length 0 stands for a function another unit owns and is skipped, as is a
    counter record whose length is negative (gcc 11+: its counters are all
    zero and not written). ``None`` when the file does not walk cleanly: a
    record runs past the end, a function record is shorter than its
    checksums, or bytes trail the last record.
    """
    if header.notes:
        preamble = notes_preamble(data, header, layout)
        if preamble is None:
            return None
        pos = preamble.end
    else:
        pos = layout.header_len
    if pos > len(data):
        return None
    order = header.order
    out: list[tuple[int, ...]] = []
    while pos < len(data):
        if pos + _WORD > len(data):
            return None
        (tag,) = struct.unpack_from(f"{order}I", data, pos)
        if tag == 0:
            break  # end of file
        if pos + _RECORD_HEAD > len(data):
            return None
        (length,) = struct.unpack_from(f"{order}I", data, pos + _WORD)
        pos += _RECORD_HEAD
        if layout.negative_counter_lengths and not header.notes and length & _NEGATIVE:
            nbytes = 0
        else:
            nbytes = length if layout.byte_lengths else length * _WORD
        if pos + nbytes > len(data):
            return None
        if tag == _TAG_FUNCTION and nbytes:
            if nbytes < layout.announce_words * _WORD:
                return None
            out.append(struct.unpack_from(f"{order}{layout.announce_words}I", data, pos))
        pos += nbytes
    return out
