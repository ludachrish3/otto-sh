"""In-memory reference :class:`~otto.labs.protocol.LabRepository` (sample).

A teaching/reference host-source backend: it holds a mapping of lab name to a
list of host dicts and builds real hosts via
:func:`otto.host.factory.create_host_from_dict`. It needs no files or network, so it
runs inside doctests and the conformance suite, and SUT authors can copy it as a
starting point.

Register it from an ``init`` module and select it by name::

    from otto.labs import register_lab_repository
    from otto.examples.lab_repository import ExampleLabRepository

    register_lab_repository("example", ExampleLabRepository)

then in ``.otto/settings.toml``::

    [[lab.sources]]
    backend = "example"

Direct usage:

>>> from otto.examples.lab_repository import ExampleLabRepository
>>> repo = ExampleLabRepository()
>>> repo.list_labs()
['east', 'west']
>>> lab = repo.load_lab("east")
>>> lab.name
'east'
>>> len(lab.hosts)
1
>>> sorted(lab.resources)
['router1']
>>> [s.id for s in repo.list_host_summaries()]
['router1', 'router2']
"""

from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..config.lab import Lab
from ..host.factory import create_host_from_dict, host_identity
from ..inventory import InventoryError, resolve_host_entry
from ..labs import HostSummary, LabNotFoundError

if TYPE_CHECKING:
    from ..inventory import Inventory

# A tiny built-in dataset so the sample works out of the box (doctests +
# conformance). Each value is a list of host dicts as they'd appear in a
# lab.json entry; the mapping key supplies lab membership here, so the
# host-level "labs" field is unnecessary. This sample declares resources only
# at the LAB level, in ``_DEMO_RESOURCES`` below — one of the three levels a
# lab may use (spec 2026-08-28 three-level-reservations §2). A host dict MAY
# carry its own "resources", and an element's set reaches a host through
# ``create_host_from_dict(..., element_resources=...)``; a backend whose
# equipment is reserved per chassis or per slot has to populate those too,
# because otto reads all three off the built lab.
_DEMO_LABS: dict[str, list[dict[str, Any]]] = {
    "east": [
        {
            "ip": "10.0.0.1",
            "element": "router1",
            "creds": [{"login": "admin", "password": "admin"}],
        },
    ],
    "west": [
        {
            "ip": "10.0.1.1",
            "element": "router2",
            "creds": [{"login": "admin", "password": "admin"}],
            # Spelled out although "unix" is the factory's default: this selector
            # is what `list_host_summaries` reports and what scopes the verb menu
            # of `otto host router2 <TAB>`. A backend that drops it keeps working
            # and quietly offers every class's verbs.
            "os_type": "unix",
        },
    ],
}

_DEMO_RESOURCES: dict[str, set[str]] = {"east": {"router1"}, "west": {"router2"}}
"""What each demo lab reserves — the ``labs`` table's ``resources``, in miniature."""


class ExampleLabRepository:
    """In-memory :class:`~otto.labs.protocol.LabRepository` reference backend.

    Parameters
    ----------
    repo_dir : Path | None
        Accepted for factory/registry uniformity — :func:`otto.labs.build_lab_sources`
        constructs a custom backend as ``cls(repo_dir=..., **kwargs)``. This
        in-memory sample has no files to resolve, so it is ignored.
    labs : dict[str, list[dict]] | None
        Optional mapping of lab name to host dicts. Defaults to a small built-in
        demo dataset.
    resources : dict[str, set[str]] | None
        Optional mapping of lab name to the resources that lab reserves — the
        ``labs`` table's ``resources``, the LAB level of the three a lab may
        declare. This sample uses no other; a backend whose hosts or elements
        are separately reservable stamps those on the hosts it builds.
        Defaults to the demo dataset's own table.
    """

    def __init__(
        self,
        *,
        repo_dir: Path | None = None,  # noqa: ARG002 — required by registry-seam constructor signature (build_lab_sources passes repo_dir= to all backends)
        labs: dict[str, list[dict[str, Any]]] | None = None,
        resources: dict[str, set[str]] | None = None,
    ) -> None:
        self._labs: dict[str, list[dict[str, Any]]] = (
            {k: list(v) for k, v in _DEMO_LABS.items()} if labs is None else labs
        )
        self._resources: dict[str, set[str]] = {
            k: set(v) for k, v in (_DEMO_RESOURCES if resources is None else resources).items()
        }

    def load_lab(
        self,
        name: str,
        preferences: dict[str, dict[str, Any]] | None = None,
        inventory: "Inventory | None" = None,
    ) -> Lab:
        """Build and return a ``Lab`` from the in-memory dataset.

        Records here are complete, so resolution is a pass-through; a backend
        whose records reference the inventory resolves them exactly like this
        — one :func:`~otto.inventory.resolve_host_entry` call per entry,
        before the factory, with the returned ``ref`` handed to it as
        ``inventory_ref``. Doing it here rather than in the factory is what
        keeps the join in ONE place per backend (spec §6).

        Raises
        ------
        LabNotFoundError
            If ``name`` is not in this backend's dataset.
        """
        if name not in self._labs:
            known = ", ".join(sorted(self._labs)) or "(none)"
            raise LabNotFoundError(f"Lab {name!r} not found. Known labs: {known}")
        lab = Lab(name=name)
        for host_data in self._labs[name]:
            entry = resolve_host_entry(host_data, inventory)
            host = create_host_from_dict(
                entry.host_data,
                preferences=preferences,
                lab_name=name,
                inventory_ref=entry.ref,
            )
            lab.add_host(host)
        # Declared, never derived: the lab carries its own set (spec §8.1), and
        # UNIONING the hosts' sets into it is exactly what v2 removed. The
        # element and host levels are not folded in here either — they stay on
        # the hosts, where the gate reads them (spec 2026-08-28
        # three-level-reservations §3). A copy, so a caller cannot mutate the
        # table.
        lab.resources = set(self._resources.get(name, set()))
        return lab

    def list_labs(self) -> list[str]:
        """Return a sorted list of all lab names in this backend's dataset."""
        return sorted(self._labs)

    def list_host_summaries(self, inventory: "Inventory | None" = None) -> list[HostSummary]:
        """Enumerate hosts without building them — the optional fast path.

        Implementing :class:`~otto.labs.protocol.SupportsHostSummaries` is
        what makes ``otto host <TAB>`` and tunnel path-narrowing cheap for a
        custom backend. It is optional: drop this method and otto falls back
        to ``list_labs`` + ``load_lab``, which still works.

        Note the ids come from :func:`~otto.host.factory.host_identity`, not
        from formatting the record by hand — that is what guarantees an id
        offered by completion is one ``load_lab`` will actually produce. And
        note the per-record ``try``: enumeration feeds tab completion, so one
        bad record must be skipped, never raised — an entry this process's
        *inventory* cannot resolve included.
        """
        by_id: dict[str, HostSummary] = {}
        for name, hosts in self._labs.items():
            for host_data in hosts:
                try:
                    resolved = resolve_host_entry(host_data, inventory).host_data
                    identity = host_identity(resolved)
                except (ValueError, TypeError, InventoryError):
                    continue
                existing = by_id.get(identity.id)
                if existing is not None:
                    existing.labs.append(name)
                    continue
                by_id[identity.id] = HostSummary(
                    id=identity.id,
                    labs=[name],
                    ip=identity.ip,
                    element=identity.element,
                    element_id=identity.element_id,
                    docker_capable=identity.docker_capable,
                    os_type=str(resolved.get("os_type", "unix")),
                )
        return sorted(by_id.values(), key=lambda s: s.id)
