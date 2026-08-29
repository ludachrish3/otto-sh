"""JsonFileLabRepository: construction, search paths, file shape, links and preferences.

Also covers the v2-specific rules: declared labs, element membership by
pattern, and the migration error a v1 top-level ``hosts`` array raises (spec
§2, §7, §9).
"""

import json
import logging
from pathlib import Path

import pytest

from otto.config.lab import Lab
from otto.labs import LabNotFoundError, LabRepositoryError
from otto.labs.json_repository import JsonFileLabRepository, parse_lab_sections
from tests._fixtures.labdata import write_lab_json

_CREDS = [{"login": "u", "password": "p"}]

HOST_ENTRY = {
    "ip": "192.0.2.1",
    "element": "test1",
    "creds": [{"login": "vagrant", "password": "vagrant"}],
    "resources": ["test1"],
    "labs": ["unix"],
}


def _hosts_file(path: Path, hosts: list[dict]) -> Path:
    """Write a v2 ``lab.json`` expressing *hosts* (flat dicts) and return its path.

    The fixtures below stay flat — that is the shape the host factory takes —
    while the FILE is v2: ``write_lab_json`` hoists each host's ``labs`` onto
    its element and its ``resources`` into the ``labs`` table.
    """
    return write_lab_json(path / "lab.json", hosts)


def _write_lab(tmp_path, hosts=(), links=(), name="lab.json"):
    write_lab_json(tmp_path / name, hosts, links)


def _write(tmp_path: Path, doc: dict, name: str = "lab.json") -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(doc))
    return p


def _doc(*, labs=None, elements=(), links=()) -> dict:
    return {"labs": labs or {}, "elements": list(elements), "links": list(links)}


def _el(name, labs, hosts=None, **extra) -> dict:
    return {
        "name": name,
        "labs": labs,
        "hosts": hosts or [{"ip": "10.0.0.1", "creds": _CREDS}],
        **extra,
    }


