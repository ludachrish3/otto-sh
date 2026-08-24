"""Applying exclusion rules to a CoverageStore."""

import re
from pathlib import Path

from otto.coverage.exclusions.apply import apply_exclusions
from otto.coverage.exclusions.paths import glob_to_regex
from otto.coverage.exclusions.rules import MarkerRule, PathRule, RegexRule
from otto.coverage.store.model import BranchHits, CoverageStore

SRC = (
    "int main() {\n"  # 1
    "  int a = 1;\n"  # 2
    "  debug();  // LCOV_EXCL_LINE\n"  # 3
    "  return a;\n"  # 4
    "}\n"  # 5
)


def _store_with(tmp_path: Path, source: str, linenos: list[int]) -> tuple[CoverageStore, Path]:
    path = tmp_path / "main.c"
    path.write_text(source)
    store = CoverageStore(tier_order=["unit"])
    record = store.get_or_create_file(path)
    for lineno in linenos:
        record.get_or_create_line(lineno).hits.add("unit", 0)
    return store, path


def _path_rule(*globs: str, stat: str = "line") -> PathRule:
    return PathRule(stat=stat, patterns=[glob_to_regex(g) for g in globs], raw_patterns=list(globs))


def test_line_records_are_deleted_not_flagged(tmp_path: Path) -> None:
    store, path = _store_with(tmp_path, SRC, [1, 2, 3, 4])
    apply_exclusions(store, [], tmp_path)
    record = store.get_or_create_file(path)
    assert sorted(record.lines) == [1, 2, 4], "line 3 must be gone from the data"
    assert record.excluded_lines == {3}


def test_excluded_line_leaves_the_denominator(tmp_path: Path) -> None:
    store, path = _store_with(tmp_path, SRC, [1, 2, 3, 4])
    record = store.get_or_create_file(path)
    for lineno in (1, 2, 4):
        record.get_or_create_line(lineno).hits.add("unit", 1)
    before = record.line_coverage_pct()
    apply_exclusions(store, [], tmp_path)
    after = store.get_or_create_file(path).line_coverage_pct()
    assert before == 75.0
    assert after == 100.0, "removing the one uncovered line must move the number"


def test_branch_stat_clears_branches_and_keeps_the_line(tmp_path: Path) -> None:
    store, path = _store_with(tmp_path, "a;\nassert(x);\nb;\n", [1, 2, 3])
    record = store.get_or_create_file(path)
    record.lines[2].branches.append(BranchHits(block=0, branch=0))
    rule = RegexRule(stat="branch", pattern=re.compile(r"\bassert\("))
    apply_exclusions(store, [rule], tmp_path)
    assert 2 in record.lines, "branch-only exclusion must keep the line"
    assert record.lines[2].branches == []
    assert record.branch_excluded_lines == {2}


def test_a_branch_rule_records_a_line_that_has_no_record(tmp_path: Path) -> None:
    """A branch rule may name a source line gcov emitted no ``LineRecord`` for.

    Two things are pinned. The stage must not conjure a record to clear
    branches on, and the line number is still recorded: the set is the rules'
    verdict on the SOURCE, the same contract :attr:`excluded_lines` carries,
    so "no record here" is not a reason to drop it. Intersecting the final
    set with the surviving records would be the alternative, and this test is
    what makes that a decision rather than an accident.
    """
    store, path = _store_with(tmp_path, "a;\nif (x) y();\nb;\n", [1, 3])
    rule = RegexRule(stat="branch", pattern=re.compile(r"\bif\b"))
    apply_exclusions(store, [rule], tmp_path)
    record = store.get_or_create_file(path)
    assert sorted(record.lines) == [1, 3], "no record may be conjured for line 2"
    assert record.branch_excluded_lines == {2}


