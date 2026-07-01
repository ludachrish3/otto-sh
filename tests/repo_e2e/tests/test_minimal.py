"""Minimal env-gated fixture suite for CLI e2e tests.

This suite registers with otto so that e2e tests can verify discovery
(``--list-suites``) and invocation (exit-code contract) without touching
any real host.
"""

import os
from typing import Annotated

import typer

from otto import options
from otto.suite import OttoSuite, register_suite


@options
class E2EFixtureOptions:
    label: Annotated[str, typer.Option(help="Label for the e2e fixture run.")] = "e2e"


@register_suite()
class TestE2EFixture(OttoSuite[E2EFixtureOptions]):
    """Deterministic hostless fixture suite for CLI e2e tests."""

    Options = E2EFixtureOptions

    async def test_gated(self, suite_options: E2EFixtureOptions) -> None:
        """Passes normally; fails only when OTTO_E2E_FAIL=1 (exit-code contract)."""
        assert os.environ.get("OTTO_E2E_FAIL") != "1"
