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


def test_is_active_wholly_unbounded_is_always_active():
    """Neither bound known is a held resource, not an unusable row."""
    r = Reservation(user="alice", resource="rack3")
    assert r.is_active(now=AWARE) is True


def test_is_active_open_ended_after_start():
    """``end=None`` never expires: started an hour ago, still held."""
    r = Reservation(user="alice", resource="rack3", start=AWARE - timedelta(hours=1))
    assert r.is_active(now=AWARE) is True


def test_is_active_false_before_a_known_start():
    r = Reservation(user="alice", resource="rack3", start=AWARE + timedelta(seconds=1))
    assert r.is_active(now=AWARE) is False


def test_is_active_unknown_start_within_a_known_end():
    """``start=None`` reads as since-forever, so only the end can refuse."""
    r = Reservation(user="alice", resource="rack3", end=AWARE + timedelta(seconds=1))
    assert r.is_active(now=AWARE) is True


def test_is_active_false_after_a_known_end_with_unknown_start():
    r = Reservation(user="alice", resource="rack3", end=AWARE - timedelta(seconds=1))
    assert r.is_active(now=AWARE) is False


def test_is_active_inside_both_bounds():
    r = Reservation(
        user="alice",
        resource="rack3",
        start=AWARE - timedelta(hours=1),
        end=AWARE + timedelta(hours=1),
    )
    assert r.is_active(now=AWARE) is True


def test_is_active_at_the_start_instant_is_inclusive():
    """``start <= now``: a booking is held the moment it begins."""
    r = Reservation(user="alice", resource="rack3", start=AWARE, end=AWARE + timedelta(hours=1))
    assert r.is_active(now=AWARE) is True


def test_is_active_at_the_end_instant_is_inclusive():
    """``now <= end``: a booking is still held the moment it ends."""
    r = Reservation(user="alice", resource="rack3", start=AWARE - timedelta(hours=1), end=AWARE)
    assert r.is_active(now=AWARE) is True


def test_is_active_one_second_past_the_end_is_false():
    r = Reservation(
        user="alice",
        resource="rack3",
        start=AWARE - timedelta(hours=1),
        end=AWARE - timedelta(seconds=1),
    )
    assert r.is_active(now=AWARE) is False


def test_is_active_defaults_now_to_utc_now():
    now = datetime.now(timezone.utc)
    assert Reservation(user="alice", resource="rack3", start=now - timedelta(minutes=1)).is_active()
    assert not Reservation(
        user="alice", resource="rack3", start=now + timedelta(minutes=1)
    ).is_active()


def test_holders_capability_is_structural():
    class WithHolders:
        def holders(self, resource: str) -> "list[Reservation]":
            return []

    class WithoutHolders:
        pass

    assert isinstance(WithHolders(), SupportsResourceHolders)
    assert not isinstance(WithoutHolders(), SupportsResourceHolders)
