"""Deterministic coverage-report fixture.

One builder shared by the report browser suite
(tests/e2e/cov/report_browser/) and the docs-media screenshot
(scripts/capture_docs_media.py), so the pixels users see in the guide are
produced by the exact HTML the browser tests pin.

Two tiers (system, unit), two files, every pill state the renderer knows:
branch-taken, branch-not-taken, branch-unreachable — plus a fully covered
file and a partially covered one so sorting has something to reorder.
Rendered with ``prefix=base_dir`` so displayed paths are the deterministic
``product/main.c`` / ``product/utils.c`` regardless of the tmp dir.
"""

from pathlib import Path

from otto.coverage.renderer.html_renderer import HtmlRenderer
from otto.coverage.store.model import (
    BranchHits,
    CoverageStore,
    FileRecord,
    LineRecord,
)

_MAIN_C = """\
#include <stdio.h>

int checked_add(int a, int b) {
    if (a > 0 && b > 0) {
        return a + b;
    }
    return 0;
}

int main(void) {
    printf("%d\\n", checked_add(20, 22));
    return 0;
}
"""

_UTILS_C = """\
int double_it(int x) {
    return x * 2;
}

int never_called(int x) {
    return x - 1;
}
"""


def _line(number: int, hits: dict[str, int]) -> LineRecord:
    rec = LineRecord(line_number=number)
    for tier, n in hits.items():
        rec.hits.add(tier, n)
    return rec


def _branch(block: int, branch: int, hits: dict[str, int], *, reachable: bool) -> BranchHits:
    bh = BranchHits(block=block, branch=branch)
    for tier, n in hits.items():
        bh.hits.add(tier, n)
    for tier in ("system", "unit"):
        bh.set_reachable(tier, reachable)
    return bh


def build_fixture_report(base_dir: Path) -> Path:
    """Write sample sources under *base_dir* and render the report; return its dir.

    FileRecords carry absolute paths (the renderer reads the sources from
    them); ``prefix=base_dir`` makes the *displayed* paths the short
    ``product/...`` form — same strings in the browser pins and the docs
    screenshot.
    """
    src_dir = base_dir / "product"
    src_dir.mkdir(parents=True, exist_ok=True)
    (src_dir / "main.c").write_text(_MAIN_C)
    (src_dir / "utils.c").write_text(_UTILS_C)

    store = CoverageStore(tier_order=["system", "unit"])

    main_rec = FileRecord(path=src_dir / "main.c")
    for lineno, hits in [
        (3, {"system": 4, "unit": 12}),
        (4, {"system": 4, "unit": 12}),
        (5, {"system": 4, "unit": 8}),
        (7, {"unit": 4}),
        (10, {"system": 4}),
        (11, {"system": 4}),
        (12, {"system": 4}),
    ]:
        main_rec.lines[lineno] = _line(lineno, hits)
    # The `if (a > 0 && b > 0)` line: one taken pair-half, one never-taken,
    # one unreachable — all three pill classes on one line.
    main_rec.lines[4].branches = [
        _branch(0, 0, {"system": 4, "unit": 8}, reachable=True),
        _branch(0, 1, {}, reachable=True),
        _branch(0, 2, {}, reachable=False),
    ]
    store.merge_file(main_rec)

    utils_rec = FileRecord(path=src_dir / "utils.c")
    utils_rec.lines[2] = _line(2, {"unit": 6})
    utils_rec.lines[6] = _line(6, {})
    store.merge_file(utils_rec)

    report_dir = base_dir / "report"
    HtmlRenderer(report_dir, project_name="otto example product", prefix=base_dir).render(store)
    return report_dir
