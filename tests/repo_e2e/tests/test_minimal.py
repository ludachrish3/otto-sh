"""Minimal env-gated fixture tests for CLI e2e tests.

e2e tests use this class to verify discovery (``otto test --list-tests``) and
invocation (the exit-code contract) without touching any real host.
"""

import os

from repo_e2e_instructions.options import E2EFixtureOptions


class TestE2EFixture:
    """Deterministic hostless fixture tests for CLI e2e tests."""

    async def test_gated(self, ctx) -> None:
        """Passes normally; fails only when OTTO_E2E_FAIL=1 (exit-code contract)."""
        label = ctx.options(E2EFixtureOptions).label
        assert os.environ.get("OTTO_E2E_FAIL") != "1", f"OTTO_E2E_FAIL=1 (label={label!r})"
