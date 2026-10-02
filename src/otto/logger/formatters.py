"""Log formatters: multiline splitter, Rich-markup renderer, and the file traceback."""

import builtins
import contextlib
import functools
import io
import itertools
import re
from datetime import datetime
from logging import (
    Formatter,
    LogRecord,
)
from pathlib import Path
from types import TracebackType
from typing import (
    Any,
    Literal,
)

from rich.console import (
    Console,
)
from rich.text import Text
from typing_extensions import override

from ..console import CONSOLE

ExcInfo = tuple[type[BaseException], BaseException, TracebackType | None] | tuple[None, None, None]
"""A record's ``exc_info``: what ``sys.exc_info()`` returns."""

TRACEBACK_FILE_WIDTH = 160
"""Column width of a traceback in a log file.

Fixed rather than the terminal's: a file is read later, often somewhere wider,
and a frame's location line folded at 80 columns is no longer one line a
reader can grep or copy."""

TRACEBACK_MAX_FRAMES = 100
"""Frames a log file's traceback shows before it elides the middle."""

CONSOLE_TRACEBACK_MAX_FRAMES = 20
"""Frames the console's traceback shows before it elides the middle."""

LOCALS_MAX_DEPTH = 3
"""How deep a local's value is expanded in a log file before Rich elides it.

The bound that matters: Rich expands containers, dataclasses and pydantic
models field by field, and a frame with an ``OttoContext`` or a host in scope
would otherwise write out the whole object graph behind it."""

LOCALS_MAX_LENGTH = 10
"""Items of a container shown in a local's value before Rich elides the rest."""

LOCALS_MAX_STRING = 120
"""Characters of a string shown in a local's value before Rich truncates it."""

TRACE_ATTR = "otto_trace"
"""The record attribute holding the exception as Rich ``Trace`` data, captured where it was logged.

Set by :func:`capture_exception` in place of the record's live ``exc_info``."""

EXC_ID_ATTR = "otto_exc_id"
"""The record and exception attribute holding an int that names one exception object.

Stamped on the exception the first time it is captured and copied onto every
record that carries it, so a file can tell "this exception again" from "a
different one" without the record keeping the exception alive."""

NO_LOCALS_ATTR = "otto_no_locals"
"""Record attribute a logger filter sets to keep the exception's locals out of every sink.

For a logger whose tracebacks are known to pass through a frame holding a
secret: ``otto.monitor.server`` sets it on ``uvicorn.error``, whose route
failures run through the frame that holds the dashboard's access key. A logger
filter runs on the logging thread before any handler, so it is in place before
:func:`capture_exception` looks at the frames."""

_exc_ids = itertools.count(1)

_PLAIN = Formatter()
"""The stdlib's own exception formatting, for the fallback text."""

_console = Console(highlight=False, width=CONSOLE.width)
"""Dedicated capture-only console for rendering Rich markup to a string.

Must remain a separate object from the display console (otto.console.CONSOLE),
but takes its width so log lines wrap in the file exactly as on screen.
Without a fixed width Rich re-probes fds 0/1/2 on every render, and otto's
in-process pytest run leaves those non-tty — collapsing the width to 80 and
wrapping the log file narrower than the terminal. CONSOLE is already pinned
to the launch-time terminal width in otto.console."""

_ANSI = re.compile(
    r"\x1b"  # ESC
    r"(?:"
    r"\[[0-9;]*[a-zA-Z]"  # CSI sequences (covers SGR and cursor/erase codes)
    r"|\][^\x07\x1b]*"  # OSC sequences
    r"(?:\x07|\x1b\\)"  # OSC terminator (BEL or ST)
    r"|[@-_][^@-_]*"  # other two-character escape sequences
    r")"
)


def format_log_time(dt: datetime) -> Text:
    """Format a datetime as a bracketed ``[ YYYY-MM-DD HH:MM:SS.mmm ]`` Rich Text."""
    return Text(f"[ {dt.strftime('%Y-%m-%d %H:%M:%S')}.{dt.microsecond // 1000:03d} ]")


