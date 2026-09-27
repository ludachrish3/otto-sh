"""otto's in-package web build artifacts: the built frontends and where they live.

The built directories sit in this package (``make web`` fills them);
:mod:`otto._webassets.artifacts` names each one's path and registers every
one in ``ALL``.

Every name is exported lazily (PEP 562), the shape every otto package shares.
The resolver does not write a resolved name back into the module dict; see
``otto.config``'s ``__dir__`` for why.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .artifacts import ALL as ALL
    from .artifacts import COVAPP as COVAPP
    from .artifacts import MONITOR as MONITOR

# name -> the module that defines it, imported on first access by __getattr__.
_LAZY_ATTRS: dict[str, str] = {
    "ALL": "otto._webassets.artifacts",
    "COVAPP": "otto._webassets.artifacts",
    "MONITOR": "otto._webassets.artifacts",
}


def __getattr__(name: str) -> object:
    """PEP 562 lazy resolver for otto._webassets's public exports."""
    import importlib

    if name in _LAZY_ATTRS:
        return getattr(importlib.import_module(_LAZY_ATTRS[name]), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """Include the lazy exports in dir()/tab-completion; the module dict holds none of them."""
    return sorted(set(globals()) | set(_LAZY_ATTRS))


__all__ = [
    "ALL",
    "COVAPP",
    "MONITOR",
]
