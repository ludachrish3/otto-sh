"""Which cells of the venue's space a run actually measures, and how to get them back.

Spec §2 makes ``pytest-randomly``'s session seed the root of ALL sanctioned
randomness in this suite. This module DERIVES from that seed; it never creates
one. The reproduce handle for a failing conformance run is the
``--randomly-seed=N`` printed in that run's own pytest header, and that handle
is only true if the draw is a pure function of ``(space, budget, seed)``:

- a sampler that seeded itself (``os.urandom``, a clock, a fresh
  ``Random()``) would draw cells the header's number cannot reproduce, and
  the header would still be printed -- a reproduce handle that lies is worse
  than none, because the failure is then re-run, seen green, and closed;
- a sampler that drew from module-level ``random`` would be steered by
  whoever seeded that process-global stream last. ``pytest-randomly``
  reseeds it per test, so the draw would move under an unrelated test's
  seeding and, again, the published seed would not reproduce it.

So there is no PRNG object here at all. The draw is a KEYED DIGEST ORDER:
each cell is ranked by ``blake2b(seed:label)`` and the budget's worth of
lowest ranks is taken -- the standard min-hash sample, uniform over subsets
and without replacement.

THE REASON IS THE REPRODUCE HANDLE'S SHELF LIFE, and it is about Python
versions rather than about processes. This repo supports five interpreters
and gates on all of them (``.python-versions``, and ci.yml's matrix: 3.10
through 3.14), while the seed a failing run prints carries no interpreter
with it -- so the run that redraws those cells may well be a different Python
from the run that failed, months and a bump later. A digest is fixed by its
specification: same bytes in, same bytes out, on any conforming
implementation. ``random.sample``'s selection order is an implementation
detail instead, and that implementation does move -- MEASURED, its body
differs between 3.10 and 3.14 (the validation and ``counts=`` paths).

Be exact about what that measurement does and does not show. MEASURED here on
3.10, 3.11, 3.12, 3.13, 3.14 and 3.15b1: ``random.Random(seed).sample(...)``
returned the same draw on all six, so nothing is broken today -- the digest
buys a guarantee, not a bug fix. Those same six return an identical
``blake2b`` digest for a fixed input (``0a0f81a8a18c5bf92af572d8a9a18144``).

Uniformity is the one property this owes the stdlib version, which had it for
free. ``tests/unit/test_conformance_sampling.py`` measures it instead of
assuming it: over 400 seeds drawing 1 of 8, every cell is drawn and the
busiest is 1.26x its expected share.

Secondary, and NOT the reason: ``random.Random(seed)`` also trips ruff's
S311, whose subject is cryptographic suitability -- irrelevant to a seeded
test sampler, and a call this repo has already made at each of the ten sites
carrying ``# noqa: S311`` (six in ``scripts/gen_monitor_fixtures.py``, four
under ``tests/``, two of those the chaos lane's own seeded ``Random``,
commented "reproducibility, not security"). That noqa
would have been perfectly acceptable here; the version-stability argument
above is what decides it.

Not to be confused with :mod:`otto.testing.conformance`, which asserts that
pluggable BACKEND INTERFACES conform. This tree is about HOST CONTRACTS.
"""

import hashlib
import logging

import pytest

from tests.conformance._resolved import ResolvedCell

_LOG = logging.getLogger(__name__)


def cell_label(resolved: ResolvedCell) -> str:
    """A cell's name, in the ONE spelling the run uses everywhere.

    Also the parametrization id (``tests/conformance/conftest.py``), on
    purpose: the log below is how a reader gets from a failing test id back to
    the cell it named, and two spellings of the same cell would make that walk
    a guess.

    Names the venue's KIND as well as the cell, because ``local`` appears as
    both an element and as the term/transfer of every BusyBox cell, so
    ``element-term-transfer`` alone reads the same for cells standing up very
    different machinery. The kind is what tells a reader of a failure line
    whether an ``sshd`` was involved.
    """
    cell = resolved.cell
    return f"{resolved.kind}[{cell.element}:{cell.term}:{cell.transfer}]"


