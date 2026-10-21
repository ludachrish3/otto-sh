import logging
import shutil
import subprocess
from pathlib import Path, PurePosixPath

import pytest

from otto.coverage.merge.gcovfile import GcnoSources, gcno_sources
from otto.coverage.objects import ObjectList
from otto.coverage.pairing import GcnoIndex, notes_for_stem
from otto.coverage.sources import SourceResolver
from otto.coverage.unit_filter import (
    filter_info,
    gcno_named_sources,
    gcno_project_sources,
    include_patterns,
    product_source_set,
    select_unit_sources,
)
from tests.unit.cov._gcno import clang_gcno, gcc13_gcno


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    for rel in ["src/core/add.c", "src/core/add.h", "tests/test_add.cpp"]:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("x\n")
    return root


def test_synthetic_gcc13_lines_record_is_read(tmp_path):
    p = tmp_path / "x.gcno"
    p.write_bytes(gcc13_gcno("/w", ["/w/a.c", "/w/b.h", "/usr/include/c.h"]))
    got = gcno_sources(p)
    assert got == GcnoSources(cwd="/w", names=["/w/a.c", "/w/b.h", "/usr/include/c.h"])


def test_absolute_names_under_root_are_kept(tmp_path):
    root = _project(tmp_path)
    gcno = root / "build/add.c.gcno"
    gcno.parent.mkdir(parents=True)
    gcno.write_bytes(
        gcc13_gcno(
            str(root / "build"),
            [str(root / "src/core/add.c"), str(root / "src/core/add.h"), "/usr/include/stdio.h"],
        )
    )
    got = gcno_project_sources(gcno, root, SourceResolver(root))
    assert got == [Path("src/core/add.c"), Path("src/core/add.h")]


def test_relative_gcc_name_resolves_against_cwd(tmp_path):
    # tail matching alone would pick the longer a/src/x.c or b/src/x.c ambiguously; the cwd decides
    root = tmp_path / "proj"
    for rel in ["a/src/x.c", "b/src/x.c"]:
        (root / rel).parent.mkdir(parents=True)
        (root / rel).write_text("x\n")
    gcno = root / "build/x.c.gcno"
    gcno.parent.mkdir()
    gcno.write_bytes(gcc13_gcno(str(root / "b"), ["src/x.c"]))
    assert gcno_project_sources(gcno, root, SourceResolver(root)) == [Path("b/src/x.c")]


def test_bare_clang_name_resolves_against_the_notes_directory(tmp_path):
    root = _project(tmp_path)
    gcno = root / "src/core/add.c.gcno"
    gcno.write_bytes(clang_gcno("add.c", ["add.h"]))
    got = gcno_named_sources(gcno, root, SourceResolver(root))
    assert got.project == [Path("src/core/add.c"), Path("src/core/add.h")]
    assert got.external == []


def test_external_names_are_kept_as_recorded(tmp_path):
    root = _project(tmp_path)
    gcno = root / "build/x.gcno"
    gcno.parent.mkdir()
    gcno.write_bytes(gcc13_gcno("/nowhere", ["gone/x.c", "/usr/include/stdio.h"]))
    got = gcno_named_sources(gcno, root, SourceResolver(root))
    assert got.external == ["gone/x.c", "/usr/include/stdio.h"]


@pytest.mark.skipif(shutil.which("clang") is None, reason="clang not installed")
def test_real_clang_bare_name_in_source_dir(tmp_path):
    root = _project(tmp_path)
    core = root / "src/core"
    (core / "add.c").write_text("int add(int a){return a+1;}\n")
    subprocess.run(["clang", "--coverage", "-c", "add.c", "-o", "add.c.o"], cwd=core, check=True)
    got = gcno_project_sources(core / "add.c.gcno", root, SourceResolver(root))
    assert got == [Path("src/core/add.c")]


