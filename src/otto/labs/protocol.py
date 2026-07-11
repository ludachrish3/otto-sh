"""``LabRepository`` protocol — the DB-agnostic interface all lab-repository backends satisfy."""

from typing import (
    TYPE_CHECKING,
    Any,
    Protocol,
    runtime_checkable,
)

if TYPE_CHECKING:
    # Deferred: otto.config.lab imports otto.labs.json_repository (for the
    # built-in "json" backend), so a module-level import here would cycle
    # when otto.labs is the first thing imported. Runtime code never
    # constructs a Lab from this module, so the string annotation below is
    # never resolved outside of static type-checking.
    from ..config.lab import Lab


@runtime_checkable
class LabRepository(Protocol):
    """DB-agnostic interface for loading labs.

    A backend is configured at construction time (the built-in JSON backend
    takes its ``search_paths`` in ``__init__``), then queried through the two
    methods below. Selection and construction happen in
    :func:`otto.labs.build_lab_repository`.
    """

    def load_lab(
        self,
        name: str,
        preferences: dict[str, dict[str, Any]] | None = None,
    ) -> "Lab":
        """Load a lab by name.

        Parameters
        ----------
        name : str
            Name of the lab to load.
        preferences : dict[str, dict[str, Any]] | None
            The unified ``{selector: {capability: [...] | option_table: {key: val}}}``
            product-preference table forwarded to the factory, which matches each
            host's ``id`` and applies the result. ``None`` reproduces today's
            behavior.

        Returns
        -------
        Lab
            Fully constructed lab.

        Raises
        ------
        LabNotFoundError
            If no lab named ``name`` exists.
        LabRepositoryError
            If the backend fails to satisfy the query (I/O, parse, network).
        """
        ...

    def list_labs(self) -> list[str]:
        """List all lab names this backend can provide.

        Returns
        -------
        list[str]
            Lab names (every element a ``str``).
        """
        ...
