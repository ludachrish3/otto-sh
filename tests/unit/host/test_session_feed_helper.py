"""Pins for the FeedAfterWriteMixin feeder-ordering primitive (_session_feed).

The behaviors pinned here are the ones the 65-site migration relies on; each
was chosen against its motivating defect's shape (review §3.4):
- eager baseline: a baseline read lazily (inside the coroutine) would already
  include the write it means to wait for — the wait would never fire.
- loud premise expiry: the old sleep-then-feed shape fed into the void when
  the session never wrote; the symptom was an unrelated-looking read timeout.
- drift guard: the family has FOUR doubles in four files; a fifth copy that
  reverts to sleep-ordering must not appear silently.
"""

import asyncio

import pytest

from otto.utils import WaitTimeoutError
from tests.unit.host._session_feed import FeedAfterWriteMixin


class _Double(FeedAfterWriteMixin):
    def __init__(self) -> None:
        self.written: list[str] = []
        self.fed: list[str] = []

    def feed(self, data: str) -> None:
        self.fed.append(data)


@pytest.mark.asyncio
async def test_feeds_only_after_a_new_write_past_the_eager_baseline():
    d = _Double()
    d.written.append("pre-existing command")
    # Baseline is captured HERE — the pre-existing write must not satisfy it.
    task = asyncio.create_task(d.feed_after_write("chunk-1", "chunk-2"))
    for _ in range(20):
        await asyncio.sleep(0)
    assert d.fed == [], "fed before any new write — baseline not eager or not honored"
    d.written.append("the awaited command")
    await asyncio.wait_for(task, timeout=5)
    assert d.fed == ["chunk-1", "chunk-2"]

    # Version-independent discriminator (opus W16 review, MAJOR): the phase
    # above cannot distinguish eager from lazy capture when the task body
    # runs before the awaited append (on <=3.11, wait_for task-wraps the
    # SUT so feeders start early and a lazy baseline reads the same 1).
    # Here the write lands BETWEEN the synchronous helper call and the
    # task's first step — eager baseline (1) sees 2 > 1 and feeds; a lazy
    # baseline would read 2 at task start and wait forever.
    d2 = _Double()
    d2.written.append("pre")
    coro = d2.feed_after_write("late-chunk", timeout=1.0)
    d2.written.append("command written before the feeder task ever ran")
    await asyncio.wait_for(asyncio.ensure_future(coro), timeout=5)
    assert d2.fed == ["late-chunk"]


@pytest.mark.asyncio
async def test_never_writing_session_fails_the_premise_loudly():
    d = _Double()
    with pytest.raises(WaitTimeoutError, match="never wrote a command past baseline 0"):
        await d.feed_after_write("unreachable", timeout=0.2)
    assert d.fed == []


@pytest.mark.asyncio
async def test_then_hook_runs_after_the_chunks_under_the_same_ordering():
    """The transport-death sites feed no content — ``feed_eof()`` and friends
    ARE the event — so ``then`` carries the write-ordering for them, and where
    chunks are also present it runs after them (never ahead of the output it
    follows)."""
    d = _Double()
    fed_when_hook_ran: list[list[str]] = []
    task = asyncio.create_task(
        d.feed_after_write("chunk", then=lambda: fed_when_hook_ran.append(list(d.fed)))
    )
    for _ in range(20):
        await asyncio.sleep(0)
    assert fed_when_hook_ran == [], "hook ran before any new write — ordering not honored"
    d.written.append("the awaited command")
    await asyncio.wait_for(task, timeout=5)
    assert fed_when_hook_ran == [["chunk"]], "hook ran before its chunks, not after"

    # The zero-chunk shape the EOF/connection-lost/broken-pipe sites use.
    idle = _Double()
    fired: list[str] = []
    hook_task = asyncio.create_task(idle.feed_after_write(then=lambda: fired.append("eof")))
    idle.written.append("the awaited command")
    await asyncio.wait_for(hook_task, timeout=5)
    assert fired == ["eof"]
    assert idle.fed == []


# Drift guard: every ShellSession-double in the family must inherit the mixin
# (red until each file's double is wired — the wave's structural t0). The
# list is closed-world: an AST scan at review time found no other class in
# tests/ defining the feed()/written pair, so a FIFTH double is caught by
# review convention (extend this list when adding one), not by this guard.
@pytest.mark.parametrize(
    ("modname", "clsname"),
    [
        ("tests.unit.host.test_session", "MockSession"),
        ("tests.unit.host.test_session_logging", "MockSession"),
        ("tests.unit.host.test_session_output_buffering", "FrameMockSession"),
        ("tests.unit.host.test_zephyr", "MockZephyrSession"),
    ],
)
def test_every_family_double_uses_the_shared_feed_ordering(modname, clsname):
    import importlib

    cls = getattr(importlib.import_module(modname), clsname)
    assert issubclass(cls, FeedAfterWriteMixin), (
        f"{modname}.{clsname} does not inherit FeedAfterWriteMixin — a family "
        "double has drifted back to wall-clock feeder ordering"
    )
