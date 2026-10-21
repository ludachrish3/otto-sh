import logging
from pathlib import Path

from otto.coverage.sources import SourceResolver, write_resolved_info


def _tree(root: Path, *files: str) -> None:
    for f in files:
        p = root / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x\n")


def test_existing_path_is_used_without_any_log(tmp_path, caplog):
    _tree(tmp_path, "src/a.c")
    with caplog.at_level("DEBUG"):
        assert SourceResolver(tmp_path).resolve(str(tmp_path / "src/a.c")) == tmp_path / "src/a.c"
    assert caplog.messages == []


def test_relocated_absolute_path_tail_matches(tmp_path):
    _tree(tmp_path, "src/core/a.c", "other/a.c")
    assert SourceResolver(tmp_path).resolve("/build/src/core/a.c") == tmp_path / "src/core/a.c"


def test_staging_resolved_clang_path(tmp_path):
    _tree(tmp_path / "proj", "src/lib/add.c")
    stage = tmp_path / "stage"
    r = SourceResolver(tmp_path / "proj", staging=stage)
    assert r.resolve(f"{stage}/b/x.dir/../../src/lib/add.c") == tmp_path / "proj/src/lib/add.c"


def test_ambiguous_is_one_warning_and_raw_kept(tmp_path, caplog):
    _tree(tmp_path, "a/util.c", "b/util.c")
    r = SourceResolver(tmp_path)
    with caplog.at_level("WARNING"):
        assert r.resolve("/elsewhere/util.c") is None
        assert r.resolve("/elsewhere/util.c") is None
    assert len([m for m in caplog.messages if "util.c" in m]) == 1


def test_write_resolved_info_rewrites_only_sf_lines(tmp_path):
    _tree(tmp_path, "src/a.c")
    raw = tmp_path / "raw.info"
    raw.write_text("TN:\nSF:/build/src/a.c\nDA:1,1\nend_of_record\n")
    out = tmp_path / "out.info"
    write_resolved_info(raw, out, SourceResolver(tmp_path))
    assert out.read_text() == f"TN:\nSF:{tmp_path / 'src/a.c'}\nDA:1,1\nend_of_record\n"


def test_name_only_match_is_not_enough(tmp_path, caplog):
    _tree(tmp_path, "vendor/list.h")
    with caplog.at_level("WARNING"):
        assert SourceResolver(tmp_path).resolve("/opt/sdk/include/list.h") is None
    assert len([m for m in caplog.messages if "list.h" in m]) == 1


def test_root_level_file_resolves(tmp_path):
    _tree(tmp_path, "a.c")
    assert SourceResolver(tmp_path).resolve("/build/a.c") == tmp_path / "a.c"


def test_no_candidates_is_info_not_a_warning(tmp_path, caplog):
    """A path no project file is named like (a relocated build's system header)
    is ordinary — and there is one per header — so it is INFO, not a WARNING."""
    _tree(tmp_path, "src/a.c")
    with caplog.at_level("INFO", logger="otto.coverage.sources"):
        assert SourceResolver(tmp_path).resolve("/build/missing.c") is None
    (record,) = [r for r in caplog.records if "missing.c" in r.getMessage()]
    assert record.levelno == logging.INFO
    assert "candidates: none" in record.getMessage()


def test_candidates_that_do_not_match_stay_a_warning(tmp_path, caplog):
    """Project files named like the path that do not end with it: possibly the
    file, so the user is warned."""
    _tree(tmp_path, "a/util.c", "b/util.c")
    with caplog.at_level("INFO", logger="otto.coverage.sources"):
        assert SourceResolver(tmp_path).resolve("/elsewhere/util.c") is None
    (record,) = [r for r in caplog.records if "util.c" in r.getMessage()]
    assert record.levelno == logging.WARNING


def test_relative_existing_path_is_not_taken_as_is(tmp_path, monkeypatch):
    _tree(tmp_path, "rel/b.c")
    monkeypatch.chdir(tmp_path)
    empty = tmp_path / "proj"
    empty.mkdir()
    assert SourceResolver(empty).resolve("rel/b.c") is None


def test_longest_suffix_match_wins_over_a_shorter_one(tmp_path, caplog):
    _tree(tmp_path, "a.c", "src/a.c")
    with caplog.at_level("WARNING"):
        assert SourceResolver(tmp_path).resolve("/build/src/a.c") == tmp_path / "src/a.c"
    assert caplog.messages == []
