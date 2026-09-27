"""Public API for the config package — lab loading, host access, and repo settings."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .bootstrapped import get_completion_names as get_completion_names
    from .bootstrapped import get_env as get_env
    from .bootstrapped import get_ordered_repos as get_ordered_repos
    from .bootstrapped import get_repos as get_repos
    from .bootstrapped import is_bootstrapped as is_bootstrapped
    from .dependencies import ResolvedDependency as ResolvedDependency
    from .fleet import all_hosts as all_hosts
    from .fleet import do_for_all_hosts as do_for_all_hosts
    from .fleet import get_host as get_host
    from .fleet import get_lab as get_lab
    from .fleet import run_on_all_hosts as run_on_all_hosts
    from .lab import load_lab as load_lab
    from .repo import DockerCompose as DockerCompose
    from .repo import DockerImage as DockerImage
    from .repo import DockerSettings as DockerSettings
    from .repo import MonitorSettings as MonitorSettings
    from .repo import Repo as Repo
    from .user_settings import load_user_settings as load_user_settings
    from .user_settings import user_settings_path as user_settings_path
    from .version import Version as Version

from .env import (
    load_otto_env as load_otto_env,
)

# THE PUBLIC SURFACE, declared rather than inferred. A PEP 562 name never
# enters the module dict, so without this `from otto.config import *` would
# silently stop binding the six .fleet/.lab names it bound before they went
# lazy — a library-user break with no error at either end. `__dir__` is the
# second half of the same fix: `dir()` reads the module dict directly and
# consults `__all__` not at all, so the names would stay invisible to
# introspection, tab-completion and help() even after a successful access.
__all__ = [
    "DockerCompose",
    "DockerImage",
    "DockerSettings",
    "MonitorSettings",
    "Repo",
    "ResolvedDependency",
    "Version",
    "all_hosts",
    "do_for_all_hosts",
    "get_completion_names",
    "get_env",
    "get_host",
    "get_lab",
    "get_ordered_repos",
    "get_repos",
    "is_bootstrapped",
    "load_lab",
    "load_otto_env",
    "load_user_settings",
    "run_on_all_hosts",
    "user_settings_path",
]

# name -> (source module, attribute) resolved on first access by __getattr__.
# Kept lazy (PEP 562) because .dependencies imports ..bootstrap and
# ..models.dependencies at module level, which would otherwise widen every
# surface's import graph just to expose one dataclass type. The user-settings
# pair is here for the same reason and was MEASURED: exporting it eagerly put
# otto.models.settings (and .color/.dependencies/.inventory/.home) on every CLI
# surface and broke ten import-budget caps at once.
#
# The .fleet / .lab names are lazy for the same MEASURED reason: eager
# re-exports made a bare `import otto.config` load the whole 46-module
# otto.host.* subtree (.lab's post-class otto.labs.* imports -> json_repository
# -> otto.host.factory). Callers reach them through this module's __getattr__
# or import the submodule by hand, so the host graph only loads on the
# surfaces that actually dispatch to a host.
#
# The .repo / .version names are lazy because .repo parses settings.toml: it
# brings the TOML parser, .scope, .version and .corpus_snapshot, and the
# command tree imports this package to build every `--help`, which reads no
# repo. MEASURED: eager, they were 200 of `otto --help`'s 1379 file operations.
#
# The .bootstrapped accessors (get_repos, get_env, ...) cost nothing to import
# either way, since each imports otto.bootstrap in its own body; they live in a
# submodule because a package init holds no code of its own.
#
# NOT re-exported: `otto.config.fleet.get_hosts_in_play` — it is the
# reservation readers' tolerant spelling (an empty declared fleet is zero hosts
# in play, never an abort), and a walk written against the most discoverable
# name would silently touch nothing. Its three readers import it from
# `otto.config.fleet` / `otto.context` by hand, which is the point.
_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "DockerCompose": ("otto.config.repo", "DockerCompose"),
    "DockerImage": ("otto.config.repo", "DockerImage"),
    "DockerSettings": ("otto.config.repo", "DockerSettings"),
    "MonitorSettings": ("otto.config.repo", "MonitorSettings"),
    "Repo": ("otto.config.repo", "Repo"),
    "Version": ("otto.config.version", "Version"),
    "ResolvedDependency": ("otto.config.dependencies", "ResolvedDependency"),
    "get_completion_names": ("otto.config.bootstrapped", "get_completion_names"),
    "get_env": ("otto.config.bootstrapped", "get_env"),
    "get_ordered_repos": ("otto.config.bootstrapped", "get_ordered_repos"),
    "get_repos": ("otto.config.bootstrapped", "get_repos"),
    "is_bootstrapped": ("otto.config.bootstrapped", "is_bootstrapped"),
    "all_hosts": ("otto.config.fleet", "all_hosts"),
    "do_for_all_hosts": ("otto.config.fleet", "do_for_all_hosts"),
    "get_host": ("otto.config.fleet", "get_host"),
    "get_lab": ("otto.config.fleet", "get_lab"),
    "load_lab": ("otto.config.lab", "load_lab"),
    "load_user_settings": ("otto.config.user_settings", "load_user_settings"),
    "run_on_all_hosts": ("otto.config.fleet", "run_on_all_hosts"),
    "user_settings_path": ("otto.config.user_settings", "user_settings_path"),
}


def __getattr__(name: str) -> object:
    """PEP 562 lazy resolver for config's public exports."""
    import importlib

    if name in _LAZY_EXPORTS:
        module_name, attr = _LAZY_EXPORTS[name]
        return getattr(importlib.import_module(module_name), attr)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """List the eager module contents PLUS the lazy exports.

    The resolver above deliberately does not write resolved names back into the
    module dict (a cache would make laziness a one-shot property that no later
    test could observe), so the default module ``__dir__`` — which is exactly
    ``list(module.__dict__)`` — cannot see them at any point in the process's
    life. Union, not ``__all__`` alone, so private helpers stay as discoverable
    as they were.
    """
    return sorted(set(globals()) | set(_LAZY_EXPORTS))
