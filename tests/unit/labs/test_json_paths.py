"""json backend path forms: a search path is a directory OR a .json file."""

import json
from pathlib import Path

import pytest

from otto.labs import JsonFileLabRepository, LabNotFoundError

HOST = {
    "ip": "10.0.0.1",
    "element": "alt1",
    "creds": [{"login": "u", "password": "p"}],
    "resources": ["alt1"],
    "labs": ["veg"],
}


def _write(path: Path, hosts: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"hosts": hosts}))
    return path


def test_directory_entry_still_finds_lab_json(tmp_path: Path) -> None:
    _write(tmp_path / "lab" / "lab.json", [HOST])
    repo = JsonFileLabRepository(search_paths=[tmp_path / "lab"])
    assert "alt1" in repo.load_lab("veg").hosts


def test_json_file_entry_is_used_directly(tmp_path: Path) -> None:
    f = _write(tmp_path / "global-hosts.json", [HOST])
    repo = JsonFileLabRepository(search_paths=[f])
    assert "alt1" in repo.load_lab("veg").hosts


def test_mixed_file_and_directory_entries_merge(tmp_path: Path) -> None:
    _write(tmp_path / "lab" / "lab.json", [HOST])
    other = dict(HOST, ip="10.0.0.2", element="test2", resources=["test2"])
    f = _write(tmp_path / "extra.json", [other])
    repo = JsonFileLabRepository(search_paths=[tmp_path / "lab", f])
    assert set(repo.load_lab("veg").hosts) == {"alt1", "test2"}


def test_missing_json_file_entry_is_skipped_like_missing_dir(tmp_path: Path) -> None:
    with pytest.raises(LabNotFoundError):
        JsonFileLabRepository(search_paths=[tmp_path / "absent.json"]).load_lab("veg")
