"""suite_options: CLI instance when provided, per-class defaults otherwise."""

from pathlib import Path

import pytest

from otto.configmodule.lab import Lab
from otto.context import OttoContext, reset_context, set_context
from otto.suite.plugin import OttoPlugin

pytest_plugins = ["pytester"]

SUITE_SRC = """\
from typing import Annotated
import typer
from otto import options
from otto.suite import OttoSuite

@options
class _Defaulted:
    retries: Annotated[int, typer.Option(help="n")] = 3

class TestDefaulted(OttoSuite[_Defaulted]):
    Options = _Defaulted
    def test_gets_defaults(self, suite_options):
        assert suite_options.retries == 3

@options
class _Required:
    firmware: Annotated[str, typer.Option(help="fw")]

class TestRequired(OttoSuite[_Required]):
    Options = _Required
    def test_never_runs(self, suite_options):
        raise AssertionError("should have failed at fixture setup")
"""


# pytester's runpytest_inprocess spins up a *nested* pytest session inside
# this one. The outer suite's `filterwarnings = ["error"]` (pyproject.toml)
# turns pytest-asyncio's "asyncio_default_fixture_loop_scope is unset"
# deprecation warning into a fatal INTERNALERROR during inner-session
# configure. tests/unit/suite/test_plugin.py hits the same trap driving
# pytest.main() directly and works around it with this same `-o` override;
# mirrored here for the pytester-based inner runs.
# `-p no:playwright`: pytest-playwright's session-wide soft-assertion hook
# wraps every test call and rejects re-entry ("nested soft assertion scopes
# are not supported"), so it must be disabled for in-process nested sessions —
# same fix as test_otto_suite.py / test_plugin.py / the integration
# passthrough test. These inner runs use no Playwright fixtures.
INNER_ARGS = (
    "-p",
    "no:cacheprovider",
    "-p",
    "no:playwright",
    "-o",
    "asyncio_default_fixture_loop_scope=function",
)


@pytest.fixture(autouse=True)
def _otto_context(tmp_path: Path):
    """OttoSuite.setup_class reads get_context().output_dir — install a stub
    context for the duration of the inner pytest session, mirroring the
    `_run_inner_pytest` helper in tests/unit/suite/test_otto_suite.py.
    """
    ctx = OttoContext(lab=Lab(name="_test_stub"), output_dir=tmp_path)
    token = set_context(ctx)
    try:
        yield
    finally:
        reset_context(token)


# SUITES registry isolation (needed because each pytester run below registers
# TestDefaulted/TestRequired from a fresh temp path) is provided package-wide
# by the autouse ``_isolate_suites`` fixture in ``tests/unit/suite/conftest.py``.


def test_defaulted_options_are_constructed(pytester: pytest.Pytester) -> None:
    from otto.suite.pytest_plugin import OttoOptionsPlugin

    pytester.makepyfile(test_inner=SUITE_SRC)
    result = pytester.runpytest_inprocess(
        "-k",
        "TestDefaulted",
        *INNER_ARGS,
        plugins=[OttoPlugin(), OttoOptionsPlugin(None)],
    )
    # OttoPlugin.pytest_report_teststatus (src/otto/suite/plugin.py) returns
    # a "passed"/"failed" category for every report phase (setup/call/
    # teardown), not just "call" — so result.assert_outcomes() over-counts a
    # single passing test as 3 passes. tests/unit/suite/test_otto_suite.py's
    # inner-session helper hits the same thing and asserts on the session
    # exit code instead; mirrored here.
    assert result.ret == pytest.ExitCode.OK


def test_required_options_fail_with_suite_hint(pytester: pytest.Pytester) -> None:
    from otto.suite.pytest_plugin import OttoOptionsPlugin

    pytester.makepyfile(test_inner=SUITE_SRC)
    result = pytester.runpytest_inprocess(
        "-k",
        "TestRequired",
        *INNER_ARGS,
        plugins=[OttoPlugin(), OttoOptionsPlugin(None)],
    )
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines(["*required options*otto test TestRequired*"])


def test_explicit_instance_still_wins(pytester: pytest.Pytester) -> None:
    from otto.suite.pytest_plugin import OttoOptionsPlugin

    class _Sentinel:
        retries = 99

    pytester.makepyfile(test_inner=SUITE_SRC.replace("== 3", "== 99"))
    result = pytester.runpytest_inprocess(
        "-k",
        "TestDefaulted",
        *INNER_ARGS,
        plugins=[OttoPlugin(), OttoOptionsPlugin(_Sentinel())],
    )
    assert result.ret == pytest.ExitCode.OK
