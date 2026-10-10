"""The registry of creds stores.

Mirrors :mod:`otto.inventory.registry`: a store registers a name, a
configuration model and a factory from an ``init`` module, and ``[creds]
backend = "<name>"`` selects it. The built-in ``json`` store is registered by
:class:`~otto.registry.Ref`, so naming it imports nothing until it is selected.
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
from .errors import CredsConstructionError

if TYPE_CHECKING:
    from .protocol import CredsStore


@dataclass(frozen=True)
class CredsEnv:
    """The environment otto hands a creds store.

    The config model receives it as ``context["env"]`` when the ``[creds]``
    table is parsed, and the factory as ``Configured.env``.
    """

    anchor_dir: Path
    """The directory a relative path in the table anchors to, which is the
    declaring repo's root, or ``~/.otto`` for the user settings file."""

    origin: str
    """The settings file that declared the table, for messages."""


def _describe_parse_error(exc: Exception) -> str:
    """Describe a ``[creds]`` table that failed to parse, never quoting the rejected value."""
    # Lazy: this module stays light (no pydantic) until a parse has failed.
    from pydantic import ValidationError

    from ..models.base import compact_validation_error

    if isinstance(exc, ValidationError):
        return compact_validation_error(exc)
    return f"{type(exc).__name__} (its message is not shown: it may quote the rejected value)"


_STORE_METHODS = ("lookup", "list_keys", "fingerprint")
_STORE_ATTRIBUTES = ("label",)


def _check_creds_result(name: str, obj: object) -> None:
    """Refuse a built object that is not a :class:`~otto.creds.protocol.CredsStore`."""
    missing = missing_member(obj, methods=_STORE_METHODS, attributes=_STORE_ATTRIBUTES)
    if missing is not None:
        raise TypeError(
            f"backend {name!r} built a {type(obj).__name__}, which lacks {missing}; "
            "a creds store must satisfy otto.creds.CredsStore"
        )


CREDS_BACKENDS: "BackendRegistry[CredsEnv, CredsStore, None]" = BackendRegistry(
    "creds backend",
    register_hint="otto.creds.register_creds_backend()",
    error=CredsConstructionError,
    describe_parse_error=_describe_parse_error,
    result=_check_creds_result,
)
"""Every creds store, by the name ``[creds] backend`` selects."""


@registration_boundary
def register_creds_backend(
    name: str,
    *,
    config: "type[C] | Ref",
    factory: "Callable[[Configured[C, CredsEnv]], CredsStore] | Ref",
    overwrite: bool = False,
) -> None:
    """Make a custom creds store selectable as ``[creds] backend = "<name>"``.

    Call from an ``init`` module listed in ``.otto/settings.toml``.

    *config* is the model that parses the table's keys (every key but
    ``backend``): otto calls its ``model_validate(table, context={"env":
    env})`` once, with a :class:`CredsEnv` as *env*. The
    parsed configuration must be deep-copyable. *factory* receives
    ``Configured(config, env)`` and returns the
    :class:`~otto.creds.protocol.CredsStore`. Either may be a
    :class:`~otto.registry.Ref` (``"module:attr"``), imported at first use.

    *overwrite* replaces an existing registration under *name* deliberately
    (e.g. the built-in ``json``).

    Raises:
        otto.registry.DuplicateRegistration: If *name* is taken and *overwrite* is false.
    """
    CREDS_BACKENDS.register(
        name, configured_backend(config=config, factory=factory, metadata=None), overwrite=overwrite
    )


def _register_builtins() -> None:
    """Register the built-in creds store by reference."""
    CREDS_BACKENDS.register(
        "json",
        configured_backend(
            config=Ref("otto.creds.config:JsonCredsConfig"),
            factory=Ref("otto.creds.json_store:_json_creds"),
            metadata=None,
        ),
    )


_register_builtins()
