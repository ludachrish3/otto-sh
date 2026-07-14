"""Lab dataclass and lab-loading utilities for assembling a host registry from lab data."""

from dataclasses import (
    dataclass,
    field,
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
        self.resources = self.resources.union(other.resources)
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
    Duck-typed on ``element``/``element_id``/``id``; non-``RemoteHost`` or
    empty-``element`` hosts are skipped. THE single source of truth for logical
    positions, shared by ``Lab._assign_logical_indices`` (stamping) and completion
    (handles), so the CLI's positional handles always match ``resolve_handle``.
    """
    from collections import defaultdict

    from ..host.remote_host import RemoteHost, slug

    groups: "dict[str, list[Any]]" = defaultdict(list)
    for host in hosts:
        if isinstance(host, RemoteHost) and host.element:
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
from ..labs.json_repository import JsonFileLabRepository  # noqa: E402, I001 — import after Lab class definition to avoid circular-import bootstrap


def load_lab(
    labnames: str | list[str],
    search_paths: list[Path] | None = None,
    preferences: dict[str, dict[str, Any]] | None = None,
    repository: "LabRepository | None" = None,
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
        :func:`otto.labs.build_lab_repository`). When ``None``, a built-in
        json backend over ``search_paths`` is used — preserving library/script
        behavior.

    Returns
    -------
    Lab
        Fully defined lab instance.
    """
    match labnames:
        case str():
            lab_names = split_lab_names(labnames)
        case _:
            lab_names = labnames

    if repository is None:
        repository = JsonFileLabRepository(search_paths=search_paths or [])

    labs = [repository.load_lab(name, preferences=preferences) for name in lab_names]
    lab = labs[0]
    for additional_lab in labs[1:]:
        lab += additional_lab

    # Inject the built-in `local` host so `otto host local <verb>` resolves in any
    # lab, on any backend, without a custom lab-repository. Inject-if-absent: a lab
    # that defines its own `local` host wins.
    from ..host.builtin_hosts import BUILTIN_LOCAL_HOST_ID, make_builtin_local_host

    if BUILTIN_LOCAL_HOST_ID not in lab.hosts:
        lab.add_host(make_builtin_local_host())
    else:
        getLogger(__name__).debug(
            "Lab %r defines its own %r host; skipping the built-in local host.",
            lab.name,
            BUILTIN_LOCAL_HOST_ID,
        )

    lab._assign_logical_indices()  # noqa: SLF001 — intra-package: load_lab lives beside Lab in this module

    return lab
