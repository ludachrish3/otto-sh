"""Name → class registry for host-source (``LabRepository``) backends.

Mirrors :mod:`otto.reservations.registry` and otto's other extension
registries (``register_term_backend`` / ``register_transfer_backend`` /
``register_host_class``): a custom backend registers a bare name from an
``init`` module, and ``[lab] backend = "<name>"`` selects it. The built-in
``json`` backend is pre-registered at import so it resolves through the same
path.
"""

from __future__ import annotations

from .errors import LabRepositoryError

# Name -> LabRepository-compatible class. ``build_lab_repository`` constructs the
# resolved class (the json built-in gets search_paths=...; a custom backend gets
# repo_dir= + its ``[lab.<name>]`` kwargs).
_LAB_REPOSITORIES: dict[str, type] = {}


def register_lab_repository(name: str, cls: type) -> None:
    """Make a custom host-source backend selectable as ``backend = "<name>"``.

    Call from an ``init`` module listed in ``.otto/settings.toml``. The class
    must satisfy the :class:`~otto.storage.protocol.LabRepository` protocol.
    """
    _LAB_REPOSITORIES[name] = cls


def get_lab_repository_class(name: str) -> type:
    """Return the backend class registered under *name*.

    Raises
    ------
    LabRepositoryError
        If *name* is not registered; the message lists the registered names.
    """
    try:
        return _LAB_REPOSITORIES[name]
    except KeyError:
        known = ", ".join(sorted(_LAB_REPOSITORIES))
        raise LabRepositoryError(
            f"Unknown lab repository backend {name!r}. Registered backends: {known}. "
            f"Custom backends can be added via register_lab_repository()."
        ) from None


def _register_builtins() -> None:
    from .json_repository import JsonFileLabRepository

    _LAB_REPOSITORIES.setdefault("json", JsonFileLabRepository)


_register_builtins()
