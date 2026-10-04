"""``docker_priority`` survives every producer that turns a host into a summary.

The docker verbs pick their parent from ``HostSummary.docker_priority``, so a
producer that drops the field silently ranks every host at ``0`` and the pick
degrades to "refuse on a tie" — with nothing else failing. Each test below
reaches one producer with a ranked host (``10``) beside an unranked docker host
(``0``) and asserts the rank arrives.
"""

from pathlib import Path

import pytest

from otto.examples.lab_repository import ExampleLabRepository
from otto.host.element import Element
from otto.host.factory import host_identity
from otto.labs import CompositeLabRepository, LabSource, host_summaries
from otto.labs.json_repository import JsonFileLabRepository
from tests._fixtures.labdata import write_lab_json

_CREDS = [{"login": "u", "password": "p"}]
_HOSTS = [
    {
        "ip": "10.0.0.1",
        "element": "ranked",
        "labs": ["e"],
        "creds": _CREDS,
        "docker_capable": True,
        "docker_priority": 10,
    },
    {
        "ip": "10.0.0.2",
        "element": "plain",
        "labs": ["e"],
        "creds": _CREDS,
        "docker_capable": True,
    },
]
_EXPECTED = {"ranked": 10, "plain": 0}


def _bare(host: dict) -> dict:
    """The host entry as an element carries it: no ``element`` / ``labs`` keys."""
    return {k: v for k, v in host.items() if k not in ("element", "labs")}


def _priorities(summaries) -> dict[str, int]:
    return {s.id: s.docker_priority for s in summaries}


@pytest.fixture
def json_repo(tmp_path: Path) -> JsonFileLabRepository:
    write_lab_json(tmp_path / "lab.json", _HOSTS, links=[])
    return JsonFileLabRepository(search_paths=[tmp_path])


def test_json_backend_summaries_carry_the_rank(json_repo):
    assert _priorities(json_repo.list_host_summaries()) == _EXPECTED


def test_composite_summaries_carry_the_rank(json_repo):
    composite = CompositeLabRepository([LabSource(label="json", repository=json_repo)])
    assert _priorities(composite.list_host_summaries()) == _EXPECTED


def test_fallback_summaries_over_a_loaded_lab_carry_the_rank(json_repo):
    """A backend with no ``list_host_summaries`` is summarised from the built hosts."""

    class _NoFastPath:
        def list_labs(self):
            return json_repo.list_labs()

        def load_lab(self, name, preferences=None, inventory=None):
            return json_repo.load_lab(name, inventory=inventory)

    assert _priorities(host_summaries(_NoFastPath())) == _EXPECTED


def test_example_backend_summaries_carry_the_rank():
    elements = [{"name": h["element"], "hosts": [_bare(h)]} for h in _HOSTS]
    repo = ExampleLabRepository(labs={"e": elements}, resources={})
    assert _priorities(repo.list_host_summaries()) == _EXPECTED


def test_host_identity_carries_the_rank():
    assert host_identity(_bare(_HOSTS[0]), Element("ranked")).docker_priority == 10
    assert host_identity(_bare(_HOSTS[1]), Element("plain")).docker_priority == 0
