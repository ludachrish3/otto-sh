"""tests/cut_sweep: the every-offset probe-line cut sweep (#853), a tree of its own (#1399).

The sweep enumerates every tail of every probe line on every shell: about
88 thousand cases, run only by its own lane (``make stability-cut-sweep``,
nightly's ``probe-cut-sweep``). While it lived in ``tests/unit/host``, every
``tests/unit`` run built and held all of those cases before its ``-m``
filter deselected them. The run's memory roughly doubled, and every full
garbage collection walked those cases too. That includes the one pytest
runs at the end of each in-process test session, and the unit tests start
many such sessions. Measured on one in-process session test: 1.79 s of
garbage collection with the sweep's cases held, 0.88 s without. Nightly's
``unit-repeat`` ran past its 120-minute limit on that cost (#1399).

The tree is in ``testpaths`` (pyproject.toml), as ``tests/busybox`` and
``tests/conformance`` are, so path-less lanes (``make collect-check``, the
lane census) still see it. Every lane that names ``tests/unit`` no longer
collects it; the path-less catch-all coverage lanes still build it and
deselect it (#1400).

The sweep's cases spawn a fresh shell on a fresh pty, as ``tests/unit/host``'s
do, so this tree takes that directory's descriptor bracket, at the spawning
lanes' measured settings (``tests/_fixtures/fd_watermark.py``). It needs
nothing else from the ``tests/unit`` conftests: no multiprocessing, web
assets, dials or bed locks, and nothing on its path reads
``OTTO_INIT_TIMEOUT``.

Leaving ``tests/unit`` also takes the sweep out of ``make repeat``, which
selects ``tests/unit`` without excluding ``stability``. At its default
``COUNT=10`` that soak co-selected about 880 thousand shell cases of a
deterministic sweep; its own lane runs it.
"""

from collections.abc import Iterator

import pytest

from tests._fixtures.fd_watermark import (
    SPAWN_LANE_GC_POLICY,
    SPAWN_LANE_TOLERANCE,
    fd_watermark_bracket,
)


@pytest.fixture(autouse=True)
def _fd_watermark() -> Iterator[None]:
    """The authority's bracket, at the spawning lanes' measured settings."""
    yield from fd_watermark_bracket(tolerance=SPAWN_LANE_TOLERANCE, gc_policy=SPAWN_LANE_GC_POLICY)
