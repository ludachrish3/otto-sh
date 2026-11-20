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
completion cache's writer collects the records per project into the ``names``
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
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass


@dataclass(frozen=True, kw_only=True)
class Diagnostic:
    """Where something was found wanting, and why: the shape every report here shares.

    A host drop (``otto.labs.drops.HostDrop``) is one; so is the reason a lab
    is unavailable (:attr:`otto.labs.LabStatus.unavailable`).
    """

    where: str
    """What it concerns, as precisely as the reporting site could name it.

    A file, ``<file>: element 'x' hosts[i]``, ``lab 'y'`` or ``source <label>``."""

    reason: str
    """The error text, or the stated reason."""


@dataclass(frozen=True, kw_only=True)
class HostDrop(Diagnostic):
    """One entry, file, lab or source an enumeration left out.

    :attr:`~Diagnostic.where` names what was skipped and
    :attr:`~Diagnostic.reason` is the error text the skip swallowed.
    """

    timed_out: bool = False
    """Whether this is the enumeration's own deadline, not a skip.

    A skip or a failed source is an answer: what the enumeration left out, and
    why. A deadline is not: the source may well be fine and merely slow, so the
    completion cache's writer stores a ``names`` section built from one on the
    short TTL, and the first TAB after it expires asks again."""


_SINK: contextvars.ContextVar["list[HostDrop] | None"] = contextvars.ContextVar(
    "otto_host_drops", default=None
)


def record_drop(where: str, reason: str) -> None:
    """Record that *where* was left out because of *reason* — if anyone is collecting."""
    sink = _SINK.get()
    if sink is not None:
        sink.append(HostDrop(where=where, reason=reason))


def is_refusal(error: BaseException) -> bool:
    """Return whether *error* is a bare-reference refusal, or was raised from or during one.

    The one decision every skip site makes before recording a drop: a
    refusal recorded its own (:meth:`~otto.inventory.RefusedDefault.refuse`),
    so a site that catches it, however a backend wrapped it, records none.
    Both chains are followed: ``__cause__`` (``raise ... from``) and
    ``__context__`` (raised while handling it).
    """
    from ..inventory.errors import BareReferenceRefused  # deferred: completion path

    pending: list[BaseException] = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, BareReferenceRefused):
            return True
        pending.extend(e for e in (current.__cause__, current.__context__) if e is not None)
    return False


@contextmanager
def collecting_drops() -> Generator[list[HostDrop], None, None]:
    """Collect every :func:`record_drop` made on this thread while the block runs."""
    drops: list[HostDrop] = []
    token = _SINK.set(drops)
    try:
        yield drops
    finally:
        _SINK.reset(token)
