"""The text an otto log sink shows for a record, read off the record itself."""

import logging


def shown(record: logging.LogRecord) -> str:
    """Return what the console and both log files show for *record*, styles aside.

    A record is rendered as Rich markup only when it opted in with
    ``extra={"markup": True}``; any other record is shown as written.
    """
    message = record.getMessage()
    if not getattr(record, "markup", False):
        return message
    from rich.text import Text

    return Text.from_markup(message).plain
