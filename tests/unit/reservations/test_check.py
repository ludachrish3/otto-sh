"""Unit tests for the reservation check.

The library-facing gate (:class:`~otto.reservations.ReservationGate` and its
``evaluate()`` outcome matrix) is tested separately in ``test_gate.py``.
"""

from dataclasses import dataclass

import pytest

from otto.config.lab import Lab
from otto.reservations import (
    MissingReservationError,
    NullReservationBackend,
    check_reservations,
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
    declaration and nothing else. Since lab-definition v2 a host carries no
    ``resources`` field to contribute from, so this guards the shape rather
    than a filter.
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
        """Hosts contribute nothing: the lab is the reservable unit (spec §8.1)."""
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
        assert "held by bob" in msg
        assert "unreserved" in msg

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
        assert "held by alice, bob" in str(exc_info.value)
