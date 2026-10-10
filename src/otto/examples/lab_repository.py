"""In-memory reference :class:`~otto.labs.protocol.LabRepository` (sample).

A teaching/reference host-source backend: it holds a mapping of lab name to a
list of element dicts, each grouping its own host dicts, and builds real hosts
via :func:`otto.host.factory.create_host_from_dict`. It needs no files or
network, so it runs inside doctests and the conformance suite, and SUT authors
can copy it as a starting point.

Register it from an ``init`` module, with the model that parses a source's
options and the factory that builds the source from them, and select it by
name::

    from otto.labs import register_lab_repository
    from otto.examples.lab_repository import ExampleLabSourceConfig, example_lab_source

    register_lab_repository("example", config=ExampleLabSourceConfig, factory=example_lab_source)

then in ``.otto/settings.toml`` (every key but ``backend`` and ``name`` is an
option the config model parses; this sample's are both optional)::

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

from typing import TYPE_CHECKING, Any

from pydantic import ConfigDict

from ..host import Element, create_host_from_dict, host_identity
from ..inventory import InventoryError, resolve_host_entry
from ..lab import Lab
from ..labs import HostSummary, LabNotFoundError, logins_of_host_data
from ..models import OttoModel

if TYPE_CHECKING:
    from ..host import ProfileContext
    from ..inventory import Inventory
    from ..labs import LabSourceEnv
    from ..registry import Configured

__all__ = ["ExampleLabRepository", "ExampleLabSourceConfig", "example_lab_source"]

# A tiny built-in dataset so the sample works out of the box (doctests +
# conformance). Each value is a list of ELEMENT dicts, each carrying the host
# dicts of that element as they'd appear in a lab.json entry; the mapping key
# supplies lab membership here, so the element-level "labs" field is
# unnecessary. This sample declares resources only at the LAB level, in
# ``_DEMO_RESOURCES`` below — one of the three levels a lab may use (spec
# 2026-08-28 three-level-reservations §2). A host dict MAY carry its own
# "resources", and an element's metadata and resources ride on the ``Element``
# handed to the factory; a backend whose equipment is reserved per chassis or
# per slot has to populate those too, because otto reads all three off the
# built lab.
_DEMO_LABS: dict[str, list[dict[str, Any]]] = {
    "east": [
        {
            "name": "router1",
            "hosts": [{"ip": "10.0.0.1", "creds": [{"login": "admin", "password": "admin"}]}],
        },
    ],
    "west": [
        {
            "name": "router2",
            "hosts": [
                {
                    "ip": "10.0.1.1",
                    "creds": [{"login": "admin", "password": "admin"}],
                    # Spelled out although "unix" is the factory's default: this
                    # selector is what `list_host_summaries` reports and what
                    # scopes the verb menu of `otto host router2 <TAB>`. A
                    # backend that drops it keeps working and quietly offers
                    # every class's verbs.
                    "os_type": "unix",
                },
            ],
        },
    ],
}


def _element_of(entry: dict[str, Any]) -> Element:
    """Build the runtime element an element dict of the dataset declares."""
    return Element(
        entry["name"],
        id=entry.get("id"),
        metadata=dict(entry.get("metadata", {})),
        resources=frozenset(entry.get("resources", ())),
    )


_DEMO_RESOURCES: dict[str, set[str]] = {"east": {"router1"}, "west": {"router2"}}
"""What each demo lab reserves — the ``labs`` table's ``resources``, in miniature."""


