"""Lab dataclass and lab-loading utilities for assembling a host registry from lab data."""

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

if TYPE_CHECKING:
    from collections.abc import Iterable

    from ..host.host import Host
    from ..inventory import Inventory
    from ..labs.protocol import LabRepository
    from ..link.model import Link


LAB_SEPARATOR = "+"
"""Character combining lab names in ``--lab``, ``OTTO_LAB``, and :func:`load_lab`.

It is deliberately the same operator ``Lab.__add__`` uses to merge labs, so
one character means "combined labs" at every layer.
"""


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


@dataclass
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

    Declared last so positional construction (``Lab("name", resources, hosts,
    links)``) keeps working; leave it unset and ``__post_init__`` seeds it.
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

    def __post_init__(self) -> None:
        """Seed ``component_names`` with this lab's own name when the caller left it empty.

        So an unmerged lab is its own single component, and every lab — however
        it was built — answers ``component_names`` truthfully without callers
        having to special-case "never merged".
        """
        if not self.component_names:
            self.component_names = [self.name]

    def add_host(
        self,
        host: "Host",
    ) -> None:
        """Add a Host object to the `Lab`'s dictionary of hosts.

        Parameters
        ----------
        host : Host to add to the dictionary of hosts
        """
        if host.id in self.hosts:
            raise KeyError(
                f"Attempted to add a host with ID '{host.id}', "
                f"but this key already exists in {self.name}'s known hosts."
            ) from None

        from ..host.remote_host import RemoteHost  # lazy import avoids a module-load cycle

        if isinstance(host, RemoteHost):
            host._lab = self  # noqa: SLF001 — intra-package back-link set by Lab at host registration

        # Attribution backstop for hosts built outside the loader: container
        # hosts registered by `otto docker up` and the built-in `local` never
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

    def _assign_logical_indices(self) -> None:
        """Stamp each host's ``logical_index`` within its element-slug group.

        Delegates grouping/ordering to :func:`logical_indices` (the single source
        shared with completion), refreshes non-overridden display names, and warns
        when a canonical id shadows a different host's logical position. Idempotent.
        """
        from ..host.remote_host import RemoteHost, slug

        positions = logical_indices(self.hosts.values())
        by_group_pos: "dict[tuple[str, int], RemoteHost]" = {}
        for host in self.hosts.values():
            if not (isinstance(host, RemoteHost) and host.element):
                continue
            host.logical_index = positions.get(host.id)
            _refresh_name(host)
            if host.logical_index is not None:
                by_group_pos[(slug(host.element), host.logical_index)] = host
        # Shadow warning: a canonical id <element-slug><element_id> that resolves to
        # a DIFFERENT host than that group's element_id-th by logical index means
        # "type what you see" would reach the wrong host (only possible for a small
        # element_id colliding with a logical position — see the spec's {2,5} case).
        for host in self.hosts.values():
            if not (
                isinstance(host, RemoteHost)
                and host.logical_index is not None
                and host.element_id is not None
            ):
                continue
            key = slug(host.element)
            shadowed = self.hosts.get(f"{key}{host.element_id}")
            positional = by_group_pos.get((key, host.element_id))
            if shadowed is not None and positional is not None and shadowed is not positional:
                getLogger(__name__).warning(
                    "Host id %r shadows the display label of %r (logical %d): "
                    "typing %r reaches the id-%d host, not the labelled one.",
                    shadowed.id,
                    positional.name,
                    host.element_id,
                    shadowed.id,
                    host.element_id,
                )

    def resolve_handle(self, handle: str) -> "Host | None":
        """Resolve a typed CLI handle to a host.

        Exact canonical id wins, else the positional ``<element-slug><N>``
        form (N-th host of that element by logical index), else ``None``.
        """
        host = self.hosts.get(handle)
        if host is not None:
            return host
        import re

        from ..host.remote_host import RemoteHost, slug

        m = re.fullmatch(r"(.*?)(\d+)", handle)
        if not m:
            return None
        prefix, number = m.group(1), int(m.group(2))
        for candidate in self.hosts.values():
            if (
                isinstance(candidate, RemoteHost)
                and candidate.logical_index == number
                and slug(candidate.element) == prefix
            ):
                return candidate
        return None

    def __add__(
        self,
        other: "Lab",
    ) -> "Lab":

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
            # from "two different hosts, colliding id". Use the connection identity
            # (ip): same id + same ip = the same host (dedup, no error); same id +
            # different ip = two different machines colliding (fail loud). Only
            # RemoteHosts carry an ``ip`` and only they are merged here (built-in
            # ``local`` is injected post-merge, containers post-load).
            # (``existing is host`` would trivially share ``ip`` too, so the
            # ``existing.ip != host.ip`` check below already excludes it —
            # no separate identity check needed.)
            if (
                existing is not None
                and isinstance(existing, RemoteHost)
                and isinstance(host, RemoteHost)
                and existing.ip != host.ip
            ):
                raise ValueError(
                    f"Duplicate host id {host.id!r} for different hosts "
                    f"({existing.ip} in {pre_merge_name!r} vs {host.ip} in {other.name!r}). "
                    f"Differentiate the element string, assign/uniquify element_id, "
                    f"or set board/slot."
                )
            self.hosts[host.id] = host

        by_id = {link.id: link for link in self.links}
        by_id.update({link.id: link for link in other.links})
        self.links = list(by_id.values())

        self._assign_logical_indices()

        return self


