"""Name → class registry for inventory backends.

Mirrors otto's other extension registries (``register_reservation_backend`` /
``register_term_backend`` / ``register_host_class``): a custom backend
registers a bare name from an ``init`` module, and ``[inventory] backend =
"<name>"`` selects it. Built-ins are pre-registered at import so they resolve
through the same path.
"""

from typing import TYPE_CHECKING

from ..registry import Registry, caller_module

if TYPE_CHECKING:
    from collections.abc import Callable

    from .protocol import Inventory

# Name -> Inventory-compatible class. ``build_inventory`` constructs the
# resolved class from the ``[inventory]`` settings table.
#
# ``Callable[..., Inventory]``, not ``type[Inventory]``: each backend declares
# its own constructor signature (json takes ``path``/``supplies``, a custom
# backend takes ``repo_dir`` + its settings kwargs), so the registry's contract
# is "constructs an Inventory", not "shares an ``__init__``".
INVENTORY_BACKENDS: "Registry[Callable[..., Inventory]]" = Registry(
    "inventory backend", register_hint="otto.inventory.registry.register_inventory_backend()"
)


def register_inventory_backend(
    name: str, cls: "Callable[..., Inventory]", *, overwrite: bool = False
) -> None:
    """Make a custom inventory backend selectable as ``backend = "<name>"``.

    Call from an ``init`` module listed in ``.otto/settings.toml``. The class
    must satisfy the :class:`~otto.inventory.protocol.Inventory` protocol.

    *overwrite* replaces an existing registration under *name* deliberately
    (e.g. a built-in); by default a duplicate name raises.
    """
    INVENTORY_BACKENDS.register(name, cls, overwrite=overwrite, origin=caller_module())


def get_inventory_backend_class(name: str) -> "Callable[..., Inventory]":
    """Return the backend class registered under *name*.

    Raises
    ------
    ValueError
        If *name* is not registered; the message lists the registered names.
    """
    return INVENTORY_BACKENDS.get(name)


def _register_builtins() -> None:
    """Register the built-in inventory backends through the public path.

    The JSON backend and the NetBox backend each add their line here as they
    land, so a built-in is never reachable by a path a custom backend cannot
    also take.

    Neither import is heavy: ``pynetbox`` lives inside
    :class:`~otto.inventory.netbox.NetBoxInventory`'s fetch, so registering the
    class costs the same as registering the JSON one.
    """
    from .json_backend import JsonInventory
    from .netbox import NetBoxInventory

    register_inventory_backend("json", JsonInventory)
    register_inventory_backend("netbox", NetBoxInventory)


_register_builtins()
