"""``build_report``: the one function that decides when a reservation backend is queried."""

from datetime import datetime, timedelta, timezone

import pytest

from otto.config.lab import Lab
from otto.reservations import (
    MissingReservationError,
    NullReservationBackend,
    Reservation,
    ReservationBackendBase,
    build_report,
    check_reservations,
)
from tests.conftest import make_host

NOW = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)


class _CountingBackend(ReservationBackendBase):
    """Records every query so a test can assert how many were made."""

    def __init__(self, held: "dict[str, datetime | None]", *, username="alice", others=None):
        super().__init__(username=username)
        self.held = held  # resource -> end, for this backend's own user
        self.others = dict(others or {})  # resource -> another user holding it
        self.fetches = 0
        self.holder_queries: list[str] = []

    def fetch_reservations(self, username, start=None, end=None):
        self.fetches += 1
        return [Reservation(user=username, resource=r, end=e) for r, e in self.held.items()]

    def holders(self, resource):
        self.holder_queries.append(resource)
        user = self.others.get(resource)
        return [Reservation(user=user, resource=resource)] if user else []

    def backend_name(self):
        return "counting"


class _NoHoldersBackend(ReservationBackendBase):
    """Defines no ``holders`` at all: a runtime-checkable Protocol only checks the name exists."""

    def fetch_reservations(self, username, start=None, end=None):
        return []

    def backend_name(self):
        return "no-holders"


def _lab(*resources):
    return Lab(name="lab1", resources=set(resources), hosts={"h1": make_host("test1")})


def test_nothing_required_asks_the_backend_nothing():
    backend = _CountingBackend({})
    report = build_report(_lab(), "alice", backend, host_ids=["h1"])
    assert report.rows == []
    assert report.covered
    assert backend.fetches == 0


class _CountingNullBackend(NullReservationBackend):
    """The ``none`` backend, counting the fetches it is asked for."""

    fetches = 0

    def fetch_reservations(self, username, start=None, end=None):
        self.fetches += 1
        return super().fetch_reservations(username, start, end)


def test_the_none_backend_is_never_queried_and_answers_no_held_verdict():
    backend = _CountingNullBackend(username="alice")
    report = build_report(_lab("rack1"), "alice", backend, host_ids=["h1"])
    assert backend.fetches == 0
    assert report.null_backend is True
    assert [row.held for row in report.rows] == [None]
    assert report.covered
    assert report.missing == []


def test_a_repeated_host_id_is_in_play_once():
    report = build_report(_lab("rack1"), "alice", _CountingBackend({}), host_ids=["h1", "h1"])
    assert report.in_play == ["h1"]


def test_a_bad_host_id_still_raises_under_the_none_backend():
    with pytest.raises(ValueError, match="not in lab"):
        build_report(_lab("rack1"), "alice", NullReservationBackend(), host_ids=["ghost"])


def test_one_fetch_and_holders_only_for_what_is_missing():
    backend = _CountingBackend({"rack1": None}, others={"rack2": "bob"})
    report = build_report(_lab("rack1", "rack2"), "alice", backend, host_ids=["h1"])
    assert backend.fetches == 1
    assert backend.holder_queries == ["rack2"]
    assert [(r.resource, r.held) for r in report.rows] == [("rack1", True), ("rack2", False)]
    assert [(m.resource, [h.user for h in m.holders]) for m in report.missing] == [
        ("rack2", ["bob"])
    ]
    assert not report.covered


def test_a_backend_without_the_holders_capability_reports_holders_unknown():
    backend = _NoHoldersBackend(username="alice")
    report = build_report(_lab("rack1"), "alice", backend, host_ids=["h1"])
    assert report.missing[0].holders is None


def test_expiring_lists_held_required_bookings_inside_the_window_only():
    backend = _CountingBackend(
        {"rack1": NOW + timedelta(minutes=2), "rack2": NOW + timedelta(hours=2), "spare": NOW}
    )
    report = build_report(_lab("rack1", "rack2"), "alice", backend, host_ids=["h1"], now=NOW)
    assert [r.resource for r in report.expiring] == ["rack1"]


def test_a_backend_built_for_another_user_refuses_rather_than_reporting_everything_missing():
    backend = _CountingBackend({"rack1": None}, username="alice")
    with pytest.raises(RuntimeError, match="these must agree"):
        build_report(_lab("rack1"), "bob", backend, host_ids=["h1"])


def test_check_reservations_raises_from_the_report_with_todays_text():
    backend = _CountingBackend({}, others={"rack1": "bob"})
    with pytest.raises(MissingReservationError) as exc:
        check_reservations(_lab("rack1"), "alice", backend, host_ids=["h1"])
    assert str(exc.value) == (
        "User 'alice' does not hold all resources required by lab 'lab1'. Missing:\n"
        "  rack1  lab lab1  (held by: bob)"
    )
    assert exc.value.report is not None
    assert exc.value.report.missing[0].resource == "rack1"


def test_a_missing_reservation_error_still_takes_a_plain_message():
    err = MissingReservationError("custom")
    assert str(err) == "custom"
    assert err.report is None


def test_check_reservations_returns_the_report_when_covered():
    backend = _CountingBackend({"rack1": None})
    report = check_reservations(_lab("rack1"), "alice", backend, host_ids=["h1"])
    assert report.covered
    assert [(r.resource, r.held) for r in report.rows] == [("rack1", True)]
