"""The lab model and its loader: :class:`Lab`, the ``+`` that combines labs, and :func:`load_lab`.

A :class:`Lab` holds a named lab's hosts and declared links, and ``+`` merges
labs into one. :func:`split_lab_names` and :data:`LAB_SEPARATOR` are the one
grammar for a ``+``-combined lab selection. :func:`load_lab` builds a lab from
lab names through a lab source (by default the ``json`` backend over
``search_paths``), stamps each host with the component lab it came from, and
injects the built-in ``local`` host. The public paths are :class:`otto.lab.Lab`,
:func:`otto.lab.load_lab` and :func:`otto.load_lab`.

The module sits in ``otto.labs``, beside the lab sources it drives (spec
2026-10-11 placement-and-phases §3.7). Its runtime imports are the standard
library's: the host classes, the link derivation and the lab sources are
imported inside the functions that use them, so importing it, or naming
``otto.lab.Lab``, loads no host code.
"""

from dataclasses import (
    dataclass,
    field,
    replace,
)
from logging import getLogger
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
)

from typing_extensions import final

if TYPE_CHECKING:
    from ..host.host import Host
    from ..host.inventory_ref import MachineIdentity
    from ..inventory.protocol import Inventory
    from ..inventory.view import InventoryView
    from ..link.model import Link
    from .protocol import LabRepository


LAB_SEPARATOR = "+"
"""Character combining lab names in ``--lab``, ``OTTO_LAB``, and :func:`load_lab`.

It is deliberately the same operator ``Lab.__add__`` uses to merge labs, so
one character means "combined labs" at every layer.
"""


def _describe(host: "Host") -> str:
    """Name a host by its declaration, for a collision message: what the user wrote, not the id."""
    from ..host.docker_host import DockerContainerHost  # lazy: module-load cycle
    from ..host.remote_host import RemoteHost, describe_identity

    if isinstance(host, DockerContainerHost):
        parent = getattr(host.parent, "id", "?")
        return f"container {host.project}/{host.service} on {parent}"
    if isinstance(host, RemoteHost):
        return describe_identity(host.element.name, host.board, host.slot, host.ip)
    return f"host {host.id!r} ({type(host).__name__})"


def _merge_collision(
    *,
    host_id: str,
    mine: "MachineIdentity",
    mine_lab: str,
    theirs: "MachineIdentity",
    theirs_lab: str,
) -> str:
    """Return the ``+`` collision message for two machines declared under *host_id*.

    It names both addresses and, when they match (two inventories reusing one
    address), both identities as ``name/key``.
    """
    from ..host.inventory_ref import spell_ref

    hint = "Give the elements distinct names, or set board/slot."
    if mine.ip != theirs.ip:
        return (
            f"Duplicate host id {host_id!r} for different hosts "
            f"({mine.ip} in {mine_lab!r} vs {theirs.ip} in {theirs_lab!r}). {hint}"
        )
    return (
        f"Duplicate host id {host_id!r} for different machines at one address "
        f"({mine.ip} as {spell_ref(ref=mine.ref)} in {mine_lab!r} vs {theirs.ip} as "
        f"{spell_ref(ref=theirs.ref)} in {theirs_lab!r}): two inventories, or two keys of one, "
        f"name two machines. {hint}"
    )


def split_lab_names(value: str) -> list[str]:
    """Split a ``+``-combined lab selection into individual lab names.

    Each segment is stripped, so ``"a + b"`` and ``"a+b"`` are equivalent. An
    empty segment is a fail-loud error rather than a silently dropped name. The
    comma has no special meaning — it is an ordinary character in a lab name.

    Args:
        value: A lab selection such as ``"tech1"`` or ``"tech1+overlay"``.

    Returns:
        The individual lab names, in the order given.

    Raises:
        ValueError: If any segment is empty after stripping.

    >>> split_lab_names("tech1+overlay")
    ['tech1', 'overlay']
    >>> split_lab_names("tech1")
    ['tech1']
    >>> split_lab_names("a,b")
    ['a,b']
    """
    names = [segment.strip() for segment in value.split(LAB_SEPARATOR)]
    if not all(names):
        raise ValueError(
            f"Invalid lab selection {value!r}: empty lab name "
            f"(names are combined with {LAB_SEPARATOR!r}). Expected LAB[+LAB...]"
        )
    return names


