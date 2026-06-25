"""Docstring for otto.logger.logger"""

import atexit
import re
from datetime import datetime, timedelta
from logging import (
    FileHandler,
    Logger,
    LogRecord,
    getLogger,
    getLoggerClass,
    setLoggerClass,
)
from logging.handlers import QueueHandler, QueueListener
from os import listdir
from pathlib import Path
from queue import Queue
from shutil import rmtree
from typing import cast

from rich.highlighter import NullHighlighter
from rich.logging import RichHandler

from ..console import CONSOLE
from .formatters import (
    RichFormatter,
    format_log_time,
)


# Matches the timestamp directory names that ``create_output_dir`` writes:
# ``YYYYMMDD_HHMMSS_mmm`` optionally followed by ``_<subcommand>``. Used by
# ``remove_old_logs`` as a fail-safe so that a misconfigured ``xdir`` (e.g.
# accidentally pointing at a project root) can't lead to rmtree'ing
# unrelated subdirectories — only otto-created log dirs are candidates.
_LOG_DIR_NAME_RE = re.compile(r'^\d{8}_\d{6}_\d{3}(_.+)?$')


class OttoLogger(Logger):
    """
    Root logger for the Otto framework.

    Only the root "otto" logger is an OttoLogger instance. Child loggers
    (e.g. "otto.host", "otto.cli") are standard Logger instances that
    propagate log records up to this root, where handlers are attached.

    All log methods can provide the following arguments to modify log statements:\n
    The rich console markup syntax can be used to color and style logs.
    The rich console panel options can be used to add headers, footers, and other frills.
    """

    def __init__(self,
        name: str,
        level: int = 0,
    ) -> None:
        super().__init__(name, level)

        self._xdir: Path
        self._output_dir: Path
        self._rich_logging: bool = False
        self._listener: QueueListener | None = None
        self._keep_seconds: float | None = None
        self._output_dir_atexit_registered: bool = False

    @property
    def xdir(self):
        """Base directory in which all logs are written."""

        return self._xdir

    @xdir.setter
    def xdir(self, xdir: str | Path):
        self._xdir = Path(xdir)

    @property
    def output_dir(self):
        """Base directory in which logs and other artifacts are stored for an invocation."""
        return self._output_dir

    @property
    def keep_seconds(self) -> float | None:
        """Log retention period in seconds, set from --log-days during initialization."""
        return self._keep_seconds

    @keep_seconds.setter
    def keep_seconds(self, value: float) -> None:
        self._keep_seconds = value

    def _command_to_dir_name(self,
        command: str,
    ) -> str:

        return command.replace('-', '_')

    def create_output_dir(self,
        command: str,
        subcommand: str | None = None,
    ):
        """Set base directory in which logs are written for an invocation.

        Args:
            command: Top level command (e.g. run, test, monitor).
            subcommand: The main argument to the command, if there is one.
        """

        # Name the xdir down to the millisecond.
        # %f provides microseconds, so slicing off the last 3 digits gives milliseconds
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')[:-3]

        command = self._command_to_dir_name(command)

        if subcommand is not None:
            subcommand = f'_{self._command_to_dir_name(subcommand)}'
        else:
            subcommand = ''

        # Subcommands are kabob case, but the convention for directories is snake case.
        # Replace all hyphens with underscores in the subcommand name.
        dir_name = f"{timestamp}{subcommand}"

        self._output_dir = self.xdir / command / dir_name

        # Create the output directory and save its path
        self._output_dir.mkdir(parents=True)

        # Register once so the final output dir is always printed last, after all
        # log listeners have been stopped (atexit is LIFO; subsequent listener.stop
        # registrations will run before this one).
        if not self._output_dir_atexit_registered:
            atexit.register(lambda: CONSOLE.print(f"\nOutput directory: {self._output_dir}", highlight=False))
            self._output_dir_atexit_registered = True

        if self._keep_seconds is not None:
            self.remove_old_logs(self._keep_seconds)

        self._add_log_handlers()

    def _add_log_handlers(self):
        """Wrap existing and new handlers in a QueueListener for non-blocking I/O."""

        existing_handlers = list(self.handlers)
        for h in existing_handlers:
            self.removeHandler(h)

        log_file_path = self._output_dir / 'otto.log'
        log_file = FileHandler(log_file_path, mode='x')
        richFormatter = RichFormatter()
        richFormatter.rich = self.rich_logging
        log_file.setFormatter(richFormatter)

        log_queue: Queue[LogRecord] = Queue(-1)
        self._listener = QueueListener(log_queue, *existing_handlers, log_file,
                                       respect_handler_level=True)
        self.addHandler(QueueHandler(log_queue))
        self._listener.start()
        atexit.register(self._listener.stop)

    def remove_old_logs(self,
        seconds: float,
    ):
        """
        Remove all logs older than `seconds` seconds old.

        This method deals with seconds, enabling quick unit testing.

        Args:
            seconds: Number of seconds to retain old logs.
        """

        xdir = self.xdir

        if not xdir.is_dir():
            return

        oldest = datetime.now() - timedelta(seconds=seconds)
        oldest = oldest.timestamp()
        loggedDeletion = False

        for cmd_dir_name in listdir(xdir):
            cmd_dir = xdir / cmd_dir_name
            # Skip stray files that callers (e.g. tests) may have written
            # alongside the expected ``<cmd>/<log_dir>/`` tree.
            if not cmd_dir.is_dir():
                continue
            for log_dir_name in listdir(cmd_dir):
                output_dir = cmd_dir / log_dir_name

                # Fail-safe: only rmtree entries that match the timestamped
                # log-dir naming pattern ``create_output_dir`` writes. Without
                # this, a misconfigured xdir (the original test_cov.py flake
                # left it pointing at the project root) would walk arbitrary
                # subtrees and rmtree any old-enough directory.
                if not _LOG_DIR_NAME_RE.match(log_dir_name):
                    continue
                if not output_dir.is_dir():
                    continue
                if output_dir.stat().st_mtime < oldest:

                    # Only log the fact that logs are being deleted once when the first old directory is found
                    if not loggedDeletion:
                        days = seconds / 60 / 60 / 24
                        days_str = f'{days:0.0f} {"day" if days == 1 else "days"}'

                        self.info(f"[magenta]Deleting log directories that are more than {days_str} old")
                        loggedDeletion = True
                    rmtree(output_dir)
                    self.debug(f"Removed {output_dir}")

    @property
    def rich_logging(self):
        return self._rich_logging

    @rich_logging.setter
    def rich_logging(self, flag: bool):
        self._rich_logging = flag


