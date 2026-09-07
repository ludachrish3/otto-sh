"""Structural tests for the backend contract and its optional capabilities."""

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from otto.reservations import (
    Reservation,
    ReservationBackend,
    SupportsResourceHolders,
    SupportsUsernameCompletion,
)


class _TwoMethodBackend:
    """The whole required contract: fetch_reservations + backend_name."""

    def fetch_reservations(self, username, start=None, end=None):
        return [Reservation(user=username, resource="rack3")]

    def backend_name(self):
        return "two-method"


def test_two_methods_satisfy_the_backend_protocol():
    assert isinstance(_TwoMethodBackend(), ReservationBackend)


def test_a_backend_missing_fetch_reservations_does_not_satisfy_it():
    class B:
        def backend_name(self):
            return "nameless"

    assert not isinstance(B(), ReservationBackend)


def test_the_required_contract_does_not_include_holders():
    """``holders`` is a capability — the required Protocol must not imply it."""
    assert not isinstance(_TwoMethodBackend(), SupportsResourceHolders)


def test_class_with_holders_satisfies_the_capability():
    class B(_TwoMethodBackend):
        def holders(self, resource):
            return [Reservation(user="alice", resource=resource)]

    assert isinstance(B(), SupportsResourceHolders)


def test_class_with_list_usernames_satisfies():
    class B:
        def list_usernames(self):
            return ["alice"]

    assert isinstance(B(), SupportsUsernameCompletion)


def test_class_without_list_usernames_does_not():
    class B:
        pass

    assert not isinstance(B(), SupportsUsernameCompletion)


def test_reservation_is_frozen():
    r = _TwoMethodBackend().fetch_reservations("alice")[0]

    with pytest.raises(FrozenInstanceError):
        r.resource = "other"  # type: ignore[misc]


def test_reservation_carries_aware_bounds_when_the_backend_knows_them():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 12, 31, tzinfo=timezone.utc)
    r = Reservation(user="alice", resource="rack3-psu", start=start, end=end)
    assert (r.start, r.end) == (start, end)
