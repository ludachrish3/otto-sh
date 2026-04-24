from os import (
    listdir,
)
from pathlib import Path, PosixPath
from time import sleep

import pytest as pytest

from otto.logger.logger import (
    getOttoLogger,
    initOttoLogger,
)

logger = getOttoLogger()

# TODO: This needs to conditionally define MockPath to be a WindowsPath
class MockPath(PosixPath):

    mtimeAdj: int
    '''Number of seconds to add to the file's mtime stat field.'''

    def stat(self, *args, **kwargs):
        stat = super().stat()
        stat.st_mtime += self.mtimeAdj
        return stat

@pytest.fixture(autouse=True, scope='function')
def create_logger(tmpdir):
    """Clear the environment variables Otto cares about before every test"""

    initOttoLogger(xdir=tmpdir, log_level='INFO', keep_days=7)
    logger.create_output_dir(command='pytest', subcommand='logger_test')

@pytest.mark.integration
def test_removeOldLogs_old_logs_exist_same_command(caplog):

    sleep(0.02)

    logger.create_output_dir(command='pytest', subcommand='thing1')
    logger.create_output_dir(command='pytest', subcommand='thing2')

    pytest_dir = logger.xdir / 'pytest'

    # Make sure there are 3 output dirs to start (1 from the standard fixture, then the above 2)
    assert len(listdir(pytest_dir)) == 3

    logger.removeOldLogs(seconds=0.01)
    assert len(listdir(pytest_dir)) == 2

    assert len(caplog.records) == 1
    logrecord = caplog.records[0]
    assert logrecord.message == '[magenta]Deleting log directories that are more than 0 days old'

@pytest.mark.integration
def test_removeOldLogs_old_logs_exist_different_command(caplog):

    pytest_dir = logger.xdir / 'pytest'
    not_pytest_dir = logger.xdir / 'not_pytest'

    # Create another log_dir around the same time as the first,
    # but with a different command
    logger.create_output_dir(command='not_pytest', subcommand='thing1')

    sleep(0.02)

    logger.create_output_dir(command='pytest', subcommand='thing1')
    logger.create_output_dir(command='not_pytest', subcommand='thing2')

    assert len(listdir(pytest_dir)) == 2
    assert len(listdir(not_pytest_dir)) == 2
    logger.removeOldLogs(seconds=0.02)
    assert len(listdir(pytest_dir)) == 1
    assert len(listdir(pytest_dir)) == 1

    assert len(caplog.records) == 1
    logrecord = caplog.records[0]
    assert logrecord.message == '[magenta]Deleting log directories that are more than 0 days old'

def test_removeOldLogs_old_logs_do_not_exist(tmpdir, caplog):

    xdir = logger.xdir

    assert len(listdir(xdir)) == 1
    logger.removeOldLogs(seconds=60)
    assert len(listdir(xdir)) == 1

    assert len(caplog.records) == 0

def test_removeOldLogs_xdir_does_not_exist(tmpdir, caplog):
    """removeOldLogs returns cleanly when xdir does not exist."""
    logger.xdir = Path(tmpdir) / 'nonexistent'
    logger.removeOldLogs(seconds=60)
    assert len(caplog.records) == 0

# TODO: Look into a better way to automate verification of this test.
# Currently does not colorize or any other rich formatting
# Maybe verify stdout content?
def test_log_formatting(tmpdir, caplog):
    logger.info("[magenta]This is important")
    assert len(caplog.records) == 1