def test_relative_clang_name_tail_matches(tmp_path):
    root = _project(tmp_path)
    gcno = root / "build/add.c.gcno"
    gcno.parent.mkdir(parents=True)
    gcno.write_bytes(clang_gcno("../src/core/add.c", ["../src/core/add.h"]))
    got = gcno_project_sources(gcno, root, SourceResolver(root))
    assert set(got) == {Path("src/core/add.c"), Path("src/core/add.h")}


@pytest.mark.skipif(shutil.which("clang") is None, reason="clang not installed")
def test_real_clang_nested_cwd_names_resolve(tmp_path):
    # compile from build/, so clang records ../src/... names relative to it
    root = _project(tmp_path)
    (root / "src/core/add.c").write_text('#include "add.h"\nint add(int a){return a+1;}\n')
    (root / "src/core/add.h").write_text("int add(int);\n")
    build = root / "build"
    build.mkdir()
    subprocess.run(
        ["clang", "--coverage", "-I../src/core", "-c", "../src/core/add.c", "-o", "add.c.o"],
        cwd=build,
        check=True,
    )
    got = gcno_project_sources(build / "add.c.gcno", root, SourceResolver(root))
    assert Path("src/core/add.c") in got


def test_relative_stem_anchors_at_the_project_root(tmp_path, monkeypatch):
    root = _project(tmp_path)
    notes = root / "main.gcno"
    notes.write_bytes(b"")
    monkeypatch.chdir(tmp_path)  # report run from elsewhere
    assert notes_for_stem(PurePosixPath("main"), GcnoIndex(root)) == notes


def test_unreadable_notes_contribute_nothing(tmp_path):
    root = _project(tmp_path)
    gcno = root / "build/x.gcno"
    gcno.parent.mkdir(parents=True)
    gcno.write_bytes(b"junk")
    assert gcno_project_sources(gcno, root, SourceResolver(root)) == []


def test_stem_notes_direct_then_tail_search(tmp_path):
    root = _project(tmp_path)
    direct = root / "build/src/core/add.c.gcno"
    direct.parent.mkdir(parents=True)
    direct.write_bytes(b"")
    index = GcnoIndex(root)
    assert notes_for_stem(PurePosixPath(str(root / "build/src/core/add.c")), index) == direct
    # a container path: /build/src/core/add.c → the local notes sharing the longest tail
    assert notes_for_stem(PurePosixPath("/build/src/core/add.c"), index) == direct
    assert notes_for_stem(PurePosixPath("/build/src/other.c"), index) is None


def test_product_source_set_unions_objects(tmp_path):
    root = _project(tmp_path)
    a = root / "build/src/core/add.c.gcno"
    a.parent.mkdir(parents=True)
    a.write_bytes(
        gcc13_gcno(str(root), [str(root / "src/core/add.c"), str(root / "src/core/add.h")])
    )
    objects = ObjectList(
        product="core", paths=[PurePosixPath(str(root / "build/src/core/add.c"))], source="art"
    )
    got = product_source_set(objects, root, SourceResolver(root), GcnoIndex(root))
    assert got == {Path("src/core/add.c"), Path("src/core/add.h")}


def test_stem_tie_identical_notes_takes_one(tmp_path):
    root = _project(tmp_path)
    blob = gcc13_gcno(str(root), ["src/core/add.c"])
    for d in ["b1/src/core", "b2/src/core"]:
        (root / d).mkdir(parents=True)
        (root / d / "add.c.gcno").write_bytes(blob)
    got = notes_for_stem(PurePosixPath("/x/src/core/add.c"), GcnoIndex(root))
    assert got is not None
    assert got.name == "add.c.gcno"


def test_stem_tie_differing_notes_is_none(tmp_path):
    root = _project(tmp_path)
    for i, d in enumerate(["b1/src/core", "b2/src/core"]):
        (root / d).mkdir(parents=True)
        (root / d / "add.c.gcno").write_bytes(gcc13_gcno(str(root), [f"src/core/a{i}.c"]))
    assert notes_for_stem(PurePosixPath("/x/src/core/add.c"), GcnoIndex(root)) is None


