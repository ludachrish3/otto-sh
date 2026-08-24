"""The cell fixture every contract in this tree takes, and the marker that files it.

Two jobs, and they are the same job seen from either end: make the suite
runnable, and keep it out of every lane that did not ask for it.

THE MARKER IS WHAT KEEPS THIS TREE OUT OF THE DEFAULT GATES, and the reason is
the same shape as ``tests/busybox/conftest.py``'s, arrived at from a different
resource. ``Makefile``'s ``M_HOSTLESS`` is a pure negation
(``not integration and not embedded and ...``), so a brand-new test carrying
none of those markers satisfies every clause and is SELECTED by the ordinary
gates. The moment ``tests/conformance`` joined ``testpaths`` — which it had to,
or a path-less ``pytest -m conformance`` collects nothing and exits 5 — every
``make coverage`` would have started a throwaway ``sshd`` on 127.0.0.1 and run
five BusyBox artifacts as subprocesses. ``not conformance`` now rides every
catch-all in ``Makefile`` and ``noxfile.py`` for exactly that reason.

DELIBERATELY STAMPS NOTHING ELSE, and ``xdist_group`` is the one it would be
tempting to add. It could not be trusted to: this hook's position relative to
xdist's own ``pytest_collection_modifyitems`` depends on the INVOCATION SHAPE
(naming this directory on the command line makes this an *initial* conftest,
which registers early and therefore runs LATE, after xdist has already read the
markers), so a group stamped here would be honoured under ``make conformance``
and silently inert under ``pytest tests/conformance``. See
``tests/busybox/conftest.py``, which records the same trap and the measurement
behind it.

Each module here ALSO declares ``pytestmark = [pytest.mark.conformance]``, and
those marks are load-bearing rather than redundant, for the three reasons
``tests/busybox/conftest.py`` sets out: the stamp's effect is
invocation-shape-dependent while a ``pytestmark`` is attached at item
construction and so is not; the stamp cannot survive its own file, so deleting
or renaming this conftest would silently rejoin every contract here to the
catch-all lanes; and a test should say what it needs where it is written.
"""

from pathlib import Path

import pytest

from tests.conformance._cells import ResolvedCell, resolve_space
from tests.conformance._sample import cell_label, draw, log_draw, root_seed
from tests.conformance._venue import cell_budget, current_venue

_CONFORMANCE_ROOT = Path(__file__).parent

# Resolved once, at conftest import, because the sampler draws from it and
# pytest needs the parameter list before collection finishes.
# Cheap enough to sit on the collection path of every path-less run in the
# repo: measured at 0.01s for the hermetic venue's 8 cells, because
# `_cells.resolve_space` deliberately starts no daemon and fetches nothing at
# resolve time.
_SPACE = resolve_space()

# An empty space is the one failure this suite could not otherwise report. Every
# contract would be parametrized over nothing, collect zero items, and the lane
# would pass — the "green run that asserted nothing" that the whole venue design
# exists to make impossible. `hermetic_space()` always contains the local cell,
# so this cannot fire today; it is here for the venue that can shrink.
if not _SPACE:
    raise RuntimeError(
        "the conformance venue resolved ZERO cells, so every contract below would "
        "be parametrized over nothing and the lane would report green having "
        "asserted nothing -- see tests/conformance/_cells.py"
    )


_DRAWN_KEY = pytest.StashKey["list[ResolvedCell]"]()


def _drawn_cells(config) -> "list[ResolvedCell]":
    """The cells THIS session measures: ``cell_budget()`` of them, off the root seed.

    Memoised on the CONFIG's stash rather than in a module global, so the memo
    cannot outlive the session that made it: a module global would hand a
    second ``Config`` built in the same process (this repo builds one in
    ``tests/unit/test_browser_guard.py``) the first session's draw and seed.

    The run's own log is emitted here rather than in
    :func:`pytest_sessionstart` on purpose. Whether that hook fires for this
    conftest depends on the INVOCATION SHAPE — it is registered early enough
    only when this tree is an initial conftest (named on the command line, or
    reached through ``testpaths``) — while a draw is needed unconditionally at
    parametrization. Logging from the memo means the line is emitted exactly
    once per process by whichever of the two paths gets here first, so no
    invocation can run this suite without saying what it drew.

    Every process in a session draws the SAME cells: the draw is a pure
    function of the space, the budget and the seed, and xdist hands each worker
    the controller's ``randomly_seed``. That is not merely tidy — xdist rejects
    a session whose workers collected different test ids.
    """
    drawn = config.stash.get(_DRAWN_KEY, None)
    if drawn is None:
        seed = root_seed(config)
        drawn = draw(_SPACE, cell_budget(), seed)
        config.stash[_DRAWN_KEY] = drawn
        log_draw(current_venue(), _SPACE, drawn, seed)
    return drawn


def pytest_sessionstart(session):
    """Draw and report at session start, per spec §4 — see :func:`_drawn_cells`.

    Session start rather than after collection, and the cost is visible: this
    tree is in ``testpaths``, so EVERY path-less run in the repo logs a draw —
    including ``make coverage``, whose two path-less legs (``Makefile:600``
    and ``:602``) each log one and then deselect every conformance item
    (measured on a full run: exactly two draw lines, and zero
    ``tests/conformance/`` node ids selected). No whole-repo deselect count is
    quoted here on purpose: it drifts with every test the repo gains, and the
    ``838`` first written in this docstring was the hostless lane's figure
    (``M_HOSTLESS``), not this one's — which is 313 today and will not stay
    313. Conditioning
    on the final selected item list is the obvious fix and a worse one: that
    list only exists in the process that collected, and measured under the
    suite's default ``-n auto`` exactly ONE of these lines reaches the terminal
    with four workers running — so a line moved later could reach no terminal
    at all. A draw announced in a run that then skips the tree is honest
    about what it says: a draw was made, from a space of this size.
    """
    _drawn_cells(session.config)


def pytest_generate_tests(metafunc):
    """Parametrize ``resolved_cell`` over the DRAW, not over the whole space.

    Here rather than on the fixture's ``params=``: the draw needs
    ``metafunc.config`` to read pytest-randomly's seed, and a ``params=``
    argument is evaluated at conftest import, before any config exists.
    """
    if "resolved_cell" in metafunc.fixturenames:
        metafunc.parametrize(
            "resolved_cell", _drawn_cells(metafunc.config), ids=cell_label, indirect=True
        )


def pytest_collection_modifyitems(config, items):
    """Auto-apply the ``conformance`` marker to every item under this tree.

    Idempotent and additive: an item that already declares the marker is
    unharmed, and any other marker it carries survives.
    """
    for item in items:
        if _CONFORMANCE_ROOT in item.path.parents:
            item.add_marker("conformance")


@pytest.fixture
def resolved_cell(request: pytest.FixtureRequest) -> ResolvedCell:
    """One drawn cell of the selected venue's space, per test.

    Parametrized indirectly by :func:`pytest_generate_tests` above, which is
    where the draw and the ids live. Reproduce a failing cell with the
    ``--randomly-seed=N`` this run printed in its header;
    ``OTTO_CONFORMANCE_CELLS=all`` widens any run back to the whole space.

    Hands back the ``ResolvedCell``, not an open host: opening is an async
    context manager the test enters itself, so a contract that wants to observe
    the host both during and after a failure (the timeout contract does) can
    keep the session in scope for exactly as long as it needs.
    """
    return request.param
