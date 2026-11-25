"""Concurrent ``exec`` calls on one host each return their own command's answer.

``exec`` is the call a caller may gather. Where it has a process or channel
of its own that holds by construction; where every command shares one line
(a console, an embedded host) the calls take turns on it, one at a time
(``BaseHost._line_turn``). Either way no call's value carries another call's
output, and none is missing its own.

Each call goes through ``issue``, which asserts the route the cell's entry
must take (``check_route``), so a console cell that silently took the pool
fails here as in the other exec contracts.

The stimulus is the cell's ``distinct_answer_template``, filled with one
token per call, on every userland: a POSIX shell prints the token, and a
Zephyr shell, which has no ``echo``, quotes the unknown command back.
"""

import asyncio

import pytest

from tests.conformance._concurrency import crossed
from tests.conformance._entry import issue, opened
from tests.conformance._resolved import ResolvedCell

pytestmark = [pytest.mark.asyncio, pytest.mark.conformance]

_CALLS = 4
"""How many execs the contract gathers: enough that a line shared without turns interleaves."""


@pytest.mark.entries("exec")
@pytest.mark.observable(
    "each of 4 concurrent execs' value against the token its `distinct_answer_template` printed"
)
async def test_concurrent_execs_each_return_their_own_result(resolved_cell: ResolvedCell) -> None:
    """Gathered execs: each value carries its own token and no other call's."""
    words = resolved_cell.vocabulary
    tokens = [f"otto-cf-conc-{i}" for i in range(_CALLS)]
    async with opened(resolved_cell) as host:
        results = await asyncio.gather(
            *(
                issue(host, resolved_cell, words.distinct_answer_template.format(token=token))
                for token in tokens
            )
        )
    assert crossed(results, tokens) == []


@pytest.mark.entries("exec")
@pytest.mark.positive_control("exec-concurrent")
async def test_control_swapped_results_are_reported(resolved_cell: ResolvedCell) -> None:
    """Two answers handed to each other's callers are both reported.

    What makes this control fail is a host that answers one value for every
    command (the `every-printf-answers-one-value` twist): its first
    assertion goes red. The second checks the judge itself, that swapped
    answers are each reported.
    """
    words = resolved_cell.vocabulary
    tokens = ["otto-cf-conc-a", "otto-cf-conc-b"]
    async with opened(resolved_cell) as host:
        results = [
            await issue(host, resolved_cell, words.distinct_answer_template.format(token=token))
            for token in tokens
        ]
    assert crossed(results, tokens) == []
    assert len(crossed(list(reversed(results)), tokens)) == 2