def draw(
    space: "list[ResolvedCell]", budget: "int | None", seed: "int | None"
) -> "list[ResolvedCell]":
    """Up to *budget* cells of *space*, chosen by *seed* alone.

    ``budget is None`` means every cell (``OTTO_CONFORMANCE_CELLS=all``), and a
    budget at or above the space's size is the same thing said differently.

    ``seed is None`` means this run has no root seed at all -- pytest-randomly
    is not loaded, so no ``--randomly-seed=N`` exists to reproduce a sample
    with. That draws the WHOLE space rather than a sample nobody could re-run:
    the budget can only ever REDUCE what runs, so falling back to everything is
    the direction that cannot hide a cell. :func:`log_draw` says so out loud.

    Ranks by ``blake2b(seed:label)`` and takes the lowest *budget* of them (see
    the module docstring). Keyed on the cell's LABEL rather than on its index,
    so the cells a seed draws do not all change the day an unrelated cell joins
    or leaves the space -- the labels are unique per cell, which
    ``tests/unit/test_conformance_cells.py`` already pins.

    Leaves the caller's list untouched, and never sorts or shuffles it in
    place: the conftest holds ONE space and draws from it, so an in-place
    reorder would make a second call with the same seed disagree with the
    first.
    """
    if budget is None or seed is None or budget >= len(space):
        return list(space)
    return sorted(space, key=lambda resolved: _rank(seed, resolved))[:budget]


def _rank(seed: int, resolved: ResolvedCell) -> bytes:
    """Where *resolved* sorts for this seed. Uniform, and a pure function of both."""
    return hashlib.blake2b(f"{seed}:{cell_label(resolved)}".encode(), digest_size=16).digest()


def root_seed(config: pytest.Config) -> "int | None":
    """This run's ``pytest-randomly`` seed, or ``None`` if there isn't one.

    ``pytest_configure`` resolves ``randomly_seed`` to an int and writes it
    back onto the config (and xdist workers receive the controller's value
    through ``workerinput``), so every process in one session derives the same
    draw -- which is also what keeps xdist's collection-match check happy.

    ``None`` covers exactly one case: ``-p no:randomly`` UNREGISTERS the
    option, and ``Config.getoption`` raises ``ValueError`` on an undeclared
    name UNLESS the caller passes a ``default`` -- so ``default=None`` here is
    load-bearing, not decoration. That shape is really run in this repo
    (``tests/unit/test_env_hermeticity.py`` drives a ``-p no:randomly``
    subprocess), and this tree sits in ``testpaths``, so a path-less run of it
    would collect this suite and a raise here would turn "no seed to sample
    from" into a collection error for a run that was never going to execute a
    conformance test.
    """
    seed = config.getoption("randomly_seed", default=None)
    return seed if isinstance(seed, int) else None


def log_draw(
    venue: str, space: "list[ResolvedCell]", drawn: "list[ResolvedCell]", seed: "int | None"
) -> None:
    """Report what this run drew, out of how many, at which seed.

    Spec §4 asks for BOTH numbers on every run, and the asymmetry is the whole
    reason: ``drawn=8`` alone reads identically whether the venue offered 17
    cells or had collapsed to 8. A venue that quietly stops resolving its
    loopback ``sshd`` still reports a full-sized draw; only ``space=`` moving
    makes that visible.

    Emitted at ``INFO`` because the ini pins ``log_level = INFO`` with
    ``log_cli = true``, so this is a level the run actually SHOWS. A line
    logged below the run's level is a line no one reads, and its guard would
    then be asserting on a message that never reaches a terminal.
    """
    _LOG.info(
        "conformance: venue=%s space=%d cells drawn=%d seed=%s",
        venue,
        len(space),
        len(drawn),
        seed,
    )
    _LOG.info("conformance: drew %s", ", ".join(cell_label(cell) for cell in drawn))
    if seed is None:
        _LOG.warning(
            "conformance: NO ROOT SEED -- pytest-randomly is not loaded, so no "
            "--randomly-seed=N could reproduce a sample. Drew the whole space instead; "
            "the cell budget was NOT applied"
        )
