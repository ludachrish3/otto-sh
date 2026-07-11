"""Unit tests for config.load_lab repository routing."""

import json
from pathlib import Path

from otto.config.lab import Lab, load_lab
from otto.labs.json_repository import JsonFileLabRepository
from tests._fixtures.labdata import lab_data_dir


def _hosts_file(path: Path, hosts: list[dict]) -> Path:
    f = path / "lab.json"
    f.write_text(json.dumps({"hosts": hosts}))
    return f


def test_load_lab_default_repository_uses_search_paths(tmp_path):
    """With no repository given, load_lab builds a json backend over search_paths."""
    _hosts_file(
        tmp_path,
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
    lab = load_lab("testlab", search_paths=[tmp_path])
    assert isinstance(lab, Lab)
    assert lab.name == "testlab"
    # `local` is the built-in host load_lab injects into every lab.
    assert set(lab.hosts) == {"orange", "local"}


def test_load_lab_uses_injected_repository(tmp_path):
    """A passed repository is used instead of the default json backend."""
    _hosts_file(
        tmp_path,
        [
            {
                "ip": "10.10.200.11",
                "element": "orange",
                "creds": [{"login": "vagrant", "password": "vagrant"}],
                "resources": ["orange"],
                "labs": ["injected"],
            },
        ],
    )
    repo = JsonFileLabRepository([tmp_path])
    lab = load_lab("injected", repository=repo)
    assert lab.name == "injected"
    assert set(lab.hosts) == {"orange", "local"}  # `local` = built-in injection


def test_load_lab_merges_multiple_names(tmp_path):
    """Comma-joined names merge into one lab (preserved behavior)."""
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
    lab = load_lab("lab_a,lab_b", repository=repo)
    assert set(lab.hosts) == {"orange", "tomato", "local"}  # `local` = built-in injection


def test_load_lab_round_trips_declared_link_from_fixture():
    """The tech1 fixture's carrot<->tomato udp link survives load_lab end to end."""
    lab = load_lab("veggies", search_paths=[lab_data_dir() / "tech1"])

    assert len(lab.links) == 1
    (link,) = lab.links
    assert {link.a.host, link.b.host} == {"carrot_seed", "tomato_seed"}
    assert link.protocol == "udp"
    assert {link.a.ip, link.b.ip} == {"192.168.1.11", "192.168.1.12"}