def test_stem_one_component_tail_never_pairs(tmp_path):
    root = _project(tmp_path)
    (root / "build").mkdir()
    (root / "build/main.gcno").write_bytes(b"")
    assert notes_for_stem(PurePosixPath("/elsewhere/main"), GcnoIndex(root)) is None


def test_select_splits_kept_and_dropped(tmp_path):
    root = _project(tmp_path)
    hdir = root / "build/tests"
    hdir.mkdir(parents=True)
    (hdir / "add.c.gcno").write_bytes(gcc13_gcno(str(root), [str(root / "src/core/add.c")]))
    (hdir / "test_add.cpp.gcno").write_bytes(
        gcc13_gcno(str(root), [str(root / "tests/test_add.cpp"), "/usr/include/gtest/gtest.h"])
    )
    sel = select_unit_sources(hdir, root, SourceResolver(root), {Path("src/core/add.c"): "core"})
    assert sel.kept == {Path("src/core/add.c"): "core"}
    assert sel.dropped == ["tests/test_add.cpp", "/usr/include/gtest/gtest.h"]
    # only the notes naming a kept source prove the compiler: a test-only object does not
    assert sel.notes == [hdir / "add.c.gcno"]


def test_metacharacters_become_question_marks():
    assert include_patterns([Path("src/a[b].c"), Path("src/x*?.c")]) == [
        "*/src/a?b?.c",
        "*/src/x??.c",
    ]


def test_exact_filter_drops_glob_false_positive(tmp_path):
    root = _project(tmp_path)
    (root / "tests/src/core").mkdir(parents=True)
    (root / "tests/src/core/add.c").write_text("x\n")
    info = tmp_path / "in.info"
    info.write_text(
        f"TN:\nSF:{root}/src/core/add.c\nDA:1,1\nend_of_record\n"
        f"TN:\nSF:{root}/tests/src/core/add.c\nDA:1,1\nend_of_record\n"
    )
    out = tmp_path / "out.info"
    filter_info(info, out, {Path("src/core/add.c")}, root, SourceResolver(root))
    text = out.read_text()
    assert f"SF:{root}/src/core/add.c" in text
    assert "tests/src" not in text
    assert text.count("TN:") == 1


def test_filter_drops_unresolvable_and_outside_root_records(tmp_path):
    root = _project(tmp_path)
    outside = tmp_path / "elsewhere.c"
    outside.write_text("x\n")
    info = tmp_path / "in.info"
    info.write_text(
        f"TN:\nSF:{root}/src/core/add.c\nDA:1,1\nend_of_record\n"
        "TN:\nSF:/nowhere/zz_missing.c\nDA:1,1\nend_of_record\n"
        f"TN:\nSF:{outside}\nDA:1,1\nend_of_record\n"
    )
    out = tmp_path / "out.info"
    filter_info(info, out, {Path("src/core/add.c")}, root, SourceResolver(root))
    text = out.read_text()
    assert text.count("end_of_record") == 1
    assert "zz_missing" not in text
    assert "elsewhere" not in text


def test_quiet_resolver_logs_an_unresolved_name_at_debug(tmp_path, caplog):
    root = _project(tmp_path)
    (root / "src/config.h").write_text("x\n")
    with caplog.at_level("DEBUG", logger="otto.coverage.sources"):
        assert SourceResolver(root, quiet=True).resolve("/opt/sdk/config.h") is None
    (record,) = [r for r in caplog.records if "config.h" in r.getMessage()]
    assert record.levelno == logging.DEBUG


def test_loud_resolver_still_warns_on_a_same_named_candidate(tmp_path, caplog):
    # preservation: the e2e captures' resolver is unchanged
    root = _project(tmp_path)
    (root / "src/config.h").write_text("x\n")
    with caplog.at_level("DEBUG", logger="otto.coverage.sources"):
        assert SourceResolver(root).resolve("/opt/sdk/config.h") is None
    (record,) = [r for r in caplog.records if "config.h" in r.getMessage()]
    assert record.levelno == logging.WARNING


