"""Wiring tests: the CLI lab-source path honors [lab] and stays backward compatible."""

import json
from pathlib import Path

import pytest

from otto.configmodule.lab import load_lab
from otto.storage import (
    JsonFileLabRepository,
    LabRepositoryError,
    build_lab_repository,
    register_lab_repository,
)
from otto.storage.registry import _LAB_REPOSITORIES


def _hosts_file(path: Path, hosts: list[dict]) -> Path:
    f = path / "hosts.json"
    f.write_text(json.dumps(hosts))
    return f


def test_no_lab_block_defaults_to_json_over_merged_paths(tmp_path):
    """Backward compat: no [lab] block -> json backend over aggregated labs paths."""
    p1 = tmp_path / "r1"
    p2 = tmp_path / "r2"
    p1.mkdir()
    p2.mkdir()
    _hosts_file(
        p1,
        [
            {
                "ip": "10.10.200.11",
                "element": "orange",
                "creds": {"vagrant": "vagrant"},
                "resources": ["orange"],
                "labs": ["merged"],
            },
        ],
    )
    _hosts_file(
        p2,
        [
            {
                "ip": "10.10.200.12",
                "element": "tomato",
                "creds": {"vagrant": "vagrant"},
                "resources": ["tomato"],
                "labs": ["merged"],
            },
        ],
    )

    # lab_settings == {} (no [lab] block); aggregated search paths from both repos.
    repository = build_lab_repository({}, tmp_path, search_paths=[p1, p2])
    assert isinstance(repository, JsonFileLabRepository)

    lab = load_lab("merged", repository=repository)
    # orange + tomato from the two search paths, plus the built-in `local` host.
    assert set(lab.hosts) == {"orange", "tomato", "local"}


def test_custom_backend_selected_by_name(tmp_path):
    """A [lab] backend name selects a registered custom repository."""
    sentinel_lab_name = "from-custom"

    class DictRepo:
        def __init__(self, repo_dir, names=None):
            self.repo_dir = repo_dir
            self._names = names or []

        def load_lab(self, name, preferences=None):
            from otto.configmodule.lab import Lab

            return Lab(name=name)

        def list_labs(self):
            return list(self._names)

    register_lab_repository("dict-wiring-test", DictRepo)
    try:
        repository = build_lab_repository(
            {"backend": "dict-wiring-test", "dict-wiring-test": {"names": [sentinel_lab_name]}},
            tmp_path,
            search_paths=[tmp_path],
        )
        assert isinstance(repository, DictRepo)
        assert repository.list_labs() == [sentinel_lab_name]
        lab = load_lab(sentinel_lab_name, repository=repository)
        assert lab.name == sentinel_lab_name
    finally:
        _LAB_REPOSITORIES.pop("dict-wiring-test", None)


def test_unknown_backend_name_raises(tmp_path):
    with pytest.raises(LabRepositoryError, match="Unknown lab repository backend"):
        build_lab_repository({"backend": "nope"}, tmp_path, search_paths=[tmp_path])
