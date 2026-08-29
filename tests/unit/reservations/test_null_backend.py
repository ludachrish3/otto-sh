"""Unit tests for the null reservation backend and its no-op behavior in the check."""

from otto.config.lab import Lab
from otto.reservations import (
    NullReservationBackend,
    check_reservations,
    is_null_backend,
)
from tests.conftest import make_host


class _RealBackend:
    """A backend that DOES track reservations — the other side of the predicate."""

    def get_reserved_resources(self, username: str) -> set[str]:
        return {"rack1"}

    def who_reserved(self, resource: str) -> list[str]:
        return ["alice"]

    def backend_name(self) -> str:
        return "real"


def test_empty_resources():
    backend = NullReservationBackend()
    assert backend.get_reserved_resources("anyone") == set()


def test_no_holder():
    backend = NullReservationBackend()
    assert backend.who_reserved("any-resource") == []


def test_backend_name():
    assert NullReservationBackend().backend_name() == "none"


def test_check_reservations_is_noop_with_null_backend():
    """Even when the lab has required resources, the null backend skips the check."""
    lab = Lab(
        name="test_lab",
        resources={"rack1"},
        hosts={"test1": make_host("test1")},
    )
    # Should not raise — the null backend short-circuits the check.
    check_reservations(lab, username="alice", backend=NullReservationBackend())


def test_is_null_backend_tells_the_two_apart():
    """THE shared predicate: ``check_reservations`` and ``otto reservation check`` both read it.

    Pinned here rather than at each caller because the whole point is that
    there is one answer. ``tests/unit/cli/test_reservation.py::
    test_check_table_renders_n_a_under_the_null_backend`` and
    :func:`test_check_reservations_is_noop_with_null_backend` above both go red
    together if this function stops telling them apart.
    """
    assert is_null_backend(NullReservationBackend()) is True
    assert is_null_backend(_RealBackend()) is False
