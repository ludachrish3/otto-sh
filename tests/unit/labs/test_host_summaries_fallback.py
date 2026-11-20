"""A backend that implements nothing extra still gets host enumeration.

This is the "custom backends are real" half of the capability: before it,
completion and tunnel narrowing read ``lab.json`` directly, so a CMDB-backed
or in-memory backend contributed *nothing* to either. Now ``host_summaries``
falls back to ``list_labs`` + ``load_lab`` — slower than the fast path, but
correct for any backend, with zero code from its author.
"""

from typing import Any

import pytest

from otto.host.element import Element
from otto.host.factory import create_host_from_dict
from otto.labs import LabNotFoundError, SupportsHostSummaries, host_summaries, list_host_ids
from otto.labs.loader import Lab

_CREDS = [{"login": "u", "password": "p"}]


class _MinimalRepository:
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
            element = Element(name=host_data["element"])
            entry = {k: v for k, v in host_data.items() if k != "element"}
            lab.add_host(create_host_from_dict(entry, preferences=preferences, element=element))
        return lab

    def list_labs(self) -> list[str]:
        return sorted(self._labs)


@pytest.fixture
def minimal_repository() -> _MinimalRepository:
    return _MinimalRepository(
        {
            "east": [
                {"ip": "10.0.0.1", "element": "router", "creds": _CREDS},
                {"ip": "10.0.0.2", "element": "shared", "creds": _CREDS},
            ],
            "west": [
                {"ip": "10.0.0.2", "element": "shared", "creds": _CREDS},
            ],
        }
    )


def test_minimal_backend_does_not_advertise_the_capability(minimal_repository):
    """Positive control: without the method, the fast path must NOT be taken."""
    assert not isinstance(minimal_repository, SupportsHostSummaries)


def test_fallback_summarizes_every_host(minimal_repository):
    """The fallback enumerates through load_lab, so ids are constructed ones.

    Exactly the backend's own hosts — a backend's ``load_lab`` returns only
    its data (otto's ``local`` is injected later, by ``labs.loader.load_lab``),
    so nothing is filtered on the way through.
    """
    summaries = host_summaries(repository=minimal_repository)
    assert [s.id for s in summaries] == ["router", "shared"]

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
    repository = _MinimalRepository(
        {"east": [{"ip": "10.9.9.9", "element": "local", "creds": _CREDS}]},
    )
    (summary,) = host_summaries(repository=repository)
    assert summary.id == "local"
    assert summary.ip == "10.9.9.9"


def test_list_host_ids_is_the_id_only_view(minimal_repository):
    ids = list_host_ids(repository=minimal_repository)
    assert "router" in ids
    assert ids == sorted(ids), "ids come back sorted"


def test_a_failing_lab_is_skipped_not_raised():
    """One unloadable lab must not deny completion for the rest."""

    class _HalfBroken(_MinimalRepository):
        def load_lab(self, name, preferences=None, inventory=None):
            if name == "broken":
                raise RuntimeError("backend exploded")
            return super().load_lab(name, preferences)

    repository = _HalfBroken(
        {
            "ok": [{"ip": "10.0.0.1", "element": "fine", "creds": _CREDS}],
            "broken": [{"ip": "10.0.0.2", "element": "never", "creds": _CREDS}],
        }
    )
    ids = [s.id for s in host_summaries(repository=repository)]
    assert "fine" in ids
    assert "never" not in ids


def _refuse_through(inventory: Any, *, wrapping: str) -> None:
    """Look up ``sw-1`` in *inventory* (which refuses it), raising as *wrapping* says."""
    if wrapping == "direct":
        inventory.lookup("sw-1")
    try:
        inventory.lookup("sw-1")
    except Exception as e:
        if wrapping == "explicit":
            raise RuntimeError("backend could not resolve sw-1") from e
        raise RuntimeError("backend could not resolve sw-1")  # noqa: B904 — the implicit chain under test


class _RefusingLoads:
    """A legacy backend with no summaries route whose loads let the stand-in's refusal escape."""

    def __init__(self, wrapping: str) -> None:
        self.wrapping = wrapping

    def list_labs(self) -> list[str]:
        return ["bench"]

    def load_lab(self, name, *, preferences=None, inventory=None) -> Lab:
        _refuse_through(inventory, wrapping=self.wrapping)
        raise AssertionError("unreachable")


class _RefusingSummaries(_RefusingLoads):
    """The same backend with a summaries route, which lets the refusal escape too."""

    def list_host_summaries(self, *, inventory=None) -> list:
        _refuse_through(inventory, wrapping=self.wrapping)
        raise AssertionError("unreachable")


@pytest.mark.parametrize("wrapping", ["direct", "explicit", "implicit"])
@pytest.mark.parametrize("backend", [_RefusingSummaries, _RefusingLoads])
def test_one_refusal_is_one_drop_however_a_backend_wraps_it(tmp_path, backend, wrapping):
    """A D1 refusal records its one drop; no catch site records a second (Astra I9)."""
    from otto.inventory import InventoryView
    from otto.labs import CompositeLabRepository, LabSource
    from otto.labs.drops import collecting_drops
    from tests._fixtures.named_inventory import FakeInventory

    repository = backend(wrapping)
    root = tmp_path / "proj"
    view = InventoryView(
        inventories={"default": FakeInventory({"sw-1": {"ip": "10.0.0.1"}})},
        bare_refused_by={root: str(root / ".otto" / "settings.toml")},
    )
    composite = CompositeLabRepository(
        [LabSource(label="legacy", repository=repository, project_root=root)]
    )
    with collecting_drops() as drops:
        assert composite.list_host_summaries_in_view(view=view) == []
    assert [d.where for d in drops] == ["inventory reference 'sw-1'"]
