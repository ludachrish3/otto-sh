"""The registry of host-source (``LabRepository``) backends.

A backend registers a name, a configuration model and a factory from an
``init`` module, and a ``[[lab.sources]]`` entry's ``backend = "<name>"``
selects it. Otto parses the entry's other keys with the configuration model
after every project's init modules have run, then builds the source with the
factory. The built-in ``json`` and ``jsonnet`` backends are registered by
:class:`~otto.registry.Ref`, so naming either imports nothing. ``jsonnet``
yields its name: a team's own registration of ``jsonnet`` replaces it, with a
warning, rather than failing as a duplicate.
"""

from collections.abc import Callable
from typing import TYPE_CHECKING

from ..registry import (
    BackendRegistry,
    C,
    Configured,
    Ref,
    configured_backend,
    registration_boundary,
)
from .errors import LabSourceConstructionError
from .sources import LabSourceEnv

if TYPE_CHECKING:
    from .protocol import LabRepository


def _describe_parse_error(exc: Exception) -> str:
    """Describe a source's options that failed to parse, never quoting the rejected value."""
    # Lazy: this module stays light (no pydantic) until a parse has failed.
    from pydantic import ValidationError

    from ..models.base import compact_validation_error

    if isinstance(exc, ValidationError):
        return compact_validation_error(exc)
    return f"{type(exc).__name__} (its message is not shown: it may quote the rejected value)"


def _check_lab_repository(name: str, obj: object) -> None:
    """Refuse a built object that cannot serve as a lab source."""
    missing = [m for m in ("load_lab", "list_labs") if not callable(getattr(obj, m, None))]
    if missing:
        raise TypeError(
            f"backend {name!r} built a {type(obj).__name__}, which has no callable "
            f"{' or '.join(missing)}; a lab source must satisfy otto.labs.LabRepository"
        )


LAB_REPOSITORIES: "BackendRegistry[LabSourceEnv, LabRepository, None]" = BackendRegistry(
    "lab repository backend",
    register_hint="otto.labs.register_lab_repository()",
    error=LabSourceConstructionError,
    describe_parse_error=_describe_parse_error,
    result=_check_lab_repository,
)
"""Every lab-source backend, by the name a ``[[lab.sources]]`` entry selects."""


@registration_boundary
def register_lab_repository(
    name: str,
    *,
    config: "type[C] | Ref",
    factory: "Callable[[Configured[C, LabSourceEnv]], LabRepository] | Ref",
    overwrite: bool = False,
) -> None:
    """Make a custom host-source backend selectable as ``backend = "<name>"``.

    Call from an ``init`` module listed in ``.otto/settings.toml``.

    *config* is the model that parses a source's options (every key of its
    ``[[lab.sources]]`` entry but ``backend`` and ``name``): otto calls its
    ``model_validate(options, context={"env": env})`` once per source, after
    every project's init modules have run, with a :class:`~otto.labs.sources.LabSourceEnv`
    as *env*. The parsed configuration must be deep-copyable. *factory*
    receives ``Configured(config, env)`` and returns the
    :class:`~otto.labs.protocol.LabRepository`. Either may be a
    :class:`~otto.registry.Ref` (``"module:attr"``), imported at first use.

    *overwrite* replaces an existing registration under *name* deliberately
    (e.g. the built-in ``json``).

    Raises:
        otto.registry.DuplicateRegistration: If *name* is taken and *overwrite* is false.
    """
    LAB_REPOSITORIES.register(
        name=name,
        entry=configured_backend(config=config, factory=factory, metadata=None),
        overwrite=overwrite,
    )


def _register_builtins() -> None:
    """Register the built-in lab repositories by reference.

    ``jsonnet`` yields its name, so a team that registered its own
    ``jsonnet`` lab source before otto shipped one keeps it.
    """
    LAB_REPOSITORIES.register(
        name="json",
        entry=configured_backend(
            config=Ref(target="otto.labs.json_repository:JsonLabSourceConfig"),
            factory=Ref(target="otto.labs.json_repository:_json_lab_source"),
            metadata=None,
        ),
    )
    LAB_REPOSITORIES.register(
        name="jsonnet",
        entry=configured_backend(
            config=Ref(target="otto.labs.jsonnet_source:JsonnetLabSourceConfig"),
            factory=Ref(target="otto.labs.jsonnet_source:_jsonnet_lab_source"),
            metadata=None,
        ),
        yielding=True,
    )


_register_builtins()
