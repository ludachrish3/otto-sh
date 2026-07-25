"""Tests for the coverage data model."""

import json
from pathlib import Path

import pytest

from otto.coverage.store.model import (
    BranchHits,
    CoverageStore,
    FileRecord,
    LineHits,
    LineRecord,
    Thresholds,
)


def test_load_rejects_old_format(tmp_path):
    p = tmp_path / "store.json"
    p.write_text('{"format": 1, "contexts": []}')
    with pytest.raises(ValueError, match="regenerate"):
        CoverageStore.load(p)


class TestLineHits:
    def test_defaults(self):
        h = LineHits()
        assert h.counts == {}
        assert h.total() == 0
        assert not h.is_hit()
        assert h.for_tier("system") == 0

    def test_add_and_total(self):
        h = LineHits()
        h.add("system", 3)
        h.add("unit", 5)
        h.add("manual", 1)
        assert h.total() == 9
        assert h.is_hit()
        assert h.is_hit("unit")
        assert not h.is_hit("nonexistent")

    def test_merge(self):
        a = LineHits(counts={"system": 1, "unit": 2})
        b = LineHits(counts={"system": 3, "manual": 4})
        a.merge(b)
        assert a.for_tier("system") == 4
        assert a.for_tier("unit") == 2
        assert a.for_tier("manual") == 4

    def test_to_dict(self):
        h = LineHits()
        h.add("system", 1)
        h.add("unit", 2)
        h.add("manual", 3)
        assert h.to_dict() == {"system": 1, "unit": 2, "manual": 3}


class TestBranchHits:
    def test_defaults(self):
        bh = BranchHits(block=0, branch=1)
        assert bh.branch_id == (0, 1)
        assert bh.is_reachable("system") is None
        assert bh.is_reachable() is None
        assert not bh.is_hit_for()

    def test_set_reachable(self):
        bh = BranchHits(block=0, branch=0)
        bh.set_reachable("system", True)
        assert bh.is_reachable("system") is True
        assert bh.is_reachable() is True

    def test_reachable_stays_true(self):
        bh = BranchHits(block=0, branch=0)
        bh.set_reachable("unit", True)
        bh.set_reachable("unit", False)
        assert bh.is_reachable("unit") is True

    def test_is_hit_for(self):
        bh = BranchHits(block=0, branch=0)
        bh.hits.add("system", 5)
        assert bh.is_hit_for("system")
        assert not bh.is_hit_for("unit")
        assert bh.is_hit_for()  # any tier

    def test_merge(self):
        a = BranchHits(block=0, branch=0)
        a.hits.add("system", 1)
        a.set_reachable("system", True)
        b = BranchHits(block=0, branch=0)
        b.hits.add("unit", 2)
        b.set_reachable("unit", True)
        a.merge(b)
        assert a.hits.for_tier("system") == 1
        assert a.hits.for_tier("unit") == 2
        assert a.is_reachable("system") is True
        assert a.is_reachable("unit") is True

    def test_to_dict(self):
        bh = BranchHits(block=1, branch=2)
        bh.hits.add("system", 3)
        d = bh.to_dict()
        assert d["block"] == 1
        assert d["branch"] == 2
        assert d["hits"]["system"] == 3


class TestLineRecord:
    def test_merge(self):
        a = LineRecord(line_number=10, hits=LineHits(counts={"system": 1}))
        b = LineRecord(line_number=10, hits=LineHits(counts={"unit": 2}))
        a.merge(b)
        assert a.hits.for_tier("system") == 1
        assert a.hits.for_tier("unit") == 2

    def test_merge_branches(self):
        a = LineRecord(line_number=5)
        a.branches.append(BranchHits(block=0, branch=0, hits=LineHits(counts={"system": 1})))
        b = LineRecord(line_number=5)
        b.branches.append(BranchHits(block=0, branch=0, hits=LineHits(counts={"unit": 2})))
        b.branches.append(BranchHits(block=0, branch=1, hits=LineHits(counts={"system": 3})))
        a.merge(b)
        assert len(a.branches) == 2
        assert a.branches[0].hits.for_tier("system") == 1
        assert a.branches[0].hits.for_tier("unit") == 2


