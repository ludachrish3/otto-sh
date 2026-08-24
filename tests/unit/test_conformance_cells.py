"""What the hermetic conformance venue can build, and what it refuses to pretend it can.

Not to be confused with :mod:`otto.testing.conformance`, which asserts that
pluggable *backend interfaces* conform. This covers the *host contract*
conformance suite's cell resolution under ``tests/conformance/``.
"""

import dataclasses
from types import SimpleNamespace

import pytest

from tests.conformance import _cells
from tests.conformance._cells import ResolvedCell, hermetic_space, resolve_space


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


def test_the_bed_venue_raises_rather_than_resolving_an_empty_space(monkeypatch) -> None:
    """``current_venue()`` can already answer ``bed`` with nothing behind it.

    Returning ``[]`` there would make every sampled contract vacuously green --
    the defect class this whole workstream exists to eliminate -- so the
    unbuilt venue names the item that will build it instead.
    """
    monkeypatch.setenv("OTTO_CONFORMANCE_BED", "1")
    with pytest.raises(NotImplementedError, match="item 4"):
        resolve_space()


def test_resolved_cells_are_frozen() -> None:
    """A sampler that could rewrite a drawn cell would log one and assert another."""
    resolved = hermetic_space()[0]
    assert isinstance(resolved, ResolvedCell)
    with pytest.raises(dataclasses.FrozenInstanceError):
        resolved.kind = "rewritten"
