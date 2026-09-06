"""Unit tests for the reservation check.

The library-facing gate (:class:`~otto.reservations.ReservationGate` and its
``evaluate()`` outcome matrix) is tested separately in ``test_gate.py``.
"""

from dataclasses import dataclass

import pytest

from otto.config.lab import Lab
from otto.host.element import Element
from otto.reservations import (
    MissingReservationError,
    NullReservationBackend,
    ResourceOrigin,
    check_reservations,
    required_resource_origins,
    required_resources,
)
from tests.conftest import make_host


@dataclass
class _FakeBackend:
    """Minimal in-memory ReservationBackend for testing the check function."""

    owners: dict[str, str]  # resource -> username

    def get_reserved_resources(self, username: str) -> set[str]:
        return {r for r, u in self.owners.items() if u == username}

    def who_reserved(self, resource: str) -> list[str]:
        u = self.owners.get(resource)
        return [u] if u is not None else []

    def backend_name(self) -> str:
        return "fake"


def _lab_with_resources() -> Lab:
    """Build a lab declaring {rack1} that also holds hosts.

    The hosts are the point: ``required_resources`` must read the LAB's
    declaration and nothing else. A host contributes nothing unless it
    declares ``resources`` — spec 2026-08-28 three-level-reservations — and
    ``make_host`` never sets that field, so this guards the shape rather than
    a filter.
    """
    return Lab(
        name="test_lab",
        resources={"rack1"},
        hosts={
            "test1": make_host("test1"),
            "test2": make_host("test2"),
        },
    )


def _lab_declaring(*resources: str) -> Lab:
    """Build a host-less lab that declares exactly ``resources``."""
    return Lab(name="test_lab", resources=set(resources))


class TestRequiredResources:
    def test_declared_lab_resources_only(self):
        """A lab whose hosts declare nothing requires exactly its own set."""
        lab = _lab_with_resources()
        assert required_resources(lab) == {"rack1"}

    def test_empty_lab(self):
        lab = Lab(name="empty")
        assert required_resources(lab) == set()


class TestCheckReservations:
    def test_full_coverage_returns_silently(self):
        lab = _lab_declaring("rack1", "test1", "test2")
        backend = _FakeBackend(
            owners={
                "rack1": "alice",
                "test1": "alice",
                "test2": "alice",
            }
        )
        check_reservations(lab, "alice", backend)  # must not raise

    def test_partial_coverage_raises_with_holders(self):
        lab = _lab_declaring("rack1", "test1", "test2")
        backend = _FakeBackend(
            owners={
                "rack1": "alice",
                "test1": "bob",  # held by someone else
                # test2 is absent from the dict: unreserved
            }
        )
        with pytest.raises(MissingReservationError) as exc_info:
            check_reservations(lab, "alice", backend)
        msg = str(exc_info.value)
        assert "alice" in msg
        assert "test_lab" in msg
        assert "test1" in msg
        assert "test2" in msg
        assert "held by: bob" in msg
        assert "held by: nobody" in msg

    def test_error_does_not_mention_skip_flag(self):
        """Regression guard — MissingReservationError must not advertise --skip-reservation-check."""  # noqa: E501 — descriptive docstring
        lab = _lab_with_resources()
        backend = _FakeBackend(owners={})
        with pytest.raises(MissingReservationError) as exc_info:
            check_reservations(lab, "alice", backend)
        assert "--skip-reservation-check" not in str(exc_info.value)
        assert "-R" not in str(exc_info.value)

    def test_null_backend_is_noop(self):
        lab = _lab_with_resources()
        check_reservations(lab, "anyone", NullReservationBackend())  # must not raise

    def test_empty_lab_is_noop(self):
        lab = Lab(name="empty")
        # Empty required set — backend never queried
        backend = _FakeBackend(owners={})
        check_reservations(lab, "alice", backend)

    def test_lists_multiple_holders_in_message(self):
        class _MultiHolderBackend:
            def __init__(self, holders):
                self._h = holders

            def get_reserved_resources(self, username):
                return {r for r, us in self._h.items() if username in us}

            def who_reserved(self, resource):
                return list(self._h.get(resource, []))

            def backend_name(self):
                return "multi"

        lab = Lab(name="shared_lab", resources={"rack1"})
        backend = _MultiHolderBackend(holders={"rack1": ["alice", "bob"]})
        with pytest.raises(MissingReservationError) as exc_info:
            check_reservations(lab, "carol", backend)
        assert "held by: alice, bob" in str(exc_info.value)


def _three_level_lab() -> Lab:
    """Build a lab spanning all three reservation levels (spec 2026-08-28
    three-level-reservations §4).

    ``make_host`` looks its argument up as a tech1 fixture element name (see
    ``tests/_fixtures/labdata.py:host_data``), and tech1 has no
    ``chassis1``/``chassis2``/``gw`` elements to look up — so real fixture
    ids (``test1``/``test2``/``test3``) are used to construct valid hosts,
    then ``id``/``element`` are overridden (plain dataclass fields, settable
    post-construction) to this scenario's shape: two hosts sharing one chassis
    element, plus a gateway host with no resources of its own.
    """
    chassis = Element("chassis", id=1, resources=frozenset({"chassis-1"}))
    h1 = make_host("test1")
    h1.id, h1.element, h1.resources = "chassis1", chassis, frozenset({"slot-1"})
    h2 = make_host("test2")
    h2.id, h2.element, h2.resources = "chassis2", chassis, frozenset({"slot-2"})
    gw = make_host("test3")
    gw.id, gw.element = "gw", Element("gw")
    return Lab(name="rig", resources={"rig-pdu"}, hosts={"chassis1": h1, "chassis2": h2, "gw": gw})


