import json
from pathlib import Path

import pytest

from otto.configmodule.lab import Lab
from otto.storage import LabNotFoundError, LabRepositoryError
from otto.storage.json_repository import JsonFileLabRepository

HOST_ENTRY = {
    "ip": "192.0.2.1",
    "element": "carrot",
    "creds": [{"login": "vagrant", "password": "vagrant"}],
    "resources": ["carrot"],
    "labs": ["veggies"],
}


def _hosts_file(path: Path, hosts: list[dict]) -> Path:
    """Write a ``lab.json`` (object form) holding *hosts* and return its path."""
    f = path / "lab.json"
    f.write_text(json.dumps({"hosts": hosts}))
    return f


def _write_lab(tmp_path, hosts=(), links=(), name="lab.json"):
    payload = {"hosts": list(hosts), "links": list(links)}
    (tmp_path / name).write_text(json.dumps(payload))


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
                    "creds": [{"login": "vagrant", "password": "vagrant"}],
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
                    "creds": [{"login": "vagrant", "password": "vagrant"}],
                    "resources": ["orange"],
                    "labs": ["multilab"],
                },
                {
                    "ip": "10.10.200.12",
                    "element": "tomato",
                    "board": "seed",
                    "creds": [{"login": "vagrant", "password": "vagrant"}],
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
        """A missing lab.json raises LabNotFoundError, not FileNotFoundError."""
        repo = JsonFileLabRepository([tmp_path])

        with pytest.raises(LabNotFoundError) as exc_info:
            repo.load_lab("nonexistent")

        assert str(tmp_path) in str(exc_info.value)

    def test_load_lab_not_found_lab_absent(self, tmp_path):
        """lab.json exists but the lab name is not present -> LabNotFoundError."""
        _hosts_file(
            tmp_path,
            [
                {
                    "ip": "10.10.200.11",
                    "element": "orange",
                    "creds": [{"login": "vagrant", "password": "vagrant"}],
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
                    "creds": [{"login": "vagrant", "password": "vagrant"}],
                    "resources": ["orange"],
                    "labs": ["lab_a"],
                },
                {
                    "ip": "10.10.200.12",
                    "element": "tomato",
                    "creds": [{"login": "vagrant", "password": "vagrant"}],
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
                    "creds": [{"login": "vagrant", "password": "vagrant"}],
                    "resources": ["orange"],
                    "labs": ["testlab"],
                },
            ],
        )

        repo = JsonFileLabRepository([path1, path2])
        lab = repo.load_lab("testlab")

        assert isinstance(lab, Lab)
        assert lab.name == "testlab"

    def test_load_lab_invalid_json(self, tmp_path):
        """Malformed JSON raises LabRepositoryError."""
        (tmp_path / "lab.json").write_text("{invalid json")

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
                    "creds": [{"login": "vagrant", "password": "vagrant"}],
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
                    "creds": [{"login": "vagrant", "password": "vagrant"}],
                    "resources": ["orange", "citrus"],
                    "labs": ["resourcelab"],
                },
                {
                    "ip": "10.10.200.12",
                    "element": "tomato",
                    "creds": [{"login": "vagrant", "password": "vagrant"}],
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
                    "creds": [{"login": "vagrant", "password": "vagrant"}],
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
                    "creds": [{"login": "vagrant", "password": "vagrant"}],
                    "resources": ["orange"],
                    "labs": ["alpha"],
                },
                {
                    "ip": "10.10.200.12",
                    "element": "tomato",
                    "creds": [{"login": "vagrant", "password": "vagrant"}],
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
                    "creds": [{"login": "vagrant", "password": "vagrant"}],
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
                    "creds": [{"login": "vagrant", "password": "vagrant"}],
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
        """A malformed lab.json is skipped by list_labs, not fatal."""
        (tmp_path / "lab.json").write_text("{invalid json")
        repo = JsonFileLabRepository([tmp_path])
        assert repo.list_labs() == []

    def test_default_search_paths_empty(self):
        """Constructed with no search paths -> no labs, no lab file found."""
        repo = JsonFileLabRepository()
        assert repo.list_labs() == []


class TestLabFileShape:
    """The lab.json object contract: hosts/links sections, comment keys, hard cutover."""

    def test_array_top_level_rejected(self, tmp_path):
        (tmp_path / "lab.json").write_text(json.dumps([{"ip": "192.0.2.1"}]))
        repo = JsonFileLabRepository(search_paths=[tmp_path])
        with pytest.raises(LabRepositoryError, match="JSON object"):
            repo.load_lab("veggies")

    def test_unknown_section_rejected(self, tmp_path):
        (tmp_path / "lab.json").write_text(json.dumps({"hosts": [], "routes": []}))
        repo = JsonFileLabRepository(search_paths=[tmp_path])
        with pytest.raises(LabRepositoryError, match="unknown section"):
            repo.load_lab("veggies")

    def test_top_level_comment_keys_allowed(self, tmp_path):
        _write_lab(tmp_path, hosts=[HOST_ENTRY])
        payload = json.loads((tmp_path / "lab.json").read_text())
        payload["_comment"] = "a note"
        (tmp_path / "lab.json").write_text(json.dumps(payload))
        repo = JsonFileLabRepository(search_paths=[tmp_path])
        assert repo.load_lab("veggies").hosts  # loads fine

    def test_missing_sections_default_empty(self, tmp_path):
        (tmp_path / "lab.json").write_text(json.dumps({}))
        repo = JsonFileLabRepository(search_paths=[tmp_path])
        with pytest.raises(LabNotFoundError):  # no hosts -> lab not found
            repo.load_lab("veggies")

    def test_section_not_array_rejected(self, tmp_path):
        (tmp_path / "lab.json").write_text(json.dumps({"hosts": {"not": "a list"}}))
        repo = JsonFileLabRepository(search_paths=[tmp_path])
        with pytest.raises(LabRepositoryError, match="must be a JSON array"):
            repo.load_lab("veggies")

    def test_hosts_json_is_not_read(self, tmp_path):
        """Hard cutover: a legacy hosts.json is invisible."""
        (tmp_path / "hosts.json").write_text(json.dumps([HOST_ENTRY]))
        repo = JsonFileLabRepository(search_paths=[tmp_path])
        with pytest.raises(LabNotFoundError, match=r"lab\.json"):
            repo.load_lab("veggies")


class TestLoadLabWithPreferences:
    """End-to-end tests for the unified ``preferences=`` parameter on ``load_lab``."""

    def _hosts(self, tmp_path):
        _hosts_file(
            tmp_path,
            [
                {
                    "ip": "10.10.200.11",
                    "element": "orange",
                    "creds": [{"login": "vagrant", "password": "vagrant"}],
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