@final
@dataclass(kw_only=True)
class Lab:
    """Container for a named lab environment and its registered hosts.

    A ``Lab`` aggregates the ``Host`` objects parsed from lab data files under
    a single name.  Multiple labs can be merged via ``+`` to build a composite
    environment that spans several lab data sources.
    """

    name: str
    """Name of this lab."""

    resources: set[str] = field(default_factory=set)
    """Resources required to reserve this lab."""

    hosts: "dict[str, Host]" = field(default_factory=dict)
    """Host objects, keyed by unique host id."""

    links: "list[Link]" = field(default_factory=list)
    """Declared links loaded from lab data (implicit links are derived, not stored)."""

    component_names: list[str] = field(default_factory=list)
    """The lab names this lab was assembled from, in merge order.

    A lab loaded on its own holds ``[name]``; ``a + b`` holds ``["a", "b"]``
    while ``name`` becomes ``"a+b"``. Carried rather than re-derived by
    splitting ``name`` on :data:`LAB_SEPARATOR`: the composite name is a
    display string assembled by ``__add__``, and re-parsing it would make
    every reader re-implement (and eventually disagree about) the merge.

    Leave it unset and ``__post_init__`` seeds it.
    """

    metadata: dict[str, dict[str, Any]] = field(default_factory=dict)
    """Opaque user data from each lab's ``labs`` table entry, keyed by LAB NAME.

    Keyed rather than flat because a merged lab (``a + b``) carries both labs'
    tables and a host must be able to read its OWN lab's data; otto never
    interprets the values. A lab loaded from a source that does not declare it
    contributes no key — an absent key means "not declared here", never "empty".

    Declared after ``component_names`` for the same reason that one is declared
    last: positional construction must keep working.
    """

    _identities: "dict[str, list[MachineIdentity]]" = field(
        default_factory=dict, init=False, repr=False, compare=False
    )
    """Every machine identity merged into each host id, in merge order.

    The stored host is the last one merged, but a later merge must be judged
    against every declaration it replaced, or the verdict would depend on the
    order and grouping of ``+``. Filled by :meth:`_identity_history`.
    """

    def __post_init__(self) -> None:
        """Seed ``component_names`` with this lab's own name when the caller left it empty.

        So an unmerged lab is its own single component, and every lab — however
        it was built — answers ``component_names`` truthfully without callers
        having to special-case "never merged".
        """
        if not self.component_names:
            self.component_names = [self.name]

    def __otto_summary__(self) -> str:
        """Return the one-line identity a failing ``expect()`` report shows.

        For example ``<Lab bench>``: only the name; the hosts it holds, and
        their credentials, stay out of the report.
        """
        return f"<{type(self).__name__} {self.name}>"

    def add_host(
        self,
        host: "Host",
    ) -> None:
        """Add a Host object to the `Lab`'s dictionary of hosts.

        Parameters
        ----------
        host : Host to add to the dictionary of hosts

        Raises
        ------
        LabRepositoryError
            If another declaration already composed this host's id; the
            message names both.
        """
        from ..host.remote_host import RemoteHost  # lazy import avoids a module-load cycle

        existing = self.hosts.get(host.id)
        if existing is not None:
            # lazy: collision path only; keeps this module's import light
            from .errors import LabRepositoryError

            hint = ""
            if isinstance(existing, RemoteHost) and isinstance(host, RemoteHost):
                hint = " Give the elements distinct names, or set board/slot."
            raise LabRepositoryError(
                f"host id {host.id!r} in lab {self.name!r}: {_describe(existing)} "
                f"collides with {_describe(host)}.{hint}"
            )

        if isinstance(host, RemoteHost):
            host._lab = self  # noqa: SLF001 — intra-package back-link set by Lab at host registration

        # Attribution backstop for hosts built outside the loader: container
        # hosts registered by `otto docker compose up` and the built-in `local` never
        # pass through the factory's ``lab_name``. ``or`` so an existing stamp
        # always wins — a host declared in lab "a" keeps saying "a" even when
        # some other lab registers it. Pre-merge, ``self.name`` IS the component
        # lab; a host joining a lab AFTER a merge gets the composite name
        # ("a+b"), which is the honest answer — a container registered into a
        # composite lab belongs to no single component of it.
        host.source_lab = host.source_lab or self.name

        self.hosts[host.id] = host

    def static_links(self) -> "list[Link]":
        """Return the static link layer: implicit hop edges plus declared links.

        Free (no I/O). Declared wins over implicit on route-id collision.
        Dynamic links are NOT here — see ``otto.tunnel.discovery`` (async, costed).
        """
        from ..link.derive import implicit_links  # lazy: keep Lab import-light

        merged = {link.id: link for link in implicit_links(self.hosts)}
        for link in self.links:
            merged[link.id] = link
        return list(merged.values())

    def _identity_history(self, *, host_id: str) -> "list[MachineIdentity]":
        """Return every machine identity merged under *host_id*, seeding it from the host.

        A lab that has merged nothing under *host_id* holds one identity, its
        own host's; a host that is not a
        :class:`~otto.host.remote_host.RemoteHost` (or no host) has none.
        """
        from ..host.inventory_ref import MachineIdentity
        from ..host.remote_host import RemoteHost

        if host_id not in self._identities:
            host = self.hosts.get(host_id)
            self._identities[host_id] = (
                [MachineIdentity(ip=host.ip, ref=host.inventory_ref)]
                if isinstance(host, RemoteHost)
                else []
            )
        return self._identities[host_id]

    def _replace_host(self, *, host_id: str, other: "Lab | None") -> None:
        """Replace the host under *host_id*, and its identity history, with *other*'s.

        The one place a host and the history it is judged by change together:
        the host *other* holds under *host_id* is stored with every identity
        *other* merged under it, and with ``None`` both go. A history left behind by
        a replaced host would let a later :meth:`__add__` accept a machine
        that conflicts with the surviving one.
        """
        if other is None:
            del self.hosts[host_id]
            self._identities.pop(host_id, None)
            return
        self._identities[host_id] = list(other._identity_history(host_id=host_id))  # noqa: SLF001 — another Lab, narrowed from Optional
        self.hosts[host_id] = other.hosts[host_id]

    def __add__(
        self,
        other: "Lab",
    ) -> "Lab":

        from ..host.inventory_ref import first_conflict
        from ..host.remote_host import RemoteHost

        pre_merge_name = self.name
        self.name = f"{self.name}{LAB_SEPARATOR}{other.name}"
        # Beside the name concatenation, because it repairs what the name
        # concatenation costs: the merged lab used to remember only "a+b", so
        # every question about the parts ("which labs is this?", "which lab is
        # this host from?") could only be answered by re-parsing a display
        # string. Carry the components instead — per-host attribution rides on
        # ``Host.source_lab``, stamped before the hosts ever reach this method.
        self.component_names = [*self.component_names, *other.component_names]
        self.resources = self.resources.union(other.resources)
        # Key-disjoint by construction (each key is a COMPONENT lab name, and a
        # lab is merged into itself nowhere), so a plain union is the whole
        # rule: no field-level blend of two labs' tables, ever.
        self.metadata = {**self.metadata, **other.metadata}
        for host in other.hosts.values():
            if isinstance(host, RemoteHost):
                host._lab = self
        for host in other.hosts.values():
            existing = self.hosts.get(host.id)
            # A host declared in multiple labs is reconstructed as a DISTINCT
            # object per lab, so object identity cannot tell "same host, two labs"
            # from "two different hosts, colliding id". Judge the machine
            # instead (``same_machine``): its address and inventory provenance.
            # Every identity either side has merged under this id is checked
            # against every one the other has, so ``a + (b + c)`` and
            # ``(a + b) + c`` agree. Only RemoteHosts carry an ``ip`` and only
            # they are merged here (built-in ``local`` is injected post-merge,
            # containers post-load).
            theirs = other._identity_history(host_id=host.id)
            if isinstance(existing, RemoteHost) and isinstance(host, RemoteHost):
                mine = self._identity_history(host_id=host.id)
                for incoming in theirs:
                    clash = first_conflict(incoming=incoming, seen=mine)
                    if clash is not None:
                        raise ValueError(
                            _merge_collision(
                                host_id=host.id,
                                mine=clash,
                                mine_lab=pre_merge_name,
                                theirs=incoming,
                                theirs_lab=other.name,
                            )
                        )
                self._identities[host.id] = [*mine, *theirs]
                self.hosts[host.id] = host
            else:
                self._replace_host(host_id=host.id, other=other)

        by_id = {link.id: link for link in self.links}
        by_id.update({link.id: link for link in other.links})
        self.links = list(by_id.values())

        return self