def test_a_stale_branch_exclusion_is_replaced_not_accumulated(tmp_path: Path) -> None:
    """Re-entry: the stage owns ``branch_excluded_lines`` outright.

    ``CoverageStore.load`` restores whatever the previous run recorded. If the
    configured rules changed since, that set is stale, and the stage must
    replace it exactly as it replaces :attr:`excluded_lines` — the two fields
    must not disagree about re-entry.
    """
    store, path = _store_with(tmp_path, "a;\nif (x) y();\nb;\n", [1, 2, 3])
    record = store.get_or_create_file(path)
    record.lines[2].branches.append(BranchHits(block=0, branch=0))
    # What a previous run, under a different rule set, left on the record.
    record.branch_excluded_lines = {99}
    record.excluded_lines = {98}

    apply_exclusions(store, [RegexRule(stat="branch", pattern=re.compile(r"\bif\b"))], tmp_path)

    kept = store.get_or_create_file(path)
    assert kept.branch_excluded_lines == {2}, "the stale 99 must not survive"
    assert kept.excluded_lines == set(), "and excluded_lines behaves the same way"


def test_line_stat_subsumes_branch_stat_on_the_same_line(tmp_path: Path) -> None:
    store, path = _store_with(tmp_path, "a;\nassert(x);\nb;\n", [1, 2, 3])
    store.get_or_create_file(path).lines[2].branches.append(BranchHits(block=0, branch=0))
    rules = [
        RegexRule(stat="branch", pattern=re.compile(r"\bassert\(")),
        RegexRule(stat="line", pattern=re.compile(r"\bassert\(")),
    ]
    apply_exclusions(store, rules, tmp_path)
    record = store.get_or_create_file(path)
    assert 2 not in record.lines
    assert record.branch_excluded_lines == set(), "a deleted line no longer 'still counts'"


def test_path_rule_drops_the_whole_file(tmp_path: Path) -> None:
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    store, _ = _store_with(tmp_path, "a;\n", [1])
    vendor_file = vendor / "v.c"
    vendor_file.write_text("a;\n")
    store.get_or_create_file(vendor_file).get_or_create_line(1).hits.add("unit", 0)
    assert store.file_count() == 2
    rule = _path_rule("vendor/**")
    apply_exclusions(store, [rule], tmp_path)
    assert store.file_count() == 1


def test_a_relative_glob_matches_under_a_symlinked_root(tmp_path: Path) -> None:
    """The store canonicalises every path it holds, so *root* must resolve too.

    ``get_or_create_file`` stores ``real/vendor/v.c``; an unresolved ``link``
    root makes ``relative_to`` raise, and every relative glob silently
    matches nothing.
    """
    real = tmp_path / "real"
    (real / "vendor").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real)
    vendor_file = link / "vendor" / "v.c"
    vendor_file.write_text("a;\n")
    store = CoverageStore(tier_order=["unit"])
    store.get_or_create_file(vendor_file).get_or_create_line(1).hits.add("unit", 0)
    assert store.file_count() == 1

    apply_exclusions(store, [_path_rule("vendor/**")], link)
    assert store.file_count() == 0, "a symlinked root must still name its own subdirectories"


def test_branch_path_rule_clears_branches_and_names_the_lines(tmp_path: Path) -> None:
    """A ``stat="branch"`` path rule keeps every line and reports what it cleared."""
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    vendor_file = vendor / "v.c"
    vendor_file.write_text("a;\nif (x) y();\nb;\n")
    store = CoverageStore(tier_order=["unit"])
    record = store.get_or_create_file(vendor_file)
    for lineno in (1, 2, 3):
        record.get_or_create_line(lineno).hits.add("unit", 0)
    record.lines[2].branches.append(BranchHits(block=0, branch=0))

    apply_exclusions(store, [_path_rule("vendor/**", stat="branch")], tmp_path)

    kept = store.get_or_create_file(vendor_file)
    assert sorted(kept.lines) == [1, 2, 3], "a branch-scoped path rule drops no lines"
    assert kept.lines[2].branches == []
    assert kept.branch_excluded_lines == {2}


