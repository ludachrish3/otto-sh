"""Whether a contract's command has printed its start token, read off the route under test.

``exec`` hands a caller no output until it returns, so a contract that has to
act while the command runs (cancel it, say) learns that it started from what
the host LOGS as the command's output: every route relays each output line it
reads through :meth:`~otto.host.host.BaseHost._log_output`, as a record whose
``host`` extra is the host that ran it, one ``... | <line>`` per output line.
:func:`output_seen` watches those records for a line that IS one token and
sets an :class:`asyncio.Event` when it arrives. A whole line, because the
command's own log record carries the token too, inside the command line
that prints it, and that record arrives before anything has run.

The witness (``tests/conformance/_witness.py``) answers a different question,
whether the process is running, off a second observer; this one says only
that the route under test delivered the command's own output, which is what
"it started" means to a caller of that route.
"""

import asyncio
import contextlib
import logging
from collections.abc import Iterator

from otto.host.host import BaseHost

_OUTPUT_LOGGER = "otto"
"""The logger every host's output record propagates to."""


class _TokenSeen(logging.Handler):
    """Sets *seen* on the first record of *host*'s output with a line that is *token*."""

    def __init__(self, host: BaseHost, token: str, seen: asyncio.Event) -> None:
        super().__init__(level=logging.DEBUG)
        self._host = host
        self._line = f"| {token}"
        self._seen = seen

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(record, "host", None) is not self._host:
            return
        if any(line.rstrip().endswith(self._line) for line in record.getMessage().splitlines()):
            self._seen.set()


@contextlib.contextmanager
def output_seen(host: BaseHost, token: str) -> "Iterator[asyncio.Event]":
    """An event set once *host* has relayed an output line that is *token*.

    The handler is attached for the context's life only, and the logger's
    level is lowered to let INFO output records through while it is, then
    put back.
    """
    seen = asyncio.Event()
    handler = _TokenSeen(host, token, seen)
    target = logging.getLogger(_OUTPUT_LOGGER)
    level = target.level
    if target.getEffectiveLevel() > logging.INFO:
        target.setLevel(logging.INFO)
    target.addHandler(handler)
    try:
        yield seen
    finally:
        target.removeHandler(handler)
        target.setLevel(level)
