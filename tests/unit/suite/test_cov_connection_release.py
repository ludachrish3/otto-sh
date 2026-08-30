"""Regression: OttoSuite must release host connections at class teardown under --cov.

`otto test --cov` runs the suite via ``pytest.main()`` (each class on its own
``loop_scope='class'`` event loop), then runs coverage collection via a separate
``asyncio.run(collect_coverage)``.  A persistent shell session — and the single
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
    fixture_fn = OttoSuite._otto_release_connections.__wrapped__
    # A classmethod fixture: the wrapped object may be the classmethod itself.
    fixture_fn = getattr(fixture_fn, "__func__", fixture_fn)
    gen = fixture_fn(OttoSuite, request)
    await gen.__anext__()  # setup → suspend at yield
    with contextlib.suppress(StopAsyncIteration):
        await gen.__anext__()  # resume → run teardown


@pytest.mark.asyncio
async def test_release_connections_closes_hosts_under_cov():
    host = MagicMock(id="zephyr37-llext")
    host.close = AsyncMock()
    with patch("otto.config.all_hosts", return_value=[host]):
        await _drive(_request(cov=True))
    host.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_release_connections_noop_without_cov():
    host = MagicMock(id="zephyr37-llext")
    host.close = AsyncMock()
    with patch("otto.config.all_hosts", return_value=[host]):
        await _drive(_request(cov=False))
    host.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_release_connections_tolerates_close_errors():
    """A host that fails to close must not break teardown for the others."""
    bad = MagicMock(id="bad")
    bad.close = AsyncMock(side_effect=RuntimeError("boom"))
    good = MagicMock(id="good")
    good.close = AsyncMock()
    with patch("otto.config.all_hosts", return_value=[bad, good]):
        await _drive(_request(cov=True))  # must not raise
    good.close.assert_awaited_once()


def test_release_fixture_is_a_classmethod_and_warns_nothing(tmp_path) -> None:
    """Spec §5.6 / Appendix A.2: the instance-method form is deprecated in pytest 9.1.
    Run a real inner session with that deprecation escalated to an error.

    The suite requests ``suite_dir``/``test_dir``/``suite_options`` so the
    session sets up EVERY class-scoped fixture otto provides:
    ``OttoSuite._otto_release_connections`` (autouse, classmethod),
    ``OttoPlugin._otto_class_monitor_task`` (autouse, staticmethod) and
    ``OttoOptionsPlugin.suite_dir`` / ``suite_options`` (staticmethods).
    pytest's check (``resolve_fixture_function``) only asks whether the bound
    ``__self__`` is a type, so a class-scoped instance method on a plugin
    OBJECT fails it exactly as one on a test class does — which is why the
    plugin fixtures are staticmethods reaching their plugin through
    ``request.config.stash``.
    """
    import sys

    from otto.config.lab import Lab
    from otto.context import OttoContext, reset_context, set_context
    from otto.suite.plugin import OttoPlugin
    from otto.suite.pytest_plugin import OttoOptionsPlugin
    from otto.suite.run import ASYNCIO_LOOP_ARGS

    test_file = tmp_path / "test_nowarn.py"
    test_file.write_text("""\
from otto.suite.suite import OttoSuite


class _Opts:
    pass


class TestNoWarn(OttoSuite):
    Options = _Opts

    async def test_a(self, suite_dir, test_dir, suite_options) -> None:
        assert isinstance(suite_options, _Opts)
""")
    token = set_context(OttoContext(lab=Lab(name="_test_stub"), output_dir=tmp_path))
    try:
        exit_code = pytest.main(
            [
                str(test_file),
                "-o",
                "asyncio_mode=auto",
                *ASYNCIO_LOOP_ARGS,
                "--no-cov",
                "--override-ini",
                "addopts=",
                "-p",
                "no:playwright",
                "-W",
                "error::pytest.PytestRemovedIn10Warning",
            ],
            plugins=[OttoPlugin(), OttoOptionsPlugin(None)],
        )
    finally:
        sys.modules.pop(test_file.stem, None)
        reset_context(token)
    assert exit_code == pytest.ExitCode.OK
