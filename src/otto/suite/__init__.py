"""Public API for otto test suites: ``OttoSuite``, ``OttoOptionsPlugin``."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .pytest_plugin import OttoOptionsPlugin
    from .suite import OttoSuite

__all__ = ["OttoOptionsPlugin", "OttoSuite"]


def __getattr__(name: str) -> object:
    if name == "OttoSuite":
        from .suite import OttoSuite

        return OttoSuite
    if name == "OttoOptionsPlugin":
        from .pytest_plugin import OttoOptionsPlugin

        return OttoOptionsPlugin
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
