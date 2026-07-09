"""Regression guard for issue #110 — CliRunner invokes survive mid-invoke logging.

With ``log_cli = true``, a log record reaching the root logger during a
``CliRunner.invoke`` used to close the runner's isolated stream (via pytest's
live-log capture-suspension), so typer's ``outstreams[0].getvalue()`` raised
``ValueError: I/O operation on closed file``. The unit-tree
``_clirunner_live_log_capture_guard`` autouse fixture (tests/unit/conftest.py)
detaches the live-log handlers for the invoke window. These tests exercise the
exact mechanism — a command that logs on a NON-``otto`` logger (which
``no_logger_output_dir``'s ``otto.propagate=False`` does not cover) — and force
GC after each invoke so the vulnerable ``__del__`` fires deterministically. They
pass with the guard and fail loudly (``ValueError``) if it is removed.
"""

import gc
import logging

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