def capture_exception(record: LogRecord) -> None:
    """Replace *record*'s live ``exc_info`` with data, on the thread that logged it.

    A record bound for the listener thread must not carry a live traceback.
    The frame that caught the exception is still running when the listener
    renders, so walking its locals there races the code that owns them (a dict
    changing size mid-walk raises, and an exception escaping a handler kills
    the listener and every record after it); and a queued traceback keeps every
    frame and local alive until the listener lets go of the record, so their
    finalizers run late and on the wrong thread.

    So the exception is captured here instead, as three pieces of plain data:
    a Rich ``Trace`` (with locals within the ``LOCALS_MAX_*`` bounds, or none
    when a filter set :data:`NO_LOCALS_ATTR`) under :data:`TRACE_ATTR`; the
    stdlib's plain traceback in ``exc_text``, the fallback when the trace cannot
    be taken or rendered; and the exception's id under
    :data:`EXC_ID_ATTR`. ``exc_info`` is cleared. A record with no exception
    is left alone.
    """
    exc_info = record.exc_info
    if not exc_info or exc_info[0] is None or exc_info[1] is None:
        return
    exc_type, exc_value, tb = exc_info
    record.exc_info = None
    exc_id = getattr(exc_value, EXC_ID_ATTR, None)
    if not isinstance(exc_id, int):
        exc_id = next(_exc_ids)
        # An exception class that refuses attributes is captured all the
        # same; it simply cannot be recognized the next time it is logged.
        with contextlib.suppress(Exception):
            setattr(exc_value, EXC_ID_ATTR, exc_id)
    setattr(record, EXC_ID_ATTR, exc_id)
    try:
        record.exc_text = _PLAIN.formatException((exc_type, exc_value, tb))
    except Exception:  # noqa: BLE001 — the fallback text must never cost the record
        record.exc_text = f"{exc_type.__name__}: (traceback could not be formatted)"
    trace = None
    try:
        trace = _extract_trace(
            exc_value, tb, show_locals=not getattr(record, NO_LOCALS_ATTR, False)
        )
    except Exception:  # noqa: BLE001 — exc_text above stands in for a trace Rich could not take
        trace = None
    setattr(record, TRACE_ATTR, trace)


_STOP_CHAIN = BaseException("otto: stop Rich following the exception chain")
"""Seeds the visited set ``_extract_stack`` hands Rich, so the set is never empty.

A non-empty set stops Rich following ``__cause__``/``__context__``, which
otto does itself. A sentinel rather than the exception being extracted: an
exception may be unhashable (a ``@dataclass`` exception, or one defining
``__eq__`` without ``__hash__``), and Rich itself only ever hashes group
members."""


def _is_exception_group(exc: BaseException) -> bool:
    # Read from builtins: BaseExceptionGroup does not exist before 3.11.
    group_type = vars(builtins).get("BaseExceptionGroup")
    return group_type is not None and isinstance(exc, group_type)


def _extract_trace(exc_value: BaseException, tb: TracebackType | None, *, show_locals: bool) -> Any:
    """Return a Rich ``Trace`` of *exc_value*, with the locals bounds on every stack.

    The same trace ``Traceback.extract`` builds, assembled one stack at a time.
    Rich extracts an exception group's members itself and passes them only
    some of the bounds: no ``locals_max_depth`` and its default string limit,
    so a member's locals were walked and stored in full. Here each stack is
    extracted on its own, with the members of a group held back (Rich skips
    an exception already in ``_visited_exceptions``, and a non-empty set also
    stops it following the chain: see ``_STOP_CHAIN``), and the members are
    then extracted the same way and attached. The structure follows Rich's: the chain is
    followed through ``__cause__`` and ``__context__`` until the first group,
    a member is extracted without its own chain, and an exception met twice
    among the members is shown once.
    """
    visited: set[BaseException] = set()
    stacks: list[Any] = []
    seen: set[int] = set()
    current: BaseException | None = exc_value
    current_tb = tb
    is_cause = False
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        stacks.append(
            _extract_stack(
                current, current_tb, is_cause=is_cause, show_locals=show_locals, visited=visited
            )
        )
        if visited:
            # Rich stops following the chain once a group has been met.
            break
        if current.__cause__ is not None and current.__cause__ is not current:
            current, is_cause = current.__cause__, True
        elif current.__context__ is not None and not current.__suppress_context__:
            current, is_cause = current.__context__, False
        else:
            current = None
        current_tb = current.__traceback__ if current is not None else None
    from rich.traceback import Trace

    return Trace(stacks=stacks)


def _extract_stack(
    exc: BaseException,
    tb: TracebackType | None,
    *,
    is_cause: bool,
    show_locals: bool,
    visited: set[BaseException],
) -> Any:
    """Extract one exception's stack with the bounds, then its group members, recursively."""
    from rich.traceback import Trace, Traceback

    members: list[BaseException] = (
        list(getattr(exc, "exceptions", ())) if _is_exception_group(exc) else []
    )
    stack = Traceback.extract(
        type(exc),
        exc,
        tb,
        show_locals=show_locals,
        locals_max_length=LOCALS_MAX_LENGTH,
        locals_max_string=LOCALS_MAX_STRING,
        locals_max_depth=LOCALS_MAX_DEPTH,
        _visited_exceptions={_STOP_CHAIN, *members},
    ).stacks[0]
    stack.is_cause = is_cause
    for member in members:
        if member in visited:
            continue
        visited.add(member)
        stack.exceptions.append(
            Trace(
                stacks=[
                    _extract_stack(
                        member,
                        member.__traceback__,
                        is_cause=False,
                        show_locals=show_locals,
                        visited=visited,
                    )
                ]
            )
        )
    return stack