def test_origins_cover_all_three_levels_in_a_stable_order():
    assert required_resource_origins(_three_level_lab()) == [
        # The element owner is its slug — the element's ``id`` is data and
        # never labels anything (spec 2026-09-05 §2.1).
        ResourceOrigin("chassis-1", "element", "chassis"),
        ResourceOrigin("rig-pdu", "lab", "rig"),
        ResourceOrigin("slot-1", "host", "chassis1"),
        ResourceOrigin("slot-2", "host", "chassis2"),
    ]
    assert required_resources(_three_level_lab()) == {"chassis-1", "rig-pdu", "slot-1", "slot-2"}


def test_host_ids_selects_the_fleet_in_play_and_none_means_all():
    lab = _three_level_lab()
    assert required_resources(lab, host_ids=["chassis1"]) == {"rig-pdu", "chassis-1", "slot-1"}
    assert required_resources(lab, host_ids=["gw"]) == {"rig-pdu"}
    assert required_resources(lab, host_ids=[]) == {"rig-pdu"}
    assert required_resources(lab, host_ids=None) == required_resources(lab)


def test_same_identifier_at_two_levels_is_one_requirement_with_two_origins():
    lab = _three_level_lab()
    lab.hosts["gw"].resources = frozenset({"rig-pdu"})
    origins = [o for o in required_resource_origins(lab) if o.resource == "rig-pdu"]
    assert origins == [
        ResourceOrigin("rig-pdu", "lab", "rig"),
        ResourceOrigin("rig-pdu", "host", "gw"),
    ]
    assert required_resources(lab) == {"chassis-1", "rig-pdu", "slot-1", "slot-2"}


def test_an_unknown_host_id_is_a_value_error_naming_it():
    with pytest.raises(ValueError, match=r"not in lab 'rig': \['ghost'\]"):
        required_resources(_three_level_lab(), host_ids=["chassis1", "ghost"])


def test_missing_error_names_each_origin_and_holder():
    lab = _three_level_lab()
    backend = _FakeBackend(
        owners={"rig-pdu": "chris", "chassis-1": "chris", "slot-1": "chris", "slot-2": "dana"}
    )
    check_reservations(lab, "chris", backend, host_ids=["chassis1"])  # slot-2 is not in play
    with pytest.raises(MissingReservationError) as info:
        check_reservations(lab, "chris", backend)
    text = str(info.value)
    assert "does not hold all resources required by lab 'rig'" in text
    assert "slot-2" in text
    assert "host chassis2" in text
    assert "held by: dana" in text
    assert "slot-1" not in text
    backend.owners.pop("rig-pdu")
    with pytest.raises(MissingReservationError, match=r"rig-pdu\s+lab rig\s+\(held by: nobody\)"):
        check_reservations(lab, "chris", backend, host_ids=["gw"])


def test_the_null_backend_does_not_suppress_the_unknown_host_id_bug():
    """``backend = "none"`` is no scheduler, not a licence to skip the walk.

    The unknown-``host_ids`` ``ValueError`` (spec §4) is a BUG detector, and a
    deployment with no backend configured is exactly where a broken lab file
    would otherwise sit unnoticed longest. The contract the move must not break
    is "never queried": a ``NullReservationBackend`` still answers nothing here.

    Red at HEAD (``if is_null_backend(backend): return`` above the walk):
    ``check_reservations`` returned ``None`` and said nothing.
    """
    lab = _three_level_lab()
    with pytest.raises(ValueError, match=r"not in lab 'rig': \['ghost'\]"):
        check_reservations(lab, "chris", NullReservationBackend(), host_ids=["ghost"])


def test_message_padding_aligns_the_level_column_for_different_length_resources():
    """``width`` is computed once over ALL missing resources, not per line —
    a short and a long resource name must still line up at the level column."""
    lab = _lab_declaring("a", "much-longer-name")
    backend = _FakeBackend(owners={})
    with pytest.raises(MissingReservationError) as exc_info:
        check_reservations(lab, "alice", backend)
    lines = str(exc_info.value).splitlines()
    # width = len("much-longer-name") == 16, so "a" pads to 15 trailing spaces.
    assert "  a" + " " * 15 + "  lab test_lab  (held by: nobody)" in lines
    assert "  much-longer-name  lab test_lab  (held by: nobody)" in lines


def test_a_single_instance_element_renders_its_bare_name():
    gw = make_host("test3")
    gw.id, gw.element = "gw", Element("gw", resources=frozenset({"gw-lock"}))
    lab = Lab(name="rig", hosts={"gw": gw})
    assert ResourceOrigin("gw-lock", "element", "gw") in required_resource_origins(lab)


def test_the_element_owner_is_the_slug_not_the_written_name():
    """The owner label is ``element.slug``, and here that differs from ``name``.

    Every other element in this file (``chassis``, ``gw``) is spelled in a form
    its own slug reproduces, so ``owner = element.name`` reads identically at
    all of them and would survive. ``Chassis A`` is the shape that separates
    the two: its slug is ``chassis-a``, and the owner column is a correlation
    key — the same token the host ids of that element are prefixed with.
    """
    cab = make_host("test3")
    cab.id = "cab"
    cab.element = Element("Chassis A", resources=frozenset({"chassis-a-lock"}))
    lab = Lab(name="rig", hosts={"cab": cab})
    origins = required_resource_origins(lab)
    assert ResourceOrigin("chassis-a-lock", "element", "chassis-a") in origins
