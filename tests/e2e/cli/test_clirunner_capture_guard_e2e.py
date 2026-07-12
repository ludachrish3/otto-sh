"""Regression guard: the issue-#110 CliRunner shield reaches the e2e tree too.

The ``_clirunner_live_log_capture_guard`` fixture (root ``tests/conftest.py``)
detaches pytest's live-log handlers for the duration of every
``CliRunner.invoke``. Without it, any record reaching the ROOT logger mid-invoke
makes pytest suspend stdout capture, which drops click's isolated stream
wrapper; its ``__del__`` closes the buffer and typer's
``outstreams[0].getvalue()`` raises ``ValueError: I/O operation on closed file``.

The guard originally lived in ``tests/unit/conftest.py``, so it covered only the
unit tree — while ``tests/e2e/cli`` drives the very same ``CliRunner`` against
commands (``otto monitor``) that log from non-otto loggers (uvicorn, asyncio)
during the invoke. That gap is what issue #133 hit: two e2e tests died on the
#110 signature once ``otto monitor``'s review branch began really booting a
server. This test pins the guard's reach into THIS tree — it fails loudly if the
fixture is ever narrowed back to ``tests/unit``.

Mirrors ``tests/unit/cli/test_clirunner_capture_guard.py`` on purpose: each tree
proves its own coverage, since the bug was one of scope, not of logic.
"""

import gc
import logging

import pytest
import typer
from typer.testing import CliRunner

pytestmark = pytest.mark.hostless

runner = CliRunner()


def _logging_app(logger_name: str) -> typer.Typer:
    app = typer.Typer()

    @app.command()
    def go() -> None:
        logging.getLogger(logger_name).warning("mid-invoke record")
        typer.echo("done")

    return app


def test_e2e_invoke_survives_non_otto_logger_during_invoke() -> None:
    """A non-otto logger record mid-invoke must not corrupt the runner's stream."""
    app = _logging_app("thirdparty.e2e_deep")
    for _ in range(30):
        result = runner.invoke(app, [])
        gc.collect()  # force __del__ of any dropped isolated stream wrapper
        assert result.exit_code == 0, result.output
        assert "done" in result.output
