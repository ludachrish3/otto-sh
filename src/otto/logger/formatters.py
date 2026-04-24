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

_console = Console(highlight=False)
"""Dedicated capture-only console for rendering Rich markup to a string.
Must remain separate from the display console (otto.console.CONSOLE)."""

_ANSI = re.compile(
    r'\x1b'                 # ESC
    r'(?:'
    r'\[[0-9;]*[a-zA-Z]'    # CSI sequences (covers SGR and cursor/erase codes)
    r'|\][^\x07\x1b]*'      # OSC sequences
    r'(?:\x07|\x1b\\)'      # OSC terminator (BEL or ST)
    r'|[@-_][^@-_]*'        # other two-character escape sequences
    r')'
)

def format_log_time(dt: datetime) -> Text:
    return Text( f'[ {dt.strftime("%Y-%m-%d %H:%M:%S")}.{dt.microsecond // 1000:03d} ]')

class MultilineFormatter(Formatter):

    def format(self, record: LogRecord) -> str:

        # Store the original full message to restore later
        originalMsg = record.msg
        formattedLines: list[str] = []

        # Create smaller log records, each with just a single line
        # Format each one on its own so that they have the proper formatting,
        # then join them together with newlines. Splitting on the newline
        # character is needed instead of calling splitlines() because
        # splitlines() ignores the first trailing newline character.
        for line in originalMsg.splitlines():

            record.msg = line
            formattedLine = super().format(record)

            formattedLines.append(formattedLine)

        output = '\n'.join(formattedLines)

        # Restore the original message, including all newlines
        record.msg = originalMsg

        return output

_default_log_format = '{asctime} [{levelname:^7}] {message}'
_default_log_style  = '{'

FormatType = Literal['%'] | Literal['{']
class RichFormatter(MultilineFormatter):

    def __init__(self,
        fmt: str = _default_log_format,
        style: FormatType = _default_log_style,
        **kwargs: Any,
    ):
        super().__init__(fmt=fmt, style=style, **kwargs)

    def format(self, record: LogRecord) -> str:
        msg = super().format(record=self._stylize(record))

        # Remove all ANSI characters if rich logging is disabled
        if not self.rich:
            msg =_ANSI.sub('', msg)

        return msg

    def _stylize(self,
        record: LogRecord,
    ) -> LogRecord:

        with _console.capture() as capture:

            _console.print(record.msg,
                markup=True,
            )

        record.msg = capture.get()
        return record

    @property
    def rich(self):
        return self._rich

    @rich.setter
    def rich(self, flag: bool):
        self._rich = flag
