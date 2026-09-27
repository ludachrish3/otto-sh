"""Public API for otto test suites: ``OttoSuite``, ``OttoOptionsPlugin``, ``run_suite``.

Also exports the suite-less selection API: ``run_selection`` (run tests by
name/marker without a suite subcommand), ``find_suite``, and the typed
``NoTestsMatchedError`` / ``UnknownSelectionError`` / ``SuiteRunResult`` /
``RunOptions`` records the two run paths share.

Every name is exported lazily (PEP 562), the shape every otto package shares:
naming ``OttoSuite`` does not load the run machinery or the pytest plugin.
The resolver does not write a resolved name back into the module dict; see
``otto.config``'s ``__dir__`` for why.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .pytest_plugin import OttoOptionsPlugin as OttoOptionsPlugin
    from .run import NoTestsMatchedError as NoTestsMatchedError
    from .run import RunOptions as RunOptions
    from .run import SuiteRunResult as SuiteRunResult
    from .run import find_suite as find_suite
    from .run import run_selection as run_selection
    from .run import run_suite as run_suite
    from .selection import UnknownSelectionError as UnknownSelectionError
    from .suite import OttoSuite as OttoSuite

# name -> the module that defines it, imported on first access by __getattr__.
_LAZY_ATTRS: dict[str, str] = {
    "OttoOptionsPlugin": "otto.suite.pytest_plugin",
    "NoTestsMatchedError": "otto.suite.run",
    "RunOptions": "otto.suite.run",
    "SuiteRunResult": "otto.suite.run",
    "find_suite": "otto.suite.run",
    "run_selection": "otto.suite.run",
    "run_suite": "otto.suite.run",
    "UnknownSelectionError": "otto.suite.selection",
    "OttoSuite": "otto.suite.suite",
}


def __getattr__(name: str) -> object:
    """PEP 562 lazy resolver for otto.suite's public exports."""
    import importlib

    if name in _LAZY_ATTRS:
        return getattr(importlib.import_module(_LAZY_ATTRS[name]), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """Include the lazy exports in dir()/tab-completion; the module dict holds none of them."""
    return sorted(set(globals()) | set(_LAZY_ATTRS))


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
