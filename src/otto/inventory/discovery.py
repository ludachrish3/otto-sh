"""Discovery: everything an inventory holds, records and instances (lab templating §6.2).

A template reads an inventory whole: every record, and the instances the
backend declares (``rig-7`` is ``edge-17`` as ``dut`` and ``ixia-9`` as
``tgen``). Instances are an optional capability, detected structurally:
a backend that can state them implements :class:`SupportsDiscovery`, and
every other backend is walked through :meth:`~otto.inventory.Inventory.lookup`
and :meth:`~otto.inventory.Inventory.list_keys`, unchanged.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from typing_extensions import final

from ..models.inventory import InventoryInstance, InventoryRecord

if TYPE_CHECKING:
    from .protocol import Inventory


@final
@dataclass(frozen=True, kw_only=True)
class Discovery:
    """What one inventory holds: its records and, when it can say, its instances.

    ``instances`` of ``None`` means the backend has no notion of instances
    (unsupported); ``{}`` means it has one and declares none (supported but
    empty). An instance's members are the backend's own keys and need no
    record in ``records``.

    Compared by value and unhashable by design: its fields are dicts, so the
    hash ``frozen=True`` would generate could only raise.
    """

    records: dict[str, InventoryRecord]
    instances: dict[str, InventoryInstance] | None = None
    __hash__ = None  # type: ignore[assignment]


@runtime_checkable
class SupportsDiscovery(Protocol):
    """Optional capability: answer discovery in one call, instances included.

    The method's name is deliberately distinctive: a plugin's own
    ``discover`` is never mistaken for it.
    """

    def inventory_discovery(self) -> Discovery:
        """Return every record this inventory holds, and its instances or ``None``."""
        ...


def discover_inventory(*, inventory: "Inventory") -> Discovery:
    """Discover *inventory*: its own capability when it has one, else a key walk.

    The walk looks up every key :meth:`~otto.inventory.Inventory.list_keys`
    returns and reports ``instances=None``. Errors propagate as the backend
    raises them.
    """
    if isinstance(inventory, SupportsDiscovery):
        return inventory.inventory_discovery()
    return Discovery(records={key: inventory.lookup(key) for key in inventory.list_keys()})
