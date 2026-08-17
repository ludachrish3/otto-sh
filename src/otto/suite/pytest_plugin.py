"""Pytest plugin objects for otto suites.

Imported only when running a suite — kept out of register.py so importing the
registry never pulls in pytest.
"""

from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio

from ..errors import EnsureStateError

if TYPE_CHECKING:
    # Typing only: the converge functions are imported inside the fixture
    # bodies (see `ensure_installed`), so nothing here needs otto.project at
    # module scope — and `Result` is only ever read, never constructed.
    from ..result import Result


def _raise_unless_converged(result: "Result", verb: str) -> None:
    """Turn a non-ok converge *result* into an error naming the failing host.

    One helper rather than the same two lines in each fixture below: the three
    messages have to stay identical, and a mirrored default that drifts is the
    failure mode this whole layer exists to avoid.

    ``is_ok``, not ``status is Status.Success``, and the difference is the
    common case: a converge with nothing to do reports ``Status.Skipped``
    ("already installed"), which is a pass. Only ``Failed`` / ``Error`` /
    ``NotRun`` reach the raise.
    """
    if not result.is_ok:
        raise EnsureStateError(f"{verb} failed: {result.msg}")


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

    # ── converging fixtures ──────────────────────────────────────────────────
    #
    # Function-scoped (pytest's default) on purpose: the guarantee is per test
    # CASE, and the cost of asking again when the state already holds is one
    # `status()` sweep.
    #
    # `loop_scope="function"`, and it is MEASURED rather than assumed. Under
    # `otto test`'s pytest arguments (`asyncio_mode=auto`, neither
    # `asyncio_default_test_loop_scope` nor `asyncio_default_fixture_loop_scope`
    # set) pytest-asyncio gives every test — suite method or plain function —
    # its OWN function-scoped loop, and gives a `loop_scope="class"` fixture a
    # THIRD loop that no test ever runs on. "Class" does not mean "the loop the
    # class's tests share": they do not share one. A converge opens host
    # connections, and a connection is bound to the loop that opened it, so a
    # class-scoped converge would hand the test sessions whose reads await
    # futures on a loop that is no longer running — the hang
    # `OttoSuite._otto_release_connections` exists to prevent under `--cov`.
    # Function loop scope puts the converge on exactly the loop the requesting
    # test runs on. Stated explicitly rather than left to the ini default so a
    # repo that sets `asyncio_default_fixture_loop_scope` cannot move it off
    # that loop. `test_ensure_fixtures_are_available_to_a_suite` pins the loop
    # identity, and fails under `loop_scope="class"`.
    #
    # Each is a one-line wrapper over `otto.project`'s converge layer, imported
    # inside the body: the CLI's `otto run install` calls the same function, so
    # the fixture and the command cannot diverge. The import is deliberately
    # from the package (`..project`), not from `..project.orchestrator` — the
    # re-export is the seam every other caller uses.

    @pytest_asyncio.fixture(loop_scope="function")
    async def ensure_installed(self) -> None:
        """Converge the lab to fully-installed before this test.

        Cheap when already installed (one status sweep). A PARTIAL lab is
        uninstalled first, then installed fresh. Convergence failure ERRORS
        the test with the failing host named — never a skip.
        """
        from ..project import ensure_installed as _ensure

        _raise_unless_converged(await _ensure(), "ensure_installed")

    @pytest_asyncio.fixture(loop_scope="function")
    async def ensure_uninstalled(self) -> None:
        """Converge the lab to fully-uninstalled before this test.

        A lab that is already uninstalled is a no-op; a PARTIAL one runs the
        uninstall, which is the case a boolean "is it installed?" cannot see.
        Convergence failure ERRORS the test with the failing host named.
        """
        from ..project import ensure_uninstalled as _ensure

        _raise_unless_converged(await _ensure(), "ensure_uninstalled")

    @pytest_asyncio.fixture(loop_scope="function")
    async def ensure_clean(self) -> None:
        """Converge the lab to clean before this test.

        Stronger than uninstalled: dev tools and toolchain tools are not
        products, so an uninstalled-but-tooled lab still gets cleaned.
        Convergence failure ERRORS the test with the failing host named.
        """
        from ..project import ensure_clean as _ensure

        _raise_unless_converged(await _ensure(), "ensure_clean")
