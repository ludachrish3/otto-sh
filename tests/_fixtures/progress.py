"""One definition of a correct transfer-progress stream, for every venue.

The conformance surface ``transfer-progress`` (bed and hermetic), the unit
tests that capture events from fakes, and the Rich render test all call
:func:`assert_progress_invariants`; a backend that satisfies it here
satisfies it everywhere. The clauses are the spec's §2, numbered in the
messages so a red names its clause. One of them, 8c, is implied by its
siblings and can never fire alone -- it says so at its own assert, and it is
not coverage.
"""

import math
from collections.abc import Callable
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

ProgressHandler = Callable[[str, str, int, int], None]
"""otto's ``TransferProgressHandler`` shape, spelled without importing otto."""

ProgressFactory = Callable[[], ProgressHandler]
"""otto's ``TransferProgressFactory`` shape: a fresh handler per file."""

ProgressFactoryMaker = Callable[[Any, str], ProgressFactory]
"""The shape of ``make_rich_progress_factory(progress, host_name)`` itself."""


@dataclass(frozen=True)
class ProgressEvent:
    """One ``handler(src, dst, bytes_done, bytes_total)`` call, recorded."""

    src: str
    dst: str
    done: int
    total: int


def capture_progress() -> "tuple[list[ProgressEvent], ProgressFactoryMaker]":
    """A spy in the shape of ``make_rich_progress_factory`` and the list it fills.

    ``spy(progress, host_name)`` returns a factory; every ``factory()`` call
    returns a fresh handler appending into the ONE shared list -- the product's
    own per-file shape, so a two-file transfer is two streams in one list,
    split by ``src``. Performing that split is the CALLER's job:
    :func:`assert_progress_invariants` takes ONE file's stream and its clause 2
    REFUSES a foreign ``src``, so hand it a per-``src`` slice of this list and
    never the list whole.
    """
    events: list[ProgressEvent] = []

    # ``progress`` and ``host_name`` go unread on purpose: to be a drop-in the
    # spy must ACCEPT what make_rich_progress_factory accepts, and a list needs
    # neither a Rich Progress nor a host name.
    def spy(progress: Any, host_name: str) -> ProgressFactory:
        def factory() -> ProgressHandler:
            # A NEW handler per call, never one hoisted out here: the real
            # make_rich_progress_handler keeps per-file closure state
            # (current_src / task_id), so sharing one handler across files
            # would be a different object graph from the product's.
            def handler(src: str, dst: str, done: int, total: int) -> None:
                events.append(ProgressEvent(src=src, dst=dst, done=done, total=total))

            return handler

        return factory

    return events, spy


def assert_progress_invariants(
    events: list[ProgressEvent], *, src: str, total: int, granularity: "int | None"
) -> None:
    """Refuse any stream that is not correct progress for *src*, clause by clause.

    *granularity* is ONE arm of a backend's declared
    :class:`~otto.host.transfer.base.ProgressGranularity` -- the stride for the
    direction under test, or ``None`` for a whole-file backend.
    """
    assert total > 0, (
        f"total must be positive; an empty payload is outside this contract (got {total})"
    )
    assert events, f"no progress events for {src}"  # 1
    strangers = [e.src for e in events if e.src != src]
    assert not strangers, (  # 2
        f"an event names a different source: {strangers[0]!r} != {src!r}"
    )
    totals = {e.total for e in events}
    assert totals == {total}, (  # 3
        f"total changed between events or is not {total}: {sorted(totals)}"
    )
    for e in events:
        assert 0 <= e.done <= total, f"bytes_done {e.done} is outside [0, {total}]"  # 4
    dones = [e.done for e in events]
    assert all(b > a for a, b in pairwise(dones)), (  # 5
        f"bytes_done is not strictly increasing: {dones}"
    )
    assert dones[-1] == total, (  # 6
        f"final event is {dones[-1]} of {total}: the bar never finishes"
    )
    if granularity is None:
        assert len(events) == 1, f"promised ONE event; saw {len(events)}: {dones}"  # 7
        return
    assert dones[0] <= granularity, (  # 8a
        f"first event advanced {dones[0]} bytes; the stride is {granularity} -- "
        "the bar cannot begin past one stride"
    )
    for a, b in pairwise(dones):
        assert b - a <= granularity, (  # 8b
            f"advanced {b - a} bytes between events; the stride is {granularity}"
        )
    # 8c is IMPLIED by 6 + 8a + 8b: the first event advances at most G, every
    # step advances at most G, and the last equals total, so n*G >= total
    # already and n >= ceil(total / G). Nothing can violate 8c alone, so no
    # test covers it and deleting it reds nothing -- do NOT read it as
    # coverage. It is kept because the spec's §2 numbering is the contractual
    # map between clause and message, and because it is the backstop that
    # fires if 8b is ever weakened.
    expected = math.ceil(total / granularity)
    assert len(events) >= expected, (  # 8c
        f"{len(events)} events for {total} bytes at stride {granularity}; "
        f"at least {expected} expected"
    )


def events_for(
    *, src: str, total: int, granularity: "int | None", dst: str = ""
) -> list[ProgressEvent]:
    """A compliant synthetic stream at the declared stride.

    A *total* at or below the stride yields exactly ONE event, so a caller that
    wants to see SEVERAL must size the payload above the stride -- the spec's
    ``3*G + 17``, which is four events with a partial last one.
    """
    assert total > 0, f"total must be positive; there is no stream for {total} bytes"
    assert granularity is None or granularity > 0, (
        f"a stride must be positive or None (got {granularity})"
    )
    if granularity is None:
        return [ProgressEvent(src=src, dst=dst, done=total, total=total)]
    dones = [*range(granularity, total, granularity), total]
    return [ProgressEvent(src=src, dst=dst, done=d, total=total) for d in dones]
