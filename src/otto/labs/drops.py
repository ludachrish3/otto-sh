"""What a best-effort host enumeration left out, and why — completion's outlet.

Every enumeration that feeds shell completion is best-effort by contract: a
malformed lab file, an entry whose inventory reference cannot be resolved, a
source that fails — none of it may crash or warn into the user's TAB. Until
2026-09-03 that silence had no outlet. An id that did not complete had been
dropped somewhere along the chain with at most a DEBUG log nobody was
reading, and "no host comes up" was unanswerable without reading source
(four distinct causes were reproduced that day, every one of them silent).

This module is the outlet's recording half. The skip sites call
:func:`record_drop` naming WHERE the entry was and WHY it was left out; the
completion cache's writer collects the records per repo into the ``names``
payload (``host_drops``), and ``otto cache info`` prints them beside the
hosts the cache offers. Records are taken only while a sink is open
(:func:`collecting_drops`), so the loud ``load_lab`` path — which raises
instead of skipping — and any enumeration nobody asked about cost nothing.

The sink is a contextvar, opened and read on the SAME thread: the completion
cache enumerates on a worker thread (its deadline wrapper) and opens the sink
there rather than relying on the worker seeing its caller's context — on the
default CPython build a new thread does not inherit it, and a build that does
hands the worker a copy, so a sink opened by the caller is the wrong list
either way.
"""

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass


@dataclass(frozen=True)
class HostDrop:
    """One entry, file, lab or source an enumeration left out."""

    where: str
    """What was skipped: a file, ``<file>: element 'x' hosts[i]``, ``lab 'y'``,
    ``source <label>`` — whatever the skip site could name."""

    reason: str
    """The error text the skip swallowed."""


_SINK: contextvars.ContextVar["list[HostDrop] | None"] = contextvars.ContextVar(
    "otto_host_drops", default=None
)


def record_drop(where: str, reason: str) -> None:
    """Record that *where* was left out because of *reason* — if anyone is collecting."""
    sink = _SINK.get()
    if sink is not None:
        sink.append(HostDrop(where=where, reason=reason))


@contextmanager
def collecting_drops() -> Iterator[list[HostDrop]]:
    """Collect every :func:`record_drop` made on this thread while the block runs."""
    drops: list[HostDrop] = []
    token = _SINK.set(drops)
    try:
        yield drops
    finally:
        _SINK.reset(token)
