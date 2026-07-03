"""Scan source text for LCOV exclusion markers (spec §8).

lcov's geninfo already drops these regions from measured data; this scan
exists so the renderer can *show* exclusions instead of leaving them
indistinguishable from blank lines.
"""

_LINE_MARKERS = ("LCOV_EXCL_LINE", "LCOV_EXCL_BR_LINE")
_START_MARKERS = ("LCOV_EXCL_START", "LCOV_EXCL_BR_START")
_STOP_MARKERS = ("LCOV_EXCL_STOP", "LCOV_EXCL_BR_STOP")


def scan_excluded_lines(source: str, extra_markers: list[str] | None = None) -> set[int]:
    """1-based line numbers excluded by markers (block bounds inclusive)."""
    line_markers = _LINE_MARKERS + tuple(extra_markers or ())
    excluded: set[int] = set()
    in_block = False
    for lineno, text in enumerate(source.splitlines(), start=1):
        if in_block:
            excluded.add(lineno)
            if any(m in text for m in _STOP_MARKERS):
                in_block = False
            continue
        if any(m in text for m in _START_MARKERS):
            excluded.add(lineno)
            in_block = True
        elif any(m in text for m in line_markers):
            excluded.add(lineno)
    return excluded
