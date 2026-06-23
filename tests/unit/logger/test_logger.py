import os
import time
from os import (
    listdir,
)
from pathlib import Path

import pytest as pytest

from otto.logger.logger import (
    get_otto_logger,
    init_otto_logger,
)

logger = get_otto_logger()


@pytest.fixture(autouse=True, scope='function')
def create_logger(tmpdir):
    """Clear the environment variables Otto cares about before every test"""

    init_otto_logger(xdir=tmpdir, log_level='INFO', keep_days=7)
    logger.create_output_dir(command='pytest', subcommand='logger_test')

def _backdate(directory: Path, seconds: float) -> None:
    """Set ``directory``'s mtime ``seconds`` into the past.

    ``remove_old_logs`` keys off ``st_mtime``; backdating explicitly makes the
    age cutoff deterministic instead of racing a sub-second wall-clock window.
    """
    past = time.time() - seconds
    os.utime(directory, (past, past))


def test_remove_old_logs_ignores_non_output_entries():
    """Stray files and non-output directories must never be pruned.

    Regression guard: with a misconfigured ``xdir`` (e.g. left pointing at the
    repo root by a leaked global), the old code walked real content and called
    ``rmtree('docs/conf.py')`` → NotADirectoryError. Only timestamped output
    directories may be pruned, even when the strays are older than the cutoff.
    """
    cmd_dir = logger.xdir / 'pytest'
    stray_file = cmd_dir / 'conf.py'
    stray_file.write_text('not a log dir')
    stray_dir = cmd_dir / 'guide'
    stray_dir.mkdir()
    _backdate(stray_file, seconds=3600)
    _backdate(stray_dir, seconds=3600)

    # Must not raise, and must leave both strays untouched.
    logger.remove_old_logs(seconds=60)

    assert stray_file.exists(), 'remove_old_logs deleted a stray file'
    assert stray_dir.exists(), 'remove_old_logs deleted a non-output directory'


def test_remove_old_logs_old_logs_do_not_exist(tmpdir, caplog):

    xdir = logger.xdir

    assert len(listdir(xdir)) == 1
    logger.remove_old_logs(seconds=60)
    assert len(listdir(xdir)) == 1

    assert len(caplog.records) == 0

def test_remove_old_logs_xdir_does_not_exist(tmpdir, caplog):
    """remove_old_logs returns cleanly when xdir does not exist."""
    logger.xdir = Path(tmpdir) / 'nonexistent'
    logger.remove_old_logs(seconds=60)
    assert len(caplog.records) == 0

# TODO: Look into a better way to automate verification of this test.
# Currently does not colorize or any other rich formatting
# Maybe verify stdout content?
def test_log_formatting(tmpdir, caplog):
    logger.info("[magenta]This is important")
    assert len(caplog.records) == 1
