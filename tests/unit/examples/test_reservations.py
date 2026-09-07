"""Behavior + conformance for the ExampleReservationBackend reference backend."""

from otto.examples.reservations import ExampleReservationBackend
from otto.reservations import (
    SupportsResourceHolders,
    SupportsUsernameCompletion,
    register_reservation_backend,
)
from otto.reservations.registry import RESERVATION_BACKENDS
from otto.testing import assert_reservation_backend_conforms


def test_backend_name_stable():
    backend = ExampleReservationBackend()
    assert backend.backend_name() == "example"
    assert backend.backend_name() == backend.backend_name()


def test_fetch_reservations_returns_the_users_rows():
    backend = ExampleReservationBackend()
    rows = backend.fetch_reservations("alice")
    assert [(r.user, r.resource) for r in rows] == [("alice", "lab-a"), ("alice", "shared")]
    assert backend.fetch_reservations("nobody") == []


def test_rows_are_open_ended_not_sentinel_dated():
    # The demo dataset records no times, so every row says so with None on
    # both bounds -- never an epoch or a far-future stand-in.
    for row in ExampleReservationBackend().fetch_reservations("alice"):
        assert row.start is None
        assert row.end is None


def test_reservations_member_queries_the_constructed_username():
    backend = ExampleReservationBackend(username="alice")
    assert [r.resource for r in backend.reservations] == ["lab-a", "shared"]


def test_holders_multi_holder_sorted():
    backend = ExampleReservationBackend()
    # "shared" is held by both alice and bob -- deterministic, deduped.
    assert [h.user for h in backend.holders("shared")] == ["alice", "bob"]
    assert [h.user for h in backend.holders("lab-a")] == ["alice"]
    assert backend.holders("unheld") == []


def test_implements_both_optional_capabilities():
    backend = ExampleReservationBackend()
    assert isinstance(backend, SupportsUsernameCompletion)
    assert isinstance(backend, SupportsResourceHolders)
    assert backend.list_usernames() == ["alice", "bob"]


def test_custom_dataset_overrides_demo():
    backend = ExampleReservationBackend(reservations={"carol": ["x"]})
    assert backend.list_usernames() == ["carol"]
    assert [h.user for h in backend.holders("x")] == ["carol"]


def test_accepts_url_for_factory_uniformity():
    # build_backend may call cls(url=url, **kwargs).
    backend = ExampleReservationBackend(url="https://example")
    assert backend.backend_name() == "example"


def test_sample_conforms_with_round_trip_and_capability():
    assert_reservation_backend_conforms(
        ExampleReservationBackend(username="alice"),
        known_user="alice",
        known_resources=["lab-a", "shared"],
    )


def test_registrable_by_name():
    register_reservation_backend("example-reservations-test", ExampleReservationBackend)
    try:
        assert RESERVATION_BACKENDS.get("example-reservations-test") is ExampleReservationBackend
    finally:
        RESERVATION_BACKENDS.unregister("example-reservations-test")