class TestFileRecord:
    def test_get_or_create_line(self):
        fr = FileRecord(path=Path("/a.c"))
        lr = fr.get_or_create_line(10)
        assert lr.line_number == 10
        assert fr.get_or_create_line(10) is lr

    def test_line_coverage_pct(self):
        fr = FileRecord(path=Path("/a.c"))
        fr.get_or_create_line(1).hits.add("system", 1)
        fr.get_or_create_line(2)  # not hit
        assert fr.line_coverage_pct() == 50.0
        assert fr.line_coverage_pct("system") == 50.0
        assert fr.line_coverage_pct("unit") == 0.0

    def test_line_coverage_pct_empty(self):
        assert FileRecord(path=Path("/a.c")).line_coverage_pct() == 0.0

    def test_branch_coverage_pct(self):
        fr = FileRecord(path=Path("/a.c"))
        lr = fr.get_or_create_line(1)
        bh1 = BranchHits(block=0, branch=0, hits=LineHits(counts={"system": 1}))
        bh1.set_reachable("system", True)
        bh2 = BranchHits(block=0, branch=1)
        bh2.set_reachable("system", True)
        lr.branches = [bh1, bh2]
        assert fr.branch_coverage_pct("system") == 50.0

    def test_sorted_lines(self):
        fr = FileRecord(path=Path("/a.c"))
        fr.get_or_create_line(10)
        fr.get_or_create_line(1)
        fr.get_or_create_line(5)
        nums = [lr.line_number for lr in fr.sorted_lines()]
        assert nums == [1, 5, 10]


class TestCoverageStore:
    def test_get_or_create_file(self):
        store = CoverageStore()
        fr = store.get_or_create_file(Path("/a.c"))
        assert store.file_count() == 1
        assert store.get_or_create_file(Path("/a.c")) is fr

    def test_register_tier(self):
        store = CoverageStore()
        store.register_tier("unit")
        store.register_tier("system")
        store.register_tier("unit")  # duplicate, should be a no-op
        assert store.tier_order == ["unit", "system"]

    def test_init_with_tier_order(self):
        store = CoverageStore(tier_order=["unit", "system", "manual"])
        assert store.tier_order == ["unit", "system", "manual"]

    def test_overall_pct(self):
        store = CoverageStore()
        fr = store.get_or_create_file(Path("/a.c"))
        fr.get_or_create_line(1).hits.add("system", 1)
        fr.get_or_create_line(2)
        assert store.overall_pct() == 50.0
        assert store.overall_pct("system") == 50.0

    def test_save_load_roundtrip(self, tmp_path):
        store = CoverageStore(tier_order=["system", "unit"])
        fr = store.get_or_create_file(Path("/a.c"))
        lr = fr.get_or_create_line(1)
        lr.hits.add("system", 5)
        lr.state = "stale"
        bh = BranchHits(block=0, branch=0, hits=LineHits(counts={"system": 2}))
        bh.set_reachable("system", True)
        lr.branches.append(bh)

        save_path = tmp_path / "store.json"
        store.save(save_path)

        loaded = CoverageStore.load(save_path)
        assert loaded.file_count() == 1
        assert loaded.tier_order == ["system", "unit"]
        loaded_files = list(loaded.files())
        loaded_lr = loaded_files[0].lines[1]
        assert loaded_lr.hits.for_tier("system") == 5
        assert loaded_lr.state == "stale"
        assert len(loaded_lr.branches) == 1
        assert loaded_lr.branches[0].is_reachable("system") is True

    def test_merge_file(self):
        store = CoverageStore()
        fr1 = FileRecord(path=Path("/a.c"))
        fr1.get_or_create_line(1).hits.add("system", 3)
        store.merge_file(fr1)

        fr2 = FileRecord(path=Path("/a.c"))
        fr2.get_or_create_line(1).hits.add("unit", 2)
        store.merge_file(fr2)

        assert store.file_count() == 1
        merged = next(iter(store.files()))
        assert merged.lines[1].hits.for_tier("system") == 3
        assert merged.lines[1].hits.for_tier("unit") == 2

    def test_load_defaults_state_runs_tier_colors_when_absent(self, tmp_path):
        # A well-formed v4 file may still omit the optional "runs"/"tier_colors"
        # keys (e.g. a minimal hand-written fixture); load() must default them.
        minimal = {
            "format": 4,
            "tier_order": ["system"],
            "files": [
                {
                    "path": "/a.c",
                    "lines": {"1": {"hits": {"system": 1}, "branches": []}},
                }
            ],
        }
        save_path = tmp_path / "minimal.json"
        save_path.write_text(json.dumps(minimal))

        loaded = CoverageStore.load(save_path)
        assert loaded.runs == []
        assert loaded.tier_colors == {}
        loaded_lr = next(iter(loaded.files())).lines[1]
        assert loaded_lr.state is None

    def test_store_has_no_provenance_attribute(self, tmp_path):
        store = CoverageStore()
        assert not hasattr(store, "provenance")
        store.add_run(tier="manual", ticket="T-1")
        path = tmp_path / "store.json"
        store.save(path)
        assert "provenance" not in json.loads(path.read_text())

    def test_save_omits_removed_commit_keys(self, tmp_path):
        store = CoverageStore()
        fr = store.get_or_create_file(Path("/a.c"))
        fr.get_or_create_line(1).hits.add("system", 1)

        save_path = tmp_path / "store.json"
        store.save(save_path)

        raw = json.loads(save_path.read_text())
        line_dict = raw["files"][0]["lines"]["1"]
        assert "commit" not in line_dict
        assert "author" not in line_dict
        assert "summary" not in line_dict
        assert line_dict["state"] is None


