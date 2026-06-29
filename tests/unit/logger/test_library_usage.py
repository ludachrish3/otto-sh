"""Guarantees for using otto as a library — no CLI-specific handler baggage."""

import logging

import pytest

from otto.logger import get_otto_logger, management


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
        get_otto_logger("demo").warning("library warning")
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
    management.capture_external_loggers(["some_product_pkg"])
    management.reset()

    otto = _otto()
    assert otto.propagate is True
    assert all(isinstance(h, logging.NullHandler) for h in otto.handlers)
    assert management._state.listener is None
    # The product prefix logger must not retain otto's QueueHandler after reset.
    from logging.handlers import QueueHandler

    prod = logging.getLogger("some_product_pkg")
    assert not any(isinstance(h, QueueHandler) for h in prod.handlers)
