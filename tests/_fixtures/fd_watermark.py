"""The one FD-watermark bracket for the leak-sensitive lanes (Wave 14).

Three chaos/stability conftests (``tests/e2e/tunnel_stability``,
``tests/e2e/chaos``, ``tests/integration/chaos``) used to carry hand-rolled
copies of this fixture, and one drifted: the integration copy took its
baseline *without*
a ``gc.collect()``, inflating ``before`` by whatever garbage the previous
test left uncollected — a load-dependent false tolerance while its
docstring said "Same shape as…" (review §5.5). The shape that wins is the
tunnel-stability one: collect BEFORE the baseline so the count is the
test's own floor, collect again before the verdict, and give collector
timing one retry before failing.

``tests/unit/host`` adopted it too (2026-08-09), where the real subprocess
spawning lives — but on different terms, because the two knobs below were
measured there rather than inherited.

What this bracket can and cannot see
------------------------------------
It sees descriptors still open at the verdict, held by objects that SURVIVE a
``gc.collect()``. It does not see a descriptor that the collector can release,
because every path below collects before deciding — deliberately, so collector
timing cannot manufacture a red build (``test_gc_only_garbage_is_absorbed_not_blamed``).

That exclusion is load-bearing, so do not read this bracket as cover for the
unclosed-transport flake. Measured 2026-08-09 by mutating ``dab13a7b``'s fix
back out behind a probe test that asserts nothing: the bracket stayed GREEN at
``tolerance=0`` and the unraisable ResourceWarning plugin is what went red. A
leaked asyncio transport is collectable, so the pre-verdict collect closes its
pipe before the count is taken. The instruments for THAT class are the in-test
zero-tolerance measurement (``test_timed_out_exec_does_not_leak_its_pipe_fds``
counts descriptors with the loop still open and no collect in between) and the
armed transport detector; the ast-grep rule stops the API that made the leak
unfixable in the first place. This bracket is the standing net for descriptors
held by something still alive.

Tolerance
---------
The verdict is ``after <= before + tolerance``, so a lane's tolerance is the
size of the smallest RETAINED leak it can miss. ``FD_TOLERANCE = 4`` was
chosen for the bed lanes, where a live SSH session and its channels move the
count around under the test's feet; it also means those lanes cannot see a
leak of four descriptors or fewer. A lane can only tighten to zero if its own
floor is genuinely flat. Under ``tests/unit/host`` it is: across 1426 tests,
measured over five runs and both orderings, exactly two tests moved the count
at all, and both were multiprocessing's one-off arena allocation rather than a
leak (see that lane's conftest).

GC policy
---------
``"always"`` is the original shape and costs two ``gc.collect()`` calls PER
TEST whether or not the test goes near a descriptor. On ``tests/unit/host``
that is 16.1s -> 54.0s (**3.3x**), which is why the first adoption tried to
buy it back by naming the modules that spawn.

``"on-suspicion"`` skips both collects on the fast path and only collects —
then re-reads — when the raw count came back above tolerance. Same directory,
same 1426 tests: **16.1s, no measurable cost at all**, and it reports the same
two tests as the eager policy. That is the whole reason the unit lane needs no
allowlist: an instrument that is free can cover everything.

The tradeoff is real and is the same one review §5.5 was about, so state it
plainly rather than let a docstring imply otherwise: with no baseline collect,
garbage left by the PREVIOUS test inflates ``before``, and if that garbage is
freed inside this test's window it can offset a real leak of the same size.
``"always"`` is immune to that and ``"on-suspicion"`` is not. The bed lanes,
which have the noisy heaps, keep ``"always"``. On the unit lane the two
policies were compared directly over 1426 tests and disagreed on nothing.

Consumers import ``_fd_watermark`` by name into their conftest — pytest
registers imported fixtures exactly as if defined there — or define a local
fixture that ``yield from``s the generator below when they need non-default
knobs. The authoritative list is ``_CONSUMERS`` in
``tests/unit/test_fd_watermark.py``, which pins the bracket's behavior and
fails if any consumer conftest re-grows a local copy of the logic.
"""

import gc
from collections.abc import Iterator
from pathlib import Path
from typing import Literal

import pytest

FD_TOLERANCE = 4

GcPolicy = Literal["always", "on-suspicion"]


def open_fd_count() -> int:
    """Number of file descriptors currently open in this process."""
    return len(list(Path("/proc/self/fd").iterdir()))


def fd_watermark_bracket(
    tolerance: int = FD_TOLERANCE, *, gc_policy: GcPolicy = "always"
) -> Iterator[None]:
    """Generator body of the bracket, driveable directly by unit tests.

    See the module docstring for what the two knobs cost and what they buy.
    Both policies reach the same verdict expression; they differ only in
    whether the collects happen unconditionally or only once the raw count
    has already looked wrong.
    """
    if gc_policy == "always":
        gc.collect()
    before = open_fd_count()
    yield
    if gc_policy == "always":
        gc.collect()
    after = open_fd_count()
    if after > before + tolerance:
        # Either the first look at this boundary ("on-suspicion") or a second
        # chance for collector timing ("always"). Same call, same purpose:
        # never blame a test for descriptors the collector simply had not got
        # to yet.
        gc.collect()
        after = open_fd_count()
    assert after <= before + tolerance, (
        f"local fd leak across test: {before} -> {after} open fds (tolerance {tolerance})"
    )


@pytest.fixture(autouse=True)
def _fd_watermark() -> Iterator[None]:
    """Local-side leak bracket: the process's open-FD count must return to
    baseline (±tolerance) once the test's fixtures have closed every host.
    Autouse and dependency-free, so pytest instantiates it BEFORE (and
    finalizes it AFTER) the lane's host/lab fixtures — the bracket wraps
    their whole lifetime. One gc pass before the baseline keeps the
    previous test's uncollected garbage out of ``before``; one more before
    the verdict (with a single retry) absorbs collector timing."""
    yield from fd_watermark_bracket()