class TestRuns:
    def test_add_run_allocates_sequential_ids(self):
        store = CoverageStore()
        a = store.add_run(tier="manual", label="rack2-slot4", board="rack2-slot4-id")
        b = store.add_run(tier="system", board="gw-a")
        assert (a, b) == (0, 1)
        assert store.runs[a].label == "rack2-slot4"
        assert store.runs[b].label == "gw-a"  # falls back to board

    def test_add_run_label_falls_back_to_tier(self):
        store = CoverageStore()
        rid = store.add_run(tier="unit")
        rec = store.runs[rid]
        assert rec.label == "unit"
        assert rec.board == ""
        assert rec.base_commit == ""
        assert rec.aging is False

    def test_line_merge_adds_run_hits_and_unions_stale(self):
        a = LineRecord(line_number=1)
        a.run_hits = {0: 2}
        a.stale_runs = [1]
        b = LineRecord(line_number=1)
        b.run_hits = {0: 3, 2: 1}
        b.stale_runs = [1, 3]
        a.merge(b)
        assert a.run_hits == {0: 5, 2: 1}
        assert a.stale_runs == [1, 3]

    def test_runs_roundtrip_through_store_json(self, tmp_path):
        store = CoverageStore(tier_order=["manual"])
        rid = store.add_run(
            tier="manual",
            label="slot4",
            board="slot4-id",
            labs=["lab1"],
            captured_at="2026-07-01T00:00:00Z",
            tester={"name": "Alice"},
            ticket="T-1",
            note="n",
            base_commit="deadbeef",
            dirty_remap=True,
        )
        store.runs[rid].aging = True
        store.tier_colors = {"manual": "#ff0000"}
        fr = store.get_or_create_file(Path("/a.c"))
        lr = fr.get_or_create_line(5)
        lr.hits.add("manual", 4)
        lr.run_hits[rid] = 4
        fr.get_or_create_line(6).stale_runs.append(rid)

        path = tmp_path / "store.json"
        store.save(path)
        raw = json.loads(path.read_text())
        assert raw["format"] == 4
        assert raw["runs"][0]["base_commit"] == "deadbeef"
        line5 = raw["files"][0]["lines"]["5"]
        assert line5["run"] == {"0": 4}
        assert "stale_run" not in line5  # omitted when empty
        assert raw["files"][0]["lines"]["6"]["stale_run"] == [0]

        loaded = CoverageStore.load(path)
        assert loaded.tier_colors == {"manual": "#ff0000"}
        (lrec,) = list(loaded.runs)
        assert (lrec.id, lrec.label, lrec.ticket, lrec.aging) == (0, "slot4", "T-1", True)
        assert lrec.dirty_remap is True
        assert lrec.base_commit == "deadbeef"
        (frec,) = list(loaded.files())
        assert frec.lines[5].run_hits == {0: 4}
        assert frec.lines[6].stale_runs == [0]

    def test_load_defaults_runs_when_absent(self, tmp_path):
        minimal = {
            "format": 4,
            "tier_order": ["system"],
            "files": [{"path": "/a.c", "lines": {"1": {"hits": {"system": 1}, "branches": []}}}],
        }
        path = tmp_path / "minimal.json"
        path.write_text(json.dumps(minimal))
        loaded = CoverageStore.load(path)
        assert loaded.runs == []
        assert next(iter(loaded.files())).lines[1].run_hits == {}

    def test_file_merge_clone_path_does_not_double_hits(self):
        # Regression: FileRecord.merge's else-branch seeded the clone with
        # copied counts AND then merged them again, doubling every hit.
        a = FileRecord(path=Path("/x.c"))
        b = FileRecord(path=Path("/x.c"))
        lb = b.get_or_create_line(1)
        lb.hits.add("system", 5)
        lb.run_hits = {0: 5}
        a.merge(b)
        assert a.lines[1].hits.for_tier("system") == 5
        assert a.lines[1].run_hits == {0: 5}


