"""``LabRepository`` protocol — the DB-agnostic interface all lab-repository backends satisfy."""

from dataclasses import dataclass, field
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
    from ..inventory import Inventory


@runtime_checkable
class LabRepository(Protocol):
    """DB-agnostic interface for loading labs.

    A backend is configured at construction time (the built-in JSON backend
    takes its ``search_paths`` in ``__init__``), then queried through the two
    methods below. Selection and construction happen in
    :func:`otto.labs.build_lab_sources`, one instance per ``[[lab.sources]]``
    entry.
    """

    def load_lab(
        self,
        name: str,
        preferences: dict[str, dict[str, Any]] | None = None,
        inventory: "Inventory | None" = None,
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
        inventory : Inventory | None
            The process inventory referenced entries resolve against (spec
            2026-08-28 host-inventory §6); ``None`` means a referenced entry
            is an error.

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


@dataclass(frozen=True)
class HostSummary:
    """Identity and addressing for one host, without constructing it.

    What tab completion and tunnel path-narrowing need to *name* and *reach*
    a host. Deliberately small: anything requiring creds, interfaces,
    transports, or options is a job for
    :meth:`~otto.labs.protocol.LabRepository.load_lab`, not for a second host
    model growing here.

    ``frozen=True`` blocks attribute rebinding, not mutation of ``labs`` (or
    ``lab_patterns``) — producers deliberately append to them while merging a
    host that appears in several labs, before the summary is handed out. (A consequence: a summary
    is not hashable. Key collections by ``.id``.)
    """

    id: str
    """The host's canonical id — byte-identical to the built host's ``.id``."""

    labs: list[str] = field(default_factory=list)
    """Lab names this host belongs to."""

    lab_patterns: list[str] = field(default_factory=list)
    """Membership patterns of the element this host belongs to (json backend);
    the composite re-resolves them against every source's declared labs so
    ``labs`` is complete across sources. Empty for backends that return
    concrete ``labs``."""

    ip: str = ""
    """Management address, or ``""`` when the backend does not expose one."""

    element: str = ""
    """Element name, used to synthesize positional handles (``dut1``)."""

    element_id: int | None = None
    """Element index within its element group, or None."""

    docker_capable: bool = False
    """Whether the host can host containers (drives ``otto docker --on``)."""


@runtime_checkable
class SupportsHostSummaries(Protocol):
    """Optional capability: enumerate hosts without constructing them.

    A backend that can answer "which hosts exist, and what are their ids /
    labs / addresses" more cheaply than a full :meth:`LabRepository.load_lab`
    implements this; otto detects it structurally and uses it for tab
    completion and tunnel narrowing.

    Implementing it is OPTIONAL and purely an optimization: a backend that
    omits it still gets completion, because
    :func:`otto.labs.host_summaries` falls back to ``list_labs`` +
    ``load_lab``. The fallback is always correct, just slower.

    An implementation MUST agree with ``load_lab`` in three ways, all checked
    by :func:`otto.testing.assert_lab_repository_conforms`: every id
    byte-identical to the one ``load_lab`` reports (an id that does not
    round-trip is worse than no completion, because it offers the user
    something that will not dispatch); every host ``load_lab`` produces
    summarized, or completion silently stops offering it; and every FIELD
    equal to the built host's. The fields have defaults so this dataclass
    will let you omit them, but each drives a surface — see the class
    docstring above. Backends over raw records should reach
    for :func:`otto.host.factory.host_identity`, which applies the same
    profile merge and validation the host factory applies.
    """

    def list_host_summaries(self, inventory: "Inventory | None" = None) -> list[HostSummary]:
        """Every host this backend knows, across every lab.

        *inventory* is the process inventory referenced entries resolve
        against (spec 2026-08-28 host-inventory §6); ``None`` means a
        referenced entry cannot be identified and is skipped, like any other
        unresolvable record.
        """
        ...
