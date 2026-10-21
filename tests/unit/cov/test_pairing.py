import struct
from pathlib import Path, PurePosixPath

import pytest

from otto.coverage.errors import CoverageDataMismatchError, PairingError
from otto.coverage.objects import ObjectList, Unavailable
from otto.coverage.pairing import GcnoIndex, pair_product

NONE = Unavailable(product="app", reason="test")


def _write(path: Path, magic: bytes, stamp: int, extra: bytes = b"") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(magic + b"B33*" + struct.pack("<I", stamp) + extra)
    return path


def _gcda(product_dir: Path, rel: str, stamp: int) -> Path:
    return _write(product_dir / f"{rel}.gcda", b"adcg", stamp)


def _gcno(path: Path, stamp: int, extra: bytes = b"") -> Path:
    return _write(path, b"oncg", stamp, extra)


def _objects(*paths: str) -> ObjectList:
    return ObjectList(product="app", paths=[PurePosixPath(p) for p in paths], source="t")


def test_local_build_never_walks_the_root(tmp_path):
    root = tmp_path / "proj"
    obj = root / "build/app.dir/src/main.cpp"
    _gcno(obj.with_name("main.cpp.gcno"), 1)
    _gcno(root / "build-old/app.dir/src/main.cpp.gcno", 1)
    _gcno(root / "tests/build/main.cpp.gcno", 1)
    pdir = tmp_path / "cov/h/app"
    _gcda(pdir, str(obj).lstrip("/"), 1)
    index = GcnoIndex(root)
    pairs = pair_product(pdir, _objects(str(obj)), index, label="h/app")
    assert [p.gcno for p in pairs] == [obj.with_name("main.cpp.gcno")]
    assert index.walks == 0


def test_incompatible_local_notes_is_a_mismatch_not_a_search(tmp_path):
    root = tmp_path / "proj"
    obj = root / "build/src/main.cpp"
    _gcno(obj.with_name("main.cpp.gcno"), 9)  # rebuilt since the binary
    _gcno(root / "build-old/src/main.cpp.gcno", 1)  # stale tree that happens to match
    pdir = tmp_path / "cov/h/app"
    _gcda(pdir, str(obj).lstrip("/"), 1)
    index = GcnoIndex(root)
    with pytest.raises(CoverageDataMismatchError, match="stamp mismatch"):
        pair_product(pdir, _objects(str(obj)), index, label="h/app")
    assert index.walks == 0


def test_mismatch_frame_and_detail_give_one_remedy(tmp_path):
    root = tmp_path / "proj"
    obj = root / "build/src/main.cpp"
    _gcno(obj.with_name("main.cpp.gcno"), 9)
    pdir = tmp_path / "cov/h/app"
    _gcda(pdir, str(obj).lstrip("/"), 1)
    with pytest.raises(CoverageDataMismatchError) as ei:
        pair_product(pdir, _objects(str(obj)), GcnoIndex(root), label="h/app")
    msg = str(ei.value)
    # The counters on the host belong to the deployed build: restore it and fetch
    # again, or redeploy and re-run the tests. Never "re-run and report on a new dir".
    assert "otto cov get" in msg
    assert "redeploy" in msg
    assert "report on the new output directory" not in msg
    assert "rebuild and redeploy" not in msg


def test_format_version_mismatch_frame_does_not_claim_build_stamps(tmp_path):
    root = tmp_path / "proj"
    obj = root / "build/src/main.cpp"
    notes = obj.with_name("main.cpp.gcno")
    notes.parent.mkdir(parents=True)
    notes.write_bytes(b"oncgA12*" + struct.pack("<I", 1))
    pdir = tmp_path / "cov/h/app"
    _gcda(pdir, str(obj).lstrip("/"), 1)
    with pytest.raises(CoverageDataMismatchError) as ei:
        pair_product(pdir, _objects(str(obj)), GcnoIndex(root), label="h/app")
    msg = str(ei.value)
    assert "format version" in msg
    assert "carry different build stamps" not in msg


def test_relocated_builds_walk_once_and_take_the_stamp_match(tmp_path):
    root = tmp_path / "proj"
    _gcno(root / "build/app.dir/src/main.cpp.gcno", 2)
    _gcno(root / "build-old/app.dir/src/main.cpp.gcno", 1)
    _gcno(root / "build/app.dir/src/util.cpp.gcno", 3)
    pdir = tmp_path / "cov/h/app"
    _gcda(pdir, "build/app.dir/src/main.cpp", 2)
    _gcda(pdir, "build/app.dir/src/util.cpp", 3)
    index = GcnoIndex(root)
    objs = _objects("/build/app.dir/src/main.cpp", "/build/app.dir/src/util.cpp")
    pairs = pair_product(pdir, objs, index, label="h/app")
    assert pairs[0].gcno == root / "build/app.dir/src/main.cpp.gcno"
    assert index.walks == 1


