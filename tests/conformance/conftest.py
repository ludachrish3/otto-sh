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
tempting to add. **CORRECTED, WITH A MEASUREMENT.** This paragraph used to say
that a group stamped here would be honoured under ``make conformance`` and
silently inert under ``pytest tests/conformance``, because this hook's position
relative to xdist's own ``pytest_collection_modifyitems`` depends on the
INVOCATION SHAPE. That is TRUE of ``tests/integration/host/`` and NOT of this
tree, and the difference is structural: ``tests/conformance`` is itself a
``testpaths`` entry (``pyproject.toml``), so this conftest is an *initial*
conftest under BOTH shapes and the shape decides nothing. Measured here, one
cell's six items under ``-n2 --dist loadgroup``: with the stamp and no
``tryfirst`` there is no ``@group`` suffix in EITHER shape and the items split
across two workers; adding ``tryfirst`` makes it honoured in BOTH (``@local``
suffix, all six on one worker). What the stamp depends on is ``tryfirst`` and
``--dist loadgroup``, not the command line. This tree still stamps no group,
and ``tests/conformance/_console_safety.py`` records the three reasons a LOCK
was chosen over one for the single-client-console protection below.

Each module here ALSO declares ``pytestmark = [pytest.mark.conformance]``, and
those marks are load-bearing rather than redundant, for the reasons
``tests/busybox/conftest.py`` sets out: the stamp cannot survive its own file,
so deleting or renaming this conftest would silently rejoin every contract here
to the catch-all lanes; and a test should say what it needs where it is
written. Its third reason -- that a stamp's effect is invocation-shape
dependent while a ``pytestmark`` is attached at item construction and so is not
-- is a true statement about a stamp in general and, per the measurement above,
NOT one about a stamp in this tree.
"""

from collections.abc import Callable
from functools import partial
from pathlib import Path

import pytest

from tests.conformance._cells import resolve_space
from tests.conformance._console_safety import (
    console_lock_dir,
    opens_a_single_client_console,
    serialized_console,
    unhonored_console_lock,
)
from tests.conformance._observable import note_observable as _note_observable
from tests.conformance._observation import (
    note_domain_exclusions,
    record_phase,
    write_domain_exclusions,
)
from tests.conformance._resolved import ResolvedCell
from tests.conformance._sample import cell_label, draw, log_draw, root_seed
from tests.conformance._venue import cell_budget, current_venue

_CONFORMANCE_ROOT = Path(__file__).parent

# Resolved once, at conftest import, because the sampler draws from it and
# pytest needs the parameter list before collection finishes.
# Cheap enough to sit on the collection path of every path-less run in the
# repo: measured at 0.01s for the hermetic venue's 8 cells, because
# `_cells.resolve_space` deliberately starts no daemon and fetches nothing at
# resolve time. The BED venue's 49 cells measure 0.06s on a cold interpreter
# and 0.011s warm — RE-MEASURED, and the host-build count in this comment was
# wrong before: it is 65 builds through otto's factory over 16 distinct
# elements, not nineteen. Three questions are asked per element (its kind, its
# scratch directory and its vocabulary, each of which builds the host to read
# otto's answer) on top of the menus `axis_space` reads per lab, and `test1`
# is crossed under two labs. That cost is paid only under
# `OTTO_CONFORMANCE_BED=1`, which no default lane sets.
_SPACE = resolve_space()

# An empty space is the one failure this suite could not otherwise report. Every
# contract would be parametrized over nothing, collect zero items, and the lane
# would pass — the "green run that asserted nothing" that the whole venue design
# exists to make impossible. Written for "the venue that can shrink", which now
# exists: `hermetic_space()` always contains the local cell and `bed_space()`
# cannot come back empty either (`axis_space` RAISES on a lab no host declares),
# so neither venue can fire this from real data — which is why
# `tests/unit/test_conformance_cells.py` injects an empty bed space and observes
# this refusal, rather than leaving it as a check nothing has ever reached.
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


#: The name a contract module gives a one-argument predicate to DECLARE ITS
#: APPLICABLE DOMAIN -- the drawn cells it is a contract about. Absent means
#: "every drawn cell", which is what three of the six contracts here say -- the
#: three in the exec file. The other three are covered by two declarations (the
#: transfer file declares one for both of its, and the timeout file for its
#: one), which is why the module count and the contract count differ.
_DOMAIN_HOOK = "applicable_cell"

#: The name a contract module gives a one-argument function to declare that a
#: drawn cell is EXPECTED TO FAIL, returning the reason or None. Applied as
#: ``xfail(strict=True)``, which is an ASSERTION and not a suppression: the
#: item must fail, an unexpected pass (``XPASS``) is a hard error, and the
#: marker's own removal is what a fixed product forces. NO MODULE DECLARES ONE
#: TODAY: ``test_transfer_contract.py`` did until 2026-08-25, for a root-caused
#: defect in otto's ``nc`` listener spelling on BusyBox, and the universal
#: ``nc -l -p PORT`` spelling repaid it -- which is the removal this strictness
#: exists to force. The mechanism stays for the next declaration, and the
#: convention with it: the whole decision lives in the declaring module under
#: its own banner, so that reversing it is a single-file change.
_XFAIL_HOOK = "expected_failure"


def pytest_generate_tests(metafunc):
    """Parametrize ``resolved_cell`` over the DRAW, narrowed to the module's domain.

    Here rather than on the fixture's ``params=``: the draw needs
    ``metafunc.config`` to read pytest-randomly's seed, and a ``params=``
    argument is evaluated at conftest import, before any config exists.

    A CONTRACT MAY DECLARE THE CELLS IT IS ABOUT, by defining a module-level
    :data:`_DOMAIN_HOOK` predicate over the ``ResolvedCell``. Two MODULES do
    today, covering three of the six contracts, and they read different
    things:

    - ``test_transfer_contract.py`` is about ``put``/``get``, and the three bed
      guests declaring ``filesystem: "none"`` have nowhere to put a file --
      OTTO'S own answer, not this suite's (see
      :attr:`~tests.conformance._resolved.ResolvedCell.remote_scratch`).
    - ``test_timeout_contract.py`` is about a command that outlives its budget,
      and the seven bed Zephyr cells have no such command -- THIS SUITE'S
      answer, because otto describes no host as "can be made to block". Its
      docstring says so rather than letting the two narrowings look alike.

    A CONTRACT MAY ALSO DECLARE A CELL A KNOWN FAILURE, by defining a
    module-level :data:`_XFAIL_HOOK` function returning a reason or ``None``.
    That becomes ``xfail(strict=True)`` on that cell's items -- see
    :func:`_expected_failure_marks` for why the strictness is not optional. No
    module declares one today; one did until 2026-08-25, for a root-caused
    defect in otto's own ``nc`` listener, and the fix removed the declaration
    along with the defect. The two hooks say DIFFERENT things and must not be
    confused: a domain says the contract is not ABOUT this cell, so nothing is
    asserted and nothing is claimed; an expected failure says the contract IS
    about it and otto currently breaks it, so the failure is asserted and the
    fix is what removes the declaration.

    Declaring a domain is NOT skipping and NOT shrinking the space. A skip
    inside a drawn cell reports success for a contract nobody ran, which is
    the failure this suite exists to make impossible; and dropping the cell
    from the space would delete the guest entirely, since a Zephyr host
    reports a single ``(telnet, console)`` pair and so has exactly one cell.
    What a domain says is what the contract COVERS, which is a different
    statement from what a run MEASURED, and the run's own draw line still
    names every cell it drew.

    A domain that narrows the draw to NOTHING raises rather than
    parametrizing over an empty list. pytest's answer to an empty parameter
    set is to mark the item SKIPPED, which would put back exactly the silent
    green this whole venue is built against -- and the failure would be
    invisible in a report that already contains the draw line. Unreachable
    from real data (of the 49 bed cells, 3 are outside the transfer domain and
    7 outside the timeout one; 0 of the 8 hermetic cells is outside either), so
    it is exercised by injection in
    ``tests/unit/test_conformance_bed.py`` rather than left as a check nothing
    has ever reached.
    """
    if "resolved_cell" not in metafunc.fixturenames:
        return
    drawn = _drawn_cells(metafunc.config)
    applies = getattr(metafunc.module, _DOMAIN_HOOK, None)
    expected = getattr(metafunc.module, _XFAIL_HOOK, None)
    if applies is not None:
        within = [resolved for resolved in drawn if applies(resolved)]
        # The complement, remembered before it disappears. An excluded cell
        # generates no item, so `pytest_runtest_makereport` can never see it --
        # and the matrix's `not_observable` list has no other source. See
        # `tests/conformance/_observation.py`.
        note_domain_exclusions(
            metafunc.config,
            metafunc.definition.nodeid,
            [resolved for resolved in drawn if resolved not in within],
        )
        if not within:
            raise RuntimeError(
                f"{metafunc.module.__name__} declares an applicable domain that none of "
                f"the {len(drawn)} drawn cells is in: "
                f"{', '.join(cell_label(resolved) for resolved in drawn)}. "
                f"Parametrizing over nothing would mark this contract SKIPPED, which "
                f"reports success for a contract nobody ran -- widen the draw "
                f"(OTTO_CONFORMANCE_CELLS), or fix the domain."
            )
        drawn = within
    params = [
        pytest.param(resolved, marks=_expected_failure_marks(expected, resolved))
        for resolved in drawn
    ]
    metafunc.parametrize("resolved_cell", params, ids=cell_label, indirect=True)


def _expected_failure_marks(
    expected: "Callable[[ResolvedCell], str | None] | None", resolved: "ResolvedCell"
) -> "list[pytest.MarkDecorator]":
    """``[xfail(strict=True)]`` when the module declares *resolved* a known failure.

    STRICT, ALWAYS, and never parametrized on that. A non-strict xfail is a
    suppression -- it absorbs both outcomes, so the lane goes green whether
    the defect is present or fixed and nothing ever forces the marker's
    removal. Strict asserts the failure: the item must fail, and the day otto
    is fixed the run reports ``XPASS`` and the lane is RED until someone
    deletes the declaration. That is the property that makes this an
    assertion about a known product defect rather than a way to buy a green.

    Kept out of :func:`pytest_generate_tests` so the strictness is stated in
    one place and cannot be softened per module.
    """
    if expected is None:
        return []
    reason = expected(resolved)
    if not reason:
        return []
    return [pytest.mark.xfail(reason=reason, strict=True)]


def pytest_collection_modifyitems(config, items):
    """Auto-apply the ``conformance`` marker to every item under this tree.

    Idempotent and additive: an item that already declares the marker is
    unharmed, and any other marker it carries survives.
    """
    for item in items:
        if _CONFORMANCE_ROOT in item.path.parents:
            item.add_marker("conformance")


def _cell_under_test(node) -> "ResolvedCell | None":
    """The cell *node* was parametrized over, or None if it was not.

    Read off the CALLSPEC rather than off the fixture, and that is what makes
    it usable from a hook: :func:`pytest_runtest_call` runs before any fixture
    value is reachable, and asking the fixture would be asking the very
    machinery whose effect is in question. ``resolved_cell`` is an INDIRECT
    param, and an indirect param's raw value is what ``callspec.params`` holds
    -- the same read ``tests/integration/host/conftest.py`` makes of its own
    indirect ``host1``.

    ``isinstance`` rather than a bare lookup: this tree's non-parametrized
    tests (the bed opener witness) and anything a future module parametrizes
    under a name collision must answer None rather than a value this code would
    then call ``.kind`` on.
    """
    callspec = getattr(node, "callspec", None)
    if callspec is None:
        return None
    resolved = callspec.params.get("resolved_cell")
    return resolved if isinstance(resolved, ResolvedCell) else None


# No source-reachability hook here, on purpose. One lived at
# ``pytest_collection_finish``, keyed on a BusyBox cell surviving collection —
# reasoning that was right about deselection (``make coverage`` collects this
# tree and deselects all of it) and silent about ``--collect-only``, which
# collects the cells, executes none of them, and fires the hook regardless
# (issue #264). ``session.items`` being non-empty proves items were COLLECTED,
# never that they will EXECUTE. The one-probe bound now lives in
# ``tests._fixtures.busybox.busybox_binary``, the consumer, which every
# BusyBox cell's ``open_host`` reaches at the moment it needs the artifact.


@pytest.fixture(autouse=True)
def _single_client_console(request: pytest.FixtureRequest, tmp_path_factory):
    """Hold the console EXCLUSIVELY for any item whose cell opens a single-client one.

    Autouse and function-scoped, and depending on neither ``resolved_cell`` nor
    the host, so the hold spans the entire window in which the test body can
    have a console open -- the ``async with resolved_cell.open_host()`` in
    every contract here opens and closes inside the body, well within it.

    A no-op for every other cell. That is the whole point of a lock rather than
    an ``-n0`` lane: the bed venue's 49 cells include 7 single-client console
    ones, and only those seven pay for the serialization. Full one-group
    serialization of the bed measured >450s against the Makefile's 240s cap
    (``tests/integration/host/conftest.py``'s grouping note), which is the
    price this does not pay.

    Why an EXCLUSIVE hold rather than the SHARED one the integration tree's
    per-device tests take, and what it does not cover, are in
    ``tests/conformance/_console_safety.py``.
    """
    resolved = _cell_under_test(request.node)
    if resolved is None or not opens_a_single_client_console(resolved):
        yield
        return
    with serialized_console(console_lock_dir(tmp_path_factory)):
        yield


def pytest_runtest_call(item):
    """Refuse to run a console cell that is not actually holding the console.

    The regression guard for the fixture above, and it is deliberately NOT
    written as "the fixture ran". ``tests/integration/host/conftest.py``'s
    ``_unhonored_group`` makes the same distinction from the other side: the
    presence of an ``xdist_group`` marker proves nothing, because xdist may
    have read the markers before they were set, so that guard looks for the
    ``@group`` suffix xdist LEAVES BEHIND. Here the equivalent of the ignored
    marker is a ``console_access`` that opened its lock files in a directory no
    other worker uses, or one replaced by a no-op: every local signal still
    says "protected". :func:`unhonored_console_lock` asks the kernel instead.

    In ``pytest_runtest_call`` rather than ``pytest_runtest_setup`` because the
    thing it checks is a fixture's OUTCOME, so it has to run after setup;
    pluggy dispatches this conftest's impl before ``_pytest.runner``'s (core
    plugins register first, and same-phase impls run LIFO), so a failure here
    aborts the item before its body can open anything. Verified in this
    worktree, both invocation shapes.
    """
    resolved = _cell_under_test(item)
    if resolved is None or not opens_a_single_client_console(resolved):
        return
    unhonored = unhonored_console_lock()
    if unhonored is not None:
        pytest.fail(
            f"single-client console serialization is NOT in effect for "
            f"{cell_label(resolved)}: {unhonored}.\n"
            "Two clients on one Zephyr console is not a flake -- when a send fails at "
            "accept time the guest re-initialises its telnet backend and then refuses "
            "every connection until `make qemu-restart` (issue #260; two guests taken "
            "down that way in one day).\n"
            "Cause: tests/conformance/conftest.py's autouse `_single_client_console` "
            "fixture must wrap every item whose cell opens one. If it was narrowed or "
            "removed, restore it rather than silencing this check.",
            pytrace=False,
        )


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    """Emit this item's observation record, from the phase reports themselves.

    A WRAPPER over the report hook and not a line in the contract bodies, and
    the difference is the whole point: a record written inside a test, before
    its assertions run, reports success for a test that then fails -- a green
    record vouching for a red test, which is the fabrication
    ``schemas/support_matrix.json`` exists to prevent. Here the outcome is
    already decided and is read off pytest's own report.

    The cell is read off the CALLSPEC (:func:`_cell_under_test`) rather than
    off the fixture, for the reason that function gives: a report hook has no
    fixture values to ask.

    Not guarded by ``suppress``. An emitter that failed quietly would leave a
    run looking measured while it recorded nothing, which is worse than the
    loud INTERNALERROR a raise here produces.
    """
    report = yield
    record_phase(item, report, _cell_under_test(item), current_venue())
    return report


def pytest_sessionfinish(session, exitstatus):
    """Write the domain-exclusion records this process earned.

    At session finish because the gate is "a contract this process actually
    ran": an exclusion record says *this run exercised contract C and cell X
    was outside its domain*, and only the completed run knows the first half.
    See ``tests/conformance/_observation.py`` for why the excluded cells
    cannot be recovered from any report hook.
    """
    write_domain_exclusions(session.config, current_venue())


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


@pytest.fixture
def note_observable(request: pytest.FixtureRequest) -> "Callable[[str], None]":
    """Let a CONTRACT say which observable this cell actually gave it.

    A thin adapter over ``tests/conformance/_observable.py``'s function of the
    same name, so a contract body reads ``note_observable("...")`` rather than
    threading ``request`` through itself.

    WHY A CONTRACT WOULD NEED THIS AT ALL, when it already declares an
    observable on its ``@pytest.mark.observable`` marker: the marker is a
    template rendered against the cell's vocabulary, and a template cannot
    express a branch the CELL decides at run time. ``put(mode=...)`` is read
    back with ``stat -c %a`` where the backend carries a permission model and
    is a pre-flight REFUSAL where it does not; the framing contract can assert
    exact equality only where the tester chose the output. Those are different
    observables, not two spellings of one, and only the running test knows
    which it got.

    THIS DOES NOT MAKE A RECORD FABRICABLE. The OUTCOME still comes from
    pytest's own report at teardown -- nothing written through here can turn a
    failure into a pass. What a body supplies is the DESCRIPTION of what it
    watched, which is the one thing only the body knows.
    """
    return partial(_note_observable, request)


@pytest.fixture
def remote_scratch(resolved_cell: ResolvedCell, tmp_path: Path) -> Path:
    """A directory ON THIS CELL'S HOST that the test may transfer into.

    The venue answers, not the contract, and that is the whole point: the
    hermetic venue's far side IS the runner, so its answer is a directory
    under this test's own ``tmp_path``; the bed venue's far side is a device
    across a hop, so its answer is a path otto resolved from the host's
    declared filesystem. A contract that spelled either one itself would be
    right in one venue and wrong in the other -- which is the bug this
    fixture replaces, a runner ``tmp_path`` handed to ``scp`` as a remote
    directory on every bed cell.

    Raises rather than returning a fallback for a cell that has nowhere to
    put a file. That cell should never reach here: a contract needing a
    remote directory declares that domain (``applicable_cell``) and is not
    parametrized over it at all, so arriving here means the domain and the
    fixture disagree -- and a fallback would silently transfer into a
    runner path again.
    """
    if resolved_cell.remote_scratch is None:
        raise RuntimeError(
            f"{cell_label(resolved_cell)} has nowhere on its host to put a file, so this "
            f"contract must declare an applicable domain (`applicable_cell`) that excludes "
            f"it rather than asking for a remote directory it does not have"
        )
    return resolved_cell.remote_scratch(tmp_path)
