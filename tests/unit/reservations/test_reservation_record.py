from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

from otto.reservations import Reservation, SupportsResourceHolders

AWARE = datetime(2026, 9, 7, 15, 0, tzinfo=timezone.utc)


def test_times_default_to_none():
    r = Reservation(user="alice", resource="rack3")
    assert r.start is None
    assert r.end is None


def test_is_frozen():
    r = Reservation(user="alice", resource="rack3")
    with pytest.raises(FrozenInstanceError):
        r.user = "bob"  # type: ignore[misc]


def test_expires_within_true_inside_window():
    r = Reservation(user="alice", resource="rack3", end=AWARE + timedelta(minutes=4, seconds=59))
    assert r.expires_within(timedelta(minutes=5), now=AWARE) is True


def test_expires_within_false_outside_window():
    r = Reservation(user="alice", resource="rack3", end=AWARE + timedelta(minutes=5, seconds=1))
    assert r.expires_within(timedelta(minutes=5), now=AWARE) is False


def test_expires_within_false_when_open_ended():
    """end=None means never expires — it must never warn."""
    r = Reservation(user="alice", resource="rack3", end=None)
    assert r.expires_within(timedelta(minutes=5), now=AWARE) is False


def test_expires_within_true_when_already_past():
    """A row otto is still holding whose end has passed is maximally urgent."""
    r = Reservation(user="alice", resource="rack3", end=AWARE - timedelta(seconds=1))
    assert r.expires_within(timedelta(minutes=5), now=AWARE) is True


def test_expires_within_defaults_now_to_utc_now():
    r = Reservation(
        user="alice",
        resource="rack3",
        end=datetime.now(timezone.utc) + timedelta(minutes=1),
    )
    assert r.expires_within(timedelta(minutes=5)) is True


def test_holders_capability_is_structural():
    class WithHolders:
        def holders(self, resource: str) -> "list[Reservation]":
            return []

    class WithoutHolders:
        pass

    assert isinstance(WithHolders(), SupportsResourceHolders)
    assert not isinstance(WithoutHolders(), SupportsResourceHolders)
