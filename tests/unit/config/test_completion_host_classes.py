"""The class map completion scopes host verbs with, without building a host."""

from pathlib import Path

from otto.config.completion_cache import collect_host_classes_by_id
from otto.config.project import Project
from tests._fixtures.fake_project import fake_project
from tests._fixtures.labdata import json_lab_sources, write_lab_json

_CREDS = [{"login": "u", "password": "p"}]


def _project(tmp_path: Path, hosts: list[dict]) -> Project:
    lab = tmp_path / "lab"
    lab.mkdir()
    write_lab_json(lab / "lab.json", hosts)
    return fake_project(lab_sources=json_lab_sources(tmp_path, [lab]), root=tmp_path)


def test_maps_ids_to_the_profile_base_class(tmp_path):
    project = _project(
        tmp_path,
        [
            {"ip": "1.1.1.1", "element": "u1", "labs": ["e"], "creds": _CREDS},
            {"ip": "1.1.1.2", "element": "z1", "labs": ["e"], "creds": _CREDS, "os_type": "zephyr"},
        ],
    )
    assert collect_host_classes_by_id([project]) == {"u1": "unix", "z1": "zephyr"}


def test_an_unregistered_profile_is_omitted_not_guessed(tmp_path):
    project = _project(
        tmp_path,
        [{"ip": "1.1.1.1", "element": "m1", "labs": ["e"], "creds": _CREDS, "os_type": "nosuch"}],
    )
    assert collect_host_classes_by_id([project]) == {}


def test_empty_without_projects():
    assert collect_host_classes_by_id([]) == {}
