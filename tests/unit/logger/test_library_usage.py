"""Guarantees for using otto as a library — no CLI-specific handler baggage."""

import json
import logging
import subprocess
import sys

import pytest

from otto.logger import management


@pytest.fixture(autouse=True)
def _clean_management():
    management.reset()
    yield
    management.reset()


def _otto():
    return logging.getLogger("otto")


def test_plain_import_attaches_only_nullhandler():
    # Fresh library-citizen state (the autouse reset() restored it).
    handlers = _otto().handlers
    assert handlers, "otto should carry its NullHandler"
    assert all(isinstance(h, logging.NullHandler) for h in handlers)


def test_library_logger_propagates_to_consumer_root():
    # A library consumer configures ITS OWN handler on the root logger; otto's
    # records must reach it (propagate=True in library-citizen mode).
    assert _otto().propagate is True
    records = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    root = logging.getLogger()
    handler = _Capture()
    root.addHandler(handler)
    try:
        logging.getLogger("otto.demo").warning("library warning")
    finally:
        root.removeHandler(handler)
    assert "library warning" in records


def test_no_queue_listener_or_file_handlers_on_import():
    assert management._state.listener is None
    # No FileHandler / QueueHandler anywhere on 'otto' in library mode.
    from logging.handlers import QueueHandler

    for h in _otto().handlers:
        assert not isinstance(h, (logging.FileHandler, QueueHandler))


def test_reset_restores_library_citizen_state_after_cli_init(tmp_path):
    # Simulate a CLI run, then reset() and confirm we're back to library mode.
    management.init_cli_logging(xdir=tmp_path, log_level="INFO", keep_days=7)
    management.create_output_dir("test")
    management.reset()

    otto = _otto()
    assert otto.propagate is True
    assert all(isinstance(h, logging.NullHandler) for h in otto.handlers)
    assert management._state.listener is None
    # Root carries none of otto's handlers any more (the CLI installs there).
    root = logging.getLogger()
    assert not [h for h in root.handlers if getattr(h, management.OTTO_HANDLER_ATTR, False)]


def test_import_otto_touches_neither_root_handlers_nor_levels():
    """Spec §3.4: library posture — import configures nothing beyond otto's NullHandler.

    Measured in a FRESH interpreter, which is the only shape in which the import
    is a real act: ``otto`` is already in this process's ``sys.modules`` (line 7
    imports it), so an in-process ``import otto`` runs no module code and any
    before/after snapshot around it reduces to ``assert x == x``.

    The subprocess reports root's ``(handler count, level)`` before and after
    ``import otto``. Both must be a virgin root logger — no handlers, WARNING.
    """
    script = (
        "import json, logging\n"
        "root = logging.getLogger()\n"
        "before = [len(root.handlers), root.level]\n"
        "import otto  # the act under test\n"
        "after = [len(root.handlers), root.level]\n"
        "print(json.dumps({'before': before, 'after': after, 'otto_nulls': "
        "sum(isinstance(h, logging.NullHandler) for h in logging.getLogger('otto').handlers)}))\n"
    )
    # check=False: a non-zero exit is reported by the assertion below, which
    # shows the child's stderr — CalledProcessError would hide it.
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=120, check=False
    )
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    seen = json.loads(proc.stdout)
    assert seen["before"] == [0, logging.WARNING], seen
    assert seen["after"] == [0, logging.WARNING], seen
    # The one thing the import IS allowed to do (otto/__init__.py), pinned so
    # the assertions above can't pass by the import having quietly failed.
    assert seen["otto_nulls"] == 1, seen