@functools.cache
def _suppressed_packages() -> tuple[str, ...]:
    """Directories of Typer and click, whose frames are shown without code or locals.

    The same suppression Typer's own excepthook applies (Typer vendors its
    click under its own directory; a standalone click is covered if one is
    installed). Located with ``find_spec``, which finds a top-level package
    without importing it, so rendering a traceback never loads either; and
    asked once per process, on the first traceback.
    """
    from importlib.util import find_spec

    dirs: list[str] = []
    for name in ("typer", "click"):
        spec = find_spec(name)
        if spec is not None and spec.origin is not None:
            dirs.append(str(Path(spec.origin).parent))
    return tuple(dirs)


def render_traceback(record: LogRecord, *, color: bool) -> str:
    """Render the exception :func:`capture_exception` took off *record*, for a log file.

    The one renderer for an exception in a file sink. It prints the Rich
    traceback as a renderable, never as markup, so a ``[bold]`` in a source
    line or a local's value comes out as written. The width is
    :data:`TRACEBACK_FILE_WIDTH` whatever the terminal is; *color* keeps
    Rich's ANSI styling, otherwise the text is plain. Frames inside Typer and
    click are shown by location only.

    Returns the record's plain ``exc_text`` when there is no trace to render,
    and ``""`` when the record carries no exception. Raises if Rich fails, so
    the caller can fall back to that same plain text.

    The console is built per call rather than shared with the message
    renderer above, which is sized to the terminal and whose colour follows
    whether otto started in one.
    """
    trace = getattr(record, TRACE_ATTR, None)
    if trace is None:
        return record.exc_text or ""
    from rich.traceback import Traceback

    console = Console(
        file=io.StringIO(),
        width=TRACEBACK_FILE_WIDTH,
        color_system="truecolor" if color else None,
        force_terminal=color,
        force_jupyter=False,
        force_interactive=False,
        highlight=False,
    )
    console.print(
        Traceback(
            trace,
            width=TRACEBACK_FILE_WIDTH,
            code_width=None,
            show_locals=True,
            locals_max_length=LOCALS_MAX_LENGTH,
            locals_max_string=LOCALS_MAX_STRING,
            locals_max_depth=LOCALS_MAX_DEPTH,
            suppress=_suppressed_packages(),
            max_frames=TRACEBACK_MAX_FRAMES,
        )
    )
    file = console.file
    return file.getvalue() if isinstance(file, io.StringIO) else ""


class MultilineFormatter(Formatter):
    """``logging.Formatter`` that formats each line of a multiline message separately.

    Prevents leading continuation lines from being emitted without the log
    prefix (timestamp/level), keeping log files and console output parseable.

    A record's exception and stack go BELOW the message, rendered once and
    prefixed line by line like the message. The stdlib ``format`` appends
    them to whatever it formats, which here is every line of the message.

    Each formatter writes one exception's traceback once: a record carrying an
    exception it has already written (the same object logged twice, or logged
    and then reported again as the failure that ended the command) gets
    ``(traceback written above)`` instead. Exceptions are known by the id
    :func:`capture_exception` stamps on them, and a formatter belongs to one
    file, so this is a per-file memory.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._written: set[int] = set()
        self._just_written: int | None = None

    @override
    def format(self, record: LogRecord) -> str:

        # Store the original full message to restore later
        original_msg = record.msg
        original_args = record.args
        exc_info = record.exc_info
        exc_text = record.exc_text
        stack_info = record.stack_info
        formatted_lines: list[str] = []
        self._just_written = None

        # Kept off the record while its lines are formatted: the stdlib would
        # append them to each line.
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        try:
            # Create smaller log records, each with just a single line
            # Format each one on its own so that they have the proper formatting,
            # then join them together with newlines. Splitting on the newline
            # character is needed instead of calling splitlines() because
            # splitlines() ignores the first trailing newline character.
            for line in original_msg.splitlines():
                record.msg = line
                formatted_line = super().format(record)

                formatted_lines.append(formatted_line)

            # The tail is text already: no %-args to apply to it.
            record.args = None
            for block in self._tail(record, exc_info, exc_text, stack_info):
                for line in block.rstrip("\n").splitlines():
                    record.msg = line
                    formatted_lines.append(super().format(record))
        finally:
            # Restore the original message, including all newlines
            record.msg = original_msg
            record.args = original_args
            record.exc_info = exc_info
            record.exc_text = exc_text
            record.stack_info = stack_info

        return "\n".join(formatted_lines)

    def forget_last_write(self) -> None:
        """Unmark the exception the last :meth:`format` call wrote: the write failed.

        The handler calls this from ``handleError``, so ``(traceback written
        above)`` is only ever true: an exception whose traceback never reached
        the file is written in full next time.
        """
        if self._just_written is not None:
            self._written.discard(self._just_written)
            self._just_written = None

    def render_exception(self, record: LogRecord) -> str:
        """Render the exception captured on *record*; may raise, the caller falls back."""
        return render_traceback(record, color=False)

    def _tail(
        self,
        record: LogRecord,
        exc_info: "ExcInfo | None",
        exc_text: str | None,
        stack_info: str | None,
    ) -> list[str]:
        """Return the exception and stack text that follow a record's message."""
        blocks: list[str] = []
        exc_id = getattr(record, EXC_ID_ATTR, None)
        if exc_id is not None and exc_id in self._written:
            blocks.append("(traceback written above)")
        else:
            text = self._exception_text(record, exc_info, exc_text)
            if text:
                blocks.append(text)
                if exc_id is not None:
                    self._written.add(exc_id)
                    self._just_written = exc_id
        if stack_info:
            blocks.append(self.formatStack(stack_info))
        return blocks

    def _exception_text(
        self, record: LogRecord, exc_info: "ExcInfo | None", exc_text: str | None
    ) -> str:
        if getattr(record, TRACE_ATTR, None) is not None:
            try:
                return self.render_exception(record)
            except Exception:  # noqa: BLE001 — the plain text captured with the trace stands in
                return exc_text or ""
        if exc_info is not None and exc_info[1] is not None:
            # A record that never passed through capture_exception (a caller
            # using this formatter on a handler of its own).
            return self.formatException(exc_info)
        return exc_text or ""


