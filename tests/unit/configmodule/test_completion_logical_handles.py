"""Completion enumerates canonical ids AND positional logical handles."""

import json
from pathlib import Path
from types import SimpleNamespace

from otto.configmodule.completion_cache import collect_host_ids, collect_host_ids_by_lab


def _repo(tmp_path: Path) -> SimpleNamespace:
    # Minimal fake Repo (duck-typed: collect_host_ids only reads `.labs`),
    # matching the pattern used by tests/unit/configmodule/test_completion_host_ids.py.
    labs_dir = tmp_path / "labs"
    labs_dir.mkdir()
    creds = [{"login": "u", "password": "p"}]
    (labs_dir / "lab.json").write_text(
        json.dumps(
            {
                "hosts": [
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
                ]
            }
        )
    )
    return SimpleNamespace(labs=[labs_dir])


def test_collect_host_ids_includes_logical_handles(tmp_path):
    ids = set(collect_host_ids([_repo(tmp_path)]))
    assert {"server47", "server103"} <= ids  # canonical
    assert {"server1", "server2"} <= ids  # logical handles


def test_collect_host_ids_by_lab_includes_logical_handles(tmp_path):
    by_lab = collect_host_ids_by_lab([_repo(tmp_path)])
    assert {"server47", "server103", "server1", "server2"} <= set(by_lab["east"])
