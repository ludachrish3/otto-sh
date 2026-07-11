"""Public API for otto test suites: ``OttoSuite``, ``OttoOptionsPlugin``, ``run_suite``.

Also exports the suite-less selection API: ``run_selection`` (run tests by
name/marker without a suite subcommand), ``find_suite``, and the typed
``NoTestsMatchedError`` / ``UnknownSelectionError`` / ``SuiteRunResult`` /
``RunOptions`` records the two run paths share.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .pytest_plugin import OttoOptionsPlugin
    from .run import (
        NoTestsMatchedError,
        RunOptions,
        SuiteRunResult,
        find_suite,
        run_selection,
        run_suite,
    )
    from .selection import UnknownSelectionError
    from .suite import OttoSuite

__all__ = [
    "NoTestsMatchedError",
    "OttoOptionsPlugin",
    "OttoSuite",
    "RunOptions",
    "SuiteRunResult",
    "UnknownSelectionError",
    "find_suite",
    "run_selection",
    "run_suite",
]


def __getattr__(name: str) -> object:
    if name == "OttoSuite":
        from .suite import OttoSuite

        return OttoSuite
    if name == "OttoOptionsPlugin":
        from .pytest_plugin import OttoOptionsPlugin

        return OttoOptionsPlugin
    if name == "UnknownSelectionError":
        from .selection import UnknownSelectionError

        return UnknownSelectionError
    if name in {
        "NoTestsMatchedError",
        "RunOptions",
        "SuiteRunResult",
        "find_suite",
        "run_selection",
        "run_suite",
    }:
        from . import run

        return getattr(run, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
