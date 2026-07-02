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
    from ..host.host import Host
    from ..storage.protocol import LabRepository


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

    def __add__(
        self,
        other: "Lab",
    ) -> "Lab":

        from ..host.remote_host import RemoteHost

        self.name = f"{self.name}_{other.name}"
        self.resources = self.resources.union(other.resources)
        for host in other.hosts.values():
            if isinstance(host, RemoteHost):
                host._lab = self
        self.hosts.update(other.hosts)

        return self


# Imported here (after Lab is fully defined) rather than at the top of the
# module to avoid a circular-import bootstrap: json_repository imports Lab
# from this module, so this import must wait until Lab is defined.
from ..storage.json_repository import JsonFileLabRepository  # noqa: E402, I001 — import after Lab class definition to avoid circular-import bootstrap


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
        Name(s) of lab data to retrieve (a comma-separated string is split).
    search_paths : list[Path] | None
        Directories searched by the default json backend. Ignored when
        ``repository`` is supplied.
    preferences : dict[str, dict[str, Any]] | None
        The unified ``{selector: {capability: [...] | option_table: {key: val}}}``
        product-preference table applied to every host in the resulting lab.
        ``None`` reproduces today's behavior.
    repository : LabRepository | None
        A pre-built host-source backend (e.g. from
        :func:`otto.storage.build_lab_repository`). When ``None``, a built-in
        json backend over ``search_paths`` is used — preserving library/script
        behavior.

    Returns
    -------
    Lab
        Fully defined lab instance.
    """
    match labnames:
        case str():
            lab_names = labnames.split(",")
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
        getLogger("otto").debug(
            "Lab %r defines its own %r host; skipping the built-in local host.",
            lab.name,
            BUILTIN_LOCAL_HOST_ID,
        )

    return lab
