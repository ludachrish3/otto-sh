"""The registry of inventory backends.

A backend registers a name, a configuration model and a factory from an
``init`` module, and ``[inventory] backend = "<name>"`` selects it. Otto parses
the table's other keys (all but ``backend`` and ``cache_ttl``) with the
configuration model, then builds the inventory with the factory. The built-ins,
``json`` and ``netbox``, are registered by :class:`~otto.registry.Ref`, so
naming one imports nothing until it is selected.
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..registry import (
    BackendRegistry,
    C,
    Configured,
    Ref,
    configured_backend,
    missing_member,
    registration_boundary,
)
from .errors import InventoryConstructionError

if TYPE_CHECKING:
    from ..registry import BackendEntry, Proposed
    from .protocol import Inventory


@dataclass(frozen=True)
class InventoryEnv:
    """The environment otto hands an inventory backend.

    The config model receives it as ``context["env"]`` when the
    ``[inventory]`` table is parsed, and the factory as ``Configured.env``.
    """

    anchor_dir: Path
    """The directory a relative path in the table anchors to, which is the
    declaring repo's root, or ``~/.otto`` for the user settings file."""

    origin: str
    """The settings file that declared the table, for messages."""


@dataclass(frozen=True)
class InventoryMetadata:
    """What an inventory backend states at registration, read without importing it."""

    snapshot_cache: bool = True
    """Whether otto may wrap the backend in the snapshot cache.

    The cache applies only to a backend that also reports no freshness (its
    ``fingerprint()`` is ``None``) under a ``cache_ttl`` above zero. The
    built-in ``json`` backend states ``False``: it reads a local file, and
    asking its fingerprint would stat that file while otto constructs it.
    """


def _describe_parse_error(exc: Exception) -> str:
    """Describe an ``[inventory]`` table that failed to parse, never quoting the rejected value."""
    # Lazy: this module stays light (no pydantic) until a parse has failed.
    from pydantic import ValidationError

    from ..models.base import compact_validation_error

    if isinstance(exc, ValidationError):
        return compact_validation_error(exc)
    return f"{type(exc).__name__} (its message is not shown: it may quote the rejected value)"


def _check_inventory_entry(
    name: str,
    entry: "BackendEntry[InventoryEnv, Inventory, InventoryMetadata]",
    proposed: "Proposed[BackendEntry[InventoryEnv, Inventory, InventoryMetadata]]",
) -> None:
    """``INVENTORY_BACKENDS``' validate: every entry states an :class:`InventoryMetadata`.

    The snapshot-cache decision reads it at every build; checking it here
    keeps a malformed direct registration out of the table rather than
    failing later outside the error pipeline.
    """
    del proposed
    if not isinstance(entry.metadata, InventoryMetadata):
        raise TypeError(
            f"inventory backend {name!r}: the entry's metadata is a "
            f"{type(entry.metadata).__name__}, not an InventoryMetadata"
        )


_INVENTORY_METHODS = ("lookup", "list_keys", "fingerprint")
_INVENTORY_ATTRIBUTES = ("label", "supplies")


def _check_inventory_result(name: str, obj: object) -> None:
    """Refuse a built object that is not an :class:`~otto.inventory.protocol.Inventory`."""
    missing = missing_member(obj, methods=_INVENTORY_METHODS, attributes=_INVENTORY_ATTRIBUTES)
    if missing is not None:
        raise TypeError(
            f"backend {name!r} built a {type(obj).__name__}, which lacks {missing}; "
            "an inventory must satisfy otto.inventory.Inventory"
        )


INVENTORY_BACKENDS: "BackendRegistry[InventoryEnv, Inventory, InventoryMetadata]" = BackendRegistry(
    "inventory backend",
    register_hint="otto.inventory.register_inventory_backend()",
    error=InventoryConstructionError,
    describe_parse_error=_describe_parse_error,
    result=_check_inventory_result,
    validate=_check_inventory_entry,
)
"""Every inventory backend, by the name ``[inventory] backend`` selects."""


@registration_boundary
def register_inventory_backend(
    name: str,
    *,
    config: "type[C] | Ref",
    factory: "Callable[[Configured[C, InventoryEnv]], Inventory] | Ref",
    snapshot_cache: bool = True,
    overwrite: bool = False,
) -> None:
    """Make a custom inventory backend selectable as ``[inventory] backend = "<name>"``.

    Call from an ``init`` module listed in ``.otto/settings.toml``.

    *config* is the model that parses the table's keys (every key but
    ``backend`` and ``cache_ttl``): otto calls its
    ``model_validate(table, context={"env": env})`` once, with an
    :class:`InventoryEnv` as *env*. The parsed
    configuration must be deep-copyable. *factory* receives
    ``Configured(config, env)`` and returns the
    :class:`~otto.inventory.protocol.Inventory`. Either may be a
    :class:`~otto.registry.Ref` (``"module:attr"``), imported at first use.

    *snapshot_cache* states whether otto may wrap the backend in the snapshot
    cache (see :class:`InventoryMetadata`). *overwrite* replaces an existing
    registration under *name* deliberately (e.g. a built-in).

    Raises:
        otto.registry.DuplicateRegistration: If *name* is taken and *overwrite* is false.
    """
    INVENTORY_BACKENDS.register(
        name,
        configured_backend(
            config=config,
            factory=factory,
            metadata=InventoryMetadata(snapshot_cache=snapshot_cache),
        ),
        overwrite=overwrite,
    )


def _register_builtins() -> None:
    """Register the built-in inventory backends by reference."""
    INVENTORY_BACKENDS.register(
        "json",
        configured_backend(
            config=Ref("otto.inventory.config:JsonInventoryConfig"),
            factory=Ref("otto.inventory.json_backend:_json_inventory"),
            metadata=InventoryMetadata(snapshot_cache=False),
        ),
    )
    INVENTORY_BACKENDS.register(
        "netbox",
        configured_backend(
            config=Ref("otto.inventory.config:NetBoxInventoryConfig"),
            factory=Ref("otto.inventory.netbox:_netbox_inventory"),
            metadata=InventoryMetadata(snapshot_cache=True),
        ),
    )


_register_builtins()