def init_otto_logger(
    xdir: Path,
    log_level: str,
    keep_days: float,
    rich_log_file: bool = False,
    verbose: bool = False,
) -> OttoLogger:
    """
    Initialize the root OttoLogger.

    Args:
        xdir (Path): _description_
        log_level (str): _description_
    """

    logger = get_otto_logger()

    logger.setLevel(log_level)
    is_debug = log_level == 'DEBUG'

    stdout_handler = RichHandler(level=log_level,
                                 console=CONSOLE,
                                 show_time=verbose,
                                 tracebacks_max_frames=20,
                                 tracebacks_show_locals=True,
                                 markup=True,
                                 highlighter=NullHighlighter(),
                                 show_path=is_debug,
                                 enable_link_path=False,
                                 log_time_format=format_log_time,
                                 omit_repeated_times=False,
                                )

    logger.addHandler(stdout_handler)

    # Ensure that the xdir is set
    logger.xdir = xdir

    # Set the rich log file status now in case a log file is eventually created
    logger.rich_logging = rich_log_file

    # Store retention period so subcommand callbacks can trigger cleanup when
    # a real command runs (not when help is displayed).
    logger.keep_seconds = keep_days * 24 * 60 * 60

    return logger


def get_otto_logger(
    name: str | None = None,
) -> OttoLogger:
    """
    Return the OttoLogger hierarchy logger with the specified name.

    If no name is specified, return the root OttoLogger.
    Named loggers are standard Logger instances that propagate to the root OttoLogger.
    """

    logger_name = f"otto.{name}" if name else "otto"

    prev_class = getLoggerClass()
    setLoggerClass(OttoLogger)
    try:
        logger = getLogger(logger_name)
        return cast(OttoLogger, logger)

    # Ensure that the logger class is set back to the previous value
    finally:
        setLoggerClass(prev_class)
