"""Parsing otto's inputs: project settings, versions, user settings and the environment.

``otto.config`` parses, discovery included, and ``otto.bootstrap`` composes; the
composed-projects accessors are in :mod:`otto.declared` (spec
``docs/superpowers/specs/2026-10-06-repo-and-scope-inputs-design.md`` S-3; spec
2026-10-11 placement-and-phases §3.9). The lab and fleet API is :mod:`otto.lab`.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .dependencies import ResolvedDependency as ResolvedDependency
    from .project import Project as Project
    from .scope import DockerCompose as DockerCompose
    from .scope import DockerImage as DockerImage
    from .scope import DockerSettings as DockerSettings
    from .scope import MonitorSettings as MonitorSettings
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
    "Project",
    "ResolvedDependency",
    "Version",
    "load_otto_env",
    "load_user_settings",
    "user_settings_path",
]

# name -> (source module, attribute) resolved on first access by __getattr__.
# Kept lazy (PEP 562) because .dependencies imports .scope and
# ..models.dependencies at module level, which would otherwise widen every
# surface's import graph just to expose one dataclass type. The user-settings
# pair is here for the same reason and was MEASURED: exporting it eagerly put
# otto.models.settings (and .color/.dependencies/.inventory/.home) on every CLI
# surface and broke ten import-budget caps at once.
#
# The .project / .version names are lazy because .project parses settings.toml: it
# brings the TOML parser, .scope, .version and .corpus_snapshot, and the
# command tree imports this package to build every `--help`, which reads no
# project. MEASURED: eager, they were 200 of `otto --help`'s 1379 file operations.
# The settings types (Docker*, MonitorSettings) live in the foundation module
# otto.config.scope, which the project loader and the settings specs both read
# (spec 2026-10-11 placement-and-phases §3.6); importing one loads that module
# alone, not the project loader.
_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "DockerCompose": ("otto.config.scope", "DockerCompose"),
    "DockerImage": ("otto.config.scope", "DockerImage"),
    "DockerSettings": ("otto.config.scope", "DockerSettings"),
    "MonitorSettings": ("otto.config.scope", "MonitorSettings"),
    "Project": ("otto.config.project", "Project"),
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
