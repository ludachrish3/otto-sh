"""Public API for otto tests: ``run_tests`` and the ``OttoFixturesPlugin`` pytest plugin.

``run_tests`` runs tests by name and/or marker across every repo, taking a
``RunOptions`` record and returning a ``SuiteRunResult``; a selection that
matches nothing raises ``NoTestsMatchedError``, or ``UnknownSelectionError``
for a name no collected test matches.

Every name is exported lazily (PEP 562), the shape every otto package shares:
importing ``otto.suite`` does not load the run machinery or the pytest plugin.
The resolver does not write a resolved name back into the module dict; see
``otto.config``'s ``__dir__`` for why.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .pytest_plugin import OttoFixturesPlugin as OttoFixturesPlugin
    from .run import NoTestsMatchedError as NoTestsMatchedError
    from .run import RunOptions as RunOptions
    from .run import SuiteRunResult as SuiteRunResult
    from .run import run_tests as run_tests
    from .selection import UnknownSelectionError as UnknownSelectionError

# name -> the module that defines it, imported on first access by __getattr__.
_LAZY_ATTRS: dict[str, str] = {
    "OttoFixturesPlugin": "otto.suite.pytest_plugin",
    "NoTestsMatchedError": "otto.suite.run",
    "RunOptions": "otto.suite.run",
    "SuiteRunResult": "otto.suite.run",
    "run_tests": "otto.suite.run",
    "UnknownSelectionError": "otto.suite.selection",
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
    "OttoFixturesPlugin",
    "RunOptions",
    "SuiteRunResult",
    "UnknownSelectionError",
    "run_tests",
]