class TestJsonFileLabRepository:
    """Tests for JsonFileLabRepository (construct-time search paths)."""

    def test_load_lab_simple(self, tmp_path):
        _hosts_file(
            tmp_path,
            [
                {
                    "ip": "10.10.200.11",
                    "element": "alt1",
                    "creds": [{"login": "vagrant", "password": "vagrant"}],
                    "resources": ["alt1"],
                    "labs": ["testlab"],
                },
            ],
        )

        repo = JsonFileLabRepository([tmp_path])
        lab = repo.load_lab("testlab")

        assert isinstance(lab, Lab)
        assert lab.name == "testlab"
        assert len(lab.hosts) == 1
        assert "alt1" in lab.resources

    def test_load_lab_multiple_hosts(self, tmp_path):
        _hosts_file(
            tmp_path,
            [
                {
                    "ip": "10.10.200.11",
                    "element": "alt1",
                    "creds": [{"login": "vagrant", "password": "vagrant"}],
                    "resources": ["alt1"],
                    "labs": ["multilab"],
                },
                {
                    "ip": "10.10.200.12",
                    "element": "test2",
                    "creds": [{"login": "vagrant", "password": "vagrant"}],
                    "resources": ["test2"],
                    "labs": ["multilab"],
                },
            ],
        )

        repo = JsonFileLabRepository([tmp_path])
        lab = repo.load_lab("multilab")

        assert isinstance(lab, Lab)
        assert lab.name == "multilab"
        assert len(lab.hosts) == 2
        assert "alt1" in lab.resources
        assert "test2" in lab.resources

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
                    "element": "alt1",
                    "creds": [{"login": "vagrant", "password": "vagrant"}],
                    "resources": ["alt1"],
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
                    "element": "alt1",
                    "creds": [{"login": "vagrant", "password": "vagrant"}],
                    "resources": ["alt1"],
                    "labs": ["lab_a"],
                },
                {
                    "ip": "10.10.200.12",
                    "element": "test2",
                    "creds": [{"login": "vagrant", "password": "vagrant"}],
                    "resources": ["test2"],
                    "labs": ["lab_b"],
                },
            ],
        )

        repo = JsonFileLabRepository([tmp_path])
        lab = repo.load_lab("lab_a")

        assert len(lab.hosts) == 1
        assert "alt1" in lab.hosts

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
                    "element": "alt1",
                    "creds": [{"login": "vagrant", "password": "vagrant"}],
                    "resources": ["alt1"],
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
                    "element": "alt1",
                    "creds": [{"login": "vagrant", "password": "vagrant"}],
                    "labs": ["badlab"],
                },
            ],
        )

        repo = JsonFileLabRepository([tmp_path])

        with pytest.raises(LabRepositoryError) as exc_info:
            repo.load_lab("badlab")

        # The element and the host's index WITHIN it — not a running count
        # across the lab, which no longer exists as a flat array.
        assert "element 'alt1' hosts[0]" in str(exc_info.value)
        assert "ip" in str(exc_info.value)

    def test_load_lab_resources_come_from_the_declaring_labs_entry(self, tmp_path):
        """The lab is the reservable unit: ``resources`` are DECLARED per lab.

        Replaces the v1 aggregation test (host resources unioned into the lab)
        — hosts no longer carry ``resources`` at all. ``write_lab_json`` hoists
        each flat host's into its own labs' table entries, so the fixture still
        reads the same while pinning the new rule: the OTHER lab's declared
        resources stay out of this one.
        """
        _hosts_file(
            tmp_path,
            [
                {
                    "ip": "10.10.200.11",
                    "element": "alt1",
                    "creds": [{"login": "vagrant", "password": "vagrant"}],
                    "resources": ["alt1", "citrus"],
                    "labs": ["resourcelab"],
                },
                {
                    "ip": "10.10.200.12",
                    "element": "test2",
                    "creds": [{"login": "vagrant", "password": "vagrant"}],
                    "resources": ["test2", "vegetable"],
                    "labs": ["otherlab"],
                },
            ],
        )

        repo = JsonFileLabRepository([tmp_path])
        lab = repo.load_lab("resourcelab")

        assert lab.resources == {"alt1", "citrus"}

    def test_load_lab_host_ids_generated(self, tmp_path):
        _hosts_file(
            tmp_path,
            [
                {
                    "ip": "10.10.200.11",
                    "element": "alt1",
                    "board": "qemu",
                    "slot": 0,
                    "creds": [{"login": "vagrant", "password": "vagrant"}],
                    "resources": ["alt1"],
                    "labs": ["idlab"],
                },
            ],
        )

        repo = JsonFileLabRepository([tmp_path])
        lab = repo.load_lab("idlab")

        assert "alt1_qemu0" in lab.hosts

    def test_list_labs(self, tmp_path):
        _hosts_file(
            tmp_path,
            [
                {
                    "ip": "10.10.200.11",
                    "element": "alt1",
                    "creds": [{"login": "vagrant", "password": "vagrant"}],
                    "resources": ["alt1"],
                    "labs": ["alpha"],
                },
                {
                    "ip": "10.10.200.12",
                    "element": "test2",
                    "creds": [{"login": "vagrant", "password": "vagrant"}],
                    "resources": ["test2"],
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
                    "element": "alt1",
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
                    "element": "test2",
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
    """The lab.json object contract: labs/elements/links sections, comment keys, hard cutover."""

    def test_array_top_level_rejected(self, tmp_path):
        (tmp_path / "lab.json").write_text(json.dumps([{"ip": "192.0.2.1"}]))
        repo = JsonFileLabRepository(search_paths=[tmp_path])
        with pytest.raises(LabRepositoryError, match="JSON object"):
            repo.load_lab("unix")

    def test_unknown_section_rejected(self, tmp_path):
        (tmp_path / "lab.json").write_text(json.dumps({"elements": [], "routes": []}))
        repo = JsonFileLabRepository(search_paths=[tmp_path])
        with pytest.raises(LabRepositoryError, match="unknown section"):
            repo.load_lab("unix")

    def test_top_level_comment_keys_allowed(self, tmp_path):
        _write_lab(tmp_path, hosts=[HOST_ENTRY])
        payload = json.loads((tmp_path / "lab.json").read_text())
        payload["_comment"] = "a note"
        (tmp_path / "lab.json").write_text(json.dumps(payload))
        repo = JsonFileLabRepository(search_paths=[tmp_path])
        assert repo.load_lab("unix").hosts  # loads fine

    def test_missing_sections_default_empty(self, tmp_path):
        (tmp_path / "lab.json").write_text(json.dumps({}))
        repo = JsonFileLabRepository(search_paths=[tmp_path])
        with pytest.raises(LabNotFoundError):  # neither declared nor matched
            repo.load_lab("unix")

    def test_section_not_array_rejected(self, tmp_path):
        (tmp_path / "lab.json").write_text(json.dumps({"elements": {"not": "a list"}}))
        repo = JsonFileLabRepository(search_paths=[tmp_path])
        with pytest.raises(LabRepositoryError, match="must be a JSON array"):
            repo.load_lab("unix")

    def test_labs_section_not_object_rejected(self, tmp_path):
        (tmp_path / "lab.json").write_text(json.dumps({"labs": ["unix"]}))
        repo = JsonFileLabRepository(search_paths=[tmp_path])
        with pytest.raises(LabRepositoryError, match="'labs' must be a JSON object"):
            repo.load_lab("unix")

    def test_hosts_json_is_not_read(self, tmp_path):
        """Hard cutover: a legacy hosts.json is invisible."""
        (tmp_path / "hosts.json").write_text(json.dumps([HOST_ENTRY]))
        repo = JsonFileLabRepository(search_paths=[tmp_path])
        with pytest.raises(LabNotFoundError, match=r"lab\.json"):
            repo.load_lab("unix")


class TestDeclaredLinks:
    """``links`` section consumption in ``load_lab`` (Task 5: declared-link resolution)."""

    def test_declared_link_between_in_lab_hosts_loads_with_resolved_ips(self, tmp_path):
        host_a = {**HOST_ENTRY, "element": "test1"}
        host_b = {**HOST_ENTRY, "element": "test2", "ip": "192.0.2.2"}
        _write_lab(
            tmp_path,
            hosts=[host_a, host_b],
            links=[
                {
                    "endpoints": [{"host": "test1"}, {"host": "test2"}],
                    "protocol": "tcp",
                }
            ],
        )
        repo = JsonFileLabRepository([tmp_path])
        lab = repo.load_lab("unix")

        assert len(lab.links) == 1
        (link,) = lab.links
        assert {link.a.host, link.b.host} == {"test1", "test2"}
        assert {link.a.ip, link.b.ip} == {"192.0.2.1", "192.0.2.2"}

    def test_cross_lab_link_resolves_dangling_endpoint(self, tmp_path):
        """One endpoint outside the requested lab still resolves its ip
        from the raw host dict, and the link surfaces (>= 1 endpoint in-lab).
        """
        host_a = {**HOST_ENTRY, "element": "test1", "labs": ["unix"]}
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
                    "endpoints": [{"host": "test1"}, {"host": "kiwi_seed"}],
                    "protocol": "tcp",
                }
            ],
        )
        repo = JsonFileLabRepository([tmp_path])
        lab = repo.load_lab("unix")

        assert "kiwi_seed" not in lab.hosts  # dangling: not part of this lab's hosts
        assert len(lab.links) == 1
        (link,) = lab.links
        assert {link.a.host, link.b.host} == {"test1", "kiwi_seed"}
        assert {link.a.ip, link.b.ip} == {"192.0.2.1", "192.0.2.9"}

    def test_link_fully_outside_lab_is_excluded(self, tmp_path):
        host_a = {**HOST_ENTRY, "element": "test1", "labs": ["unix"]}
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
        lab = repo.load_lab("unix")

        assert lab.links == []

    def test_unrelated_lab_bad_links_do_not_break_load(self, tmp_path):
        """A typo'd/malformed link between two hosts of a DIFFERENT lab must not
        break loading the requested lab — symmetric with the cross-lab host-record
        containment. The requested lab's own valid link still surfaces.
        """
        test1 = {**HOST_ENTRY, "element": "test1", "labs": ["unix"]}
        test2 = {
            **HOST_ENTRY,
            "element": "test2",
            "ip": "192.0.2.2",
            "labs": ["unix"],
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
            hosts=[test1, test2, kiwi],
            links=[
                # (0) the requested lab's OWN valid link — must survive.
                {
                    "endpoints": [{"host": "test1"}, {"host": "test2"}],
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
        lab = repo.load_lab("unix")

        assert set(lab.hosts) == {"test1", "test2"}
        assert len(lab.links) == 1
        (link,) = lab.links
        assert {link.a.host, link.b.host} == {"test1", "test2"}

    def test_unknown_host_link_raises_with_index_and_source(self, tmp_path):
        host_a = {**HOST_ENTRY, "element": "test1", "labs": ["unix"]}
        _write_lab(
            tmp_path,
            hosts=[host_a],
            links=[
                {
                    "endpoints": [{"host": "nope"}, {"host": "test1"}],
                    "protocol": "tcp",
                }
            ],
        )
        repo = JsonFileLabRepository([tmp_path])

        with pytest.raises(LabRepositoryError, match=r"lab\.json.*index 0"):
            repo.load_lab("unix")

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
        host_a = {**HOST_ENTRY, "element": "test1", "labs": ["unix"]}
        malformed_other = {
            **HOST_ENTRY,
            "element": "kiwi",
            "board": "seed",
            "labs": ["other"],
            **malformed_fields,
        }
        _write_lab(tmp_path, hosts=[host_a, malformed_other])

        repo = JsonFileLabRepository([tmp_path])
        lab = repo.load_lab("unix")

        assert "test1" in lab.hosts

    def test_duplicate_cross_lab_addressing_warns_and_keeps_first(self, tmp_path, caplog):
        """Two lab files whose hosts derive the same id, with DIFFERING addressing
        (different ip), must not raise while building the cross-lab addressing
        map: the FIRST file's addressing wins, a warning is logged, and the
        requested lab still loads (cross-lab resilience — distinct from
        ``Lab.__add__``'s same-lab merge, which fails loud).

        The colliding records belong to DISTINCT elements — ``dup`` id 1 and
        ``dup1``, both on board ``seed`` — which is the only way to reach this
        path under v2 (spec §9): the same element ``(name, id)`` in two files of
        one source is now a duplicate-element error, not an addressing clash.
        """
        path1 = tmp_path / "path1"
        path2 = tmp_path / "path2"
        path1.mkdir()
        path2.mkdir()

        test1 = {**HOST_ENTRY, "element": "test1", "labs": ["unix"]}
        dup_first = {
            **HOST_ENTRY,
            "element": "dup",
            "element_id": 1,
            "board": "seed",
            "ip": "192.0.2.50",
            "labs": ["other1"],
        }
        dup_second = {
            **HOST_ENTRY,
            "element": "dup1",
            "board": "seed",
            "ip": "192.0.2.99",
            "labs": ["other2"],
        }
        _write_lab(
            path1,
            hosts=[test1, dup_first],
            links=[
                {
                    "endpoints": [{"host": "test1"}, {"host": "dup1_seed"}],
                    "protocol": "tcp",
                }
            ],
        )
        _write_lab(path2, hosts=[dup_second])

        repo = JsonFileLabRepository([path1, path2])

        with caplog.at_level(logging.WARNING):
            lab = repo.load_lab("unix")

        assert any("Duplicate host id" in r.message for r in caplog.records)
        assert "test1" in lab.hosts
        (link,) = lab.links
        dup_ip = link.a.ip if link.a.host == "dup1_seed" else link.b.ip
        assert dup_ip == "192.0.2.50"  # first file's addressing kept


class TestLoadLabWithPreferences:
    """End-to-end tests for the unified ``preferences=`` parameter on ``load_lab``."""

    def _hosts(self, tmp_path):
        _hosts_file(
            tmp_path,
            [
                {
                    "ip": "10.10.200.11",
                    "element": "alt1",
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


def test_top_level_hosts_is_the_migration_error(tmp_path: Path) -> None:
    _write(tmp_path, {"hosts": [], "links": []})
    with pytest.raises(LabRepositoryError, match=r"'hosts'.*moved.*'elements'"):
        JsonFileLabRepository([tmp_path]).load_lab("unix")


def test_parse_lab_sections_shapes() -> None:
    out = parse_lab_sections({"$schema": "x", "_c": 1, "labs": {"unix": {}}, "elements": []}, "f")
    assert out == {"labs": {"unix": {}}, "elements": [], "links": []}
    with pytest.raises(LabRepositoryError, match="unknown section"):
        parse_lab_sections({"routes": []}, "f")
    with pytest.raises(LabRepositoryError, match="'labs' must be a JSON object"):
        parse_lab_sections({"labs": []}, "f")
    with pytest.raises(LabRepositoryError, match="'elements' must be a JSON array"):
        parse_lab_sections({"elements": {}}, "f")


def test_members_by_fullmatch_pattern_and_flattened_identity(tmp_path: Path) -> None:
    _write(
        tmp_path,
        _doc(
            labs={"unix": {"resources": ["unix-bed"], "metadata": {"d": 1}}, "unix2": {}},
            elements=[
                _el("test1", ["unix"]),
                _el("dut", ["unix(\\..*)?"], id=3, hosts=[{"ip": "10.0.0.2", "creds": _CREDS}]),
                _el("other", ["unix2"], hosts=[{"ip": "10.0.0.3", "creds": _CREDS}]),
            ],
        ),
    )
    lab = JsonFileLabRepository([tmp_path]).load_lab("unix")
    assert set(lab.hosts) == {"test1", "dut3"}
    assert lab.hosts["dut3"].element == "dut"
    assert lab.hosts["dut3"].element_id == 3
    assert lab.resources == {"unix-bed"}
    assert lab.metadata == {"unix": {"d": 1}}


def test_element_metadata_copied_per_host(tmp_path: Path) -> None:
    _write(
        tmp_path,
        _doc(
            labs={"l": {}},
            elements=[
                _el(
                    "dut",
                    ["l"],
                    metadata={"rack": "B4"},
                    hosts=[
                        {"ip": "10.0.0.1", "board": "a", "creds": _CREDS},
                        {"ip": "10.0.0.2", "board": "b", "creds": _CREDS},
                    ],
                )
            ],
        ),
    )
    lab = JsonFileLabRepository([tmp_path]).load_lab("l")
    a, b = lab.hosts["dut_a"], lab.hosts["dut_b"]
    assert a.element_metadata == {"rack": "B4"}
    assert b.element_metadata == {"rack": "B4"}
    a.element_metadata["rack"] = "Z"
    assert b.element_metadata == {"rack": "B4"}


def test_source_with_neither_members_nor_declaration_raises_not_found(tmp_path: Path) -> None:
    _write(tmp_path, _doc(labs={"unix": {}}, elements=[_el("t", ["unix"])]))
    with pytest.raises(LabNotFoundError):
        JsonFileLabRepository([tmp_path]).load_lab("embedded")


def test_declaration_only_and_members_only_contributions(tmp_path: Path) -> None:
    _write(tmp_path, _doc(labs={"unix": {"resources": ["r"]}}, elements=[_el("t", ["other"])]))
    declared_only = JsonFileLabRepository([tmp_path]).load_lab("unix")
    assert declared_only.hosts == {}
    assert declared_only.resources == {"r"}
    members_only = JsonFileLabRepository([tmp_path]).load_lab("other")
    assert set(members_only.hosts) == {"t"}
    assert members_only.resources == set()
    assert "other" not in members_only.metadata


def test_list_labs_is_the_declared_set_not_the_patterns(tmp_path: Path) -> None:
    _write(tmp_path, _doc(labs={"unix": {}, "b": {}}, elements=[_el("t", ["unix", "ghost"])]))
    assert JsonFileLabRepository([tmp_path]).list_labs() == ["b", "unix"]


def test_duplicate_element_key_errors(tmp_path: Path) -> None:
    _write(
        tmp_path, _doc(labs={"l": {}}, elements=[_el("dut", ["l"], id=1), _el("dut", ["l"], id=1)])
    )
    with pytest.raises(LabRepositoryError, match=r"duplicate element \('dut', 1\)"):
        JsonFileLabRepository([tmp_path]).load_lab("l")


def test_host_entry_error_names_element_and_index(tmp_path: Path) -> None:
    _write(
        tmp_path,
        _doc(labs={"l": {}}, elements=[_el("dut", ["l"], hosts=[{"ipp": "x", "creds": _CREDS}])]),
    )
    # (?s): the pydantic report is multi-line — the offending key is on a later
    # line than the "element 'dut' hosts[0]" prefix this test is really pinning.
    with pytest.raises(LabRepositoryError, match=r"(?s)element 'dut' hosts\[0\].*ipp"):
        JsonFileLabRepository([tmp_path]).load_lab("l")


def test_provider_failure_is_wrapped_with_the_entry_that_caused_it(
    tmp_path: Path, monkeypatch
) -> None:
    """A SUT-repo provider blowing up on one host must not escape as a raw traceback.

    ``create_host_from_dict`` runs repo-owned code after validation
    (``to_host``, then the product and dev-tool providers), which can raise
    ANYTHING. The composite only absorbs ``LabNotFoundError``, so an unwrapped
    ``AttributeError`` reaches the CLI with none of the file/element/index
    context spec §9 exists to give — and with no clue which of a hundred hosts
    is at fault.
    """
    _write(tmp_path, _doc(labs={"l": {}}, elements=[_el("dut", ["l"])]))
    boom = AttributeError("provider is broken")

    def _explode(host: object) -> None:
        raise boom

    monkeypatch.setattr("otto.host.factory.apply_product_providers", _explode)

    with pytest.raises(LabRepositoryError, match=r"element 'dut' hosts\[0\]") as exc_info:
        JsonFileLabRepository([tmp_path]).load_lab("l")
    assert exc_info.value.__cause__ is boom  # chained, never swallowed
    assert "provider is broken" in str(exc_info.value)


def test_summaries_carry_patterns_and_resolved_labs(tmp_path: Path) -> None:
    _write(tmp_path, _doc(labs={"unix": {}, "unix.b4": {}}, elements=[_el("t", ["unix(\\..*)?"])]))
    (s,) = JsonFileLabRepository([tmp_path]).list_host_summaries()
    assert s.id == "t"
    assert s.lab_patterns == ["unix(\\..*)?"]
    assert s.labs == ["unix", "unix.b4"]


def test_links_still_resolve_against_flattened_ids(tmp_path: Path) -> None:
    _write(
        tmp_path,
        _doc(
            labs={"l": {}},
            elements=[
                _el(
                    "a",
                    ["l"],
                    hosts=[{"ip": "10.0.0.1", "creds": _CREDS, "interfaces": {"e": "192.168.1.1"}}],
                ),
                _el(
                    "b",
                    ["l"],
                    hosts=[{"ip": "10.0.0.2", "creds": _CREDS, "interfaces": {"e": "192.168.1.2"}}],
                ),
            ],
            links=[
                {"endpoints": [{"host": "a", "interface": "e"}, {"host": "b", "interface": "e"}]}
            ],
        ),
    )
    lab = JsonFileLabRepository([tmp_path]).load_lab("l")
    assert len(lab.links) == 1
