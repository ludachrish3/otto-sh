import json
import logging
from pathlib import Path

import pytest

from otto.config.lab import Lab
from otto.labs import LabNotFoundError, LabRepositoryError
from otto.labs.json_repository import JsonFileLabRepository

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


class TestDeclaredLinks:
    """``links`` section consumption in ``load_lab`` (Task 5: declared-link resolution)."""

    def test_declared_link_between_in_lab_hosts_loads_with_resolved_ips(self, tmp_path):
        host_a = {**HOST_ENTRY, "element": "carrot", "board": "seed"}
        host_b = {**HOST_ENTRY, "element": "tomato", "board": "seed", "ip": "192.0.2.2"}
        _write_lab(
            tmp_path,
            hosts=[host_a, host_b],
            links=[
                {
                    "endpoints": [{"host": "carrot_seed"}, {"host": "tomato_seed"}],
                    "protocol": "tcp",
                }
            ],
        )
        repo = JsonFileLabRepository([tmp_path])
        lab = repo.load_lab("veggies")

        assert len(lab.links) == 1
        (link,) = lab.links
        assert {link.a.host, link.b.host} == {"carrot_seed", "tomato_seed"}
        assert {link.a.ip, link.b.ip} == {"192.0.2.1", "192.0.2.2"}

    def test_cross_lab_link_resolves_dangling_endpoint(self, tmp_path):
        """One endpoint outside the requested lab still resolves its ip
        from the raw host dict, and the link surfaces (>= 1 endpoint in-lab).
        """
        host_a = {**HOST_ENTRY, "element": "carrot", "board": "seed", "labs": ["veggies"]}
        host_other = {
            **HOST_ENTRY,
            "element": "kiwi",
            "board": "seed",
            "ip": "192.0.2.9",
            "labs": ["other"],
        }
        _write_lab(
            tmp_path,
            hosts=[host_a, host_other],
            links=[
                {
                    "endpoints": [{"host": "carrot_seed"}, {"host": "kiwi_seed"}],
                    "protocol": "tcp",
                }
            ],
        )
        repo = JsonFileLabRepository([tmp_path])
        lab = repo.load_lab("veggies")

        assert "kiwi_seed" not in lab.hosts  # dangling: not part of this lab's hosts
        assert len(lab.links) == 1
        (link,) = lab.links
        assert {link.a.host, link.b.host} == {"carrot_seed", "kiwi_seed"}
        assert {link.a.ip, link.b.ip} == {"192.0.2.1", "192.0.2.9"}

    def test_link_fully_outside_lab_is_excluded(self, tmp_path):
        host_a = {**HOST_ENTRY, "element": "carrot", "board": "seed", "labs": ["veggies"]}
        host_x = {
            **HOST_ENTRY,
            "element": "kiwi",
            "board": "seed",
            "ip": "192.0.2.9",
            "labs": ["other"],
        }
        host_y = {
            **HOST_ENTRY,
            "element": "mango",
            "board": "seed",
            "ip": "192.0.2.10",
            "labs": ["other"],
        }
        _write_lab(
            tmp_path,
            hosts=[host_a, host_x, host_y],
            links=[
                {
                    "endpoints": [{"host": "kiwi_seed"}, {"host": "mango_seed"}],
                    "protocol": "tcp",
                }
            ],
        )
        repo = JsonFileLabRepository([tmp_path])
        lab = repo.load_lab("veggies")

        assert lab.links == []

    def test_unrelated_lab_bad_links_do_not_break_load(self, tmp_path):
        """A typo'd/malformed link between two hosts of a DIFFERENT lab must not
        break loading the requested lab — symmetric with the cross-lab host-record
        containment. The requested lab's own valid link still surfaces.
        """
        carrot = {**HOST_ENTRY, "element": "carrot", "board": "seed", "labs": ["veggies"]}
        tomato = {
            **HOST_ENTRY,
            "element": "tomato",
            "board": "seed",
            "ip": "192.0.2.2",
            "labs": ["veggies"],
        }
        kiwi = {
            **HOST_ENTRY,
            "element": "kiwi",
            "board": "seed",
            "ip": "192.0.2.9",
            "labs": ["other"],
        }
        _write_lab(
            tmp_path,
            hosts=[carrot, tomato, kiwi],
            links=[
                # (0) the requested lab's OWN valid link — must survive.
                {
                    "endpoints": [{"host": "carrot_seed"}, {"host": "tomato_seed"}],
                    "protocol": "tcp",
                },
                # (1) unrelated lab: references an unknown host — must be SKIPPED,
                # not raise (pre-fix this failed every lab's load).
                {
                    "endpoints": [{"host": "ghost_seed"}, {"host": "phantom_seed"}],
                    "protocol": "udp",
                },
                # (2) unrelated lab: structurally malformed (1 endpoint) — must be SKIPPED.
                {"endpoints": [{"host": "kiwi_seed"}]},
            ],
        )
        repo = JsonFileLabRepository([tmp_path])
        lab = repo.load_lab("veggies")

        assert set(lab.hosts) == {"carrot_seed", "tomato_seed"}
        assert len(lab.links) == 1
        (link,) = lab.links
        assert {link.a.host, link.b.host} == {"carrot_seed", "tomato_seed"}

    def test_unknown_host_link_raises_with_index_and_source(self, tmp_path):
        host_a = {**HOST_ENTRY, "element": "carrot", "board": "seed", "labs": ["veggies"]}
        _write_lab(
            tmp_path,
            hosts=[host_a],
            links=[
                {
                    "endpoints": [{"host": "nope"}, {"host": "carrot_seed"}],
                    "protocol": "tcp",
                }
            ],
        )
        repo = JsonFileLabRepository([tmp_path])

        with pytest.raises(LabRepositoryError, match=r"lab\.json.*index 0"):
            repo.load_lab("veggies")

    @pytest.mark.parametrize(
        "malformed_fields",
        [
            {"interfaces": None},
            {"interfaces": ["eth0"]},
            {"interfaces": {"eth0": 123}},
            {"board": 123},
        ],
        ids=["interfaces-none", "interfaces-list", "interfaces-bad-entry", "board-int"],
    )
    def test_malformed_unrelated_lab_host_does_not_crash_load(self, tmp_path, malformed_fields):
        """A malformed host record belonging to a DIFFERENT lab must be skipped
        by the cross-lab addressing build, not crash the requested lab's load.
        """
        host_a = {**HOST_ENTRY, "element": "carrot", "board": "seed", "labs": ["veggies"]}
        malformed_other = {
            **HOST_ENTRY,
            "element": "kiwi",
            "board": "seed",
            "labs": ["other"],
            **malformed_fields,
        }
        _write_lab(tmp_path, hosts=[host_a, malformed_other])

        repo = JsonFileLabRepository([tmp_path])
        lab = repo.load_lab("veggies")

        assert "carrot_seed" in lab.hosts

    def test_duplicate_cross_lab_addressing_warns_and_keeps_first(self, tmp_path, caplog):
        """Two lab files that each declare a host slugging to the same id, with
        DIFFERING addressing (different ip), must not raise while building the
        cross-lab addressing map: the FIRST file's addressing wins, a warning
        is logged, and the requested lab still loads (cross-lab resilience —
        distinct from ``Lab.__add__``'s same-lab merge, which fails loud).
        """
        path1 = tmp_path / "path1"
        path2 = tmp_path / "path2"
        path1.mkdir()
        path2.mkdir()

        carrot = {**HOST_ENTRY, "element": "carrot", "board": "seed", "labs": ["veggies"]}
        dup_first = {
            **HOST_ENTRY,
            "element": "dup",
            "board": "seed",
            "ip": "192.0.2.50",
            "labs": ["other1"],
        }
        dup_second = {
            **HOST_ENTRY,
            "element": "dup",
            "board": "seed",
            "ip": "192.0.2.99",
            "labs": ["other2"],
        }
        _write_lab(
            path1,
            hosts=[carrot, dup_first],
            links=[
                {
                    "endpoints": [{"host": "carrot_seed"}, {"host": "dup_seed"}],
                    "protocol": "tcp",
                }
            ],
        )
        _write_lab(path2, hosts=[dup_second])

        repo = JsonFileLabRepository([path1, path2])

        with caplog.at_level(logging.WARNING):
            lab = repo.load_lab("veggies")

        assert any("Duplicate host id" in r.message for r in caplog.records)
        assert "carrot_seed" in lab.hosts
        (link,) = lab.links
        dup_ip = link.a.ip if link.a.host == "dup_seed" else link.b.ip
        assert dup_ip == "192.0.2.50"  # first file's addressing kept


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