def test_component_wise_matching(tmp_path):
    root = tmp_path / "proj"
    _gcno(root / "b/xmain.cpp.gcno", 1)
    pdir = tmp_path / "cov/h/app"
    _gcda(pdir, "b/main.cpp", 1)
    with pytest.raises(PairingError) as e:
        pair_product(pdir, NONE, GcnoIndex(root), label="h/app")
    assert e.value.kind == "no-notes"


def test_no_notes_lists_the_rejected_candidates(tmp_path):
    root = tmp_path / "proj"
    _gcno(root / "b/main.cpp.gcno", 5)
    pdir = tmp_path / "cov/h/app"
    _gcda(pdir, "b/main.cpp", 1)
    with pytest.raises(PairingError) as e:
        pair_product(pdir, NONE, GcnoIndex(root), label="h/app")
    assert str(root / "b/main.cpp.gcno") in str(e.value)
    assert "stamp mismatch" in str(e.value)


def test_strip_collision_is_merged_on_device(tmp_path):
    pdir = tmp_path / "cov/h/app"
    _gcda(pdir, "main.cpp", 1)
    with pytest.raises(PairingError) as e:
        pair_product(
            pdir, _objects("/a/main.cpp", "/b/main.cpp"), GcnoIndex(tmp_path), label="h/app"
        )
    assert e.value.kind == "merged-on-device"
    assert "GCOV_PREFIX_STRIP" in str(e.value)


def test_unlisted_gcda_falls_back_to_tail_search(tmp_path, caplog):
    root = tmp_path / "proj"
    _gcno(root / "build/lib/util.c.gcno", 3)
    pdir = tmp_path / "cov/h/app"
    _gcda(pdir, "build/lib/util.c", 3)
    with caplog.at_level("INFO"):
        pairs = pair_product(pdir, _objects("/x/main.c"), GcnoIndex(root), label="h/app")
    assert pairs[0].gcno == root / "build/lib/util.c.gcno"
    assert any("not in the artifact's object list" in m for m in caplog.messages)


def test_identical_copies_take_one_and_name_the_others(tmp_path, caplog):
    root = tmp_path / "proj"
    _gcno(root / "t1/x/main.c.gcno", 4)
    _gcno(root / "t2/x/main.c.gcno", 4)
    pdir = tmp_path / "cov/h/app"
    _gcda(pdir, "x/main.c", 4)
    with caplog.at_level("DEBUG"):
        pairs = pair_product(pdir, NONE, GcnoIndex(root), label="h/app")
    assert len(pairs) == 1
    assert any("t1/x/main.c.gcno" in m and "t2/x/main.c.gcno" in m for m in caplog.messages)


def test_differing_trees_are_an_error_naming_every_candidate(tmp_path):
    root = tmp_path / "proj"
    _gcno(root / "t1/x/main.c.gcno", 4, b"\x01")
    _gcno(root / "t2/x/main.c.gcno", 4, b"\x02")
    _gcno(root / "t3/main.c.gcno", 4)  # shorter tail: ranked out, still listed
    _gcno(root / "t4/x/main.c.gcno", 8)  # incompatible: listed with its reason
    pdir = tmp_path / "cov/h/app"
    _gcda(pdir, "x/main.c", 4)
    with pytest.raises(PairingError) as e:
        pair_product(pdir, NONE, GcnoIndex(root), label="h/app")
    text = str(e.value)
    assert e.value.kind == "several-trees"
    for t in ("t1", "t2", "t3", "t4"):
        assert f"{t}/" in text
    assert "stamp mismatch" in text
    assert "shorter path match" in text


def test_cmake_stem(tmp_path):
    root = tmp_path / "proj"
    _gcno(root / "build/CMakeFiles/app.dir/src/add.cpp.gcno", 5)
    pdir = tmp_path / "cov/h/app"
    _gcda(pdir, "build/CMakeFiles/app.dir/src/add.cpp", 5)
    assert pair_product(pdir, NONE, GcnoIndex(root), label="h/app")[0].gcno.name == "add.cpp.gcno"


def test_kernel_tmp_notes_direct(tmp_path):
    root = tmp_path / "proj"
    _gcno(root / "mod/.tmp_foo.gcno", 6)
    pdir = tmp_path / "cov/h/kmod"
    _gcda(pdir, f"{root}/mod/foo".lstrip("/"), 6)
    index = GcnoIndex(root)
    pairs = pair_product(pdir, _objects(f"{root}/mod/foo"), index, label="h/kmod")
    assert pairs[0].gcno.name == ".tmp_foo.gcno"
    assert index.walks == 0


def test_kernel_tmp_notes_found_despite_unrelated_plain_name(tmp_path):
    root = tmp_path / "proj"
    _gcno(root / "other/foo.gcno", 99)  # incompatible plain-name notes elsewhere
    _gcno(root / "mod/.tmp_foo.gcno", 6)
    pdir = tmp_path / "cov/h/kmod"
    _gcda(pdir, "mod/foo", 6)
    pairs = pair_product(pdir, NONE, GcnoIndex(root), label="h/kmod")
    assert pairs[0].gcno == root / "mod/.tmp_foo.gcno"
