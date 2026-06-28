import json
from pathlib import Path

import pytest

from otto.configmodule.lab import Lab
from otto.storage import LabNotFoundError, LabRepositoryError
from otto.storage.json_repository import JsonFileLabRepository


def _hosts_file(path: Path, hosts: list[dict]) -> Path:
    """Write a hosts.json file to the given directory and return its path."""
    f = path / "hosts.json"
    f.write_text(json.dumps(hosts))
    return f


class TestJsonFileLabRepository:
    """Tests for JsonFileLabRepository (construct-time search paths)."""

    def test_load_lab_simple(self, tmp_path):
        _hosts_file(
            tmp_path,
            [
                {
                    "ip": "10.10.200.11",
                    "element": "orange",
                    "board": "seed",
                    "creds": {"vagrant": "vagrant"},
                    "resources": ["orange"],
                    "labs": ["testlab"],
                },
            ],
        )

        repo = JsonFileLabRepository([tmp_path])
        lab = repo.load_lab("testlab")

        assert isinstance(lab, Lab)
        assert lab.name == "testlab"
        assert len(lab.hosts) == 1
        assert "orange" in lab.resources

    def test_load_lab_multiple_hosts(self, tmp_path):
        _hosts_file(
            tmp_path,
            [
                {
                    "ip": "10.10.200.11",
                    "element": "orange",
                    "board": "seed",
                    "creds": {"vagrant": "vagrant"},
                    "resources": ["orange"],
                    "labs": ["multilab"],
                },
                {
                    "ip": "10.10.200.12",
                    "element": "tomato",
                    "board": "seed",
                    "creds": {"vagrant": "vagrant"},
                    "resources": ["tomato"],
                    "labs": ["multilab"],
                },
            ],
        )

        repo = JsonFileLabRepository([tmp_path])
        lab = repo.load_lab("multilab")

        assert isinstance(lab, Lab)
        assert lab.name == "multilab"
        assert len(lab.hosts) == 2
        assert "orange" in lab.resources
        assert "tomato" in lab.resources

    def test_load_lab_not_found_no_hosts_file(self, tmp_path):
        """A missing hosts.json raises LabNotFoundError, not FileNotFoundError."""
        repo = JsonFileLabRepository([tmp_path])

        with pytest.raises(LabNotFoundError) as exc_info:
            repo.load_lab("nonexistent")

        assert str(tmp_path) in str(exc_info.value)

    def test_load_lab_not_found_lab_absent(self, tmp_path):
        """hosts.json exists but the lab name is not present -> LabNotFoundError."""
        _hosts_file(
            tmp_path,
            [
                {
                    "ip": "10.10.200.11",
                    "element": "orange",
                    "creds": {"vagrant": "vagrant"},
                    "resources": ["orange"],
                    "labs": ["other_lab"],
                },
            ],
        )

        repo = JsonFileLabRepository([tmp_path])

        with pytest.raises(LabNotFoundError) as exc_info:
            repo.load_lab("nonexistent")

        assert "nonexistent" in str(exc_info.value)

    def test_load_lab_only_returns_matching_hosts(self, tmp_path):
        _hosts_file(
            tmp_path,
            [
                {
                    "ip": "10.10.200.11",
                    "element": "orange",
                    "creds": {"vagrant": "vagrant"},
                    "resources": ["orange"],
                    "labs": ["lab_a"],
                },
                {
                    "ip": "10.10.200.12",
                    "element": "tomato",
                    "creds": {"vagrant": "vagrant"},
                    "resources": ["tomato"],
                    "labs": ["lab_b"],
                },
            ],
        )

        repo = JsonFileLabRepository([tmp_path])
        lab = repo.load_lab("lab_a")

        assert len(lab.hosts) == 1
        assert "orange" in lab.hosts

    def test_load_lab_multiple_search_paths(self, tmp_path):
        path1 = tmp_path / "path1"
        path2 = tmp_path / "path2"
        path1.mkdir()
        path2.mkdir()

        _hosts_file(
            path2,
            [
                {
                    "ip": "10.10.200.11",
                    "element": "orange",
                    "creds": {"vagrant": "vagrant"},
                    "resources": ["orange"],
                    "labs": ["testlab"],
                },
            ],
        )

        repo = JsonFileLabRepository([path1, path2])
        lab = repo.load_lab("testlab")

        assert isinstance(lab, Lab)
        assert lab.name == "testlab"

    def test_load_lab_not_a_list(self, tmp_path):
        """A non-array JSON root raises LabRepositoryError."""
        (tmp_path / "hosts.json").write_text(json.dumps({"hosts": []}))

        repo = JsonFileLabRepository([tmp_path])

        with pytest.raises(LabRepositoryError) as exc_info:
            repo.load_lab("badlab")

        assert "array" in str(exc_info.value)

    def test_load_lab_invalid_json(self, tmp_path):
        """Malformed JSON raises LabRepositoryError."""
        (tmp_path / "hosts.json").write_text("[{invalid json")

        repo = JsonFileLabRepository([tmp_path])

        with pytest.raises(LabRepositoryError):
            repo.load_lab("badlab")

    def test_load_lab_invalid_host_data(self, tmp_path):
        """Invalid host data raises LabRepositoryError with index context."""
        _hosts_file(
            tmp_path,
            [
                {
                    "element": "orange",
                    "creds": {"vagrant": "vagrant"},
                    "labs": ["badlab"],
                },
            ],
        )

        repo = JsonFileLabRepository([tmp_path])

        with pytest.raises(LabRepositoryError) as exc_info:
            repo.load_lab("badlab")

        assert "index 0" in str(exc_info.value)
        assert "ip" in str(exc_info.value)

    def test_load_lab_resource_aggregation(self, tmp_path):
        _hosts_file(
            tmp_path,
            [
                {
                    "ip": "10.10.200.11",
                    "element": "orange",
                    "creds": {"vagrant": "vagrant"},
                    "resources": ["orange", "citrus"],
                    "labs": ["resourcelab"],
                },
                {
                    "ip": "10.10.200.12",
                    "element": "tomato",
                    "creds": {"vagrant": "vagrant"},
                    "resources": ["tomato", "vegetable"],
                    "labs": ["resourcelab"],
                },
            ],
        )

        repo = JsonFileLabRepository([tmp_path])
        lab = repo.load_lab("resourcelab")

        assert "orange" in lab.resources
        assert "citrus" in lab.resources
        assert "tomato" in lab.resources
        assert "vegetable" in lab.resources

    def test_load_lab_host_ids_generated(self, tmp_path):
        _hosts_file(
            tmp_path,
            [
                {
                    "ip": "10.10.200.11",
                    "element": "orange",
                    "board": "seed",
                    "slot": 0,
                    "creds": {"vagrant": "vagrant"},
                    "resources": ["orange"],
                    "labs": ["idlab"],
                },
            ],
        )

        repo = JsonFileLabRepository([tmp_path])
        lab = repo.load_lab("idlab")

        assert "orange_seed0" in lab.hosts

    def test_list_labs(self, tmp_path):
        _hosts_file(
            tmp_path,
            [
                {
                    "ip": "10.10.200.11",
                    "element": "orange",
                    "creds": {"vagrant": "vagrant"},
                    "resources": ["orange"],
                    "labs": ["alpha"],
                },
                {
                    "ip": "10.10.200.12",
                    "element": "tomato",
                    "creds": {"vagrant": "vagrant"},
                    "resources": ["tomato"],
                    "labs": ["beta"],
                },
            ],
        )

        repo = JsonFileLabRepository([tmp_path])
        assert repo.list_labs() == ["alpha", "beta"]

    def test_list_labs_multiple_search_paths(self, tmp_path):
        path1 = tmp_path / "p1"
        path2 = tmp_path / "p2"
        path1.mkdir()
        path2.mkdir()

        _hosts_file(
            path1,
            [
                {
                    "ip": "10.10.200.11",
                    "element": "orange",
                    "creds": {"vagrant": "vagrant"},
                    "resources": [],
                    "labs": ["alpha"],
                },
            ],
        )
        _hosts_file(
            path2,
            [
                {
                    "ip": "10.10.200.12",
                    "element": "tomato",
                    "creds": {"vagrant": "vagrant"},
                    "resources": [],
                    "labs": ["beta"],
                },
            ],
        )

        repo = JsonFileLabRepository([path1, path2])
        assert repo.list_labs() == ["alpha", "beta"]

    def test_list_labs_no_hosts_file(self, tmp_path):
        repo = JsonFileLabRepository([tmp_path])
        assert repo.list_labs() == []

    def test_list_labs_skips_malformed_file(self, tmp_path):
        """A malformed hosts.json is skipped by list_labs, not fatal."""
        (tmp_path / "hosts.json").write_text("[{invalid json")
        repo = JsonFileLabRepository([tmp_path])
        assert repo.list_labs() == []

    def test_default_search_paths_empty(self):
        """Constructed with no search paths -> no labs, no hosts file found."""
        repo = JsonFileLabRepository()
        assert repo.list_labs() == []


