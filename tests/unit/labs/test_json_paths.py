"""json backend path forms: a search path is a directory, a .json file, or a glob."""

import json
from pathlib import Path

import pytest

from otto.labs import JsonFileLabRepository, LabNotFoundError, LabRepositoryError
from otto.labs.json_repository import expand_lab_paths
from tests._fixtures.labdata import write_lab_json

HOST = {
    "ip": "10.0.0.1",
    "element": "alt1",
    "creds": [{"login": "u", "password": "p"}],
    "resources": ["alt1"],
    "labs": ["veg"],
}


def test_directory_entry_still_finds_lab_json(tmp_path: Path) -> None:
    write_lab_json(tmp_path / "lab" / "lab.json", [HOST])
    repo = JsonFileLabRepository(search_paths=[tmp_path / "lab"])
    assert "alt1" in repo.load_lab("veg").hosts


def test_json_file_entry_is_used_directly(tmp_path: Path) -> None:
    f = write_lab_json(tmp_path / "global-hosts.json", [HOST])
    repo = JsonFileLabRepository(search_paths=[f])
    assert "alt1" in repo.load_lab("veg").hosts


def test_mixed_file_and_directory_entries_merge(tmp_path: Path) -> None:
    write_lab_json(tmp_path / "lab" / "lab.json", [HOST])
    other = dict(HOST, ip="10.0.0.2", element="test2", resources=["test2"])
    # The second file only CONTRIBUTES members: within one source a lab
    # declared in two files is an error (spec §2.4).
    f = write_lab_json(tmp_path / "extra.json", [other], declare_labs=False)
    repo = JsonFileLabRepository(search_paths=[tmp_path / "lab", f])
    assert set(repo.load_lab("veg").hosts) == {"alt1", "test2"}


def test_missing_json_file_entry_is_skipped_like_missing_dir(tmp_path: Path) -> None:
    with pytest.raises(LabNotFoundError):
        JsonFileLabRepository(search_paths=[tmp_path / "absent.json"]).load_lab("veg")


def test_glob_entry_expands_sorted_json_files_only(tmp_path: Path) -> None:
    d = tmp_path / "elements"
    d.mkdir()
    write_lab_json(d / "b.json", [dict(HOST, element="b", ip="10.0.0.2")], declare_labs=False)
    write_lab_json(d / "a.json", [dict(HOST, element="a", ip="10.0.0.1")], declare_labs=False)
    (d / "notes.md").write_text("not a lab file")
    (tmp_path / "labs.json").write_text(json.dumps({"labs": {"veg": {"resources": ["bed"]}}}))
    repo = JsonFileLabRepository([tmp_path / "labs.json", tmp_path / "elements" / "*.json"])
    lab = repo.load_lab("veg")
    assert set(lab.hosts) == {"a", "b"}
    assert lab.resources == {"bed"}  # declared in a labs-only file, members elsewhere


def test_recursive_glob(tmp_path: Path) -> None:
    (tmp_path / "sites" / "east").mkdir(parents=True)
    write_lab_json(tmp_path / "sites" / "east" / "e.json", [HOST], declare_labs=False)
    (tmp_path / "labs.json").write_text(json.dumps({"labs": {"veg": {}}}))
    repo = JsonFileLabRepository([tmp_path / "labs.json", tmp_path / "sites" / "**" / "*.json"])
    assert set(repo.load_lab("veg").hosts) == {"alt1"}


def test_glob_matching_nothing_contributes_nothing(tmp_path: Path) -> None:
    (tmp_path / "labs.json").write_text(
        json.dumps(
            {
                "labs": {"veg": {}},
                "elements": [
                    {
                        "name": "x",
                        "labs": ["veg"],
                        "hosts": [{"ip": "10.0.0.9", "creds": HOST["creds"]}],
                    }
                ],
            }
        )
    )
    repo = JsonFileLabRepository([tmp_path / "labs.json", tmp_path / "nothing" / "*.json"])
    assert set(repo.load_lab("veg").hosts) == {"x"}


def test_overlapping_path_entries_read_a_file_once(tmp_path: Path) -> None:
    """A directory plus a glob over it is the documented layout, not a duplicate.

    ``paths = [d, d/"*.json"]`` — the main file by directory, the split files by
    glob — named ``d/lab.json`` twice, and the in-source duplicate rule then
    reported the file as colliding with ITSELF.
    """
    d = tmp_path / "lab"
    write_lab_json(d / "lab.json", [HOST])
    repo = JsonFileLabRepository([d, d / "*.json"])
    assert set(repo.load_lab("veg").hosts) == {"alt1"}


def test_expand_lab_paths_dedupes_keeping_first_seen_order(tmp_path: Path) -> None:
    """De-dup is by RESOLVED path and preserves the first occurrence's position."""
    d = tmp_path / "lab"
    write_lab_json(d / "lab.json", [HOST])
    write_lab_json(d / "extra.json", [dict(HOST, element="b", ip="10.0.0.2")], declare_labs=False)

    # The third entry spells the SAME file a different way, so a textual
    # de-dup would keep it and a resolved one drops it.
    found = expand_lab_paths([d, d / "*.json", d.parent / "lab" / ".." / "lab" / "lab.json"])

    assert found == [d / "lab.json", d / "extra.json"]


def test_duplicate_element_across_files_of_one_source_errors(tmp_path: Path) -> None:
    write_lab_json(tmp_path / "one.json", [HOST], declare_labs=False)
    write_lab_json(tmp_path / "two.json", [dict(HOST, ip="10.0.0.9")], declare_labs=False)
    repo = JsonFileLabRepository([tmp_path / "*.json"])
    with pytest.raises(LabRepositoryError, match=r"element alt1 in .*one\.json.*two\.json"):
        repo.load_lab("veg")


def test_duplicate_lab_declaration_across_files_of_one_source_errors(tmp_path: Path) -> None:
    (tmp_path / "one.json").write_text(json.dumps({"labs": {"veg": {}}}))
    (tmp_path / "two.json").write_text(json.dumps({"labs": {"veg": {"resources": ["x"]}}}))
    with pytest.raises(LabRepositoryError, match=r"'veg'.*one\.json.*two\.json"):
        JsonFileLabRepository([tmp_path / "*.json"]).load_lab("veg")
