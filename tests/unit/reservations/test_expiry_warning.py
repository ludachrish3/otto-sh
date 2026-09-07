"""Unit tests for the five-minute reservation-expiry warning.

The warning lives in :func:`~otto.reservations.check.warn_expiring_reservations`
rather than inside ``check_reservations`` because that function has THREE call
sites — the preamble gate, ``otto reservation check``, and the out-of-fleet
named-host check in ``otto.cli.host`` — and every one of them requires the
lab-level resources. One implementation inside it would announce a lapsing
lab-level booking twice on any run that names an out-of-fleet host.

The helper never fetches: each caller passes rows it has already got, behind
that caller's own null-backend and empty-requirement short-circuits, which is
what keeps a lab needing no reservation usable while the scheduler is down.
"""

import os
import time
from datetime import datetime, timedelta, timezone

import pytest

from otto.reservations import Reservation
from otto.reservations.check import (
    EXPIRY_WARNING_WINDOW,
    reset_expiry_warnings,
    warn_expiring_reservations,
)

NOW = datetime(2026, 9, 7, 15, 0, tzinfo=timezone.utc)
NEEDED = {"rack3"}


def _rows(**kw):
    return [Reservation(user="alice", resource="rack3", **kw)]


def test_warns_at_four_minutes_fifty_nine(caplog):
    """Inside the window by one second: the boundary's near side."""
    with caplog.at_level("WARNING"):
        warn_expiring_reservations(
            _rows(end=NOW + timedelta(minutes=4, seconds=59)), NEEDED, now=NOW
        )
    assert "rack3" in caplog.text
    assert "expires" in caplog.text.lower()


def test_does_not_warn_at_five_minutes_one_second(caplog):
    """Outside it by one second. Paired with the test above, this pins the threshold."""
    with caplog.at_level("WARNING"):
        warn_expiring_reservations(
            _rows(end=NOW + timedelta(minutes=5, seconds=1)), NEEDED, now=NOW
        )
    assert caplog.text == ""


def test_does_not_warn_for_open_ended_reservation(caplog):
    """``end is None`` is a booking with nothing to lapse, not an imminent one."""
    with caplog.at_level("WARNING"):
        warn_expiring_reservations(_rows(end=None), NEEDED, now=NOW)
    assert caplog.text == ""


def test_does_not_warn_for_a_resource_this_run_does_not_need(caplog):
    """Scope is the resources that would reject the next command."""
    rows = [
        Reservation(user="alice", resource="unrelated", end=NOW + timedelta(minutes=1)),
    ]
    with caplog.at_level("WARNING"):
        warn_expiring_reservations(rows, NEEDED, now=NOW)
    assert caplog.text == ""


def test_already_expired_row_warns(caplog):
    """A booking whose end has passed is the most urgent case, not one to skip."""
    with caplog.at_level("WARNING"):
        warn_expiring_reservations(_rows(end=NOW - timedelta(seconds=1)), NEEDED, now=NOW)
    assert "rack3" in caplog.text


def test_an_already_expired_row_reports_zero_minutes_not_a_negative_one(caplog):
    """The countdown floors at zero: "expires in -1 minute(s)" is nonsense to read."""
    with caplog.at_level("WARNING"):
        warn_expiring_reservations(_rows(end=NOW - timedelta(minutes=3)), NEEDED, now=NOW)
    assert "expires in 0 minute(s)" in caplog.text


def test_window_is_five_minutes():
    assert timedelta(minutes=5) == EXPIRY_WARNING_WINDOW


def test_the_same_booking_is_announced_only_once(caplog):
    """The suppression set is what stops two call sites double-announcing one rack.

    Mutation: drop the ``_warned_expiring`` guard from the helper and the
    second call logs again, taking the count to 2.
    """
    rows = _rows(end=NOW + timedelta(minutes=1))
    with caplog.at_level("WARNING"):
        warn_expiring_reservations(rows, NEEDED, now=NOW)
        warn_expiring_reservations(rows, NEEDED, now=NOW)
    assert caplog.text.count("rack3") == 1


