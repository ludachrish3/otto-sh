"""The one walk over otto's own modules every tree-wide test imports from."""

import importlib
import pkgutil
from types import ModuleType

import otto

SKIPPED_PACKAGES = ("otto._webassets",)
"""Packages the walk never enters: built web assets, which hold no Python otto defines."""


def _skipped(name: str) -> bool:
    """Whether the walk must not import module *name*.

    Three kinds: a ``tests`` package at any depth (test code, not otto); a
    :data:`SKIPPED_PACKAGES` entry or anything under it; and a ``__main__``
    module at any depth, which runs the CLI when imported (``otto.__main__``
    is the ``python -m otto`` entry point and would dispatch against
    pytest's own argv).
    """
    parts = name.split(".")
    if "tests" in parts or parts[-1] == "__main__":
        return True
    return any(name == p or name.startswith(f"{p}.") for p in SKIPPED_PACKAGES)


def otto_module_names(package: ModuleType = otto) -> list[str]:
    """Every importable module name under *package* (default all of otto), sorted.

    *package* itself is included. Nothing is imported but the packages
    ``pkgutil.walk_packages`` must import to descend.
    """
    walked = pkgutil.walk_packages(package.__path__, f"{package.__name__}.")
    return sorted([package.__name__, *(i.name for i in walked if not _skipped(i.name))])


def import_otto_modules(package: ModuleType = otto) -> list[ModuleType]:
    """Import every module :func:`otto_module_names` names; an import failure raises."""
    return [importlib.import_module(name) for name in otto_module_names(package)]