class TestLoadLabWithPreferences:
    """End-to-end tests for the unified ``preferences=`` parameter on ``load_lab``."""

    def _hosts(self, tmp_path):
        _hosts_file(
            tmp_path,
            [
                {
                    "ip": "10.10.200.11",
                    "element": "orange",
                    "creds": {"vagrant": "vagrant"},
                    "resources": [],
                    "labs": ["testlab"],
                    "ssh_options": {"port": 9000},
                },
            ],
        )

    def test_defaults_apply_during_load(self, tmp_path):
        """Product preferences (option tables) merge into hosts during load_lab.
        The preference connect_timeout wins; the host-only port is preserved.
        """
        self._hosts(tmp_path)
        repo = JsonFileLabRepository([tmp_path])
        lab = repo.load_lab(
            "testlab",
            preferences={".*": {"ssh_options": {"connect_timeout": 99.0}}},
        )
        host = next(iter(lab.hosts.values()))
        assert host.ssh_options.port == 9000  # host-only key preserved
        assert host.ssh_options.connect_timeout == 99.0  # preferences wins

    def test_defaults_none_unchanged_behavior(self, tmp_path):
        """``preferences=None`` matches today's behavior."""
        self._hosts(tmp_path)
        repo = JsonFileLabRepository([tmp_path])
        lab = repo.load_lab("testlab")
        host = next(iter(lab.hosts.values()))
        assert host.ssh_options.port == 9000
        from otto.host.options import SshOptions

        assert host.ssh_options.connect_timeout == SshOptions().connect_timeout
