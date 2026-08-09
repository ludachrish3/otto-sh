"""tests/unit/host — every test is bracketed on its open-descriptor count.

Adopted 2026-08-09. This directory is where the host code drives real
subprocesses with real pipes (``LocalHost.exec``, the session shells, the
file-ops helpers), so it is where a descriptor is most likely to be stranded,
and until now the only lanes with a bracket were the chaos/stability ones.

Be exact about what that buys, because the first version of this file was not:
it does NOT catch ``dab13a7b``'s leak, the one that motivated bringing the
bracket here at all. Verified by mutating that fix back out behind a probe
test that asserts nothing — the bracket stayed green and the unraisable
detector is what caught it. A leaked transport is collectable, and every
bracket path collects before its verdict, so the pipe is closed before the
count is taken; tightening the tolerance does not change that. What this file
adds is a standing net for descriptors held by something still ALIVE at
teardown, which nothing else in this lane watches. The collectable class
belongs to ``test_timed_out_exec_does_not_leak_its_pipe_fds`` (which counts
in-test, loop open, no collect in between) and to the armed detector.

Two knobs, both measured here rather than inherited from the bed lanes:

``tolerance=0``. The verdict is ``after <= before + tolerance``, so the bed
lanes' default of 4 cannot see a retained leak of four descriptors or fewer.
Zero is affordable here because the floor is flat: over 1426 tests, five runs,
both orderings, exactly two tests moved the count at all — and both were
multiprocessing's one-off arena, handled below.

``gc_policy="on-suspicion"``. The eager policy's two ``gc.collect()`` calls
run per TEST, not per spawn, and cost 16.1s -> 54.0s (3.3x) across this
directory — which is what an earlier version of this file tried to buy back by
listing the five modules that construct a subprocess transport. That list was
the wrong instrument twice over: it cost a spawn-counting hook whose failure
mode corrupted pytest's teardown, and its chokepoint could not see
``multiprocessing`` at all — ``test_console_lock.py`` forks its children
through neither ``BaseSubprocessTransport`` nor ``subprocess.Popen``, and it is
the ONE module in this directory that ever moved the descriptor count.
Collecting only once the raw count already looks wrong costs nothing
measurable (16.1s, unchanged) and finds the same tests, so there is no
allowlist here and nothing for a future author to remember to update.
"""

import multiprocessing as mp
from collections.abc import Iterator

import pytest

# Deliberately NOT importing the authority's ``_fd_watermark`` fixture the way
# the chaos lanes do: pytest registers a fixture under its function's own name,
# so importing it here — even aliased — would silently bring the bed lanes'
# tolerance of 4 and eager gc policy with it. The generator body is the shared
# part, and that is what gets imported.
from tests._fixtures.fd_watermark import fd_watermark_bracket


@pytest.fixture(scope="session", autouse=True)
def _warm_the_multiprocessing_arena() -> None:
    """Pay multiprocessing's one-off descriptor cost outside any test's bracket.

    ``multiprocessing``'s shared-memory heap allocates its first arena lazily
    and keeps it for the life of the process — on Linux that is an mmap'd
    descriptor per arena, held by a module-level singleton. It is a cache, not
    a leak, but at ``tolerance=0`` it is indistinguishable from one: whichever
    test first touched ``mp.Barrier``/``mp.Value`` was billed +2 descriptors
    that never come back, and which test that was depended on ordering.
    ``test_console_lock.py`` was the module hit, and both its tests were hit in
    turn depending on the lane.

    Session-scoped and autouse, so pytest sets it up before the function-scoped
    bracket takes its first baseline: the allocation lands outside every
    watermark rather than on an arbitrary test. This is the same move
    ``test_timed_out_exec_does_not_leak_its_pipe_fds`` makes when it runs one
    throwaway ``exec`` before its baseline — take the process's one-time costs
    first, then measure.
    """
    barrier = mp.Barrier(2)
    value = mp.Value("i", 0)
    event = mp.Event()
    del barrier, value, event


@pytest.fixture(autouse=True)
def _fd_watermark() -> Iterator[None]:
    """The authority's bracket, at this lane's measured settings.

    Delegates to ``fd_watermark_bracket`` rather than re-implementing it: the
    hand-rolled copies drifted apart once already (review §5.5), and the whole
    point of the authority is that the baseline-verdict shape lives in exactly
    one place.
    """
    yield from fd_watermark_bracket(tolerance=0, gc_policy="on-suspicion")
