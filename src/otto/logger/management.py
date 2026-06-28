"""CLI-side logging management for otto.

otto-the-library only *emits* log records (via ``logging.getLogger('otto'…)``)
and never configures handlers — see ``otto.logger``. This module is the
application/CLI side: it configures the ``'otto'`` logger's handlers + formatters,
creates each invocation's output directory, and prunes old log directories.

**Context-free** by design (it does not import ``otto.context``):
``create_output_dir`` *creates and returns* the per-run directory; the CLI
records that path on ``OttoContext.output_dir``. These functions are called only
by ``otto/cli/*.py`` (and tests).

Scope guardrail: keep this strictly logging config + the per-run dir's
creation/rotation. It is not a catch-all.
"""

import atexit
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from logging import FileHandler, LogRecord, NullHandler, getLogger
from logging.handlers import QueueHandler, QueueListener
from os import listdir
from pathlib import Path
from queue import Queue
from shutil import rmtree

from rich.highlighter import NullHighlighter
from rich.logging import RichHandler

from ..console import CONSOLE
from .formatters import RichFormatter, format_log_time

# Matches the timestamp directory names ``create_output_dir`` writes:
# ``YYYYMMDD_HHMMSS_mmm`` optionally followed by ``_<subcommand>``. Fail-safe so
# a misconfigured ``xdir`` can't lead to rmtree'ing unrelated subtrees.
_LOG_DIR_NAME_RE = re.compile(r"^\d{8}_\d{6}_\d{3}(_.+)?$")

# Max wall-clock seconds ``remove_old_logs`` may spend scanning per call — a
# safety valve against stat storms on large/slow (e.g. NFS) trees; a backlog
# drains across subsequent runs.
LOG_ROTATE_BUDGET_SECONDS = 5.0


@dataclass
class _LogConfig:
    xdir: Path | None = None
    keep_seconds: float | None = None
    rich_log_file: bool = False
    last_output_dir: Path | None = None
    listener: QueueListener | None = None
    atexit_registered: bool = False
    console_handler: RichHandler | None = None


_state = _LogConfig()


def _stop_listener() -> None:
    """atexit-safe: stop the QueueListener once; a no-op if already stopped."""
    if _state.listener is not None:
        _state.listener.stop()
        _state.listener = None


def _print_output_dir() -> None:
    """atexit-safe: print the final output dir, unless reset() cleared it."""
    if _state.last_output_dir is not None:
        CONSOLE.print(f"\nOutput directory: {_state.last_output_dir}", highlight=False)


def reset() -> None:
    """Reset module state, detach otto's handlers, and restore library-citizen
    state (propagate=True + NullHandler) — test helper.

    Unregisters atexit callbacks before stopping the listener so that real-exit
    teardown never double-stops a listener or prints a ``None`` output dir.
    """
    atexit.unregister(_stop_listener)
    atexit.unregister(_print_output_dir)
    otto = getLogger("otto")
    for h in list(otto.handlers):
        otto.removeHandler(h)
    if _state.listener is not None:
        _state.listener.stop()
        for h in _state.listener.handlers:
            h.close()
    _state.xdir = None
    _state.keep_seconds = None
    _state.rich_log_file = False
    _state.last_output_dir = None
    _state.listener = None
    _state.atexit_registered = False
    _state.console_handler = None
    # Restore propagation to True so the logger behaves as a library logger
    # (i.e. propagates records to the root logger for test capture etc.).
    otto.propagate = True
    # Restore the library-citizen NullHandler (removed above; may have been
    # added by otto.logger.__init__ at import time — idempotent).
    if not any(isinstance(h, NullHandler) for h in otto.handlers):
        otto.addHandler(NullHandler())


