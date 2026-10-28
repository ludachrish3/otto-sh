"""``otto.vendoring``: the one copy/compare core under every shipped-sources export."""

from pathlib import Path

from otto.vendoring import compare_files, export_files


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.txt").write_text("alpha\n")
    (source / "run.sh").write_text("#!/bin/sh\n")
    (source / "run.sh").chmod(0o755)
    return source


def test_export_writes_every_name_and_reports_them_changed(tmp_path: Path):
    source = _source(tmp_path)
    changed = export_files(source, ["a.txt", "run.sh"], tmp_path / "dest")
    assert changed == ["a.txt", "run.sh"]
    assert (tmp_path / "dest" / "a.txt").read_text() == "alpha\n"
    assert (tmp_path / "dest" / "run.sh").stat().st_mode & 0o111


def test_a_second_export_changes_nothing(tmp_path: Path):
    source = _source(tmp_path)
    export_files(source, ["a.txt", "run.sh"], tmp_path)
    assert export_files(source, ["a.txt", "run.sh"], tmp_path) == []


def test_export_overwrites_an_edited_file_and_names_only_it(tmp_path: Path):
    source = _source(tmp_path)
    export_files(source, ["a.txt", "run.sh"], tmp_path)
    (tmp_path / "a.txt").write_text("edited\n")
    assert export_files(source, ["a.txt", "run.sh"], tmp_path) == ["a.txt"]
    assert (tmp_path / "a.txt").read_text() == "alpha\n"


def test_export_restores_a_lost_execute_bit(tmp_path: Path):
    source = _source(tmp_path)
    export_files(source, ["run.sh"], tmp_path)
    (tmp_path / "run.sh").chmod(0o644)
    assert export_files(source, ["run.sh"], tmp_path) == ["run.sh"]
    assert (tmp_path / "run.sh").stat().st_mode & 0o111


def test_compare_sorts_names_into_missing_differing_and_equal(tmp_path: Path):
    source = _source(tmp_path)
    (source / "b.txt").write_text("beta\n")
    export_files(source, ["a.txt", "b.txt"], tmp_path / "copy")
    (tmp_path / "copy" / "a.txt").write_text("edited\n")
    found = compare_files(source, ["a.txt", "b.txt", "run.sh"], tmp_path / "copy")
    assert found.differing == ["a.txt"]
    assert found.equal == ["b.txt"]
    assert found.missing == ["run.sh"]
    assert not found.matches


def test_compare_of_an_identical_copy_matches(tmp_path: Path):
    source = _source(tmp_path)
    export_files(source, ["a.txt"], tmp_path / "copy")
    assert compare_files(source, ["a.txt"], tmp_path / "copy").matches


def test_a_check_result_maps_each_state_to_the_one_exit_contract(tmp_path: Path):
    from otto.vendoring import check_result

    source = _source(tmp_path)
    export_files(source, ["a.txt"], tmp_path / "copy")
    current = compare_files(source, ["a.txt"], tmp_path / "copy")
    assert check_result(tmp_path, current, absent=False).exit_code == 0
    (tmp_path / "copy" / "a.txt").write_text("edited\n")
    differs = check_result(
        tmp_path, compare_files(source, ["a.txt"], tmp_path / "copy"), absent=False
    )
    assert (differs.state, differs.exit_code, differs.differing) == ("differs", 1, ["a.txt"])
    assert differs.differences == "differs: a.txt"
    absent = check_result(
        tmp_path, compare_files(source, ["a.txt"], tmp_path / "none"), absent=True
    )
    assert (absent.state, absent.exit_code) == ("absent", 2)


def test_both_verbs_share_the_one_check_result_type():
    from otto.coverage.embedded_gcov import library as embedded
    from otto.kmodcov import library as kmod
    from otto.vendoring import CheckResult

    assert embedded.check_patch(Path("/nonexistent-dir")).__class__ is CheckResult
    assert issubclass(kmod.CheckResult, CheckResult)
