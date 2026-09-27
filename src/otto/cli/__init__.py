"""otto is a test suite and instruction framework.

This package holds the command tree (``.main``) and one module per verb.
Every name is exported lazily (PEP 562), the shape every otto package shares:
``app`` resolves ``.main`` on first access, so importing one verb's module,
or a helper such as ``.registry``, does not build the whole command tree. The
resolver does not write a resolved name back into the module dict; see
``otto.config``'s ``__dir__`` for why.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .main import app as app

# name -> the module that defines it, imported on first access by __getattr__.
_LAZY_ATTRS: dict[str, str] = {
    "app": "otto.cli.main",
}


def __getattr__(name: str) -> object:
    """PEP 562 lazy resolver for otto.cli's public exports."""
    import importlib

    if name in _LAZY_ATTRS:
        return getattr(importlib.import_module(_LAZY_ATTRS[name]), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """Include the lazy exports in dir()/tab-completion; the module dict holds none of them."""
    return sorted(set(globals()) | set(_LAZY_ATTRS))


__all__ = [
    "app",
]
