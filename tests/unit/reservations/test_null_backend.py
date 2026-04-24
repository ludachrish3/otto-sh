"""Unit tests for the null reservation backend and its no-op behavior in the check."""

from otto.configmodule.lab import Lab
from otto.reservations import (
    NullReservationBackend,
    check_reservations,
)

from tests.unit.conftest import make_host


def test_empty_resources():
    backend = NullReservationBackend()
    assert backend.get_reserved_resources("anyone") == set()


def test_no_holder():
    backend = NullReservationBackend()
    assert backend.who_reserved("any-resource") is None


def test_backend_name():
    assert NullReservationBackend().backend_name() == "none"


def test_check_reservations_is_noop_with_null_backend():
    """Even when the lab has required resources, the null backend skips the check."""
    lab = Lab(name="test_lab", resources={"rack1"}, hosts={"carrot_seed": make_host("carrot", resources={"carrot"})})
    # Should not raise — the null backend short-circuits the check.
    check_reservations(lab, username="alice", backend=NullReservationBackend())
