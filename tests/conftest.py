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


# ---------------------------------------------------------------------------
# Asyncio leak detector (diagnostic, autouse on host tests)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _detect_asyncio_leaks(request):
    """Attribute leaked asyncio transports to the test that created them.

    Recipe from ``~/wiki/inbox/2026-04-24-detect-asyncio-leaks-at-source.md``:
    after each test, look for live transports whose ``_loop`` is closed.
    Those are the things that fire ``ResourceWarning`` from ``__del__`` at
    GC time and are then escalated by pytest's ``[unraisable]`` plugin into
    a ``PytestUnraisableExceptionWarning`` on whichever *next* test happens
    to be running — the source of the xdist-flake symptom.

    Only enabled by setting ``OTTO_DETECT_ASYNCIO_LEAKS=1`` in the env so
    it doesn't slow the regular run with the per-test ``gc.collect()``.
    """
    yield
    import os
    if not os.environ.get('OTTO_DETECT_ASYNCIO_LEAKS'):
        return
    import gc
    from asyncio.base_subprocess import BaseSubprocessTransport
    from asyncio.selector_events import _SelectorTransport
    gc.collect()
    leaks = []
    for o in gc.get_objects():
        if not isinstance(o, (BaseSubprocessTransport, _SelectorTransport)):
            continue
        loop = getattr(o, '_loop', None)
        if loop is None or not loop.is_closed():
            continue
        # Filter to ones that would actually emit a ResourceWarning from
        # __del__: i.e., the transport is still "open" (closing flag unset).
        # Already-closed transports don't warn even if they linger in GC.
        closing = getattr(o, '_closing', None)
        sock = getattr(o, '_sock', None)
        details = f' closing={closing} sock={sock!r}'
        # Show what's referencing this transport so we can find the leak.
        referrers = gc.get_referrers(o)
        ref_summary = ', '.join(
            f'{type(r).__module__}.{type(r).__name__}'
            for r in referrers[:5] if r is not gc.get_referrers and r is not leaks
        )
        leaks.append(f"{o!r}{details}\n    referrers: {ref_summary}")
    if leaks:
        # Print rather than raise: we want to *attribute* the leak, not
        # fail the test that detected it.
        print(f"\nLEAK after {request.node.nodeid}: {len(leaks)} live transport(s) bound to closed loop:")
        for l in leaks:
            print(f"  {l}")
