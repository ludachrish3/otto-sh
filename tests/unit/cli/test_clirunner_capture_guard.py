"""Regression guard for issue #110 — CliRunner invokes survive mid-invoke logging.

With ``log_cli = true``, a log record reaching the root logger during a
``CliRunner.invoke`` used to close the runner's isolated stream (via pytest's
live-log capture-suspension), so typer's ``outstreams[0].getvalue()`` raised
``ValueError: I/O operation on closed file``. The ROOT-conftest
``_clirunner_live_log_capture_guard`` autouse fixture (moved there from the
unit tree by the #133 fix) detaches the live-log handlers for the invoke
window. These tests exercise the exact mechanism — a command that logs on a
NON-``otto`` logger (which ``no_logger_output_dir``'s ``otto.propagate=False``
does not cover) — and force GC after each invoke so the vulnerable ``__del__``
fires deterministically. They pass with the guard and fail loudly
(``ValueError``) if it is removed. The last test pins the guard's fail-loud
arm: a pytest rename of the private handler classes must turn every test red
by name, never silently disarm the shield (review §5.4).
"""

import gc
import logging

import pytest
import typer
from typer.testing import CliRunner

runner = CliRunner()


def _logging_app(logger_name: str) -> typer.Typer:
    app = typer.Typer()

    @app.command()
    def go() -> None:
        logging.getLogger(logger_name).warning("mid-invoke record")
        typer.echo("done")

    return app


def test_invoke_survives_non_otto_logger_during_invoke():
    """A non-otto logger record mid-invoke must not corrupt the runner's stream."""
    app = _logging_app("thirdparty.deep")
    for _ in range(30):
        result = runner.invoke(app, [])
        gc.collect()  # force __del__ of any dropped isolated stream wrapper
        assert result.exit_code == 0, result.output
        assert "done" in result.output


def test_invoke_still_lets_caplog_capture_mid_invoke_records(caplog):
    """The guard is surgical: caplog still captures logs emitted during the invoke."""
    app = _logging_app("thirdparty.caplog_probe")
    with caplog.at_level("WARNING"):
        result = runner.invoke(app, [])
        gc.collect()
    assert result.exit_code == 0, result.output
    assert any("mid-invoke record" in rec.message for rec in caplog.records)


def test_guard_fails_loud_when_pytest_renames_the_handlers(monkeypatch):
    """A vanished ``_LiveLoggingStreamHandler`` must fail, not silently yield.

    The old arm was ``except ImportError: yield`` — a pytest upgrade would
    have inertly disarmed the guard for every CliRunner site while the tests
    above (which pin reach and surgicality, not liveness) stayed green. Drives
    the guard's generator body directly with the import broken and expects the
    named hard failure.
    """
    from tests.conftest import _clirunner_guard_impl

    monkeypatch.delattr("_pytest.logging._LiveLoggingStreamHandler")
    gen = _clirunner_guard_impl()
    with pytest.raises(pytest.fail.Exception, match="cannot arm"):
        next(gen)