def test_a_deleted_line_leaves_the_branch_excluded_set(tmp_path: Path) -> None:
    """A line the rules delete outright is not a line whose branches were excluded.

    The branch-scoped path rule clears line 2's branches, and then the
    built-in marker deletes line 2 entirely; only line 3 survives as
    branch-excluded.
    """
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    vendor_file = vendor / "v.c"
    vendor_file.write_text("a;\nif (x) y();  // LCOV_EXCL_LINE\nif (z) w();\n")
    store = CoverageStore(tier_order=["unit"])
    record = store.get_or_create_file(vendor_file)
    for lineno in (1, 2, 3):
        record.get_or_create_line(lineno).hits.add("unit", 0)
    record.lines[2].branches.append(BranchHits(block=0, branch=0))
    record.lines[3].branches.append(BranchHits(block=0, branch=0))

    apply_exclusions(store, [_path_rule("vendor/**", stat="branch")], tmp_path)

    kept = store.get_or_create_file(vendor_file)
    assert sorted(kept.lines) == [1, 3], "the marker still deletes its own line"
    assert kept.branch_excluded_lines == {3}


def test_a_path_excluded_file_is_never_opened(tmp_path: Path, monkeypatch) -> None:
    """Path rules short-circuit: no source read for a file being dropped."""
    store, _ = _store_with(tmp_path, "a;\n", [1])
    reads: list[Path] = []
    original = Path.read_text

    def spy(self, *args, **kwargs):
        reads.append(self)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", spy)
    rule = _path_rule("**")
    apply_exclusions(store, [rule], tmp_path)
    assert reads == []


def test_unreadable_source_keeps_every_line(tmp_path: Path, caplog) -> None:
    store = CoverageStore(tier_order=["unit"])
    missing = tmp_path / "gone.c"
    record = store.get_or_create_file(missing)
    for lineno in (1, 2, 3):
        record.get_or_create_line(lineno).hits.add("unit", 0)
    apply_exclusions(store, [], tmp_path)
    assert sorted(store.get_or_create_file(missing).lines) == [1, 2, 3]
    assert "gone.c" in caplog.text


def test_an_unreadable_source_reports_no_verdict_in_either_field(tmp_path: Path) -> None:
    """The unreadable-source arm cannot leave the two exclusion fields disagreeing.

    Both are cleared ahead of the read, so a re-entered record whose source
    has since become unreadable exits with NO verdict rather than the
    previous run's verdict in one field and nothing in the other. Every line
    is kept on this arm, so reporting stale exclusions beside them would be
    a claim the run never established.
    """
    store = CoverageStore(tier_order=["unit"])
    missing = tmp_path / "gone.c"
    record = store.get_or_create_file(missing)
    for lineno in (1, 2, 3):
        record.get_or_create_line(lineno).hits.add("unit", 0)
    # What CoverageStore.load restores from a previous run's store.json.
    record.excluded_lines = {98}
    record.branch_excluded_lines = {99}

    apply_exclusions(store, [], tmp_path)

    kept = store.get_or_create_file(missing)
    assert sorted(kept.lines) == [1, 2, 3], "an unreadable source keeps every line"
    assert kept.excluded_lines == set()
    assert kept.branch_excluded_lines == set()


def test_a_record_past_eof_is_never_excluded(tmp_path: Path) -> None:
    """Shrunk-file tolerance: the scan only sees lines the source has."""
    store, path = _store_with(tmp_path, SRC, [1, 2, 3, 4, 999])
    apply_exclusions(store, [], tmp_path)
    assert 999 in store.get_or_create_file(path).lines


def test_builtin_markers_apply_with_no_configured_rules(tmp_path: Path) -> None:
    """otto now enforces LCOV_EXCL_* itself, so harvested .info is covered."""
    store, path = _store_with(tmp_path, SRC, [3])
    apply_exclusions(store, [], tmp_path)
    assert store.get_or_create_file(path).lines == {}


def test_custom_marker_rule_removes_its_line(tmp_path: Path) -> None:
    src = "a;\nb();  // MYPROJ_NO_COV_LINE\nc;\n"
    store, path = _store_with(tmp_path, src, [1, 2, 3])
    apply_exclusions(store, [MarkerRule(stat="line", name="MYPROJ_NO_COV")], tmp_path)
    assert sorted(store.get_or_create_file(path).lines) == [1, 3]
