from pathlib import Path
from typing import (
    Protocol,
    runtime_checkable,
)

from ..configmodule.lab import Lab


@runtime_checkable
class LabRepository(Protocol):
    """Protocol defining DB-agnostic interface for loading labs."""

    def load_lab(self,
        name: str,
        search_paths: list[Path],
    ) -> Lab:
        """
        Load a lab by name from the repository.

        Parameters
        ----------
        name : str
            Name of the lab to load
        search_paths : list[Path]
            Directories to search for the lab data

        Returns
        -------
        Lab
            Fully constructed Lab object with all hosts

        Raises
        ------
        FileNotFoundError
            If lab cannot be found in any search path
        ValueError
            If lab data is malformed
        ImportError
            If module loading fails (Python repository only)
        """
        ...

    def supports_location(self, path: Path) -> bool:
        """
        Check if this repository can handle data at the given location.

        Parameters
        ----------
        path : Path
            Location to check

        Returns
        -------
        bool
            True if this repository can load from this location
        """
        ...

    def list_labs(self, search_paths: list[Path]) -> list[str]:
        """
        List all valid lab names available in the search paths.

        Parameters
        ----------
        search_paths : list[Path]
            Directories to search for labs

        Returns
        -------
        list[str]
            List of lab names found in the search paths
        """
        ...
