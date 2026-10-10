"""The registry of reservation backends.

A backend registers a name, a configuration model and a factory from an
``init`` module, and ``[reservations] backend = "<name>"`` selects it. Otto
parses the backend's ``[reservations.<name>]`` sub-table with the
configuration model, then builds the backend with the factory. The built-ins,
``none`` and ``json``, are registered by :class:`~otto.registry.Ref`, so naming
one imports nothing until it is selected.
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
from .check import ReservationConstructionError

if TYPE_CHECKING:
    from .protocol import ReservationBackend


@dataclass(frozen=True)
class ReservationEnv:
    """The environment otto hands a reservation backend.

    The config model receives it as ``context["env"]`` when the
    ``[reservations.<name>]`` sub-table is parsed, and the factory as
    ``Configured.env``.
    """

    repo_dir: Path
    """The root of the repo whose ``[reservations]`` table is in effect; a
    relative path in the sub-table anchors to it."""

    username: str | None
    """The identity otto resolved for this invocation (``--holder`` or the
    login name), which the backend queries reservations for."""

    origin: str
    """The settings file that declared the table, for messages."""

    url: str | None
    """The ``url`` key of the ``[reservations]`` table, when set."""


def _describe_parse_error(exc: Exception) -> str:
    """Describe a sub-table that failed to parse, never quoting the rejected value."""
    # Lazy: this module stays light (no pydantic) until a parse has failed.
    from pydantic import ValidationError

    from ..models.base import compact_validation_error

    if isinstance(exc, ValidationError):
        return compact_validation_error(exc)
    return f"{type(exc).__name__} (its message is not shown: it may quote the rejected value)"


_BACKEND_METHODS = ("fetch_reservations", "backend_name")


def _check_reservation_result(name: str, obj: object) -> None:
    """Refuse a built object that is not a reservation backend (a static duck check)."""
    missing = missing_member(obj, methods=_BACKEND_METHODS)
    if missing is not None:
        raise TypeError(
            f"backend {name!r} built a {type(obj).__name__}, which lacks {missing}; "
            "a reservation backend must satisfy otto.reservations.ReservationBackend"
        )


RESERVATION_BACKENDS: "BackendRegistry[ReservationEnv, ReservationBackend, None]" = BackendRegistry(
    "reservation backend",
    register_hint="otto.reservations.register_reservation_backend()",
    error=ReservationConstructionError,
    describe_parse_error=_describe_parse_error,
    result=_check_reservation_result,
)
"""Every reservation backend, by the name ``[reservations] backend`` selects."""


@registration_boundary
def register_reservation_backend(
    name: str,
    *,
    config: "type[C] | Ref",
    factory: "Callable[[Configured[C, ReservationEnv]], ReservationBackend] | Ref",
    overwrite: bool = False,
) -> None:
    """Make a custom reservation backend selectable as ``[reservations] backend = "<name>"``.

    Call from an ``init`` module listed in ``.otto/settings.toml``.

    *config* is the model that parses the ``[reservations.<name>]``
    sub-table: otto calls its ``model_validate(table, context={"env": env})``
    once per gate, with a :class:`ReservationEnv` as *env*. The parsed
    configuration must be deep-copyable. *factory* receives
    ``Configured(config, env)`` and returns the
    :class:`~otto.reservations.protocol.ReservationBackend`. Either may be a
    :class:`~otto.registry.Ref` (``"module:attr"``), imported at first use.

    *overwrite* replaces an existing registration under *name* deliberately
    (e.g. a built-in).

    Raises:
        otto.registry.DuplicateRegistration: If *name* is taken and *overwrite* is false.
    """
    RESERVATION_BACKENDS.register(
        name,
        configured_backend(config=config, factory=factory, metadata=None),
        overwrite=overwrite,
    )


def _register_builtins() -> None:
    """Register the built-in reservation backends by reference."""
    RESERVATION_BACKENDS.register(
        "none",
        configured_backend(
            config=Ref("otto.reservations.null_backend:NoneReservationConfig"),
            factory=Ref("otto.reservations.null_backend:_none_reservations"),
            metadata=None,
        ),
    )
    RESERVATION_BACKENDS.register(
        "json",
        configured_backend(
            config=Ref("otto.reservations.json_backend:JsonReservationConfig"),
            factory=Ref("otto.reservations.json_backend:_json_reservations"),
            metadata=None,
        ),
    )


_register_builtins()