_default_log_format = "{asctime} [{levelname:<5}] {message}"
_default_log_style = "{"

FormatType = Literal["%", "{"]


class RichFormatter(MultilineFormatter):
    """``MultilineFormatter`` for the log file handler that controls Rich markup.

    The console handler is a :class:`rich.logging.RichHandler` of its own; this
    formatter is attached to the file handler. When ``rich`` is ``True``, markup
    is rendered to ANSI escape sequences via an internal capture console; when
    ``rich`` is ``False`` (the default), ANSI is stripped so log files stay plain.

    An exception is rendered by :func:`render_traceback`, after the markup
    pass and outside it, so the traceback is never read as markup. With
    ``rich`` set it keeps its colours whether or not otto started in a
    terminal.
    """

    def __init__(
        self,
        fmt: str = _default_log_format,
        style: FormatType = _default_log_style,
        **kwargs: Any,
    ) -> None:
        super().__init__(fmt=fmt, style=style, **kwargs)
        self._rich = False

    @override
    def formatException(self, ei: ExcInfo) -> str:
        """Return a Rich traceback for a live *ei*, or the stdlib's plain one if Rich fails.

        Only reached by a record that never passed through
        :func:`capture_exception`; otto's own records arrive captured.
        """
        record = LogRecord("", 0, "", 0, "", None, ei)
        capture_exception(record)
        try:
            return render_traceback(record, color=self.rich)
        except Exception:  # noqa: BLE001 — any failure to render must still leave a traceback in the file
            return super().formatException(ei)

    @override
    def render_exception(self, record: LogRecord) -> str:
        return render_traceback(record, color=self.rich)

    @override
    def format(self, record: LogRecord) -> str:
        """Render Rich markup in the record, then delegate to ``MultilineFormatter``.

        THE RECORD IS RESTORED BEFORE RETURNING, and that is not tidiness.
        ``_stylize`` replaces ``record.msg`` with its RENDERED form, and a
        ``logging`` record is one object handed to every handler in turn --
        ``otto.logger.management.setup_output_dir`` puts the console handler,
        ``console.log`` and ``verbose.log`` behind a single ``QueueListener``,
        all three formatting the same instance. Without the restore, the second
        file handler renders text the first one already rendered: markup that
        was correctly ESCAPED at the source comes back as a bare ``[bench]``
        the first pass produced, and the second pass eats it as a style tag. The
        symptom is one log file carrying a fact and the other silently missing
        it. ``MultilineFormatter`` above saves and restores for the same reason;
        this method simply did not.
        """
        original_msg = record.msg
        try:
            msg = super().format(record=self._stylize(record))
        finally:
            record.msg = original_msg

        # Remove all ANSI characters if rich logging is disabled
        if not self.rich:
            msg = _ANSI.sub("", msg)

        return msg

    def _stylize(
        self,
        record: LogRecord,
    ) -> LogRecord:

        with _console.capture() as capture:
            _console.print(
                record.msg,
                markup=True,
            )

        record.msg = capture.get()
        return record

    @property
    def rich(self) -> bool:
        """``True`` when Rich ANSI output is enabled; ``False`` strips ANSI sequences."""
        return self._rich

    @rich.setter
    def rich(self, flag: bool) -> None:
        self._rich = flag
