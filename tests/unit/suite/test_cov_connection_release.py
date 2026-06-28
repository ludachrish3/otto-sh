"""Regression: OttoSuite must release host connections at class teardown under --cov.

`otto test --cov` runs the suite via ``pytest.main()`` (each class on its own
``loop_scope='class'`` event loop), then runs coverage collection via a separate
``asyncio.run(_run_coverage)``.  A persistent shell session — and the single
socket of an RTOS telnet console — is bound to the loop that opened it; reusing
it from the collector's loop hangs (reads await futures on the now-closed class
loop) and the stale single-client socket blocks the collector's reconnect.

The ``_otto_release_connections`` fixture closes host connections in the class
loop that created them, but only under ``--cov`` (ordinary runs keep their
persistent sessions and pay no reconnect cost).
"""

import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from otto.suite.plugin import otto_cov_key
from otto.suite.suite import OttoSuite


def _request(cov: bool) -> MagicMock:
    req = MagicMock()
    stash = {otto_cov_key: cov}
    req.config.stash.get.side_effect = lambda key, default=None: stash.get(key, default)
    return req


async def _drive(request: MagicMock) -> None:
    suite = OttoSuite.__new__(OttoSuite)  # bare instance; the fixture only needs request
    gen = OttoSuite._otto_release_connections.__wrapped__(suite, request)
    await gen.__anext__()  # setup → suspend at yield
    with contextlib.suppress(StopAsyncIteration):
        await gen.__anext__()  # resume → run teardown


@pytest.mark.asyncio
async def test_release_connections_closes_hosts_under_cov():
    host = MagicMock(id="sprout_cov")
    host.close = AsyncMock()
    with patch("otto.configmodule.all_hosts", return_value=[host]):
        await _drive(_request(cov=True))
    host.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_release_connections_noop_without_cov():
    host = MagicMock(id="sprout_cov")
    host.close = AsyncMock()
    with patch("otto.configmodule.all_hosts", return_value=[host]):
        await _drive(_request(cov=False))
    host.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_release_connections_tolerates_close_errors():
    """A host that fails to close must not break teardown for the others."""
    bad = MagicMock(id="bad")
    bad.close = AsyncMock(side_effect=RuntimeError("boom"))
    good = MagicMock(id="good")
    good.close = AsyncMock()
    with patch("otto.configmodule.all_hosts", return_value=[bad, good]):
        await _drive(_request(cov=True))  # must not raise
    good.close.assert_awaited_once()
