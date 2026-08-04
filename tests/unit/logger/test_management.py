import logging
import logging.handlers

import pytest

from otto.host.host import HostFilter
from otto.logger import management
from otto.logger.mode import LogMode


@pytest.fixture(autouse=True)
def _clean_management():
    management.reset()
    yield
    management.reset()


def test_init_cli_logging_wires_console_handler_on_plain_logger(tmp_path):
    management.init_cli_logging(xdir=tmp_path, log_level="INFO", keep_days=7)
    otto = logging.getLogger("otto")
    assert otto.level == logging.INFO
    # A real (non-Null) handler is attached, on a PLAIN logging.Logger.
    assert type(otto) is logging.Logger
    assert any(not isinstance(h, logging.NullHandler) for h in otto.handlers)


def test_create_output_dir_returns_and_creates_dir(tmp_path):
    management.init_cli_logging(xdir=tmp_path, log_level="INFO", keep_days=7)
    out = management.create_output_dir("test", "mysuite")
    assert out.exists()
    assert out.is_dir()
    assert out.parent == tmp_path / "test"
    assert (out / "console.log").exists()  # file handler created the console transcript
    # The library NullHandler must not be funneled into the QueueListener.
    listener_handlers = management._state.listener.handlers
    assert any(not isinstance(h, logging.NullHandler) for h in listener_handlers)
    assert not any(isinstance(h, logging.NullHandler) for h in listener_handlers)


def test_create_output_dir_writes_console_and_verbose(tmp_path):
    management.init_cli_logging(xdir=tmp_path, log_level="INFO", keep_days=7)
    out = management.create_output_dir("test", "mysuite")
    assert (out / "console.log").exists()
    assert (out / "verbose.log").exists()
    assert not (out / "otto.log").exists()


def test_verbose_log_keeps_quiet_console_log_drops_it(tmp_path):
    management.init_cli_logging(xdir=tmp_path, log_level="INFO", keep_days=7)
    management.attach_console_suppress_filter(HostFilter())
    out = management.create_output_dir("test")
    log = logging.getLogger("otto")
    host = type("H", (), {"name": "h1"})()
    log.info("@h1 > | quiet line", extra={"host": host, "log_mode": LogMode.QUIET})
    management._state.listener.stop()  # flush the queue
    assert "quiet line" in (out / "verbose.log").read_text()
    assert "quiet line" not in (out / "console.log").read_text()


def test_verbose_floor():
    assert management.verbose_floor("INFO") == logging.INFO
    assert management.verbose_floor("WARNING") == logging.INFO
    assert management.verbose_floor("DEBUG") == logging.DEBUG


def test_remove_old_logs_respects_time_budget(tmp_path, monkeypatch):
    management.init_cli_logging(xdir=tmp_path, log_level="INFO", keep_days=7)
    cmd_dir = tmp_path / "test"
    cmd_dir.mkdir(parents=True, exist_ok=True)
    import os

    olds = []
    for i in range(6):
        d = cmd_dir / f"20200101_0000{i:02d}_000"
        d.mkdir()
        past = 10_000.0
        os.utime(d, (d.stat().st_atime - past, d.stat().st_mtime - past))
        olds.append(d)
    ticks = iter([float(n) for n in range(1000)])
    monkeypatch.setattr(management.time, "monotonic", lambda: next(ticks))
    management.remove_old_logs(seconds=60, time_budget=2.5)
    assert [d for d in olds if d.exists()], "budget should stop before removing all"


def test_library_import_attaches_nullhandler():
    import otto.logger

    otto = logging.getLogger("otto")
    assert any(isinstance(h, logging.NullHandler) for h in otto.handlers)