class ExampleLabRepository:
    """In-memory :class:`~otto.labs.protocol.LabRepository` reference backend.

    Parameters
    ----------
    labs : dict[str, list[dict]] | None
        Optional mapping of lab name to element dicts (``name``, optional
        ``id``/``metadata``/``resources``, and the ``hosts`` list). Defaults to
        a small built-in demo dataset.
    resources : dict[str, set[str]] | None
        Optional mapping of lab name to the resources that lab reserves — the
        ``labs`` table's ``resources``, the LAB level of the three a lab may
        declare. This sample uses no other; a backend whose hosts or elements
        are separately reservable stamps those on the hosts it builds.
        Defaults to the demo dataset's own table.
    profiles : ProfileContext | None
        The repo data profiles a host's ``os_type`` may name — what the
        factory hands a source as ``env.profiles``. Every call that resolves
        an ``os_type`` passes them on (``profiles=``), so a host may select an
        ``[os_profiles]`` table any selected repo declares. Defaults to none.
    """

    def __init__(
        self,
        *,
        labs: dict[str, list[dict[str, Any]]] | None = None,
        resources: dict[str, set[str]] | None = None,
        profiles: "ProfileContext | None" = None,
    ) -> None:
        self._profiles = profiles
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
        for entry in self._labs[name]:
            # One Element per element dict, shared by every host of it — what
            # the lab loader does, and what makes ``host_a.element is
            # host_b.element`` hold for siblings.
            element = _element_of(entry)
            for host_data in entry["hosts"]:
                resolved = resolve_host_entry(host_data, inventory, element)
                host = create_host_from_dict(
                    resolved.host_data,
                    preferences=preferences,
                    lab_name=name,
                    element=element,
                    inventory_ref=resolved.ref,
                    profiles=self._profiles,
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
        note the two ``try`` blocks: enumeration feeds tab completion, so one
        bad record must be skipped, never raised — an entry this process's
        *inventory* cannot resolve included.

        There are TWO because there are two records. The dataset is
        caller-supplied and never validated, so the ELEMENT entry can be bad on
        its own: ``_element_of`` raises ``KeyError`` without a ``name`` and
        ``ValueError`` on a name that slugs to nothing, before any host of it
        is looked at. That skip is per element, and the inner one stays per
        host, so one bad host does not take its siblings down with it.
        """
        by_id: dict[str, HostSummary] = {}
        for name, entries in self._labs.items():
            for entry in entries:
                try:
                    element = _element_of(entry)
                    hosts = entry["hosts"]
                except (ValueError, TypeError, KeyError):
                    continue
                for host_data in hosts:
                    try:
                        resolved = resolve_host_entry(host_data, inventory, element).host_data
                        identity = host_identity(resolved, element, profiles=self._profiles)
                    except (ValueError, TypeError, KeyError, InventoryError):
                        continue
                    existing = by_id.get(identity.id)
                    if existing is not None:
                        existing.labs.append(name)
                        continue
                    by_id[identity.id] = HostSummary(
                        id=identity.id,
                        labs=[name],
                        ip=identity.ip,
                        docker_capable=identity.docker_capable,
                        docker_priority=identity.docker_priority,
                        os_type=str(resolved.get("os_type", "unix")),
                        logins=logins_of_host_data(resolved),
                    )
        return sorted(by_id.values(), key=lambda s: s.id)


class ExampleLabSourceConfig(OttoModel):
    """The options a ``backend = "example"`` source takes: the sample's own two.

    Otto parses a source's options with this model when it prepares the
    source, after init, and refuses an unknown key (``OttoModel`` forbids
    extras). Frozen, and deep-copyable as every lab-source config model must
    be: otto hands the factory a fresh copy at each build.
    """

    model_config = ConfigDict(frozen=True)

    labs: dict[str, list[dict[str, Any]]] | None = None
    """Lab name to element dicts; ``None`` keeps the built-in demo dataset."""

    resources: dict[str, list[str]] | None = None
    """Lab name to the resources that lab reserves; ``None`` keeps the demo table."""


def example_lab_source(
    c: "Configured[ExampleLabSourceConfig, LabSourceEnv]",
) -> ExampleLabRepository:
    """Build an :class:`ExampleLabRepository` from a source's parsed options.

    ``c.env`` carries the declaring repo's root (``repo_dir``), the source's
    ``label`` and its ``origin``, which this in-memory sample needs none of
    (it reads no file), and the selected repos' data ``profiles``, which it
    keeps: a host's ``os_type`` may name an ``[os_profiles]`` table.
    """
    resources = c.config.resources
    return ExampleLabRepository(
        labs=c.config.labs,
        resources=None if resources is None else {k: set(v) for k, v in resources.items()},
        profiles=c.env.profiles,
    )
