"""Behavior + conformance for the ExampleLabRepository reference backend."""

import pytest

from otto.config.lab import Lab
from otto.examples.lab_repository import ExampleLabRepository
from otto.host.remote_host import RemoteHost
from otto.labs import LabNotFoundError, register_lab_repository
from otto.labs.registry import LAB_REPOSITORIES
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
    repo = ExampleLabRepository(
        labs={
            "only": [
                {
                    "name": "node",
                    "hosts": [{"ip": "10.9.9.9", "creds": [{"login": "u", "password": "p"}]}],
                }
            ],
        },
        # The LAB level of three (spec 2026-08-28 three-level-reservations §2):
        # this kwarg is the ``labs`` table's ``resources``. A host entry MAY
        # carry its own, and an element's set rides on the ``Element`` the
        # backend hands the factory — this sample does neither, so the built
        # host's two sets stay empty and the assertion below is about the lab's
        # declaration alone.
        resources={"only": {"node"}},
    )
    assert repo.list_labs() == ["only"]
    lab = repo.load_lab("only")
    assert "node" in lab.hosts
    assert lab.resources == {"node"}


def test_an_elements_own_data_reaches_every_host_it_groups():
    """The dataset is by ELEMENT since spec 2026-09-05 §2.6.

    A SUT author copies this backend, so the sample must show the shape that
    matters: one element dict, its ``id``/``metadata``/``resources``, and the
    hosts it groups — all of which reach the hosts as one shared ``Element``.
    """
    repo = ExampleLabRepository(
        labs={
            "rig": [
                {
                    "name": "chassis",
                    "id": 7,
                    "metadata": {"rack": "B4"},
                    "resources": ["chassis-7"],
                    "hosts": [
                        {"ip": "10.9.9.1", "board": "cpu", "creds": [{"login": "u"}]},
                        {"ip": "10.9.9.2", "board": "io", "creds": [{"login": "u"}]},
                    ],
                }
            ]
        },
        resources={},
    )
    lab = repo.load_lab("rig")
    cpu, io = lab.hosts["chassis_cpu"], lab.hosts["chassis_io"]
    assert cpu.element is io.element  # one Element per element dict, shared
    assert (cpu.element.name, cpu.element.id) == ("chassis", 7)
    assert cpu.element.metadata == {"rack": "B4"}
    assert cpu.element.resources == frozenset({"chassis-7"})
    assert {s.id for s in repo.list_host_summaries()} == {"chassis_cpu", "chassis_io"}


def test_accepts_repo_dir_for_registry_compatibility(tmp_path):
    # build_lab_sources constructs a custom backend as cls(repo_dir=..., **kwargs)
    repo = ExampleLabRepository(repo_dir=tmp_path)
    assert repo.list_labs() == ["east", "west"]


def test_sample_conforms():
    assert_lab_repository_conforms(ExampleLabRepository(), expected_labs=["east", "west"])


def test_registrable_by_name():
    register_lab_repository("example-host-source-test", ExampleLabRepository)
    try:
        assert LAB_REPOSITORIES.get("example-host-source-test") is ExampleLabRepository
    finally:
        LAB_REPOSITORIES.unregister("example-host-source-test")


def test_list_host_summaries_applies_the_factory_default_when_none_is_declared():
    """Neither demo host declares ``os_type``, so the fast path applies the
    same default (``"unix"``) the factory would apply when building the host."""
    summaries = {s.id: s for s in ExampleLabRepository().list_host_summaries()}
    assert summaries["router1"].os_type == "unix"


def test_list_host_summaries_skips_a_malformed_element_entry():
    """Enumeration feeds tab completion, so one bad ELEMENT dict must not deny the rest.

    The dataset is caller-supplied and unvalidated, and the element is built
    before any host of it is: a missing ``name``/``hosts`` key raises
    ``KeyError`` and a name that slugs to nothing raises ``ValueError``, both
    outside the factory call the per-record ``try`` was written for. Two bad
    entries beside one good one, so a guard that only covered the first kind
    still reddens.
    """
    repo = ExampleLabRepository(
        labs={
            "mixed": [
                # No "name": KeyError inside _element_of.
                {"hosts": [{"ip": "10.0.0.1", "creds": [{"login": "u"}]}]},
                # A name that slugs empty: ValueError from Element.__post_init__.
                {"name": "___", "hosts": [{"ip": "10.0.0.2", "creds": [{"login": "u"}]}]},
                # No "hosts": KeyError on the element entry, after a valid Element.
                {"name": "hostless"},
                {"name": "good", "hosts": [{"ip": "10.0.0.3", "creds": [{"login": "u"}]}]},
            ]
        },
        resources={},
    )
    summaries = repo.list_host_summaries()
    assert [s.id for s in summaries] == ["good"]
    assert summaries[0].ip == "10.0.0.3"
