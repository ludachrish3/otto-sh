"""Scan source text for LCOV exclusion markers (spec §8).

lcov's geninfo already drops these regions from measured data; this scan
exists so the renderer can *show* exclusions instead of leaving them
indistinguishable from blank lines.
"""

_LINE_MARKERS = ("LCOV_EXCL_LINE", "LCOV_EXCL_BR_LINE")
_START_MARKERS = ("LCOV_EXCL_START", "LCOV_EXCL_BR_START")
_STOP_MARKERS = ("LCOV_EXCL_STOP", "LCOV_EXCL_BR_STOP")


def _marker_events(text: str, line_markers: tuple[str, ...]) -> list[tuple[int, str]]:
    """(position, kind) for every marker occurrence on a line, sorted by position.

    ``kind`` is ``"start"``, ``"stop"``, or ``"line"``. Sorting by string
    position lets callers resolve START/STOP pairs that share a line in
    either order (e.g. ``STOP ... START`` reopening a block mid-line).
    """
    events: list[tuple[int, str]] = []
    for marker in _START_MARKERS:
        pos = text.find(marker)
        if pos != -1:
            events.append((pos, "start"))
    for marker in _STOP_MARKERS:
        pos = text.find(marker)
        if pos != -1:
            events.append((pos, "stop"))
    for marker in line_markers:
        pos = text.find(marker)
        if pos != -1:
            events.append((pos, "line"))
    events.sort(key=lambda event: event[0])
    return events


def scan_excluded_lines(source: str, extra_markers: list[str] | None = None) -> set[int]:
    """1-based line numbers excluded by markers (block bounds inclusive)."""
    line_markers = _LINE_MARKERS + tuple(extra_markers or ())
    excluded: set[int] = set()
    in_block = False
    for lineno, text in enumerate(source.splitlines(), start=1):
        line_excluded = in_block
        for _pos, kind in _marker_events(text, line_markers):
            if kind == "line":
                line_excluded = True
            elif kind == "stop":
                if in_block:
                    in_block = False
                    line_excluded = True
            elif kind == "start":
                in_block = True
                line_excluded = True
        if line_excluded:
            excluded.add(lineno)
    return excluded
