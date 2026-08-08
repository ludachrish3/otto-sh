"""The one FD-watermark bracket for the leak-sensitive lanes (Wave 14).

Three conftests (``tests/e2e/tunnel_stability``, ``tests/e2e/chaos``,
``tests/integration/chaos``) used to carry hand-rolled copies of this
fixture, and one drifted: the integration copy took its baseline *without*
a ``gc.collect()``, inflating ``before`` by whatever garbage the previous
test left uncollected — a load-dependent false tolerance while its
docstring said "Same shape as…" (review §5.5). The shape that wins is the
tunnel-stability one: collect BEFORE the baseline so the count is the
test's own floor, collect again before the verdict, and give collector
timing one retry before failing.

Consumers import ``_fd_watermark`` by name into their conftest — pytest
registers imported fixtures exactly as if defined there.
``tests/unit/test_fd_watermark.py`` pins the bracket's behavior and fails
if any consumer conftest re-grows a local copy.
"""

import gc
from collections.abc import Iterator
from pathlib import Path

import pytest

FD_TOLERANCE = 4


def open_fd_count() -> int:
    """Number of file descriptors currently open in this process."""
    return len(list(Path("/proc/self/fd").iterdir()))


def fd_watermark_bracket() -> Iterator[None]:
    """Generator body of the bracket, driveable directly by unit tests."""
    gc.collect()
    before = open_fd_count()
    yield
    gc.collect()
    after = open_fd_count()
    if after > before + FD_TOLERANCE:
        gc.collect()
        after = open_fd_count()
    assert after <= before + FD_TOLERANCE, (
        f"local fd leak across test: {before} -> {after} open fds"
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
