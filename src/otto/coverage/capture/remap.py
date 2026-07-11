"""Map line numbers across a ``-U0`` unified diff.

One engine, two uses (spec §6): retrieval-time dirty-tree correction
(NEW = modified working tree → OLD = HEAD) and report-time manual
validity (OLD = capture anchored at ``base_commit`` → NEW = current
source).  Lines inside a hunk on either side have no counterpart and
map to ``None``.
"""

import re
from dataclasses import dataclass

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass(frozen=True)
class Hunk:
    """One ``@@ -a,b +c,d @@`` header (counts default to 1 when omitted)."""

    old_start: int
    old_count: int
    new_start: int
    new_count: int


def parse_u0_hunks(diff_text: str) -> list[Hunk]:
    """Parse hunk headers out of a single-file ``-U0`` diff."""
    hunks: list[Hunk] = []
    for line in diff_text.splitlines():
        m = _HUNK_RE.match(line)
        if m:
            old_start = int(m.group(1))
            old_count = int(m.group(2)) if m.group(2) is not None else 1
            new_start = int(m.group(3))
            new_count = int(m.group(4)) if m.group(4) is not None else 1
            hunks.append(Hunk(old_start, old_count, new_start, new_count))
    return hunks


class LineRemapper:
    """Bidirectional line mapping across the hunks of one file's diff.

    For each hunk, "has *line* moved past this hunk yet" is decided by a
    threshold computed from the *other* side's fields translated through
    the cumulative offset -- not from this hunk's own same-side
    ``start + count``.  That distinction only matters for a zero-count
    side (a pure insertion has ``old_count == 0``; a pure deletion has
    ``new_count == 0``): git's "position is the line before" convention
    means a zero-count side's own ``start`` is not a reliable same-side
    boundary marker (some hunks encode it as ``old_start + offset``, the
    line right at the gap; others as one less, the last untouched line
    before the gap -- both occur in practice). Translating the always-
    unambiguous opposite side through the running offset sidesteps the
    ambiguity entirely: ``old_start + old_count`` (resp. ``new_start +
    new_count``) is well-defined regardless of which convention the
    *other* side's zero-count marker happens to follow.
    """

    def __init__(self, hunks: list[Hunk]) -> None:
        self._hunks = hunks

    def new_to_old(self, line: int) -> int | None:
        """OLD-side line for NEW-side *line*, or None inside a changed hunk."""
        offset = 0  # old = new + offset, accumulated over hunks fully before `line`
        o2n_offset = 0  # new = old + o2n_offset, running through the current hunk
        for h in self._hunks:
            # NEW lines occupied by this hunk: [new_start, new_start+new_count)
            # (count 0 -> occupies nothing; a pure old-side deletion adds no
            # new lines, so it can never make a NEW line unmapped)
            if h.new_count > 0 and h.new_start <= line < h.new_start + h.new_count:
                return None
            o2n_offset += h.new_count - h.old_count
            # First untouched NEW line after this hunk = its OLD end translated
            # by the offset that applies once this hunk's shift is included.
            after_threshold = h.old_start + h.old_count + o2n_offset
            if line < after_threshold:
                break
            offset += h.old_count - h.new_count
        return line + offset

    def old_to_new(self, line: int) -> int | None:
        """NEW-side line for OLD-side *line*, or None when changed/deleted."""
        offset = 0  # new = old + offset, accumulated over hunks fully before `line`
        n2o_offset = 0  # old = new + n2o_offset, running through the current hunk
        for h in self._hunks:
            if h.old_count > 0 and h.old_start <= line < h.old_start + h.old_count:
                return None
            n2o_offset += h.old_count - h.new_count
            # First untouched OLD line after this hunk = its NEW end translated
            # by the offset that applies once this hunk's shift is included.
            after_threshold = h.new_start + h.new_count + n2o_offset
            if line < after_threshold:
                break
            offset += h.new_count - h.old_count
        return line + offset
