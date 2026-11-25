"""The lab sources (DB-agnostic backends) and the lab model with its loader (``loader``).

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
    from .composite import HostListing as HostListing
    from .composite import LabSource as LabSource
    from .composite import LabStatusReport as LabStatusReport
    from .composite import SourceLinks as SourceLinks
    from .composite import SourceUnavailable as SourceUnavailable
    from .drops import Diagnostic as Diagnostic
    from .errors import LabNotFoundError as LabNotFoundError
    from .errors import LabRepositoryError as LabRepositoryError
    from .errors import LabSourceConstructionError as LabSourceConstructionError
    from .errors import LabUnavailableError as LabUnavailableError
    from .protocol import DeclaredLinkInputs as DeclaredLinkInputs
    from .protocol import HostSummary as HostSummary
    from .protocol import LabContribution as LabContribution
    from .protocol import LabRepository as LabRepository
    from .protocol import LabScope as LabScope
    from .protocol import LabStatus as LabStatus
    from .protocol import LoginSummary as LoginSummary
    from .protocol import SkippedHostRecord as SkippedHostRecord
    from .protocol import SourceRef as SourceRef
    from .protocol import SupportsDeclaredLinks as SupportsDeclaredLinks
    from .protocol import SupportsHostSummaries as SupportsHostSummaries
    from .protocol import SupportsHostSummariesInView as SupportsHostSummariesInView
    from .protocol import SupportsInventoryView as SupportsInventoryView
    from .protocol import SupportsLabContribution as SupportsLabContribution
    from .protocol import SupportsLabScope as SupportsLabScope
    from .protocol import SupportsLabStatus as SupportsLabStatus
    from .protocol import SupportsOverridePolicy as SupportsOverridePolicy
    from .protocol import logins_of_creds as logins_of_creds
    from .protocol import logins_of_host_data as logins_of_host_data
    from .registry import register_lab_repository as register_lab_repository
    from .sources import JSON_FILE_RULE as JSON_FILE_RULE
    from .sources import LabFileRule as LabFileRule
    from .sources import LabSourceEnv as LabSourceEnv
    from .sources import LabSourceFacts as LabSourceFacts
    from .sources import build_lab_sources as build_lab_sources
    from .summaries import host_summaries as host_summaries
    from .summaries import host_summaries_in_view as host_summaries_in_view
    from .summaries import list_host_ids as list_host_ids
    from .views import lab_scope_of as lab_scope_of
    from .views import lab_statuses as lab_statuses
    from .views import labs_view as labs_view
    from .views import load_contribution_through as load_contribution_through
    from .views import load_lab_through as load_lab_through

# name -> the module that defines it, imported on first access by __getattr__.
_LAZY_ATTRS: dict[str, str] = {
    "CompositeLabRepository": "otto.labs.composite",
    "LabSource": "otto.labs.composite",
    "HostListing": "otto.labs.composite",
    "LabStatusReport": "otto.labs.composite",
    "SourceLinks": "otto.labs.composite",
    "SourceUnavailable": "otto.labs.composite",
    "Diagnostic": "otto.labs.drops",
    "LabNotFoundError": "otto.labs.errors",
    "LabRepositoryError": "otto.labs.errors",
    "LabSourceConstructionError": "otto.labs.errors",
    "LabUnavailableError": "otto.labs.errors",
    "JSON_FILE_RULE": "otto.labs.sources",
    "LabFileRule": "otto.labs.sources",
    "LabSourceEnv": "otto.labs.sources",
    "LabSourceFacts": "otto.labs.sources",
    "DeclaredLinkInputs": "otto.labs.protocol",
    "SkippedHostRecord": "otto.labs.protocol",
    "HostSummary": "otto.labs.protocol",
    "LabContribution": "otto.labs.protocol",
    "LabRepository": "otto.labs.protocol",
    "LabScope": "otto.labs.protocol",
    "LabStatus": "otto.labs.protocol",
    "LoginSummary": "otto.labs.protocol",
    "SourceRef": "otto.labs.protocol",
    "SupportsDeclaredLinks": "otto.labs.protocol",
    "SupportsHostSummaries": "otto.labs.protocol",
    "SupportsHostSummariesInView": "otto.labs.protocol",
    "SupportsInventoryView": "otto.labs.protocol",
    "SupportsLabContribution": "otto.labs.protocol",
    "SupportsLabScope": "otto.labs.protocol",
    "SupportsLabStatus": "otto.labs.protocol",
    "SupportsOverridePolicy": "otto.labs.protocol",
    "logins_of_creds": "otto.labs.protocol",
    "logins_of_host_data": "otto.labs.protocol",
    "register_lab_repository": "otto.labs.registry",
    "build_lab_sources": "otto.labs.sources",
    "host_summaries": "otto.labs.summaries",
    "host_summaries_in_view": "otto.labs.summaries",
    "list_host_ids": "otto.labs.summaries",
    "lab_scope_of": "otto.labs.views",
    "lab_statuses": "otto.labs.views",
    "labs_view": "otto.labs.views",
    "load_contribution_through": "otto.labs.views",
    "load_lab_through": "otto.labs.views",
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
    "JSON_FILE_RULE",
    "CompositeLabRepository",
    "DeclaredLinkInputs",
    "Diagnostic",
    "HostListing",
    "HostSummary",
    "LabContribution",
    "LabFileRule",
    "LabNotFoundError",
    "LabRepository",
    "LabRepositoryError",
    "LabScope",
    "LabSource",
    "LabSourceConstructionError",
    "LabSourceEnv",
    "LabSourceFacts",
    "LabStatus",
    "LabStatusReport",
    "LabUnavailableError",
    "LoginSummary",
    "SkippedHostRecord",
    "SourceLinks",
    "SourceRef",
    "SourceUnavailable",
    "SupportsDeclaredLinks",
    "SupportsHostSummaries",
    "SupportsHostSummariesInView",
    "SupportsInventoryView",
    "SupportsLabContribution",
    "SupportsLabScope",
    "SupportsLabStatus",
    "SupportsOverridePolicy",
    "build_lab_sources",
    "host_summaries",
    "host_summaries_in_view",
    "lab_scope_of",
    "lab_statuses",
    "labs_view",
    "list_host_ids",
    "load_contribution_through",
    "load_lab_through",
    "logins_of_creds",
    "logins_of_host_data",
    "register_lab_repository",
]