def _json_source(search_paths: list[Path]) -> "LabRepository":
    """Build a json source over *search_paths* through the lab-source registry.

    No project is selected on this path, so its hosts see no project data profiles.
    """
    from ..host.os_profile import ProfileContext
    from .registry import LAB_REPOSITORIES
    from .sources import LabSourceEnv

    prepared = LAB_REPOSITORIES.prepare(
        name="json",
        raw={"paths": [str(p) for p in search_paths]},
        env=LabSourceEnv(
            project_root=Path.cwd(),
            label="json",
            origin="load_lab(search_paths=)",
            profiles=ProfileContext.empty(),
        ),
        source="load_lab(search_paths=)",
    )
    return LAB_REPOSITORIES.build(prepared=prepared)


def _load_components(
    lab_names: list[str],
    *,
    repository: "LabRepository",
    preferences: dict[str, dict[str, Any]] | None,
    view: "InventoryView",
) -> list[Lab]:
    """Load each of *lab_names* from *repository* through *view*, in order.

    A composite decides availability once, in one status report, for every
    component; any other repository loads through
    :func:`~otto.labs.load_lab_through`.
    """
    from .composite import CompositeLabRepository
    from .views import load_lab_through

    if isinstance(repository, CompositeLabRepository):
        report = repository.lab_status_report(view=view)
        return [
            repository.load_lab_reported(name, preferences=preferences, view=view, report=report)
            for name in lab_names
        ]
    return [
        load_lab_through(repository=repository, name=name, preferences=preferences, view=view)
        for name in lab_names
    ]


