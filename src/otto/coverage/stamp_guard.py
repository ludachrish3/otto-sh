"""The ``.gcno`` stamp guard: a built binary must carry the stamp of every notes file.

Every GCC compilation writes one random 32-bit stamp into the object's ``.gcno``
and into the ``gcov_info`` struct linked into the binary. Counters decode only
against the notes of the compilation that produced them, so a binary shipped
after its sources were recompiled reports "stamp mismatch with notes file" at
report time. :func:`verify_stamps` makes that a build-time failure instead.

It is the one check behind ``otto cov check-stamp``, for any GCC-built ELF
binary, and behind ``otto cov embedded-gcov finish``, which runs it on the
extension it has just stripped. It reads GCC's gcov format (the notes header
and the ``gcov_info`` layout) through :mod:`otto.host.gcovformat`, and the
binary's pointer width and byte order from its ELF header.
"""

from pathlib import Path

from ..errors import OttoError
from ..host.gcovformat import info_stamps, parse_header
from .objects import elf_ident


class StampError(OttoError):
    """The stamp check failed; the message names the file at fault."""


def _read(path: Path, what: str) -> bytes:
    """Return *path*'s bytes, or raise :class:`StampError` naming it as *what*."""
    try:
        return path.read_bytes()
    except OSError as exc:
        raise StampError(f"{path}: cannot read the {what} ({exc})") from exc


def verify_stamps(binary: Path, gcno_dir: Path, *, shown_as: Path | None = None) -> list[int]:
    """Check every ``.gcno`` under *gcno_dir* shares its stamp with a ``gcov_info`` in *binary*.

    The binary that runs and the notes that decode its counters must be one
    compilation, or the report fails late with "stamp mismatch with notes
    file". The version word and stamp are read in *binary*'s own byte order
    and pointer width, which the notes file's byte order need not match.
    Returns the verified stamps, one per notes file in path order.

    Messages name *binary* as *shown_as* when given: ``finish`` checks a
    temporary file and names the destination it is about to become.

    Raises :class:`StampError` naming the file at fault: an unreadable or
    non-ELF binary, a *gcno_dir* that is not a directory or holds no ``.gcno``,
    an unreadable or non-notes ``.gcno``, a binary with no ``gcov_info`` of the
    notes' version, or one whose ``gcov_info`` stamps omit a notes file's.
    """
    image = _read(binary, "binary")
    if shown_as is not None:
        binary = shown_as
    ident = elf_ident(image)
    if ident is None:
        raise StampError(f"{binary}: not an ELF image")
    if not gcno_dir.is_dir():
        raise StampError(f"{gcno_dir}: not a directory")
    try:
        notes = sorted(gcno_dir.rglob("*.gcno"))
    except OSError as exc:
        raise StampError(f"{gcno_dir}: cannot list the .gcno files ({exc})") from exc
    if not notes:
        raise StampError(f"{gcno_dir}: no .gcno file under it")
    verified = []
    for path in notes:
        header = parse_header(_read(path, "notes file"))
        if header is None or not header.notes or header.stamp is None:
            raise StampError(f"{path}: not a readable gcov notes file")
        stamps = info_stamps(
            image, header.version, pointer_bits=ident.pointer_bits, big_endian=ident.big_endian
        )
        if not stamps:
            raise StampError(
                f"{binary}: no gcov_info for version {header.version} (from {path}); "
                "is it instrumented with -fprofile-arcs?"
            )
        if header.stamp not in stamps:
            raise StampError(
                f"{path}: stamp {header.stamp:#x} is not in {binary} "
                "(the binary is stale relative to its notes)"
            )
        verified.append(header.stamp)
    return verified
