"""HostSummary carries the entry's os_type so completion can scope host verbs by class."""

from pathlib import Path
from types import SimpleNamespace

from otto.config import completion_cache as cc
from otto.labs import HostSummary
from tests._fixtures.labdata import json_lab_sources, write_lab_json

_CREDS = [{"login": "u", "password": "p"}]


def _repo(tmp_path: Path, hosts: list[dict]) -> SimpleNamespace:
    lab = tmp_path / "lab"
    lab.mkdir()
    write_lab_json(lab / "lab.json", hosts)
    return SimpleNamespace(
        lab_sources=json_lab_sources(tmp_path, [lab]),
        sut_dir=tmp_path,
        inventory_settings={},
    )


def _summaries(repo) -> dict[str, HostSummary]:
    return {s.id: s for s in cc.repo_host_summaries(repo, cc.resolve_process_inventory([repo]))}


def test_json_backend_records_the_declared_os_type(tmp_path):
    repo = _repo(
        tmp_path,
        [{"ip": "1.1.1.1", "element": "z1", "labs": ["e"], "creds": _CREDS, "os_type": "zephyr"}],
    )
    assert _summaries(repo)["z1"].os_type == "zephyr"


def test_json_backend_applies_the_factory_default_when_none_is_declared(tmp_path):
    repo = _repo(tmp_path, [{"ip": "1.1.1.1", "element": "u1", "labs": ["e"], "creds": _CREDS}])
    assert _summaries(repo)["u1"].os_type == "unix"


def test_the_field_defaults_to_none_for_backends_that_do_not_know():
    assert HostSummary(id="x").os_type is None


def test_composite_copies_os_type_across_sources(tmp_path):
    from otto.labs import build_lab_sources

    repo = _repo(
        tmp_path,
        [{"ip": "1.1.1.1", "element": "z1", "labs": ["e"], "creds": _CREDS, "os_type": "zephyr"}],
    )
    composite = build_lab_sources([repo])
    got = {s.id: s.os_type for s in composite.list_host_summaries()}
    assert got == {"z1": "zephyr"}


def test_the_example_backend_reports_each_records_os_type():
    """The reference backend is what a SUT author copies, so its summaries must
    carry the selector — declared, and defaulted the way the factory defaults it."""
    from otto.examples.lab_repository import ExampleLabRepository

    repo = ExampleLabRepository(
        labs={
            "e": [
                {"ip": "1.1.1.1", "element": "z", "creds": _CREDS, "os_type": "zephyr"},
                {"ip": "1.1.1.2", "element": "u", "creds": _CREDS},
            ]
        },
        resources={},
    )
    assert {s.id: s.os_type for s in repo.list_host_summaries()} == {"z": "zephyr", "u": "unix"}


def test_load_lab_fallback_records_the_built_hosts_os_type():
    """A backend with no ``list_host_summaries`` still reports a real
    ``os_type``: the fallback builds an actual host (``create_host_from_dict``,
    which applies the factory's ``"unix"`` default) and reads it back off the
    host object, not off the raw record — so it must not report ``None`` for a
    host that plainly has one."""
    from otto.config.lab import Lab
    from otto.host.factory import create_host_from_dict
    from otto.labs import host_summaries

    class _MinimalRepo:
        """A LabRepository with only the two required methods (no fast path)."""

        def load_lab(self, name, preferences=None, inventory=None):
            lab = Lab(name=name)
            lab.add_host(create_host_from_dict({"ip": "1.1.1.1", "element": "u1", "creds": _CREDS}))
            return lab

        def list_labs(self):
            return ["only"]

    summaries = {s.id: s for s in host_summaries(_MinimalRepo())}
    assert summaries["u1"].os_type == "unix"
