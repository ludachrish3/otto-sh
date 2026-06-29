"""Log formatters: multiline splitter and Rich-markup renderer."""

import re
from datetime import datetime
from logging import (
    Formatter,
    LogRecord,
)
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


class MultilineFormatter(Formatter):
    """``logging.Formatter`` that formats each line of a multiline message separately.

    Prevents leading continuation lines from being emitted without the log
    prefix (timestamp/level), keeping log files and console output parseable.
    """

    @override
    def format(self, record: LogRecord) -> str:

        # Store the original full message to restore later
        original_msg = record.msg
        formatted_lines: list[str] = []

        # Create smaller log records, each with just a single line
        # Format each one on its own so that they have the proper formatting,
        # then join them together with newlines. Splitting on the newline
        # character is needed instead of calling splitlines() because
        # splitlines() ignores the first trailing newline character.
        for line in original_msg.splitlines():
            record.msg = line
            formatted_line = super().format(record)

            formatted_lines.append(formatted_line)

        output = "\n".join(formatted_lines)

        # Restore the original message, including all newlines
        record.msg = original_msg

        return output


_default_log_format = "{asctime} [{levelname:^7}] {message}"
_default_log_style = "{"

FormatType = Literal["%", "{"]


class RichFormatter(MultilineFormatter):
    """``MultilineFormatter`` for the log file handler that controls Rich markup.

    The console handler uses :class:`rich.logging.RichHandler` directly; this
    formatter is attached to the file handler. When ``rich`` is ``True``, markup
    is rendered to ANSI escape sequences via an internal capture console; when
    ``rich`` is ``False`` (the default), ANSI is stripped so log files stay plain.
    """

    def __init__(
        self,
        fmt: str = _default_log_format,
        style: FormatType = _default_log_style,
        **kwargs: Any,
    ) -> None:
        super().__init__(fmt=fmt, style=style, **kwargs)

    @override
    def format(self, record: LogRecord) -> str:
        """Render Rich markup in the record, then delegate to ``MultilineFormatter``."""
        msg = super().format(record=self._stylize(record))

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
