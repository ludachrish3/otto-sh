from pathlib import Path
from typing import (
    Any,
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
        defaults: dict[str, dict[str, Any]] | None = None,
        preferences: dict[str, dict[str, list[str]]] | None = None,
    ) -> Lab:
        """
        Load a lab by name from the repository.

        Parameters
        ----------
        name : str
            Name of the lab to load
        search_paths : list[Path]
            Directories to search for the lab data
        defaults : dict[str, dict[str, Any]] | None
            Optional repo-level option defaults forwarded to the host
            factory. Backends should pass this through unchanged; the
            factory handles per-key merging beneath each host's own
            ``*_options``. ``None`` reproduces today's behavior.
        preferences : dict[str, dict[str, list[str]]] | None
            The nested ``{selector: {capability: [...]}}`` product-preference
            table forwarded to the factory, which matches each host's ``id``
            and applies the result. ``None`` reproduces today's behavior.

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
