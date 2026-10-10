"""Labs module for DB-agnostic lab/host repository pattern.

Every name is exported lazily (PEP 562), the shape every otto package shares:
naming the repository contract or an error does not load the json backend,
the composite or the source builder. The built-in ``json`` backend is
registered by reference in ``.registry``, so no import here exists for its
side effect. The resolver does not write a resolved name back into the
module dict; see ``otto.config``'s ``__dir__`` for why.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .composite import CompositeLabRepository as CompositeLabRepository
    from .composite import LabSource as LabSource
    from .errors import LabNotFoundError as LabNotFoundError
    from .errors import LabRepositoryError as LabRepositoryError
    from .errors import LabSourceConstructionError as LabSourceConstructionError
    from .json_repository import JsonFileLabRepository as JsonFileLabRepository
    from .protocol import HostSummary as HostSummary
    from .protocol import LabRepository as LabRepository
    from .protocol import LoginSummary as LoginSummary
    from .protocol import SupportsHostSummaries as SupportsHostSummaries
    from .protocol import logins_of_creds as logins_of_creds
    from .protocol import logins_of_host_data as logins_of_host_data
    from .registry import register_lab_repository as register_lab_repository
    from .sources import LabSourceEnv as LabSourceEnv
    from .sources import build_lab_sources as build_lab_sources
    from .summaries import host_summaries as host_summaries
    from .summaries import list_host_ids as list_host_ids

# name -> the module that defines it, imported on first access by __getattr__.
_LAZY_ATTRS: dict[str, str] = {
    "CompositeLabRepository": "otto.labs.composite",
    "LabSource": "otto.labs.composite",
    "LabNotFoundError": "otto.labs.errors",
    "LabRepositoryError": "otto.labs.errors",
    "LabSourceConstructionError": "otto.labs.errors",
    "LabSourceEnv": "otto.labs.sources",
    "JsonFileLabRepository": "otto.labs.json_repository",
    "HostSummary": "otto.labs.protocol",
    "LabRepository": "otto.labs.protocol",
    "LoginSummary": "otto.labs.protocol",
    "SupportsHostSummaries": "otto.labs.protocol",
    "logins_of_creds": "otto.labs.protocol",
    "logins_of_host_data": "otto.labs.protocol",
    "register_lab_repository": "otto.labs.registry",
    "build_lab_sources": "otto.labs.sources",
    "host_summaries": "otto.labs.summaries",
    "list_host_ids": "otto.labs.summaries",
}


def __getattr__(name: str) -> object:
    """PEP 562 lazy resolver for otto.labs's public exports."""
    import importlib

    if name in _LAZY_ATTRS:
        return getattr(importlib.import_module(_LAZY_ATTRS[name]), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """Include the lazy exports in dir()/tab-completion; the module dict holds none of them."""
    return sorted(set(globals()) | set(_LAZY_ATTRS))


__all__ = [
    "CompositeLabRepository",
    "HostSummary",
    "JsonFileLabRepository",
    "LabNotFoundError",
    "LabRepository",
    "LabRepositoryError",
    "LabSource",
    "LabSourceConstructionError",
    "LabSourceEnv",
    "LoginSummary",
    "SupportsHostSummaries",
    "build_lab_sources",
    "host_summaries",
    "list_host_ids",
    "logins_of_creds",
    "logins_of_host_data",
    "register_lab_repository",
]
