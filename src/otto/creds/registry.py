"""Name → class registry for creds stores (spec 2026-09-06 creds-store §5.3).

Mirrors :mod:`otto.inventory.registry`: a custom store registers a bare name
from an ``init`` module, and ``[creds] backend = "<name>"`` selects it. The
built-in ``json`` store is pre-registered at import through the same path.
"""

from typing import TYPE_CHECKING

from ..registry import Registry, caller_module

if TYPE_CHECKING:
    from collections.abc import Callable

    from .protocol import CredsStore

CREDS_BACKENDS: "Registry[Callable[..., CredsStore]]" = Registry(
    "creds backend", register_hint="otto.creds.registry.register_creds_backend()"
)


def register_creds_backend(
    name: str, cls: "Callable[..., CredsStore]", *, overwrite: bool = False
) -> None:
    """Make a custom creds store selectable as ``[creds] backend = "<name>"``.

    Call from an ``init`` module listed in ``.otto/settings.toml``. The class
    must satisfy :class:`~otto.creds.protocol.CredsStore`; *overwrite*
    replaces an existing registration deliberately.
    """
    CREDS_BACKENDS.register(name, cls, overwrite=overwrite, origin=caller_module())


def get_creds_backend_class(name: str) -> "Callable[..., CredsStore]":
    """Return the store class registered under *name*; unknown names list the registered ones."""
    return CREDS_BACKENDS.get(name)


def _register_builtins() -> None:
    from .json_store import JsonCredsStore

    register_creds_backend("json", JsonCredsStore)


_register_builtins()
