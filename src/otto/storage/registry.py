"""Name → class registry for host-source (``LabRepository``) backends.

Mirrors :mod:`otto.reservations.registry` and otto's other extension
registries (``register_term_backend`` / ``register_transfer_backend`` /
``register_host_class``): a custom backend registers a bare name from an
``init`` module, and ``[lab] backend = "<name>"`` selects it. The built-in
``json`` backend is pre-registered at import so it resolves through the same
path.
"""

from ..registry import Registry, caller_module
from .errors import LabRepositoryError

# Name -> LabRepository-compatible class. ``build_lab_repository`` constructs the
# resolved class (the json built-in gets search_paths=...; a custom backend gets
# repo_dir= + its ``[lab.<name>]`` kwargs).
LAB_REPOSITORIES: Registry[type] = Registry(
    "lab repository backend", register_hint="otto.storage.registry.register_lab_repository()"
)


def register_lab_repository(name: str, cls: type, *, overwrite: bool = False) -> None:
    """Make a custom host-source backend selectable as ``backend = "<name>"``.

    Call from an ``init`` module listed in ``.otto/settings.toml``. The class
    must satisfy the :class:`~otto.storage.protocol.LabRepository` protocol.

    *overwrite* replaces an existing registration under *name* deliberately
    (e.g. a built-in); by default a duplicate name raises.
    """
    LAB_REPOSITORIES.register(name, cls, overwrite=overwrite, origin=caller_module())


def get_lab_repository_class(name: str) -> type:
    """Return the backend class registered under *name*.

    Raises
    ------
    LabRepositoryError
        If *name* is not registered; the message lists the registered names.
    """
    try:
        return LAB_REPOSITORIES.get(name)
    except ValueError as e:
        raise LabRepositoryError(str(e)) from e


def _register_builtins() -> None:
    """Register the built-in lab repositories through the public path."""
    from .json_repository import JsonFileLabRepository

    register_lab_repository("json", JsonFileLabRepository)


_register_builtins()
