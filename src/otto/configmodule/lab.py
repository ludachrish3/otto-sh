from __future__ import annotations

from dataclasses import (
    dataclass,
    field,
)
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..host import RemoteHost


@dataclass
class Lab():
    name: str
    """Name of this lab."""

    resources: set[str] = field(default_factory=set)
    """Resources required to reserve this lab."""

    hosts: dict[str, RemoteHost] = field(default_factory=dict)
    """Host objects, keyed by unique host id."""

    def addHost(self,
        host: RemoteHost,
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

        self.hosts[host.id] = host

    def __add__(self,
        other: 'Lab',
    ) -> 'Lab':

        self.name = f"{self.name}_{other.name}"
        self.resources.union(other.resources)
        self.hosts.update(other.hosts)

        return self

def _getIndividualLab(
    labname: str,
    search_paths: list[Path] | None = None,
) -> Lab:
    """
    Load an individual lab by name.

    Parameters
    ----------
    labname : str
        Name of the lab to load
    search_paths : list[Path] | None
        Directories to search for lab data. If None, uses empty list.

    Returns
    -------
    Lab
        Loaded lab object
    """

    # TODO: Straighten out imports so this is imported at the top
    # Import here to avoid circular dependencies
    from ..storage.json_repository import JsonFileLabRepository

    if search_paths is None:
        search_paths = []

    repo = JsonFileLabRepository()
    return repo.load_lab(labname, search_paths)

def getLab(
    labnames: str | list[str],
    search_paths: list[Path] | None = None,
) -> Lab:
    """
    Perform all actions necessary to build a Lab object based on a list of lab names.

    Parameters
    ----------
    labnames : str | list[str]
        Name(s) of lab data to retrieve.
    search_paths : list[Path] | None
        Directories to search for lab data.

    Returns
    -------
    Lab
        Fully defined lab instance.
    """

    match labnames:
        case str():
            labnameList = labnames.split(",")
        case _:
            labnameList = labnames

    labs = [_getIndividualLab(name, search_paths) for name in labnameList]
    lab = labs[0]
    for additionalLab in labs[1:]:
        lab += additionalLab

    return lab
