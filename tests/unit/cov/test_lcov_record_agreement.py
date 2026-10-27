"""The capture parser and the report loader read one ``.info`` the same way.

Both consume lcov's text: ``parse_info`` into a capture, ``LCOVLoader`` into
a store. A source that appears twice in one file (two ``SF:`` records for
the same path — what ``lcov -a`` concatenation and a per-object ``geninfo``
run both produce) must add up to the same totals on both paths; before the
shared reader the capture kept the LAST record and the loader the SUM.
"""

from pathlib import Path

from otto.coverage.capture.model import parse_info
from otto.coverage.merge.lcov_loader import LCOVLoader
from otto.coverage.store.model import CoverageStore

TWICE = """\
TN:
SF:{src}
DA:1,2
DA:2,0
BRDA:3,0,0,2
BRDA:3,0,1,-
end_of_record
SF:{src}
DA:1,3
DA:2,1
BRDA:3,0,0,-
BRDA:3,0,1,4
end_of_record
"""


def _info(tmp_path: Path) -> tuple[Path, Path]:
    src = tmp_path / "f.c"
    info = tmp_path / "twice.info"
    info.write_text(TWICE.format(src=src))
    return info, src


def test_a_repeated_source_adds_up_on_both_paths(tmp_path: Path) -> None:
    info, src = _info(tmp_path)

    lines, _ = parse_info(info)[str(src)]
    store = CoverageStore()
    LCOVLoader(store).load(info, "unit")
    record = store.get_or_create_file(src)

    assert lines == {1: 5, 2: 1}
    assert record.lines[1].hits.for_tier("unit") == 5
    assert record.lines[2].hits.for_tier("unit") == 1


def test_a_repeated_branch_is_reachable_if_either_record_reached_it(tmp_path: Path) -> None:
    info, src = _info(tmp_path)

    _, branches = parse_info(info)[str(src)]
    store = CoverageStore()
    LCOVLoader(store).load(info, "unit")
    stored = {(b.block, b.branch): b for b in store.get_or_create_file(src).lines[3].branches}

    # (block 0, branch 0): reached twice then never — still reached, 2 times.
    # (block 0, branch 1): never then 4 — reached, 4 times.
    assert branches[3] == [(0, 0, 2), (0, 1, 4)]
    assert stored[(0, 0)].is_reachable("unit") is True
    assert stored[(0, 0)].hits.for_tier("unit") == 2
    assert stored[(0, 1)].is_reachable("unit") is True
    assert stored[(0, 1)].hits.for_tier("unit") == 4


def test_the_loader_counts_a_repeated_source_once(tmp_path: Path) -> None:
    info, _ = _info(tmp_path)
    assert LCOVLoader(CoverageStore()).load(info, "unit") == 1
