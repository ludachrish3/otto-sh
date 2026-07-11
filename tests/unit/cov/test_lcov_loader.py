"""Tests for the LCOV .info file loader."""

import pytest

from otto.coverage.merge.lcov_loader import LCOVLoader
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
