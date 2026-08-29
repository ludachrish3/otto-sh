"""Public API for the config package — lab loading, host access, and repo settings."""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..models.settings import OttoEnvSettings
    from .dependencies import ResolvedDependency as ResolvedDependency
    from .user_settings import load_user_settings as load_user_settings
    from .user_settings import user_settings_path as user_settings_path

from .env import (
    load_otto_env as load_otto_env,
)
from .fleet import (
    all_hosts as all_hosts,
)
from .fleet import (
    do_for_all_hosts as do_for_all_hosts,
)
from .fleet import (
    get_host as get_host,
)
from .fleet import (
    get_lab as get_lab,
)
from .fleet import (
    run_on_all_hosts as run_on_all_hosts,
)
from .lab import (
    load_lab as load_lab,
)
from .repo import (
    DockerCompose as DockerCompose,
)
from .repo import (
    DockerImage as DockerImage,
)
from .repo import (
    DockerSettings as DockerSettings,
)
from .repo import (
    MonitorSettings as MonitorSettings,
)
from .repo import (
    Repo,
)
from .version import (
    Version as Version,
)

# name -> (source module, attribute) resolved on first access by __getattr__.
# Kept lazy (PEP 562) because .dependencies imports ..bootstrap and
# ..models.dependencies at module level, which would otherwise widen every
# surface's import graph just to expose one dataclass type. The user-settings
# pair is here for the same reason and was MEASURED: exporting it eagerly put
# otto.models.settings (and .color/.dependencies/.inventory/.home) on every CLI
# surface and broke ten import-budget caps at once.
_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "ResolvedDependency": ("otto.config.dependencies", "ResolvedDependency"),
    "load_user_settings": ("otto.config.user_settings", "load_user_settings"),
    "user_settings_path": ("otto.config.user_settings", "user_settings_path"),
}


def __getattr__(name: str) -> object:
    """PEP 562 lazy resolver for config's public exports."""
    import importlib

    if name in _LAZY_EXPORTS:
        module_name, attr = _LAZY_EXPORTS[name]
        return getattr(importlib.import_module(module_name), attr)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def get_repos() -> list[Repo]:
    """Return the ``Repo`` objects for the configured SUT directories (bootstraps lazily)."""
    from ..bootstrap import bootstrap

    return bootstrap().repos


def get_ordered_repos() -> list[Repo]:
    """Return configured repos in dependency-topological order (bootstraps lazily).

    Dependencies first, dependents after — the walk order the ``otto.project``
    orchestrator installs in (and reverses to uninstall). Skipped repos
    (unsatisfied required deps) are absent, exactly as they are absent from
    phase-2 registration.
    """
    from ..bootstrap import bootstrap

    return bootstrap().ordered_repos


def get_env() -> "OttoEnvSettings":
    """Return the startup environment settings (bootstraps discovery lazily)."""
    from ..bootstrap import discover

    return discover().env


def get_completion_names() -> dict[str, Any] | None:
    """Return cached instruction/suite/host data when the completion fast path is active.

    Return ``None`` when not active.

    Returned keys:

    - ``instructions`` / ``suites``: each a list of
      ``{"name": str, "options": [...]}`` dicts. :mod:`otto.cli.main` rebuilds
      Typer stubs from them.
    - ``hosts``: a plain list of host-ID strings. :mod:`otto.cli.host`'s
      ``host_id`` completer prefers this over live ``lab.json`` parsing.
    - ``term_backends``: a ``list[str]`` of registered term backend names.
      :mod:`otto.cli.host`'s ``--term`` completer prefers this over the live
      registry.
    - ``transfer_backends``: a list of
      ``{"name": str, "host_families": [str, ...]}`` dicts for registered
      transfer backends. :mod:`otto.cli.host`'s ``--transfer`` completer
      prefers this over the live registry.
    """
    from ..bootstrap import get_completion_names as _get

    return _get()