def _refresh_name(host: "Host") -> None:
    """Recompute a non-overridden host's display name from its current logical_index."""
    if getattr(host, "_name_overridden", False):
        return
    generate = getattr(host, "_generate_name", None)
    if generate is not None:
        host.name = generate()


def logical_indices(hosts: "Iterable[Any]") -> dict[str, int]:
    """Host id -> 1-based logical index within its ``slug(element)`` group.

    Ordered by ``element_id`` ascending (``id`` tie-break); only groups with more
    than one member are numbered (a unique element is absent from the map).
    Accepts a :class:`~otto.host.remote_host.RemoteHost` or a
    :class:`~otto.labs.protocol.HostSummary` (completion numbers hosts it has
    only summarized, never built) — anything else, and any empty-``element``
    entry, is skipped. That exclusion is load-bearing: built-in hosts and
    synthesized container hosts are not remote hosts and must never take a
    positional handle. THE single source of truth for logical positions, shared
    by ``Lab._assign_logical_indices`` (stamping) and completion (handles), so
    the CLI's positional handles always match ``resolve_handle``.
    """
    from collections import defaultdict

    from ..host.remote_host import RemoteHost, slug
    from ..labs import HostSummary

    groups: "dict[str, list[Any]]" = defaultdict(list)
    for host in hosts:
        if isinstance(host, (RemoteHost, HostSummary)) and host.element:
            groups[slug(host.element)].append(host)
    positions: dict[str, int] = {}
    for members in groups.values():
        if len(members) < 2:  # noqa: PLR2004 — a group of 1 is "unique", not numbered
            continue
        ordered = sorted(members, key=lambda h: (h.element_id is None, h.element_id or 0, h.id))
        for pos, host in enumerate(ordered, start=1):
            positions[host.id] = pos
    return positions


# Imported here (after Lab is fully defined) rather than at the top of the
# module to avoid a circular-import bootstrap: json_repository imports Lab
# from this module, so this import must wait until Lab is defined.
from ..labs.composite import CompositeLabRepository, LabSource  # noqa: E402, I001 — import after Lab class definition to avoid circular-import bootstrap
from ..labs.json_repository import JsonFileLabRepository  # noqa: E402 — import after Lab class definition to avoid circular-import bootstrap


def load_lab(
    labnames: str | list[str],
    search_paths: list[Path] | None = None,
    preferences: dict[str, dict[str, Any]] | None = None,
    repository: "LabRepository | None" = None,
    inventory: "Inventory | None" = None,
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
        :func:`otto.labs.build_lab_sources`). When ``None``, a built-in json
        backend over ``search_paths`` is used — wrapped in a one-source
        :class:`~otto.labs.composite.CompositeLabRepository`, which is where
        lab existence and the declared-but-memberless rule live.
    inventory : Inventory | None
        The process inventory (:func:`otto.inventory.build_inventory`), passed
        straight through to every component load so entries carrying an
        ``inventory`` key resolve against it (spec 2026-08-28 host-inventory
        §6). ``None`` — the default — makes a referenced entry an error, which
        is exactly what a process with no ``[inventory]`` table should say.

    Returns
    -------
    Lab
        Fully defined lab instance. Every host carries a non-empty
        ``source_lab`` naming the COMPONENT lab it came from (never the
        composite) and a :class:`~otto.host.lab_info.LabInfo` in ``lab_info``
        describing that same component, and ``component_names`` lists those
        components in order.

    Notes
    -----
    The built-in ``local`` host is injected after the merge and belongs to no
    component, so it is attributed to ``component_names[0]`` — the first lab
    the caller named. Attributing it to the composite ("a+b") would name a lab
    no component owns; picking the first component keeps ``local`` inside a lab
    the caller actually selected, and for the overwhelmingly common
    single-lab case the two answers coincide.
    """
    match labnames:
        case str():
            lab_names = split_lab_names(labnames)
        case _:
            lab_names = labnames

    if repository is None:
        # One json source still goes through the composite: lab existence and
        # the declared-but-memberless rule live there and nowhere else. A
        # caller-supplied repository is used as given.
        repository = CompositeLabRepository(
            [
                LabSource(
                    label="json", repository=JsonFileLabRepository(search_paths=search_paths or [])
                )
            ]
        )

    labs = [
        repository.load_lab(name, preferences=preferences, inventory=inventory)
        for name in lab_names
    ]

    from ..host.lab_info import LabInfo  # lazy: this module is imported by host modules

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
            # element_metadata): ``LabInfo`` copies it in ``__post_init__``, so
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

    lab._assign_logical_indices()  # noqa: SLF001 — intra-package: load_lab lives beside Lab in this module

    return lab
