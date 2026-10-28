"""Split one tree-wide ``git diff -M -w -U0`` stream into per-file diffs.

One subprocess answers the whole anchor chain for a capture (spec §9):
rename detection (``-M``), whitespace immunity (``-w``), and per-file
hunks arrive in a single pass instead of one ``git diff`` per file.
Keys are OLD paths — the coordinates a capture is anchored in.
"""

from collections.abc import Iterator
from dataclasses import dataclass, field

from .remap import Hunk, parse_u0_hunks

_DEV_NULL = "/dev/null"


@dataclass(frozen=True)
class FileDiff:
    """One file's slice of a tree diff, in capture (old-path) terms."""

    old_path: str
    new_path: str | None  # None = deleted since base
    hunks: list[Hunk] = field(default_factory=list)


def unquote(path: str) -> str:
    """Undo git's C-style quoting for paths with specials (best effort)."""
    if path.startswith('"') and path.endswith('"'):
        return path[1:-1].encode().decode("unicode_escape")
    return path


def strip_side(path: str, prefix: str) -> str | None:
    """Strip a diff side's ``a/``/``b/`` prefix, or ``None`` for ``/dev/null``."""
    path = unquote(path)
    if path == _DEV_NULL:
        return None
    if path.startswith(prefix):
        return path[len(prefix) :]
    return path


@dataclass(frozen=True)
class SectionHeader:
    """The paths one ``diff --git`` section names before its first ``@@``."""

    old: str | None
    """From ``rename from`` or ``--- a/``; ``None`` for a creation (the old-side path)."""

    new: str | None
    """From ``rename to`` or ``+++ b/``; ``None`` for a deletion (the new-side path)."""

    has_hunks: bool
    """Whether a ``+++`` marker was seen — a clean rename has none, and no hunk block."""


def iter_diff_sections(diff_text: str) -> Iterator[list[str]]:
    """Yield each ``diff --git`` section of a multi-file diff as its lines.

    Text before the first header is not a section and is dropped.
    """
    section: list[str] = []
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            if section:
                yield section
            section = [line]
        elif section:
            section.append(line)
    if section:
        yield section


def read_section_header(section: list[str]) -> SectionHeader:
    """Read a section's old and new paths from the headers before its first ``@@``.

    ``---``/``+++`` lines carry rename targets and ``/dev/null`` markers;
    clean renames have no hunk block, so their paths come from
    ``rename from``/``rename to`` instead. Every parser of a multi-file diff
    reads its sections through this, whatever it then keeps.
    """
    old: str | None = None
    new: str | None = None
    has_hunks = False
    for line in section:
        if line.startswith("@@ "):
            break
        if line.startswith("rename from "):
            old = unquote(line[len("rename from ") :])
        elif line.startswith("rename to "):
            new = unquote(line[len("rename to ") :])
        elif line.startswith("--- "):
            old = strip_side(line[4:], "a/")
        elif line.startswith("+++ "):
            new = strip_side(line[4:], "b/")
            has_hunks = True
    return SectionHeader(old=old, new=new, has_hunks=has_hunks)


def parse_multifile_u0(diff_text: str) -> dict[str, FileDiff]:
    """Parse a multi-file ``-U0`` diff into ``{old_path: FileDiff}``.

    Sections are delimited by ``diff --git`` headers. Old/new paths come
    from ``---``/``+++`` lines when present (they carry rename targets and
    ``/dev/null`` markers); clean renames have no hunk block, so their
    paths come from ``rename from``/``rename to`` lines instead. Pure
    additions (old side ``/dev/null``) are dropped — no capture is
    anchored in a file that did not exist at base.
    """
    out: dict[str, FileDiff] = {}
    for section in iter_diff_sections(diff_text):
        header = read_section_header(section)
        if header.old is None:
            continue  # pure addition (old side /dev/null) or unparsable
        hunks = parse_u0_hunks("\n".join(section)) if header.has_hunks or header.new is None else []
        out[header.old] = FileDiff(old_path=header.old, new_path=header.new, hunks=hunks)
    return out
