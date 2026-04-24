import json
from pathlib import Path

import pytest

from otto.configmodule.lab import Lab
from otto.storage.json_repository import JsonFileLabRepository


def _hosts_file(path: Path, hosts: list[dict]) -> Path:
    """Write a hosts.json file to the given directory and return its path."""
    f = path / "hosts.json"
    f.write_text(json.dumps(hosts))
    return f


class TestJsonFileLabRepository:
    """Tests for JsonFileLabRepository class."""

    def test_supports_location_directory(self, tmp_path):
        """Test that supports_location returns True for directories."""
        repo = JsonFileLabRepository()
        assert repo.supports_location(tmp_path) is True

    def test_supports_location_file(self, tmp_path):
        """Test that supports_location returns False for files."""
        repo = JsonFileLabRepository()
        test_file = tmp_path / "test.txt"
        test_file.write_text("test")
        assert repo.supports_location(test_file) is False

    def test_load_lab_simple(self, tmp_path):
        """Test loading a simple lab with one host."""
        _hosts_file(tmp_path, [
            {
                "ip": "10.10.200.11",
                "ne": "orange",
                "board": "seed",
                "creds": {"vagrant": "vagrant"},
                "resources": ["orange"],
                "labs": ["testlab"],
            },
        ])

        repo = JsonFileLabRepository()
        lab = repo.load_lab("testlab", [tmp_path])

        assert isinstance(lab, Lab)
        assert lab.name == "testlab"
        assert len(lab.hosts) == 1
        assert "orange" in lab.resources

    def test_load_lab_multiple_hosts(self, tmp_path):
        """Test loading a lab with multiple hosts."""
        _hosts_file(tmp_path, [
            {
                "ip": "10.10.200.11",
                "ne": "orange",
                "board": "seed",
                "creds": {"vagrant": "vagrant"},
                "resources": ["orange"],
                "labs": ["multilab"],
            },
            {
                "ip": "10.10.200.12",
                "ne": "tomato",
                "board": "seed",
                "creds": {"vagrant": "vagrant"},
                "resources": ["tomato"],
                "labs": ["multilab"],
            },
        ])

        repo = JsonFileLabRepository()
        lab = repo.load_lab("multilab", [tmp_path])

        assert isinstance(lab, Lab)
        assert lab.name == "multilab"
        assert len(lab.hosts) == 2
        assert "orange" in lab.resources
        assert "tomato" in lab.resources

    def test_load_lab_not_found_no_hosts_file(self, tmp_path):
        """Test that FileNotFoundError is raised when no hosts.json exists."""
        repo = JsonFileLabRepository()

        with pytest.raises(FileNotFoundError) as exc_info:
            repo.load_lab("nonexistent", [tmp_path])

        assert str(tmp_path) in str(exc_info.value)

    def test_load_lab_not_found_lab_absent(self, tmp_path):
        """Test that FileNotFoundError is raised when hosts.json exists but lab name is not present."""
        _hosts_file(tmp_path, [
            {
                "ip": "10.10.200.11",
                "ne": "orange",
                "creds": {"vagrant": "vagrant"},
                "resources": ["orange"],
                "labs": ["other_lab"],
            },
        ])

        repo = JsonFileLabRepository()

        with pytest.raises(FileNotFoundError) as exc_info:
            repo.load_lab("nonexistent", [tmp_path])

        assert "nonexistent" in str(exc_info.value)

    def test_load_lab_only_returns_matching_hosts(self, tmp_path):
        """Test that only hosts belonging to the requested lab are returned."""
        _hosts_file(tmp_path, [
            {
                "ip": "10.10.200.11",
                "ne": "orange",
                "creds": {"vagrant": "vagrant"},
                "resources": ["orange"],
                "labs": ["lab_a"],
            },
            {
                "ip": "10.10.200.12",
                "ne": "tomato",
                "creds": {"vagrant": "vagrant"},
                "resources": ["tomato"],
                "labs": ["lab_b"],
            },
        ])

        repo = JsonFileLabRepository()
        lab = repo.load_lab("lab_a", [tmp_path])

        assert len(lab.hosts) == 1
        assert "orange" in lab.hosts

    def test_load_lab_multiple_search_paths(self, tmp_path):
        """Test loading lab from multiple search paths."""
        path1 = tmp_path / "path1"
        path2 = tmp_path / "path2"
        path1.mkdir()
        path2.mkdir()

        _hosts_file(path2, [
            {
                "ip": "10.10.200.11",
                "ne": "orange",
                "creds": {"vagrant": "vagrant"},
                "resources": ["orange"],
                "labs": ["testlab"],
            },
        ])

        repo = JsonFileLabRepository()
        lab = repo.load_lab("testlab", [path1, path2])

        assert isinstance(lab, Lab)
        assert lab.name == "testlab"

    def test_load_lab_not_a_list(self, tmp_path):
        """Test that ValueError is raised when JSON root is not an array."""
        (tmp_path / "hosts.json").write_text(json.dumps({"hosts": []}))

        repo = JsonFileLabRepository()

        with pytest.raises(ValueError) as exc_info:
            repo.load_lab("badlab", [tmp_path])

        assert "array" in str(exc_info.value)

    def test_load_lab_invalid_json(self, tmp_path):
        """Test that json.JSONDecodeError is raised for malformed JSON."""
        (tmp_path / "hosts.json").write_text("[{invalid json")

        repo = JsonFileLabRepository()

        with pytest.raises(Exception):  # json.JSONDecodeError
            repo.load_lab("badlab", [tmp_path])

    def test_load_lab_invalid_host_data(self, tmp_path):
        """Test that ValueError is raised for invalid host data."""
        _hosts_file(tmp_path, [
            {
                "ne": "orange",
                "creds": {"vagrant": "vagrant"},
                "labs": ["badlab"],
            },
        ])

        repo = JsonFileLabRepository()

        with pytest.raises(ValueError) as exc_info:
            repo.load_lab("badlab", [tmp_path])

        assert "index 0" in str(exc_info.value)
        assert "ip" in str(exc_info.value)

    def test_load_lab_resource_aggregation(self, tmp_path):
        """Test that resources from hosts are aggregated to lab level."""
        _hosts_file(tmp_path, [
            {
                "ip": "10.10.200.11",
                "ne": "orange",
                "creds": {"vagrant": "vagrant"},
                "resources": ["orange", "citrus"],
                "labs": ["resourcelab"],
            },
            {
                "ip": "10.10.200.12",
                "ne": "tomato",
                "creds": {"vagrant": "vagrant"},
                "resources": ["tomato", "vegetable"],
                "labs": ["resourcelab"],
            },
        ])

        repo = JsonFileLabRepository()
        lab = repo.load_lab("resourcelab", [tmp_path])

        assert "orange" in lab.resources
        assert "citrus" in lab.resources
        assert "tomato" in lab.resources
        assert "vegetable" in lab.resources

    def test_load_lab_host_ids_generated(self, tmp_path):
        """Test that host IDs are properly generated."""
        _hosts_file(tmp_path, [
            {
                "ip": "10.10.200.11",
                "ne": "orange",
                "board": "seed",
                "slot": 0,
                "creds": {"vagrant": "vagrant"},
                "resources": ["orange"],
                "labs": ["idlab"],
            },
        ])

        repo = JsonFileLabRepository()
        lab = repo.load_lab("idlab", [tmp_path])

        assert "orange_seed0" in lab.hosts

    def test_list_labs(self, tmp_path):
        """Test that list_labs returns all unique lab names from hosts.json."""
        _hosts_file(tmp_path, [
            {
                "ip": "10.10.200.11",
                "ne": "orange",
                "creds": {"vagrant": "vagrant"},
                "resources": ["orange"],
                "labs": ["alpha"],
            },
            {
                "ip": "10.10.200.12",
                "ne": "tomato",
                "creds": {"vagrant": "vagrant"},
                "resources": ["tomato"],
                "labs": ["beta"],
            },
        ])

        repo = JsonFileLabRepository()
        labs = repo.list_labs([tmp_path])

        assert labs == ["alpha", "beta"]

    def test_list_labs_multiple_search_paths(self, tmp_path):
        """Test list_labs aggregates across multiple search paths."""
        path1 = tmp_path / "p1"
        path2 = tmp_path / "p2"
        path1.mkdir()
        path2.mkdir()

        _hosts_file(path1, [
            {
                "ip": "10.10.200.11",
                "ne": "orange",
                "creds": {"vagrant": "vagrant"},
                "resources": [],
                "labs": ["alpha"],
            },
        ])
        _hosts_file(path2, [
            {
                "ip": "10.10.200.12",
                "ne": "tomato",
                "creds": {"vagrant": "vagrant"},
                "resources": [],
                "labs": ["beta"],
            },
        ])

        repo = JsonFileLabRepository()
        labs = repo.list_labs([path1, path2])

        assert labs == ["alpha", "beta"]

    def test_list_labs_no_hosts_file(self, tmp_path):
        """Test that list_labs returns empty list when no hosts.json exists."""
        repo = JsonFileLabRepository()
        labs = repo.list_labs([tmp_path])
        assert labs == []
