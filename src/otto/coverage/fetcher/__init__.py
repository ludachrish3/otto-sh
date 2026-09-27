"""Coverage fetcher: retrieve ``.gcda`` files from remote and embedded hosts.

Every name is exported lazily (PEP 562), the shape every otto package shares:
``import otto.coverage.fetcher.embedded`` does not load ``.remote``. The
resolver does not write a resolved name back into the module dict; see
``otto.config``'s ``__dir__`` for why.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .remote import GcdaFetcher as GcdaFetcher

# name -> the module that defines it, imported on first access by __getattr__.
_LAZY_ATTRS: dict[str, str] = {
    "GcdaFetcher": "otto.coverage.fetcher.remote",
}


def __getattr__(name: str) -> object:
    """PEP 562 lazy resolver for otto.coverage.fetcher's public exports."""
    import importlib

    if name in _LAZY_ATTRS:
        return getattr(importlib.import_module(_LAZY_ATTRS[name]), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """Include the lazy exports in dir()/tab-completion; the module dict holds none of them."""
    return sorted(set(globals()) | set(_LAZY_ATTRS))


__all__ = [
    "GcdaFetcher",
]
