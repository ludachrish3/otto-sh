"""Tests for the LCOV .info file loader."""

import pytest

from otto.coverage.merge.lcov_loader import (
    LCOVLoader,
    _parse_block_id,
    parse_fn_record,
    parse_fnda_record,
)
from otto.coverage.merge.paths import PathMapping, PathRemapper
from otto.coverage.store.model import CoverageStore

SAMPLE_INFO = """\
TN:test
SF:/build/src/foo.c
DA:1,5
DA:2,0
DA:3,10
BRDA:3,0,0,5
BRDA:3,0,1,-
end_of_record
SF:/build/src/bar.c
DA:1,1
end_of_record
"""


@pytest.fixture
def info_file(tmp_path):
    p = tmp_path / "test.info"
    p.write_text(SAMPLE_INFO)
    return p


@pytest.fixture
def source_tree(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "foo.c").write_text("line1\nline2\nline3\n")
    (src / "bar.c").write_text("line1\n")
    return tmp_path


class TestLCOVLoader:
    def test_load_basic(self, info_file, source_tree):
        store = CoverageStore()
        remapper = PathRemapper(
            [
                PathMapping("/build", str(source_tree)),
            ]
        )
        loader = LCOVLoader(store, remapper)
        n = loader.load(info_file, "system")
        assert n == 2
        assert store.file_count() == 2
        # Loader registered the tier
        assert "system" in store.tier_order

    def test_line_hits(self, info_file, source_tree):
        store = CoverageStore()
        remapper = PathRemapper([PathMapping("/build", str(source_tree))])
        loader = LCOVLoader(store, remapper)
        loader.load(info_file, "system")

        foo = store.get_or_create_file(source_tree / "src" / "foo.c")
        assert foo.lines[1].hits.for_tier("system") == 5
        assert foo.lines[2].hits.for_tier("system") == 0
        assert foo.lines[3].hits.for_tier("system") == 10

    def test_branch_data(self, info_file, source_tree):
        store = CoverageStore()
        remapper = PathRemapper([PathMapping("/build", str(source_tree))])
        loader = LCOVLoader(store, remapper)
        loader.load(info_file, "system")

        foo = store.get_or_create_file(source_tree / "src" / "foo.c")
        branches = foo.lines[3].branches
        assert len(branches) == 2

        taken = next(b for b in branches if b.branch == 0)
        assert taken.hits.for_tier("system") == 5
        assert taken.is_reachable("system") is True

        unreachable = next(b for b in branches if b.branch == 1)
        assert unreachable.hits.for_tier("system") == 0
        assert unreachable.is_reachable("system") is False

    def test_multi_tier_loading(self, source_tree, tmp_path):
        system_info = tmp_path / "system.info"
        system_info.write_text(f"TN:\nSF:{source_tree}/src/foo.c\nDA:1,3\nend_of_record\n")
        unit_info = tmp_path / "unit.info"
        unit_info.write_text(f"TN:\nSF:{source_tree}/src/foo.c\nDA:1,7\nDA:2,1\nend_of_record\n")

        store = CoverageStore()
        remapper = PathRemapper([])  # paths already local
        loader = LCOVLoader(store, remapper)
        loader.load(system_info, "system")
        loader.load(unit_info, "unit")

        foo = store.get_or_create_file(source_tree / "src" / "foo.c")
        assert foo.lines[1].hits.for_tier("system") == 3
        assert foo.lines[1].hits.for_tier("unit") == 7
        assert foo.lines[2].hits.for_tier("unit") == 1
        assert foo.lines[2].hits.for_tier("system") == 0
        # Both tiers registered in load order
        assert store.tier_order == ["system", "unit"]

    def test_arbitrary_tier_name(self, source_tree, tmp_path):
        """Loader should accept any tier name, not just the canonical ones."""
        info = tmp_path / "smoke.info"
        info.write_text(f"TN:\nSF:{source_tree}/src/foo.c\nDA:1,2\nend_of_record\n")
        store = CoverageStore()
        loader = LCOVLoader(store, PathRemapper([]))
        loader.load(info, "smoke")

        foo = store.get_or_create_file(source_tree / "src" / "foo.c")
        assert foo.lines[1].hits.for_tier("smoke") == 2
        assert "smoke" in store.tier_order

    def test_load_credits_run_id_for_hit_lines(self, tmp_path):
        from otto.coverage.store.model import CoverageStore

        info = tmp_path / "x.info"
        info.write_text(f"TN:\nSF:{tmp_path / 'f.c'}\nDA:1,3\nDA:2,0\nend_of_record\n")
        store = CoverageStore()
        run_id = store.add_run(tier="unit")
        loader = LCOVLoader(store, PathRemapper([]))
        loader.load(info, "unit", run_id=run_id)

        (fr,) = list(store.files())
        assert fr.lines[1].run_hits == {run_id: 3}
        assert fr.lines[2].run_hits == {}  # zero-count line: no run credit
        assert fr.lines[1].hits.for_tier("unit") == 3


