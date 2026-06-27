import os
import time
from os import listdir
from pathlib import Path

import pytest

from otto.logger import management


@pytest.fixture(autouse=True, scope='function')
def create_logger(tmpdir):
    """Initialize management state and create output dir before every test."""
    management.init_cli_logging(xdir=tmpdir, log_level='INFO', keep_days=7)
    management.create_output_dir(command='pytest', subcommand='logger_test')
    yield
    management.reset()


def _backdate(directory: Path, seconds: float) -> None:
    """Set ``directory``'s mtime ``seconds`` into the past.

    ``remove_old_logs`` keys off ``st_mtime``; backdating explicitly makes the
    age cutoff deterministic instead of racing a sub-second wall-clock window.
    """
    past = time.time() - seconds
    os.utime(directory, (past, past))


def test_remove_old_logs_old_logs_exist_same_command(caplog):
    """Old log directories are pruned; newer ones are kept (same command)."""
    management.create_output_dir(command='pytest', subcommand='thing1')
    management.create_output_dir(command='pytest', subcommand='thing2')

    pytest_dir = management._state.xdir / 'pytest'

    # Make sure there are 3 output dirs to start (1 from the standard fixture, then the above 2)
    assert len(listdir(pytest_dir)) == 3

    # Backdate the fixture's logger_test dir an hour into the past; thing1 and
    # thing2 keep their real (current) mtime, so the result no longer depends
    # on how fast the machine reaches remove_old_logs.
    (old_dir,) = (d for d in pytest_dir.iterdir() if d.name.endswith('_logger_test'))
    _backdate(old_dir, seconds=3600)

    with caplog.at_level('INFO', logger='otto'):
        management.remove_old_logs(seconds=60)
    assert len(listdir(pytest_dir)) == 2

    assert len(caplog.records) == 1
    assert caplog.records[0].message == '[magenta]Deleting log directories that are more than 0 days old'


def test_remove_old_logs_old_logs_exist_different_command(caplog):
    """Old log directories are pruned across multiple command dirs."""
    pytest_dir = management._state.xdir / 'pytest'
    not_pytest_dir = management._state.xdir / 'not_pytest'

    # Create an old log_dir under each command, then a fresh one under each.
    management.create_output_dir(command='not_pytest', subcommand='thing1')
    management.create_output_dir(command='pytest', subcommand='thing1')
    management.create_output_dir(command='not_pytest', subcommand='thing2')

    assert len(listdir(pytest_dir)) == 2
    assert len(listdir(not_pytest_dir)) == 2

    # Backdate the older dirs an hour into the past; the rest keep their real
    # (current) mtime, so remove_old_logs deletes exactly the backdated ones
    # regardless of timing.
    _backdate(next(d for d in not_pytest_dir.iterdir() if d.name.endswith('_thing1')), seconds=3600)
    _backdate(next(d for d in pytest_dir.iterdir() if d.name.endswith('_logger_test')), seconds=3600)

    with caplog.at_level('INFO', logger='otto'):
        management.remove_old_logs(seconds=60)
    assert len(listdir(pytest_dir)) == 1
    assert len(listdir(not_pytest_dir)) == 1

    assert len(caplog.records) == 1
    assert caplog.records[0].message == '[magenta]Deleting log directories that are more than 0 days old'
