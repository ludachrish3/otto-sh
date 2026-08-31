"""CLI-side logging management for otto.

otto-the-library only *emits* log records (via ``logging.getLogger('otto'…)``)
and never configures handlers — see ``otto.logger``. This module is the
application/CLI side: it configures the ROOT logger's handlers + formatters,
creates each invocation's output directory, and prunes old log directories.

Configuring ROOT (not the ``'otto'`` logger) is what makes capture
zero-registration: a record from ANY logger — otto's own, a repo's product
code, a suite module, a third-party library — reaches otto's three sinks by
plain propagation, with no allowlist to keep in sync.

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
from collections.abc import Mapping
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
    # The directory the CURRENT fan-out writes into, RESOLVED, so a repeated
    # install can tell "the sinks are already here" from "wire a new pair of
    # files" without mistaking a second spelling of one directory for a second
    # directory. Set by install_sinks; distinct from last_output_dir, which is
    # what the CLI prints at exit (in the caller's spelling) and which no
    # embedder path sets.
    sinks_dir: Path | None = None
    # The log level the CURRENT fan-out's two file handlers were built with.
    # Their levels are fixed at construction, so a repeated install that keeps
    # the fan-out has to re-level them itself (see _set_sink_levels).
    sinks_log_level: str | None = None
    listener: QueueListener | None = None
    atexit_registered: bool = False
    console_handler: RichHandler | None = None
    console_log_handler: FileHandler | None = None
    verbose_handler: FileHandler | None = None
    log_level: str | None = None
    show_time: bool = False
    # The root logger's level as found by the first install_console call, so
    # reset() can put it back exactly (None = otto never installed).
    saved_root_level: int | None = None
    # Every logger name apply_library_levels has forced a level on, so reset()
    # can hand them back to NOTSET (inherit root) instead of leaving otto's
    # noise floor behind in a process otto no longer configures.
    floored_loggers: list[str] = field(default_factory=list[str])
    # The [logging.levels] overrides applied so far, so a re-install re-applies
    # them instead of reverting to the bare defaults. See apply_library_levels.
    level_overrides: dict[str, str] = field(default_factory=dict[str, str])


_state = _LogConfig()


def verbose_floor(log_level: str) -> int:
    """Return the root-logger / verbose.log floor: DEBUG when debugging, else INFO."""
    return logging.DEBUG if log_level == "DEBUG" else logging.INFO


OTTO_HANDLER_ATTR = "_otto_installed"
"""Marker attribute on every handler otto installs on the root logger.

Otto detaches ONLY marked handlers (install, re-install, reset) — foreign
root handlers (pytest's caplog/log-cli capture, an embedder's own) keep
receiving records via propagation and are never touched. See spec §3.2.
"""


def _mark(handler: logging.Handler) -> logging.Handler:
    setattr(handler, OTTO_HANDLER_ATTR, True)
    return handler


def _otto_handlers(logger: logging.Logger) -> list[logging.Handler]:
    return [h for h in logger.handlers if getattr(h, OTTO_HANDLER_ATTR, False)]


def _sinks_are_live() -> bool:
    """Whether the CURRENT fan-out can still deliver a record to its files.

    The sink-identity fields (``sinks_dir``, the two file handlers) outlive the
    listener: :func:`shutdown_listener` claims and stops it for an exit path
    and leaves the rest standing. A process that survives that — the force path
    it was written for does not, but an embedder calling it does — is left with
    a stopped fan-out whose bookkeeping still says "the sinks are here", so
    "which directory" is not on its own an answer to "are the sinks working".
    A listener that a caller stopped directly (tests flushing a queue) reads as
    dead here for the same reason: ``QueueListener.stop`` clears its thread.
    """
    return _state.listener is not None and getattr(_state.listener, "_thread", None) is not None


DEFAULT_LIBRARY_LEVELS: dict[str, str] = {
    # Per-operation chatter: "executing <function connect.<locals>.connector…>"
    # / "operation … completed" for every statement the coverage and monitor
    # databases run.
    "aiosqlite": "WARNING",
    # "Using selector: EpollSelector" on every event loop otto opens — four of
    # the eight lines a scaffolded `otto test --log-level DEBUG` writes to
    # verbose.log before this entry existed. What this does NOT cost: every
    # asyncio diagnostic otto's own leak hunts run on — "Task exception was
    # never retrieved", "Unclosed transport", "Unclosed event loop", the
    # slow-callback "Executing <Task…> took N seconds" — is emitted at WARNING
    # or ERROR and is unaffected. Everything below WARNING except the selector
    # line is already gated behind asyncio's own `loop.get_debug()`, which otto
    # never turns on.
    "asyncio": "WARNING",
    # The spec's named offender (§4.1): a per-packet DEBUG trace of every SSH
    # channel, which is why it is here despite emitting nothing in the hostless
    # seeding run — see the docstring below.
    "asyncssh": "WARNING",
}
"""Per-library noise floor applied at install time (spec §4.1).

