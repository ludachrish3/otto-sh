"""Coverage collection and reporting for gcov-instrumented binaries.

Coverage works in two steps:

1. **Collect**: ``otto test --cov`` fetches ``.gcda`` files from remote
   hosts into the suite's output directory using
   :class:`~otto.coverage.fetcher.remote.GcdaFetcher`.

2. **Report**: ``otto cov`` merges collected ``.gcda`` files, loads
   coverage data, and renders a multi-tier HTML report using
   :class:`~otto.coverage.reporter.CoverageReporter`.
"""

from .collect import CollectResult, clean_remote_gcda, collect_coverage
from .errors import CoverageConfigError, NoCoverageDataError
from .fetcher.remote import GcdaFetcher
from .reporter import CoverageReporter
from .store.model import CoverageStore

__all__ = [
    "CollectResult",
    "CoverageConfigError",
    "CoverageReporter",
    "CoverageStore",
    "GcdaFetcher",
    "NoCoverageDataError",
    "clean_remote_gcda",
    "collect_coverage",
]
