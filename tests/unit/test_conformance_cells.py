"""What the hermetic conformance venue can build, and what it refuses to pretend it can.

Also where :func:`~tests.conformance._cells.resolve_space` is asserted, in
both venues: it is the one function that picks between them, so a test that
pinned only the hermetic half would leave the switch itself uncovered.

Not to be confused with :mod:`otto.testing.conformance`, which asserts that
pluggable *backend interfaces* conform. This covers the *host contract*
conformance suite's cell resolution under ``tests/conformance/``.
"""

import dataclasses
import importlib.util
from types import SimpleNamespace

import pytest

from tests._fixtures.paths import PROJECT_ROOT
from tests._fixtures.profiles import Cell
from tests.conformance import _cells
from tests.conformance._bed import BED_BUSYBOX, BED_UNIX, BED_ZEPHYR, bed_space
from tests.conformance._cells import hermetic_space, resolve_space
from tests.conformance._resolved import ResolvedCell
from tests.conformance._sample import cell_label
from tests.conformance._vocabulary import POSIX

_CONFORMANCE_CONFTEST = PROJECT_ROOT / "tests" / "conformance" / "conftest.py"


def _never_opened():
    """An opener no test in this file calls. Resolution never opens anything."""
    raise AssertionError("a cell resolved in a unit test must not be opened")


def _load_conformance_conftest():
    """Execute ``tests/conformance/conftest.py`` fresh, under a name of its own.

    The conftest resolves the venue's space at IMPORT (``_SPACE =
    resolve_space()``) and refuses an empty one there, so the only way to
    exercise that refusal is to run the module body again. Loaded under a
    private name and never inserted into ``sys.modules``, so the copy pytest
    is already running this session with is untouched.

    What this does NOT prove: that pytest surfaces the failure as a run-ending
    error. That is pytest's own handling of an exception raised while
    importing a conftest, and reproducing it here would cost a subprocess run
    for a behaviour no edit in this repo can change.
    """
    spec = importlib.util.spec_from_file_location(
        "otto_conformance_conftest_probe", _CONFORMANCE_CONFTEST
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_hermetic_space_is_not_empty() -> None:
    """A venue that resolves nothing would make every sample vacuously green."""
    space = hermetic_space()
    assert space, "the hermetic venue resolved no cells at all"


def test_no_resolved_cell_claims_a_lab_only_access_path() -> None:
    """No cell the hermetic venue offers may claim telnet or a console.

    This pins the FIXTURES' declarations, not the resolver's filter, and the
    distinction matters: measured, this test stays GREEN with ``_servable``
    replaced by ``return True``, because the real loopback entry declares only
    ssh / sftp + scp and every busybox cell is local. The guard that actually
    exercises the exclusion is
    ``test_a_menu_entry_the_venue_cannot_serve_is_excluded_not_offered`` below,
    which injects telnet and console into the built host's menus.

    Kept because the property is still worth pinning at this layer: if a future
    fixture starts declaring a lab-only path, the hermetic space must not
    silently start offering cells nobody can stand up.
    """
    space = hermetic_space()
    assert not [c for c in space if c.cell.term == "telnet"]
    assert not [c for c in space if c.cell.transfer == "console"]


def test_every_resolved_cell_names_how_it_was_built() -> None:
    """``kind`` is what the run logs; an unnamed cell is unreproducible."""
    for resolved in hermetic_space():
        assert resolved.kind in {"loopback-ssh", "busybox-artifact", "local"}


def test_the_space_covers_more_than_one_kind() -> None:
    """Otherwise the exclusion assertions above pass for the wrong reason."""
    kinds = {r.kind for r in hermetic_space()}
    assert len(kinds) >= 2, f"only {kinds} resolved; exclusion tests prove nothing"


def test_no_cell_is_drawn_twice() -> None:
    """A duplicate is a cell that gets two lots of the sampler's attention.

    Sampling without replacement over a list with repeats silently reweights
    the space, so the same contract can be asserted twice while another goes
    unasserted -- and the run's own "drew N of M" log would still read right.
    """
    cells = [r.cell for r in hermetic_space()]
    assert len(cells) == len(set(cells))


def test_every_resolved_cell_carries_an_opener() -> None:
    """A cell with no way to stand its host up cannot be asserted against."""
    for resolved in hermetic_space():
        assert callable(resolved.open_host), f"{resolved.cell} resolved without an opener"


def test_a_menu_entry_the_venue_cannot_serve_is_excluded_not_offered() -> None:
    """Prove the exclusion FIRES rather than being satisfied by a thin space.

    The real loopback entry declares only ``ssh`` / ``sftp`` + ``scp``, so
    ``test_no_resolved_cell_claims_a_lab_only_access_path`` above would hold
    even with no filter at all -- it tests the chaos fixture's declaration,
    not this resolver. Here the built
    host is made to report ``telnet`` and ``console`` as well; both must be
    dropped, and the servable pairs must survive.
    """
    monkeyed = SimpleNamespace(
        element="loopback",
        valid_terms=["ssh", "telnet"],
        valid_transfers=["sftp", "console", "scp"],
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_cells, "create_host_from_dict", lambda data: monkeyed)
        space = _cells.hermetic_space()
    pairs = {(c.cell.term, c.cell.transfer) for c in space if c.kind == "loopback-ssh"}
    assert pairs == {("ssh", "sftp"), ("ssh", "scp")}


def test_loopback_cells_are_read_off_the_built_host_not_hardcoded() -> None:
    """Item 2's lesson: a re-derived menu is wrong for more than half the bed.

    A hardcoded ``["sftp", "scp"]`` would keep passing after the loopback
    target stopped agreeing with it, so the menu is mutated here and the space
    must follow it.
    """
    monkeyed = SimpleNamespace(element="loopback", valid_terms=["ssh"], valid_transfers=["nc"])
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_cells, "create_host_from_dict", lambda data: monkeyed)
        space = _cells.hermetic_space()
    loopback = [c.cell for c in space if c.kind == "loopback-ssh"]
    assert [(c.term, c.transfer) for c in loopback] == [("ssh", "nc")]