def test_capture_external_logger_lands_in_sinks(tmp_path):
    management.init_cli_logging(xdir=tmp_path, log_level="INFO", keep_days=7)
    out = management.create_output_dir("test")
    management.capture_external_loggers(["myproduct"])
    logging.getLogger("myproduct.install").info("product line")
    logging.getLogger("asyncssh").info("third-party noise")
    management._state.listener.stop()  # flush the queue
    verbose = (out / "verbose.log").read_text()
    assert "product line" in verbose
    assert "third-party noise" not in verbose


def test_set_capture_prefixes_auto_applies_on_output_dir(tmp_path):
    # set_capture_prefixes stashes a wishlist (called from the main callback,
    # before the QueueHandler exists); create_output_dir attaches it later.
    management.init_cli_logging(xdir=tmp_path, log_level="INFO", keep_days=7)
    management.set_capture_prefixes(["myproduct"])
    out = management.create_output_dir("test")
    logging.getLogger("myproduct.install").info("auto product line")
    management._state.listener.stop()  # flush the queue
    assert "auto product line" in (out / "verbose.log").read_text()


def test_reset_detaches_captured_external_loggers(tmp_path):
    management.init_cli_logging(xdir=tmp_path, log_level="INFO", keep_days=7)
    management.create_output_dir("test")
    management.capture_external_loggers(["myproduct"])
    lg = logging.getLogger("myproduct")
    assert any(isinstance(h, logging.handlers.QueueHandler) for h in lg.handlers)
    management.reset()
    assert not any(isinstance(h, logging.handlers.QueueHandler) for h in lg.handlers)
    assert lg.level == logging.NOTSET


# ---------------------------------------------------------------------------
# shutdown_listener's bounded flush (the sync_phase force path's only caller)
#
# The force-path call site wraps this in contextlib.suppress(Exception), so a
# regression here (probe inverted, _thread rename, enqueue failure) would be
# swallowed silently forever — these tests are the only loud surface it has.
# ---------------------------------------------------------------------------


class _CaptureHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def _record(msg: str) -> logging.LogRecord:
    return logging.LogRecord("otto.flush", logging.INFO, __file__, 1, msg, None, None)


def _listener_with_queued_record(msg: str):
    import queue

    capture = _CaptureHandler()
    q: "queue.Queue[logging.LogRecord]" = queue.Queue(-1)
    listener = logging.handlers.QueueListener(q, capture)
    listener.start()
    q.put(_record(msg))
    management._state.listener = listener
    return listener, q, capture


def test_shutdown_listener_bounded_flush_drains_and_claims():
    listener, _q, capture = _listener_with_queued_record("flush-me")
    management.shutdown_listener(join_timeout=5.0)
    assert capture.messages == ["flush-me"], "bounded flush must drain queued records"
    assert management._state.listener is None, "the listener must be claimed out of module state"
    thread = listener._thread
    assert thread is None or not thread.is_alive(), "the listener thread must have stopped"
    # Idempotent: a second call (force watchdog racing atexit) no-ops.
    management.shutdown_listener(join_timeout=5.0)


def test_shutdown_listener_bounded_flush_skips_when_queue_mutex_held():
    listener, q, capture = _listener_with_queued_record("never-flushed")
    # A wedged producer holds the queue mutex: the force path must abandon the
    # flush (never the exit) — and must not enqueue the stop sentinel.
    assert q.mutex.acquire(blocking=False)
    try:
        management.shutdown_listener(join_timeout=5.0)
    finally:
        q.mutex.release()
    assert management._state.listener is None, "state is claimed even when the flush is skipped"
    assert listener._thread is not None
    assert listener._thread.is_alive(), (
        "the listener must NOT have been stopped through a held mutex"
    )
    # Cleanup: drain and stop for real now that the mutex is free.
    listener.stop()
    assert capture.messages == ["never-flushed"]


def test_stop_listener_unbounded_branch_still_stops():
    listener, _q, capture = _listener_with_queued_record("atexit-path")
    management._stop_listener()
    assert capture.messages == ["atexit-path"]
    assert management._state.listener is None
    assert listener._thread is None, "the unbounded branch uses QueueListener.stop()"
