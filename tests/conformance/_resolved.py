"""The one shape both conformance venues resolve their cells to.

A module of its own, and the reason is an import EDGE rather than tidiness.
``tests/conformance/_cells.py`` is the HERMETIC venue: it imports the tier-2
chaos lane's loopback ``sshd`` fixtures and the BusyBox artifact matrix at
module level, because that is the machinery it stands its cells up with. While
:class:`ResolvedCell` lived there, every importer of the BED venue's resolver
got that machinery as well -- MEASURED before the split, ``import
tests.conformance._bed`` pulled in ``tests.integration.chaos._sshd`` and
``tests.integration.chaos._target``, for the sake of a dataclass with three
fields. ``tests/unit/test_conformance_bed.py`` holds that separation with a
subprocess import probe, and with a positive control that keeps the probe
honest by pointing it at a module which does still drag them in.

The edge also turned into a CYCLE the moment ``resolve_space()`` stopped
raising for the bed venue and started dispatching to ``_bed.bed_space()``:
``_cells`` then imports ``_bed``, and ``_bed`` imported ``_cells`` for the
shape. Measured, with the shape still in ``_cells``::

    ImportError: cannot import name 'ResolvedCell' from partially initialized
    module 'tests.conformance._cells' (most likely due to a circular import)

So this module must stay a leaf: the shape, and the names its annotations
need -- ``Vocabulary`` among them, which is itself a leaf holding only frozen
data (``tests/conformance/_vocabulary.py``). Anything that stands a host UP
belongs in the venue that stands it up -- put a fixture import here and both
venues inherit it again, which is the condition the split was made to end.

Not to be confused with :mod:`otto.testing.conformance`, which asserts that
pluggable BACKEND INTERFACES conform. This tree is about HOST CONTRACTS.
"""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path

from otto.host.host import BaseHost
from tests._fixtures.profiles import Cell
from tests.conformance._vocabulary import Vocabulary


@dataclass(frozen=True)
class ResolvedCell:
    """A cell a venue can actually stand up, and how.

    Frozen because of what spec s4 asks the sampler to do with these: log the
    cell it drew, then hand it to the contract assertions. A cell that could be
    rewritten in between would let the log name one cell while another was
    measured.
    """

    cell: Cell
    kind: str
    open_host: Callable[[], AbstractAsyncContextManager[BaseHost]]
    remote_scratch: "Callable[[Path], Path] | None"
    """Where a transfer to this cell's host may land, given the test's own ``tmp_path``.

    THREE STATES, and the third is the one that needed a ruling. A callable
    that ignores its argument and answers a DEVICE path is the bed venue's
    answer; a callable that answers a directory under the runner's
    ``tmp_path`` is the hermetic venue's, whose far side IS the runner's own
    filesystem; and ``None`` means this cell's host has nowhere to put a file
    at all.

    A CALLABLE rather than a plain path because the hermetic venue's answer
    cannot exist at resolve time: its remote directory is a per-test
    ``tmp_path``, and the space is resolved once at conftest import. The bed's
    answer is a fixed device path that ignores the argument, which is why the
    parameter is spelled as unused there.

    ``None`` is otto's own answer rather than this suite's judgement. Every
    cell that carries it is an
    :class:`~otto.host.embedded_host.EmbeddedHost` whose
    :class:`~otto.host.embedded_filesystem.EmbeddedFileSystem` reports
    ``supports_transfer`` False -- the same property
    ``otto.host.transfer.console.ConsoleFileTransfer`` short-circuits both
    ``_run_put`` and ``_run_get`` on. A contract that needs a remote directory
    declares that domain and parametrizes over the cells that have one; see
    ``tests/conformance/test_transfer_contract.py``. Excluding the cell from a
    CONTRACT is not excluding it from the SPACE: a Zephyr guest reports a
    single ``(telnet, console)`` pair, so dropping the cell would delete the
    guest and take its exec and timeout coverage with it.

    REQUIRED, with no default. A default of ``None`` would file every cell a
    future venue resolves as "has nowhere to put a file", and the transfer
    contract would then narrow to nothing while every other contract still
    passed -- a venue silently losing a contract, which is the failure this
    suite exists to make impossible.
    """

    vocabulary: Vocabulary
    """The spellings this cell's userland answers the contracts in.

    REQUIRED, with no default, for the same reason ``remote_scratch`` is: a
    default of :data:`~tests.conformance._vocabulary.POSIX` would hand every
    cell a future venue resolves bash's spellings, and a userland that has
    none of them would go red for a reason that reads as a product bug. A
    venue must SAY what dialect its cells answer in.

    What varies here is the STIMULUS and its expected values. The property
    each contract asserts is identical across every cell, which
    ``tests/unit/test_conformance_bed.py`` proves by running each contract
    against a scripted host under every vocabulary -- honest hosts pass under
    all of them, and each lying host fails under all of them.
    """
