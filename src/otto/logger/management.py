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
import logging
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from logging import FileHandler, Filter, LogRecord, NullHandler, getLogger
from logging.handlers import QueueHandler, QueueListener
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
    console_log_handler: FileHandler | None = None
    verbose_handler: FileHandler | None = None
    log_level: str | None = None
    show_time: bool = False
    # External-logger capture: the stashed wishlist (set in the main callback,
    # before the QueueHandler exists) and the prefixes actually attached (so
    # reset() can detach exactly what it added).
    capture_prefixes: list[str] = field(default_factory=list)
    captured_prefixes: list[str] = field(default_factory=list)


_state = _LogConfig()


def verbose_floor(log_level: str) -> int:
    """Return the 'otto' logger / verbose.log floor: DEBUG when debugging, else INFO."""
    return logging.DEBUG if log_level == "DEBUG" else logging.INFO


def set_capture_prefixes(prefixes: Iterable[str]) -> None:
    """Stash the external-logger prefixes to attach once the QueueHandler exists.

    Called from the CLI's main callback (before any subcommand wires the
    listener via ``create_output_dir``). The actual attach happens later in
    ``_add_log_handlers`` -> ``capture_external_loggers``.
    """
    _state.capture_prefixes = sorted(set(prefixes))


def capture_external_loggers(prefixes: Iterable[str]) -> None:
    """Route the named top-level loggers into otto's sinks (CLI/app only).

    Finds the shared ``QueueHandler`` on the ``'otto'`` logger and attaches it to
    each prefix's logger at the verbose floor, so product code using a plain
    ``logging.getLogger(__name__)`` is captured without third-party noise. A
    no-op when the QueueHandler does not exist yet (no output dir wired).
    """
    otto = getLogger("otto")
    queue_handler = next((h for h in otto.handlers if isinstance(h, QueueHandler)), None)
    if queue_handler is None:
        return
    floor = verbose_floor(_state.log_level or "INFO")
    for prefix in prefixes:
        lg = getLogger(prefix)
        lg.setLevel(floor)
        if queue_handler not in lg.handlers:
            lg.addHandler(queue_handler)
        _state.captured_prefixes.append(prefix)


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
    """Reset module state, detach otto's handlers, and restore library-citizen state — test helper.

    Unregisters atexit callbacks before stopping the listener so that real-exit
    teardown never double-stops a listener or prints a ``None`` output dir.
    """
    atexit.unregister(_stop_listener)
    atexit.unregister(_print_output_dir)
    otto = getLogger("otto")
    for h in list(otto.handlers):
        otto.removeHandler(h)
    # Detach the shared QueueHandler from any external loggers we captured, and
    # restore their level to NOTSET so they behave as untouched library loggers.
    for prefix in _state.captured_prefixes:
        lg = getLogger(prefix)
        for h in list(lg.handlers):
            if isinstance(h, QueueHandler):
                lg.removeHandler(h)
        lg.setLevel(logging.NOTSET)
    if _state.listener is not None:
        # The listener may already be stopped (e.g. a test flushed the queue via
        # ``listener.stop()``). QueueListener.stop() sets ``_thread = None`` and
        # crashes on a second call, so only stop a still-running listener.
        if getattr(_state.listener, "_thread", None) is not None:
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
    _state.console_log_handler = None
    _state.verbose_handler = None
    _state.log_level = None
    _state.show_time = False
    _state.capture_prefixes = []
    _state.captured_prefixes = []
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
    show_time: bool = False,
) -> None:
    """Configure the ``'otto'`` logger for a CLI invocation (was init_otto_logger)."""
    logger = getLogger("otto")
    # Set the logger to the verbose floor so INFO records still reach the queue
    # even at ``--log-level WARNING``; each handler then filters by its own level.
    logger.setLevel(verbose_floor(log_level))
    # CLI has its own handlers; don't double-log to the root logger.
    logger.propagate = False
    is_debug = log_level == "DEBUG"

    stdout_handler = RichHandler(
        level=log_level,
        console=CONSOLE,
        show_time=show_time,
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
    _state.log_level = log_level
    _state.show_time = show_time


def _command_to_dir_name(command: str) -> str:
    return command.replace("-", "_")


def create_output_dir(command: str, subcommand: str | None = None) -> Path:
    """Create this invocation's output dir, wire the file handler, prune old logs, and return it.

    The caller records it on ``OttoContext.output_dir``.
    """
    if _state.xdir is None:
        raise RuntimeError("init_cli_logging() must run before create_output_dir() (xdir unset)")

    # Name the dir down to the millisecond (%f is microseconds; drop last 3).
    timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S_%f")[:-3]
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


def _make_file_handler(path: Path, level: int, rich: bool) -> FileHandler:
    """Build a ``FileHandler`` at *level* with a (optionally rich) ``RichFormatter``."""
    fh = FileHandler(path, mode="x")
    fh.setLevel(level)
    fmt = RichFormatter()
    fmt.rich = rich
    fh.setFormatter(fmt)
    return fh


def _add_log_handlers(output_dir: Path) -> None:
    """Wrap the console + two file handlers in a QueueListener for non-blocking I/O.

    Three sinks fan through the listener: the console handler registered by
    ``init_cli_logging``, ``console.log`` (a faithful console transcript at
    ``--log-level``), and ``verbose.log`` (NEW, at the verbose floor — INFO, or
    DEBUG at ``--log-level DEBUG``). Any other handlers already present on the
    logger (e.g. pytest's log-capture handler) are left in place so that
    test-infrastructure capture keeps working with ``propagate=False``.
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

    # Build the new async fan-out: console (non-blocking) + two files.
    console_handlers = [_state.console_handler] if _state.console_handler is not None else []
    log_level = _state.log_level or "INFO"
    level = logging.getLevelName(log_level)
    console_log = _make_file_handler(output_dir / "console.log", level, _state.rich_log_file)
    verbose_log = _make_file_handler(
        output_dir / "verbose.log", verbose_floor(log_level), _state.rich_log_file
    )
    # console.log is a faithful console transcript, so it inherits the console
    # handler's suppress filters (e.g. HostFilter). attach_console_suppress_filter
    # may run before this dir exists (the CLI attaches in its callback, builds the
    # dir later), so copy them here; verbose.log deliberately keeps QUIET records.
    if _state.console_handler is not None:
        for filt in _state.console_handler.filters:
            console_log.addFilter(filt)
    _state.console_log_handler = console_log
    _state.verbose_handler = verbose_log

    log_queue: Queue[LogRecord] = Queue(-1)
    _state.listener = QueueListener(
        log_queue, *console_handlers, console_log, verbose_log, respect_handler_level=True
    )
    logger.addHandler(QueueHandler(log_queue))
    _state.listener.start()
    atexit.register(_stop_listener)

    # Now that the QueueHandler exists, attach it to any stashed product /
    # external logger prefixes so their plain getLogger(__name__) records fan
    # into the same sinks.
    capture_external_loggers(_state.capture_prefixes)


def attach_console_suppress_filter(filt: Filter) -> None:
    """Apply *filt* to the console + console.log handlers only (NOT verbose.log)."""
    for h in (_state.console_handler, _state.console_log_handler):
        if h is not None:
            h.addFilter(filt)


def remove_old_logs(
    seconds: float,
    *,
    time_budget: float = LOG_ROTATE_BUDGET_SECONDS,
) -> None:
    """Remove log dirs older than ``seconds``, time-boxed to ``time_budget``.

    When the budget is exceeded the scan stops early and resumes on the next
    call, bounding the per-run cost on large/slow (e.g. NFS) trees.
    """
    # Deliberately the literal 'otto' logger, not getLogger(__name__): this
    # module's whole job is configuring *that* logger's handlers (see the
    # module docstring), and its own emitted records (below) are otto-CLI
    # user-facing output that belongs on the same handlers/sinks as every
    # other otto.* log call, not a distinctly-named child logger.
    logger = getLogger("otto")
    xdir = _state.xdir
    if xdir is None or not xdir.is_dir():
        return

    oldest = (datetime.now(tz=timezone.utc) - timedelta(seconds=seconds)).timestamp()
    logged_deletion = False
    start = time.monotonic()
    budget_hit = False

    for cmd_dir in xdir.iterdir():
        if budget_hit:
            break
        if not cmd_dir.is_dir():
            continue
        for output_dir in cmd_dir.iterdir():
            if time.monotonic() - start > time_budget:
                budget_hit = True
                break
            if not _LOG_DIR_NAME_RE.match(output_dir.name):
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