def load_lab(
    labnames: str | list[str],
    *,
    search_paths: list[Path] | None = None,
    preferences: dict[str, dict[str, Any]] | None = None,
    repository: "LabRepository | None" = None,
    inventory: "Inventory | None" = None,
    view: "InventoryView | None" = None,
) -> Lab:
    """
    Build a Lab object from one or more lab names.

    Parameters
    ----------
    labnames : str | list[str]
        Name(s) of lab data to retrieve. A string is split on ``+``
        (see :func:`split_lab_names`); a list is used as-is.
    search_paths : list[Path] | None
        Directories searched by the default json backend. Ignored when
        ``repository`` is supplied.
    preferences : dict[str, dict[str, Any]] | None
        The unified ``{selector: {capability: [...] | option_table: {key: val}}}``
        product-preference table applied to every host in the resulting lab.
        ``None`` reproduces today's behavior.
    repository : LabRepository | None
        A pre-built host-source backend (e.g. from
        :func:`otto.labs.build_lab_sources`). When ``None``, the ``json``
        backend registered in :data:`~otto.labs.registry.LAB_REPOSITORIES`
        is built over ``search_paths`` (relative entries anchor to the
        current directory) — wrapped in a one-source
        :class:`~otto.labs.composite.CompositeLabRepository`, which is where
        lab existence and the declared-but-memberless rule live. With no
        ``search_paths`` the composite has no source, and loading fails with
        configuration guidance.
    inventory : Inventory | None
        The process inventory (:func:`otto.inventory.build_inventory`), passed
        straight through to every component load so entries carrying an
        ``inventory`` key resolve against it (spec 2026-08-28 host-inventory
        §6). ``None`` — the default — makes a referenced entry an error, which
        is exactly what a process with no ``[inventory]`` table should say.
        It stands for a view holding it alone as ``default``
        (:meth:`~otto.inventory.InventoryView.of_default`).
    view : InventoryView | None
        The process inventory view (:func:`otto.inventory.build_inventory_view`)
        every component resolves its references through, named inventories
        included. Give *inventory* or *view*, not both.

    Returns
    -------
    Lab
        Fully defined lab instance. Every host carries a non-empty
        ``source_lab`` naming the COMPONENT lab it came from (never the
        composite) and a :class:`~otto.host.lab_info.LabInfo` in ``lab_info``
        describing that same component, and ``component_names`` lists those
        components in order.

    Raises
    ------
    ValueError
        If both *inventory* and *view* are given.
    LabUnavailableError
        If a :class:`~otto.labs.composite.CompositeLabRepository` reports a
        component unavailable.

    Notes
    -----
    For a :class:`~otto.labs.composite.CompositeLabRepository` (every
    configured process), availability is decided once for the whole
    selection: :meth:`~otto.labs.composite.CompositeLabRepository.lab_status_report`
    runs once, however many components ``a+b`` names, and each component
    loads through
    :meth:`~otto.labs.composite.CompositeLabRepository.load_lab_reported`.
    Any other repository loads each component through
    :func:`~otto.labs.load_lab_through`, with no report: a legacy backend's
    labs are all available.

    The built-in ``local`` host is injected after the merge and belongs to no
    component, so it is attributed to ``component_names[0]`` — the first lab
    the caller named. Attributing it to the composite ("a+b") would name a lab
    no component owns; picking the first component keeps ``local`` inside a lab
    the caller actually selected, and for the overwhelmingly common
    single-lab case the two answers coincide.
    """
    if inventory is not None and view is not None:
        raise ValueError("load_lab() takes inventory= or view=, not both")
    match labnames:
        case str():
            lab_names = split_lab_names(labnames)
        case _:
            lab_names = labnames

    if repository is None:
        # Imported inside the call, as every collaborator here is: the json
        # backend a source builds imports `otto.host.factory`, and importing
        # this module (or naming `otto.lab.Lab`) must not load the otto.host.*
        # subtree on a surface that never loads a lab.
        from .composite import CompositeLabRepository, LabSource

        # One json source still goes through the composite: lab existence and
        # the declared-but-memberless rule live there and nowhere else. A
        # caller-supplied repository is used as given. With no search paths
        # the composite is empty, and its load_lab fails loud with guidance.
        sources: list[LabSource] = []
        if search_paths:
            sources.append(LabSource(label="json", repository=_json_source(search_paths)))
        repository = CompositeLabRepository(sources)

    if view is None:
        from ..inventory.view import InventoryView

        view = InventoryView.of_default(inventory=inventory)
    labs = _load_components(lab_names, repository=repository, preferences=preferences, view=view)

    # lazy: keeps ``import otto.labs.loader`` from loading otto.host
    # (tests/unit/test_import_contracts.py pins this)
    from ..host.lab_info import LabInfo

    # Attribution sweep, BEFORE the merge. After ``+`` every host lives in a lab
    # named "a+b", so a sweep run one line later would answer "which lab is this
    # host from?" with the composite for every host — the exact erasure
    # ``source_lab`` exists to prevent. Running it per component also covers
    # backends that build hosts without the factory's ``lab_name`` or drop them
    # straight into ``Lab.hosts`` (bypassing ``add_host``'s own backstop).
    # ``lab_info`` is stamped in the same sweep and for the same reason: it
    # names the COMPONENT lab (spec §4), which only exists before the merge.
    for component in labs:
        resources = frozenset(component.resources)
        declared_metadata = component.metadata.get(component.name, {})
        for component_host in component.hosts.values():
            component_host.source_lab = component_host.source_lab or component.name
            # A per-host COPY of the metadata table (mutation isolation, like
            # the element's own metadata): ``LabInfo`` copies it in ``__post_init__``, so
            # one host's write into it can never reach the lab's table or a
            # sibling host's.
            component_host.lab_info = LabInfo(
                name=component.name,
                resources=resources,
                metadata=declared_metadata,
            )

    # Captured BEFORE the merge, for the built-in `local` host injected after
    # it: ``+`` mutates ``labs[0]`` in place, so afterwards its ``name`` is the
    # composite ("a+b") and its ``resources`` the union — neither of which
    # describes the first COMPONENT any more.
    first_component_info = LabInfo(
        name=labs[0].name,
        resources=frozenset(labs[0].resources),
        metadata=labs[0].metadata.get(labs[0].name, {}),
    )

    lab = labs[0]
    for additional_lab in labs[1:]:
        lab += additional_lab

    # Inject the built-in `local` host so `otto host local <verb>` resolves in any
    # lab, on any backend, without a custom lab-repository. Inject-if-absent: a lab
    # that defines its own `local` host wins.
    from ..host.builtin_hosts import BUILTIN_LOCAL_HOST_ID, make_builtin_local_host

    if BUILTIN_LOCAL_HOST_ID not in lab.hosts:
        local_host = make_builtin_local_host()
        # `local` is injected after the merge, so the lab it joins is the
        # COMPOSITE ("a+b") — a name no component owns and no lab-scoped filter
        # will ever match. Attribute it to the first component instead (see the
        # docstring); the explicit stamp also pre-empts ``add_host``'s
        # composite-name backstop.
        #
        # BOTH attribution channels, from the SAME component: the sweep above
        # runs pre-merge and never sees this host, so leaving ``lab_info`` at
        # its empty default would have the one host present in every lab
        # disagreeing with itself about which lab it is in.
        local_host.source_lab = lab.component_names[0]
        # ``replace`` rather than the object itself: it re-runs
        # ``LabInfo.__post_init__``, so `local` gets its own metadata dict
        # instead of aliasing the one every host of that component holds.
        local_host.lab_info = replace(first_component_info)
        lab.add_host(local_host)
    else:
        getLogger(__name__).debug(
            "Lab %r defines its own %r host; skipping the built-in local host.",
            lab.name,
            BUILTIN_LOCAL_HOST_ID,
        )

    return lab
