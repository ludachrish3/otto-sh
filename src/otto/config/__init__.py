"""Parsing otto's inputs: repo settings, versions, user settings and the environment.

``otto.config`` parses and ``otto.bootstrap`` composes (spec 2026-10-06
repo-and-scope-inputs, S-3): the lab and fleet API is :mod:`otto.lab`, and the
repo accessors are in :mod:`otto.bootstrap`.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .dependencies import ResolvedDependency as ResolvedDependency
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
# silently bind none of the lazy names below. `__dir__` is the second half of
# the same fix: `dir()` reads the module dict directly and consults `__all__`
# not at all, so the names would stay invisible to introspection,
# tab-completion and help() even after a successful access.
__all__ = [
    "DockerCompose",
    "DockerImage",
    "DockerSettings",
    "MonitorSettings",
    "Repo",
    "ResolvedDependency",
    "Version",
    "load_otto_env",
    "load_user_settings",
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
# The .repo / .version names are lazy because .repo parses settings.toml: it
# brings the TOML parser, .scope, .version and .corpus_snapshot, and the
# command tree imports this package to build every `--help`, which reads no
# repo. MEASURED: eager, they were 200 of `otto --help`'s 1379 file operations.
_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "DockerCompose": ("otto.config.repo", "DockerCompose"),
    "DockerImage": ("otto.config.repo", "DockerImage"),
    "DockerSettings": ("otto.config.repo", "DockerSettings"),
    "MonitorSettings": ("otto.config.repo", "MonitorSettings"),
    "Repo": ("otto.config.repo", "Repo"),
    "Version": ("otto.config.version", "Version"),
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
