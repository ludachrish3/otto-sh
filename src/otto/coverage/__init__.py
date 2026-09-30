"""Coverage collection and reporting for gcov-instrumented binaries.

Coverage works in two steps:

1. **Collect**: ``otto test --cov`` fetches ``.gcda`` files from remote
   hosts into the suite's output directory using
   :class:`~otto.coverage.fetcher.remote.GcdaFetcher`.

2. **Report**: ``otto cov`` merges collected ``.gcda`` files, loads
   coverage data, and renders a multi-tier HTML report using
   :class:`~otto.coverage.reporter.CoverageReporter`.

Every name is exported lazily (PEP 562): a plain ``otto test`` imports
``otto.coverage.instrumentation`` to decide whether coverage applies, and that
must not pull the reporter, the merger and the store it never uses. Only a
caller that names one of them pays for its module.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config.coverage_settings import CoverageConfigError as CoverageConfigError
    from .collect import CollectResult as CollectResult
    from .collect import clean_remote_gcda as clean_remote_gcda
    from .collect import collect_coverage as collect_coverage
    from .config import DestinationError as DestinationError
    from .errors import CoverageNotInstrumentedError as CoverageNotInstrumentedError
    from .errors import NoCoverageDataError as NoCoverageDataError
    from .fetcher.remote import GcdaFetcher as GcdaFetcher
    from .report_inputs import ReportInputs as ReportInputs
    from .report_inputs import resolve_report_inputs as resolve_report_inputs
    from .reporter import CoverageReporter as CoverageReporter
    from .store.model import CoverageStore as CoverageStore

# name -> the module that defines it, imported on first access by __getattr__.
_LAZY_ATTRS: dict[str, str] = {
    "CollectResult": "otto.coverage.collect",
    "CoverageConfigError": "otto.config.coverage_settings",
    "CoverageNotInstrumentedError": "otto.coverage.errors",
    "CoverageReporter": "otto.coverage.reporter",
    "CoverageStore": "otto.coverage.store.model",
    "DestinationError": "otto.coverage.config",
    "GcdaFetcher": "otto.coverage.fetcher.remote",
    "NoCoverageDataError": "otto.coverage.errors",
    "ReportInputs": "otto.coverage.report_inputs",
    "clean_remote_gcda": "otto.coverage.collect",
    "collect_coverage": "otto.coverage.collect",
    "resolve_report_inputs": "otto.coverage.report_inputs",
}


def __getattr__(name: str) -> object:
    """PEP 562 lazy resolver for otto.coverage's public exports."""
    import importlib

    if name in _LAZY_ATTRS:
        return getattr(importlib.import_module(_LAZY_ATTRS[name]), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """Include the lazy exports in dir()/tab-completion; the module dict holds none of them."""
    return sorted(set(globals()) | set(_LAZY_ATTRS))


__all__ = [
    "CollectResult",
    "CoverageConfigError",
    "CoverageNotInstrumentedError",
    "CoverageReporter",
    "CoverageStore",
    "DestinationError",
    "GcdaFetcher",
    "NoCoverageDataError",
    "ReportInputs",
    "clean_remote_gcda",
    "collect_coverage",
    "resolve_report_inputs",
]
