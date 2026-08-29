"""Completion enumerates canonical ids AND positional logical handles."""

from pathlib import Path
from types import SimpleNamespace

from otto.config.completion_cache import collect_host_ids, collect_host_ids_by_lab
from tests._fixtures.labdata import json_lab_sources, write_lab_json


def _repo(tmp_path: Path) -> SimpleNamespace:
    # Minimal fake Repo (duck-typed: collect_host_ids only reads `.lab_sources`),
    # matching the pattern used by tests/unit/config/test_completion_host_ids.py.
    labs_dir = tmp_path / "labs"
    labs_dir.mkdir()
    creds = [{"login": "u", "password": "p"}]
    write_lab_json(
        labs_dir / "lab.json",
        [
            {
                "ip": "10.0.0.1",
                "element": "server",
                "element_id": 47,
                "labs": ["east"],
                "creds": creds,
            },
            {
                "ip": "10.0.0.2",
                "element": "server",
                "element_id": 103,
                "labs": ["east"],
                "creds": creds,
            },
        ],
    )
    return SimpleNamespace(
        lab_sources=json_lab_sources(labs_dir.parent, [labs_dir]),
        sut_dir=labs_dir.parent,
        # `build_inventory` reads it on the enumeration path; without it the
        # enumeration is contained and offers no hosts at all.
        inventory_settings={},
    )


def test_collect_host_ids_includes_logical_handles(tmp_path):
    ids = set(collect_host_ids([_repo(tmp_path)]))
    assert {"server47", "server103"} <= ids  # canonical
    assert {"server1", "server2"} <= ids  # logical handles


def test_collect_host_ids_by_lab_includes_logical_handles(tmp_path):
    by_lab = collect_host_ids_by_lab([_repo(tmp_path)])
    assert {"server47", "server103", "server1", "server2"} <= set(by_lab["east"])
