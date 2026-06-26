"""Behavior + conformance for the ExampleLabRepository reference backend."""

import pytest

from otto.configmodule.lab import Lab
from otto.examples.lab_repository import ExampleLabRepository
from otto.host.remote_host import RemoteHost
from otto.storage import LabNotFoundError, register_lab_repository
from otto.storage.registry import _LAB_REPOSITORIES
from otto.testing import assert_lab_repository_conforms


def test_default_demo_dataset_lists_and_loads():
    repo = ExampleLabRepository()
    assert repo.list_labs() == ["east", "west"]
    lab = repo.load_lab("east")
    assert isinstance(lab, Lab)
    assert lab.name == "east"
    assert len(lab.hosts) == 1
    host = next(iter(lab.hosts.values()))
    assert isinstance(host, RemoteHost)


def test_unknown_lab_raises_lab_not_found():
    repo = ExampleLabRepository()
    with pytest.raises(LabNotFoundError):
        repo.load_lab("does-not-exist")


def test_custom_dataset_overrides_demo():
    repo = ExampleLabRepository(labs={
        "only": [{"ip": "10.9.9.9", "element": "node", "creds": {"u": "p"},
                  "resources": ["node"]}],
    })
    assert repo.list_labs() == ["only"]
    assert "node" in repo.load_lab("only").hosts


def test_accepts_repo_dir_for_registry_compatibility(tmp_path):
    # build_lab_repository constructs a custom backend as cls(repo_dir=..., **kwargs)
    repo = ExampleLabRepository(repo_dir=tmp_path)
    assert repo.list_labs() == ["east", "west"]


def test_sample_conforms():
    assert_lab_repository_conforms(
        ExampleLabRepository(), expected_labs=["east", "west"]
    )


def test_registrable_by_name():
    register_lab_repository("example-host-source-test", ExampleLabRepository)
    try:
        assert _LAB_REPOSITORIES["example-host-source-test"] is ExampleLabRepository
    finally:
        _LAB_REPOSITORIES.pop("example-host-source-test", None)
