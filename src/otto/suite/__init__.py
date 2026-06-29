"""Public API for otto test suites: ``OttoSuite``, ``register_suite``, ``OttoOptionsPlugin``."""
from typing import TYPE_CHECKING

from .register import register_suite as register_suite

if TYPE_CHECKING:
    from .pytest_plugin import OttoOptionsPlugin
    from .suite import OttoSuite

__all__ = ["OttoOptionsPlugin", "OttoSuite", "register_suite"]


def __getattr__(name: str) -> object:
    if name == "OttoSuite":
        from .suite import OttoSuite

        return OttoSuite
    if name == "OttoOptionsPlugin":
        from .pytest_plugin import OttoOptionsPlugin

        return OttoOptionsPlugin
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
