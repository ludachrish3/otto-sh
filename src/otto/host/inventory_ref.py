"""``InventoryRef`` — where a host's inventory-owned fields came from (spec §7).

Stamped by :func:`otto.host.factory.create_host_from_dict` at build, before the
product providers run, like ``element_metadata``. An inline host, a container
and the builtin ``local`` host carry an empty ref.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class InventoryRef:
    """Inventory provenance of one host.

    Hashing raises ``TypeError`` (``extra`` is a dict); key collections by
    ``key``.
    """

    key: str = ""
    """The inventory key the entry referenced; ``""`` for an inline host."""

    backend: str = ""
    """The inventory's label, e.g. ``json:/home/me/lab/inventory.json``."""

    extra: dict[str, Any] = field(default_factory=dict)
    """The record's opaque ``extra`` table — a per-host copy; otto never reads it."""

    def __post_init__(self) -> None:
        # frozen=True blocks rebinding, not mutation of the dict behind the
        # field (the LabInfo lesson): copy, so no host aliases the record.
        object.__setattr__(self, "extra", dict(self.extra))

    @property
    def referenced(self) -> bool:
        """Whether this host was built from an inventory record."""
        return bool(self.key)