def init_cli_logging(
    xdir: Path,
    log_level: str,
    keep_days: float,
    rich_log_file: bool = False,
    verbose: bool = False,
) -> None:
    """Configure the ``'otto'`` logger for a CLI invocation (was init_otto_logger)."""
    logger = getLogger("otto")
    logger.setLevel(log_level)
    # CLI has its own handlers; don't double-log to the root logger.
    logger.propagate = False
    is_debug = log_level == "DEBUG"

    stdout_handler = RichHandler(
        level=log_level,
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

    _state.xdir = Path(xdir)
    _state.rich_log_file = rich_log_file
    _state.keep_seconds = keep_days * 24 * 60 * 60
    _state.console_handler = stdout_handler


def _command_to_dir_name(command: str) -> str:
    return command.replace("-", "_")


def create_output_dir(command: str, subcommand: str | None = None) -> Path:
    """Create this invocation's output dir, wire the file handler, prune old
    logs, and return the dir. The caller records it on ``OttoContext.output_dir``.
    """
    if _state.xdir is None:
        raise RuntimeError("init_cli_logging() must run before create_output_dir() (xdir unset)")

    # Name the dir down to the millisecond (%f is microseconds; drop last 3).
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    command = _command_to_dir_name(command)
    sub = f"_{_command_to_dir_name(subcommand)}" if subcommand is not None else ""
    output_dir = _state.xdir / command / f"{timestamp}{sub}"
    output_dir.mkdir(parents=True)
    _state.last_output_dir = output_dir

    # Print the final output dir once at exit (atexit is LIFO; registering this
    # before the listener.stop below keeps it printing last).
    if not _state.atexit_registered:
        atexit.register(_print_output_dir)
        _state.atexit_registered = True

    if _state.keep_seconds is not None:
        remove_old_logs(_state.keep_seconds)

    _add_log_handlers(output_dir)
    return output_dir


def _add_log_handlers(output_dir: Path) -> None:
    """Wrap the console + file handlers in a QueueListener for non-blocking I/O.

    Only the console handler registered by ``init_cli_logging`` and the new
    ``FileHandler`` are fanned into the listener.  Any other handlers already
    present on the logger (e.g. pytest's log-capture handler) are left in place
    so that test-infrastructure capture keeps working with ``propagate=False``.
    """
    logger = getLogger("otto")
    # Remove only the handlers we own: the NullHandler, any QueueHandler from a
    # previous create_output_dir call, and the console handler (which is fanned
    # into the listener below — leaving it on the logger too would double-emit
    # every record to the console). External handlers (e.g. pytest caplog) are
    # NOT removed so they keep receiving records synchronously.
    for h in list(logger.handlers):
        if isinstance(h, (NullHandler, QueueHandler)) or h is _state.console_handler:
            logger.removeHandler(h)

    # Build the new async fan-out: console (non-blocking) + file.
    console_handlers = [_state.console_handler] if _state.console_handler is not None else []
    log_file = FileHandler(output_dir / "otto.log", mode="x")
    rich_formatter = RichFormatter()
    rich_formatter.rich = _state.rich_log_file
    log_file.setFormatter(rich_formatter)

    log_queue: Queue[LogRecord] = Queue(-1)
    _state.listener = QueueListener(
        log_queue, *console_handlers, log_file, respect_handler_level=True
    )
    logger.addHandler(QueueHandler(log_queue))
    _state.listener.start()
    atexit.register(_stop_listener)


def remove_old_logs(
    seconds: float,
    *,
    time_budget: float = LOG_ROTATE_BUDGET_SECONDS,
) -> None:
    """Remove log dirs older than ``seconds``, time-boxed to ``time_budget``.

    When the budget is exceeded the scan stops early and resumes on the next
    call, bounding the per-run cost on large/slow (e.g. NFS) trees.
    """
    logger = getLogger("otto")
    xdir = _state.xdir
    if xdir is None or not xdir.is_dir():
        return

    oldest = (datetime.now() - timedelta(seconds=seconds)).timestamp()
    logged_deletion = False
    start = time.monotonic()
    budget_hit = False

    for cmd_dir_name in listdir(xdir):
        if budget_hit:
            break
        cmd_dir = xdir / cmd_dir_name
        if not cmd_dir.is_dir():
            continue
        for log_dir_name in listdir(cmd_dir):
            if time.monotonic() - start > time_budget:
                budget_hit = True
                break
            output_dir = cmd_dir / log_dir_name
            if not _LOG_DIR_NAME_RE.match(log_dir_name):
                continue
            if not output_dir.is_dir():
                continue
            if output_dir.stat().st_mtime < oldest:
                if not logged_deletion:
                    days = seconds / 60 / 60 / 24
                    days_str = f"{days:0.0f} {'day' if days == 1 else 'days'}"
                    logger.info(
                        f"[magenta]Deleting log directories that are more than {days_str} old"
                    )
                    logged_deletion = True
                rmtree(output_dir)
                logger.debug(f"Removed {output_dir}")

    if budget_hit:
        logger.debug(
            "Log rotation hit its %gs time budget; remaining old directories "
            "will be removed on the next run.",
            time_budget,
        )