def test_objects_without_local_notes_are_summarised_at_info(tmp_path, caplog):
    root = _project(tmp_path)
    (root / "build").mkdir()
    (root / "build/add.c.gcno").write_bytes(gcc13_gcno(str(root), [str(root / "src/core/add.c")]))
    objects = ObjectList(
        product="core",
        paths=[PurePosixPath(f"{root}/build/add.c")]
        + [PurePosixPath(f"/elsewhere/gone{i}.c") for i in range(5)],
        source="core.bin",
    )
    with caplog.at_level("INFO", logger="otto.coverage.unit_filter"):
        got = product_source_set(objects, root, SourceResolver(root), GcnoIndex(root))
    assert got == {Path("src/core/add.c")}
    (record,) = [r for r in caplog.records if r.levelno == logging.INFO]
    assert record.getMessage() == (
        "unit: product core: 5 of 6 objects have no local notes, so their sources are "
        "not in its source set: /elsewhere/gone0.c, /elsewhere/gone1.c, "
        "/elsewhere/gone2.c, ..."
    )


def test_select_dedups_across_notes_files_in_a_nested_dir(tmp_path):
    root = _project(tmp_path)
    hdir = root / "build/tests"
    owned = [str(root / "src/core/add.c"), str(root / "src/core/add.h")]
    test_file = [str(root / "tests/test_add.cpp"), "/usr/include/gtest/gtest.h"]
    for sub, names in [("a", owned + test_file), ("b/deep/er", owned + test_file)]:
        (hdir / sub).mkdir(parents=True)
        (hdir / sub / "x.gcno").write_bytes(gcc13_gcno(str(root), names))
    owners = {Path("src/core/add.c"): "core", Path("src/core/add.h"): "core"}
    sel = select_unit_sources(hdir, root, SourceResolver(root), owners)
    assert sel.kept == owners
    assert sel.dropped == ["tests/test_add.cpp", "/usr/include/gtest/gtest.h"]


def test_filter_passes_function_and_branch_records_through_crlf(tmp_path):
    root = _project(tmp_path)
    body = [
        "TN:",
        f"SF:{root}/src/core/add.c",
        "FN:1,add",
        "FNDA:3,add",
        "FNF:1",
        "FNH:1",
        "BRDA:2,0,0,1",
        "BRDA:2,0,1,-",
        "DA:1,3",
        "end_of_record",
    ]
    info = tmp_path / "in.info"
    info.write_bytes(("\r\n".join(body) + "\r\n").encode())
    out = tmp_path / "out.info"
    filter_info(info, out, {Path("src/core/add.c")}, root, SourceResolver(root))
    assert out.read_text().splitlines() == body


def test_filter_accepts_an_end_of_record_with_trailing_blanks(tmp_path):
    root = _project(tmp_path)
    info = tmp_path / "in.info"
    info.write_text(f"TN:\nSF:{root}/src/core/add.c\nDA:1,1\nend_of_record \t\n")
    out = tmp_path / "out.info"
    filter_info(info, out, {Path("src/core/add.c")}, root, SourceResolver(root))
    assert f"SF:{root}/src/core/add.c" in out.read_text()


def test_filter_drops_a_truncated_final_record_and_warns(tmp_path, caplog):
    root = _project(tmp_path)
    info = tmp_path / "in.info"
    info.write_text(
        f"TN:\nSF:{root}/src/core/add.c\nDA:1,1\nend_of_record\n"
        f"TN:\nSF:{root}/src/core/add.h\nDA:1,1\n"
    )
    out = tmp_path / "out.info"
    keep = {Path("src/core/add.c"), Path("src/core/add.h")}
    with caplog.at_level("DEBUG", logger="otto.coverage.unit_filter"):
        filter_info(info, out, keep, root, SourceResolver(root))
    text = out.read_text()
    assert text.count("end_of_record") == 1
    assert "add.h" not in text
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert str(info) in warnings[0].getMessage()
