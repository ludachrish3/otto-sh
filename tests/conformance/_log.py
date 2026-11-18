"""What a host's output log received, read off the records the host emits.

Every route relays each output line it reads through
:meth:`~otto.host.host.BaseHost._log_output`: one record per line, on a
logger under ``otto``, whose ``host`` extra is the host that ran it and whose
message is :func:`~otto.host.host.output_log_prefix` followed by the line.
:func:`output_record` is the one test of "this record is this host's output";
:func:`logged_lines` and :func:`logged_output` collect those lines (and their
modes) for a block, ``tests.conformance._started`` watches them for a start
token, and :func:`records_containing` finds a token in any record of otto's.
"""

import contextlib
import dataclasses
import logging
from collections.abc import Callable, Iterator

from otto.host.host import BaseHost, output_log_prefix
from otto.logger.mode import LogMode

_OUTPUT_LOGGER = "otto"
"""The logger every host's output record propagates to."""


@dataclasses.dataclass(frozen=True)
class OutputRecord:
    """One output line a host logged, and the mode it was stamped with."""

    text: str
    """The line as the program wrote it: the record's ``relayed`` text."""

    log_mode: LogMode
    """The record's ``log_mode``, the stamp the console filter reads."""


def output_record(record: logging.LogRecord, host: BaseHost) -> "OutputRecord | None":
    """The output line *record* carries for *host*, or ``None`` when it is not *host*'s output.

    The one test of "this record is this host's output". The line as the
    program wrote it, exactly: the record's ``relayed`` text
    (:func:`~otto.logger.relay.log_relayed` keeps it beside the escaped
    message), no split, no strip. A record of another host, or one of
    *host*'s command lines, is ``None``.
    """
    if getattr(record, "host", None) is not host:
        return None
    if not record.getMessage().startswith(output_log_prefix(host.name)):
        return None
    text = getattr(record, "relayed", None)
    if text is None:
        return None
    return OutputRecord(text=text, log_mode=getattr(record, "log_mode", LogMode.NORMAL))


class _RecordHandler(logging.Handler):
    """Hands every record it receives to *seen*."""

    def __init__(self, seen: "Callable[[logging.LogRecord], None]") -> None:
        super().__init__(level=logging.DEBUG)
        self._seen = seen

    def emit(self, record: logging.LogRecord) -> None:
        self._seen(record)


@contextlib.contextmanager
def _watching_records(seen: "Callable[[logging.LogRecord], None]", *, level: int) -> Iterator[None]:
    """Call *seen* with every record otto's loggers emit at *level* or above while the block runs.

    The handler is attached for the block's life only, and the logger's
    level is lowered to *level* while it is, then put back.
    """
    handler = _RecordHandler(seen)
    target = logging.getLogger(_OUTPUT_LOGGER)
    saved = target.level
    if target.getEffectiveLevel() > level:
        target.setLevel(level)
    target.addHandler(handler)
    try:
        yield
    finally:
        target.removeHandler(handler)
        target.setLevel(saved)


@contextlib.contextmanager
def watching_output(host: BaseHost, seen: "Callable[[OutputRecord], None]") -> Iterator[None]:
    """Call *seen* with each output record *host* logs while the block runs."""

    def output(record: logging.LogRecord) -> None:
        found = output_record(record, host)
        if found is not None:
            seen(found)

    with _watching_records(output, level=logging.INFO):
        yield


@contextlib.contextmanager
def logged_output(host: BaseHost) -> "Iterator[list[OutputRecord]]":
    """The output records *host* logs inside the block, one element per record, in order."""
    records: "list[OutputRecord]" = []
    with watching_output(host, records.append):
        yield records


@contextlib.contextmanager
def logged_lines(host: BaseHost) -> "Iterator[list[str]]":
    """The output lines *host* logs inside the block, one element per record, in order."""
    lines: "list[str]" = []
    with watching_output(host, lambda found: lines.append(found.text)):
        yield lines


@contextlib.contextmanager
def records_containing(token: str) -> "Iterator[list[str]]":
    """Every record of otto's loggers, at any level, whose text holds *token*, inside the block.

    Each element names the record's logger and level and gives its text:
    the message, and the ``relayed`` text where the record has one.
    """
    found: "list[str]" = []

    def check(record: logging.LogRecord) -> None:
        text = f"{record.getMessage()} {getattr(record, 'relayed', '')}"
        if token in text:
            found.append(f"{record.name} {record.levelname}: {record.getMessage()!r}")

    with _watching_records(check, level=logging.DEBUG):
        yield found