def test_a_different_end_for_the_same_resource_is_a_new_warning(caplog):
    """Suppression keys on ``(resource, end)``: a re-booked rack is news again."""
    with caplog.at_level("WARNING"):
        warn_expiring_reservations(_rows(end=NOW + timedelta(minutes=1)), NEEDED, now=NOW)
        warn_expiring_reservations(_rows(end=NOW + timedelta(minutes=2)), NEEDED, now=NOW)
    assert caplog.text.count("rack3") == 2


def test_reset_clears_the_suppression_set(caplog):
    """The fixture's own mechanism, exercised directly."""
    rows = _rows(end=NOW + timedelta(minutes=1))
    with caplog.at_level("WARNING"):
        warn_expiring_reservations(rows, NEEDED, now=NOW)
        reset_expiry_warnings()
        warn_expiring_reservations(rows, NEEDED, now=NOW)
    assert caplog.text.count("rack3") == 2


def test_warnings_are_ordered_by_soonest_end(caplog):
    """Sorted by ``(end, resource)`` so the most urgent line is read first."""
    rows = [
        Reservation(user="alice", resource="late", end=NOW + timedelta(minutes=4)),
        Reservation(user="alice", resource="soon", end=NOW + timedelta(minutes=1)),
    ]
    with caplog.at_level("WARNING"):
        warn_expiring_reservations(rows, {"late", "soon"}, now=NOW)
    assert caplog.text.index("soon") < caplog.text.index("late")


def test_the_suppression_set_leaks_nothing_between_tests():
    """Guards the autouse fixture in ``tests/conftest.py``.

    Every test above warns about ``rack3``; without the fixture this one
    would see a set that is already populated.
    """
    from otto.reservations import check as check_mod

    assert check_mod._warned_expiring == set()


@pytest.mark.parametrize("needed", [set(), {"other"}])
def test_an_empty_or_disjoint_requirement_warns_about_nothing(caplog, needed):
    with caplog.at_level("WARNING"):
        warn_expiring_reservations(_rows(end=NOW + timedelta(seconds=1)), needed, now=NOW)
    assert caplog.text == ""


@pytest.fixture
def far_east_timezone():
    """Run the test at UTC+14, where a local rendering can never equal a UTC one.

    Without this the local-time guard is VACUOUS on any machine whose clock is
    already UTC — CI's, and this VM's — because ``.astimezone()`` is then a
    no-op and dropping it changes nothing. Kiritimati is the largest offset
    there is and has no DST, so the rendered clock time is stable.

    ``TZ`` carries no ``OTTO_`` prefix, so the suite's ambient-env strip leaves
    it alone; ``tzset()`` is what makes ``time``/``datetime`` re-read it, and it
    has to run again on the way out or the new zone leaks into every later test
    in this process.
    """
    previous = os.environ.get("TZ")
    os.environ["TZ"] = "Pacific/Kiritimati"
    time.tzset()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


def test_the_warning_renders_the_end_in_the_viewers_local_zone(caplog, far_east_timezone):
    """The clock time is the reader's, not the backend's.

    Every JSON ``expires`` is normalised to UTC and other schedulers answer in
    whatever zone they were configured with. ``%H:%M`` carries no offset, so an
    unconverted time reads as local and is simply wrong — the user hands the
    hardware back before their booking has actually lapsed.

    Mutation: drop ``.astimezone()`` from the warning and this reads 15:03.
    """
    end = NOW + timedelta(minutes=3)  # 15:03 UTC, 05:03 the next day at UTC+14
    with caplog.at_level("WARNING"):
        warn_expiring_reservations(_rows(end=end), NEEDED, now=NOW)
    assert "at 05:03" in caplog.text, caplog.text