def test_a_busybox_release_this_machine_cannot_run_is_excluded_not_skipped() -> None:
    """The only exclusion branch that fires on a differently-provisioned machine.

    x86 artifacts need qemu-user-static with binfmt registered on aarch64. A
    machine without it must lose those cells from the SPACE -- a skip inside a
    drawn cell would report success for a contract nobody ran.
    """
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_cells, "can_run", lambda arch, **kwargs: False)
        space = _cells.hermetic_space()
    assert not [c for c in space if c.kind == "busybox-artifact"]
    assert space, "excluding busybox must not empty the venue"


def test_resolve_space_defaults_to_the_hermetic_venue(monkeypatch) -> None:
    monkeypatch.delenv("OTTO_CONFORMANCE_BED", raising=False)
    assert [r.cell for r in resolve_space()] == [r.cell for r in hermetic_space()]


def test_the_bed_venue_resolves_the_whole_bed_space(monkeypatch) -> None:
    """F5: the ``NotImplementedError`` is gone, and must not leave a vacuum behind.

    ``assert resolve_space()`` alone is not that guarantee. It reads the same
    for a space of 49 and for a space of 1, and a truncated space is the shape
    that certifies everything it dropped: the run would log ``drew 1 of 1``
    and go green having asserted one cell. So the dispatch is pinned against
    the whole space the bed venue offers -- whose own size and order are
    pinned against a recorded expectation in
    ``tests/unit/test_conformance_bed.py`` -- and against the kinds it must
    contain, so a space truncated to one family reddens here too.
    """
    monkeypatch.setenv("OTTO_CONFORMANCE_BED", "1")
    resolved = resolve_space()
    assert resolved, "the bed venue resolved zero cells -- every contract would be vacuous"
    assert [cell_label(r) for r in resolved] == [cell_label(r) for r in bed_space()]
    assert {r.kind for r in resolved} == {BED_UNIX, BED_BUSYBOX, BED_ZEPHYR}


