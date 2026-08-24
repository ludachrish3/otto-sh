"""End-to-end guards: exclusions must MOVE the reported numbers.

Asserting that a filtered store reports some number is a guard that cannot
fail. Every test here computes the same store twice — rules on and rules
off — and asserts the difference.
"""

import re
from pathlib import Path

import pytest

from otto.coverage.exclusions.apply import apply_exclusions
from otto.coverage.exclusions.rules import PreprocessorRule
from otto.coverage.renderer.spa_data import build_index_payload
from otto.coverage.store.model import CoverageStore

SRC = (
    "int main() {\n"  # 1 hit
    "  int a = 1;\n"  # 2 hit
    "#ifdef DEBUG_LOG\n"  # 3 excluded
    "  dump_a();\n"  # 4 excluded, uncovered
    "  dump_b();\n"  # 5 excluded, uncovered
    "#endif\n"  # 6
    "  return a;\n"  # 7 hit
    "}\n"  # 8
)


def _build(tmp_path: Path) -> tuple[CoverageStore, Path]:
    path = tmp_path / "main.c"
    path.write_text(SRC)
    store = CoverageStore(tier_order=["unit"])
    record = store.get_or_create_file(path)
    for lineno in (1, 2, 7):
        record.get_or_create_line(lineno).hits.add("unit", 1)
    for lineno in (4, 5):
        record.get_or_create_line(lineno).hits.add("unit", 0)
    return store, path


def _rule() -> PreprocessorRule:
    return PreprocessorRule(stat="line", pattern=re.compile(r"#ifdef DEBUG_LOG"))


def test_file_percentage_moves(tmp_path: Path) -> None:
    unfiltered, path = _build(tmp_path)
    baseline = unfiltered.get_or_create_file(path).line_coverage_pct()

    filtered, path2 = _build(tmp_path)
    apply_exclusions(filtered, [_rule()], tmp_path)
    after = filtered.get_or_create_file(path2).line_coverage_pct()

    assert baseline == pytest.approx(60.0)
    assert after == pytest.approx(100.0)


def test_index_payload_totals_move(tmp_path: Path) -> None:
    """The tree rollup the SPA reads must shrink too, not just the record."""
    unfiltered, _ = _build(tmp_path)
    before = build_index_payload(unfiltered, project_name="P", prefix=tmp_path, stamp="S")

    filtered, _ = _build(tmp_path)
    apply_exclusions(filtered, [_rule()], tmp_path)
    after = build_index_payload(filtered, project_name="P", prefix=tmp_path, stamp="S")

    assert before["total_lines"] == 5
    assert after["total_lines"] == 3
    assert before["tree"]["stats"]["lines"]["hit"] == 3
    assert after["tree"]["stats"]["lines"]["hit"] == 3


def test_excluded_flag_still_reports_the_count(tmp_path: Path) -> None:
    """Deleting the records must not cost the per-file 'N excl' pill."""
    filtered, _ = _build(tmp_path)
    apply_exclusions(filtered, [_rule()], tmp_path)
    payload = build_index_payload(filtered, project_name="P", prefix=tmp_path, stamp="S")
    # Lines 3, 4, 5. The #endif on line 6 is the arm TERMINATOR and is
    # deliberately not excluded (Task 3: the opening directive is greyed,
    # the terminator is not, so a following #else stays uncoloured).
    assert payload["tree"]["stats"]["flags"]["excluded"] == 3


@pytest.mark.asyncio
async def test_excluded_lines_never_reach_ticket_attribution(tmp_path, monkeypatch) -> None:
    """Placement guard: the filter runs BEFORE attribution.

    Attribution walks the store's final line set. If the filter ran after it,
    an excluded line would still be attributed to a ticket and counted in
    per-ticket coverage.
    """
    from otto.coverage.renderer import spa_renderer as spa_renderer_module
    from otto.coverage.reporter import CollectionInputs, CoverageReporter

    source = tmp_path / "main.c"
    source.write_text(SRC)
    info = tmp_path / "unit.info"
    info.write_text(f"TN:\nSF:{source}\nDA:1,1\nDA:2,1\nDA:4,0\nDA:5,0\nDA:7,1\nend_of_record\n")

    seen: dict[str, set[int]] = {}

    def fake_annotate(self, store, repo_root):
        for record in store.files():
            seen[record.path.name] = set(record.lines)
        # Falls off the end returning None, which is the real
        # _annotate_tickets' "no attribution" signal — it is what gates
        # _apply_overrides, so the double has to reproduce it.

    class FakeRenderer:
        def __init__(self, output_dir, *, project_name="Coverage Report", prefix=None):
            pass

        def render(self, store):
            pass

    monkeypatch.setattr(CoverageReporter, "_annotate_tickets", fake_annotate)
    monkeypatch.setattr(spa_renderer_module, "SpaRenderer", FakeRenderer)

    reporter = CoverageReporter(
        gcda_dirs=[],
        source_root=tmp_path,
        output_dir=tmp_path / "out",
        tiers=[("unit", info)],
        collection=CollectionInputs(repo_root=tmp_path, exclusion_rules=[_rule()]),
    )
    await reporter.run()

    assert seen["main.c"] == {1, 2, 7}, (
        "lines 4 and 5 are inside the excluded #ifdef arm and must already be "
        "gone by the time attribution sees the store"
    )