Overridable per logger in ``settings.toml`` ``[logging.levels]``. The table
decides what ENTERS the funnel; the sinks decide what shows. Unlisted loggers
inherit root, so capture stays zero-registration: the table only quiets or
un-quiets.

How the entries were chosen: ``aiosqlite`` and ``asyncio`` were SEEDED
EMPIRICALLY — each was measured emitting through otto's own dependency tree,
and a library never observed logging gets no entry. ``asyncssh`` is the
exception the spec itself names as the known offender: its traffic only
appears under live SSH load, so a hostless seeding run cannot exercise it, and
its absence from such a run is not evidence against the entry. Re-seed by
measuring; do not delete ``asyncssh`` on a hostless count of zero.

``uvicorn.error`` / ``uvicorn.access`` are deliberately ABSENT though the
seeding run saw both emit at INFO: otto integrates with those two on purpose
(``otto.monitor.server`` attaches filters that suppress one shutdown warning
and redact the access log's query string), so their INFO records are wanted
output, not noise. Quieting them here would delete the access log and make
that redaction filter dead code.
"""


def apply_library_levels(overrides: Mapping[str, str] | None = None) -> None:
    """Set per-library logger levels: the defaults, then *overrides* on top.

    *overrides* are REMEMBERED on the state, not just applied: ``install_console``
    re-applies the table on every install, and without the memory a second
    install would quietly put a repo's ``[logging.levels]`` back to otto's
    defaults — the one knob the operator set, silently undone. ``reset()``
    forgets them again.

    Records every name it touches so ``reset()`` can hand those loggers back.
    Level names are validated upstream for the CLI path (``LoggingConfigSpec``
    rejects an unknown level name, and the names ``otto`` / ``otto.*``), but the
    embedder entry point of spec §3.4 does not go through that model — so the
    name string is passed to ``setLevel`` as-is, whose own error names the value
    the caller actually wrote.
    """
    if overrides:
        _state.level_overrides.update(overrides)
    merged = {**DEFAULT_LIBRARY_LEVELS, **_state.level_overrides}
    for name, level in merged.items():
        getLogger(name).setLevel(level)
        if name not in _state.floored_loggers:
            _state.floored_loggers.append(name)


def install_console(log_level: str, *, show_time: bool = False) -> None:
    """Attach otto's console handler to the ROOT logger and set the entry floor.

    Runs early (CLI main callback), before any gate that might want to warn —
    from here on a plain ``logger.warning`` anywhere in the process is visible.
    Idempotent: a marked console handler already present is replaced, not
    duplicated.

    This is also where the per-library noise floor is applied
    (:func:`apply_library_levels`, spec §4): root sits at the verbose floor, so
    without it ``--log-level DEBUG`` would admit every dependency's DEBUG
    records into the sinks. The CLI re-applies the same function once repo
    settings are parsed, adding each repo's ``[logging.levels]`` overrides —
    and because those overrides are remembered on the state, re-installing the
    console afterwards re-applies them rather than reverting to the defaults.
    """
    root = getLogger()
    if _state.saved_root_level is None:
        _state.saved_root_level = root.level
    # Replace a marked console handler, but never a marked QueueHandler:
    # ``install_sinks`` may already have wired the three sinks behind it, and
    # detaching it here would silently stop console.log/verbose.log while the
    # listener kept running.
    for h in _otto_handlers(root):
        if not isinstance(h, QueueHandler):
            root.removeHandler(h)
    previous = _state.console_handler
    stdout_handler = RichHandler(
        level=log_level,
        console=CONSOLE,
        show_time=show_time,
        tracebacks_max_frames=20,
        tracebacks_show_locals=True,
        markup=True,
        highlighter=NullHighlighter(),
        show_path=log_level == "DEBUG",
        enable_link_path=False,
        log_time_format=format_log_time,
        omit_repeated_times=False,
    )
    _mark(stdout_handler)
    if previous is not None:
        # Carry the suppress filters over. ``attach_console_suppress_filter``
        # hangs HostFilter/LogMode on the console handler, and a freshly built
        # replacement carries none of them — so without this, "attach a
        # suppress filter, then re-install to raise the level" silently
        # UN-suppresses. Same principle as install_sinks copying the console's
        # filters onto console.log.
        for filt in previous.filters:
            stdout_handler.addFilter(filt)
    if _state.listener is None:
        root.addHandler(stdout_handler)
    else:
        # The sinks are already up, so the console handler hangs INSIDE the
        # listener's fan-out and not on root: adding the new one to root as
        # well would print every record twice (once direct, once through the
        # queue), and leaving the old one in the fan-out would print the OLD
        # one's rendering forever. Swap it where it actually lives. The
        # replacement goes first, matching install_sinks' construction order;
        # a fan-out with no console handler yet (install_sinks ran without an
        # install_console) gains one rather than silently dropping this call.
        # The displaced handler is left to be collected — nothing references
        # it once it is out of both root and the fan-out, and it owns no
        # stream of its own to close (it renders through the shared CONSOLE).
        _state.listener.handlers = (
            stdout_handler,
            *(h for h in _state.listener.handlers if h is not previous),
        )
    root.setLevel(verbose_floor(log_level))
    apply_library_levels()
    _state.console_handler = stdout_handler


def install(
    log_level: str = "INFO",
    *,
    output_dir: "Path | None" = None,
    show_time: bool = False,
    overrides: "Mapping[str, str] | None" = None,
) -> None:
    """Route the process's logging through otto's sinks — the embedder entry.

    ``install_console`` + the noise floor (defaults merged with *overrides*),
    and — when *output_dir* is given — the ``console.log``/``verbose.log``
    fan-out in that directory (created if needed; no rotation, no per-run
    subdirectory: the embedder owns its directory layout). The CLI's own path
    is this same machinery plus per-run directories. Undo with :func:`reset`.

    Safe to call again (spec §3.4 leaves the ordering contract to this entry
    point). A second call SWAPS: the console handler is replaced wherever it
    currently hangs — on root, or inside the listener's fan-out once the sinks
    are up, carrying the suppress filters with it — so a re-install never
    prints a record twice, never strands the file sinks and never silently
    un-suppresses. Naming the directory the sinks already write to keeps them,
    rather than reopening (and, at ``mode="x"``, failing on) files that are
    still open; a *different* directory retires the old fan-out for a new one.
    Both directories are compared RESOLVED, so two spellings of one directory
    are one directory. A changed *log_level* reaches the kept files too: they
    are re-levelled in place, which continues the transcript rather than
    starting a second one. It does so whether or not the call repeats
    *output_dir* — the level knob means the same thing either way; omitting
    the directory leaves WHERE the sinks write alone, not what they admit.

    *overrides* ACCUMULATE across calls: each call merges its table into the
    ones already applied, and only :func:`reset` forgets them. So a second
    install with no *overrides* keeps the first call's, and one that omits a
    logger the first call named does NOT retract that entry — to retract it,
    name the logger with the level you want, or ``reset()`` and install afresh.
    """
    install_console(log_level, show_time=show_time)
    _state.log_level = log_level
    apply_library_levels(overrides)
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        # RESOLVED on both sides of the comparison below (``install_sinks``
        # stores the resolved form). A caller's spelling is not an identity:
        # ``Path("out")`` and an absolute path to the same directory compared
        # unequal, re-entering install_sinks — which stops the listener and
        # closes the files BEFORE reopening console.log at ``mode="x"``, so
        # the second install raised FileExistsError and left the process with
        # no handlers and no listener at all.
        output_dir = output_dir.resolve()
        if _state.sinks_dir != output_dir or not _sinks_are_live():
            # Liveness, not just identity: after ``shutdown_listener()`` the
            # bookkeeping still names this directory while nothing delivers to
            # it any more, so trusting the directory alone left the console
            # working and both files silently dead (and a dead QueueHandler on
            # root growing a queue nobody drains). ``install_sinks`` REVIVES
            # rather than reopens when the directory matches — see its
            # docstring — so the earlier transcript survives the rebuild.
            install_sinks(output_dir)
        elif _state.sinks_log_level != log_level:
            _set_sink_levels(log_level)
    elif _state.listener is not None and _state.sinks_log_level != log_level:
        # Same re-level, reached without naming the directory again. Omitting
        # *output_dir* says "leave where the sinks write alone", not "leave
        # what they admit alone" — without this the level knob would mean one
        # thing when the call repeats the directory and another when it does
        # not, and the DEBUG the caller just asked for would print on the
        # console while both files silently dropped it.
        _set_sink_levels(log_level)


def _set_sink_levels(log_level: str) -> None:
    """Re-level the CURRENT fan-out's two files for a changed ``log_level``.

    Both re-install paths that KEEP the fan-out — an unchanged directory, and
    a call that names no directory at all — where rebuilding is not an option
    and would be wrong anyway: the two ``FileHandler``s take their level at
    construction, so keeping the fan-out froze ``console.log`` and
    ``verbose.log`` at the first call's level — a record admitted by the new
    root level printed on screen and was dropped by both files under
    ``respect_handler_level=True``. Rebuilding cannot fix that here (the files
    are open, and ``mode="x"`` refuses to reopen them), and truncating them
    would delete the transcript the earlier level already wrote. Setting the
    levels in place continues that one transcript at the new level, which is
    what an embedder raising the level mid-process is asking for.
    """
    if _state.console_log_handler is not None:
        _state.console_log_handler.setLevel(logging.getLevelName(log_level))
    if _state.verbose_handler is not None:
        _state.verbose_handler.setLevel(verbose_floor(log_level))
    _state.sinks_log_level = log_level


def _stop_listener() -> None:
    """atexit-safe: stop the QueueListener once; a no-op if already stopped."""
    shutdown_listener()


def shutdown_listener(*, join_timeout: float | None = None) -> None:
    """Public flush-and-stop for exit paths that bypass atexit.

    ``lifecycle``'s forced sync-phase exit (``os._exit``) skips interpreter
    teardown, so buffered log lines would be lost without an explicit stop.
    The listener is claimed (swapped out of module state) before it is
    stopped, so a force-path watchdog racing atexit reduces to one stop and
    one no-op — a plain read-swap under the GIL, adequate for exit paths,
    not a general any-thread guarantee.

    With *join_timeout* the flush is BOUNDED, for force paths that must
    never hang: the sentinel is enqueued only if the queue's mutex is free
    (a wedged producer must never wedge the exit), and the listener thread
    is joined with the timeout instead of forever (a listener stuck on a
    slow sink — e.g. NFS — forfeits its tail of buffered lines).
    """
    listener = _state.listener
    _state.listener = None
    if listener is None:
        return
    if join_timeout is None:
        listener.stop()
        return
    queue_mutex = getattr(listener.queue, "mutex", None)
    if queue_mutex is not None:
        # Probe, don't block: enqueue_sentinel acquires this mutex. Its
        # critical sections are tiny stdlib bookkeeping that cannot wedge,
        # but the force path's contract is "never hang", so trade the flush
        # away rather than trust that. (A live producer re-taking the mutex
        # between release and enqueue only delays us microseconds.)
        if not queue_mutex.acquire(blocking=False):
            return
        queue_mutex.release()
    listener.enqueue_sentinel()
    thread = listener._thread  # noqa: SLF001 — QueueListener has no bounded stop; join its (stable-since-3.2) thread directly
    if thread is not None:
        thread.join(timeout=join_timeout)


def _print_output_dir() -> None:
    """atexit-safe: print the final output dir, unless reset() cleared it."""
    if _state.last_output_dir is not None:
        CONSOLE.print(f"\nOutput directory: {_state.last_output_dir}", highlight=False)


def reset() -> None:
    """Undo install_console/install_sinks: the public counterpart of install() (spec §3.4/§6).

    Restores the root logger exactly as otto found it: only MARKED handlers are
    detached (a foreign root handler — pytest's capture, an embedder's own —
    is never touched), and root's level goes back to the value the first
    ``install_console`` recorded. Every logger the noise floor (spec §4) forced
    a level on goes back to ``NOTSET`` — inheriting root, which is what an
    otto-free process expects.

    Unregisters atexit callbacks before stopping the listener so that real-exit
    teardown never double-stops a listener or prints a ``None`` output dir.
    """
    atexit.unregister(_stop_listener)
    atexit.unregister(_print_output_dir)
    root = getLogger()
    for h in _otto_handlers(root):
        root.removeHandler(h)
    if _state.saved_root_level is not None:
        root.setLevel(_state.saved_root_level)
    for name in _state.floored_loggers:
        getLogger(name).setLevel(logging.NOTSET)
    _state.floored_loggers.clear()
    _state.level_overrides.clear()
    closed: list[logging.Handler] = []
    if _state.listener is not None:
        # The listener may already be stopped (e.g. a test flushed the queue via
        # ``listener.stop()``). QueueListener.stop() sets ``_thread = None`` and
        # crashes on a second call, so only stop a still-running listener.
        if getattr(_state.listener, "_thread", None) is not None:
            _state.listener.stop()
        for h in _state.listener.handlers:
            h.close()
            closed.append(h)
    # The two log files are closed whether or not a listener still holds them.
    # ``shutdown_listener`` claims the listener and returns, leaving these open;
    # gating the close on the listener therefore dropped two open file handles
    # on the floor here, and the files stayed open until the garbage collector
    # happened to reach them. Exactly once: the fan-out above holds these same
    # objects on the ordinary path.
    for fh in (_state.console_log_handler, _state.verbose_handler):
        if fh is not None and not any(fh is done for done in closed):
            fh.close()
    _state.xdir = None
    _state.keep_seconds = None
    _state.rich_log_file = False
    _state.last_output_dir = None
    _state.sinks_dir = None
    _state.sinks_log_level = None
    _state.listener = None
    _state.atexit_registered = False
    _state.console_handler = None
    _state.console_log_handler = None
    _state.verbose_handler = None
    _state.log_level = None
    _state.show_time = False
    _state.saved_root_level = None
    # Belt-and-braces library-citizen state on the 'otto' logger. Nothing sets
    # propagate=False any more (the CLI configures ROOT), so this is a safety
    # line for a caller that bent it, not part of otto's own teardown.
    otto = getLogger("otto")
    otto.propagate = True
    # Restore the library-citizen NullHandler (added by otto.logger.__init__ at
    # import time — idempotent).
    if not any(isinstance(h, NullHandler) for h in otto.handlers):
        otto.addHandler(NullHandler())


def init_cli_logging(
    xdir: Path,
    log_level: str,
    keep_days: float,
    rich_log_file: bool = False,
    show_time: bool = False,
) -> None:
    """Record the CLI invocation's logging config and install the console on ROOT.

    The CLI-facing wrapper around :func:`install_console`: it stores the
    per-invocation state ``create_output_dir``/``install_sinks`` read later
    (xdir, retention, rich-file flag, level, show-time) and installs the
    console handler. It does not touch the ``'otto'`` logger at all — under
    root capture that logger is an ordinary library logger.
    """
    _state.xdir = Path(xdir)
    _state.rich_log_file = rich_log_file
    _state.keep_seconds = keep_days * 24 * 60 * 60
    _state.log_level = log_level
    _state.show_time = show_time
    install_console(log_level, show_time=show_time)


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

    install_sinks(output_dir)
    return output_dir


def _make_file_handler(path: Path, level: int, rich: bool) -> FileHandler:
    """Build a ``FileHandler`` at *level* with a (optionally rich) ``RichFormatter``."""
    fh = FileHandler(path, mode="x")
    fh.setLevel(level)
    fmt = RichFormatter()
    fmt.rich = rich
    fh.setFormatter(fmt)
    return fh


def install_sinks(output_dir: Path) -> None:
    """Wrap the console + two file handlers in a QueueListener for non-blocking I/O.

    Three sinks fan through the listener: the console handler installed by
    ``install_console``, ``console.log`` (a faithful console transcript at
    ``--log-level``), and ``verbose.log`` (at the verbose floor — INFO, or
    DEBUG at ``--log-level DEBUG``). Everything hangs on the ROOT logger, so
    any logger in the process feeds these sinks by propagation.

    Idempotent: a second call retires the previous fan-out first — the old
    listener is stopped (flushing what it still holds into the OLD run's
    files) and those files are closed — so two output dirs never leave two
    listeners and two QueueHandlers alive.

    A call naming the directory the current sinks ALREADY write to REVIVES
    instead: the listener is rebuilt around the two file handlers that are
    already open, and they are neither closed nor reopened. That is the only
    thing it can honestly mean. The files exist, and the handlers use
    ``mode="x"`` — a guard against otto ever appending to another run's
    transcript — so reopening is refused by construction; recreating them
    would delete the records the earlier fan-out wrote. The venue is a
    fan-out whose listener was stopped (``shutdown_listener`` on a force-exit
    path the process then survived) and an :func:`install` that revives it.
    Handlers whose streams are already CLOSED are never adopted — a closed
    ``FileHandler`` re-opens on its next emit, ``mode="x"`` refuses the
    existing file, and the raise inside ``QueueListener._monitor`` would kill
    the whole fan-out thread. Such a call falls through to the rebuild and
    fails loudly HERE, at the caller, instead.

    The retirement happens BEFORE the new files are opened, so a NEW directory
    whose files cannot be opened (no permission, ENOSPC, a ``console.log``
    already there) leaves the process with no otto handlers and no listener.
    ``create_output_dir`` cannot reach that: it always names a fresh
    timestamped directory it has just created.
    """
    root = getLogger()
    # The current run's own files, when this call names the directory they
    # already write to: reused below rather than closed and rebuilt.
    reusable = (
        (_state.console_log_handler, _state.verbose_handler)
        if _state.sinks_dir == output_dir.resolve()
        and _state.console_log_handler is not None
        and _state.verbose_handler is not None
        and _state.console_log_handler.stream is not None
        and _state.verbose_handler.stream is not None
        else None
    )
    if _state.listener is not None:
        # Guarded exactly as reset() is: QueueListener.stop() sets
        # ``_thread = None`` and crashes on a second call, and a test may have
        # flushed the queue with its own stop() already.
        if getattr(_state.listener, "_thread", None) is not None:
            _state.listener.stop()
        if reusable is None:
            # Close the PREVIOUS run's files only. The console handler is
            # deliberately left open — it is reused in the new fan-out below.
            for stale in (_state.console_log_handler, _state.verbose_handler):
                if stale is not None:
                    stale.close()
        _state.listener = None
    # Remove only the handlers we own (marked): the console handler — which is
    # fanned into the listener below, so leaving it on root too would
    # double-emit every record — and any QueueHandler from a previous
    # create_output_dir call. Foreign handlers (e.g. pytest caplog) are NOT
    # removed so they keep receiving records synchronously.
    for h in _otto_handlers(root):
        root.removeHandler(h)

    # Build the new async fan-out: console (non-blocking) + two files.
    console_handlers = [_state.console_handler] if _state.console_handler is not None else []
    log_level = _state.log_level or "INFO"
    level = logging.getLevelName(log_level)
    if reusable is not None:
        # Already open, already carrying this directory's transcript and (for
        # console.log) the console's suppress filters. Only the levels can have
        # moved since, and _set_sink_levels is the one place that decides them.
        console_log, verbose_log = reusable
        _set_sink_levels(log_level)
    else:
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
    # Stored RESOLVED: this is an identity for "the sinks are already here",
    # and a caller's spelling is not one. See install().
    _state.sinks_dir = output_dir.resolve()
    _state.sinks_log_level = log_level

    log_queue: Queue[LogRecord] = Queue(-1)
    _state.listener = QueueListener(
        log_queue, *console_handlers, console_log, verbose_log, respect_handler_level=True
    )
    root.addHandler(_mark(QueueHandler(log_queue)))
    _state.listener.start()
    atexit.register(_stop_listener)


def attach_console_suppress_filter(filt: Filter) -> None:
    """Apply *filt* to the console + console.log handlers only (NOT verbose.log).

    ONE filter per class per handler. Each ``ensure_cli_session`` builds a fresh
    ``HostFilter`` and calls this, and ``install_console`` copies every filter it
    finds onto the replacement handler — so N in-process invocations (a
    CliRunner test file; never production, where one process is one invocation)
    left N interchangeable HostFilters, each re-deciding the same verdict for
    every record. Today's filters are pure functions of the record, so the cost
    was only wasted comparisons; the dedupe is here so a future suppress filter
    that carries state cannot inherit the pattern silently.

    Deliberately by exact class, not by identity or equality: the duplicates are
    distinct objects with no ``__eq__``, and a SUBCLASS of an attached filter is
    a different suppression rule that must still attach.
    """
    for h in (_state.console_handler, _state.console_log_handler):
        if h is None:
            continue
        if any(type(existing) is type(filt) for existing in h.filters):
            continue
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
