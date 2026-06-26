"""Unit tests for configmodule.load_lab repository routing."""

import json
from pathlib import Path

from otto.configmodule.lab import Lab, load_lab
from otto.storage.json_repository import JsonFileLabRepository


def _hosts_file(path: Path, hosts: list[dict]) -> Path:
    f = path / "hosts.json"
    f.write_text(json.dumps(hosts))
    return f


def test_load_lab_default_repository_uses_search_paths(tmp_path):
    """With no repository given, load_lab builds a json backend over search_paths."""
    _hosts_file(tmp_path, [
        {
            "ip": "10.10.200.11",
            "element": "orange",
            "creds": {"vagrant": "vagrant"},
            "resources": ["orange"],
            "labs": ["testlab"],
        },
    ])
    lab = load_lab("testlab", search_paths=[tmp_path])
    assert isinstance(lab, Lab)
    assert lab.name == "testlab"
    assert len(lab.hosts) == 1


def test_load_lab_uses_injected_repository(tmp_path):
    """A passed repository is used instead of the default json backend."""
    _hosts_file(tmp_path, [
        {
            "ip": "10.10.200.11",
            "element": "orange",
            "creds": {"vagrant": "vagrant"},
            "resources": ["orange"],
            "labs": ["injected"],
        },
    ])
    repo = JsonFileLabRepository([tmp_path])
    lab = load_lab("injected", repository=repo)
    assert lab.name == "injected"
    assert len(lab.hosts) == 1


def test_load_lab_merges_multiple_names(tmp_path):
    """Comma-joined names merge into one lab (preserved behavior)."""
    _hosts_file(tmp_path, [
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
    ])
    repo = JsonFileLabRepository([tmp_path])
    lab = load_lab("lab_a,lab_b", repository=repo)
    assert len(lab.hosts) == 2
