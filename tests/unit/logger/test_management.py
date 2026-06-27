import logging

import pytest

from otto.logger import management


@pytest.fixture(autouse=True)
def _clean_management():
    management.reset()
    yield
    management.reset()


def test_init_cli_logging_wires_console_handler_on_plain_logger(tmp_path):
    management.init_cli_logging(xdir=tmp_path, log_level='INFO', keep_days=7)
    otto = logging.getLogger('otto')
    assert otto.level == logging.INFO
    # A real (non-Null) handler is attached, on a PLAIN logging.Logger.
    assert type(otto) is logging.Logger
    assert any(not isinstance(h, logging.NullHandler) for h in otto.handlers)


def test_create_output_dir_returns_and_creates_dir(tmp_path):
    management.init_cli_logging(xdir=tmp_path, log_level='INFO', keep_days=7)
    out = management.create_output_dir('test', 'mysuite')
    assert out.exists() and out.is_dir()
    assert out.parent == tmp_path / 'test'
    assert (out / 'otto.log').exists()  # file handler created the log file
    # The library NullHandler must not be funneled into the QueueListener.
    listener_handlers = management._state.listener.handlers
    assert any(not isinstance(h, logging.NullHandler) for h in listener_handlers)
    assert not any(isinstance(h, logging.NullHandler) for h in listener_handlers)


def test_remove_old_logs_respects_time_budget(tmp_path, monkeypatch):
    management.init_cli_logging(xdir=tmp_path, log_level='INFO', keep_days=7)
    cmd_dir = tmp_path / 'test'
    cmd_dir.mkdir(parents=True, exist_ok=True)
    import os
    olds = []
    for i in range(6):
        d = cmd_dir / f'20200101_0000{i:02d}_000'
        d.mkdir()
        past = 10_000.0
        os.utime(d, (os.stat(d).st_atime - past, os.stat(d).st_mtime - past))
        olds.append(d)
    ticks = iter([float(n) for n in range(0, 1000)])
    monkeypatch.setattr(management.time, 'monotonic', lambda: next(ticks))
    management.remove_old_logs(seconds=60, time_budget=2.5)
    assert [d for d in olds if d.exists()], 'budget should stop before removing all'


def test_library_import_attaches_nullhandler():
    import otto.logger  # noqa: F401
    otto = logging.getLogger('otto')
    assert any(isinstance(h, logging.NullHandler) for h in otto.handlers)
