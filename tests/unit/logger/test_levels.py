"""WARN/CRIT level-name aliases and the fixed-width level column they enable.

``otto.logger`` eagerly wires short aliases for the two long stdlib level
names (``WARNING`` → ``WARN``, ``CRITICAL`` → ``CRIT``) so every level name
is ≤5 characters — letting the file-sink formatter use a fixed-width column
without truncating or overflowing it.
"""

import logging

import otto.logger  # noqa: F401 — import triggers the eager `from . import levels` wiring
from otto.logger.formatters import RichFormatter


def test_warning_level_name_aliased_to_warn():
    assert logging.getLevelName(logging.WARNING) == "WARN"


def test_critical_level_name_aliased_to_crit():
    assert logging.getLevelName(logging.CRITICAL) == "CRIT"


def test_file_formatter_aligns_message_column_across_all_levels():
    """The message must start at the same column for every level name.

    All five level names (DEBUG/INFO/WARN/ERROR/CRIT) are ≤5 characters once
    the aliases are wired, so a fixed-width ``{levelname:<5}`` column keeps
    the message text aligned regardless of which level fired.
    """
    formatter = RichFormatter()
    formatter.rich = False
    levels = [
        logging.DEBUG,
        logging.INFO,
        logging.WARNING,
        logging.ERROR,
        logging.CRITICAL,
    ]

    message_columns: list[int] = []
    for level in levels:
        record = logging.LogRecord(
            name="otto",
            level=level,
            pathname=__file__,
            lineno=1,
            msg="the message",
            args=None,
            exc_info=None,
        )
        formatted = formatter.format(record)
        message_columns.append(formatted.index("the message"))

    assert len(set(message_columns)) == 1, (
        f"message column differs across levels: {message_columns}"
    )
