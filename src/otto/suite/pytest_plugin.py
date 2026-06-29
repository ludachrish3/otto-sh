"""Pytest plugin objects for otto suites. Imported only when running a suite —
kept out of register.py so importing the registry never pulls in pytest."""
from typing import Any

import pytest


class OttoOptionsPlugin:
    """Pytest plugin that provides the suite Options instance as a fixture.

    Tests request the ``suite_options`` fixture as a parameter::

        async def test_something(self, suite_options) -> None:
            assert suite_options.device_type == "router"
    """

    __name__ = "otto-options"

    def __init__(self, options: Any | None) -> None:
        self.options = options

    @pytest.fixture(scope="session")
    def suite_options(self) -> Any:
        """Return the Options dataclass instance populated from CLI arguments."""
        return self.options

    @pytest.fixture
    def ctx(self) -> Any:
        """Return the active OttoContext for this invocation."""
        from ..context import get_context

        return get_context()