class TestStoreV4Config:
    def test_save_emits_v4_thresholds_and_stat_types(self, tmp_path) -> None:
        store = CoverageStore(tier_order=["system"])
        p = tmp_path / "store.json"
        store.save(p)
        raw = json.loads(p.read_text())
        assert raw["format"] == 4
        assert raw["thresholds"] == {"high": 80.0, "medium": 70.0}
        assert raw["stat_types"] == ["line", "branch", "decision"]

    def test_thresholds_roundtrip(self, tmp_path) -> None:
        store = CoverageStore(tier_order=["system"])
        store.thresholds = Thresholds(high=90.0, medium=75.0)
        p = tmp_path / "store.json"
        store.save(p)
        loaded = CoverageStore.load(p)
        assert loaded.thresholds == Thresholds(high=90.0, medium=75.0)

    def test_load_defaults_thresholds_when_absent(self, tmp_path) -> None:
        p = tmp_path / "store.json"
        p.write_text('{"format": 4, "tier_order": ["system"], "files": []}')
        loaded = CoverageStore.load(p)
        assert loaded.thresholds == Thresholds()

    def test_load_rejects_v3(self, tmp_path) -> None:
        p = tmp_path / "store.json"
        p.write_text('{"format": 3, "tier_order": [], "files": []}')
        with pytest.raises(ValueError, match="found v3"):
            CoverageStore.load(p)


class TestRunHost:
    def test_add_run_host_roundtrip(self, tmp_path) -> None:
        store = CoverageStore(tier_order=["nightly"])
        rid = store.add_run(tier="nightly", label="Rig One", board="rig-1", host="rig-1")
        p = tmp_path / "store.json"
        store.save(p)
        raw = json.loads(p.read_text())
        assert raw["runs"][0]["host"] == "rig-1"
        loaded = CoverageStore.load(p)
        assert loaded.runs[rid].host == "rig-1"

    def test_add_run_defaults_host_empty(self) -> None:
        store = CoverageStore(tier_order=["system"])
        rid = store.add_run(tier="system")
        assert store.runs[rid].host == ""

    def test_per_host_lines_derivable_from_run_hits(self) -> None:
        """Plan C's per-host breakdown needs no new per-line data: grouping
        LineRecord.run_hits by RunRecord.host reconstructs per-host line
        counts, because one capture == one host == one run."""
        store = CoverageStore(tier_order=["nightly"])
        r1 = store.add_run(tier="nightly", label="Rig One", host="rig-1")
        r2 = store.add_run(tier="nightly", label="Rig Two", host="rig-2")
        f = FileRecord(path=Path("src/a.c"))
        l1 = f.get_or_create_line(1)
        l1.hits.add("nightly", 1)
        l1.run_hits = {r1: 1}
        l2 = f.get_or_create_line(2)
        l2.hits.add("nightly", 1)
        l2.run_hits = {r1: 1, r2: 1}
        store.merge_file(f)
        per_host: dict[str, int] = {}
        for rec in store.files():
            for line in rec.lines.values():
                for rid in line.run_hits:
                    host = store.runs[rid].host
                    per_host[host] = per_host.get(host, 0) + 1
        assert per_host == {"rig-1": 2, "rig-2": 1}


class TestLineTicketSlot:
    def test_ticket_roundtrip(self, tmp_path) -> None:
        store = CoverageStore(tier_order=["system"])
        f = FileRecord(path=Path("a.c"))
        line = f.get_or_create_line(1)
        line.hits.add("system", 1)
        line.ticket = "PROJ-123"
        store.merge_file(f)
        p = tmp_path / "store.json"
        store.save(p)
        loaded = CoverageStore.load(p)
        assert next(iter(loaded.files())).lines[1].ticket == "PROJ-123"

    def test_ticket_absent_when_unset(self, tmp_path) -> None:
        store = CoverageStore(tier_order=["system"])
        f = FileRecord(path=Path("a.c"))
        f.get_or_create_line(1).hits.add("system", 1)
        store.merge_file(f)
        p = tmp_path / "store.json"
        store.save(p)
        raw = json.loads(p.read_text())
        (line_dict,) = raw["files"][0]["lines"].values()
        assert "ticket" not in line_dict

    def test_merge_keeps_first_set_ticket(self) -> None:
        a = LineRecord(line_number=1, ticket="PROJ-1")
        b = LineRecord(line_number=1, ticket="PROJ-2")
        a.merge(b)
        assert a.ticket == "PROJ-1"
        c = LineRecord(line_number=1)
        c.merge(b)
        assert c.ticket == "PROJ-2"