def test_the_bed_venue_does_not_hand_back_the_hermetic_space(monkeypatch) -> None:
    """The mis-dispatch that a truthiness check cannot see.

    A ``resolve_space`` that fell through to ``hermetic_space()`` under the bed
    knob returns eight real cells: non-empty, every contract runs, every
    assertion passes -- against a loopback ``sshd`` while the run's own header
    says ``venue=bed``. The two spaces share no cell label (the kind is part of
    it), so this reads as a discriminator rather than as a size comparison.
    """
    monkeypatch.setenv("OTTO_CONFORMANCE_BED", "1")
    bed = {cell_label(r) for r in resolve_space()}
    monkeypatch.delenv("OTTO_CONFORMANCE_BED")
    hermetic = {cell_label(r) for r in resolve_space()}
    assert bed, "the bed venue resolved nothing, so the comparison below proves nothing"
    assert hermetic, "the hermetic venue resolved nothing, so the comparison below proves nothing"
    assert not bed & hermetic, f"both venues resolved {sorted(bed & hermetic)}"


def test_the_bed_venue_reads_the_bed_resolver_rather_than_agreeing_with_it(monkeypatch) -> None:
    """INJECTED: the dispatch must CALL ``bed_space``, not merely match it today.

    Both tests above compare the dispatch with real lab data, so a copy of the
    bed space -- a snapshot taken at import, a second concatenation written
    out here -- would satisfy them for as long as the copy happened to agree.
    A sentinel no real lab data can produce cannot be agreed with by accident.
    """
    sentinel = [
        ResolvedCell(
            cell=Cell("sentinel", "ssh", "scp"),
            kind="sentinel",
            open_host=_never_opened,
            # A fabrication has to answer this too, and answering `None` here
            # is deliberate: nothing in this test transfers, and a sentinel
            # that claimed a real landing directory would be inventing a
            # property the dispatch is not being asked about.
            remote_scratch=None,
            vocabulary=POSIX,
        )
    ]
    monkeypatch.setenv("OTTO_CONFORMANCE_BED", "1")
    monkeypatch.setattr(_cells, "bed_space", lambda: sentinel)
    assert resolve_space() == sentinel


def test_an_empty_bed_space_fails_the_run_rather_than_going_green(monkeypatch) -> None:
    """F5's replacement guarantee, exercised instead of asserted.

    The raise this venue removed existed so that a space of nothing could not
    pass vacuously. What replaces it is the check
    ``tests/conformance/conftest.py`` already makes on ``_SPACE`` at import --
    written in item 3 for "the venue that can shrink", and until now unable to
    fire at all, since ``hermetic_space()`` always contains the local cell.
    The bed venue is that venue, so this is where that guard stops being
    unreachable code.

    The hostile condition is INJECTED. Real lab data cannot produce an empty
    bed space -- ``axis_space`` raises ``KeyError`` on a lab no host declares,
    so even a renamed lab fails loudly rather than resolving nothing -- and a
    guard that waited for real data to go empty would never run.
    """
    monkeypatch.setenv("OTTO_CONFORMANCE_BED", "1")
    monkeypatch.setattr(_cells, "bed_space", list)  # `list()` IS the empty space
    with pytest.raises(RuntimeError, match="resolved ZERO cells"):
        _load_conformance_conftest()


def test_the_conftest_loads_when_the_bed_space_is_not_empty(monkeypatch) -> None:
    """The positive control for the test above, and it pins the wiring too.

    Without this, an unrelated ``RuntimeError`` -- a bad path, an import error
    in the probe -- would satisfy that ``pytest.raises`` and the empty-space
    guard would look proven while never having been reached. This also pins
    what the conftest resolves under the knob: the real bed space, which is
    the wiring Task 4 is for.
    """
    monkeypatch.setenv("OTTO_CONFORMANCE_BED", "1")
    module = _load_conformance_conftest()
    assert [cell_label(r) for r in module._SPACE] == [cell_label(r) for r in bed_space()]


def test_resolved_cells_are_frozen() -> None:
    """A sampler that could rewrite a drawn cell would log one and assert another."""
    resolved = hermetic_space()[0]
    assert isinstance(resolved, ResolvedCell)
    with pytest.raises(dataclasses.FrozenInstanceError):
        resolved.kind = "rewritten"
