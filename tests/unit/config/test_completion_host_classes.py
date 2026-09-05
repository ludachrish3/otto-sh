"""The class map completion scopes host verbs with, without building a host."""

from pathlib import Path
from types import SimpleNamespace

from otto.config.completion_cache import collect_host_classes_by_id
from tests._fixtures.labdata import json_lab_sources, write_lab_json

_CREDS = [{"login": "u", "password": "p"}]


def _repo(tmp_path: Path, hosts: list[dict]) -> SimpleNamespace:
    lab = tmp_path / "lab"
    lab.mkdir()
    write_lab_json(lab / "lab.json", hosts)
    return SimpleNamespace(
        lab_sources=json_lab_sources(tmp_path, [lab]), sut_dir=tmp_path, inventory_settings={}
    )


def test_maps_ids_to_the_profile_base_class(tmp_path):
    repo = _repo(
        tmp_path,
        [
            {"ip": "1.1.1.1", "element": "u1", "labs": ["e"], "creds": _CREDS},
            {"ip": "1.1.1.2", "element": "z1", "labs": ["e"], "creds": _CREDS, "os_type": "zephyr"},
        ],
    )
    assert collect_host_classes_by_id([repo]) == {"u1": "unix", "z1": "zephyr"}


def test_logical_handles_map_to_their_hosts_class(tmp_path):
    repo = _repo(
        tmp_path,
        [
            {
                "ip": "1.1.1.1",
                "element": "server",
                "element_id": 47,
                "labs": ["e"],
                "creds": _CREDS,
            },
            {
                "ip": "1.1.1.2",
                "element": "server",
                "element_id": 103,
                "labs": ["e"],
                "creds": _CREDS,
                "os_type": "zephyr",
            },
        ],
    )
    got = collect_host_classes_by_id([repo])
    assert got["server1"] == "unix"
    assert got["server2"] == "zephyr"


def test_an_unregistered_profile_is_omitted_not_guessed(tmp_path):
    repo = _repo(
        tmp_path,
        [{"ip": "1.1.1.1", "element": "m1", "labs": ["e"], "creds": _CREDS, "os_type": "nosuch"}],
    )
    assert collect_host_classes_by_id([repo]) == {}


def test_empty_without_repos():
    assert collect_host_classes_by_id([]) == {}
