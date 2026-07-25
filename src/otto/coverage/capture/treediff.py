"""Split one tree-wide ``git diff -M -w -U0`` stream into per-file diffs.

One subprocess answers the whole anchor chain for a capture (spec §9):
rename detection (``-M``), whitespace immunity (``-w``), and per-file
hunks arrive in a single pass instead of one ``git diff`` per file.
Keys are OLD paths — the coordinates a capture is anchored in.
"""

from dataclasses import dataclass, field

from .remap import Hunk, parse_u0_hunks

_DEV_NULL = "/dev/null"


@dataclass(frozen=True)
class FileDiff:
    """One file's slice of a tree diff, in capture (old-path) terms."""

    old_path: str
    new_path: str | None  # None = deleted since base
    hunks: list[Hunk] = field(default_factory=list)


def _unquote(path: str) -> str:
    """Undo git's C-style quoting for paths with specials (best effort)."""
    if path.startswith('"') and path.endswith('"'):
        return path[1:-1].encode().decode("unicode_escape")
    return path


def _strip_side(path: str, prefix: str) -> str | None:
    path = _unquote(path)
    if path == _DEV_NULL:
        return None
    if path.startswith(prefix):
        return path[len(prefix) :]
    return path


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
    section: list[str] = []

    def flush() -> None:
        if not section:
            return
        old: str | None = None
        new: str | None = None
        have_newline = False
        in_hunks = False
        for line in section:
            if line.startswith("@@ "):
                in_hunks = True
            if not in_hunks:
                if line.startswith("rename from "):
                    old = _unquote(line[len("rename from ") :])
                elif line.startswith("rename to "):
                    new = _unquote(line[len("rename to ") :])
                elif line.startswith("--- "):
                    old = _strip_side(line[4:], "a/")
                elif line.startswith("+++ "):
                    new = _strip_side(line[4:], "b/")
                    have_newline = True
        if old is None:
            return  # pure addition (old side /dev/null) or unparsable
        hunks = parse_u0_hunks("\n".join(section)) if have_newline or new is None else []
        out[old] = FileDiff(old_path=old, new_path=new, hunks=hunks)

    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            flush()
            section = [line]
        elif section:
            section.append(line)
    flush()
    return out
