"""Whether a contract's command has printed its start token, read off the route under test.

``exec`` hands a caller no output until it returns, so a contract that has to
act while the command runs (cancel it, say) learns that it started from what
the host LOGS as the command's output: every route relays each output line it
reads to the host's output log (``tests.conformance._log``, which decides
which records are the host's output). :func:`output_seen` watches those
lines for one that IS one token and sets an :class:`asyncio.Event` when it
arrives. A whole line, because the command's own log record carries the
token too, inside the command line that prints it.

The witness (``tests/conformance/_witness.py``) answers a different question,
whether the process is running, off a second observer; this one says only
that the route under test delivered the command's own output, which is what
"it started" means to a caller of that route.
"""

import asyncio
import contextlib
from collections.abc import Iterator

from otto.host.host import BaseHost
from tests.conformance._log import OutputRecord, watching_output


@contextlib.contextmanager
def output_seen(host: BaseHost, token: str) -> "Iterator[asyncio.Event]":
    """An event set once *host* has relayed an output line that is *token*.

    The lines are read through :func:`~tests.conformance._log.watching_output`,
    for the context's life only.
    """
    seen = asyncio.Event()

    def check(found: OutputRecord) -> None:
        # Stripped: only the token's presence matters here, never the line's exact bytes.
        if found.text.rstrip() == token:
            seen.set()

    with watching_output(host, check):
        yield seen
