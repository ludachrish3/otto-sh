"""Root test conftest — registers the shared async timeout fixture for dev test runs."""

import os

# Disable colored CLI output before typer/click/rich are imported anywhere.
# CI runners (e.g. GitHub Actions) set FORCE_COLOR, which causes Rich to embed
# ANSI escapes in help/error text and breaks substring assertions like
# `'--flag' in result.output`.
os.environ["NO_COLOR"] = "1"
os.environ["TERM"] = "dumb"
for _var in ("FORCE_COLOR", "CLICOLOR_FORCE", "PY_COLORS", "CLICOLOR"):
    os.environ.pop(_var, None)

import pytest  # noqa: E402

from otto.suite import timeout  # noqa: E402
from otto.logger import getOttoLogger  # noqa: E402

_logger = getOttoLogger()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item: pytest.Item):
    """Implement ``@pytest.mark.retry(n)`` for dev pytest runs.

    Provides the marker under bare ``pytest`` — ``otto.suite.plugin.OttoPlugin``
    only registers under ``otto test``. Used to gate known-flaky integration
    tests (nc transfers through an SSH hop) — see
    ``todo/hop_nc_transfer_flake.md`` for the underlying issue.

    Implemented as a hookwrapper so the first attempt runs through the default
    hook and retries override the outcome on success — a plain ``tryfirst``
    impl would let the default re-run (and possibly fail) the test after a
    retry succeeded.
    """
    outcome = yield
    retry_marker = item.get_closest_marker('retry')
    if retry_marker is None or outcome.excinfo is None:
        return
    n = int(retry_marker.args[0]) if retry_marker.args else 1
    first_exc = outcome.excinfo[1]
    _logger.warning(f'retry: {item.nodeid} attempt 1/{n} failed: {first_exc}')
    for attempt in range(1, n):
        try:
            item.runtest()
        except Exception as exc:
            _logger.warning(
                f'retry: {item.nodeid} attempt {attempt + 1}/{n} failed: {exc}'
            )
            outcome.force_exception(exc)
            continue
        outcome.force_result(None)
        return


def pytest_configure(config):  # type: ignore[no-untyped-def]
    if not config.pluginmanager.has_plugin('otto-timeout'):
        config.pluginmanager.register(timeout, name='otto-timeout')
