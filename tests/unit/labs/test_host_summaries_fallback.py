"""A backend that implements nothing extra still gets host enumeration.

This is the "custom backends are real" half of the capability: before it,
completion and tunnel narrowing read ``lab.json`` directly, so a CMDB-backed
or in-memory backend contributed *nothing* to either. Now ``host_summaries``
falls back to ``list_labs`` + ``load_lab`` — slower than the fast path, but
correct for any backend, with zero code from its author.
"""

from typing import Any

import pytest

from otto.config.lab import Lab
from otto.host.factory import create_host_from_dict
from otto.labs import LabNotFoundError, SupportsHostSummaries, host_summaries, list_host_ids

_CREDS = [{"login": "u", "password": "p"}]


class _MinimalRepo:
    """A LabRepository with the two required methods and nothing else."""

    def __init__(self, labs: dict[str, list[dict[str, Any]]]) -> None:
        self._labs = labs

    def load_lab(
        self,
        name: str,
        preferences: dict[str, dict[str, Any]] | None = None,
        inventory: object = None,
    ) -> Lab:
        if name not in self._labs:
            raise LabNotFoundError(name)
        lab = Lab(name=name)
        for host_data in self._labs[name]:
            lab.add_host(create_host_from_dict(host_data, preferences=preferences))
        return lab

    def list_labs(self) -> list[str]:
        return sorted(self._labs)


@pytest.fixture
def minimal_repo() -> _MinimalRepo:
    return _MinimalRepo(
        {
            "east": [
                {"ip": "10.0.0.1", "element": "router", "element_id": 1, "creds": _CREDS},
                {"ip": "10.0.0.2", "element": "shared", "creds": _CREDS},
            ],
            "west": [
                {"ip": "10.0.0.2", "element": "shared", "creds": _CREDS},
            ],
        }
    )


def test_minimal_backend_does_not_advertise_the_capability(minimal_repo):
    """Positive control: without the method, the fast path must NOT be taken."""
    assert not isinstance(minimal_repo, SupportsHostSummaries)


def test_fallback_summarizes_every_host(minimal_repo):
    """The fallback enumerates through load_lab, so ids are constructed ones.

    Exactly the backend's own hosts — a backend's ``load_lab`` returns only
    its data (otto's ``local`` is injected later, by ``config.lab.load_lab``),
    so nothing is filtered on the way through.
    """
    summaries = host_summaries(minimal_repo)
    assert [s.id for s in summaries] == ["router1", "shared"]

    shared = next(s for s in summaries if s.id == "shared")
    assert sorted(shared.labs) == ["east", "west"], "a host in two labs merges"
    assert shared.ip == "10.0.0.2"


def test_a_backend_defining_its_own_local_keeps_it():
    """A lab that defines its own ``local`` host is data, not otto's built-in.

    otto documents that a lab-defined ``local`` wins over the injected
    built-in, so enumeration must not filter the name — doing so would strip
    a real host's address from tunnel narrowing on this path while the
    capability fast path (which never filtered) kept it.
    """
    repo = _MinimalRepo(
        {"east": [{"ip": "10.9.9.9", "element": "local", "creds": _CREDS}]},
    )
    (summary,) = host_summaries(repo)
    assert summary.id == "local"
    assert summary.ip == "10.9.9.9"


def test_list_host_ids_is_the_id_only_view(minimal_repo):
    ids = list_host_ids(minimal_repo)
    assert "router1" in ids
    assert ids == sorted(ids), "ids come back sorted"


def test_a_failing_lab_is_skipped_not_raised():
    """One unloadable lab must not deny completion for the rest."""

    class _HalfBroken(_MinimalRepo):
        def load_lab(self, name, preferences=None, inventory=None):
            if name == "broken":
                raise RuntimeError("backend exploded")
            return super().load_lab(name, preferences)

    repo = _HalfBroken(
        {
            "ok": [{"ip": "10.0.0.1", "element": "fine", "creds": _CREDS}],
            "broken": [{"ip": "10.0.0.2", "element": "never", "creds": _CREDS}],
        }
    )
    ids = [s.id for s in host_summaries(repo)]
    assert "fine" in ids
    assert "never" not in ids