class TestFunctionRecords:
    def test_parse_fn_two_field_form(self):
        fn = parse_fn_record("4,checked_add")
        assert (fn.start_line, fn.end_line, fn.name) == (4, None, "checked_add")

    def test_parse_fn_three_field_form_lcov2(self):
        fn = parse_fn_record("4,9,checked_add")
        assert (fn.start_line, fn.end_line, fn.name) == (4, 9, "checked_add")

    def test_parse_fn_keeps_commas_inside_a_cpp_name(self):
        fn = parse_fn_record("12,std::pair<int, char> make(int, char)")
        assert fn.start_line == 12
        assert fn.end_line is None
        assert fn.name == "std::pair<int, char> make(int, char)"

    def test_parse_fn_numeric_name_is_not_mistaken_for_an_end_line(self):
        fn = parse_fn_record("4,123")
        assert (fn.start_line, fn.end_line, fn.name) == (4, None, "123")

    def test_parse_fnda_splits_once(self):
        hit = parse_fnda_record("7,std::pair<int, char> make(int, char)")
        assert hit.count == 7
        assert hit.name == "std::pair<int, char> make(int, char)"

    def test_load_records_functions_per_tier(self, source_tree, tmp_path):
        info = tmp_path / "fn.info"
        info.write_text(
            f"TN:\nSF:{source_tree}/src/foo.c\n"
            "FN:1,foo\nFN:3,bar\nFNDA:5,foo\nFNDA:0,bar\nDA:1,5\nDA:3,0\nend_of_record\n"
        )
        store = CoverageStore()
        LCOVLoader(store, PathRemapper([])).load(info, "system")
        foo = store.get_or_create_file(source_tree / "src" / "foo.c")
        assert foo.functions["foo"].start_line == 1
        assert foo.functions["foo"].hits.for_tier("system") == 5
        assert foo.functions["bar"].hits.for_tier("system") == 0
        assert foo.functions["bar"].hits.is_hit() is False

    def test_two_loads_merge_function_hits_across_tiers(self, source_tree, tmp_path):
        a = tmp_path / "a.info"
        a.write_text(f"TN:\nSF:{source_tree}/src/foo.c\nFN:1,foo\nFNDA:2,foo\nend_of_record\n")
        b = tmp_path / "b.info"
        b.write_text(f"TN:\nSF:{source_tree}/src/foo.c\nFN:1,foo\nFNDA:3,foo\nend_of_record\n")
        store = CoverageStore()
        loader = LCOVLoader(store, PathRemapper([]))
        loader.load(a, "system")
        loader.load(b, "unit")
        foo = store.get_or_create_file(source_tree / "src" / "foo.c")
        assert foo.functions["foo"].hits.to_dict() == {"system": 2, "unit": 3}

    def test_orphan_fnda_is_kept_at_line_zero_with_a_warning(self, source_tree, tmp_path, caplog):
        info = tmp_path / "orphan.info"
        info.write_text(f"TN:\nSF:{source_tree}/src/foo.c\nFNDA:4,ghost\nend_of_record\n")
        store = CoverageStore()
        with caplog.at_level("WARNING", logger="otto.coverage.merge.lcov_loader"):
            LCOVLoader(store, PathRemapper([])).load(info, "system")
        foo = store.get_or_create_file(source_tree / "src" / "foo.c")
        assert foo.functions["ghost"].start_line == 0
        assert foo.functions["ghost"].hits.for_tier("system") == 4
        assert any("ghost" in r.message and "FN" in r.message for r in caplog.records)


class TestBlockIdParsing:
    """``BRDA:<line>,[e]<block>,<branch>,<taken>`` (``man geninfo``): a
    leading ``e`` on the block field is the exception tag — "'exception
    tag' is 'e' if this is a branch related to exception handling" —
    confirmed against lcov 2.0's own parser (``lcovutil.pm``:
    ``/^BRDA:(\\d+),(e?)(\\d+),(.+)$/``). Seen live as
    ``BRDA:<line>,e0,<branch>,<taken>`` from arm64 kernel headers'
    asm-goto alternatives in a kmod product's report; otto's store has no
    exception-branch concept, so these records are dropped entirely."""

    def test_decimal_block_id_stays_decimal(self):
        assert _parse_block_id("20") == 20

    def test_exception_tagged_block_id_is_dropped(self):
        assert _parse_block_id("e0") is None

    def test_load_drops_an_exception_tagged_branch(self, source_tree, tmp_path):
        info = tmp_path / "exception_block.info"
        info.write_text(
            f"TN:\nSF:{source_tree}/src/foo.c\nDA:3,10\nBRDA:3,e0,0,5\nBRDA:3,e0,1,-\nend_of_record\n"
        )
        store = CoverageStore()
        LCOVLoader(store, PathRemapper([])).load(info, "system")
        foo = store.get_or_create_file(source_tree / "src" / "foo.c")
        assert foo.lines[3].branches == []

    def test_load_keeps_an_untagged_branch(self, source_tree, tmp_path):
        info = tmp_path / "untagged_block.info"
        info.write_text(
            f"TN:\nSF:{source_tree}/src/foo.c\nDA:3,10\nBRDA:3,0,0,5\nBRDA:3,0,1,-\nend_of_record\n"
        )
        store = CoverageStore()
        LCOVLoader(store, PathRemapper([])).load(info, "system")
        foo = store.get_or_create_file(source_tree / "src" / "foo.c")
        branches = foo.lines[3].branches
        assert len(branches) == 2
        assert all(b.block == 0 for b in branches)

    def test_load_a_mixed_file_yields_only_the_untagged_branches(self, source_tree, tmp_path):
        info = tmp_path / "mixed_block.info"
        info.write_text(
            f"TN:\nSF:{source_tree}/src/foo.c\nDA:3,10\n"
            "BRDA:3,0,0,5\nBRDA:3,0,1,-\nBRDA:3,e1,0,3\nBRDA:3,e1,1,-\n"
            "end_of_record\n"
        )
        store = CoverageStore()
        LCOVLoader(store, PathRemapper([])).load(info, "system")
        foo = store.get_or_create_file(source_tree / "src" / "foo.c")
        branches = foo.lines[3].branches
        assert len(branches) == 2
        assert all(b.block == 0 for b in branches)
