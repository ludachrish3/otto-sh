"""Credentials by inventory key, from a pluggable store (spec 2026-09-06 creds-store).

``[creds]`` selects a registered :class:`~otto.creds.protocol.CredsStore`;
``otto.inventory`` wraps the process inventory in a ``CredsOverlay`` that
merges the store's entries under the record's, by login. This package never
imports ``otto.inventory``.

Every name is exported lazily (PEP 562), the shape every otto package shares:
naming the store contract does not load the json store or the ``[creds]``
compiler. The built-in json store is registered by reference in
``.registry``, so no import here exists for its side effect. The resolver
does not write a resolved name back into the module dict; see
``otto.config``'s ``__dir__`` for why.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import CompiledCreds as CompiledCreds
    from .config import JsonCredsConfig as JsonCredsConfig
    from .config import compile_creds as compile_creds
    from .config import compile_creds_table as compile_creds_table
    from .config import construct_creds_store as construct_creds_store
    from .errors import CredsConstructionError as CredsConstructionError
    from .errors import CredsError as CredsError
    from .json_store import JsonCredsStore as JsonCredsStore
    from .json_store import parse_creds_document as parse_creds_document
    from .protocol import CredsStore as CredsStore
    from .registry import CredsEnv as CredsEnv
    from .registry import register_creds_backend as register_creds_backend

# name -> the module that defines it, imported on first access by __getattr__.
_LAZY_ATTRS: dict[str, str] = {
    "CompiledCreds": "otto.creds.config",
    "compile_creds": "otto.creds.config",
    "compile_creds_table": "otto.creds.config",
    "construct_creds_store": "otto.creds.config",
    "CredsError": "otto.creds.errors",
    "JsonCredsStore": "otto.creds.json_store",
    "parse_creds_document": "otto.creds.json_store",
    "CredsStore": "otto.creds.protocol",
    "register_creds_backend": "otto.creds.registry",
    "CredsEnv": "otto.creds.registry",
    "CredsConstructionError": "otto.creds.errors",
    "JsonCredsConfig": "otto.creds.config",
}


def __getattr__(name: str) -> object:
    """PEP 562 lazy resolver for otto.creds's public exports."""
    import importlib

    if name in _LAZY_ATTRS:
        return getattr(importlib.import_module(_LAZY_ATTRS[name]), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """Include the lazy exports in dir()/tab-completion; the module dict holds none of them."""
    return sorted(set(globals()) | set(_LAZY_ATTRS))


__all__ = [
    "CompiledCreds",
    "CredsConstructionError",
    "CredsEnv",
    "CredsError",
    "CredsStore",
    "JsonCredsConfig",
    "JsonCredsStore",
    "compile_creds",
    "compile_creds_table",
    "construct_creds_store",
    "parse_creds_document",
    "register_creds_backend",
]
