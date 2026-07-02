"""Pytest plugin objects for otto suites.

Imported only when running a suite — kept out of register.py so importing the
registry never pulls in pytest.
"""

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

    @pytest.fixture(scope="class")
    def suite_options(self, request: pytest.FixtureRequest) -> Any:
        """Return the suite's Options instance.

        Single-suite runs (``otto test <SuiteName> --flags``) pass the
        CLI-built instance in — returned as-is. Selection runs
        (``otto test --tests ...`` / ``-m ...``) span suites, so each suite's
        ``Options`` is default-constructed once per class; required fields
        make the suite's tests fail with a pointer at the single-suite form.
        """
        if self.options is not None:
            return self.options
        cls = getattr(request, "cls", None)
        if cls is None:
            return None
        opts_cls = getattr(cls, "Options", None)
        if opts_cls is None:
            return None
        try:
            return opts_cls()
        except Exception as exc:  # noqa: BLE001 — opts_cls() may raise pydantic ValidationError, TypeError, or any other construction error; all are reported as a missing-options hint
            pytest.fail(
                f"suite {cls.__name__!r} has required options — "
                f"run `otto test {cls.__name__} ...` to pass them ({exc})",
                pytrace=False,
            )

    @pytest.fixture
    def ctx(self) -> Any:
        """Return the active OttoContext for this invocation."""
        from ..context import get_context

        return get_context()
