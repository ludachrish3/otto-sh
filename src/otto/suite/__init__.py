"""Public API for otto test suites: ``OttoSuite``, ``OttoOptionsPlugin``, ``run_suite``."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .pytest_plugin import OttoOptionsPlugin
    from .run import RunOptions, SuiteRunResult, find_suite, run_suite
    from .suite import OttoSuite

__all__ = [
    "OttoOptionsPlugin",
    "OttoSuite",
    "RunOptions",
    "SuiteRunResult",
    "find_suite",
    "run_suite",
]


def __getattr__(name: str) -> object:
    if name == "OttoSuite":
        from .suite import OttoSuite

        return OttoSuite
    if name == "OttoOptionsPlugin":
        from .pytest_plugin import OttoOptionsPlugin

        return OttoOptionsPlugin
    if name in {"RunOptions", "SuiteRunResult", "find_suite", "run_suite"}:
        from . import run

        return getattr(run, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
